"""Bounded clean Primary artwork discovery for Jellyfin."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import ipaddress
import json
import re
import time
import warnings
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

import config as _cfg
from cache import (
    get_cached_artwork_candidates,
    get_cached_text_detection,
    set_cached_artwork_candidates,
    set_cached_text_detection,
)
from text_detect import DETECT_RES_SIG, poster_has_burned_in_text, text_detection_ready


SCHEMA_VERSION = 1
MAX_BODY_BYTES = 4096
MAX_ENUMERATED_PER_SOURCE = 8
MAX_OCR_ATTEMPTS = 16
MAX_RESULTS = 8
SEARCH_DEADLINE_SECONDS = 20.0
MAX_INFLIGHT_SEARCHES = 2
MAX_SEARCH_STARTS_PER_MINUTE = 30
METADATA_MAX_BYTES = 2 * 1024 * 1024
MIN_PORTRAIT_ASPECT = 0.55
MAX_PORTRAIT_ASPECT = 0.80
OCR_POLICY_REVISION = "clean-primary-v1"
RANKING_REVISION = "source-alternation-v1"
RESULT_CACHE_TTL_SECONDS = 24 * 60 * 60
PARTIAL_CACHE_TTL_SECONDS = 15 * 60

_TMDB_ID_RE = re.compile(r"^[1-9]\d{0,9}$")
_TVDB_ID_RE = re.compile(r"^[1-9]\d{0,9}$")
_IMDB_ID_RE = re.compile(r"^tt\d{1,10}$")
_TMDB_FILE_RE = re.compile(r"^/[A-Za-z0-9_-]+\.(?:jpe?g|png|webp)$", re.IGNORECASE)
_FANART_FILE_RE = re.compile(r"^[A-Za-z0-9_-]+\.(?:jpe?g|png|webp)$", re.IGNORECASE)
_IMAGE_CONTENT_TYPES = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
}


class CandidateDiscoveryError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class _SourceFailure(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _CandidateRejected(Exception):
    pass


class _DetectionUnavailable(Exception):
    pass


@dataclass(frozen=True)
class CandidateRequest:
    media_type: str
    tmdb_id: str
    imdb_id: str
    tvdb_id: str | None


@dataclass(frozen=True)
class SourceCandidate:
    source: str
    url: str
    width: int
    height: int
    language: str | None
    rating: float = 0.0
    votes: int = 0
    likes: int = 0
    ordinal: int = 0
    sha256: str | None = None

    def response_value(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "url": self.url,
            "width": self.width,
            "height": self.height,
            "language": self.language,
        }


@dataclass(frozen=True)
class _Identity:
    titles: tuple[str, ...]
    tvdb_verified: bool


@dataclass(frozen=True)
class DiscoverySettings:
    tmdb_api_key: str
    fanart_project_api_key: str
    fanart_client_key: str
    max_image_bytes: int
    max_width: int
    max_height: int
    max_pixels: int
    ocr_box_threshold: float

    @classmethod
    def from_config(cls) -> "DiscoverySettings":
        return cls(
            tmdb_api_key=_cfg.SERVER_TMDB_KEY,
            fanart_project_api_key=_cfg.JELLYFIN_ARTWORK_FANART_PROJECT_API_KEY,
            fanart_client_key=_cfg.JELLYFIN_ARTWORK_FANART_CLIENT_KEY,
            max_image_bytes=_cfg.SELECTED_MAX_BYTES,
            max_width=_cfg.SELECTED_MAX_WIDTH,
            max_height=_cfg.SELECTED_MAX_HEIGHT,
            max_pixels=_cfg.SELECTED_MAX_PIXELS,
            ocr_box_threshold=_cfg.PPOCR_BOX_THRESHOLD,
        )

    @property
    def fanart_enabled(self) -> bool:
        return bool(self.fanart_project_api_key and self.fanart_client_key)


class _SearchAdmission:
    def __init__(self) -> None:
        self._lock: asyncio.Lock | None = None
        self._active = 0
        self._starts: deque[float] = deque()

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def start(self, now: float | None = None) -> str | None:
        current = time.monotonic() if now is None else now
        async with self._get_lock():
            while self._starts and current - self._starts[0] >= 60.0:
                self._starts.popleft()
            if len(self._starts) >= MAX_SEARCH_STARTS_PER_MINUTE:
                return "rate_limited"
            if self._active >= MAX_INFLIGHT_SEARCHES:
                return "concurrency_limited"
            self._starts.append(current)
            self._active += 1
            return None

    async def finish(self) -> None:
        async with self._get_lock():
            self._active = max(0, self._active - 1)

    def reset_for_tests(self) -> None:
        self._lock = None
        self._active = 0
        self._starts.clear()


_search_admission = _SearchAdmission()
_ocr_executor: ThreadPoolExecutor | None = None


def _get_ocr_executor() -> ThreadPoolExecutor:
    global _ocr_executor
    if _ocr_executor is None:
        _ocr_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="candidate-ocr",
        )
    return _ocr_executor


def shutdown_candidate_ocr_executor() -> None:
    global _ocr_executor
    if _ocr_executor is not None:
        _ocr_executor.shutdown(wait=True, cancel_futures=True)
        _ocr_executor = None


def discovery_key_matches(candidate: str) -> bool:
    configured = _cfg.JELLYFIN_ARTWORK_DISCOVERY_KEY
    if not configured:
        return False
    if _cfg.ACCESS_KEY and hmac.compare_digest(configured, _cfg.ACCESS_KEY):
        return False
    try:
        return hmac.compare_digest(
            candidate.encode("utf-8"),
            configured.encode("utf-8"),
        )
    except UnicodeEncodeError:
        return False


def decode_request_body(body: bytes) -> CandidateRequest:
    def _strict_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise CandidateDiscoveryError(
                    400,
                    "duplicate_field",
                    f"Duplicate JSON field: {key}",
                )
            result[key] = value
        return result

    try:
        value = json.loads(body, object_pairs_hook=_strict_object)
    except CandidateDiscoveryError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateDiscoveryError(400, "invalid_json", "Malformed JSON body") from exc

    base_fields = {"schema_version", "type", "tmdb_id", "imdb_id"}
    if not isinstance(value, dict) or set(value) not in (
        base_fields,
        base_fields | {"tvdb_id"},
    ):
        raise CandidateDiscoveryError(
            400,
            "invalid_schema",
            "Body must contain exactly schema_version, type, tmdb_id, imdb_id, and optional tvdb_id",
        )
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise CandidateDiscoveryError(
            400,
            "unsupported_schema",
            "schema_version must be 1",
        )
    if value["type"] not in ("movie", "series"):
        raise CandidateDiscoveryError(400, "invalid_type", "type must be movie or series")
    for field in ("tmdb_id", "imdb_id"):
        if not isinstance(value[field], str):
            raise CandidateDiscoveryError(400, "invalid_schema", f"{field} must be a string")
    if not _TMDB_ID_RE.fullmatch(value["tmdb_id"]):
        raise CandidateDiscoveryError(400, "invalid_tmdb_id", "tmdb_id is malformed")
    if not _IMDB_ID_RE.fullmatch(value["imdb_id"]):
        raise CandidateDiscoveryError(400, "invalid_imdb_id", "imdb_id is malformed")
    tvdb_id = value.get("tvdb_id")
    if tvdb_id is not None:
        if not isinstance(tvdb_id, str) or not _TVDB_ID_RE.fullmatch(tvdb_id):
            raise CandidateDiscoveryError(400, "invalid_tvdb_id", "tvdb_id is malformed")
    return CandidateRequest(
        media_type=value["type"],
        tmdb_id=value["tmdb_id"],
        imdb_id=value["imdb_id"],
        tvdb_id=tvdb_id,
    )


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, parsed)


def _safe_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return default
    return parsed


def _is_portrait(width: int, height: int) -> bool:
    if width <= 0 or height <= 0 or width >= height:
        return False
    aspect = width / height
    return MIN_PORTRAIT_ASPECT <= aspect <= MAX_PORTRAIT_ASPECT


def _validate_base_url(url: str, expected_host: str) -> str:
    if not isinstance(url, str) or len(url) > 2048:
        raise _CandidateRejected()
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise _CandidateRejected() from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected_host
        or parsed.netloc not in (expected_host, f"{expected_host}:443")
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        raise _CandidateRejected()
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        pass
    else:
        raise _CandidateRejected()
    return parsed.path


def canonical_tmdb_url(file_path: str) -> str:
    if not isinstance(file_path, str) or not _TMDB_FILE_RE.fullmatch(file_path):
        raise _CandidateRejected()
    url = f"https://image.tmdb.org/t/p/original{file_path}"
    path = _validate_base_url(url, "image.tmdb.org")
    if not path.startswith("/t/p/original/") or path.count("/") != 4:
        raise _CandidateRejected()
    return url


def canonical_fanart_url(
    url: str,
    *,
    media_type: str,
    tmdb_id: str,
    tvdb_id: str | None,
) -> str:
    path = _validate_base_url(url, "assets.fanart.tv")
    if media_type == "movie":
        prefix = f"/fanart/movies/{tmdb_id}/movieposter/"
    else:
        if tvdb_id is None:
            raise _CandidateRejected()
        prefix = f"/fanart/tv/{tvdb_id}/tvposter/"
    if not path.startswith(prefix):
        raise _CandidateRejected()
    filename = path[len(prefix):]
    if "/" in filename or not _FANART_FILE_RE.fullmatch(filename):
        raise _CandidateRejected()
    return f"https://assets.fanart.tv{prefix}{filename}"


async def _bounded_response_bytes(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, str] | None,
    max_bytes: int,
) -> tuple[bytes, str]:
    try:
        async with client.stream(
            "GET",
            url,
            params=params,
            follow_redirects=False,
        ) as response:
            if 300 <= response.status_code < 400:
                raise _SourceFailure("redirect")
            if response.status_code != 200:
                raise _SourceFailure("upstream_status")
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    declared_length = int(content_length)
                    if declared_length < 0:
                        raise _SourceFailure("malformed_response")
                    if declared_length > max_bytes:
                        raise _SourceFailure("response_too_large")
                except ValueError as exc:
                    raise _SourceFailure("malformed_response") from exc
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > max_bytes:
                    raise _SourceFailure("response_too_large")
            return bytes(body), response.headers.get("content-type", "")
    except _SourceFailure:
        raise
    except (httpx.HTTPError, OSError) as exc:
        raise _SourceFailure("transport") from exc


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, str],
) -> Any:
    body, content_type = await _bounded_response_bytes(
        client,
        url,
        params=params,
        max_bytes=METADATA_MAX_BYTES,
    )
    if content_type.split(";", 1)[0].strip().lower() not in (
        "application/json",
        "text/json",
        "",
    ):
        raise _SourceFailure("invalid_content_type")
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _SourceFailure("malformed_response") from exc


async def _fetch_identity(
    client: httpx.AsyncClient,
    request: CandidateRequest,
    settings: DiscoverySettings,
) -> _Identity:
    if not settings.tmdb_api_key:
        raise CandidateDiscoveryError(
            503,
            "sources_unavailable",
            "Artwork discovery sources are unavailable",
        )
    endpoint = "movie" if request.media_type == "movie" else "tv"
    try:
        payload = await _get_json(
            client,
            f"https://api.themoviedb.org/3/{endpoint}/{request.tmdb_id}",
            {
                "api_key": settings.tmdb_api_key,
                "append_to_response": "external_ids",
            },
        )
    except _SourceFailure as exc:
        raise CandidateDiscoveryError(
            503,
            "identity_unavailable",
            "Provider identity verification is unavailable",
        ) from exc
    if not isinstance(payload, dict) or _safe_int(payload.get("id")) != int(request.tmdb_id):
        raise CandidateDiscoveryError(
            503,
            "identity_unavailable",
            "Provider identity verification returned malformed data",
        )
    external_ids = payload.get("external_ids")
    if not isinstance(external_ids, dict):
        raise CandidateDiscoveryError(
            503,
            "identity_unavailable",
            "Provider identity verification returned malformed data",
        )
    if external_ids.get("imdb_id") != request.imdb_id:
        raise CandidateDiscoveryError(
            400,
            "mixed_identity",
            "Provider IDs do not identify the same title",
        )
    tvdb_verified = False
    if request.tvdb_id is not None:
        external_tvdb = external_ids.get("tvdb_id")
        if _safe_int(external_tvdb) != int(request.tvdb_id):
            raise CandidateDiscoveryError(
                400,
                "mixed_identity",
                "Provider IDs do not identify the same title",
            )
        tvdb_verified = True
    title_fields = (
        ("title", "original_title")
        if request.media_type == "movie"
        else ("name", "original_name")
    )
    titles = tuple(
        dict.fromkeys(
            value.strip()
            for field in title_fields
            if isinstance((value := payload.get(field)), str) and value.strip()
        )
    )
    if not titles:
        raise CandidateDiscoveryError(
            503,
            "identity_unavailable",
            "Provider identity verification returned malformed data",
        )
    return _Identity(titles=titles, tvdb_verified=tvdb_verified)


async def _tmdb_candidates(
    client: httpx.AsyncClient,
    request: CandidateRequest,
    settings: DiscoverySettings,
) -> list[SourceCandidate]:
    endpoint = "movie" if request.media_type == "movie" else "tv"
    payload = await _get_json(
        client,
        f"https://api.themoviedb.org/3/{endpoint}/{request.tmdb_id}/images",
        {
            "api_key": settings.tmdb_api_key,
            "include_image_language": "null",
        },
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("posters"), list):
        raise _SourceFailure("malformed_response")
    candidates = []
    for ordinal, item in enumerate(payload["posters"]):
        if not isinstance(item, dict) or item.get("iso_639_1") is not None:
            continue
        try:
            url = canonical_tmdb_url(item.get("file_path"))
        except _CandidateRejected:
            continue
        width = _safe_int(item.get("width"))
        height = _safe_int(item.get("height"))
        if not _is_portrait(width, height):
            continue
        candidates.append(
            SourceCandidate(
                source="tmdb",
                url=url,
                width=width,
                height=height,
                language=None,
                rating=_safe_float(item.get("vote_average")),
                votes=_safe_int(item.get("vote_count")),
                ordinal=ordinal,
            )
        )
    candidates.sort(
        key=lambda item: (
            -(item.width * item.height),
            -item.rating,
            -item.votes,
            item.ordinal,
        )
    )
    return candidates[:MAX_ENUMERATED_PER_SOURCE]


async def _fanart_candidates(
    client: httpx.AsyncClient,
    request: CandidateRequest,
    identity: _Identity,
    settings: DiscoverySettings,
) -> list[SourceCandidate]:
    if request.media_type == "series" and not identity.tvdb_verified:
        return []
    if request.media_type == "movie":
        path = f"movies/{request.tmdb_id}"
        field = "movieposter"
    else:
        path = f"tv/{request.tvdb_id}"
        field = "tvposter"
    payload = await _get_json(
        client,
        f"https://webservice.fanart.tv/v3/{path}",
        {
            "api_key": settings.fanart_project_api_key,
            "client_key": settings.fanart_client_key,
        },
    )
    if not isinstance(payload, dict):
        raise _SourceFailure("malformed_response")
    values = payload.get(field, [])
    if not isinstance(values, list):
        raise _SourceFailure("malformed_response")
    candidates = []
    for ordinal, item in enumerate(values):
        if not isinstance(item, dict) or item.get("lang") != "00":
            continue
        try:
            url = canonical_fanart_url(
                item.get("url"),
                media_type=request.media_type,
                tmdb_id=request.tmdb_id,
                tvdb_id=request.tvdb_id,
            )
        except _CandidateRejected:
            continue
        width = _safe_int(item.get("width"))
        height = _safe_int(item.get("height"))
        if (width or height) and not _is_portrait(width, height):
            continue
        candidates.append(
            SourceCandidate(
                source="fanart",
                url=url,
                width=width,
                height=height,
                language=None,
                likes=_safe_int(item.get("likes")),
                ordinal=ordinal,
            )
        )
    candidates.sort(
        key=lambda item: (
            -(item.width * item.height),
            -item.likes,
            item.ordinal,
        )
    )
    return candidates[:MAX_ENUMERATED_PER_SOURCE]


def _decode_screening_image(
    body: bytes,
    content_type: str,
    settings: DiscoverySettings,
) -> tuple[Image.Image, int, int]:
    media_type = content_type.split(";", 1)[0].strip().lower()
    expected_format = _IMAGE_CONTENT_TYPES.get(media_type)
    if expected_format is None:
        raise _CandidateRejected()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(body)) as probe:
                decoded_format = (probe.format or "").upper()
                width, height = probe.size
                if decoded_format != expected_format:
                    raise _CandidateRejected()
                if (
                    width > settings.max_width
                    or height > settings.max_height
                    or width * height > settings.max_pixels
                ):
                    raise _CandidateRejected()
                if not _is_portrait(width, height):
                    raise _CandidateRejected()
                probe.verify()
            with Image.open(io.BytesIO(body)) as decoded:
                decoded.load()
                image = decoded.convert("RGBA")
    except _CandidateRejected:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        ValueError,
        SyntaxError,
    ) as exc:
        raise _CandidateRejected() from exc
    return image, width, height


async def _fetch_screening_image(
    client: httpx.AsyncClient,
    candidate: SourceCandidate,
    settings: DiscoverySettings,
) -> tuple[bytes, Image.Image, int, int]:
    try:
        body, content_type = await _bounded_response_bytes(
            client,
            candidate.url,
            params=None,
            max_bytes=settings.max_image_bytes,
        )
    except _SourceFailure as exc:
        raise _CandidateRejected() from exc
    if not body:
        raise _CandidateRejected()
    image, width, height = _decode_screening_image(body, content_type, settings)
    return body, image, width, height


async def _ocr_has_text(
    image: Image.Image,
    digest: str,
    titles: tuple[str, ...],
    settings: DiscoverySettings,
) -> bool:
    cache_key = (
        f"candidate:{digest}:{DETECT_RES_SIG}:{OCR_POLICY_REVISION}:"
        f"{settings.ocr_box_threshold:.4f}"
    )
    cached = get_cached_text_detection(cache_key)
    if cached is not None:
        return cached
    if not text_detection_ready():
        raise _DetectionUnavailable()

    ocr_image = image.copy()

    def _scan() -> bool | None:
        try:
            return poster_has_burned_in_text(
                ocr_image,
                conf=settings.ocr_box_threshold,
                title=titles,
                source="poster",
                debug=False,
            )
        finally:
            ocr_image.close()

    try:
        result = await asyncio.get_running_loop().run_in_executor(
            _get_ocr_executor(),
            _scan,
        )
    except Exception as exc:
        raise _DetectionUnavailable() from exc
    if result is None:
        raise _DetectionUnavailable()
    set_cached_text_detection(cache_key, result)
    return result


async def _screen_source(
    client: httpx.AsyncClient,
    candidates: list[SourceCandidate],
    *,
    titles: tuple[str, ...],
    settings: DiscoverySettings,
    seen_digests: set[str],
    work: dict[str, int],
) -> list[SourceCandidate]:
    accepted = []
    for candidate in candidates:
        if work["ocr"] >= MAX_OCR_ATTEMPTS:
            break
        try:
            body, image, width, height = await _fetch_screening_image(
                client,
                candidate,
                settings,
            )
        except _CandidateRejected:
            continue
        digest = hashlib.sha256(body).hexdigest()
        if digest in seen_digests:
            image.close()
            continue
        seen_digests.add(digest)
        work["ocr"] += 1
        try:
            has_text = await _ocr_has_text(image, digest, titles, settings)
        finally:
            image.close()
        if has_text:
            continue
        accepted.append(
            replace(
                candidate,
                width=width,
                height=height,
                sha256=digest,
            )
        )
    if candidates and candidates[0].source == "fanart":
        accepted.sort(
            key=lambda item: (
                -(item.width * item.height),
                -item.likes,
                item.ordinal,
            )
        )
    else:
        accepted.sort(
            key=lambda item: (
                -(item.width * item.height),
                -item.rating,
                -item.votes,
                item.ordinal,
            )
        )
    return accepted


def _alternate_sources(
    tmdb: list[SourceCandidate],
    fanart: list[SourceCandidate],
) -> list[SourceCandidate]:
    result = []
    index = 0
    while len(result) < MAX_RESULTS and (index < len(tmdb) or index < len(fanart)):
        if index < len(tmdb):
            result.append(tmdb[index])
            if len(result) >= MAX_RESULTS:
                break
        if index < len(fanart):
            result.append(fanart[index])
        index += 1
    return result[:MAX_RESULTS]


def _cache_key(request: CandidateRequest, settings: DiscoverySettings) -> str:
    policy = {
        "schema_version": SCHEMA_VERSION,
        "type": request.media_type,
        "tmdb_id": request.tmdb_id,
        "imdb_id": request.imdb_id,
        "tvdb_id": request.tvdb_id,
        "sources": {
            "tmdb": bool(settings.tmdb_api_key),
            "fanart": settings.fanart_enabled,
            "fanart_series_eligible": (
                request.media_type == "movie" or request.tvdb_id is not None
            ),
        },
        "ocr": f"{DETECT_RES_SIG}:{OCR_POLICY_REVISION}:{settings.ocr_box_threshold:.4f}",
        "ranking": RANKING_REVISION,
        "bounds": {
            "bytes": settings.max_image_bytes,
            "width": settings.max_width,
            "height": settings.max_height,
            "pixels": settings.max_pixels,
            "aspect": [MIN_PORTRAIT_ASPECT, MAX_PORTRAIT_ASPECT],
        },
    }
    encoded = json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_cached_response(
    value: Any,
    request: CandidateRequest,
    settings: DiscoverySettings,
) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "sources",
        "candidates",
    }:
        return None
    if value["schema_version"] != SCHEMA_VERSION or not isinstance(value["sources"], dict):
        return None
    candidates = value["candidates"]
    if not isinstance(candidates, list) or len(candidates) > MAX_RESULTS:
        return None
    for item in candidates:
        if not isinstance(item, dict) or set(item) != {
            "source",
            "url",
            "width",
            "height",
            "language",
        }:
            return None
        try:
            if item["source"] == "tmdb":
                if canonical_tmdb_url(
                    item["url"].removeprefix(
                        "https://image.tmdb.org/t/p/original"
                    )
                ) != item["url"]:
                    return None
            elif item["source"] == "fanart":
                if canonical_fanart_url(
                    item["url"],
                    media_type=request.media_type,
                    tmdb_id=request.tmdb_id,
                    tvdb_id=request.tvdb_id,
                ) != item["url"]:
                    return None
            else:
                return None
        except _CandidateRejected:
            return None
        if (
            type(item["width"]) is not int
            or type(item["height"]) is not int
            or not _is_portrait(item["width"], item["height"])
            or item["width"] > settings.max_width
            or item["height"] > settings.max_height
            or item["width"] * item["height"] > settings.max_pixels
            or item["language"] is not None
        ):
            return None
    return value


async def discover_candidates(
    client: httpx.AsyncClient,
    request: CandidateRequest,
    *,
    settings: DiscoverySettings | None = None,
) -> tuple[dict[str, Any], str]:
    settings = settings or DiscoverySettings.from_config()
    admission_failure = await _search_admission.start()
    if admission_failure:
        raise CandidateDiscoveryError(
            429,
            admission_failure,
            "Artwork discovery capacity is temporarily exhausted",
        )
    try:
        try:
            async with asyncio.timeout(SEARCH_DEADLINE_SECONDS):
                cache_key = _cache_key(request, settings)
                cached = get_cached_artwork_candidates(
                    cache_key,
                    complete_ttl=RESULT_CACHE_TTL_SECONDS,
                    partial_ttl=PARTIAL_CACHE_TTL_SECONDS,
                )
                validated_cache = _validate_cached_response(
                    cached,
                    request,
                    settings,
                )
                if validated_cache is not None:
                    return validated_cache, "cache"
                if not text_detection_ready():
                    raise CandidateDiscoveryError(
                        503,
                        "text_detection_unavailable",
                        "Text detection is unavailable or uncertain",
                    )

                identity = await _fetch_identity(client, request, settings)
                statuses = {
                    "tmdb": "ready",
                    "fanart": (
                        "disabled"
                        if not settings.fanart_enabled
                        else (
                            "skipped"
                            if request.media_type == "series" and not identity.tvdb_verified
                            else "ready"
                        )
                    ),
                }
                tmdb_values: list[SourceCandidate] = []
                fanart_values: list[SourceCandidate] = []

                async def _load_tmdb() -> None:
                    nonlocal tmdb_values
                    try:
                        tmdb_values = await _tmdb_candidates(client, request, settings)
                    except _SourceFailure:
                        statuses["tmdb"] = "failed"

                async def _load_fanart() -> None:
                    nonlocal fanart_values
                    if statuses["fanart"] != "ready":
                        return
                    try:
                        fanart_values = await _fanart_candidates(
                            client,
                            request,
                            identity,
                            settings,
                        )
                    except _SourceFailure:
                        statuses["fanart"] = "failed"

                await asyncio.gather(_load_tmdb(), _load_fanart())
                configured_statuses = [
                    statuses["tmdb"],
                    statuses["fanart"],
                ]
                if not any(status == "ready" for status in configured_statuses):
                    raise CandidateDiscoveryError(
                        503,
                        "all_sources_failed",
                        "All configured artwork sources failed",
                    )

                seen_digests: set[str] = set()
                work = {"ocr": 0}
                try:
                    clean_tmdb = await _screen_source(
                        client,
                        tmdb_values,
                        titles=identity.titles,
                        settings=settings,
                        seen_digests=seen_digests,
                        work=work,
                    )
                    clean_fanart = await _screen_source(
                        client,
                        fanart_values,
                        titles=identity.titles,
                        settings=settings,
                        seen_digests=seen_digests,
                        work=work,
                    )
                except _DetectionUnavailable as exc:
                    raise CandidateDiscoveryError(
                        503,
                        "text_detection_unavailable",
                        "Text detection is unavailable or uncertain",
                    ) from exc

                selected = _alternate_sources(clean_tmdb, clean_fanart)
                response = {
                    "schema_version": SCHEMA_VERSION,
                    "sources": statuses,
                    "candidates": [item.response_value() for item in selected],
                }
                partial = "failed" in statuses.values()
                result_class = (
                    "partial"
                    if partial
                    else ("empty" if not selected else "complete")
                )
                set_cached_artwork_candidates(cache_key, response, result_class)
                return response, result_class
        except TimeoutError as exc:
            raise CandidateDiscoveryError(
                503,
                "deadline_exceeded",
                "Artwork discovery exceeded its deadline",
            ) from exc
    finally:
        await _search_admission.finish()
