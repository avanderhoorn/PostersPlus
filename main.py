#main.py
import asyncio
import base64
import binascii
import hashlib
import hmac
import io
import json
import logging
import os
import re
import time
import warnings
import httpx
import numpy as np
from datetime import datetime, timedelta, timezone
import zoneinfo
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, HTMLResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError
from starlette.routing import Match

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)
# Pull uvicorn's loggers into our root handler so all output shares the same format.
for _uv_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    _uv_logger = logging.getLogger(_uv_name)
    _uv_logger.handlers = []
    _uv_logger.propagate = True


class _TruncateUrlFilter(logging.Filter):
    """
    Redact API keys and truncate long URL paths in log records.

    Two responsibilities:
      1. For uvicorn.access records, truncate the request path so long URLs
         don't fill the log.
      2. For ALL records, redact every common API-key query parameter pattern
         in both record.msg and record.args.  This catches keys that slip
         through when an httpx exception is logged (its __str__ includes the
         full upstream URL with our outbound api_key=) as well as anything
         else that might inadvertently include a key.
    """
    _MAX = 80
    # Match query params we hold (tmdb_key, mdblist_key, access_key) AND the
    # upstream parameter names we forward keys under (api_key, apikey).
    _KEY_RE = re.compile(
        r'((?:tmdb_key|mdblist_key|access_key|api_key|apikey|client_key)=)[^&\s\'\"]*',
        re.IGNORECASE,
    )

    @classmethod
    def _redact(cls, value):
        if isinstance(value, str):
            return cls._KEY_RE.sub(r'\1***', value)
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn.access records: args = (client_addr, method, path, http_version, status_code, ...)
        if (
            record.name == "uvicorn.access"
            and isinstance(record.args, tuple)
            and len(record.args) >= 3
        ):
            path = record.args[2]
            if isinstance(path, str):
                path = self._KEY_RE.sub(r'\1***', path)
                if path.startswith("/v1/artwork/candidates?"):
                    path = "/v1/artwork/candidates?<redacted>"
                if len(path) > self._MAX:
                    path = path[: self._MAX] + "…"
                record.args = (record.args[0], record.args[1], path) + record.args[3:]

        # Generic redaction for every other record (application logs).
        # We redact in msg and args so the formatted output is safe regardless
        # of whether the record uses % substitution or pre-formatted strings.
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._redact(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: self._redact(v) for k, v in record.args.items()}

        # Tracebacks (logger.exception / exc_info=True) are formatted lazily
        # by the handler.  Pre-format and redact exc_text here so the
        # downstream formatter uses our sanitised copy rather than re-rendering.
        if record.exc_info and not record.exc_text:
            import traceback
            record.exc_text = self._redact(
                "".join(traceback.format_exception(*record.exc_info))
            )
        elif record.exc_text:
            record.exc_text = self._redact(record.exc_text)

        return True


# Attach to the root handler, not the root logger — propagation calls
# callHandlers() directly on parent loggers, skipping their logger-level filters.
_url_filter = _TruncateUrlFilter()
for _handler in logging.getLogger().handlers:
    _handler.addFilter(_url_filter)

# httpx logs every outbound HTTP request at INFO level, including full URLs with
# API keys in query strings.  Raise its level to WARNING so those lines are never
# written to the log — our own try/except blocks capture errors explicitly.
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request coalescing
# ---------------------------------------------------------------------------
# Maps final_cache_key -> Future[bytes] for in-flight renders.
# When multiple requests arrive simultaneously for the same uncached poster
# (common during a burst from AIOMetadata loading a library), only the first
# runs the full pipeline; the rest await its Future and get the result for free.
# This dict is per-worker-process — cross-process deduplication would require
# a shared store like Redis, but intra-process coalescing handles the common
# burst pattern well enough at this scale.
_render_inflight: dict[str, "asyncio.Future[bytes]"] = {}

# Coalesces concurrent fetch_poster_metadata calls for the same (tmdb_id,
# media_type, language) tuple.  Without this, simultaneous /poster + /logo
# requests for the same cold title each fire their own TMDB API call.
_metadata_inflight: dict[str, "asyncio.Future[tuple]"] = {}


async def _coalesced_fetch_poster_metadata(
    client: "httpx.AsyncClient",
    tmdb_id: str,
    tmdb_key: str,
    media_type: str,
    lang: str,
    secondary_lang: str = "",
) -> tuple:
    endpoint = "tv" if media_type in ("tv", "series") else "movie"
    inflight_key = tmdb_metadata_cache_key(endpoint, tmdb_id, lang, secondary_lang)

    existing = _metadata_inflight.get(inflight_key)
    if existing is not None:
        logger.debug(f"Coalescing metadata fetch for {media_type}/{tmdb_id} ({lang})")
        return await existing

    fut: "asyncio.Future[tuple]" = asyncio.get_running_loop().create_future()
    fut.add_done_callback(
        lambda f: f.exception() if not f.cancelled() and f.exception() else None
    )
    _metadata_inflight[inflight_key] = fut
    try:
        result = await fetch_poster_metadata(
            client, tmdb_id, tmdb_key, media_type, lang, secondary_lang
        )
        fut.set_result(result)
        return result
    except Exception as exc:
        if not fut.done():
            fut.set_exception(exc)
        raise
    except BaseException:
        if not fut.done():
            fut.cancel()
        raise
    finally:
        _metadata_inflight.pop(inflight_key, None)


# ---------------------------------------------------------------------------
# Background quality fetching
# ---------------------------------------------------------------------------
# Quality data (AIOStreams / scrapers) is fetched in the background so poster
# responses are never blocked by a slow scraper call.  The poster is served
# immediately without quality badges on a cache miss; the next request for the
# same title will find the quality cached and render badges normally.
#
# _quality_bg_inflight: tracks imdb_ids with an active background fetch so
#   scroll bursts don't launch duplicate fetches for the same title.
# _quality_bg_semaphore: caps concurrent AIOStreams calls so a large burst
#   doesn't hammer the scrapers with hundreds of simultaneous requests.

_quality_bg_inflight: set[str] = set()
_quality_bg_semaphore: "asyncio.Semaphore | None" = None   # created inside event loop
_quality_source_backoff_until: dict[str, float] = {}
_quality_source_fail_count: dict[str, int] = {}

# ---------------------------------------------------------------------------
# Rating fetch deduplication
# ---------------------------------------------------------------------------
# Prevents concurrent requests for the same imdb_id (different raw_params /
# final_cache_key) from triggering duplicate MDBlist API calls.  The most
# common burst: AIOMetadata requests many posters simultaneously; several
# share an uncached title with different user-config hashes so render
# coalescing alone doesn't protect them.
#
# _rating_fetch_inflight: maps imdb_id -> asyncio.Event that fires once the
#   first fetch completes.  Subsequent requests wait, then re-read the DB.
# _rating_backoff: maps (imdb_id, API key) -> loop-time after which a new
#   attempt is allowed. Scoping by key lets a rotated or replaced key retry
#   the same title immediately. Network failures use an escalating ladder
#   (30s/2m/8m/1h); rate-limit responses use Retry-After or 1h flat.

_rating_fetch_inflight:         dict[str, asyncio.Event] = {}
_rating_backoff:                dict[tuple[str, str], float] = {}
_rating_fail_count:             dict[tuple[str, str], int]   = {}
_mdblist_semaphore:             "asyncio.Semaphore | None" = None  # caps concurrent MDBlist HTTP calls; created inside event loop
# Caps parallel burned-in-text scans. Each slot owns an independent RapidOCR
# session in a dedicated executor, so cold-cache OCR cannot occupy render workers.
# Created inside the event loop.
_detect_semaphore:              "asyncio.Semaphore | None" = None
_detect_executor:               "ThreadPoolExecutor | None" = None
# Maps immutable image/detector keys to active OCR tasks. Different poster
# configurations often render the same source image during a burst; they should
# share one scan even when their final composite cache keys differ.
_text_detection_inflight:       dict[str, "asyncio.Task[bool | None]"] = {}
_foreground_detection_count = 0
_active_poster_renders = 0
_background_detection_queue: "asyncio.Queue[_DeferredTextDetection] | None" = None
_background_detection_keys: set[str] = set()
_background_detection_task: "asyncio.Task[None] | None" = None


@dataclass(frozen=True)
class _DeferredTextDetection:
    cache_key: str
    image_cache_key: str
    title: tuple[str, ...]
    source: str
    tmdb_id: str
    media_type: str
    image_path: str
    vote_count: int | None
    source_key: str


def _get_detect_semaphore() -> "asyncio.Semaphore":
    """Lazily create the detection-admission semaphore inside the event loop."""
    global _detect_semaphore
    if _detect_semaphore is None:
        _detect_semaphore = asyncio.Semaphore(_cfg.TEXTLESS_DETECTION_CONCURRENCY)
    return _detect_semaphore


def _get_detect_executor() -> ThreadPoolExecutor:
    """Dedicated workers so OCR bursts cannot starve poster compositing."""
    global _detect_executor
    if _detect_executor is None:
        _detect_executor = ThreadPoolExecutor(
            max_workers=_cfg.TEXTLESS_DETECTION_CONCURRENCY,
            thread_name_prefix="text-detect",
        )
    return _detect_executor


def _shutdown_detect_executor() -> None:
    global _detect_executor
    if _detect_executor is not None:
        _detect_executor.shutdown(wait=True, cancel_futures=True)
        _detect_executor = None


def _reserve_foreground_detection() -> None:
    global _foreground_detection_count
    _foreground_detection_count += 1


def _release_foreground_detection() -> None:
    global _foreground_detection_count
    _foreground_detection_count = max(0, _foreground_detection_count - 1)


def _start_text_detection(
    cache_key: str,
    image: Image.Image,
    *,
    title: tuple[str, ...],
    source: str,
    tmdb_id: str,
    vote_count: int | None,
    source_key: str,
    media_type: str | None = None,
    image_path: str | None = None,
    foreground: bool = True,
    foreground_reserved: bool = False,
) -> "asyncio.Task[bool | None]":
    """Start or join one OCR scan for an immutable source image."""
    cached = get_cached_text_detection(cache_key)
    if cached is not None:
        if foreground and foreground_reserved:
            _release_foreground_detection()
        async def _cached_result() -> bool:
            return cached
        return asyncio.create_task(_cached_result())

    existing = _text_detection_inflight.get(cache_key)
    if existing is not None:
        if foreground and foreground_reserved:
            _release_foreground_detection()
        logger.info(
            f"Coalescing burned-in text scan for {tmdb_id} "
            f"(votes={vote_count}, source={source_key})"
        )
        return existing

    if foreground and not foreground_reserved:
        _reserve_foreground_detection()

    async def _scan() -> bool | None:
        from text_detect import poster_has_burned_in_text

        try:
            async with _get_detect_semaphore():
                result = await asyncio.get_running_loop().run_in_executor(
                    _get_detect_executor(),
                    lambda: poster_has_burned_in_text(
                        image,
                        conf=_cfg.PPOCR_BOX_THRESHOLD,
                        title=title,
                        source=source,
                        debug=True,
                    ),
                )
            if result is not None:
                set_cached_text_detection(cache_key, result)
            if result is True and source == "poster" and media_type and image_path:
                from textless_report import report_fake_textless_poster
                report_fake_textless_poster(
                    media_type=media_type,
                    tmdb_id=tmdb_id,
                    image_path=image_path,
                    vote_count=vote_count,
                )
            return result
        finally:
            if foreground:
                _release_foreground_detection()

    logger.info(
        f"Scanning textless poster {tmdb_id} for burned-in text "
        f"(votes={vote_count}, source={source_key}, "
        f"priority={'foreground' if foreground else 'background'})"
    )
    task = asyncio.create_task(_scan())
    _text_detection_inflight[cache_key] = task

    def _cleanup(done: "asyncio.Task[bool | None]") -> None:
        if _text_detection_inflight.get(cache_key) is done:
            _text_detection_inflight.pop(cache_key, None)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(_cleanup)
    return task


def _queue_background_text_detection(item: _DeferredTextDetection) -> None:
    """Queue one vote-gated scan without retaining its decoded image."""
    if get_cached_text_detection(item.cache_key) is not None:
        return
    if item.cache_key in _background_detection_keys:
        return
    if _background_detection_queue is None:
        logger.warning(
            f"Background text-detection queue unavailable for {item.tmdb_id}; "
            "scan will retry on the next request"
        )
        return
    _background_detection_keys.add(item.cache_key)
    _background_detection_queue.put_nowait(item)
    logger.info(
        f"Queued vote-gated text scan for {item.tmdb_id} "
        f"(votes={item.vote_count}, pending={_background_detection_queue.qsize()})"
    )


def _load_detection_image(image_cache_key: str) -> Image.Image | None:
    cached_bytes = get_cached_tmdb_poster(image_cache_key)
    if not cached_bytes:
        return None
    return Image.open(io.BytesIO(cached_bytes)).convert("RGBA")


async def _background_text_detection_worker() -> None:
    """Drain vote-gated scans only while no foreground scan is queued or running."""
    assert _background_detection_queue is not None
    while True:
        item = await _background_detection_queue.get()
        try:
            if get_cached_text_detection(item.cache_key) is not None:
                continue
            while _foreground_detection_count > 0 or _active_poster_renders > 0:
                await asyncio.sleep(0.1)

            image = await asyncio.get_running_loop().run_in_executor(
                None, _load_detection_image, item.image_cache_key
            )
            if image is None:
                logger.warning(
                    f"Deferred text scan source unavailable for {item.tmdb_id}; "
                    "scan will retry on the next request"
                )
                continue

            # A poster render may have arrived while the image was loading.
            while _foreground_detection_count > 0 or _active_poster_renders > 0:
                await asyncio.sleep(0.1)

            await asyncio.shield(_start_text_detection(
                item.cache_key,
                image,
                title=item.title,
                source=item.source,
                tmdb_id=item.tmdb_id,
                vote_count=item.vote_count,
                media_type=item.media_type,
                image_path=item.image_path,
                source_key=item.source_key,
                foreground=False,
            ))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                f"Deferred text scan failed for {item.tmdb_id}: {exc}"
            )
        finally:
            _background_detection_keys.discard(item.cache_key)
            _background_detection_queue.task_done()


# Per-key cooldown timestamps (event-loop time). Keyed by the API key string so
# rotation is independent — a rate-limited key stands down while the other serves.
_mdblist_key_cooldown: dict[str, float] = {}
# Index into _cfg.SERVER_MDBLIST_KEYS for the currently active server-side key.
_mdblist_active_key_idx: int = 0


def _quality_backoff_remaining(now: float | None = None) -> float:
    if now is None:
        now = asyncio.get_running_loop().time()
    return max(0.0, _quality_source_backoff_until.get(active_quality_source(), 0.0) - now)


def _record_quality_result(result) -> None:
    # QUALITY_PENDING means the source answered and is healthy — it just has no
    # value for this title yet. It is neither a success to reset the failure
    # count on nor a failure to count, so the backoff state is left untouched.
    if result is QUALITY_PENDING:
        return
    source = active_quality_source()
    if result is not FETCH_FAILED:
        _quality_source_backoff_until.pop(source, None)
        _quality_source_fail_count.pop(source, None)
        return
    now = asyncio.get_running_loop().time()
    if _quality_source_backoff_until.get(source, 0.0) > now:
        return
    failures = _quality_source_fail_count.get(source, 0) + 1
    _quality_source_fail_count[source] = failures
    delay = min(30.0 * (4 ** (failures - 1)), 1800.0)
    _quality_source_backoff_until[source] = now + delay
    logger.warning(f"Quality source {source} unavailable; backing off for {delay:.0f}s")


def _next_mdblist_server_key(current_key: str, now: float | None = None) -> str | None:
    """Select a healthy configured server key after *current_key*."""
    global _mdblist_active_key_idx
    keys = _cfg.SERVER_MDBLIST_KEYS
    if len(keys) < 2 or current_key not in keys:
        return None
    if now is None:
        now = asyncio.get_running_loop().time()
    start = keys.index(current_key)
    for offset in range(1, len(keys)):
        idx = (start + offset) % len(keys)
        candidate = keys[idx]
        if now >= _mdblist_key_cooldown.get(candidate, 0.0):
            _mdblist_active_key_idx = idx
            return candidate
    return None


def _mdblist_server_key_number(key: str | None) -> int | None:
    if not key:
        return None
    for idx, candidate in enumerate(_cfg.SERVER_MDBLIST_KEYS):
        if candidate == key:
            return idx + 1
    return None


def _mdblist_server_key_label(key: str | None) -> str:
    number = _mdblist_server_key_number(key)
    if number is None:
        return "request-supplied key"
    return f"configured key #{number}"


def _mark_mdblist_rate_limit(
    imdb_id: str, key: str, result
) -> tuple[float, str | None]:
    """Cool down a rate-limited key and select a healthy configured fallback."""
    if result.retry_after:
        backoff_secs = min(float(result.retry_after), 3600.0)
    else:
        backoff_secs = 3600.0
    now = asyncio.get_running_loop().time()
    _mdblist_key_cooldown[key] = now + backoff_secs
    _rating_backoff[_rating_retry_key(imdb_id, key)] = now + backoff_secs
    return backoff_secs, _next_mdblist_server_key(key, now)


async def _background_quality_fetch(
    imdb_id: str,
    media_type: str,
    season: int,
    episode: int,
    release_date: str | None,
) -> None:
    """Fetch quality tokens from the configured quality source and cache them.  Never raises."""
    global _quality_bg_semaphore
    if _quality_bg_semaphore is None:
        _quality_bg_semaphore = asyncio.Semaphore(_cfg.QUALITY_BG_CONCURRENCY)
    try:
        async with _quality_bg_semaphore:
            if _HTTP_CLIENT is None:
                return
            remaining = _quality_backoff_remaining()
            if remaining > 0:
                logger.debug(
                    f"Quality fetch skipped for {imdb_id}; source cooldown has {remaining:.0f}s remaining"
                )
                return
            result = await _with_retry(
                fetch_quality,
                _HTTP_CLIENT, imdb_id, media_type, season, episode, release_date,
            )
            _record_quality_result(result)
            if result is QUALITY_PENDING:
                # QualiCache is collecting in the background; the next request
                # for this title picks up the value once it lands.
                logger.info(f"Background quality fetch pending for {imdb_id}")
            elif result is not FETCH_FAILED:
                logger.info(f"Background quality fetch complete for {imdb_id}")
    except Exception as exc:
        _record_quality_result(FETCH_FAILED)
        logger.warning(f"Background quality fetch failed for {imdb_id}: {exc}")
    finally:
        _quality_bg_inflight.discard(imdb_id)

# Local imports
from age_badge import draw_quality_age_badge, draw_tier_bar, _score_points
from awards import dominant_frost_rgb
from awards import FETCH_FAILED, _RateLimited, draw_award_badge, draw_award_sash, parse_mdblist_awards
from i18n import load_languages, translate_genre, translate_sash
from cache import (
    get_cached_quality,
    get_cached_rating,
    get_cached_final_poster,
    set_cached_final_poster,
    delete_cached_final_poster,
    get_cached_tmdb_poster,
    get_cached_tmdb_metadata,
    get_cached_text_detection,
    set_cached_text_detection,
    init_db,
    is_digital_release,
    set_cached_rating,
    delete_cached_tmdb_metadata,
    prune_caches,
    get_cache_stats,
    get_app_state,
    set_app_state,
    get_db,
)
from digital_release import digital_release_poll_loop
import config as _cfg
from render_profile import RenderProfile, load_render_profile
from discovery import (
    ALL_PRIORITY_SLOTS,
    FESTIVAL_KEYWORDS,
    DiscoveryMeta,
    extract_discovery_meta,
    pick_sash,
)
from quality import (
    QUALITY_PENDING,
    QUALITY_SOURCES,
    BadgeItem,
    active_quality_source,
    fetch_quality,
    get_resized_badge,
    parse_quality,
    quality_source_configured,
    render_badges_left,
)
from ratings import (
    CustomScorePalette,
    calculate_weighted_score,
    draw_frosted_bar,
    draw_score_bar,
    draw_score_bar_vertical,
    fetch_rating,
    parse_custom_score_palette,
    score_color_for_mode,
    _draw_solid_pip,
    _score_color,
    _score_color_alt,
    _score_color_metal,
)
from tmdb import composite_logo, logo_centre_y, fetch_logo, image_language_order, fetch_poster_metadata, fetch_poster_image, fetch_backdrop_image, fetch_trending_rank, fetch_trending_candidates, fetch_popular_candidates, fetch_supplemental_candidates, fetch_catalog_candidates, fetch_release_status, fetch_recent_movie_digital_release_date, svg_logo_supported, tmdb_metadata_cache_key, _CROP_VERSION, _fetch_metahub_logo, LOGO_ABS_MAX_H, TEXT_FORWARD_PRIORITIES as _TEXT_FORWARD_LOGO_PRIORITIES
from artwork_candidates import (
    CandidateDiscoveryError,
    MAX_BODY_BYTES as CANDIDATE_MAX_BODY_BYTES,
    decode_request_body as decode_candidate_request_body,
    discover_candidates,
    discovery_key_matches,
    effective_discovery_key,
    shutdown_candidate_ocr_executor,
)

# Logo priorities that consult the secondary preferred language ("custom").
# Elsewhere the secondary language is inert and must be kept out of the image
# fetch / cache key so single-language requests keep their existing cache entry.
_SECONDARY_LANGUAGE_PRIORITIES = frozenset({
    "native_custom_text",
    "native_custom_original_text",
})
import tvdb
import anime

# ---------------------------------------------------------------------------
# Persistent HTTP client
# ---------------------------------------------------------------------------
# One client for the lifetime of the process. httpx keeps TCP connections
# alive in its connection pool, so repeated requests to the same host
# (TMDB, MDblist, AIOStreams) reuse the existing socket rather than paying
# TLS + TCP handshake overhead on every poster request.
#
# Timeouts are split:
#   connect=5s  — fail fast when a host is unreachable
#   read=12s    — allow slow responses from external APIs
#   pool=5s     — don't block forever waiting for a pool slot

_HTTP_CLIENT: httpx.AsyncClient | None = None

def _make_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=12.0, write=5.0, pool=5.0),
        limits=httpx.Limits(
            max_connections=40,
            max_keepalive_connections=20,
            keepalive_expiry=30,
        ),
        headers={
            "Accept-Encoding": "identity",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
        http2=False,   # most poster APIs don't support h2; skip the negotiation
    )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

_TMDB_ID_RE  = re.compile(r'^\d{1,10}$')
_IMDB_ID_RE  = re.compile(r'^tt\d{1,10}$')
_VALID_TYPES = frozenset({"movie", "tv", "series"})
_SELECTED_CONTENT_TYPES = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
}
_OUTPUT_FORMATS = {
    "jpeg": ("JPEG", "image/jpeg"),
    "png": ("PNG", "image/png"),
    "webp": ("WEBP", "image/webp"),
}


@dataclass(frozen=True)
class _SelectedImage:
    image: Image.Image
    sha256: str
    decoded_format: str


@dataclass(frozen=True)
class _SelectionEnvelope:
    primary: _SelectedImage
    logo: _SelectedImage | None


class _SelectionError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _check_tmdb_id(val: str) -> None:
    if not _TMDB_ID_RE.match(val):
        raise HTTPException(status_code=400, detail="Invalid tmdb_id")


def _check_imdb_id(val: str) -> None:
    if not _IMDB_ID_RE.match(val):
        raise HTTPException(status_code=400, detail="Invalid imdb_id")


def _check_type(val: str) -> None:
    if val not in _VALID_TYPES:
        raise HTTPException(status_code=400, detail="Invalid type")


def _resolve_anime_request(
    anilist_id: str, kitsu_id: str, stremio_id: str = ""
) -> "tuple[str | None, int | None]":
    """Select the anime provider for this request, or (None, None) for the
    ordinary TMDB path.

    *stremio_id* is the preferred input: it carries the raw Stremio meta id
    ("kitsu:7442", "tt0903747", "tmdb:1396"), so a client can send it with a
    plain, always-populated placeholder and never needs the optional "{name?}"
    syntax that some addons reject. Non-anime ids simply yield the TMDB path.

    The per-namespace params remain accepted so URLs generated before this
    existed keep working.

    A malformed per-namespace id raises 400 rather than falling through to the
    TMDB path, where it would surface as a confusing "Invalid tmdb_id".  AniList
    wins when both are supplied — arbitrary, but deterministic, so a client that
    sends both always lands on the same cache entry.
    """
    if not _cfg.ANIME_SOURCES_ENABLED:
        return None, None

    # A raw Stremio id is never malformed from our point of view — anything we
    # don't recognise is just a non-anime title — so it never raises.
    namespace, parsed = anime.parse_stremio_id(stremio_id)
    if namespace is not None:
        return namespace, parsed
    for namespace, raw in (("anilist", anilist_id), ("kitsu", kitsu_id)):
        raw = (raw or "").strip()
        if not raw:
            continue
        # A template pasted into a metadata provider may arrive with the
        # placeholder unsubstituted ("{kitsu_id}") when that provider has no id
        # for the title. Treat it as absent rather than malformed — otherwise a
        # single anime placeholder in the URL would 400 every live-action
        # poster served through the same template.
        if raw.startswith("{") and raw.endswith("}"):
            continue
        parsed = anime.parse_anime_id(namespace, raw)
        if parsed is None:
            raise HTTPException(status_code=400, detail=f"Invalid {namespace}_id")
        return namespace, parsed
    return None, None


def _access_key_matches(candidate: str) -> bool:
    if not _cfg.ACCESS_KEY:
        return False
    discovery_key = effective_discovery_key()
    if (
        discovery_key
        and hmac.compare_digest(
            candidate.encode("utf-8"),
            discovery_key.encode("utf-8"),
        )
    ):
        return False
    return hmac.compare_digest(
        candidate.encode("utf-8"),
        _cfg.ACCESS_KEY.encode("utf-8"),
    )


async def _read_selected_body(request: Request) -> bytes:
    return await _read_bounded_body(
        request,
        max_bytes=_cfg.SELECTED_MAX_BYTES,
        empty_message="Selected image body is empty",
        too_large_message="Selected image body is too large",
    )


async def _read_selection_body(request: Request) -> bytes:
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise _SelectionError(
            415,
            "invalid_content_type",
            "Content-Type must be application/json",
        )
    try:
        return await _read_bounded_body(
            request,
            max_bytes=_cfg.SELECTION_MAX_BYTES,
            empty_message="Selection body is empty",
            too_large_message="Selection body is too large",
        )
    except HTTPException as exc:
        code = "body_too_large" if exc.status_code == 413 else "invalid_body"
        raise _SelectionError(exc.status_code, code, str(exc.detail)) from exc


async def _read_bounded_body(
    request: Request,
    *,
    max_bytes: int,
    empty_message: str,
    too_large_message: str,
) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from exc
        if declared_length < 0:
            raise HTTPException(status_code=400, detail="Invalid Content-Length")
        if declared_length > max_bytes:
            raise HTTPException(status_code=413, detail=too_large_message)

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise HTTPException(status_code=413, detail=too_large_message)
    if not body:
        raise HTTPException(status_code=400, detail=empty_message)
    return bytes(body)


def _decode_selected_image(body: bytes, content_type: str) -> _SelectedImage:
    media_type = content_type.split(";", 1)[0].strip().lower()
    expected_format = _SELECTED_CONTENT_TYPES.get(media_type)
    if expected_format is None:
        raise HTTPException(
            status_code=415,
            detail="Content-Type must be image/jpeg, image/png, or image/webp",
        )

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(body)) as probe:
                decoded_format = (probe.format or "").upper()
                width, height = probe.size
                if decoded_format != expected_format:
                    raise HTTPException(
                        status_code=415,
                        detail="Content-Type does not match the decoded image format",
                    )
                if (
                    width > _cfg.SELECTED_MAX_WIDTH
                    or height > _cfg.SELECTED_MAX_HEIGHT
                    or width * height > _cfg.SELECTED_MAX_PIXELS
                ):
                    raise HTTPException(
                        status_code=413,
                        detail="Selected image dimensions exceed configured limits",
                    )
                probe.verify()

            with Image.open(io.BytesIO(body)) as decoded:
                decoded.load()
                image = decoded.convert("RGBA")
    except HTTPException:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise HTTPException(
            status_code=413,
            detail="Selected image exceeds Pillow decompression safety limits",
        ) from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise HTTPException(status_code=400, detail="Malformed selected image") from exc

    return _SelectedImage(
        image=image,
        sha256=hashlib.sha256(body).hexdigest(),
        decoded_format=decoded_format,
    )


