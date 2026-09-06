"""Authenticated selected-input protocol primitives for the production fork."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import io
import json
import re
import warnings
from dataclasses import dataclass

import numpy as np
from fastapi import HTTPException, Request
from fastapi.responses import Response
from PIL import Image, UnidentifiedImageError

import config as _cfg
from artwork_candidates import effective_discovery_key
from render_profile import RenderProfile


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
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class SelectedImage:
    image: Image.Image
    sha256: str
    decoded_format: str


@dataclass(frozen=True)
class SelectionEnvelope:
    primary: SelectedImage
    logo: SelectedImage | None


@dataclass
class SelectionContext:
    primary: SelectedImage
    logo: SelectedImage | None
    profile: RenderProfile
    output_format: str
    effective_treatment: str | None = None
    effective_logo_sha256: str | None = None


class SelectionError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def renderer_identity() -> tuple[str, str] | None:
    upstream = _cfg.UPSTREAM_REVISION.strip().lower()
    renderer = _cfg.RENDERER_REVISION.strip().lower()
    if (
        not _REVISION_RE.fullmatch(upstream)
        or not _REVISION_RE.fullmatch(renderer)
        or upstream == renderer
    ):
        return None
    return upstream, renderer


def access_key_matches(candidate: str) -> bool:
    if not _cfg.ACCESS_KEY:
        return False
    discovery_key = effective_discovery_key()
    if discovery_key and hmac.compare_digest(candidate, discovery_key):
        return False
    try:
        return hmac.compare_digest(
            candidate.encode("utf-8"),
            _cfg.ACCESS_KEY.encode("utf-8"),
        )
    except UnicodeEncodeError:
        return False


async def read_bounded_body(
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


async def read_selection_body(request: Request) -> bytes:
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise SelectionError(
            415,
            "invalid_content_type",
            "Content-Type must be application/json",
        )
    try:
        return await read_bounded_body(
            request,
            max_bytes=_cfg.SELECTION_MAX_BYTES,
            empty_message="Selection body is empty",
            too_large_message="Selection body is too large",
        )
    except HTTPException as exc:
        code = "body_too_large" if exc.status_code == 413 else "invalid_body"
        raise SelectionError(exc.status_code, code, str(exc.detail)) from exc


def decode_selected_image(body: bytes, content_type: str) -> SelectedImage:
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
    return SelectedImage(
        image=image,
        sha256=hashlib.sha256(body).hexdigest(),
        decoded_format=decoded_format,
    )


def _reject_duplicate_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SelectionError(400, "duplicate_field", f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def decode_selection_envelope(body: bytes) -> SelectionEnvelope:
    try:
        envelope = json.loads(
            body,
            object_pairs_hook=_reject_duplicate_fields,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                SelectionError(
                    400,
                    "invalid_schema",
                    "Selection numbers must be finite",
                )
            ),
        )
    except SelectionError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SelectionError(400, "invalid_json", "Malformed JSON body") from exc
    if not isinstance(envelope, dict) or set(envelope) != {
        "schema_version",
        "primary",
        "logo",
    }:
        raise SelectionError(
            400,
            "invalid_schema",
            "Selection body must contain exactly schema_version, primary, and logo",
        )
    if type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1:
        raise SelectionError(400, "unsupported_schema", "schema_version must be 1")
    primary = _decode_selection_part(envelope["primary"], "primary")
    logo_value = envelope["logo"]
    logo = None if logo_value is None else _decode_selection_part(logo_value, "logo")
    if logo is not None and not _logo_has_visible_pixels(logo.image):
        raise SelectionError(
            422,
            "selected_logo_empty",
            "Selected Logo has no meaningful visible pixels",
        )
    return SelectionEnvelope(primary=primary, logo=logo)


def _decode_selection_part(value, field_name: str) -> SelectedImage:
    if not isinstance(value, dict) or set(value) != {
        "content_type",
        "sha256",
        "data",
    }:
        raise SelectionError(
            400,
            "invalid_schema",
            f"{field_name} must contain exactly content_type, sha256, and data",
        )
    content_type = value["content_type"]
    expected_sha256 = value["sha256"]
    encoded = value["data"]
    if not all(
        isinstance(item, str)
        for item in (content_type, expected_sha256, encoded)
    ):
        raise SelectionError(
            400,
            "invalid_schema",
            f"{field_name} fields must be strings",
        )
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise SelectionError(
            400,
            "invalid_sha256",
            f"{field_name}.sha256 must be 64 lowercase hexadecimal characters",
        )
    max_encoded = 4 * ((_cfg.SELECTED_MAX_BYTES + 2) // 3)
    if len(encoded) > max_encoded:
        raise SelectionError(413, "image_too_large", f"{field_name} image is too large")
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
        raise SelectionError(
            400,
            "invalid_base64",
            f"{field_name}.data is not strict base64",
        ) from exc
    if not image_bytes:
        raise SelectionError(400, "empty_image", f"{field_name} image is empty")
    if len(image_bytes) > _cfg.SELECTED_MAX_BYTES:
        raise SelectionError(413, "image_too_large", f"{field_name} image is too large")
    actual_sha256 = hashlib.sha256(image_bytes).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise SelectionError(
            400,
            "image_hash_mismatch",
            f"{field_name} SHA-256 does not match decoded bytes",
        )
    try:
        return decode_selected_image(image_bytes, content_type)
    except HTTPException as exc:
        raise SelectionError(
            exc.status_code,
            "invalid_image",
            f"{field_name}: {exc.detail}",
        ) from exc


def _logo_has_visible_pixels(image: Image.Image) -> bool:
    alpha = np.asarray(image.getchannel("A"))
    visible = int(np.count_nonzero(alpha > 32))
    return visible >= max(4, image.width * image.height // 100_000)


def encode_rendered_image(image: Image.Image, output_format: str) -> bytes:
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


def image_response(content: bytes, output_format: str) -> Response:
    _, media_type = _OUTPUT_FORMATS[output_format]
    digest = hashlib.sha256(content).digest()
    response = Response(content=content, media_type=media_type)
    response.headers["Digest"] = (
        f"sha-256={base64.b64encode(digest).decode('ascii')}"
    )
    response.headers["X-Image-SHA256"] = digest.hex()
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def effective_logo_sha256(image: Image.Image) -> str:
    rgba = image.convert("RGBA")
    size = rgba.width.to_bytes(4, "big") + rgba.height.to_bytes(4, "big")
    return hashlib.sha256(b"RGBA\0" + size + rgba.tobytes()).hexdigest()


def output_format_supported(value: str) -> bool:
    return value.lower() in _OUTPUT_FORMATS
