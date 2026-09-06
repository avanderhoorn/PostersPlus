"""Strict schema-2 render profile loading for the private production adapter."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


PROFILE_SCHEMA_VERSION = 2
_MAX_PROFILE_BYTES = 64 * 1024
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_TOP_LEVEL_FIELDS = frozenset({"schema_version", "name", "public_query"})


class RenderProfileError(ValueError):
    pass


@dataclass(frozen=True)
class RenderProfile:
    schema_version: int
    name: str
    public_query: dict[str, str]
    digest: str


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _query_value(name: str, value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and not math.isfinite(value):
        raise RenderProfileError(f"profile public_query {name!r} must be finite")
    if isinstance(value, str):
        normalized = value.strip().lower()
        try:
            parsed_number = float(normalized)
        except ValueError:
            parsed_number = None
        if parsed_number is not None and not math.isfinite(parsed_number):
            raise RenderProfileError(
                f"profile public_query {name!r} must be finite"
            )
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if name == "sash_priority" and isinstance(value, list):
        if not all(isinstance(item, str) for item in value):
            raise RenderProfileError(
                "profile sash_priority must contain only strings"
            )
        return ",".join(value)
    if name in ("movie_weights", "tv_weights") and isinstance(value, dict):
        if not all(
            isinstance(key, str)
            and isinstance(weight, (int, float))
            and not isinstance(weight, bool)
            and (not isinstance(weight, float) or math.isfinite(weight))
            for key, weight in value.items()
        ):
            raise RenderProfileError(
                f"profile {name} must map source names to finite numbers"
            )
        return ",".join(f"{key}:{value[key]}" for key in sorted(value))
    raise RenderProfileError(
        f"profile public_query {name!r} has an unsupported value"
    )


def load_render_profile(
    path: str,
    allowed_query: set[str] | frozenset[str],
    *,
    key_hints: Mapping[str, str] | None = None,
) -> RenderProfile:
    raw = Path(path).read_bytes()
    if len(raw) > _MAX_PROFILE_BYTES:
        raise RenderProfileError("render profile exceeds 64 KiB")

    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RenderProfileError("render profile is not valid YAML") from exc

    if not isinstance(document, dict) or set(document) != set(_TOP_LEVEL_FIELDS):
        expected = ", ".join(sorted(_TOP_LEVEL_FIELDS))
        raise RenderProfileError(
            f"render profile must contain exactly {expected}"
        )
    schema_version = document["schema_version"]
    if type(schema_version) is not int or schema_version != PROFILE_SCHEMA_VERSION:
        raise RenderProfileError(
            f"render profile schema_version must be {PROFILE_SCHEMA_VERSION}"
        )
    name = document["name"]
    if not isinstance(name, str) or not _PROFILE_NAME_RE.fullmatch(name):
        raise RenderProfileError("render profile name must be a lowercase slug")
    public_query = document["public_query"]
    if not isinstance(public_query, dict):
        raise RenderProfileError("render profile public_query must be a mapping")
    if not all(isinstance(key, str) for key in public_query):
        raise RenderProfileError("render profile public_query keys must be strings")

    unknown = sorted(set(public_query) - set(allowed_query))
    if unknown:
        hints = key_hints or {}
        described = ", ".join(
            f"{key} ({hints[key]})" if key in hints else key for key in unknown
        )
        raise RenderProfileError(
            f"unsupported render profile public_query keys: {described}"
        )

    canonical_query = {
        key: _query_value(key, value)
        for key, value in sorted(public_query.items())
    }
    canonical_document = {
        "name": name,
        "public_query": canonical_query,
        "schema_version": schema_version,
    }
    return RenderProfile(
        schema_version=schema_version,
        name=name,
        public_query=canonical_query,
        digest=hashlib.sha256(_canonical_bytes(canonical_document)).hexdigest(),
    )