def _decode_selection_envelope(body: bytes) -> _SelectionEnvelope:
    def _object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise _SelectionError(
                    400,
                    "duplicate_field",
                    f"Duplicate JSON field: {key}",
                )
            result[key] = value
        return result

    try:
        envelope = json.loads(body, object_pairs_hook=_object)
    except _SelectionError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _SelectionError(400, "invalid_json", "Malformed JSON body") from exc

    if not isinstance(envelope, dict) or set(envelope) != {
        "schema_version",
        "primary",
        "logo",
    }:
        raise _SelectionError(
            400,
            "invalid_schema",
            "Selection body must contain exactly schema_version, primary, and logo",
        )
    if type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1:
        raise _SelectionError(
            400,
            "unsupported_schema",
            "schema_version must be 1",
        )

    primary = _decode_selection_part(envelope["primary"], "primary")
    logo_value = envelope["logo"]
    logo = None if logo_value is None else _decode_selection_part(logo_value, "logo")
    if logo is not None and not _logo_has_visible_pixels(logo.image):
        raise _SelectionError(
            422,
            "selected_logo_empty",
            "Selected Logo has no meaningful visible pixels",
        )
    return _SelectionEnvelope(primary=primary, logo=logo)


def _decode_selection_part(value, field_name: str) -> _SelectedImage:
    if not isinstance(value, dict) or set(value) != {
        "content_type",
        "sha256",
        "data",
    }:
        raise _SelectionError(
            400,
            "invalid_schema",
            f"{field_name} must contain exactly content_type, sha256, and data",
        )
    content_type = value["content_type"]
    expected_sha256 = value["sha256"]
    encoded = value["data"]
    if not all(isinstance(item, str) for item in (
        content_type,
        expected_sha256,
        encoded,
    )):
        raise _SelectionError(
            400,
            "invalid_schema",
            f"{field_name} fields must be strings",
        )
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise _SelectionError(
            400,
            "invalid_sha256",
            f"{field_name}.sha256 must be 64 lowercase hexadecimal characters",
        )
    max_encoded = 4 * ((_cfg.SELECTED_MAX_BYTES + 2) // 3)
    if len(encoded) > max_encoded:
        raise _SelectionError(
            413,
            "image_too_large",
            f"{field_name} image is too large",
        )
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise _SelectionError(
            400,
            "invalid_base64",
            f"{field_name}.data is not strict base64",
        ) from exc
    if not image_bytes:
        raise _SelectionError(
            400,
            "empty_image",
            f"{field_name} image is empty",
        )
    if len(image_bytes) > _cfg.SELECTED_MAX_BYTES:
        raise _SelectionError(
            413,
            "image_too_large",
            f"{field_name} image is too large",
        )
    actual_sha256 = hashlib.sha256(image_bytes).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise _SelectionError(
            400,
            "image_hash_mismatch",
            f"{field_name} SHA-256 does not match decoded bytes",
        )
    try:
        return _decode_selected_image(image_bytes, content_type)
    except HTTPException as exc:
        raise _SelectionError(
            exc.status_code,
            "invalid_image",
            f"{field_name}: {exc.detail}",
        ) from exc


def _logo_has_visible_pixels(image: Image.Image) -> bool:
    alpha = np.asarray(image.getchannel("A"))
    visible = int(np.count_nonzero(alpha > 32))
    return visible >= max(4, image.width * image.height // 100_000)


def _image_response(
    content: bytes,
    output_format: str,
    cache_key: str | None = None,
) -> Response:
    _, media_type = _OUTPUT_FORMATS[output_format]
    digest = hashlib.sha256(content).digest()
    response = Response(content=content, media_type=media_type)
    response.headers["Digest"] = f"sha-256={base64.b64encode(digest).decode('ascii')}"
    response.headers["X-Image-SHA256"] = digest.hex()
    if cache_key is not None:
        response.headers["ETag"] = f'"{cache_key}"'
    if _cfg.DISABLE_COMPOSITE_CACHE:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    elif _cfg.CDN_CACHE_TTL > 0:
        response.headers["Cache-Control"] = f"public, max-age={_cfg.CDN_CACHE_TTL}"
    return response


def _encode_rendered_image(image: Image.Image, output_format: str) -> bytes:
    pil_format, _ = _OUTPUT_FORMATS[output_format]
    encoded = image if pil_format == "PNG" else image.convert("RGB")
    save_kwargs = {}
    if pil_format == "JPEG":
        save_kwargs["quality"] = _cfg.JPEG_QUALITY
    elif pil_format == "WEBP":
        save_kwargs["quality"] = _cfg.WEBP_QUALITY
    buffer = io.BytesIO()
    encoded.save(buffer, format=pil_format, **save_kwargs)
    return buffer.getvalue()


def _resolve_output_format(output_format: str | None) -> str:
    resolved = (output_format or _cfg.IMAGE_FORMAT).lower()
    if resolved not in _OUTPUT_FORMATS:
        raise HTTPException(status_code=400, detail="Invalid output_format")
    return resolved


def _selected_cache_identity(
    *,
    base_identity: str,
    selected_sha256: str,
    selected_logo_sha256: str | None,
    profile_digest: str,
    renderer_revision: str,
    imdb_id: str,
    tmdb_id: str,
    media_type: str,
    quality: str,
    season: int,
    episode: int,
    output_format: str,
) -> str:
    return (
        base_identity
        + f"|base={selected_sha256}"
        + f"|logo={selected_logo_sha256 or 'provider'}"
        + f"|profile={profile_digest}"
        + f"|revision={renderer_revision}"
        + f"|ids={imdb_id}:{tmdb_id}:{media_type}"
        + f"|quality={quality}"
        + f"|episode={season}:{episode}"
        + f"|output={output_format}"
    )


def _client_cache_key(
    render_cache_key: str | None,
    selected_image: _SelectedImage | None,
) -> str | None:
    return render_cache_key if selected_image is None else None


def _selected_final_cache_key(
    imdb_id: str,
    tmdb_id: str,
    media_type: str,
    params_hash: str,
) -> str:
    return f"{imdb_id}:{tmdb_id}:{media_type}:selected:{params_hash}"


# ---------------------------------------------------------------------------
# Key resolution helpers
# ---------------------------------------------------------------------------

def _resolve_tmdb_key(query_key: str) -> str | None:
    if query_key:
        return query_key
    if _cfg.SERVER_TMDB_KEY:
        return _cfg.SERVER_TMDB_KEY
    return None


def _resolve_mdblist_key(query_key: str) -> str | None:
    if query_key:
        return query_key
    if _cfg.SERVER_MDBLIST_KEYS:
        return _cfg.SERVER_MDBLIST_KEYS[_mdblist_active_key_idx % len(_cfg.SERVER_MDBLIST_KEYS)]
    return None


def _rating_retry_key(imdb_id: str, mdblist_key: str) -> tuple[str, str]:
    """Identify retry state for one title on one MDBList API key."""
    return imdb_id, mdblist_key


def _detection_vote_ok(vote_count: int | None) -> bool:
    """True when an asset should be scanned during the foreground request."""
    return vote_count is not None and vote_count <= _cfg.TEXTLESS_DETECTION_MAX_VOTES


# ---------------------------------------------------------------------------
# Per-request configuration
# ---------------------------------------------------------------------------

_CLIENT_EDGE_INSETS = {
    "stremio_tv_nuvio": (0.0, 0.0),
    "stremio_desktop_web": (0.007, 0.004),
    # Plex renders posters uncropped in its grid/details views — no edge
    # compensation needed. Used by the plex_sync.py companion script.
    "plex": (0.0, 0.0),
    # Same story for Jellyfin's web/desktop clients — posters render
    # uncropped in the library grid and detail views. Used by the
    # jellyfin_sync.py companion script.
    "jellyfin": (0.0, 0.0),
}


@dataclass
class RequestConfig:
    """
    Holds all user-tuneable config values for a single request.
    Defaults come from the global config module; query params override them.
    """
    show_award_sash:     bool = field(default_factory=lambda: _cfg.SHOW_AWARD_SASH)
    sash_poster_color:   bool = False   # diagonal sash colour derived from poster art
    cinema_greyscale:    bool = True    # greyscale art when release_status == "Cinema"
    cinema_greyscale_skip_if_available: bool = False  # keep colour if Web/Remux source found
    release_status_cinema_only: bool = False  # only show release status when "Cinema"
    badge_display_mode:  int  = field(default_factory=lambda: _cfg.BADGE_DISPLAY_MODE)
    rating_display_mode: int  = field(default_factory=lambda: _cfg.SHOW_RATING_DISPLAY_MODE)

    accent_bar_font_size_ratio:    float = field(default_factory=lambda: _cfg.ACCENT_BAR_MODE_FONT_SIZE_RATIO)
    # Score Bar mode label suffix: 0 = Year (legacy default), 1 = Info sash, 2 = Year + Info sash
    accent_bar_append_mode:        int   = 0
    # Score Bar position knob — distance from poster bottom edge as fraction of height.
    # Default matches the legacy hardcoded 30px on a 500x750 poster.
    accent_bar_bottom_ratio:       float = 0.04
    numeric_score_font_size_ratio: float = field(default_factory=lambda: _cfg.NUMERIC_SCORE_MODE_FONT_SIZE_RATIO)
    # Clean mode (mode 2) numeric format.  When True, the rating is divided by
    # 10 and shown to one decimal (87 → "8.7", 100 → "10.0").  Default keeps
    # the legacy 0-100 integer form.
    score_out_of_10: bool = False
    accent_bar_y_offset:           float = field(default_factory=lambda: _cfg.ACCENT_BAR_MODE_FONT_Y_OFFSET)
    numeric_score_y_offset:        float = field(default_factory=lambda: _cfg.NUMERIC_SCORE_MODE_FONT_Y_OFFSET)
    score_glow_threshold:          int   = field(default_factory=lambda: _cfg.SCORE_GLOW_THRESHOLD)
    score_glow_blur:               int   = field(default_factory=lambda: _cfg.SCORE_GLOW_BLUR)
    score_glow_alpha:              int   = field(default_factory=lambda: _cfg.SCORE_GLOW_ALPHA)
    # Glow colour: "" = white (default), "match" = the score bar's own colour, or
    # a 6-digit hex string for a custom colour.
    score_glow_color:              str   = ""
    minimalist_mode_font_size_ratio:  float = field(default_factory=lambda: _cfg.MINIMALIST_MODE_FONT_SIZE_RATIO)
    minimalist_mode_font_x_offset: float = field(default_factory=lambda: _cfg.MINIMALIST_MODE_FONT_X_OFFSET)
    minimalist_mode_font_y_offset: float = field(default_factory=lambda: _cfg.MINIMALIST_MODE_FONT_Y_OFFSET)
    # What to append after the genre in Minimalist mode:
    #   0 = Year (Genre + year, rating as a colour-coded pip — the original look)
    #   1 = Rating (Genre | Score, score printed as text)
    #   2 = Year + Rating (Genre | Year | Score)
    minimalist_append_mode: int = 0
    minimalist_score_out_of_10: bool = False

    # Frosted bar (rating_display_mode == 4)
    bar_height_ratio:        float = 0.080
    bar_font_size_ratio:     float = 0.55
    bar_frost_opacity:       float = 0.85
    bar_frost_saturation:    float = 1.2   # frosted colour-cast strength (0 = grey)
    bar_bottom_inset:        float = 0.0
    bar_style:               str   = "frosted"  # "frosted"|"silver"|"gold"|"rating_black"|"rating_frosted"
    bar_accent:              str   = "silver"   # "silver"|"gold"|"palette_0"|"palette_1"|"palette_2"|"palette_custom"
    bar_score_out_of_10:     bool  = False
    bar_append:              str   = "rating_year"  # "rating_year"|"rating"|"year"|"sash"

    logo_max_w_ratio:   float = field(default_factory=lambda: _cfg.LOGO_MAX_W_RATIO)
    logo_max_h_ratio:   float = field(default_factory=lambda: _cfg.LOGO_MAX_H_RATIO)
    logo_bottom_ratio:  float = field(default_factory=lambda: _cfg.LOGO_BOTTOM_RATIO)
    logo_bottom_anchor:  bool  = False
    sash_winner_star:    bool  = False

    badge_height:            int   = field(default_factory=lambda: _cfg.BADGE_HEIGHT)
    badge_gap:               int   = field(default_factory=lambda: _cfg.BADGE_GAP)
    badge_anchor_x:          float = field(default_factory=lambda: _cfg.BADGE_ANCHOR_X_RATIO)
    badge_anchor_y:          float = field(default_factory=lambda: _cfg.BADGE_ANCHOR_Y_RATIO)
    badge_min_score:          int  = 2
    combined_badge_stacked:   bool = False

    movie_weights: dict | None = None
    tv_weights:    dict | None = None
    fallback_to_imdb: bool = False

    logo_language: str = field(default_factory=lambda: _cfg.DEFAULT_LOGO_LANGUAGE)
    # Secondary preferred language ("custom").  Only consulted by the
    # native_custom_* priorities below; blank elsewhere (and blank there degrades
    # those modes to their non-custom equivalents).
    logo_language_secondary: str = ""
    # Logo resolution priority.  "native" = the viewer's chosen logo_language
    # (e.g. en); "custom" = logo_language_secondary; "original" = the content's
    # own original language (e.g. ja for an anime).  "text" = render the
    # translated title as text.
    #   "native_original" (default): native → original → text
    #   "original_native":           original → native → text
    #   "native_if_original_english": native if content is native, else English
    #                                 → original → text
    #   "native_text":               native → English → neutral → text
    #                                 (no original-language logo)
    #   "native_custom_text":         native → custom → English → neutral → text
    #   "native_custom_original_text": native → custom → original → English
    #                                 → neutral → text
    logo_priority: str = "native_original"
    # Fallback-poster style for titles with no art: "minimal" (procedural textured
    # backdrop) or "photoreal" (hand-made photographic art that blends with real
    # posters).  Missing photoreal art degrades to the minimal set.
    fallback_bg_style: str = "minimal"
    # Original-art mode: serve TMDB's primary poster (title/logo baked into the
    # art) as-is, skipping our own logo overlay, text detection and the textless/
    # backdrop fallbacks.  The logo is part of the art in this mode.
    use_original_art: bool = False
    # Which poster original-art mode serves:
    #   "primary"   = TMDB's designated default poster (most recognisable)
    #   "top_rated" = highest-voted poster, by logo_priority language order
    original_art_source: str = "primary"
    sash_priority: list[str] = field(default_factory=lambda: list(_cfg.SASH_PRIORITY))
    muted: bool = False
    textless: bool = False
    top_gradient:    str = "high"   # off | low | medium | high | custom - strength of the top vignette
    bottom_gradient: str = "high"   # off | low | medium | high | custom - strength of the bottom vignette
    top_vignette_sash_only: bool = False
    top_gradient_opacity: float | None = None
    top_gradient_height: float | None = None
    bottom_gradient_opacity: float | None = None
    bottom_gradient_height: float | None = None
    hide_genre: bool = False
    score_color_mode: int = 2
    score_custom_palette: CustomScorePalette | None = None
    sash_badge: bool = False              # legacy; superseded by sash_mode (kept for back-compat parsing)
    sash_mode: str = "sash"               # "sash" (diagonal) | "notch"
    sash_badge_style:  str   = "frosted" # "silver" | "gold" | "frosted"
    sash_badge_size_w: float = 1.05      # horizontal scale of badge
    sash_badge_size_h: float = 1.05      # vertical scale of badge
    sash_badge_inset: float = 0.0          # top-edge offset as fraction of poster height (± small)
    sash_badge_font_ratio:   float = 0.43  # font size as fraction of badge height
    sash_badge_frost_opacity: float = 0.75 # frosted overlay opacity (0.0–1.0)
    sash_badge_frost_saturation: float = 1.2 # frosted colour-cast strength (0 = grey)
    # Reference colour mode: match the frosted tint to the poster's true colour
    # (bolder, un-pastel) instead of the saturation-scaled frosted tint. Global.
    frost_reference:         bool  = False
    sash_length_ratio: float = 1.15  # diagonal sash length as fraction of poster width
    sash_height_ratio: float = 0.12  # diagonal sash height (thickness) as fraction of poster width
    wait_for_quality: bool = False  # block response until quality is fetched (for poster-warm workflows)
    greyscale_no_quality: bool = False  # greyscale art when no quality found (needs wait_for_quality)
    rating_text_color: tuple[int, int, int] | None = None
    sash_text_color:   tuple[int, int, int] | None = None


def _parse_bool(val: str | None, default: bool) -> bool:
    if val is None:
        return default
    return val.strip().lower() not in ("0", "false", "no")


def _parse_hex_color(val: str | None) -> tuple[int, int, int] | None:
    if not val:
        return None
    v = val.strip().lstrip("#")
    if len(v) != 6:
        return None
    try:
        return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))
    except ValueError:
        return None


def _parse_weights(raw: str | None, sources: list[str]) -> dict | None:
    if not raw:
        return None
    out = {}
    try:
        for part in raw.split(","):
            part = part.strip()
            if ":" not in part:
                continue
            key, val = part.split(":", 1)
            key = key.strip().lower()
            if key in sources:
                out[key] = max(0.0, min(1.0, float(val)))
    except Exception:
        return None
    return out if out else None


def _parse_sash_priority(raw: str | None) -> list[str]:
    if not raw:
        return list(_cfg.SASH_PRIORITY)
    tokens = [s.strip() for s in raw.split(",") if s.strip()]
    # Tokens prefixed with "-" are explicit exclusions
    excluded  = {t[1:] for t in tokens if t.startswith("-") and t[1:] in ALL_PRIORITY_SLOTS}
    active    = [t      for t in tokens if not t.startswith("-") and t in ALL_PRIORITY_SLOTS]

    # Back-compat: the legacy combined "structural" / "release_status" tokens
    # expand in place to their granular slots so older saved URLs keep working.
    if "structural" in active:
        idx = active.index("structural")
        expanded = [s for s in ["short_film", "mini_series", "binge_ready"] if s not in excluded and s not in active]
        active = active[:idx] + expanded + active[idx+1:]
        
    if "release_status" in active:
        idx = active.index("release_status")
        expanded = [s for s in ["cinema", "streaming", "physical", "production", "ended", "cancelled", "airing"] if s not in excluded and s not in active]
        active = active[:idx] + expanded + active[idx+1:]
        
    # An empty, exclusion-free selection means no real sash config was supplied,
    # so fall back to the full default set. Otherwise the explicit selection is
    # authoritative: slots the user did not list are omitted rather than
    # force-appended, so sashes added in a newer version stay off until the user
    # opts in (re-import the old URL, then enable the new sashes).
    if not active and not excluded:
        return list(_cfg.SASH_PRIORITY)
    return active


def build_request_config(params: dict) -> RequestConfig:
    """Build a RequestConfig from raw query-param strings.

    All numeric overrides are clamped to a sensible range so a malicious or
    careless caller can't pass values that would melt a worker (e.g.
    score_glow_blur=99999 turning into a Gaussian kernel of that radius, or
    badge_height=99999 triggering a multi-GB image resize).  Bounds are
    deliberately a little more generous than the configurator sliders so
    power users can push past UI limits without bypassing safety.
    """
    cfg = RequestConfig()

    # Client profiles provide defaults only; explicit inset parameters below
    # remain authoritative for users who fine-tune either edge manually.
    _client_insets = _CLIENT_EDGE_INSETS.get(
        (params.get("primary_client") or "").strip().lower()
    )
    if _client_insets is not None:
        cfg.bar_bottom_inset, cfg.sash_badge_inset = _client_insets

    def _b(key, default): return _parse_bool(params.get(key), default)

    def _f(key, default, lo: float, hi: float):
        """Float param with hard clamp to [lo, hi]; invalid → default."""
        try:
            return max(lo, min(hi, float(params[key]))) if key in params else default
        except (ValueError, TypeError):
            return default

    def _i(key, default, lo: int, hi: int):
        """Int param with hard clamp to [lo, hi]; invalid → default."""
        try:
            return max(lo, min(hi, int(params[key]))) if key in params else default
        except (ValueError, TypeError):
            return default

    cfg.show_award_sash         = _b("show_award_sash",        cfg.show_award_sash)
    cfg.sash_poster_color       = _b("sash_poster_color",      cfg.sash_poster_color)
    cfg.cinema_greyscale        = _b("cinema_greyscale",       cfg.cinema_greyscale)
    cfg.cinema_greyscale_skip_if_available = _b("cinema_greyscale_skip_if_available", cfg.cinema_greyscale_skip_if_available)
    cfg.release_status_cinema_only = _b("release_status_cinema_only", cfg.release_status_cinema_only)
    cfg.muted                   = _b("muted",                  cfg.muted)
    cfg.score_out_of_10         = _b("score_out_of_10",        cfg.score_out_of_10)
    cfg.textless                = _b("textless",               cfg.textless)
    # top_gradient accepts off / low / medium / high.  Legacy boolean values
    # (true / false) from pre-v1.0.4 URLs map to high / off respectively so
    # cached configurator links keep working.
    _tg_raw = (params.get("top_gradient") or "").strip().lower()
    if _tg_raw in _TOP_GRADIENT_LEVELS:
        cfg.top_gradient = _tg_raw
    elif _tg_raw in ("true", "1", "yes"):
        cfg.top_gradient = "high"
    elif _tg_raw in ("false", "0", "no"):
        cfg.top_gradient = "off"
    elif _tg_raw == "custom":
        cfg.top_gradient = "custom"
    # else: leave RequestConfig default ("high")

    # bottom_gradient — same four-level enum as top.  Brand-new param so no
    # legacy boolean form to honour; unknown values fall through to the
    # RequestConfig default ("high") which matches the legacy behaviour.
    _bg_raw = (params.get("bottom_gradient") or "").strip().lower()
    if _bg_raw in _BOTTOM_GRADIENT_LEVELS:
        cfg.bottom_gradient = _bg_raw
    elif _bg_raw == "custom":
        cfg.bottom_gradient = "custom"

    cfg.top_vignette_sash_only = _b("top_vignette_sash_only", cfg.top_vignette_sash_only)
    val_tgo = params.get("top_gradient_opacity")
    if val_tgo is not None:
        try: cfg.top_gradient_opacity = float(val_tgo)
        except ValueError: pass
    val_tgh = params.get("top_gradient_height")
    if val_tgh is not None:
        try: cfg.top_gradient_height = float(val_tgh)
        except ValueError: pass
    val_bgo = params.get("bottom_gradient_opacity")
    if val_bgo is not None:
        try: cfg.bottom_gradient_opacity = float(val_bgo)
        except ValueError: pass
    val_bgh = params.get("bottom_gradient_height")
    if val_bgh is not None:
        try: cfg.bottom_gradient_height = float(val_bgh)
        except ValueError: pass
    cfg.hide_genre = _b("hide_genre", cfg.hide_genre)

    cfg.sash_badge              = _b("sash_badge",              cfg.sash_badge)
    # sash_mode supersedes the legacy sash_badge bool; fall back to it for old
    # URLs/presets (sash_badge=true → notch, false → diagonal sash).
    _sm_raw = (params.get("sash_mode") or "").strip().lower()
    if _sm_raw in ("hidden", "sash", "notch"):
        cfg.sash_mode = _sm_raw
    elif "show_award_sash" in params and not cfg.show_award_sash:
        cfg.sash_mode = "hidden"   # legacy: sashes turned off
    elif "sash_badge" in params:
        cfg.sash_mode = "notch" if cfg.sash_badge else "sash"
    cfg.sash_badge_inset         = _f("sash_badge_inset",         cfg.sash_badge_inset,         -0.02, 0.02)
    cfg.sash_badge_font_ratio    = _f("sash_badge_font_ratio",    cfg.sash_badge_font_ratio,    0.10, 1.0)
    cfg.sash_badge_frost_opacity = _f("sash_badge_frost_opacity", cfg.sash_badge_frost_opacity, 0.0, 1.0)
    cfg.sash_badge_frost_saturation = _f("sash_badge_frost_saturation", cfg.sash_badge_frost_saturation, 0.0, 2.0)
    cfg.sash_badge_size_w       = _f("sash_badge_size_w",       cfg.sash_badge_size_w,       0.5, 2.0)
    cfg.sash_badge_size_h       = _f("sash_badge_size_h",       cfg.sash_badge_size_h,       0.5, 2.0)
    _style_raw = params.get("sash_badge_style", cfg.sash_badge_style)
    if _style_raw in ("silver", "gold", "frosted", "black"):
        cfg.sash_badge_style = _style_raw
    cfg.sash_length_ratio       = _f("sash_length_ratio",      cfg.sash_length_ratio,      0.8, 1.5)
    cfg.sash_height_ratio       = _f("sash_height_ratio",      cfg.sash_height_ratio,      0.06, 0.20)
    cfg.wait_for_quality        = _b("wait_for_quality",        cfg.wait_for_quality)
    cfg.greyscale_no_quality    = _b("greyscale_no_quality",    cfg.greyscale_no_quality)
    cfg.score_color_mode        = _i("score_color_mode",       cfg.score_color_mode,       0,   3)
    cfg.score_custom_palette    = parse_custom_score_palette(params.get("score_custom_palette"))
    cfg.badge_display_mode      = _i("badge_display_mode",     cfg.badge_display_mode,     0,   5)
    cfg.rating_display_mode     = _i("rating_display_mode",    cfg.rating_display_mode,    0,   4)

    if "show_quality_badges" in params and "badge_display_mode" not in params:
        if _parse_bool(params.get("show_quality_badges"), True):
            cfg.badge_display_mode = 1
        else:
            cfg.badge_display_mode = 0

    # Font-size ratios are multiplied by the poster width — anything above ~0.3
    # would overflow the poster; we cap at 0.5 to leave headroom for experimentation.
    cfg.accent_bar_font_size_ratio    = _f("accent_bar_font_size_ratio",    cfg.accent_bar_font_size_ratio,    0.0, 0.5)
    cfg.accent_bar_append_mode        = _i("accent_bar_append_mode",        cfg.accent_bar_append_mode,        0,   2)
    cfg.accent_bar_bottom_ratio       = _f("accent_bar_bottom_ratio",       cfg.accent_bar_bottom_ratio,       0.0, 0.5)
    cfg.numeric_score_font_size_ratio = _f("numeric_score_font_size_ratio", cfg.numeric_score_font_size_ratio, 0.0, 0.5)
    cfg.accent_bar_y_offset           = _f("accent_bar_y_offset",           cfg.accent_bar_y_offset,           0.0, 1.0)
    cfg.numeric_score_y_offset        = _f("numeric_score_y_offset",        cfg.numeric_score_y_offset,        0.0, 1.0)
    cfg.score_glow_threshold          = _i("score_glow_threshold",          cfg.score_glow_threshold,          0,   100)
    # Glow blur is a Gaussian kernel radius — cost is O(r²) per pixel, so anything
    # above ~50 starts measurably slowing the render.  Hard cap at 50.
    cfg.score_glow_blur               = _i("score_glow_blur",               cfg.score_glow_blur,               0,   50)
    cfg.score_glow_alpha              = _i("score_glow_alpha",              cfg.score_glow_alpha,              0,   255)
    _gc_raw = (params.get("score_glow_color") or "").strip().lstrip("#").lower()
    if _gc_raw == "match":
        cfg.score_glow_color = "match"
    elif len(_gc_raw) == 6 and all(ch in "0123456789abcdef" for ch in _gc_raw):
        cfg.score_glow_color = _gc_raw
    else:
        cfg.score_glow_color = ""
    cfg.minimalist_mode_font_size_ratio = _f("minimalist_mode_font_size_ratio", cfg.minimalist_mode_font_size_ratio, 0.0, 0.5)
    cfg.minimalist_mode_font_x_offset = _f("minimalist_mode_font_x_offset", cfg.minimalist_mode_font_x_offset, 0.0, 1.0)
    cfg.minimalist_mode_font_y_offset = _f("minimalist_mode_font_y_offset", cfg.minimalist_mode_font_y_offset, 0.0, 1.0)
    cfg.minimalist_append_mode = _i("minimalist_append_mode", cfg.minimalist_append_mode, 0, 2)
    cfg.minimalist_score_out_of_10 = _b("minimalist_score_out_of_10", cfg.minimalist_score_out_of_10)

    cfg.bar_height_ratio        = _f("bar_height_ratio",        cfg.bar_height_ratio,        0.04, 0.20)
    cfg.bar_font_size_ratio     = _f("bar_font_size_ratio",     cfg.bar_font_size_ratio,     0.15, 0.70)
    cfg.bar_frost_opacity       = _f("bar_frost_opacity",       cfg.bar_frost_opacity,       0.0,  1.0)
    cfg.bar_frost_saturation    = _f("bar_frost_saturation",    cfg.bar_frost_saturation,    0.0,  2.0)
    cfg.frost_reference         = _b("frost_reference",         cfg.frost_reference)
    cfg.bar_bottom_inset        = _f("bar_bottom_inset",        cfg.bar_bottom_inset,        0.0,  0.10)
    _bst = (params.get("bar_style") or "").strip().lower()
    if _bst in ("frosted", "pure_black", "silver", "gold", "rating_black", "rating_frosted"):
        cfg.bar_style = _bst
    _bac = (params.get("bar_accent") or "").strip().lower()
    if _bac in ("silver", "gold", "sample", "palette_0", "palette_1", "palette_2", "palette_custom"):
        cfg.bar_accent = _bac
    cfg.bar_score_out_of_10     = _b("bar_score_out_of_10",     cfg.bar_score_out_of_10)
    _bap = (params.get("bar_append") or "").strip().lower()
    if _bap in ("rating_year", "rating", "year", "sash"):
        cfg.bar_append = _bap

    cfg.logo_max_w_ratio   = _f("logo_max_w_ratio",   cfg.logo_max_w_ratio,  0.0, 1.5)
    cfg.logo_max_h_ratio   = _f("logo_max_h_ratio",   cfg.logo_max_h_ratio,  0.0, 1.0)
    cfg.logo_bottom_ratio  = _f("logo_bottom_ratio",  cfg.logo_bottom_ratio, 0.0, 1.0)
    cfg.logo_bottom_anchor = _b("logo_bottom_anchor", cfg.logo_bottom_anchor)
    cfg.sash_winner_star   = _b("sash_winner_star",   cfg.sash_winner_star)

    # badge_height in pixels — generous enough to cover any reasonable customisation
    # but well below the size that would cost real memory on resize.
    cfg.badge_height             = _i("badge_height",             cfg.badge_height,             1,   200)
    cfg.badge_gap                = _i("badge_gap",                cfg.badge_gap,                0,   100)
    cfg.badge_anchor_x           = _f("badge_anchor_x",           cfg.badge_anchor_x,           0.0, 1.0)
    cfg.badge_anchor_y           = _f("badge_anchor_y",           cfg.badge_anchor_y,           0.0, 1.0)
    cfg.badge_min_score      = _i("badge_min_score",
                                  _i("combined_badge_min_score", cfg.badge_min_score, 2, 6),
                                  2, 6)
    cfg.combined_badge_stacked   = _b("combined_badge_stacked",   cfg.combined_badge_stacked)

    all_sources = list(_cfg.MOVIE_WEIGHTS.keys())
    cfg.movie_weights = _parse_weights(params.get("movie_weights"), all_sources)

    tv_sources = list(_cfg.TV_WEIGHTS.keys())
    cfg.tv_weights = _parse_weights(params.get("tv_weights"), tv_sources)
    cfg.fallback_to_imdb = _b("fallback_to_imdb", cfg.fallback_to_imdb)

    cfg.logo_language        = (params.get("logo_language", cfg.logo_language).strip().lower())
    cfg.logo_language_secondary = (
        params.get("logo_language_secondary", cfg.logo_language_secondary).strip().lower()
    )
    _lp = params.get("logo_priority")
    if _lp in (
        "native_original",
        "original_native",
        "native_if_original_english",
        "native_text",
        "native_custom_text",
        "native_custom_original_text",
    ):
        cfg.logo_priority = _lp
    elif "logo_native_fallback" in params:
        # Legacy param (boolean): true → native_original, false → native_text.
        cfg.logo_priority = "native_original" if _b("logo_native_fallback", True) else "native_text"
    _fbs = (params.get("fallback_bg_style") or "").strip().lower()
    if _fbs in ("minimal", "photoreal"):
        cfg.fallback_bg_style = _fbs
    cfg.use_original_art      = _b("use_original_art", cfg.use_original_art)
    _oas = (params.get("original_art_source") or "").strip().lower()
    if _oas in ("primary", "top_rated"):
        cfg.original_art_source = _oas
    cfg.sash_priority        = _parse_sash_priority(params.get("sash_priority"))
    cfg.rating_text_color    = _parse_hex_color(params.get("rating_text_color"))
    cfg.sash_text_color      = _parse_hex_color(params.get("sash_text_color"))

    return cfg


