import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

import yaml


_MAX_PROFILE_BYTES = 64 * 1024
_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass(frozen=True)
class RenderProfile:
    name: str
    defaults: dict[str, str]
    digest: str


def _query_value(name: str, value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"profile default {name!r} must be finite")
    if isinstance(value, str):
        normalized = value.strip().lower()
        try:
            parsed_number = float(normalized)
        except ValueError:
            parsed_number = None
        if parsed_number is not None and not math.isfinite(parsed_number):
            raise ValueError(f"profile default {name!r} must be finite")
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if name == "sash_priority" and isinstance(value, list):
        if not all(isinstance(item, str) for item in value):
            raise ValueError("profile sash_priority must contain only strings")
        return ",".join(value)
    if name in ("movie_weights", "tv_weights") and isinstance(value, dict):
        if not all(
            isinstance(key, str)
            and isinstance(weight, (int, float))
            and not isinstance(weight, bool)
            and (not isinstance(weight, float) or math.isfinite(weight))
            for key, weight in value.items()
        ):
            raise ValueError(f"profile {name} must map source names to finite numbers")
        return ",".join(f"{key}:{value[key]}" for key in sorted(value))
    raise ValueError(f"profile default {name!r} has an unsupported value")


def load_render_profile(path: str, allowed_defaults: set[str]) -> RenderProfile:
    profile_path = Path(path)
    raw = profile_path.read_bytes()
    if len(raw) > _MAX_PROFILE_BYTES:
        raise ValueError("render profile exceeds 64 KiB")

    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError("render profile is not valid YAML") from exc

    if not isinstance(document, dict) or set(document) != {"name", "defaults"}:
        raise ValueError("render profile must contain exactly name and defaults")
    name = document["name"]
    defaults = document["defaults"]
    if not isinstance(name, str) or not _PROFILE_NAME_RE.fullmatch(name):
        raise ValueError("render profile name must be a lowercase slug")
    if not isinstance(defaults, dict):
        raise ValueError("render profile defaults must be a mapping")
    if not all(isinstance(key, str) for key in defaults):
        raise ValueError("render profile default names must be strings")

    unknown = sorted(set(defaults) - allowed_defaults)
    if unknown:
        raise ValueError(f"unsupported render profile defaults: {', '.join(unknown)}")

    query_defaults = {
        key: _query_value(key, value)
        for key, value in sorted(defaults.items())
    }
    canonical = json.dumps(
        {"defaults": query_defaults, "name": name},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return RenderProfile(
        name=name,
        defaults=query_defaults,
        digest=hashlib.sha256(canonical).hexdigest(),
    )