_PROFILE_DEFAULT_FIELDS = frozenset(RequestConfig.__dataclass_fields__) | {
    "combined_badge_min_score",
    "logo_native_fallback",
    "primary_client",
    "show_quality_badges",
}
_render_profile: RenderProfile | None = None


def _load_configured_render_profile() -> RenderProfile | None:
    if not _cfg.RENDER_PROFILE_PATH:
        return None
    return load_render_profile(_cfg.RENDER_PROFILE_PATH, set(_PROFILE_DEFAULT_FIELDS))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _resolved(value):
    return value


async def _with_retry(coro_fn, *args, **kwargs):
    """Call coro_fn(*args, **kwargs) and retry once if FETCH_FAILED is returned."""
    result = await coro_fn(*args, **kwargs)
    if result is FETCH_FAILED:
        result = await coro_fn(*args, **kwargs)
    return result


def _text_center(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    cx: float,
    cy: float,
) -> tuple[float, float]:
    bbox = draw.textbbox((0, 0), text, font=font)
    bbox_width = bbox[2] - bbox[0]
    ascent, descent = font.getmetrics()
    x = cx - bbox_width / 2 - bbox[0]
    optical_adjust = int(ascent * 0.22)
    y = cy - (ascent + descent) / 2 - descent + optical_adjust
    return x, y


# ---------------------------------------------------------------------------
# Poster composition
# ---------------------------------------------------------------------------

# Top-vignette strength.  Each entry maps a level name to
# (top_height_ratio, top_max_alpha).  None means "don't draw the gradient
# at all".  The "high" preset matches the legacy always-on behaviour so
# existing URLs / cached posters render identically when top_gradient is
# omitted.  Tweak the values here to retune any preset.
_TOP_GRADIENT_LEVELS: dict[str, tuple[float, int] | None] = {
    "off":    None,
    "low":    (0.20, 150),
    "medium": (0.25, 190),
    "high":   (0.40, 220),
}

# Bottom-vignette strength.  Same shape as the top gradient — (height_ratio,
# max_alpha).  Defaults to "high" which matches the legacy alpha-255 / 50%-
# height fade.  The previous auto-softening for Minimalist/Compact rating
# modes is dropped now that users can pick the level themselves; if you
# liked the softer look on those modes, set bottom_vignette=medium.
_BOTTOM_GRADIENT_LEVELS: dict[str, tuple[float, int] | None] = {
    "off":    None,
    "low":    (0.30, 180),
    "medium": (0.40, 210),
    "high":   (0.50, 225),
}
# Easing exponent shared across all bottom-gradient presets — controls the
# curve shape (1.0 = linear; >1 starts darker at the bottom and fades faster
# at the top).  Decoupled from strength so retuning one doesn't affect the
# other.
_BOTTOM_GRADIENT_CURVE = 1.5

# Genre-specific tint multipliers (R, G, B) for the fallback canvas.
# Applied to a dark base luminance of 10–18, so the dominant channel peaks
# around 30–55 at canvas midpoint — atmospheric rather than vivid.
# Names must match GENRE_MAP values exactly.
_GENRE_TINT: dict[str, tuple[float, float, float]] = {
    "Horror":      (3.2, 0.3, 0.3),   # deep blood red
    "Thriller":    (0.4, 2.2, 0.5),   # dark hunter green
    "Mystery":     (1.0, 0.3, 3.0),   # deep indigo
    "Sci-Fi":      (0.3, 1.2, 3.2),   # cold cyan-blue
    "Fantasy":     (1.6, 0.3, 3.0),   # purple-violet
    "Action":      (3.0, 0.8, 0.3),   # orange-red
    "Adventure":   (2.6, 1.5, 0.3),   # warm amber
    "Animation":   (0.4, 0.8, 3.2),   # electric blue
    "Comedy":      (2.6, 2.4, 0.3),   # golden yellow
    "Crime":       (2.4, 0.2, 0.2),   # dark crimson
    "Documentary": (0.3, 2.2, 2.4),   # teal
    "Drama":       (0.3, 0.3, 2.6),   # deep blue
    "Family":      (2.6, 1.2, 0.3),   # warm orange
    "History":     (2.2, 1.1, 0.3),   # sepia
    "Music":       (2.8, 0.3, 2.2),   # magenta
    "Romance":     (3.0, 0.3, 0.9),   # rose
    "War":         (0.9, 1.6, 0.3),   # olive green
    "Western":     (2.8, 1.1, 0.2),   # burnt sienna
    "Kids":        (0.3, 1.1, 3.0),   # bright blue
    "Reality":     (2.4, 0.8, 0.3),   # orange
    "Soap":        (2.6, 0.3, 0.9),   # rose-pink
    "Talk":        (0.3, 1.6, 2.4),   # teal-blue
    "News":        (0.3, 0.5, 2.6),   # steel blue
}
_FALLBACK_DEFAULT_TINT = (1.0, 1.0, 1.4)   # neutral cool blue

# Display-only label shortenings.  Some genre names are too wide for the poster
# label strip; shortening them reads better than shrinking the font.  These map
# the genre name to its *printed* form only — the original genre key is still
# used for font / colour / background lookups.
_GENRE_LABEL_OVERRIDES: dict[str, str] = {
    "Documentary": "Doc",
}


def _make_fallback_canvas(genre_ids: list[int] | None = None) -> Image.Image:
    """
    Dark gradient canvas served when a title has no poster art on TMDB.

    Applies a genre-derived colour tint so the canvas feels atmospheric rather
    than generically dark.  The base luminance is 10–18 (very dark) so even the
    dominant channel stays below ~55 — readable against white text overlays.
    """
    # Resolve genre → tint by walking GENRE_PRIORITY so higher-priority genres
    # win when a title belongs to multiple genres (same order as the score label).
    tint = _FALLBACK_DEFAULT_TINT
    if genre_ids:
        gid_set = set(genre_ids)
        for gid in _cfg.GENRE_PRIORITY:
            if gid in gid_set:
                name = _cfg.GENRE_MAP.get(gid)
                if name and name in _GENRE_TINT:
                    tint = _GENRE_TINT[name]
                    break

    r_mult, g_mult, b_mult = tint
    W, H = _cfg.POSTER_WIDTH, _cfg.POSTER_HEIGHT
    t    = np.linspace(0, np.pi, H, dtype=np.float32)
    # sin curve: peaks at midheight (~18), dark at top/bottom (~10)
    v    = (10 + 8 * np.sin(t)).astype(np.float32)
    arr  = np.zeros((H, W, 4), dtype=np.uint8)
    # Clamp BEFORE casting to uint8 — casting first would wrap mod-256 on
    # any value above 255, silently inverting colour for high-multiplier tints.
    arr[:, :, 0] = np.minimum(255, v * r_mult).astype(np.uint8)[:, np.newaxis]
    arr[:, :, 1] = np.minimum(255, v * g_mult).astype(np.uint8)[:, np.newaxis]
    arr[:, :, 2] = np.minimum(255, v * b_mult).astype(np.uint8)[:, np.newaxis]
    arr[:, :, 3] = 255
    return Image.fromarray(arr, "RGBA")


def _draw_combined_text_badge(
    image: Image.Image,
    tokens: list[str],
    *,
    x: int,
    y: int,
    font_size: int,
    min_score: int = 2,
    stacked: bool = False,
) -> None:
    """Minimalist quality badge: Resolution [sep] Visual Tag

    Horizontal layout: "4K  |  HDR"  — vertical pip coloured by source.
    Stacked layout:    "4K / HDR"    — horizontal rule coloured by source,
                       stacked like a division formula (for tight notch space).

    The separator colour encodes the source — gold for Remux, silver for Web.
    Nothing is drawn if resolution or source tokens are absent, or if the
    combined quality score is below *min_score*.
    """
    token_set = set(tokens)

    if tokens and _score_points(tokens) < min_score:
        return

    if "4K" in token_set:
        res = "4K"
    elif "1080P" in token_set:
        res = "HD"
    else:
        return

    if "REMUX" in token_set:
        sep_color = (255, 210,  60)   # gold
    elif "WEBDL" in token_set:
        sep_color = (192, 192, 200)   # silver
    else:
        return

    if "DV" in token_set:
        fmt = "DV"
    elif "HDR10+" in token_set:
        fmt = "HDR+"
    elif "HDR10" in token_set:
        fmt = "HDR"
    else:
        fmt = "SDR"

    try:
        font = ImageFont.truetype(os.path.join(_FONTS_DIR, "Inter-Bold.ttf"), font_size)
    except IOError:
        font = ImageFont.load_default()

    draw = ImageDraw.Draw(image)
    ink  = (235, 235, 235, 255)

    if stacked:
        # Use textbbox so spacing is based on actual rendered glyph bounds,
        # not the full em-square (which includes invisible descender space and
        # would pin the line visually against the resolution text).
        b_res   = draw.textbbox((0, 0), res, font=font)
        b_fmt   = draw.textbbox((0, 0), fmt, font=font)
        w_res   = b_res[2] - b_res[0]
        w_fmt   = b_fmt[2] - b_fmt[0]
        h_res   = b_res[3] - b_res[1]   # actual glyph height, no dead space
        total_w = max(w_res, w_fmt)
        line_h  = max(2, font_size // 12)
        v_gap   = max(4, font_size // 5)

        # Resolution — draw so its visual top sits at y
        res_x = x + (total_w - w_res) // 2 - b_res[0]
        res_y = y - b_res[1]
        draw.text((res_x, res_y), res, font=font, fill=ink)

        # Horizontal rule — v_gap below the actual glyph bottom
        ly = y + h_res + v_gap
        draw.rounded_rectangle(
            [x, ly, x + total_w, ly + line_h],
            radius=line_h // 2,
            fill=sep_color,
        )

        # Visual tag — v_gap below the rule, aligned to its own visual top
        fmt_x = x + (total_w - w_fmt) // 2 - b_fmt[0]
        fmt_y = ly + line_h + v_gap - b_fmt[1]
        draw.text((fmt_x, fmt_y), fmt, font=font, fill=ink)

    else:
        pip_gap = int(font_size * 0.55)
        pip_w   = max(3, int(font_size * 0.15))
        pip_h   = int(font_size * 1.3)
        pip_cy  = y + round(font_size * 0.60)

        cx = x
        draw.text((cx, y), res, font=font, fill=ink)
        cx += round(draw.textlength(res, font=font)) + pip_gap
        _draw_solid_pip(image, x=cx, y_center=pip_cy, width=pip_w, height=pip_h, color=sep_color)
        cx += pip_w + pip_gap
        draw.text((cx, y), fmt, font=font, fill=ink)


def build_poster(
    image: Image.Image,
    score: int | str,
    genre: str,
    cfg: RequestConfig,
    logo: Image.Image | None = None,
    fallback_title: str | None = None,
    discovery_meta: DiscoveryMeta | None = None,
    quality_tokens: list[str] | None = None,
    release_year: str | None = None,
    age_rating: int | None = None,
    no_poster: bool = False,
) -> Image.Image:

    width, height = image.size

    # Greyscale the base art to flag "not available".  Overlays drawn afterwards
    # (sashes, badges, ratings, logo) stay in colour.  Two independent triggers:
    #   - cinema_greyscale: title still in cinemas / production (release_status,
    #     so implicitly gated on the release-status sash being enabled).
    #   - greyscale_no_quality: no stream quality was found.  Only meaningful
    #     when wait_for_quality is on (otherwise tokens may just not be fetched
    #     yet), so it's gated on it.
    _cinema_grey = (cfg.cinema_greyscale and discovery_meta is not None
                    and discovery_meta.release_status in ("Cinema", "Production"))
    # Override: if a real digital source (Web / Remux) was found, the title is
    # actually available — keep it in colour despite the cinema/production status.
    if (_cinema_grey and cfg.cinema_greyscale_skip_if_available and quality_tokens
            and any(t in ("WEBDL", "REMUX") for t in quality_tokens)):
        _cinema_grey = False
    _noquality_grey = (cfg.greyscale_no_quality and cfg.wait_for_quality and not quality_tokens)
    if _cinema_grey or _noquality_grey:
        image = ImageOps.grayscale(image).convert("RGBA")

    draw = ImageDraw.Draw(image)

    # Printed form of the genre.  Translate the canonical English name when a
    # translation exists for the request language; otherwise keep the English
    # path including the space-saving override (e.g. "Documentary" → "Doc").
    _genre_tr = translate_genre(genre, cfg.logo_language)
    if _genre_tr != genre:
        genre_label = _genre_tr
    else:
        genre_label = _GENRE_LABEL_OVERRIDES.get(genre, genre)

    if cfg.hide_genre:
        genre_label = ""
        
    # Resolve the info-sash pick once, regardless of whether the diagonal sash
    # itself is rendered independently.
    #
    # When greyscale is active on an unreleased title (Cinema / Production),
    # force the release-status slot to the front so its badge always wins — that
    # tells the user the poster is greyscale because it's unavailable, rather
    # than a title whose art happens to be black & white.
    _sash_priority = cfg.sash_priority
    if (cfg.cinema_greyscale and discovery_meta is not None
            and discovery_meta.release_status in ("Cinema", "Production")):
        _status = discovery_meta.release_status.lower()
        if _status in _sash_priority or "release_status" in _sash_priority:
            _sash_priority = [s for s in _sash_priority if s in ("release_status", _status)] + [s for s in _sash_priority if s not in ("release_status", _status)]
    sash_result = (
        pick_sash(discovery_meta, _sash_priority)
        if discovery_meta is not None
        else None
    )

    # Snapshot the artwork *before* the vignette gradients darken it. The frosted
    # bar/notch/sash sample their tint colour from this, not the graded image —
    # otherwise the near-black top/bottom the gradients paint on drags the sampled
    # colour to grey (e.g. a blue sky reads as white behind the notch).
    _frost_color_src = image.copy()

    # --- TOP GRADIENT (vectorised) ---
    # Darkens the top of the poster so the age-rating numeral and quality
    # badges stay legible over bright art.  Strength is one of four presets
    # (off / low / medium / high) — see _TOP_GRADIENT_LEVELS for the
    # (height_ratio, max_alpha) tuple each level uses.  Unknown level is
    # treated as "high" rather than skipped so a typo in a URL doesn't
    # silently disable the vignette.
    _tg_preset: tuple[float, int] | None
    if cfg.top_gradient == "custom" and cfg.top_gradient_opacity is not None and cfg.top_gradient_height is not None:
        _tg_preset = (cfg.top_gradient_height, int(cfg.top_gradient_opacity * 255 if cfg.top_gradient_opacity <= 1.0 else cfg.top_gradient_opacity))
    else:
        _tg_preset = _TOP_GRADIENT_LEVELS.get(cfg.top_gradient, _TOP_GRADIENT_LEVELS["high"])
        
    if _tg_preset is not None and (not cfg.top_vignette_sash_only or sash_result is not None):
        top_height_ratio, top_max_alpha = _tg_preset
        top_height = max(1, int(height * top_height_ratio))
        t_top = np.linspace(0, 1, top_height, dtype=np.float32)
        eased_top = ((1 - t_top) * top_max_alpha).astype(np.uint8)
        top_array = np.broadcast_to(eased_top[:, np.newaxis], (top_height, width)).copy()
        top_overlay = Image.fromarray(top_array, mode="L")
        top_tinted = Image.new("RGBA", (width, top_height), (0, 0, 0, 0))
        top_tinted.putalpha(top_overlay)
        image.paste(top_tinted, (0, 0), mask=top_tinted)

    # --- BOTTOM GRADIENT (vectorised) ---
    # Strength is one of four presets (off / low / medium / high) — see
    # _BOTTOM_GRADIENT_LEVELS for the (height_ratio, max_alpha) tuple each
    # level uses.  The previous auto-softening for Minimalist / Compact modes
    # is dropped now that the user can pick the level themselves; if you'd
    # like the lighter fade those modes used to get for free, pick "medium".
    # Unknown level falls back to "high" so a typo can't accidentally turn
    # the fade off entirely (which would break label legibility).
    if cfg.bottom_gradient == "custom" and cfg.bottom_gradient_opacity is not None and cfg.bottom_gradient_height is not None:
        _bg_preset = (cfg.bottom_gradient_height, int(cfg.bottom_gradient_opacity * 255 if cfg.bottom_gradient_opacity <= 1.0 else cfg.bottom_gradient_opacity))
    else:
        _bg_preset = _BOTTOM_GRADIENT_LEVELS.get(cfg.bottom_gradient, _BOTTOM_GRADIENT_LEVELS["high"])
        
    if _bg_preset is not None:
        bottom_height_ratio, bottom_max_alpha = _bg_preset
        bottom_height = max(1, int(height * bottom_height_ratio))
        bottom_start  = height - bottom_height
        t_bot         = np.linspace(0, 1, bottom_height, dtype=np.float32)
        eased_bot     = ((1 - (1 - t_bot) ** _BOTTOM_GRADIENT_CURVE) * bottom_max_alpha).astype(np.uint8)
        bottom_array  = np.broadcast_to(eased_bot[:, np.newaxis], (bottom_height, width)).copy()
        bottom_overlay = Image.fromarray(bottom_array, mode="L")
        bottom_tinted  = Image.new("RGBA", (width, bottom_height), (0, 0, 0, 0))
        bottom_tinted.putalpha(bottom_overlay)
        image.paste(bottom_tinted, (0, bottom_start), mask=bottom_tinted)

    # --- Badge / quality overlay ---
    mode   = cfg.badge_display_mode
    tokens = quality_tokens or []

    if mode == 1:
        # If quality is below the threshold, strip the quality tokens so the
        # badge renders silver/default rather than a misleadingly coloured tier.
        _tokens_1 = (
            tokens
            if (not tokens or _score_points(tokens) >= cfg.badge_min_score)
            else []
        )
        draw_quality_age_badge(
            image,
            age_rating,
            _tokens_1,
            anchor_x_ratio=cfg.badge_anchor_x,
            anchor_y_ratio=cfg.badge_anchor_y,
            badge_height=cfg.badge_height,
        )

    elif mode == 3:
        # Age rating only — always silver, no quality dependency
        draw_quality_age_badge(
            image,
            age_rating,
            [],
            anchor_x_ratio=cfg.badge_anchor_x,
            anchor_y_ratio=cfg.badge_anchor_y,
            badge_height=cfg.badge_height,
            always_silver=True,
        )

    elif mode == 4:
        # Accent bar — small vertical pill in tier colour, no text
        if not tokens or _score_points(tokens) >= cfg.badge_min_score:
            draw_tier_bar(
                image,
                tokens,
                anchor_x_ratio=cfg.badge_anchor_x,
                anchor_y_ratio=cfg.badge_anchor_y,
                bar_height=cfg.badge_height,
            )

    elif mode == 2:
        allowed_tokens  = {"4K", "1080P", "REMUX", "WEBDL", "DV", "HDR10+", "HDR10"}
        filtered_tokens = [t for t in tokens if t in allowed_tokens]

        if filtered_tokens and _score_points(tokens) >= cfg.badge_min_score:
            bx = int(width  * cfg.badge_anchor_x)
            by = int(height * cfg.badge_anchor_y)

            badge_items: list[BadgeItem] = [
                (get_resized_badge(token, cfg.badge_height), _cfg.QUALITY_LABELS.get(token, token))
                for token in filtered_tokens
            ]

            render_badges_left(
                image, badge_items,
                x_start=bx, y_top=by,
                badge_height=cfg.badge_height,
                badge_gap=cfg.badge_gap,
            )

    elif mode == 5:
        _draw_combined_text_badge(
            image, tokens,
            x=int(width  * cfg.badge_anchor_x),
            y=int(height * cfg.badge_anchor_y),
            font_size=cfg.badge_height,
            min_score=cfg.badge_min_score,
            stacked=cfg.combined_badge_stacked,
        )

    # --- Logo / fallback title ---
    if logo:
        composite_logo(
            image, logo,
            max_w_ratio=cfg.logo_max_w_ratio,
            max_h_ratio=cfg.logo_max_h_ratio,
            bottom_ratio=cfg.logo_bottom_ratio,
            bottom_anchor=cfg.logo_bottom_anchor,
        )
    elif fallback_title:
        # ── Genre-aware font selection ────────────────────────────────────────
        # Titles are bucketed by genre and rendered in a thematically matching
        # font so different content categories feel distinct.
        #
        # Bucket → font mapping:
        #   Horror / Thriller / Mystery  → Creepster  (gothic, unsettling)
        #   Action / Sci-Fi / Adventure  → Bebas Neue (bold, cinematic)
        #   Comedy / Animation / Family  → Pacifico   (friendly, rounded)
        #   Drama / Romance / History    → Playfair   (elegant, literary)
        #   Crime / War / Documentary    → Oswald     (authoritative, strong)
        #   Default                      → NotoSerif  (neutral, readable)
        _GENRE_FONTS: dict[str, str] = {
            "Horror":           "Creepster-Regular.ttf",
            "Thriller":         "Creepster-Regular.ttf",
            "Mystery":          "Creepster-Regular.ttf",
            "Action":           "BebasNeue-Bold.ttf",
            "Sci-Fi":           "BebasNeue-Bold.ttf",
            "Adventure":        "BebasNeue-Bold.ttf",
            "Fantasy":          "BebasNeue-Bold.ttf",
            "Western":          "BebasNeue-Bold.ttf",
            "Comedy":           "Pacifico-Regular.ttf",
            "Animation":        "Pacifico-Regular.ttf",
            "Family":           "Pacifico-Regular.ttf",
            "Drama":            "PlayfairDisplay-Bold.ttf",
            "Romance":          "PlayfairDisplay-Bold.ttf",
            "History":          "PlayfairDisplay-Bold.ttf",
            "Music":            "PlayfairDisplay-Bold.ttf",
            "Crime":            "Oswald-Bold.ttf",
            "War":              "Oswald-Bold.ttf",
            "Documentary":      "Oswald-Bold.ttf",
        }
        _font_file = _GENRE_FONTS.get(genre, "NotoSerif-Bold.ttf")

        # Fallback-title rendering, sized to fill the SAME envelope a logo fills
        # (cfg.logo_max_w_ratio width × logo_max_h_ratio height) so a text title
        # looks as substantial as a logo would — short titles like "SELF-HELP"
        # grow to fill the width instead of being pinned tiny by a char-count
        # heuristic.  The logo size ratios therefore tune the fallback text too.
        max_w          = max(1, int(width * cfg.logo_max_w_ratio))
        max_h          = max(1, min(int(height * cfg.logo_max_h_ratio), LOGO_ABS_MAX_H))
        MIN_FONT_SIZE  = 22
        MAX_LINES      = 2
        FONT_PATH      = os.path.join(_FONTS_DIR, _font_file)

        def _line_width(text: str, current_font) -> int:
            bbox = draw.textbbox((0, 0), text, font=current_font)
            return int(bbox[2] - bbox[0])

        def _wrap_lines(text: str, current_font) -> list[str]:
            """Greedy word-wrap: each line packs as many words as fit within max_w."""
            words = text.split()
            if not words:
                return []
            lines: list[str] = []
            current: list[str] = []
            for word in words:
                candidate = " ".join(current + [word])
                if _line_width(candidate, current_font) <= max_w or not current:
                    current.append(word)
                else:
                    lines.append(" ".join(current))
                    current = [word]
            if current:
                lines.append(" ".join(current))
            return lines

        def _measure_block(lines_to_measure: list[str], current_font, line_gap: int) -> tuple[int, int, list[tuple[str, tuple[int, int, int, int]]]]:
            line_boxes = [
                (line, draw.textbbox((0, 0), line, font=current_font))
                for line in lines_to_measure
            ]
            if not line_boxes:
                return 0, 0, []
            widths = [bbox[2] - bbox[0] for _, bbox in line_boxes]
            heights = [bbox[3] - bbox[1] for _, bbox in line_boxes]
            block_w = max(widths)
            block_h = sum(heights) + line_gap * (len(line_boxes) - 1)
            return block_w, block_h, line_boxes

        # Pick the largest font whose wrapped block fits the logo envelope: scan
        # high to low and take the first fit. Text fallbacks use the same hard
        # width/height envelope as image logos, including the absolute height cap
        # and bottom-anchor baseline semantics.
        try:
            font_size = MIN_FONT_SIZE
            font      = ImageFont.truetype(FONT_PATH, font_size)
            lines     = _wrap_lines(fallback_title, font)
            shadow_offset = max(2, int(font_size * 0.04))
            block_w, block_h, line_boxes = _measure_block(
                lines, font, max(1, int(font_size * 0.12))
            )
            for _fs in range(int(height * 0.26), 7, -2):
                _f  = ImageFont.truetype(FONT_PATH, _fs)
                _ls = _wrap_lines(fallback_title, _f)
                if len(_ls) > MAX_LINES:
                    continue
                _gap = max(1, int(_fs * 0.12))
                _shadow = max(2, int(_fs * 0.04))
                _block_w, _block_h, _line_boxes = _measure_block(_ls, _f, _gap)
                if _block_w + _shadow <= max_w and _block_h + _shadow <= max_h:
                    font, font_size, lines = _f, _fs, _ls
                    shadow_offset = _shadow
                    block_w, block_h, line_boxes = _block_w, _block_h, _line_boxes
                    break
        except OSError:
            font      = ImageFont.load_default()
            font_size = MIN_FONT_SIZE
            lines     = [fallback_title]
            shadow_offset = max(2, int(font_size * 0.04))
            block_w, block_h, line_boxes = _measure_block(lines, font, max(1, int(font_size * 0.12)))

        if lines and line_boxes:
            layer_w = max(1, int(np.ceil(block_w + shadow_offset)))
            layer_h = max(1, int(np.ceil(block_h + shadow_offset)))
            text_layer = Image.new("RGBA", (layer_w, layer_h), (0, 0, 0, 0))
            layer_draw = ImageDraw.Draw(text_layer)

            cursor_y = 0
            line_gap = max(1, int(font_size * 0.12))
            for line, bbox in line_boxes:
                line_w = bbox[2] - bbox[0]
                line_h = bbox[3] - bbox[1]
                tx = (block_w - line_w) / 2 - bbox[0]
                ty = cursor_y - bbox[1]
                layer_draw.text((tx + shadow_offset, ty + shadow_offset), line, font=font, fill=(0, 0, 0, 180))
                layer_draw.text((tx, ty),                                  line, font=font, fill=(255, 255, 255, 255))
                cursor_y += line_h + line_gap

            scale = min(max_w / text_layer.width, max_h / text_layer.height, 1.0)
            if scale < 1.0:
                text_layer = text_layer.resize(
                    (max(1, int(text_layer.width * scale)), max(1, int(text_layer.height * scale))),
                    Image.Resampling.LANCZOS,
                )

            logo_x = round((width - text_layer.width) / 2)
            if cfg.logo_bottom_anchor:
                baseline = height - int(height * cfg.logo_bottom_ratio)
                logo_y = baseline - text_layer.height
            else:
                centre_y = logo_centre_y(height, cfg.logo_bottom_ratio)
                logo_y = int(centre_y - text_layer.height / 2)
            image.paste(text_layer, (logo_x, logo_y), text_layer)


    # --- Frosted tint colour -------------------------------------------------
    # Every frosted element (rating bar, notch badge, poster-coloured sash) tints
    # from ONE whole-poster colour sample, taken from the un-graded artwork. Since
    # they all draw from the same sample they always match automatically — so the
    # bar simply adopts the notch's colour (and, below, its saturation) whenever a
    # frosted notch is shown, with no separate "match" toggle needed.
    _sash_shown    = cfg.sash_mode != "hidden" and sash_result is not None
    _notch_frosted = _sash_shown and cfg.sash_mode == "notch" and cfg.sash_badge_style == "frosted"
    _sash_poster   = _sash_shown and cfg.sash_mode == "sash" and cfg.sash_poster_color
    _bar_frosted   = cfg.rating_display_mode == 4 and cfg.bar_style in ("frosted", "rating_frosted")
    _frost_tint: tuple[float, float, float] | None = (
        dominant_frost_rgb(_frost_color_src)
        if (_bar_frosted or _notch_frosted or _sash_poster) else None
    )
    # One saturation for every frosted element: a frosted notch owns it (its slider
    # lives in the sash panel); otherwise the rating bar's slider drives it. Sharing
    # it keeps the bar and any sash/notch identical.
    _frost_sat = cfg.sash_badge_frost_saturation if _notch_frosted else cfg.bar_frost_saturation

    # --- Rating / genre label ---
    if cfg.rating_display_mode != 0:

        if cfg.rating_display_mode == 1:
            font_size = int(width * cfg.accent_bar_font_size_ratio)
            # Label suffix is configurable: append year, append sash text, or
            # append both joined by " · ".  Missing data degrades gracefully —
            # if "sash" is requested but no sash triggered, we just show the
            # genre; if "both" but only one is present, we show whichever did.
            #
            # The separator immediately before the sash text becomes "★" when
            # the sash is a winner (sash_type == "win") rather than "·".  Same
            # disambiguation trick used by Compact mode — festival wins and
            # nominees can share their label text, so without this they'd be
            # indistinguishable here.
            _append_year = cfg.accent_bar_append_mode in (0, 2)
            _append_sash = cfg.accent_bar_append_mode in (1, 2)
            _sash_text_for_label, _sash_type_for_label = (
                sash_result if (_append_sash and sash_result) else (None, None)
            )

            _pre_sash = [genre_label] if genre_label else []
            if _append_year and release_year:
                _pre_sash.append(str(release_year))
            _label_main = " · ".join(_pre_sash)

            if _sash_text_for_label:
                label = _label_main + " · " + translate_sash(_sash_text_for_label, cfg.logo_language) if _label_main else translate_sash(_sash_text_for_label, cfg.logo_language)
            else:
                label = _label_main
            rating_cy = height * cfg.accent_bar_y_offset

            try:
                font_meta = ImageFont.truetype(os.path.join(_FONTS_DIR, "Inter-Bold.ttf"), font_size)
            except IOError:
                font_meta = ImageFont.load_default()

            tx, ty = _text_center(draw, label, font_meta, width / 2, rating_cy)  # type: ignore
            draw.text(
                (tx, ty - int(font_size * 0.10)),
                label,
                font=font_meta,
                fill=(*cfg.rating_text_color, 255) if cfg.rating_text_color else (200, 200, 200, 255),
            )
            if cfg.score_glow_color == "match":
                _glow_color = "match"
            elif len(cfg.score_glow_color) == 6:
                _glow_color = tuple(int(cfg.score_glow_color[i:i+2], 16) for i in (0, 2, 4))
            else:
                _glow_color = None
            draw_score_bar(
                image, score,
                bottom_margin=int(height * cfg.accent_bar_bottom_ratio),
                glow_threshold=cfg.score_glow_threshold,
                glow_blur=cfg.score_glow_blur,
                glow_alpha=cfg.score_glow_alpha,
                glow_color=_glow_color,
                color_mode=cfg.score_color_mode,
                custom_palette=cfg.score_custom_palette,
            )

        elif cfg.rating_display_mode == 2:
            font_size = int(width * cfg.numeric_score_font_size_ratio)
            # Score formatting:
            #   out of 100 (default): "87", "100", "N/A"
            #   out of 10:            "8.7", "8.0" (always one decimal), "10"
            #                         (no decimal — already two glyphs wide)
            # Non-numeric scores ("N/A") pass through unchanged in either mode.
            if cfg.score_out_of_10 and isinstance(score, (int, float)):
                _score_text = "10" if score >= 100 else f"{score / 10:.1f}"
            else:
                _score_text = str(score)
            label = f"{genre_label} ★ {_score_text}" if genre_label else f"★ {_score_text}"
            rating_cy = height * cfg.numeric_score_y_offset

            try:
                font_meta = ImageFont.truetype(os.path.join(_FONTS_DIR, "Inter-Bold.ttf"), font_size)
            except IOError:
                font_meta = ImageFont.load_default()

            tx, ty = _text_center(draw, label, font_meta, width / 2, rating_cy)  # type: ignore
            draw.text(
                (tx, ty - int(font_size * 0.10)),
                label,
                font=font_meta,
                fill=(*cfg.rating_text_color, 255) if cfg.rating_text_color else (200, 200, 200, 255),
            )

        elif cfg.rating_display_mode == 3:
            font_size = int(width * cfg.minimalist_mode_font_size_ratio)

            try:
                font_meta = ImageFont.truetype(os.path.join(_FONTS_DIR, "Inter-Bold.ttf"), font_size)
            except IOError:
                font_meta = ImageFont.load_default()

            y = round(height * cfg.minimalist_mode_font_y_offset)
            right_edge = width - int(width * cfg.minimalist_mode_font_x_offset)
            _ink = (*cfg.rating_text_color, 255) if cfg.rating_text_color else (235, 235, 235, 255)

            # Segments, each tagged with the SEPARATOR that precedes it:
            #   "pip"  — silver vertical pip (before the year)
            #   "star" — ★ glyph (before the rating/score)
            #   "rpip" — pip COLOURED by score (mode 0 only: the rating shown
            #            purely by colour, no number)
            # Mode 0 ("Year"): genre [rating-pip] year
            # Mode 1 ("Rating"): genre ★ score
            # Mode 2 ("Year + Rating"): genre [pip] year ★ score
            _has_score = score not in ("N/A", None)
            # Score formatting matches the other modes: out of 100 by default,
            # one decimal out of 10 ("8.7"), with a bare "10" at the top.
            if _has_score and cfg.minimalist_score_out_of_10:
                _score_str = "10" if int(score) >= 100 else f"{int(score) / 10:.1f}"
            else:
                _score_str = str(score)
            parts = [(genre_label, None)] if genre_label else []
            if cfg.minimalist_append_mode == 0:
                if release_year:
                    parts.append((str(release_year), "rpip" if parts else None))
            elif cfg.minimalist_append_mode == 1:
                if _has_score:
                    parts.append((_score_str, "star" if parts else None))
            else:  # 2 — Year + Rating
                if release_year:
                    parts.append((str(release_year), "pip" if parts else None))
                if _has_score:
                    parts.append((_score_str, "star" if parts else None))

            pip_gap = int(font_size * 0.55)
            pip_w   = max(4, int(font_size * 0.18))
            pip_h   = int(font_size * 1.4)
            pip_cy  = round(y + font_size * 0.60)
            star_w  = draw.textlength("★", font=font_meta)

            # Lay out right-to-left: each segment, with its separator to its left.
            cursor = right_edge
            ops    = []   # (kind, x[, text]); kind in text|pip|rpip|star
            for i in range(len(parts) - 1, -1, -1):
                seg, sep = parts[i]
                seg_x = int(cursor - draw.textlength(seg, font=font_meta))
                ops.append(("text", seg_x, seg))
                cursor = seg_x
                if sep:
                    cursor -= pip_gap
                    sep_w  = star_w if sep == "star" else pip_w
                    sep_x  = cursor - sep_w
                    ops.append((sep, sep_x))
                    cursor = sep_x - pip_gap

            for op in ops:
                kind, ox = op[0], op[1]
                if kind == "text":
                    draw.text((ox, y), op[2], font=font_meta, fill=_ink)
                elif kind == "star":
                    draw.text((ox, y), "★", font=font_meta, fill=_ink)
                elif kind == "rpip":
                    draw_score_bar_vertical(image, score, x=ox, y_center=pip_cy,
                                            height=pip_h, width=pip_w,
                                            color_mode=cfg.score_color_mode,
                                            custom_palette=cfg.score_custom_palette)
                else:  # "pip"
                    _draw_solid_pip(image, x=ox, y_center=pip_cy,
                                    width=pip_w, height=pip_h, color=(192, 192, 200))

        elif cfg.rating_display_mode == 4:
            # Frosted bar — centred dot-separated label at the bottom.
            # Format: Year · Genre · ★ Rating  (omit any missing field)
            _has_score = score not in ("N/A", None)
            if _has_score:
                if cfg.bar_score_out_of_10:
                    _score_str = "10" if int(score) >= 100 else f"{int(score) / 10:.1f}"
                else:
                    _score_str = str(score)
            else:
                _score_str = ""
            _year_str  = str(release_year) if release_year else ""
            _bar_sash, _ = sash_result if sash_result else (None, None)
            if cfg.bar_append == "rating_year":
                _parts = [_year_str, genre_label or "", f"★ {_score_str}" if _score_str else ""]
            elif cfg.bar_append == "rating":
                _parts = [genre_label or "", f"★ {_score_str}" if _score_str else ""]
            elif cfg.bar_append == "year":
                _parts = [_year_str, genre_label or ""]
            else:  # "sash"
                _parts = [genre_label or "", translate_sash(_bar_sash, cfg.logo_language) if _bar_sash else ""]
            _parts = [p for p in _parts if p]
            _sep = "  ·  " if len(_parts) <= 2 else " · "
            image = draw_frosted_bar(
                image,
                left_text   = "",
                center_text = _sep.join(_parts),
                right_text  = "",
                bar_height_ratio = cfg.bar_height_ratio,
                font_size_ratio  = cfg.bar_font_size_ratio,
                frost_opacity    = cfg.bar_frost_opacity,
                frost_saturation = _frost_sat,
                frost_reference  = cfg.frost_reference,
                bottom_inset     = cfg.bar_bottom_inset,
                style            = cfg.bar_style,
                score            = score if score not in ("N/A", None) else None,
                fill_color       = (
                    None  # "sample" → let draw_frosted_bar derive from bar tint
                    if cfg.bar_accent == "sample" else
                    {"silver": (210, 210, 218), "gold": (212, 175, 55)}.get(cfg.bar_accent)
                    or (
                        score_color_for_mode(
                            int(score),
                            3 if cfg.bar_accent == "palette_custom" else int(cfg.bar_accent[-1]),
                            cfg.score_custom_palette,
                        )[0]
                        if score not in ("N/A", None) else (210, 210, 218)
                    )
                ) if cfg.bar_style in ("rating_black", "rating_frosted") else None,
                tint_rgb         = _frost_tint,
                text_color       = cfg.rating_text_color,
            )

    # --- Discovery sash / badge ---
    if cfg.sash_mode != "hidden" and sash_result is not None:
        label, sash_type = sash_result
        _is_star  = cfg.sash_winner_star and sash_type == "win"
        _label_tr = translate_sash(label, cfg.logo_language)
        if cfg.sash_mode == "notch":
            image = draw_award_badge(image, _label_tr, sash_type=sash_type,
                                     size_ratio_w=cfg.sash_badge_size_w,
                                     size_ratio_h=cfg.sash_badge_size_h,
                                     notch_style=cfg.sash_badge_style,
                                     notch_inset=cfg.sash_badge_inset,
                                     font_size_ratio=cfg.sash_badge_font_ratio,
                                     frost_opacity=cfg.sash_badge_frost_opacity,
                                     frost_saturation=cfg.sash_badge_frost_saturation,
                                     frost_reference=cfg.frost_reference,
                                     tint_rgb=_frost_tint,
                                     star=_is_star,
                                     text_color=cfg.sash_text_color)
        else:  # "sash" — diagonal
            _poster_color = _frost_tint if cfg.sash_poster_color else None
            image = draw_award_sash(image, _label_tr, sash_type=sash_type, muted=cfg.muted,
                                    length_ratio=cfg.sash_length_ratio,
                                    height_ratio=cfg.sash_height_ratio,
                                    poster_color=_poster_color,
                                    frost_saturation=_frost_sat,
                                    frost_reference=cfg.frost_reference,
                                    star=_is_star,
                                    text_color=cfg.sash_text_color)

    return image


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def _cache_prune_loop() -> None:
    """Periodically prune expired rows from all cache tables."""
    # Wait a few minutes after startup before the first run so the service
    # is fully warmed before taking the SQLite write lock.
    await asyncio.sleep(300)
    while True:
        logger.info("Running scheduled cache prune")
        await asyncio.get_running_loop().run_in_executor(None, prune_caches)

        # Evict expired entries from the in-process rating backoff dict.
        # Entries are also removed lazily on access, but titles that are never
        # re-requested would otherwise accumulate indefinitely.
        _now = asyncio.get_running_loop().time()
        expired = [k for k, v in _rating_backoff.items() if v <= _now]
        for k in expired:
            del _rating_backoff[k]
        if expired:
            logger.debug(f"Pruned {len(expired)} expired rating backoff entries")

        await asyncio.sleep(6 * 3600)   # every 6 hours


async def _run_cache_warm_cycle(client: httpx.AsyncClient) -> None:
    """
    Pre-populate the TMDB metadata, poster/backdrop image, and logo caches,
    plus the MDBList rating/award cache, for currently-trending titles — so
    the first real requests for them don't all hit upstream APIs/CDNs at
    once. The poster/backdrop + logo downloads are the slowest, most
    bandwidth-heavy part of a real request and the most likely to cause a
    burst-traffic pile-up against a stale cache, so every processed
    candidate gets the same default art fetched as a real view would.

    If CACHE_WARM_CATALOG_URLS is set, the catalogs exposed by those addon
    manifests are fetched first (the same way a Stremio client would when a
    user opens that catalog) and warmed ahead of trending/popular/
    supplemental, capped at CACHE_WARM_CATALOG_MAX_ITEMS items per catalog.

    Single pass over a ranked, deduped candidate list (catalog, then
    trending, then popular, then supplemental — top rated / now playing /
    on the air): walks until the TMDB metadata budget is spent or the
    candidate list is exhausted. MDBList lookups are interleaved for as long
    as the MDBList budget allows, then the loop continues warming TMDB-only
    for the remainder. Both budgets only count actual cache-miss
    metadata/rating API calls — entries already warm (including images and
    logos) cost nothing.

    If CACHE_WARM_QUALITY_ENABLED is set, also pre-fetches quality badge
    data (resolution/source/HDR tokens) for every processed candidate via
    the configured quality source (series default to S01E01). This is off
    by default — see the config comment for why.
    """
    global _mdblist_semaphore, _mdblist_active_key_idx, _quality_bg_semaphore

    if not _cfg.SERVER_TMDB_KEY:
        logger.info("Cache warm: skipped — no server TMDB key configured")
        return

    tmdb_budget    = max(0, _cfg.CACHE_WARM_TMDB_BUDGET)
    mdblist_budget = max(0, _cfg.CACHE_WARM_MDBLIST_BUDGET)
    if tmdb_budget == 0:
        logger.info("Cache warm: skipped — CACHE_WARM_TMDB_BUDGET is 0")
        return

    effective_mdblist_key = _resolve_mdblist_key("") if _cfg.SERVER_MDBLIST_KEYS else None
    if not effective_mdblist_key:
        mdblist_budget = 0

    # Mix three sources: trending (volatile, day/week hot list), popular
    # (broad, slow-moving catalogue staples), and supplemental (top rated /
    # now playing / on the air — acclaimed and currently-airing titles that
    # trending and popular tend to miss). Split the target list across the
    # three so warming covers "what's hot right now", "what people steadily
    # watch", and "what's airing/acclaimed". Each source is independently
    # ranked/deduped; the combined list is deduped again here so a title
    # appearing in multiple sources only costs one slot.
    target_total  = max(tmdb_budget, mdblist_budget) or tmdb_budget
    trending_target    = (target_total * 4 + 9) // 10  # ~40%
    popular_target     = (target_total * 3 + 9) // 10  # ~30%
    supplemental_target = target_total - trending_target - popular_target  # ~30%

    catalog_candidates, trending_candidates, popular_candidates, supplemental_candidates = await asyncio.gather(
        fetch_catalog_candidates(
            client, _cfg.CACHE_WARM_CATALOG_URLS, _cfg.SERVER_TMDB_KEY,
            max_items_per_catalog=_cfg.CACHE_WARM_CATALOG_MAX_ITEMS,
        ),
        fetch_trending_candidates(client, _cfg.SERVER_TMDB_KEY, max_items=trending_target),
        fetch_popular_candidates(client, _cfg.SERVER_TMDB_KEY, max_items=popular_target),
        fetch_supplemental_candidates(client, _cfg.SERVER_TMDB_KEY, max_items=supplemental_target),
    )

    # Catalog candidates come first so a user-requested catalog is warmed
    # ahead of generic trending/popular/supplemental within the shared budgets.
    seen: set[tuple[str, str]] = set()
    candidates: list[dict] = []
    for item in catalog_candidates + trending_candidates + popular_candidates + supplemental_candidates:
        key = (item["media_type"], item["tmdb_id"])
        if key in seen:
            continue
        seen.add(key)
        candidates.append(item)

    logger.info(
        f"Cache warm: starting cycle — {len(candidates)} candidates "
        f"({len(catalog_candidates)} catalog, {len(trending_candidates)} trending, "
        f"{len(popular_candidates)} popular, {len(supplemental_candidates)} supplemental, "
        f"{len(catalog_candidates) + len(trending_candidates) + len(popular_candidates) + len(supplemental_candidates) - len(candidates)} overlap), "
        f"tmdb_budget={tmdb_budget}, mdblist_budget={mdblist_budget}"
    )

    if _mdblist_semaphore is None:
        _mdblist_semaphore = asyncio.Semaphore(_cfg.MDBLIST_CONCURRENCY)

    tmdb_calls      = 0
    mdblist_calls   = 0
    quality_calls   = 0
    detection_calls = 0
    titles_seen     = 0

    # Text-detection scans (~400ms each) are pipelined: queue a scan and keep
    # processing later candidates' metadata/image/rating work while it runs,
    # only blocking once _DETECTION_PIPELINE_DEPTH scans are in flight. The
    # existing TEXTLESS_DETECTION_CONCURRENCY semaphore still caps how many
    # actually run at once — this just stops the loop from idling while they do.
    _pending_detections: list[asyncio.Task] = []
    _detection_pipeline_depth = _cfg.TEXTLESS_DETECTION_CONCURRENCY + 1

    for candidate in candidates:
        if tmdb_calls >= tmdb_budget:
            break

        tmdb_id    = candidate["tmdb_id"]
        media_type = candidate["media_type"]
        endpoint   = "tv" if media_type in ("tv", "series") else "movie"

        metadata_cache_key = tmdb_metadata_cache_key(endpoint, tmdb_id, "en")
        cached_meta = get_cached_tmdb_metadata(metadata_cache_key)

        if cached_meta is None:
            try:
                genre_ids, is_textless, logos, release_year, _title, poster_path, backdrop_path, tmdb_data = (
                    await _coalesced_fetch_poster_metadata(client, tmdb_id, _cfg.SERVER_TMDB_KEY, media_type, "en")
                )
            except Exception as exc:
                logger.warning(f"Cache warm: TMDB metadata fetch failed for {media_type}/{tmdb_id}: {exc}")
                continue
            tmdb_calls += 1
            imdb_id           = tmdb_data.get("imdb_id")
            original_language = tmdb_data.get("original_language")
            original_title    = tmdb_data.get("original_title")
            vote_count        = tmdb_data.get("vote_count")
            title             = _title
        else:
            genre_ids         = cached_meta.get("genre_ids", [])
            imdb_id           = cached_meta.get("imdb_id")
            is_textless       = cached_meta.get("is_textless", False)
            logos             = cached_meta.get("logos", [])
            poster_path       = cached_meta.get("poster_path")
            backdrop_path     = cached_meta.get("backdrop_path")
            original_language = cached_meta.get("original_language")
            release_year      = cached_meta.get("release_year")
            original_title    = cached_meta.get("original_title")
            vote_count        = cached_meta.get("vote_count")
            title             = cached_meta.get("title")

        titles_seen += 1

        # Pre-fetch the poster/backdrop image and (when applicable) the logo
        # this title would render with by default — the slow, bandwidth-heavy
        # part of a real request. fetch_poster_image/fetch_backdrop_image/
        # fetch_logo each check their own disk cache first and skip the
        # download when already warm, so this is cheap in steady state.
        # Mirrors the default poster-endpoint art selection (textless poster,
        # else backdrop fallback, else text-bearing poster) but skips the
        # CPU-heavy text-detection rescue path and original-art mode, which
        # are per-request preferences rather than the common default.
        _use_backdrop = bool(backdrop_path) and (poster_path is None or not is_textless)
        try:
            if _use_backdrop:
                await fetch_backdrop_image(client, tmdb_id, backdrop_path, avoid_text=False)
                _logo_textless = True
            elif poster_path:
                await fetch_poster_image(client, tmdb_id, media_type, poster_path)
                _logo_textless = is_textless
            else:
                _logo_textless = False

            if _logo_textless and logos:
                await fetch_logo(
                    client, logos, "en",
                    imdb_id=imdb_id,
                    original_language=original_language,
                    logo_priority="native_original",
                )
        except Exception as exc:
            logger.warning(f"Cache warm: image/logo fetch failed for {media_type}/{tmdb_id}: {exc}")

        # Pre-run burned-in-text detection on the textless art selected above —
        # the same scan a real /poster request would trigger on first view, and
        # by far the slowest per-request step (~400ms cold). Mirrors the
        # /poster cache-key scheme exactly (source tag + crop version + conf +
        # detector signature) so a warmed result is a hit on the real request.
        # Unlike /poster, no vote-count gate: warming happens off the request
        # path, so every textless title gets resolved up front.
        if _cfg.TEXTLESS_TEXT_DETECTION and is_textless and (_use_backdrop or poster_path):
            try:
                from text_detect import DETECT_RES_SIG

                if _use_backdrop:
                    _det_src = f"bd:{backdrop_path}:{_CROP_VERSION}:plain"
                    _image_cache_key = f"backdrop_{tmdb_id}_{backdrop_path.strip('/')}_{_CROP_VERSION}"
                    _det_source = "backdrop"
                else:
                    _det_src = f"ps:{poster_path}"
                    _image_cache_key = f"{media_type}_{tmdb_id}_{poster_path.strip('/')}"
                    _det_source = "poster"

                _det_key = f"{_det_src}|conf={_cfg.PPOCR_BOX_THRESHOLD}:{DETECT_RES_SIG}"
                if get_cached_text_detection(_det_key) is None:
                    _det_image = await asyncio.get_running_loop().run_in_executor(
                        None, _load_detection_image, _image_cache_key
                    )
                    if _det_image is not None:
                        _text_titles = tuple(dict.fromkeys(
                            value for value in (title, original_title) if value
                        ))
                        _pending_detections.append(_start_text_detection(
                            _det_key,
                            _det_image,
                            title=_text_titles,
                            source=_det_source,
                            tmdb_id=tmdb_id,
                            vote_count=vote_count,
                            source_key=_det_src,
                            media_type=media_type,
                            image_path=poster_path,
                            foreground=False,
                        ))
                        detection_calls += 1
                        if len(_pending_detections) >= _detection_pipeline_depth:
                            _done, _pending = await asyncio.wait(
                                _pending_detections, return_when=asyncio.FIRST_COMPLETED
                            )
                            _pending_detections = list(_pending)
            except Exception as exc:
                logger.warning(f"Cache warm: text detection failed for {media_type}/{tmdb_id}: {exc}")

        # Optionally pre-fetch quality badge data (resolution/source/HDR) via
        # the configured quality source. Series default to S01E01 — the warm
        # cycle has no concept of "which episode", so this is a best-effort
        # warm of the most commonly requested entry point. Off by default;
        # see CACHE_WARM_QUALITY_ENABLED for why.
        if _cfg.CACHE_WARM_QUALITY_ENABLED and imdb_id:
            if quality_source_configured() and _quality_backoff_remaining() <= 0:
                if get_cached_quality(imdb_id, release_year) is None:
                    if _quality_bg_semaphore is None:
                        _quality_bg_semaphore = asyncio.Semaphore(_cfg.QUALITY_BG_CONCURRENCY)
                    try:
                        async with _quality_bg_semaphore:
                            q_result = await _with_retry(
                                fetch_quality,
                                client, imdb_id, media_type, 1, 1, release_year,
                            )
                        quality_calls += 1
                        _record_quality_result(q_result)
                        if q_result is QUALITY_PENDING:
                            # Still counts as a warm: the lookup registered the
                            # title with QualiCache, which now queues it.
                            logger.debug(f"Cache warm: quality pending for {imdb_id}")
                        elif q_result is FETCH_FAILED:
                            logger.warning(f"Cache warm: quality fetch failed for {imdb_id}")
                    except Exception as exc:
                        quality_calls += 1
                        _record_quality_result(FETCH_FAILED)
                        logger.warning(f"Cache warm: quality fetch failed for {imdb_id}: {exc}")

        if mdblist_calls >= mdblist_budget or not imdb_id:
            continue

        if get_cached_rating(imdb_id) is not None:
            continue  # rating already fresh — nothing to do

        await asyncio.sleep(0.25)

        async def _fetch_rating_warm(_key: str):
            async with _mdblist_semaphore:
                return await fetch_rating(
                    client, imdb_id, _key, genre_ids, media_type,
                )

        result = await _fetch_rating_warm(effective_mdblist_key)
        mdblist_calls += 1

        if isinstance(result, _RateLimited):
            backoff_secs, replacement = _mark_mdblist_rate_limit(imdb_id, effective_mdblist_key, result)
            logger.warning(
                f"Cache warm: MDBList rate-limited on {imdb_id}; "
                f"key cooling down for {backoff_secs:.0f}s"
            )
            if replacement:
                effective_mdblist_key = replacement
            else:
                logger.info("Cache warm: no healthy MDBList key remains — stopping MDBList warming for this cycle")
                mdblist_budget = mdblist_calls  # stop further MDBList attempts
            continue

        if result is FETCH_FAILED:
            logger.warning(f"Cache warm: MDBList fetch failed for {imdb_id} — stopping MDBList warming for this cycle")
            mdblist_budget = mdblist_calls  # stop further MDBList attempts
            continue

        ratings_dict, genre, rel, keywords, age_rating = result
        award_wins, award_noms = parse_mdblist_awards(keywords, tmdb_id=tmdb_id)
        kw_names = {(kw.get("name") or "").lower().strip() for kw in keywords}
        festival_label = next(
            (label for kw, label in FESTIVAL_KEYWORDS.items() if kw in kw_names),
            None,
        )
        is_cult       = bool({"cult-classic", "cult-film"} & kw_names)
        is_true_story = "based-on-true-story" in kw_names
        is_metacritic = "metacritic-must-see" in kw_names

        set_cached_rating(
            imdb_id,
            ratings_dict if isinstance(ratings_dict, dict) else {},
            genre or "Unknown",
            rel,
            award_wins,
            award_noms,
            awards_fetched=True,
            festival_label=festival_label,
            age_rating=age_rating,
            is_cult=is_cult,
            is_true_story=is_true_story,
            is_metacritic=is_metacritic,
        )

    if _pending_detections:
        await asyncio.gather(*_pending_detections, return_exceptions=True)

    logger.info(
        f"Cache warm: cycle complete — {titles_seen} titles processed, "
        f"{tmdb_calls} TMDB calls, {mdblist_calls} MDBList calls, "
        f"{quality_calls} quality calls, {detection_calls} text-detection scans"
    )


_CACHE_WARM_LAST_RUN_KEY = "cache_warm_last_run"
# Wait this long after startup before the very first-ever cycle, so it
# doesn't compete with other startup warm-up work (text detection model,
# genre backgrounds, etc.).
_CACHE_WARM_STARTUP_GRACE_SECS = 60
# When a restart finds the cycle already overdue, still wait this long before
# running — avoids hammering startup with warming work on a crash-loop.
_CACHE_WARM_MIN_WAIT_SECS = 60


def _format_local(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _seconds_until_next_hour(target_hour: float, now: float | None = None) -> float:
    """Seconds from `now` until the next occurrence of `target_hour` (0-24,
    local time, may be fractional e.g. 4.5 for 4:30am). Always returns a
    positive value — if `target_hour` is the current hour, rolls to tomorrow.
    """
    if now is None:
        now = time.time()
    local = time.localtime(now)
    midnight = now - (local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec)
    target = midnight + target_hour * 3600
    if target <= now:
        target += 86400
    return target - now

# Third-party credentials must never be persisted to the poster cache. They are
# stripped before storage; the background regeneration cycle re-supplies the
# server-side keys. access_key is intentionally kept — the /poster replay needs
# it to pass the instance access gate, and it is the instance's own key.
_UNCACHEABLE_PARAMS = {"tmdb_key", "mdblist_key"}


def _sanitize_request_params(query: str) -> str:
    """Drop user API keys from a stored query string, preserving order."""
    if not query:
        return query
    kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True)
            if k not in _UNCACHEABLE_PARAMS]
    return urlencode(kept)


async def _run_trending_fetch_cycle(client: httpx.AsyncClient) -> None:
    logger.info("Starting scheduled trending fetch cycle")
    if not _cfg.SERVER_TMDB_KEY:
        logger.info("Trending fetch: skipped - no server TMDB key configured")
        return

    try:
        trending = await fetch_trending_candidates(
            client, _cfg.SERVER_TMDB_KEY, max_items=max(_cfg.TRENDING_FETCH_COUNT, _cfg.TRENDING_BROAD_FETCH_COUNT)
        )
    except Exception as exc:
        logger.error(f"Trending fetch: failed to fetch candidates: {exc}")
        return

    # Build the set of trending (tmdb_id, type) pairs. TV titles are cached under
    # both "tv" and "series" (Stremio uses "series"), so include both variants.
    # Keeping the media type prevents a movie and a TV show that share a numeric
    # TMDB id from cross-triggering each other's regeneration.
    trending_pairs: set[tuple[str, str]] = set()
    for item in trending:
        tid = item.get("tmdb_id")
        if tid is None:
            continue
        tid = str(tid)
        mt = item.get("media_type")
        if mt in ("tv", "series"):
            trending_pairs.add((tid, "tv"))
            trending_pairs.add((tid, "series"))
        elif mt:
            trending_pairs.add((tid, mt))
    if not trending_pairs:
        return

    db = get_db()
    try:
        rows = db.execute("SELECT cache_key, request_params FROM final_poster_cache WHERE request_params IS NOT NULL").fetchall()
    except Exception as exc:
        logger.error(f"Trending fetch: failed to query cache: {exc}")
        return

    # Use a test client to regenerate posters through the API
    regenerated_count = 0
    
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as local_client:
        for cache_key, req_params_str in rows:
            parts = cache_key.split(":")
            if len(parts) < 4:
                continue
            if (parts[1], parts[2]) not in trending_pairs:
                continue
            if not req_params_str:
                continue
            logger.info(f"Trending fetch: regenerating poster for {cache_key}")
            try:
                # Delete first so the replay misses the cache and re-renders with
                # the fresh trending rank instead of serving the stale composite.
                delete_cached_final_poster(cache_key)
                resp = await local_client.get(f"/poster?{req_params_str}")
                if resp.status_code >= 400:
                    logger.warning(f"Trending fetch: regenerate for {cache_key} returned HTTP {resp.status_code}")
                else:
                    regenerated_count += 1
            except Exception as exc:
                logger.error(f"Trending fetch: failed to regenerate poster {cache_key}: {exc}")
                    
    logger.info(f"Trending fetch cycle completed. Regenerated {regenerated_count} posters.")


async def _trending_fetch_loop() -> None:
    """Periodically fetch trending items and regenerate cached posters."""
    # First run immediately on startup
    await asyncio.sleep(10)
    try:
        if _HTTP_CLIENT is not None:
            await _run_trending_fetch_cycle(_HTTP_CLIENT)
    except Exception as exc:
        logger.error(f"Trending fetch: startup cycle failed: {exc}")

    while True:
        if not _cfg.TRENDING_FETCH_TIME:
            wait = 86400.0
        else:
            try:
                tz = zoneinfo.ZoneInfo(_cfg.TRENDING_FETCH_TIMEZONE)
            except Exception as exc:
                logger.error(f"Trending fetch: invalid timezone {_cfg.TRENDING_FETCH_TIMEZONE}: {exc}, using UTC")
                tz = zoneinfo.ZoneInfo("UTC")

            try:
                h, m = map(int, _cfg.TRENDING_FETCH_TIME.split(':'))
            except ValueError:
                h, m = 0, 0
                
            now_dt = datetime.now(tz)
            target_dt = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)
            if target_dt <= now_dt:
                target_dt += timedelta(days=1)
            wait = (target_dt - now_dt).total_seconds()
            
        logger.info(f"Trending fetch: next cycle scheduled in {wait / 3600:.1f} hours")
        await asyncio.sleep(wait)
        try:
            if _HTTP_CLIENT is not None:
                await _run_trending_fetch_cycle(_HTTP_CLIENT)
        except Exception as exc:
            logger.error(f"Trending fetch: cycle failed: {exc}")


async def _cache_warm_loop(digital_release_ready: asyncio.Event | None = None) -> None:
    """
    Periodically warm the TMDB metadata/image and MDBList rating caches for
    trending + popular titles.

    The last completed cycle's timestamp is persisted (app_state table) so a
    container restart within CACHE_WARM_INTERVAL_HOURS of the last run
    doesn't immediately re-run the whole cycle — it instead waits out the
    remainder of the interval. The very first run ever uses a short startup
    grace period instead.

    If CACHE_WARM_AT_HOUR is set, steady-state cycles (after the first) are
    instead scheduled for the next occurrence of that local hour-of-day,
    rather than exactly CACHE_WARM_INTERVAL_HOURS after the previous run.

    On the very first cycle, also wait (briefly) for the digital-release
    (movieleaks) sync to finish first, so the two startup background jobs
    don't both hammer external APIs at the same time.
    """
    if not _cfg.CACHE_WARM_ENABLED:
        return

    interval_secs = max(1.0, _cfg.CACHE_WARM_INTERVAL_HOURS) * 3600

    last_run_raw = get_app_state(_CACHE_WARM_LAST_RUN_KEY)
    if last_run_raw is not None:
        try:
            last_run = float(last_run_raw)
        except ValueError:
            last_run = None
    else:
        last_run = None

    if last_run is None:
        wait = float(_CACHE_WARM_STARTUP_GRACE_SECS)
    elif _cfg.CACHE_WARM_AT_HOUR is not None:
        wait = max(_CACHE_WARM_MIN_WAIT_SECS, _seconds_until_next_hour(_cfg.CACHE_WARM_AT_HOUR))
    else:
        wait = max(_CACHE_WARM_MIN_WAIT_SECS, (last_run + interval_secs) - time.time())

    first_cycle = True
    while True:
        logger.info(
            f"Cache warm: next cycle scheduled for {_format_local(time.time() + wait)} "
            f"(in {wait / 60:.1f} min)"
        )
        await asyncio.sleep(wait)
        if first_cycle and digital_release_ready is not None and not digital_release_ready.is_set():
            try:
                await asyncio.wait_for(digital_release_ready.wait(), timeout=120)
            except asyncio.TimeoutError:
                logger.warning(
                    "Cache warm: digital release sync didn't finish within 120s — proceeding anyway"
                )
        first_cycle = False
        try:
            if _HTTP_CLIENT is not None:
                await _run_cache_warm_cycle(_HTTP_CLIENT)
                set_app_state(_CACHE_WARM_LAST_RUN_KEY, str(time.time()))
            else:
                logger.warning("Cache warm: HTTP client not ready — skipping this cycle")
        except Exception as exc:
            logger.error(f"Cache warm: cycle failed: {exc}")
        if _cfg.CACHE_WARM_AT_HOUR is not None:
            wait = max(_CACHE_WARM_MIN_WAIT_SECS, _seconds_until_next_hour(_cfg.CACHE_WARM_AT_HOUR))
        else:
            wait = interval_secs


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _HTTP_CLIENT, _configurator_html, _render_assets_signature
    global _background_detection_queue, _background_detection_task
    global _render_profile
    init_db()
    logger.info(f"Cache initialised (composite TTL {_cfg.COMPOSITE_CACHE_TTL}s / "
                f"{_cfg.COMPOSITE_CACHE_TTL / 86400:.1f}d)")
    _HTTP_CLIENT = _make_http_client()
    logger.info("HTTP client initialised")
    # Warn on quality source misconfiguration
    _quality_source = active_quality_source()
    if _cfg.QUALITY_SOURCE not in QUALITY_SOURCES:
        logger.warning(
            f"Unknown QUALITY_SOURCE={_cfg.QUALITY_SOURCE!r} — expected one of "
            f"{', '.join(QUALITY_SOURCES)}; defaulting to aiostreams behaviour."
        )
    elif _quality_source != "aiostreams" and (bool(_cfg.AIOSTREAMS_URL) or bool(_cfg.AIOSTREAMS_AUTH)):
        logger.warning(
            f"QUALITY_SOURCE={_quality_source} but AIOSTREAMS_URL/AIOSTREAMS_AUTH are also set — "
            f"{_quality_source} will be used; AIOSTREAMS settings are ignored. "
            "Unset AIOSTREAMS_URL and AIOSTREAMS_AUTH to silence this warning."
        )
    if _quality_source == "scraper" and not _cfg.SCRAPER_URL:
        logger.warning("QUALITY_SOURCE=scraper but SCRAPER_URL is not set — quality fetching is disabled.")
    if _quality_source == "qualicache" and not _cfg.QUALICACHE_URL:
        logger.warning("QUALITY_SOURCE=qualicache but QUALICACHE_URL is not set — quality fetching is disabled.")
    if _quality_source == "qualicache" and _cfg.QUALICACHE_URL:
        logger.info(f"Quality source: QualiCache at {_cfg.QUALICACHE_URL}")
    _configurator_html = _load_configurator_html()
    _render_profile = _load_configured_render_profile()
    if _render_profile is not None:
        logger.info(
            "Render profile loaded: %s (%s)",
            _render_profile.name,
            _render_profile.digest,
        )
    load_languages()   # poster-output translations (English fallback if absent)
    _render_assets_signature = _compute_render_assets_signature()
    # Warm the genre fallback backgrounds into memory so no-art posters render
    # with zero extra latency (same idea as the badge cache warm-up).
    try:
        _warmed = 0
        for _style in _GENRE_BG_STYLES:
            _sdir = os.path.join(_GENRE_BG_DIR, _style)
            if not os.path.isdir(_sdir):
                continue
            for _fn in os.listdir(_sdir):
                if _fn.lower().endswith(".png"):
                    if _load_genre_background(_fn[:-4], _style) is not None:
                        _warmed += 1
        if _warmed:
            logger.info(f"Genre backgrounds warmed: {_warmed} entries")
        else:
            logger.info("No genre background art found — using gradient fallbacks")
    except Exception as exc:
        logger.warning(f"Genre background warm-up skipped: {exc}")
    # Burned-in-text detection: fetch + load PP-OCRv5 Mobile in the background so
    # the first textless request isn't blocked by the one-time ~4.6 MB
    # download.  On by default; skipped when the operator has opted out.
    if _cfg.TEXTLESS_TEXT_DETECTION:
        _background_detection_queue = asyncio.Queue()
        _background_detection_task = asyncio.create_task(
            _background_text_detection_worker()
        )

        async def _warm_text_detector():
            try:
                from text_detect import text_detection_status, warm_model
                ok = await asyncio.get_running_loop().run_in_executor(_get_detect_executor(), warm_model)
                log = logger.info if ok else logger.warning
                log(f"Burned-in-text detection: {text_detection_status()}")
            except Exception as exc:
                logger.warning(f"PP-OCR warm-up failed: {exc}")
        asyncio.create_task(_warm_text_detector())

    try:
        from tvdb import tvdb_status
        logger.info(f"TVDB fallback art source: {tvdb_status()}")
    except Exception as exc:
        logger.warning(f"TVDB status check failed: {exc}")

    _digital_release_ready = asyncio.Event()
    prune_task   = asyncio.create_task(_cache_prune_loop())
    digital_task = asyncio.create_task(digital_release_poll_loop(_HTTP_CLIENT, _digital_release_ready))
    cache_warm_task = asyncio.create_task(_cache_warm_loop(_digital_release_ready))
    trending_task = asyncio.create_task(_trending_fetch_loop())
    yield
    prune_task.cancel()
    digital_task.cancel()
    cache_warm_task.cancel()
    trending_task.cancel()
    if _background_detection_task is not None:
        _background_detection_task.cancel()
    # Await the cancelled tasks so their finally: blocks finish unwinding
    # before we close the HTTP client they may still be using.
    with suppress(asyncio.CancelledError):
        await prune_task
    with suppress(asyncio.CancelledError):
        await digital_task
    with suppress(asyncio.CancelledError):
        await cache_warm_task
    with suppress(asyncio.CancelledError):
        await trending_task
    if _background_detection_task is not None:
        with suppress(asyncio.CancelledError):
            await _background_detection_task
        _background_detection_task = None
    _background_detection_queue = None
    _background_detection_keys.clear()
    _shutdown_detect_executor()
    shutdown_candidate_ocr_executor()
    await _HTTP_CLIENT.aclose()
    logger.info("HTTP client closed")


app = FastAPI(lifespan=lifespan)


@app.exception_handler(_SelectionError)
async def _selection_error_response(_request: Request, exc: _SelectionError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
            }
        },
    )


@app.exception_handler(CandidateDiscoveryError)
async def _candidate_error_response(_request: Request, exc: CandidateDiscoveryError):
    logger.warning("Artwork discovery rejected: reason=%s", exc.code)
    response = JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
            }
        },
    )
    response.headers["Cache-Control"] = "no-store"
    if exc.status_code == 429:
        response.headers["Retry-After"] = "60"
    return response


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_FONTS_DIR = os.path.join(BASE_DIR, "fonts")


# ── Genre fallback backgrounds ────────────────────────────────────────────
# Atmospheric 500x750 PNGs (procedurally generated by genre_backgrounds.py, or
# hand-made overrides dropped into the same folder) used as the base for no-art
# fallback posters instead of the flat gradient.  Cached in memory; a *copy* is
# returned per request because build_poster draws onto the base.
_GENRE_BG_DIR = os.path.join(BASE_DIR, "static", "genre_bg")
# Two interchangeable fallback-background sets, chosen per request via
# fallback_bg_style: "minimal" (procedural textured) or "photoreal" (hand-made
# photographic art that blends with real posters).
_GENRE_BG_STYLES = ("minimal", "photoreal")
_genre_bg_cache: dict[str, "Image.Image | None"] = {}   # keyed "style/genre"


def _genre_bg_path(style: str, name: str) -> "str | None":
    """Filesystem path to a genre-background PNG, or None if it doesn't exist."""
    p = os.path.join(_GENRE_BG_DIR, style, f"{name}.png")
    return p if os.path.exists(p) else None


def _load_genre_background(genre: str, style: str = "minimal") -> "Image.Image | None":
    """Return a fresh RGBA copy of the genre fallback background for *style*, or
    None if none exists.  A missing image degrades gracefully: the style's
    default.png → the minimal set's genre/default → None (caller then renders the
    procedural gradient canvas).  So selecting a not-yet-populated style never
    breaks — it just falls back to minimal."""
    if style not in _GENRE_BG_STYLES:
        style = "minimal"
    key = f"{style}/{genre}"
    if key not in _genre_bg_cache:
        path = (
            _genre_bg_path(style, genre)
            or _genre_bg_path(style, "default")
            or (_genre_bg_path("minimal", genre) if style != "minimal" else None)
            or _genre_bg_path("minimal", "default")
        )
        try:
            _genre_bg_cache[key] = Image.open(path).convert("RGBA") if path else None
        except Exception:
            _genre_bg_cache[key] = None
    base = _genre_bg_cache[key]
    return base.copy() if base is not None else None


app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


def _request_targets_discovery_endpoint(request: Request) -> bool:
    for route in app.router.routes:
        match, _child_scope = route.matches(request.scope)
        if match == Match.FULL and getattr(route, "name", None) == "artwork_candidates":
            return True
    return False


@app.middleware("http")
async def authenticate_and_harden(request: Request, call_next):
    discovery_endpoint = _request_targets_discovery_endpoint(request)
    anonymous_path = (
        request.url.path == "/health"
        or request.url.path == "/static"
        or request.url.path.startswith("/static/")
    )
    if not discovery_endpoint and not anonymous_path and _cfg.ACCESS_KEY:
        header_key = request.headers.get("x-jellyfin-artwork-key", "")
        query_key = request.query_params.get("access_key", "")
        header_valid = _access_key_matches(header_key)
        query_valid = (
            not _cfg.HEADER_ONLY_AUTH
            and _access_key_matches(query_key)
        )
        if not header_valid and not query_valid:
            response = JSONResponse(status_code=403, content={"detail": "Unauthorized"})
            response.headers["server"] = "unknown"
            return response
    response = await call_next(request)
    if discovery_endpoint:
        response.headers["Cache-Control"] = "no-store"
    response.headers["server"] = "unknown"
    return response


# ---------------------------------------------------------------------------
# Server capability endpoint
# ---------------------------------------------------------------------------

@app.get("/server-caps")
async def server_caps(access_key: str = ""):
    next_refresh_hours = None
    if _cfg.TRENDING_FETCH_TIME:
        import zoneinfo
        from datetime import datetime, timedelta
        try:
            tz = zoneinfo.ZoneInfo(_cfg.TRENDING_FETCH_TIMEZONE)
        except Exception:
            tz = zoneinfo.ZoneInfo("UTC")
        try:
            h, m = map(int, _cfg.TRENDING_FETCH_TIME.split(':'))
            now_dt = datetime.now(tz)
            target_dt = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)
            if target_dt <= now_dt:
                target_dt += timedelta(days=1)
            next_refresh_hours = round((target_dt - now_dt).total_seconds() / 3600, 1)
        except Exception:
            pass

    return {
        "tmdb_key_set":          bool(_cfg.SERVER_TMDB_KEY),
        "mdblist_key_set":       bool(_cfg.SERVER_MDBLIST_KEYS),
        "mdblist_key_count":     len(_cfg.SERVER_MDBLIST_KEYS),
        "aiostreams_configured": bool(_cfg.AIOSTREAMS_URL and _cfg.AIOSTREAMS_AUTH),
        "quality_source":        active_quality_source(),
        "quality_configured":    quality_source_configured(),
        "trending_fetch_count":  _cfg.TRENDING_FETCH_COUNT,
        "trending_fetch_time":   _cfg.TRENDING_FETCH_TIME,
        "trending_fetch_timezone": _cfg.TRENDING_FETCH_TIMEZONE,
        "trending_next_refresh_hours": next_refresh_hours,
    }


# ---------------------------------------------------------------------------
# Configurator HTML
# ---------------------------------------------------------------------------

_configurator_html: str | None = None
# Strong ETag for the configurator HTML — short hash of its bytes so the
# browser can revalidate cheaply.  Without this, browsers heuristically
# cache the page and keep serving stale HTML after a container rebuild,
# which is what made sliders / dropdowns drift out of sync with the new
# defaults until a manual Reset.
_configurator_etag: str | None = None
_RENDER_CACHE_VERSION = "2"
_render_assets_signature = "startup"


def _compute_render_assets_signature() -> str:
    digest = hashlib.sha256()
    roots = (
        os.path.join(BASE_DIR, "languages"),
        os.path.join(BASE_DIR, "static", "genre_bg"),
    )
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _, filenames in os.walk(root):
            for filename in sorted(filenames):
                path = os.path.join(dirpath, filename)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                digest.update(os.path.relpath(path, BASE_DIR).encode())
                digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
    override_path = os.environ.get(
        "DISCOVERY_OVERRIDES_PATH", "/app/cache/discovery_overrides.json"
    )
    try:
        with open(override_path, "rb") as override_file:
            digest.update(override_file.read())
    except OSError:
        pass
    return digest.hexdigest()[:16]


def _server_render_signature() -> str:
    return "|".join((
        f"render={_RENDER_CACHE_VERSION}",
        f"format={_cfg.IMAGE_FORMAT}",
        # Include both quality knobs so a change to either busts the render
        # cache regardless of which format is currently active.
        f"jpeg={_cfg.JPEG_QUALITY}",
        f"webp={_cfg.WEBP_QUALITY}",
        f"contrast={int(_cfg.LOGO_CONTRAST_RESCUE)}",
        f"stretch={int(_cfg.LOGO_STRETCH_DISABLED)}:{_cfg.LOGO_STRETCH_FACTOR:g}",
        f"assets={_render_assets_signature}",
    ))


def _load_configurator_html() -> str:
    global _configurator_etag
    html_path = os.path.join(os.path.dirname(__file__), "configurator.html")
    try:
        with open(html_path, "r", encoding="utf-8") as f:
            content = f.read()

        content = content.replace("{{TRENDING_FETCH_COUNT}}", str(_cfg.TRENDING_FETCH_COUNT))
        content = content.replace("{{TRENDING_FETCH_COUNT_PLUS_ONE}}", str(_cfg.TRENDING_FETCH_COUNT + 1))
        content = content.replace("{{TRENDING_BROAD_FETCH_COUNT}}", str(_cfg.TRENDING_BROAD_FETCH_COUNT))

        _configurator_etag = '"' + hashlib.md5(content.encode("utf-8")).hexdigest()[:16] + '"'
        return content
    except FileNotFoundError:
        _configurator_etag = '"missing"'
        return "<h1>Configurator not found</h1><p>Place configurator.html alongside main.py</p>"


@app.get("/health")
async def health_check():
    """Lightweight liveness probe — no auth required, used by Docker healthcheck."""
    return {"status": "ok"}


@app.get("/ready")
async def readiness():
    if _cfg.RENDER_PROFILE_PATH and _render_profile is None:
        raise HTTPException(status_code=503, detail="Render profile is not loaded")
    if _cfg.TEXTLESS_TEXT_DETECTION and not _text_detector_ready():
        raise HTTPException(status_code=503, detail="Text detector is not ready")
    return {
        "status": "ready",
        "renderer_revision": _cfg.SOURCE_REVISION,
        "profile": (
            {
                "name": _render_profile.name,
                "digest": _render_profile.digest,
            }
            if _render_profile is not None
            else None
        ),
    }


def _text_detector_ready() -> bool:
    from text_detect import text_detection_ready
    return text_detection_ready()


@app.post("/v1/artwork/candidates")
async def artwork_candidates(request: Request):
    discovery_key = request.headers.get(
        "x-jellyfin-artwork-discovery-key", ""
    )
    if not discovery_key_matches(discovery_key):
        raise CandidateDiscoveryError(
            403,
            "unauthorized",
            "Unauthorized",
        )
    if request.query_params:
        raise CandidateDiscoveryError(
            400,
            "invalid_query",
            "Artwork discovery does not accept query parameters",
        )
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise CandidateDiscoveryError(
            415,
            "invalid_content_type",
            "Content-Type must be application/json",
        )
    try:
        body = await _read_bounded_body(
            request,
            max_bytes=CANDIDATE_MAX_BODY_BYTES,
            empty_message="Artwork discovery body is empty",
            too_large_message="Artwork discovery body is too large",
        )
    except HTTPException as exc:
        raise CandidateDiscoveryError(
            exc.status_code,
            "body_too_large" if exc.status_code == 413 else "invalid_body",
            str(exc.detail),
        ) from exc
    candidate_request = decode_candidate_request_body(body)
    if _HTTP_CLIENT is None:
        raise CandidateDiscoveryError(
            503,
            "service_unavailable",
            "Artwork discovery is not ready",
        )
    response, outcome = await discover_candidates(
        _HTTP_CLIENT,
        candidate_request,
    )
    logger.info(
        "Artwork discovery complete: type=%s tmdb=%s fanart=%s count=%d outcome=%s",
        candidate_request.media_type,
        response["sources"].get("tmdb", "unknown"),
        response["sources"].get("fanart", "unknown"),
        len(response["candidates"]),
        outcome,
    )
    result = JSONResponse(content=response)
    result.headers["Cache-Control"] = "no-store"
    return result


@app.get("/stats")
async def stats(access_key: str = ""):
    """
    Operator diagnostics: cache row counts / sizes plus live runtime state
    (in-flight renders, background quality fetches, MDBList key cooldowns).
    Gated behind the access key when one is configured.
    """
    now = asyncio.get_running_loop().time()
    keys = _cfg.SERVER_MDBLIST_KEYS
    mdblist_keys = []
    for i, k in enumerate(keys):
        cd = _mdblist_key_cooldown.get(k, 0.0)
        mdblist_keys.append({
            "index":         i + 1,
            "active":        i == (_mdblist_active_key_idx % len(keys)),
            "cooling_down":  now < cd,
            "cooldown_secs": max(0, round(cd - now)),
        })

    return {
        "cache":   get_cache_stats(),
        "runtime": {
            "renders_in_flight":        len(_render_inflight),
            "quality_fetches_in_flight": len(_quality_bg_inflight),
            "quality_source_backoff_secs": round(_quality_backoff_remaining(now)),
            "rating_fetches_in_flight":  len(_rating_fetch_inflight),
            "rating_backoff_titles":     len({imdb_id for imdb_id, _ in _rating_backoff}),
            "rating_backoff_entries":    len(_rating_backoff),
            "mdblist_keys":              mdblist_keys,
            "composite_cache_disabled":  _cfg.DISABLE_COMPOSITE_CACHE,
            "svg_logo_support":          svg_logo_supported(),
        },
    }


# TMDB genre name → id, used only by the debug canvas preview below.
_DEBUG_GENRE_IDS = {
    "Action": 28, "Adventure": 12, "Animation": 16, "Comedy": 35, "Crime": 80,
    "Documentary": 99, "Drama": 18, "Family": 10751, "Fantasy": 14, "History": 36,
    "Horror": 27, "Music": 10402, "Mystery": 9648, "Romance": 10749,
    "Sci-Fi": 878, "Thriller": 53, "War": 10752, "Western": 37,
}
_DEBUG_CANVAS_TTL = 300.0
_DEBUG_CANVAS_MAX_ENTRIES = 128
_debug_canvas_cache: dict[tuple[str, str, str, str, str], tuple[float, bytes]] = {}


@app.get("/debug/canvas")
async def debug_canvas(genre: str = "Action", title: str = "Sample Title",
                       style: str = "minimal", year: str = "2024",
                       score: str = "84", access_key: str = ""):
    """
    Render a no-art fallback card exactly as a poster-less title would: the genre
    fallback background (minimal or photoreal set) with the genre-aware title and
    the usual rating label composited on top.  Lets you eyeball any genre/style
    without hunting for a title that happens to lack poster art.
    """
    if len(title) > 200:
        raise HTTPException(status_code=400, detail="Title too long")
    cache_key = (genre, title, style, year, score)
    now = asyncio.get_running_loop().time()
    cached = _debug_canvas_cache.get(cache_key)
    if cached is not None and now - cached[0] <= _DEBUG_CANVAS_TTL:
        return Response(
            content=cached[1], media_type=f"image/{_cfg.IMAGE_FORMAT}",
            headers={"Cache-Control": "private, max-age=300"},
        )
    gid = _DEBUG_GENRE_IDS.get(genre)
    canvas = _load_genre_background(genre, style)
    if canvas is None:
        canvas = _make_fallback_canvas([gid] if gid else None).convert("RGBA")
    cfg = RequestConfig()
    _score = int(score) if score.isdigit() else "—"
    img = build_poster(canvas, _score, genre, cfg, fallback_title=title,
                       release_year=(year or None), no_poster=True)
    buf = io.BytesIO()
    _quality = _cfg.WEBP_QUALITY if _cfg.IMAGE_FORMAT == "webp" else _cfg.JPEG_QUALITY
    img.convert("RGB").save(buf, format=_cfg.IMAGE_FORMAT.upper(), quality=_quality)
    data = buf.getvalue()
    if len(_debug_canvas_cache) >= _DEBUG_CANVAS_MAX_ENTRIES:
        oldest = min(_debug_canvas_cache, key=lambda key: _debug_canvas_cache[key][0])
        _debug_canvas_cache.pop(oldest, None)
    return Response(
        content=data, media_type=f"image/{_cfg.IMAGE_FORMAT}",
        headers={"Cache-Control": "private, max-age=300"},
    )


@app.get("/debug/fallback-gallery", response_class=HTMLResponse)
async def fallback_gallery(style: str = "minimal", access_key: str = ""):
    """
    Self-contained gallery of every genre's no-art fallback card (live
    /debug/canvas renders), so an operator can review the fallback backgrounds +
    genre fonts at a glance and compare the minimal vs photoreal sets.  Gated
    behind the access key when configured.
    """
    if style not in _GENRE_BG_STYLES:
        style = "minimal"
    _ak = f"&access_key={access_key}" if access_key else ""

    # Every genre that has a background (covers the full genre map + any future
    # additions), derived from the minimal set so the gallery is never stale.
    try:
        _genres = sorted(
            f[:-4] for f in os.listdir(os.path.join(_GENRE_BG_DIR, "minimal"))
            if f.lower().endswith(".png") and f[:-4].lower() != "default"
        )
    except OSError:
        _genres = sorted(_DEBUG_GENRE_IDS)

    tiles = "".join(
        f'<figure><img loading="lazy" src="/debug/canvas?genre={g}'
        f'&title={g.replace(" ", "+")}&style={style}{_ak}" alt="{g}">'
        f'<figcaption>{g}</figcaption></figure>'
        for g in _genres
    )
    _tabs = "".join(
        f'<a class="{"on" if s == style else ""}" '
        f'href="/debug/fallback-gallery?style={s}{_ak}">{s.capitalize()}</a>'
        for s in _GENRE_BG_STYLES
    )
    html = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fallback art preview</title>
<style>
  body {{ margin:0; background:#0e0e10; color:#e8e8ea;
         font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }}
  header {{ padding:18px 20px; border-bottom:1px solid #2a2a2e;
           display:flex; align-items:center; gap:16px; flex-wrap:wrap; }}
  h1 {{ font-size:18px; margin:0; }} p {{ color:#9a9aa0; margin:0; font-size:13px; }}
  .tabs a {{ display:inline-block; padding:5px 12px; margin-right:6px; border-radius:8px;
            font-size:13px; text-decoration:none; color:#c7c7cc; background:#1c1c20;
            border:1px solid #2a2a2e; }}
  .tabs a.on {{ background:#3a3a44; color:#fff; border-color:#4a4a56; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(170px,1fr));
           gap:16px; padding:20px; }}
  figure {{ margin:0; }}
  img {{ width:100%; border-radius:10px; display:block; background:#1a1a1d; }}
  figcaption {{ text-align:center; padding-top:8px; font-size:13px; color:#c7c7cc; }}
</style></head><body>
<header>
  <h1>Fallback art preview</h1>
  <div class="tabs">{_tabs}</div>
  <p>Live no-art render for every genre — {len(_genres)} genres, "{style}" set.</p>
</header>
<div class="grid">{tiles}</div></body></html>"""
    return HTMLResponse(content=html)


@app.get("/", response_class=HTMLResponse)
async def get_configurator(request: Request, access_key: str = "", reload: str = ""):
    # ?reload=1 re-reads configurator.html from disk — useful while iterating on
    # the UI without restarting the container.  Gated on the access key so it's
    # not a public DoS vector via disk re-reads.
    global _configurator_html
    if reload:
        _configurator_html = _load_configurator_html()
        logger.info("Configurator HTML reloaded from disk")

    if _configurator_html is None:
        _load_configurator_html()  # populates the global

    # 304 short-circuit when the browser's cached copy still matches —
    # saves the 130 KB body re-download on every navigation while still
    # forcing a fresh fetch as soon as the file's contents change.
    _cache_headers = {
        "Cache-Control": "no-cache, must-revalidate",
        "ETag":          _configurator_etag or '""',
    }
    if (
        _configurator_etag
        and request.headers.get("if-none-match") == _configurator_etag
    ):
        return Response(status_code=304, headers=_cache_headers)

    return HTMLResponse(
        content=_configurator_html or _load_configurator_html(),
        headers=_cache_headers,
    )


# ---------------------------------------------------------------------------
# Search endpoint
# ---------------------------------------------------------------------------

@app.get("/search")
async def search_proxy(
    q: str,
    tmdb_key: str = "",
    access_key: str = "",
):
    if len(q) > 200:
        raise HTTPException(status_code=400, detail="Query too long")

    effective_key = _resolve_tmdb_key(tmdb_key)
    if not effective_key:
        raise HTTPException(status_code=400, detail="No TMDB API key available")

    if _HTTP_CLIENT is None:
        raise HTTPException(status_code=503, detail="Service unavailable")
    resp = await _HTTP_CLIENT.get(
        "https://api.themoviedb.org/3/search/multi",
        params={
            "api_key": effective_key,
            "query": q,
            "include_adult": "false",
            "page": "1",
        },
    )
    return Response(content=resp.content, media_type="application/json", status_code=resp.status_code)


@app.get("/resolve-imdb")
async def resolve_imdb(
    tmdb_id: str,
    type: str = "movie",
    tmdb_key: str = "",
    access_key: str = "",
):
    _check_tmdb_id(tmdb_id)
    _check_type(type)

    effective_key = _resolve_tmdb_key(tmdb_key)
    if not effective_key:
        raise HTTPException(status_code=400, detail="No TMDB API key available")

    endpoint = (
        f"https://api.themoviedb.org/3/tv/{tmdb_id}/external_ids"
        if type == "tv"
        else f"https://api.themoviedb.org/3/movie/{tmdb_id}/external_ids"
    )

    if _HTTP_CLIENT is None:
        raise HTTPException(status_code=503, detail="Service unavailable")
    resp = await _HTTP_CLIENT.get(endpoint, params={"api_key": effective_key})
    return Response(content=resp.content, media_type="application/json", status_code=resp.status_code)


# ---------------------------------------------------------------------------
# Logo endpoint
# ---------------------------------------------------------------------------

@app.get("/logo")
async def get_logo(
    tmdb_id: str,
    type: str = "movie",
    lang: str = "en",
    imdb_id: str | None = None,
    access_key: str = "",
    tmdb_key: str = "",
):
    """
    Return the best available logo PNG for a title.

    Checks the local file cache first (same cache the poster endpoint uses),
    then falls through to TMDB and Metahub as needed.  No rendering is applied —
    callers receive the original PNG exactly as stored.
    """
    if _HTTP_CLIENT is None:
        raise HTTPException(status_code=503, detail="Service unavailable")

    effective_tmdb_key = _resolve_tmdb_key((tmdb_key or "").strip())
    if not effective_tmdb_key:
        raise HTTPException(status_code=503, detail="No TMDB API key configured")
    media_type = "tv" if type in ("tv", "series") else "movie"
    effective_lang = (lang or "en").strip() or "en"

    client = _HTTP_CLIENT

    _, _, logos, _, _, _, _, tmdb_data = await _coalesced_fetch_poster_metadata(
        client, tmdb_id, effective_tmdb_key, media_type, effective_lang
    )

    # Use imdb_id from metadata if not supplied — needed for Metahub fallback
    effective_imdb_id = (imdb_id or "").strip() or tmdb_data.get("imdb_id") or None
    original_language = tmdb_data.get("original_language")

    logo_image = await fetch_logo(
        client, logos, effective_lang,
        imdb_id=effective_imdb_id,
        original_language=original_language,
    )

    if logo_image is None:
        raise HTTPException(status_code=404, detail="No logo available")

    buf = io.BytesIO()
    logo_image.save(buf, format="PNG")
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=2592000"},
    )


# ---------------------------------------------------------------------------
# Poster endpoint
# ---------------------------------------------------------------------------

async def _render_poster(
    request: Request,
    tmdb_id: str = "",
    imdb_id: str = "",
    anilist_id: str = "",
    kitsu_id: str = "",
    stremio_id: str = "",
    type: str = "movie",
    quality: str = "",
    season: int = 1,
    episode: int = 1,
    mdblist_key: str = "",
    tmdb_key: str = "",
    debug: str | None = None,
    nocache: str | None = None,
    selected_image: _SelectedImage | None = None,
    selected_logo: _SelectedImage | None = None,
    profile: RenderProfile | None = None,
    output_format: str | None = None,
):
    _check_type(type)
    output_format = _resolve_output_format(output_format)

    # -----------------------------------------------------------------------
    # Anime-native ids (AniList / Kitsu).
    #
    # A client that can supply one — AIOMetadata and similar advanced metadata
    # providers — gets art, titles, genres and a community score straight from
    # the anime provider, with no id conversion anywhere. Clients that only
    # speak imdb/tmdb/tvdb pass neither param and take the unchanged TMDB path.
    #
    # The anime id governs the ART and the metadata spine only. AIOMetadata
    # sends tmdb_id and imdb_id alongside it whenever it has them, and those are
    # worth keeping: MDBList ratings, awards, keywords, age rating and digital
    # release are all IMDb-keyed, and trending and movie release status are
    # TMDB-keyed. Discarding them leaves the info sash with nothing to say
    # beyond the foreign-language label. So enrichment still runs on whatever
    # ids the client supplied; only the art comes from the anime provider.
    #
    # `canonical_id` is the identity for the rating/quality cache tables and the
    # scraper stream id: the IMDb id when there is one (so the row is shared
    # with the ordinary path), else the "<namespace>:<id>" anime form, which
    # can't collide with a bare TMDB id or a tt-prefixed IMDb id and is already
    # what Torrentio/Comet/AIOStreams expect for anime stream lookups.
    # -----------------------------------------------------------------------
    anime_namespace, anime_id = _resolve_anime_request(anilist_id, kitsu_id, stremio_id)
    is_anime = anime_namespace is not None
    anime_key = anime.namespaced_id(anime_namespace, anime_id) if is_anime else ""

    if is_anime:
        # Both are optional on this path, but must still be well-formed if sent.
        if imdb_id:
            _check_imdb_id(imdb_id)
        if tmdb_id:
            _check_tmdb_id(tmdb_id)
        has_tmdb_id = bool(tmdb_id)
        # Downstream art fetching, log lines and detection keys are written in
        # terms of tmdb_id. Keep the real one when supplied (better cache keys,
        # and it re-enables the TMDB-only lookups); otherwise stand in the
        # anime id so those paths keep working without a second id threaded
        # through them.
        if not tmdb_id:
            tmdb_id = anime_key
        canonical_id = imdb_id or anime_key
    else:
        _check_tmdb_id(tmdb_id)
        _check_imdb_id(imdb_id)
        has_tmdb_id = True
        canonical_id = imdb_id

    # -----------------------------------------------------------------------
    # Single-user mode: check for a cached final poster first.
    # The cache key includes imdb_id and type; quality is intentionally
    # excluded because in single-user mode the quality tokens come from
    # AIOStreams (not from query params) and are themselves cached per-title.
    # If the caller passes an explicit quality= override this bypass is
    # skipped so they always get the exact poster they asked for.
    # -----------------------------------------------------------------------
    effective_tmdb_key    = _resolve_tmdb_key(tmdb_key)
    effective_mdblist_key = _resolve_mdblist_key(mdblist_key)

    # MDBList is keyed by IMDb id. Only null the key when the request genuinely
    # has no IMDb id — that makes every downstream MDBList gate (cooldown,
    # back-off, coalescing, the fetch itself) skip naturally rather than needing
    # a branch at each one. When AIOMetadata does send an IMDb id alongside the
    # anime id, the normal fetch runs and the provider score is merged into its
    # result, so the sash gets awards, keywords and an age rating too.
    if not imdb_id:
        effective_mdblist_key = None

    # An anime request gets its art and metadata from the provider, so a TMDB
    # key is optional there even when a tmdb_id is supplied — it only unlocks
    # the extra TMDB-keyed lookups (trending, movie release status).
    if not effective_tmdb_key and not is_anime:
        raise HTTPException(
            status_code=400,
            detail=(
                "No TMDB API key available. Either provide tmdb_key= as a query parameter "
                "or configure the TMDB_API_KEY environment variable on the server."
            ),
        )

    raw_params = (
        dict(profile.defaults)
        if profile is not None
        else {
            k: v for k, v in request.query_params.items()
            if k not in (
                "tmdb_id", "imdb_id", "anilist_id", "kitsu_id", "stremio_id",
                # These template placeholders are not art sources here, but
                # their presence must not fragment otherwise identical caches.
                "mal_id", "anidb_id",
                "mdblist_key", "tmdb_key", "type",
                "quality", "season", "episode", "access_key", "debug", "nocache",
            )
        }
    )
    rcfg = build_request_config(raw_params)
    if selected_logo is not None and rcfg.textless:
        raise _SelectionError(
            409,
            "selected_logo_disabled_by_profile",
            "The active render profile disables Logo composition",
        )

    # Anime is essentially always Japanese, so the foreign-language slot says
    # nothing here — but ranked highly (a reasonable choice for live-action,
    # where it is a real signal) it would mask every sash below it on every
    # anime title. Demote it to last rather than dropping it, so a title with
    # nothing else to say still gets a label; a user who removed the slot
    # entirely keeps it removed.
    #
    # Applied to rcfg itself rather than at the pick_sash call sites because
    # build_poster derives its own ordering from cfg.sash_priority, and that is
    # the one that ends up on the rendered poster.
    if is_anime and "foreign" in rcfg.sash_priority:
        rcfg.sash_priority = (
            [s for s in rcfg.sash_priority if s != "foreign"] + ["foreign"]
        )

    # Operator force-refresh: ?nocache=1 skips the composite cache READ so a fresh
    # render is produced (and re-cached), letting an operator invalidate a single
    # title without flushing the whole cache.  Only honoured when an ACCESS_KEY is
    # configured (and therefore already validated above) so open instances can't
    # be made to burn CPU on forced re-renders.
    _force_refresh = bool(
        nocache and nocache.strip().lower() in ("1", "true", "yes") and _cfg.ACCESS_KEY
    )

    # ------------------------------------------------------------------
    # Final poster cache — keyed on imdb_id, type, and a short hash of
    # all rendering parameters so different visual configs don't collide.
    # Skipped when an explicit quality= override is supplied (one-off).
    # ------------------------------------------------------------------
    if (selected_image is not None or not quality) and not _cfg.DISABLE_COMPOSITE_CACHE:
        # Server-side detection settings affect the rendered output but aren't URL
        # params, so fold a signature into the hash.  Toggling detection or
        # changing its thresholds then auto-busts stale composites (and leaves
        # cache keys unchanged when the feature is off — backward compatible).
        if _cfg.TEXTLESS_TEXT_DETECTION:
            from text_detect import DETECT_RES_SIG
            _detect_sig = (
                f"|td={_cfg.PPOCR_BOX_THRESHOLD}:{_cfg.TEXTLESS_DETECTION_MAX_VOTES}:{DETECT_RES_SIG}"
            )
        else:
            _detect_sig = ""
        _poster_selection_sig = (
            f"|ps={_cfg.TMDB_POSTER_MIN_VOTES}:"
            f"{_cfg.TMDB_POSTER_MAX_SCORE_DROP:g}"
        )
        _rating_policy_sig = (
            f"|rp={_cfg.RATING_MIN_VOTES}:"
            f"{int(rcfg.fallback_to_imdb)}"
        )
        _server_sig = "|server=" + _server_render_signature()
        _identity = (
            "&".join(f"{k}={v}" for k, v in sorted(raw_params.items()))
            + _detect_sig
            + _poster_selection_sig
            + _rating_policy_sig
            + _server_sig
        )
        if selected_image is not None:
            _identity = _selected_cache_identity(
                base_identity=_identity,
                selected_sha256=selected_image.sha256,
                selected_logo_sha256=(
                    selected_logo.sha256 if selected_logo is not None else None
                ),
                profile_digest=profile.digest if profile is not None else "",
                renderer_revision=_cfg.SOURCE_REVISION,
                imdb_id=imdb_id,
                tmdb_id=tmdb_id,
                media_type=type,
                quality=quality,
                season=season,
                episode=episode,
                output_format=output_format,
            )
        _hash_length = 32 if selected_image is not None else 16
        _params_hash = hashlib.sha256(_identity.encode()).hexdigest()[:_hash_length]
        if selected_image is not None:
            final_cache_key = _selected_final_cache_key(
                imdb_id,
                tmdb_id,
                type,
                _params_hash,
            )
        elif is_anime:
            # The same IMDb/TMDb pair can render different art when an anime
            # provider ID is present, so preserve that identity in the cache.
            final_cache_key = f"{anime_key}:{imdb_id}:{tmdb_id}:{type}:{_params_hash}"
        else:
            final_cache_key = f"{imdb_id}:{tmdb_id}:{type}:{_params_hash}"
        client_cache_key = _client_cache_key(final_cache_key, selected_image)
        cached_image = None if _force_refresh else get_cached_final_poster(final_cache_key)
        if _force_refresh:
            logger.info(f"Force refresh (nocache) for {final_cache_key} — bypassing cache read")
        if cached_image is not None:
            logger.info(f"Final poster cache hit for {final_cache_key}")
            if client_cache_key is not None:
                etag = f'"{client_cache_key}"'
                if request.headers.get("if-none-match") == etag:
                    return Response(status_code=304)
            return _image_response(cached_image, output_format, client_cache_key)
    else:
        final_cache_key = None
        client_cache_key = None

    # ------------------------------------------------------------------
    # Request coalescing: if another request in this worker is already
    # rendering the same poster, await its result instead of duplicating
    # the pipeline.  Quality-override requests (final_cache_key=None) are
    # always rendered independently.
    # ------------------------------------------------------------------
    _render_fut: "asyncio.Future[bytes] | None" = None
    if final_cache_key is not None:
        _existing_fut = _render_inflight.get(final_cache_key)
        if _existing_fut is not None:
            logger.info(f"Coalescing request for {final_cache_key}")
            try:
                return _image_response(
                    await _existing_fut,
                    output_format,
                    client_cache_key,
                )
            except Exception:
                # The in-flight render failed; fall through and try ourselves.
                pass
        _render_fut = asyncio.get_running_loop().create_future()
        # Suppress asyncio's "Future exception was never retrieved" warning when
        # the render fails and no other request is coalesced onto this future.
        _render_fut.add_done_callback(
            lambda f: f.exception() if not f.cancelled() and f.exception() else None
        )
        _render_inflight[final_cache_key] = _render_fut

    # Declare globals that are both read and written in this function so Python
    # doesn't complain about use-before-global-declaration.
    global _mdblist_active_key_idx

    cached_rating = get_cached_rating(canonical_id)

    if cached_rating is not None:
        (
            cached_ratings_dict,
            cached_genre,
            cached_release_date,
            cached_award_wins,
            cached_award_noms,
            cached_awards_fetched,
            cached_festival_label,
            cached_age_rating,
            cached_is_cult,
            cached_is_true_story,
            cached_is_metacritic,
        ) = cached_rating
    else:
        cached_ratings_dict   = None
        cached_genre          = None
        cached_release_date   = None
        cached_award_wins     = []
        cached_award_noms     = []
        cached_awards_fetched = False
        cached_festival_label = None
        cached_age_rating     = None
        cached_is_cult        = False
        cached_is_true_story  = False
        cached_is_metacritic  = False

    release_date_for_quality_ttl = cached_release_date
    rating_already_cached        = cached_rating is not None

    # ------------------------------------------------------------------
    # Rating fetch coalescing + back-off
    #
    # Goal: ensure at most one MDBList call per imdb_id per worker at a
    # time, and suppress repeated failures with key-scoped cooldowns.
    #
    # Back-off check: if a recent fetch failed, skip that title-key pair
    # until its escalating retry delay expires.
    #
    # Coalescing: if another coroutine in this worker is already fetching
    # the same imdb_id, wait for its asyncio.Event, then re-read the DB.
    # If it succeeded we get the cached data for free; if it failed we
    # re-check the back-off (now set by the other coroutine) before
    # deciding whether to attempt our own call.
    # ------------------------------------------------------------------
    _rating_event_to_set: asyncio.Event | None = None
    _rating_backoff_active = False  # set when backoff nullifies the key; used to suppress final-poster caching
    _mdblist_unavailable_reason = "no API key configured"

    if not rating_already_cached and effective_mdblist_key:
        _loop_now = asyncio.get_running_loop().time()

        # Per-key cooldown: configured server keys may rotate; request-supplied
        # keys remain isolated and simply wait for their own cooldown to expire.
        if effective_mdblist_key and _loop_now < _mdblist_key_cooldown.get(effective_mdblist_key, 0.0):
            _cooling_key = effective_mdblist_key
            _replacement = _next_mdblist_server_key(_cooling_key, _loop_now)
            if _replacement is not None:
                effective_mdblist_key = _replacement
                logger.info(
                    f"MDBList key rotated to key #{_mdblist_active_key_idx + 1} for {imdb_id}"
                )
            else:
                _remaining = _mdblist_key_cooldown.get(_cooling_key, 0.0) - _loop_now
                logger.debug(
                    f"Rating fetch for {imdb_id} skipped "
                    f"(selected MDBList key cooling down; {_remaining:.0f}s remaining)"
                )
                effective_mdblist_key = None
                _rating_backoff_active = True
                _mdblist_unavailable_reason = "selected key is cooling down"

        # Per-title and key backoff (network failures, or this title-key pair's last 429).
        if effective_mdblist_key:
            _retry_key = _rating_retry_key(imdb_id, effective_mdblist_key)
            _backoff_until = _rating_backoff.get(_retry_key)
            if _backoff_until is not None:
                if _loop_now < _backoff_until:
                    logger.debug(f"Rating fetch for {imdb_id} skipped (MDBList back-off active for selected key)")
                    effective_mdblist_key = None
                    _rating_backoff_active = True
                    _mdblist_unavailable_reason = "selected key is in back-off for this title"
                else:
                    del _rating_backoff[_retry_key]       # expired — allow a fresh attempt
                    _rating_fail_count.pop(_retry_key, None)  # reset escalation for clean slate

    if not rating_already_cached and effective_mdblist_key:
        _inflight_event = _rating_fetch_inflight.get(imdb_id)
        if _inflight_event is not None:
            # Another coroutine is mid-fetch — wait and piggyback on its result.
            logger.info(f"Rating fetch coalesced for {imdb_id} — awaiting in-flight fetch")
            await _inflight_event.wait()
            _refreshed = get_cached_rating(imdb_id)
            if _refreshed is not None:
                (
                    cached_ratings_dict,
                    cached_genre,
                    cached_release_date,
                    cached_award_wins,
                    cached_award_noms,
                    cached_awards_fetched,
                    cached_festival_label,
                    cached_age_rating,
                    cached_is_cult,
                    cached_is_true_story,
                    cached_is_metacritic,
                ) = _refreshed
                rating_already_cached        = True
                release_date_for_quality_ttl = cached_release_date
                logger.info(f"Rating coalesce succeeded for {imdb_id} — using cached result")
            else:
                # The owner already made the MDBList attempt for this title.
                # If it did not produce a cache row, do not launch a second
                # same-content request from a waiter in the same burst.
                logger.debug(
                    f"Rating fetch for {imdb_id} suppressed after coalescence "
                    "(owner did not cache rating)"
                )
                effective_mdblist_key = None
                _rating_backoff_active = True
                _mdblist_unavailable_reason = "coalesced fetch did not cache rating"
        else:
            # First request for this imdb_id — claim the fetch slot.
            _rating_event_to_set              = asyncio.Event()
            _rating_fetch_inflight[imdb_id]   = _rating_event_to_set

    # Quality tokens — cache checked exactly once here; fetch fn only writes.
    if quality:
        quality_tokens = parse_quality(quality)
        cached_tokens  = None
    else:
        cached_tokens  = get_cached_quality(canonical_id, release_date_for_quality_ttl)
        quality_tokens = cached_tokens or []

    # A quality source is available when the backend QUALITY_SOURCE selects has
    # the settings it needs — AIOStreams URL + auth, SCRAPER_URL, or QUALICACHE_URL.
    _has_quality_source = quality_source_configured()
    _quality_cooldown_active = _has_quality_source and _quality_backoff_remaining() > 0
    quality_needs_fetch = (
        rcfg.badge_display_mode in (1, 2, 4, 5)
        and not quality
        and cached_tokens is None
        and _has_quality_source
        and not _quality_cooldown_active
    )

    quality_pending = bool(_quality_cooldown_active and cached_tokens is None)
    if quality_needs_fetch and not rcfg.wait_for_quality:
        # Fire-and-forget background fetch — poster is served immediately
        # without badges; the cache will be warm on the next request.
        # Torrentio, Comet and AIOStreams all accept an anime-native stream id
        # ("kitsu:12345:1:1") because that is exactly what Stremio sends them
        # for Kitsu-catalogue items, so the canonical id passes straight
        # through and the quality badge keeps working without an IMDb id.
        if canonical_id not in _quality_bg_inflight:
            _quality_bg_inflight.add(canonical_id)
            asyncio.create_task(
                _background_quality_fetch(
                    canonical_id, type, season, episode,
                    release_date_for_quality_ttl,
                )
            )
            logger.info(f"Quality fetch deferred to background for {canonical_id}")
        else:
            logger.info(f"Quality background fetch already in progress for {canonical_id}")
        quality_needs_fetch = False
        quality_pending = True

    # An anime request without an IMDb id nulls the key deliberately, so this
    # would be noise rather than a warning.
    if not rating_already_cached and not effective_mdblist_key and imdb_id:
        logger.warning(
            f"MDBList unavailable for {imdb_id}: {_mdblist_unavailable_reason} — "
            "poster will be served without rating/award data."
        )

    effective_movie_weights = rcfg.movie_weights or _cfg.MOVIE_WEIGHTS
    effective_tv_weights    = rcfg.tv_weights    or _cfg.TV_WEIGHTS

    if _HTTP_CLIENT is None:
        raise HTTPException(status_code=503, detail="Service unavailable")
    client = _HTTP_CLIENT

    # Secondary preferred language, only when the chosen priority actually uses it.
    _effective_secondary = (
        rcfg.logo_language_secondary
        if rcfg.logo_priority in _SECONDARY_LANGUAGE_PRIORITIES
        else ""
    )

    global _active_poster_renders
    _active_poster_renders += 1
    try:
        # True only while we are actually rendering the anime provider's cover
        # art, which is what the art-specific rules key off. Distinct from
        # is_anime, which stays true for a request that fell back to TMDB art.
        using_anime_art = False
        _anime_art_missing = False
        if is_anime:
            # Neither provider ships title logos, so when a tmdb_id came with
            # the request pull TMDB's metadata alongside — purely for its logo
            # list, which is language-aware and has good anime coverage. The
            # art, title, genres, dates and score still come from the anime
            # provider. This is cached for a week and coalesced, so the
            # amortised cost is one extra call per title per week.
            _anime_meta, _logo_meta = await asyncio.gather(
                anime.fetch_anime_metadata(client, anime_namespace, anime_id),
                _coalesced_fetch_poster_metadata(
                    client, tmdb_id, effective_tmdb_key, type,
                    rcfg.logo_language, _effective_secondary,
                ) if (has_tmdb_id and effective_tmdb_key) else _resolved(None),
                return_exceptions=True,
            )
            if isinstance(_anime_meta, BaseException):
                logger.warning(f"Anime metadata fetch failed for {anime_key}: {_anime_meta}")
                _anime_meta = None
            # A failed logo lookup is never fatal — it just means no logo.
            if isinstance(_logo_meta, BaseException):
                logger.warning(f"TMDB logo lookup failed for {tmdb_id}: {_logo_meta}")
                _logo_meta = None

            using_anime_art = _anime_meta is not None
            if _anime_meta is None and _logo_meta is not None:
                # The provider has no entry, or was throttled or unreachable.
                # TMDB's metadata is already in hand (fetched for the logo), so
                # serve its art rather than dropping to a genre canvas — a
                # strictly better poster, and it degrades gracefully if the
                # provider has an outage. Rendering reverts to the normal TMDB
                # rules; only the anime-specific ART behaviour is skipped.
                logger.info(
                    f"No {anime_namespace} entry for {anime_key} — falling back to TMDB art"
                )
                _anime_meta = _logo_meta
                _anime_art_missing = True
            elif _anime_meta is None:
                # Nothing from either source — same genre canvas path a TMDB
                # title with no art takes.
                _anime_meta = anime.empty_metadata(anime_namespace)
                _anime_art_missing = True
            else:
                _anime_art_missing = False

            (
                genre_ids, is_textless, logos, release_year, title,
                poster_path, backdrop_path, tmdb_data,
            ) = _anime_meta
            if using_anime_art and _logo_meta is not None:
                logos = _logo_meta[2]
        else:
            genre_ids, is_textless, logos, release_year, title, poster_path, backdrop_path, tmdb_data = (
                await _coalesced_fetch_poster_metadata(
                    client, tmdb_id, effective_tmdb_key, type, rcfg.logo_language,
                    _effective_secondary,
                )
            )
        # Canonical IMDb id for downstream lookups (e.g. TVDB remoteid resolution):
        # the request param if supplied, else the one TMDB returned in external_ids.
        effective_imdb_id = (imdb_id or "").strip() or tmdb_data.get("imdb_id") or None
        _text_titles = tuple(dict.fromkeys(
            value for value in (title, tmdb_data.get("original_title")) if value
        ))

        # Resolve genre string from TMDB genre_ids immediately — this is always
        # available regardless of MDBlist status, so we can use it as a reliable
        # fallback if the rating fetch fails or is skipped entirely.
        _gid_set = set(genre_ids)
        _tmdb_genre = "Unknown"
        _genre_priority = (
            _cfg.ANIME_GENRE_PRIORITY if is_anime else _cfg.GENRE_PRIORITY
        )
        for _gid in _genre_priority:
            if _gid in _gid_set:
                _candidate = _cfg.GENRE_MAP.get(_gid, "")
                if _candidate:
                    _tmdb_genre = _candidate
                    break

        # Backdrop fallback: when no null-language textless poster exists, use
        # the landscape backdrop cropped to portrait.  Backdrops are almost always
        # textless by design and TMDB coverage is near-universal, so this recovers
        # the vast majority of titles that would otherwise fall back to a textual
        # poster — OR, when no poster art exists at all, a genre-tinted canvas.
        #   poster missing entirely  → prefer backdrop over the canvas
        #   poster exists with text  → prefer backdrop over the text-burned poster
        _use_backdrop = (
            selected_image is None
            and bool(backdrop_path)
            and (poster_path is None or not is_textless)
        )
        if selected_image is not None:
            is_textless = True
        if _use_backdrop:
            logger.info(f"No textless poster for {tmdb_id} — using backdrop crop as portrait fallback")
            is_textless = True          # backdrop is textless; enable logo compositing

        # Original-art mode: serve a TMDB poster (title baked into the art) as-is.
        # Override the textless/backdrop selection, force is_textless=False so the
        # existing gates skip our logo, text detection and the backdrop rescue.
        # Poster language reuses logo_priority (there's no text fallback here).
        # "native" is the REQUEST's logo_language (selected from poster_langs at
        # render time, so it isn't baked to whatever language first cached this
        # title).  Both fall back to the primary poster; off if none exist.
        _plangs    = tmdb_data.get("poster_langs") or {}
        _p_default = tmdb_data.get("original_poster_path")
        _original_lang = tmdb_data.get("original_language") or ""
        _poster_language_order = image_language_order(
            rcfg.logo_language, _original_lang, rcfg.logo_priority, _effective_secondary
        )
        _priority_lang = _poster_language_order[0] if _poster_language_order else ""
        _ranked_posters = [
            _plangs[language]
            for language in _poster_language_order
            if _plangs.get(language)
        ]
        # art_source only matters when the priority-first language is English —
        # the two TMDB English poster candidates (editorial primary vs
        # community top-rated) can differ meaningfully.  For non-English
        # priority languages TMDB has no separate "primary" concept so we
        # always use the vote-ranked poster regardless of art_source.
        _use_primary = (
            _priority_lang == "en"
            and rcfg.original_art_source == "primary"
        )
        if _use_primary:
            _orig_art = _p_default or next(iter(_ranked_posters), None)
        else:
            _orig_art = next(iter(_ranked_posters), None) or _p_default
        _use_original_art = (
            selected_image is None
            and rcfg.use_original_art
            and bool(_orig_art)
        )
        if _use_original_art:
            poster_path   = _orig_art
            is_textless   = False
            _use_backdrop = False
            logger.info(f"Original-art mode for {tmdb_id} — poster {poster_path} "
                        f"(priority={rcfg.logo_priority})")

        # Anime providers ship exactly one cover image per title and it
        # essentially always has the title logotype baked into the art, so it is
        # served as-is under the same rules as original-art mode: no logo
        # composited over it, no burned-in-text scan (it would reject nearly
        # every one, at the cost of an OCR pass), and no backdrop rescue —
        # AniList banners are 1900x400 and the portrait crop would destroy them.
        #
        # ANIME_COMPOSITE_LOGO (default on) overrides the logo half of that.
        # In practice anime cover art either carries no logotype at all or a
        # small block of Japanese text in a corner that most viewers can't read,
        # so a composited title logo is an improvement often enough to be worth
        # doing unconditionally. Text detection stays off either way: it would
        # flag that Japanese corner text on most titles and suppress the logo
        # inconsistently, which reads worse than always printing it.
        # Gated on using_anime_art, not is_anime: when the provider missed and we
        # fell back to TMDB art, that art follows the ordinary TMDB rules
        # (textless selection, backdrop rescue, text detection) as it would for
        # any other title.
        if using_anime_art and poster_path:
            _use_original_art = True
            _use_backdrop     = False
            is_textless       = bool(_cfg.ANIME_COMPOSITE_LOGO)

        if is_anime and not rating_already_cached and not effective_mdblist_key:
            # No IMDb id (or no key), so MDBList can't be asked. Supply what the
            # provider gave us instead of nothing. With an IMDb id this branch is
            # skipped and the normal MDBList fetch runs; the provider score is
            # merged into its result below either way.
            rating_coro = _resolved((
                None,
                (
                    {},
                    _tmdb_genre,
                    tmdb_data.get("tmdb_release_date"),
                    [],
                    tmdb_data.get("anime_age_rating"),
                ),
            ))
        elif rating_already_cached or not effective_mdblist_key:
            rating_coro = _resolved((
                None,
                (cached_ratings_dict, cached_genre, cached_release_date, [], cached_age_rating),
            ))
        else:
            global _mdblist_semaphore
            if _mdblist_semaphore is None:
                _mdblist_semaphore = asyncio.Semaphore(_cfg.MDBLIST_CONCURRENCY)

            async def _fetch_rating_gated(
                _key: str, _client=client, _imdb_id=imdb_id,
                _gids=genre_ids, _type=type,
                _mw=effective_movie_weights, _tw=effective_tv_weights,
            ):
                nonlocal _rating_backoff_active, _mdblist_unavailable_reason
                async with _mdblist_semaphore:
                    _fetch_key = _key
                    _fetch_now = asyncio.get_running_loop().time()
                    if (
                        _fetch_key in _cfg.SERVER_MDBLIST_KEYS
                        and _fetch_now < _mdblist_key_cooldown.get(_fetch_key, 0.0)
                    ):
                        _replacement_key = _next_mdblist_server_key(_fetch_key, _fetch_now)
                        if _replacement_key is None:
                            _remaining = _mdblist_key_cooldown.get(_fetch_key, 0.0) - _fetch_now
                            logger.debug(
                                f"Rating fetch for {_imdb_id} skipped "
                                f"({_mdblist_server_key_label(_fetch_key)} cooling down; "
                                f"{_remaining:.0f}s remaining)"
                            )
                            _rating_backoff_active = True
                            _mdblist_unavailable_reason = "selected key is cooling down"
                            return None, (
                                cached_ratings_dict, cached_genre,
                                cached_release_date, [], cached_age_rating,
                            )
                        logger.info(
                            f"MDBList key rotated from {_mdblist_server_key_label(_fetch_key)} "
                            f"to {_mdblist_server_key_label(_replacement_key)} for {_imdb_id} "
                            "before outbound fetch"
                        )
                        _fetch_key = _replacement_key
                    return _fetch_key, await fetch_rating(
                        _client, _imdb_id, _fetch_key, _gids, _type,
                        movie_weights=_mw, tv_weights=_tw,
                    )

            rating_coro = _fetch_rating_gated(effective_mdblist_key)

        # Quality is normally fetched in the background (not in this gather).
        # The one exception — wait_for_quality — is handled inline after the
        # gather completes so it never blocks rating coalescing.
        _backdrop_rescued = False
        _detection_deferred = False
        _vc = tmdb_data.get("vote_count")
        _vote_detection_ok = selected_image is not None or _detection_vote_ok(_vc)

        async def _tvdb_is_clean(cand_image, art_id, *, source="backdrop", kind="bd") -> bool:
            """Inline burned-in-text vet for a TVDB candidate (background or poster),
            mirroring the TMDB text-backdrop rescue.  Returns True only when detection
            is available, vote-gated, and reports no text.  Memoised per (tvdb id,
            kind, crop, detector)."""
            if not (_cfg.TEXTLESS_TEXT_DETECTION and _vote_detection_ok):
                return False
            try:
                from text_detect import DETECT_RES_SIG
                _src = f"tvdb_{kind}:{art_id}:{_CROP_VERSION}:ta"
                _key = f"{_src}|conf={_cfg.PPOCR_BOX_THRESHOLD}:{DETECT_RES_SIG}"
                _res = get_cached_text_detection(_key)
                if _res is None:
                    _res = await asyncio.shield(_start_text_detection(
                        _key, cand_image, title=_text_titles, source=source,
                        tmdb_id=tmdb_id, vote_count=_vc, source_key=_src))
                return _res is False
            except Exception as exc:
                logger.warning(f"TVDB {kind} vet failed for {tmdb_id}: {exc}")
                return False

        is_no_poster = (
            selected_image is None
            and poster_path is None
            and not _use_backdrop
        )
        if selected_image is not None:
            _image_coro = _resolved(selected_image.image.copy())
        elif _use_backdrop:
            # Text-aware backdrop cropping also invokes PP-OCR, so apply the
            # same foreground vote gate used by the final burned-in-text scan.
            _backdrop_avoid_text = (
                _cfg.TEXTLESS_TEXT_DETECTION and _vote_detection_ok
            )
            _image_coro = fetch_backdrop_image(
                client, tmdb_id, backdrop_path, avoid_text=_backdrop_avoid_text)
        elif is_no_poster:
            # No poster art at all.  Before settling for the genre canvas, try a
            # TVDB background (curated fanart — usually textless).  Strictly an
            # upgrade over a flat canvas.  Vet for burned-in text where possible;
            # composite our logo only on a clean one, otherwise show it as-is.
            _tvdb_bg = None
            _tvdb_bg_id = None
            if _cfg.TVDB_USE_BACKDROPS and tvdb.tvdb_enabled():
                _bd_avoid = _cfg.TEXTLESS_TEXT_DETECTION and _vote_detection_ok
                _tvdb_bg, _tvdb_bg_id = await tvdb.tvdb_backdrop(
                    client, media_type=type, imdb_id=effective_imdb_id,
                    tmdb_id=tmdb_id, avoid_text=_bd_avoid,
                )
            # Opt-in TVDB poster as a further no-art rescue (TVDB_USE_POSTERS).
            # A real poster — even one carrying its own title — beats a genre
            # canvas; we composite our logo only when it vets clean.
            _tvdb_ps = None
            _tvdb_ps_id = None
            if (_tvdb_bg is None and _cfg.TVDB_USE_POSTERS and tvdb.tvdb_enabled()):
                _tvdb_ps, _tvdb_ps_id = await tvdb.tvdb_poster(
                    client, media_type=type, language=rcfg.logo_language,
                    imdb_id=effective_imdb_id, tmdb_id=tmdb_id,
                )
            if _tvdb_bg is not None:
                if await _tvdb_is_clean(_tvdb_bg, _tvdb_bg_id):
                    is_textless = True           # clean art → composite our logo
                    logger.info(f"TVDB background for {tmdb_id} clean — using with logo")
                else:
                    logger.info(f"TVDB background for {tmdb_id} unvetted/texted — using as-is")
                is_no_poster = False
                _backdrop_rescued = True          # pre-vetted → skip the scan block
                _image_coro = _resolved(_tvdb_bg)
            elif _tvdb_ps is not None:
                if await _tvdb_is_clean(_tvdb_ps, _tvdb_ps_id, source="poster", kind="ps"):
                    is_textless = True
                    logger.info(f"TVDB poster for {tmdb_id} clean — using with logo")
                else:
                    logger.info(f"TVDB poster for {tmdb_id} unvetted/texted — using as-is")
                is_no_poster = False
                _backdrop_rescued = True
                _image_coro = _resolved(_tvdb_ps)
            else:
                # Prefer the atmospheric genre background (minimal or photoreal set,
                # per the request); fall back to the flat genre-tinted gradient if no
                # background art exists for this genre in either set.
                _bg = _load_genre_background(_tmdb_genre, rcfg.fallback_bg_style)
                _image_coro = _resolved(_bg if _bg is not None else _make_fallback_canvas(genre_ids))
        else:
            # Option A: the title has only text-bearing art (no textless poster
            # or backdrop).  Before settling for the busy official poster, try a
            # text-aware crop of a text-bearing backdrop; if it comes out clean
            # we get a nicer image plus our own logo.  Gated to low-vote titles.
            _rescued = None
            _tbp = tmdb_data.get("text_backdrop_path")
            if (selected_image is None and _cfg.TEXTLESS_TEXT_DETECTION and not is_textless and _tbp
                    and not _use_original_art
                    and _detection_vote_ok(tmdb_data.get("vote_count"))):
                try:
                    _cand = await fetch_backdrop_image(client, tmdb_id, _tbp, avoid_text=True)
                    # Memoise per (candidate backdrop and detector settings)
                    # — same rationale as the suppress path: config-independent.
                    from text_detect import DETECT_RES_SIG
                    _resc_src = f"bd:{_tbp}:{_CROP_VERSION}:ta"
                    _resc_key = f"{_resc_src}|conf={_cfg.PPOCR_BOX_THRESHOLD}:{DETECT_RES_SIG}"
                    _still_text = get_cached_text_detection(_resc_key)
                    if _still_text is None:
                        _still_text = await asyncio.shield(_start_text_detection(
                            _resc_key,
                            _cand,
                            title=_text_titles,
                            source="backdrop",
                            tmdb_id=tmdb_id,
                            vote_count=_vc,
                            source_key=_resc_src,
                        ))
                    if _still_text is False:
                        _rescued = _cand
                        logger.info(f"Text-aware backdrop crop clean for {tmdb_id} — using it with logo")
                    else:
                        logger.info(f"Text-aware backdrop crop still has text for {tmdb_id} — keeping official poster")
                except Exception as exc:
                    logger.warning(f"Backdrop rescue failed for {tmdb_id}: {exc}")
            if _rescued is not None:
                is_textless = True            # we now have textless art → composite logo
                _backdrop_rescued = True
                _image_coro = _resolved(_rescued)
            else:
                # Second rescue tier: a TVDB background, vetted the same way.  Only
                # for text-bearing posters (not a clean TMDB textless one), gated to
                # low-vote titles like the TMDB rescue above.  Falls through to the
                # official poster when TVDB has nothing clean.
                _tvdb_bg = None
                _tvdb_bg_id = None
                if (_cfg.TVDB_USE_BACKDROPS and tvdb.tvdb_enabled()
                        and not is_textless and not _use_original_art
                        and _detection_vote_ok(_vc)):
                    _tvdb_bg, _tvdb_bg_id = await tvdb.tvdb_backdrop(
                        client, media_type=type, imdb_id=effective_imdb_id,
                        tmdb_id=tmdb_id, avoid_text=True,
                    )
                # Third rescue tier (opt-in, TVDB_USE_POSTERS): a TVDB poster.
                # These usually have title text baked in, so it's only used when
                # text detection confirms it's clean — otherwise we keep the
                # official poster.  Same low-vote gate.
                _tvdb_ps = None
                _tvdb_ps_id = None
                if (_tvdb_bg is None and _cfg.TVDB_USE_POSTERS and tvdb.tvdb_enabled()
                        and not is_textless and not _use_original_art
                        and _detection_vote_ok(_vc)):
                    _tvdb_ps, _tvdb_ps_id = await tvdb.tvdb_poster(
                        client, media_type=type, language=rcfg.logo_language,
                        imdb_id=effective_imdb_id, tmdb_id=tmdb_id,
                    )
                if _tvdb_bg is not None and await _tvdb_is_clean(_tvdb_bg, _tvdb_bg_id):
                    is_textless = True
                    _backdrop_rescued = True
                    _image_coro = _resolved(_tvdb_bg)
                    logger.info(f"TVDB background rescue clean for {tmdb_id} — using with logo")
                elif _tvdb_ps is not None and await _tvdb_is_clean(
                        _tvdb_ps, _tvdb_ps_id, source="poster", kind="ps"):
                    is_textless = True
                    _backdrop_rescued = True
                    _image_coro = _resolved(_tvdb_ps)
                    logger.info(f"TVDB poster rescue clean for {tmdb_id} — using with logo")
                else:
                    _image_coro = fetch_poster_image(client, tmdb_id, type, poster_path)

        # Start eligible foreground OCR as soon as the image arrives. Higher-vote
        # assets are recorded as deferred work instead: the request keeps waiting
        # for logo/rating/info, but never waits for their textless scan.
        _detection_task: "asyncio.Task[bool | None] | None" = None
        _detection_result: bool | None = False
        _det_src: str | None = None
        _det_key: str | None = None
        _scan_selected_image = (
            _cfg.TEXTLESS_TEXT_DETECTION
            and is_textless
            and not is_no_poster
            and not _backdrop_rescued
            # Anime art is deliberately composited with a logo regardless of any
            # Japanese corner text, so the scan would only burn an OCR pass to
            # produce an inconsistent result. See the is_textless assignment.
            # TMDB fallback art is scanned normally.
            and not using_anime_art
        )
        if _scan_selected_image:
            from text_detect import DETECT_RES_SIG

            if selected_image is not None:
                _det_src = f"selected:{selected_image.sha256}"
                _image_cache_key = _det_src
                _det_source = "poster"
            elif _use_backdrop:
                _crop_variant = "ta" if _backdrop_avoid_text else "plain"
                _det_src = f"bd:{backdrop_path}:{_CROP_VERSION}:{_crop_variant}"
                _image_cache_key = (
                    f"backdrop_{tmdb_id}_{backdrop_path.strip('/')}_{_CROP_VERSION}"
                    + ("_ta" if _backdrop_avoid_text else "")
                )
                _det_source = "backdrop"
            else:
                _det_src = f"ps:{poster_path}"
                _image_cache_key = f"{type}_{tmdb_id}_{poster_path.strip('/')}"
                _det_source = "poster"

            _det_key = (
                f"{_det_src}|conf={_cfg.PPOCR_BOX_THRESHOLD}:{DETECT_RES_SIG}"
            )
            _detection_result = get_cached_text_detection(_det_key)
            if _detection_result is None:
                _base_image_coro = _image_coro
                _foreground_detection_required = (
                    _vote_detection_ok or selected_logo is not None
                )
                if _foreground_detection_required:
                    _reserve_foreground_detection()

                async def _fetch_image_and_schedule_detection():
                    nonlocal _detection_task, _detection_deferred
                    try:
                        fetched_image = await _base_image_coro
                    except BaseException:
                        if _foreground_detection_required:
                            _release_foreground_detection()
                        raise
                    if _foreground_detection_required:
                        _detection_task = _start_text_detection(
                            _det_key,
                            fetched_image,
                            title=_text_titles,
                            source=_det_source,
                            tmdb_id=tmdb_id,
                            vote_count=_vc,
                            source_key=_det_src,
                            media_type=type,
                            image_path=poster_path,
                            foreground_reserved=True,
                        )
                    else:
                        _detection_deferred = True
                        _queue_background_text_detection(_DeferredTextDetection(
                            cache_key=_det_key,
                            image_cache_key=_image_cache_key,
                            title=_text_titles,
                            source=_det_source,
                            tmdb_id=tmdb_id,
                            media_type=type,
                            image_path=poster_path,
                            vote_count=_vc,
                            source_key=_det_src,
                        ))
                    return fetched_image

                _image_coro = _fetch_image_and_schedule_detection()

        # Logo resolution across TMDB, the Metahub CDN, and (optionally) TVDB.
        # TVDB's position in the chain is set by TVDB_LOGO_PRIORITY:
        #   1 = TVDB first, 2 = after TMDB but before Metahub, 3 = last resort.
        # Priority 3 (default) and a missing TVDB key both reduce to the original
        # TMDB -> Metahub -> (TVDB) behaviour, so existing output is unchanged.
        _tvdb_logo_pri = _cfg.TVDB_LOGO_PRIORITY if tvdb.tvdb_enabled() else 3

        async def _resolve_logo():
            async def _tmdb(use_metahub):
                return await fetch_logo(
                    client, logos, rcfg.logo_language,
                    imdb_id=effective_imdb_id,
                    original_language=tmdb_data.get("original_language"),
                    logo_priority=rcfg.logo_priority,
                    use_metahub=use_metahub,
                    secondary_language=_effective_secondary,
                )

            async def _tvdb():
                return await tvdb.tvdb_logo(
                    client, media_type=type, logo_language=rcfg.logo_language,
                    original_language=tmdb_data.get("original_language"),
                    logo_priority=rcfg.logo_priority,
                    secondary_language=_effective_secondary,
                    imdb_id=effective_imdb_id, tmdb_id=tmdb_id,
                )

            async def _metahub():
                return (await _fetch_metahub_logo(client, effective_imdb_id)
                        if effective_imdb_id else None)

            # These modes have their own explicit order: TMDB language buckets
            # (native, then custom/original for the native_custom_* variants) ->
            # TMDB English -> Metahub -> TMDB neutral -> rendered text.
            if rcfg.logo_priority in _TEXT_FORWARD_LOGO_PRIORITIES:
                return await _tmdb(use_metahub=True)

            if _tvdb_logo_pri == 1:
                return (await _tvdb()) or (await _tmdb(use_metahub=True))
            if _tvdb_logo_pri == 2:
                return (await _tmdb(use_metahub=False)) or (await _tvdb()) or (await _metahub())
            # priority 3 — TMDB -> Metahub -> TVDB
            return (await _tmdb(use_metahub=True)) or (await _tvdb())

        (
            image,
            logo,
            rating_fetch_result,
            trending_rank,
        ) = await asyncio.gather(
            _image_coro,
            (
                _resolved(selected_logo.image.copy())
                if selected_logo is not None
                else _resolve_logo()
            )
            if (is_textless and not is_no_poster)
            else _resolved(None),
            rating_coro,
            # Trending rank is a TMDB list lookup, so it needs a real tmdb_id —
            # which AIOMetadata does send alongside the anime id when it has one.
            fetch_trending_rank(client, tmdb_id, effective_tmdb_key, type)
            if has_tmdb_id and effective_tmdb_key else _resolved(None),
        )

        rating_key_used, rating_result = rating_fetch_result
        if rating_key_used is not None:
            effective_mdblist_key = rating_key_used
        elif _rating_backoff_active:
            effective_mdblist_key = None

        # A rate-limited server key gets one same-request rescue attempt on the
        # next healthy configured key. Query-supplied keys remain isolated.
        #
        # This rescue is hand-rolled rather than routed through _with_retry on
        # purpose: _with_retry re-calls blindly on FETCH_FAILED, so wrapping the
        # rating fetch would fire a second request at the key that just returned
        # 429 — before the cooldown below can register it — doubling load on a
        # key that explicitly asked us to back off. Rotate first, then retry.
        if isinstance(rating_result, _RateLimited) and effective_mdblist_key:
            _failed_key = effective_mdblist_key
            _backoff_secs, _rescue_key = _mark_mdblist_rate_limit(
                imdb_id, _failed_key, rating_result
            )
            logger.warning(
                f"MDBList {_mdblist_server_key_label(_failed_key)} rate-limited "
                f"for {imdb_id}; cooling down for {_backoff_secs:.0f}s"
            )
            if _rescue_key is not None:
                effective_mdblist_key = _rescue_key
                logger.warning(
                    f"Retrying MDBList for {imdb_id} with "
                    f"{_mdblist_server_key_label(_rescue_key)}"
                )
                _rescue_used_key, rating_result = await _fetch_rating_gated(_rescue_key)
                if _rescue_used_key is not None:
                    effective_mdblist_key = _rescue_used_key
                elif _rating_backoff_active:
                    effective_mdblist_key = None

        # Inline quality wait — runs after gather so rating coalescing is never
        # blocked.  Used for poster-warm workflows where latency doesn't matter.
        if quality_needs_fetch and rcfg.wait_for_quality:
            try:
                fetched = await asyncio.wait_for(
                    _with_retry(
                        fetch_quality,
                        client, imdb_id, type, season, episode, release_date_for_quality_ttl,
                    ),
                    timeout=_cfg.QUALITY_WAIT_TIMEOUT,
                )
                _record_quality_result(fetched)
                if fetched is QUALITY_PENDING:
                    # QualiCache has queued this title but has no value yet.
                    # Waiting longer wouldn't help — it collects out of band.
                    logger.info(
                        f"Inline quality fetch pending for {imdb_id} "
                        "— serving without quality, composite not cached"
                    )
                    quality_pending = True
                elif fetched is not FETCH_FAILED:
                    quality_tokens = fetched
                    logger.info(f"Inline quality fetch complete for {imdb_id}: {quality_tokens}")
                else:
                    # The quality source returned a transient error — don't cache
                    # the composite poster without quality so the next request retries.
                    logger.warning(
                        f"Inline quality fetch failed for {imdb_id} "
                        "— serving without quality, composite not cached"
                    )
                    quality_pending = True
            except asyncio.TimeoutError:
                _record_quality_result(FETCH_FAILED)
                logger.warning(
                    f"Quality wait timed out for {imdb_id} "
                    f"after {_cfg.QUALITY_WAIT_TIMEOUT:.0f}s — serving without quality, "
                    "composite not cached so next request retries"
                )
                quality_pending = True
            quality_needs_fetch = False

        # ------------------------------------------------------------------
        # Unpack results
        # ------------------------------------------------------------------
        rate_limited  = isinstance(rating_result, _RateLimited)
        rating_failed = (
            not rating_already_cached
            and effective_mdblist_key
            and (rating_result is FETCH_FAILED or rate_limited)
        )

        if rating_failed:
            if rate_limited:
                _retry_key = _rating_retry_key(imdb_id, effective_mdblist_key)
                if _retry_key not in _rating_backoff:
                    backoff_secs, _ = _mark_mdblist_rate_limit(
                        imdb_id, effective_mdblist_key, rating_result
                    )
                    logger.warning(
                        f"MDBList rate-limited {imdb_id}; key cooling down for "
                        f"{backoff_secs:.0f}s"
                    )
            else:
                # Network / timeout failure — escalating back-off so a transient
                # hiccup retries quickly while a sustained outage backs off further.
                # Ladder: 30 s → 2 min → 8 min → 1 h (cap), using 4× multiplier.
                _failed_retry_key = _rating_retry_key(imdb_id, effective_mdblist_key)
                fail_n = _rating_fail_count.get(_failed_retry_key, 0) + 1
                _rating_fail_count[_failed_retry_key] = fail_n
                backoff_secs = min(30 * (4 ** (fail_n - 1)), 3600.0)
                logger.warning(
                    f"Rating fetch failed for {imdb_id} (attempt {fail_n}) "
                    f"— back-off {backoff_secs:.0f}s"
                )
            if not rate_limited:
                _failed_retry_key = _rating_retry_key(imdb_id, effective_mdblist_key)
                _rating_backoff[_failed_retry_key] = asyncio.get_running_loop().time() + backoff_secs
            ratings_dict   = {}
            genre          = cached_genre or _tmdb_genre
            rel            = cached_release_date
            score          = "N/A"
            keywords       = []
            award_wins     = cached_award_wins
            award_noms     = cached_award_noms
            festival_label = cached_festival_label
            age_rating     = cached_age_rating
            is_cult        = cached_is_cult
            is_true_story  = cached_is_true_story
            is_metacritic  = cached_is_metacritic
        else:
            ratings_dict, genre, rel, keywords, age_rating = rating_result
            # genre from MDBlist/cache may be None when the key is absent and
            # nothing is cached yet — fall back to the TMDB-derived genre.
            #
            # On the anime path the genre is always derived here from the
            # provider's own genre list rather than read back from the cached
            # rating row. The row's genre column exists to carry MDBList's
            # answer, which anime titles never have; trusting it would pin a
            # title to whatever ANIME_GENRE_PRIORITY said when it was first
            # cached, so a reordering wouldn't take effect until the TTL expired.
            genre = _tmdb_genre if is_anime else (genre or _tmdb_genre)

            # The provider's score rides along in the art response, so merge it
            # into whatever MDBList returned — or into an empty dict when there
            # was no IMDb id to ask about. Done here rather than at the fetch so
            # it also covers the cached path, where the row may have been first
            # written by a request that carried no anime id.
            if is_anime and isinstance(ratings_dict, dict):
                _provider_score = tmdb_data.get("anime_score")
                if _provider_score is not None:
                    ratings_dict = {**ratings_dict, anime_namespace: _provider_score}
            # Likewise the age rating: MDBList supplies one for titles it knows,
            # but Kitsu's ageRating covers those it doesn't.
            if is_anime and age_rating is None:
                age_rating = tmdb_data.get("anime_age_rating")

            # Fresh successful fetch — clear any escalation state so future
            # failures start back at the shortest interval.
            if (
                not rating_already_cached
                and not _rating_backoff_active
                and effective_mdblist_key
            ):
                _rating_fail_count.pop(
                    _rating_retry_key(imdb_id, effective_mdblist_key), None
                )

            if isinstance(ratings_dict, dict):
                weights = (
                    effective_tv_weights
                    if type in ("tv", "series")
                    else effective_movie_weights
                )
                score = calculate_weighted_score(
                    ratings_dict,
                    weights,
                    fallback_to_imdb=rcfg.fallback_to_imdb,
                    # The provider's score is the only rating an anime-native
                    # title has, and existing weights strings name none of the
                    # anime sources, so fall back to it rather than showing N/A.
                    # Giving anilist/kitsu a real weight overrides this.
                    fallback_source=anime_namespace if is_anime else None,
                )
            else:
                score = ratings_dict

            if rating_already_cached:
                award_wins     = cached_award_wins
                award_noms     = cached_award_noms
                festival_label = cached_festival_label
                age_rating     = cached_age_rating
                is_cult        = cached_is_cult
                is_true_story  = cached_is_true_story
                is_metacritic  = cached_is_metacritic
            else:
                award_wins, award_noms = parse_mdblist_awards(
                    keywords,
                    tmdb_id=tmdb_id,
                )
                kw_names = {(kw.get("name") or "").lower().strip() for kw in keywords}
                festival_label = next(
                    (label for kw, label in FESTIVAL_KEYWORDS.items() if kw in kw_names),
                    None,
                )
                is_cult       = bool({"cult-classic", "cult-film"} & kw_names)
                is_true_story = "based-on-true-story" in kw_names
                is_metacritic = "metacritic-must-see" in kw_names
                logger.info(f"Awards for {canonical_id}: wins={award_wins} noms={award_noms} "
                            f"festival={festival_label} age_rating={age_rating} "
                            f"cult={is_cult} true_story={is_true_story} metacritic={is_metacritic}")

        # ------------------------------------------------------------------
        # Write rating + awards to cache (only on a fresh fetch).
        # ------------------------------------------------------------------
        # Anime ratings come from the provider rather than MDBList, so they are
        # cached on the same terms but gated on is_anime instead of a key.
        if not rating_failed and not rating_already_cached and (
            effective_mdblist_key or is_anime
        ):
            set_cached_rating(
                canonical_id,
                ratings_dict if isinstance(ratings_dict, dict) else {},
                genre,
                rel,
                award_wins,
                award_noms,
                awards_fetched=True,
                festival_label=festival_label,
                age_rating=age_rating,
                is_cult=is_cult,
                is_true_story=is_true_story,
                is_metacritic=is_metacritic,
            )
            logger.info(f"Rating cached for {canonical_id}: score={score} genre={genre} "
                        f"wins={award_wins} noms={award_noms} festival={festival_label} "
                        f"age_rating={age_rating}")

        # Publish completion only after success is cached or failure backoff is
        # established. Otherwise a waiter can wake, miss the row, and duplicate
        # the same MDBList request.
        if _rating_event_to_set is not None:
            _rating_event_to_set.set()
            _rating_fetch_inflight.pop(imdb_id, None)
            _rating_event_to_set = None

        logger.info(f"Quality for {canonical_id}: tokens={quality_tokens} year={release_year}")

        # ------------------------------------------------------------------
        # Release status / freshness facts. TV status is mapped from already
        # fetched metadata. Movie digital freshness uses the cached TMDB
        # /release_dates helper only when the Just Added sash is enabled.
        # ------------------------------------------------------------------
        _release_status: str | None = None
        _recent_digital_release_date: str | None = None
        _rs_slots = {"release_status", "cinema", "streaming", "physical", "production", "ended", "cancelled", "airing"}
        if any(s in rcfg.sash_priority for s in _rs_slots):
            # Resolved for every title regardless of age.  There used to be an
            # age gate here that skipped the lookup for anything older than a
            # configurable limit, but it silently blanked the status on older
            # titles — which read as a bug rather than a setting.  Results are
            # cached, and stale "Cinema" on an old film is handled properly by
            # CINEMA_MAX_AGE_YEARS, which downgrades it to "Streaming".
            # For series this is a pure mapping of the status field already in
            # hand — no API call — so anime series get their lifecycle sashes
            # from the provider's status. The movie branch needs TMDB's
            # /release_dates, so it runs only when a real tmdb_id and key came
            # with the request; otherwise the slot simply doesn't fire.
            if (type in ("tv", "series")
                    or (has_tmdb_id and effective_tmdb_key)):
                _release_status = await fetch_release_status(
                    client, tmdb_id, effective_tmdb_key, type,
                    tmdb_data.get("tmdb_status"),
                )
            # r/movieleaks confirmation overrides TMDB's theatrical/production
            # status — if the film is in the digital-release cache it's already
            # streaming regardless of what the official release dates say.
            if _release_status in ("Cinema", "Production") and is_digital_release(imdb_id):
                _release_status = "Streaming"
            # Cinema-only mode: keep the badge purely as an "unavailable" marker —
            # show only Cinema / Production and drop the rest so the slot is
            # skipped (and lower-priority sashes can surface) for released titles.
            if rcfg.release_status_cinema_only and _release_status not in ("Cinema", "Production"):
                _release_status = None

        if (type not in ("tv", "series") and "just_added" in rcfg.sash_priority
                and has_tmdb_id and effective_tmdb_key):
            _recent_digital_release_date = await fetch_recent_movie_digital_release_date(
                client, tmdb_id, effective_tmdb_key,
                tmdb_data.get("tmdb_status"),
            )

        # ------------------------------------------------------------------
        # Build DiscoveryMeta
        # ------------------------------------------------------------------
        discovery_meta = extract_discovery_meta(
            tmdb_data=tmdb_data,
            media_type=type,
            award_wins=award_wins,
            award_noms=award_noms,
            trending_rank=trending_rank,
            release_date=rel,
            keywords=keywords if not rating_already_cached else [],
            festival_label_override=festival_label,
            is_cult_override=is_cult,
            is_true_story_override=is_true_story,
            is_metacritic_override=is_metacritic,
            is_digital_release_override=is_digital_release(imdb_id),
            release_status_override=_release_status,
            recent_digital_release_date=_recent_digital_release_date,
        )

        _sash_priority = rcfg.sash_priority

        # ------------------------------------------------------------------
        # Debug mode: return diagnostic JSON instead of rendering the poster.
        # Useful for troubleshooting wrong sashes, missing ratings, etc.
        # Activate with ?debug=1 (never cached, never stored).
        # ------------------------------------------------------------------
        if debug and debug.strip() in ("1", "true"):
            _sash_result = pick_sash(discovery_meta, _sash_priority)
            return JSONResponse({
                "imdb_id":           imdb_id,
                "tmdb_id":           tmdb_id,
                "type":              type,
                "score":             score if isinstance(score, str) else int(score),
                "genre":             genre,
                "release_year":      release_year,
                "release_date":      rel,
                "quality_tokens":    quality_tokens,
                "age_rating":        age_rating,
                "award_wins":        award_wins,
                "award_noms":        award_noms,
                "festival_label":    festival_label,
                "sash":              {"label": _sash_result[0], "type": _sash_result[1]} if _sash_result else None,
                "is_cult":           discovery_meta.is_cult,
                "is_true_story":     discovery_meta.is_true_story,
                "is_metacritic":     discovery_meta.is_metacritic_must_see,
                "is_new_release":    discovery_meta.is_new_release,
                "is_digital_release":discovery_meta.is_digital_release,
                "recent_digital_release_date": _recent_digital_release_date,
                "is_premiere":       discovery_meta.is_premiere,
                "is_just_added":     discovery_meta.is_just_added,
                "is_new_season":     discovery_meta.is_new_season,
                "is_returning":      discovery_meta.is_returning,
                "is_season_finale":  discovery_meta.is_season_finale,
                "trending_rank":     discovery_meta.trending_rank,
                "original_language": discovery_meta.original_language,
                "matched_studios":   discovery_meta.matched_studios,
                "matched_directors": discovery_meta.matched_directors,
                "matched_cast":      discovery_meta.matched_cast,
                "release_status":    discovery_meta.release_status,
                "sash_priority":     _sash_priority,
                "badge_display_mode":rcfg.badge_display_mode,
                "rating_display_mode":rcfg.rating_display_mode,
            })

        # ------------------------------------------------------------------
        # Burned-in-text detection. When a poster TMDB
        # tagged "textless" actually has the title burned in, compositing our
        # own logo/title would double it — so detect that and skip our overlay.
        # Cached results are always used. Uncached assets above the vote gate
        # are deferred until foreground poster rendering is idle.
        # ------------------------------------------------------------------
        _suppress_overlay = False
        if _scan_selected_image:
            _suppress_overlay = _detection_result
            if _suppress_overlay is None and _detection_task is not None:
                _suppress_overlay = await asyncio.shield(_detection_task)

            if _detection_deferred:
                logger.info(
                    f"Foreground text detection skipped for {tmdb_id}: "
                    f"vote_count={_vc!r} is outside foreground limit "
                    f"{_cfg.TEXTLESS_DETECTION_MAX_VOTES}; background scan queued"
                )
                _suppress_overlay = False
            elif _suppress_overlay is True:
                if selected_image is None and not _use_backdrop and poster_path:
                    from textless_report import report_fake_textless_poster
                    report_fake_textless_poster(
                        media_type=type,
                        tmdb_id=tmdb_id,
                        image_path=poster_path,
                        vote_count=_vc,
                    )
                logger.info(
                    f"Burned-in text detected on textless poster {tmdb_id} "
                    f"(votes={_vc}); skipping logo/title overlay"
                )
                if selected_logo is not None:
                    raise _SelectionError(
                        422,
                        "selected_primary_contains_text",
                        "Selected Primary contains title text and cannot be "
                        "combined with the selected Logo",
                    )
            elif _suppress_overlay is False:
                logger.info(
                    f"No burned-in text detected on textless poster {tmdb_id} "
                    f"(votes={_vc})"
                )
            else:
                from text_detect import text_detection_status
                if selected_logo is not None:
                    raise _SelectionError(
                        503,
                        "text_detection_unavailable",
                        "Selected Primary could not be checked for title text",
                    )
                logger.warning(
                    f"Burned-in text scan unavailable for {tmdb_id}; "
                    f"result was not cached ({text_detection_status()})"
                )
                _suppress_overlay = False

        # Offload CPU-bound PIL compositing + image encoding to the thread pool
        # so the event loop stays free for concurrent requests.
        _bp_args = dict(
            logo=logo if (is_textless and not is_no_poster and not rcfg.textless
                          and not _suppress_overlay) else None,
            fallback_title=(
                title if is_no_poster
                else (title if is_textless and not logo and not rcfg.textless
                      and not _suppress_overlay else None)
            ),
            discovery_meta=discovery_meta,
            quality_tokens=quality_tokens,
            release_year=release_year,
            age_rating=age_rating,
            no_poster=is_no_poster,
        )

        def _composite_and_encode() -> bytes:
            result = build_poster(image, score, genre, rcfg, **_bp_args)
            return _encode_rendered_image(result, output_format)

        img_bytes = await asyncio.get_running_loop().run_in_executor(
            None, _composite_and_encode
        )

        # Persist the finished poster so future requests skip the pipeline.
        # Skipped when:
        #   quality_pending      — badges would be missing; next request caches properly
        #   _detection_deferred — vote-gated OCR is queued in the background
        #   rating_failed        — MDBlist returned a hard failure; don't lock in N/A score
        #   _rating_backoff_active — a previous failure is still in its cool-down window;
        #                            backoff nullifies effective_mdblist_key so rating_failed
        #                            would evaluate False without this separate flag
        #   _anime_art_missing     — the provider had nothing this time, usually a
        #                            throttle or a blip rather than a real absence.
        #                            Caching the fallback would pin TMDB art for
        #                            the whole composite TTL, so let it re-render.
        if (final_cache_key is not None and not quality_pending and not _detection_deferred
                and not rating_failed and not _rating_backoff_active
                and not _anime_art_missing):
            _ttl_override = None
            if discovery_meta is not None:
                _sash_result = pick_sash(discovery_meta, _sash_priority)
                if _sash_result and _sash_result[1] in ("trending", "trending_broad"):
                    _ttl_override = 86400
                    
            set_cached_final_poster(
                final_cache_key,
                img_bytes,
                request_params=_sanitize_request_params(request.url.query),
                ttl_override=_ttl_override
            )
            logger.info(f"Final poster cached for {final_cache_key}")

        if _render_fut is not None:
            _render_fut.set_result(img_bytes)

        return _image_response(img_bytes, output_format, client_cache_key)

    except _SelectionError as exc:
        if _render_fut is not None and not _render_fut.done():
            _render_fut.set_exception(exc)
        raise
    except ValueError as exc:
        if _render_fut is not None and not _render_fut.done():
            _render_fut.set_exception(exc)
        logger.warning(f"No poster available for tmdb_id={tmdb_id}: {exc}")
        raise HTTPException(status_code=404, detail=str(exc))
    except httpx.TimeoutException as exc:
        if _render_fut is not None and not _render_fut.done():
            _render_fut.set_exception(exc)
        logger.warning(f"Upstream timeout for tmdb_id={tmdb_id}: {exc.__class__.__name__}")
        raise HTTPException(status_code=504, detail="Upstream request timed out")
    except httpx.HTTPStatusError as exc:
        if _render_fut is not None and not _render_fut.done():
            _render_fut.set_exception(exc)
        status = exc.response.status_code
        if status == 404:
            # TMDB returned metadata with a poster/image path that no longer exists.
            # Invalidate the (per-language) metadata cache so the next request
            # re-fetches fresh data.
            _endpoint = "tv" if type in ("tv", "series") else "movie"
            delete_cached_tmdb_metadata(tmdb_metadata_cache_key(
                _endpoint, tmdb_id, rcfg.logo_language
            ))
            logger.warning(
                f"TMDB image 404 for tmdb_id={tmdb_id} — metadata cache invalidated, "
                f"will self-heal on next request"
            )
            raise HTTPException(status_code=404, detail="Poster image not found on TMDB")
        logger.error(f"Upstream HTTP {status} for tmdb_id={tmdb_id}: {exc}")
        raise HTTPException(status_code=502, detail=f"Upstream error {status}")
    except Exception as exc:
        if _render_fut is not None and not _render_fut.done():
            _render_fut.set_exception(exc)
        logger.exception(f"Error building poster for tmdb_id={tmdb_id}")
        raise HTTPException(status_code=500, detail="Failed to build poster")
    finally:
        _active_poster_renders = max(0, _active_poster_renders - 1)
        # Fire the rating event so any coalesced waiters unblock. Under normal
        # operation this was set after cache persistence; this is the safety
        # net for error paths that exit before reaching that point.
        if _rating_event_to_set is not None:
            _rating_event_to_set.set()
            _rating_fetch_inflight.pop(imdb_id, None)
        if final_cache_key is not None:
            _render_inflight.pop(final_cache_key, None)


@app.get("/poster")
async def get_poster(
    request: Request,
    tmdb_id: str = "",
    imdb_id: str = "",
    anilist_id: str = "",
    kitsu_id: str = "",
    stremio_id: str = "",
    type: str = "movie",
    quality: str = "",
    season: int = 1,
    episode: int = 1,
    access_key: str = "",
    mdblist_key: str = "",
    tmdb_key: str = "",
    show_award_sash: str | None = None,
    badge_display_mode: str | None = None,
    show_quality_badges: str | None = None,
    rating_display_mode: str | None = None,
    accent_bar_font_size_ratio: str | None = None,
    numeric_score_font_size_ratio: str | None = None,
    accent_bar_y_offset: str | None = None,
    numeric_score_y_offset: str | None = None,
    minimalist_mode_font_size_ratio: str | None = None,
    minimalist_mode_font_x_offset: str | None = None,
    minimalist_mode_font_y_offset: str | None = None,
    score_glow_threshold: str | None = None,
    score_glow_blur: str | None = None,
    score_glow_alpha: str | None = None,
    logo_max_w_ratio: str | None = None,
    logo_max_h_ratio: str | None = None,
    logo_bottom_ratio: str | None = None,
    badge_height: str | None = None,
    badge_gap: str | None = None,
    badge_anchor_x: str | None = None,
    badge_anchor_y: str | None = None,
    movie_weights: str | None = None,
    tv_weights: str | None = None,
    logo_language: str | None = None,
    sash_priority: str | None = None,
    muted: str | None = None,
    textless: str | None = None,
    score_color_mode: str | None = None,
    debug: str | None = None,
    nocache: str | None = None,
):
    return await _render_poster(
        request=request,
        tmdb_id=tmdb_id,
        imdb_id=imdb_id,
        anilist_id=anilist_id,
        kitsu_id=kitsu_id,
        stremio_id=stremio_id,
        type=type,
        quality=quality,
        season=season,
        episode=episode,
        mdblist_key=mdblist_key,
        tmdb_key=tmdb_key,
        debug=debug,
        nocache=nocache,
    )


def _validate_private_render_request(
    request: Request,
    profile: str,
    tmdb_id: str,
    imdb_id: str,
    media_type: str,
    season: int,
    episode: int,
    output_format: str,
    *,
    machine_errors: bool = False,
) -> tuple[RenderProfile, str]:
    def _fail(status_code: int, code: str, message: str):
        if machine_errors:
            raise _SelectionError(status_code, code, message)
        raise HTTPException(status_code=status_code, detail=message)

    try:
        _check_tmdb_id(tmdb_id)
        _check_imdb_id(imdb_id)
        _check_type(media_type)
    except HTTPException as exc:
        _fail(exc.status_code, "invalid_request", str(exc.detail))
    normalized_output_format = output_format.lower()
    if normalized_output_format not in _OUTPUT_FORMATS:
        _fail(400, "invalid_request", "Invalid output_format")
    if season < 1 or episode < 1:
        _fail(400, "invalid_request", "season and episode must be positive")
    if not _cfg.ACCESS_KEY:
        _fail(
            503,
            "renderer_unavailable",
            "ACCESS_KEY must be configured for selected rendering",
        )
    header_key = request.headers.get("x-jellyfin-artwork-key", "")
    if not _access_key_matches(header_key):
        _fail(403, "unauthorized", "Unauthorized")
    render_profile = _render_profile
    if render_profile is None:
        _fail(503, "renderer_unavailable", "Render profile is not loaded")
    if profile != render_profile.name:
        _fail(404, "unknown_profile", "Unknown render profile")

    allowed_query = {
        "episode",
        "imdb_id",
        "output_format",
        "profile",
        "quality",
        "season",
        "tmdb_id",
        "type",
    }
    unknown_query = sorted(set(request.query_params) - allowed_query)
    if unknown_query:
        _fail(
            400,
            "invalid_request",
            f"Unsupported query parameters: {', '.join(unknown_query)}",
        )
    return render_profile, normalized_output_format


def _apply_private_render_headers(
    response: Response,
    render_profile: RenderProfile,
    primary: _SelectedImage,
    logo: _SelectedImage | None,
) -> None:
    response.headers["X-Render-Profile-SHA256"] = render_profile.digest
    response.headers["X-Renderer-Revision"] = _cfg.SOURCE_REVISION
    response.headers["X-Selected-Primary-SHA256"] = primary.sha256
    response.headers["X-Logo-Source"] = "selected" if logo is not None else "provider"
    if logo is not None:
        response.headers["X-Selected-Logo-SHA256"] = logo.sha256
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@app.post("/render/selected")
async def render_selected(
    request: Request,
    profile: str,
    tmdb_id: str,
    imdb_id: str,
    type: str = "movie",
    quality: str = "",
    season: int = 1,
    episode: int = 1,
    output_format: str = "jpeg",
):
    render_profile, normalized_output_format = _validate_private_render_request(
        request,
        profile,
        tmdb_id,
        imdb_id,
        type,
        season,
        episode,
        output_format,
    )

    body = await _read_selected_body(request)
    selected_image = await asyncio.get_running_loop().run_in_executor(
        None,
        _decode_selected_image,
        body,
        request.headers.get("content-type", ""),
    )
    response = await _render_poster(
        request=request,
        tmdb_id=tmdb_id,
        imdb_id=imdb_id,
        type=type,
        quality=quality,
        season=season,
        episode=episode,
        selected_image=selected_image,
        profile=render_profile,
        output_format=normalized_output_format,
    )
    _apply_private_render_headers(
        response,
        render_profile,
        selected_image,
        None,
    )
    return response


@app.post("/render/selection")
async def render_selection(
    request: Request,
    profile: str,
    tmdb_id: str,
    imdb_id: str,
    type: str = "movie",
    quality: str = "",
    season: int = 1,
    episode: int = 1,
    output_format: str = "jpeg",
):
    render_profile, normalized_output_format = _validate_private_render_request(
        request,
        profile,
        tmdb_id,
        imdb_id,
        type,
        season,
        episode,
        output_format,
        machine_errors=True,
    )
    body = await _read_selection_body(request)
    selection = await asyncio.get_running_loop().run_in_executor(
        None,
        _decode_selection_envelope,
        body,
    )
    if selection.logo is not None and (
        not _cfg.TEXTLESS_TEXT_DETECTION or not _text_detector_ready()
    ):
        raise _SelectionError(
            503,
            "text_detection_unavailable",
            "Text detection must be ready for an explicit selected Logo",
        )

    response = await _render_poster(
        request=request,
        tmdb_id=tmdb_id,
        imdb_id=imdb_id,
        type=type,
        quality=quality,
        season=season,
        episode=episode,
        selected_image=selection.primary,
        selected_logo=selection.logo,
        profile=render_profile,
        output_format=normalized_output_format,
    )
    _apply_private_render_headers(
        response,
        render_profile,
        selection.primary,
        selection.logo,
    )
    return response