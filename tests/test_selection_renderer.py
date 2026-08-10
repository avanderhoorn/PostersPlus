import base64
import hashlib
import io
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image
from starlette.requests import Request
from starlette.responses import Response

import main
from render_profile import RenderProfile


def _image_bytes(
    image_format: str = "PNG",
    *,
    size=(40, 20),
    color=(12, 34, 56, 255),
) -> bytes:
    image = Image.new("RGBA", size, color)
    output = io.BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


def _part(body: bytes, content_type: str = "image/png") -> dict[str, str]:
    return {
        "content_type": content_type,
        "sha256": hashlib.sha256(body).hexdigest(),
        "data": base64.b64encode(body).decode("ascii"),
    }


def _envelope(primary: bytes, logo: bytes | None) -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "primary": _part(primary),
            "logo": None if logo is None else _part(logo),
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _request(body: bytes, content_type: str = "application/json") -> Request:
    delivered = False
    query = (
        "profile=homestack-default&type=movie&tmdb_id=123"
        "&imdb_id=tt1234567&quality=4K"
    )

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/render/selection",
            "raw_path": b"/render/selection",
            "query_string": query.encode("ascii"),
            "headers": [
                (b"content-type", content_type.encode("ascii")),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"x-jellyfin-artwork-key", b"secret"),
            ],
            "client": ("test", 1),
            "server": ("test", 80),
        },
        receive,
    )


class SelectionEnvelopeTests(unittest.TestCase):
    def test_decodes_exact_primary_and_logo(self):
        primary = _image_bytes(color=(20, 30, 40, 255))
        logo = _image_bytes(size=(12, 5), color=(220, 30, 40, 255))

        selection = main._decode_selection_envelope(_envelope(primary, logo))

        self.assertEqual(selection.primary.sha256, hashlib.sha256(primary).hexdigest())
        self.assertEqual(selection.logo.sha256, hashlib.sha256(logo).hexdigest())
        self.assertEqual(selection.primary.image.getpixel((0, 0)), (20, 30, 40, 255))
        self.assertEqual(selection.logo.image.getpixel((0, 0)), (220, 30, 40, 255))

    def test_accepts_explicit_absent_logo(self):
        selection = main._decode_selection_envelope(
            _envelope(_image_bytes(), None)
        )
        self.assertIsNone(selection.logo)

    def test_rejects_duplicate_and_unknown_fields(self):
        primary = json.dumps(_part(_image_bytes()), separators=(",", ":"))
        duplicate = (
            '{"schema_version":1,"schema_version":1,'
            f'"primary":{primary},"logo":null}}'
        ).encode()
        with self.assertRaises(main._SelectionError) as raised:
            main._decode_selection_envelope(duplicate)
        self.assertEqual(raised.exception.code, "duplicate_field")

        value = json.loads(_envelope(_image_bytes(), None))
        value["unexpected"] = True
        with self.assertRaises(main._SelectionError) as raised:
            main._decode_selection_envelope(json.dumps(value).encode())
        self.assertEqual(raised.exception.code, "invalid_schema")

    def test_rejects_hash_base64_mime_and_transparent_logo(self):
        primary = _image_bytes()
        value = json.loads(_envelope(primary, None))
        value["primary"]["sha256"] = "0" * 64
        with self.assertRaises(main._SelectionError) as raised:
            main._decode_selection_envelope(json.dumps(value).encode())
        self.assertEqual(raised.exception.code, "image_hash_mismatch")

        value = json.loads(_envelope(primary, None))
        value["primary"]["data"] = "***"
        with self.assertRaises(main._SelectionError) as raised:
            main._decode_selection_envelope(json.dumps(value).encode())
        self.assertEqual(raised.exception.code, "invalid_base64")

        value = json.loads(_envelope(primary, None))
        value["primary"]["content_type"] = "image/jpeg"
        with self.assertRaises(main._SelectionError) as raised:
            main._decode_selection_envelope(json.dumps(value).encode())
        self.assertEqual(raised.exception.code, "invalid_image")

        transparent = _image_bytes(color=(0, 0, 0, 0))
        with self.assertRaises(main._SelectionError) as raised:
            main._decode_selection_envelope(_envelope(primary, transparent))
        self.assertEqual(raised.exception.code, "selected_logo_empty")

    def test_selected_logo_changes_cache_identity_and_pixels(self):
        base = {
            "base_identity": "settings",
            "selected_sha256": "primary",
            "profile_digest": "profile",
            "renderer_revision": "revision",
            "imdb_id": "tt1",
            "tmdb_id": "1",
            "media_type": "movie",
            "quality": "4K",
            "season": 1,
            "episode": 1,
            "output_format": "jpeg",
        }
        red_identity = main._selected_cache_identity(
            **base,
            selected_logo_sha256="red",
        )
        blue_identity = main._selected_cache_identity(
            **base,
            selected_logo_sha256="blue",
        )
        self.assertNotEqual(red_identity, blue_identity)

        canvas = Image.new("RGBA", (500, 750), (10, 20, 30, 255))
        red = Image.new("RGBA", (120, 40), (255, 0, 0, 255))
        blue = Image.new("RGBA", (120, 40), (0, 0, 255, 255))
        cfg = main.RequestConfig(
            rating_display_mode=0,
            badge_display_mode=0,
            top_gradient="off",
        )
        red_result = main.build_poster(
            canvas.copy(),
            "N/A",
            "Drama",
            cfg,
            logo=red,
        )
        blue_result = main.build_poster(
            canvas.copy(),
            "N/A",
            "Drama",
            cfg,
            logo=blue,
        )
        self.assertNotEqual(red_result.tobytes(), blue_result.tobytes())


class SelectionRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_passes_exact_pair_and_returns_provenance(self):
        primary = _image_bytes(color=(20, 30, 40, 255))
        logo = _image_bytes(size=(12, 5), color=(220, 30, 40, 255))
        profile = RenderProfile("homestack-default", {}, "profile-digest")
        rendered = Response(content=b"rendered", media_type="image/jpeg")
        render_mock = AsyncMock(return_value=rendered)

        with (
            patch.object(main._cfg, "ACCESS_KEY", "secret"),
            patch.object(main._cfg, "SOURCE_REVISION", "revision-123"),
            patch.object(main, "_render_profile", profile),
            patch.object(main, "_text_detector_ready", return_value=True),
            patch.object(main, "_render_poster", render_mock),
        ):
            response = await main.render_selection(
                request=_request(_envelope(primary, logo)),
                profile="homestack-default",
                tmdb_id="123",
                imdb_id="tt1234567",
                type="movie",
                quality="4K",
            )

        selected_primary = render_mock.await_args.kwargs["selected_image"]
        selected_logo = render_mock.await_args.kwargs["selected_logo"]
        self.assertEqual(selected_primary.sha256, hashlib.sha256(primary).hexdigest())
        self.assertEqual(selected_logo.sha256, hashlib.sha256(logo).hexdigest())
        self.assertEqual(response.headers["x-logo-source"], "selected")
        self.assertEqual(
            response.headers["x-selected-primary-sha256"],
            hashlib.sha256(primary).hexdigest(),
        )
        self.assertEqual(
            response.headers["x-selected-logo-sha256"],
            hashlib.sha256(logo).hexdigest(),
        )
        self.assertEqual(response.headers["x-render-profile-sha256"], "profile-digest")
        self.assertEqual(response.headers["x-renderer-revision"], "revision-123")

    async def test_absent_logo_preserves_provider_fallback(self):
        primary = _image_bytes()
        profile = RenderProfile("homestack-default", {}, "digest")
        render_mock = AsyncMock(
            return_value=Response(content=b"rendered", media_type="image/jpeg")
        )
        with (
            patch.object(main._cfg, "ACCESS_KEY", "secret"),
            patch.object(main, "_render_profile", profile),
            patch.object(main, "_render_poster", render_mock),
        ):
            response = await main.render_selection(
                request=_request(_envelope(primary, None)),
                profile="homestack-default",
                tmdb_id="123",
                imdb_id="tt1234567",
            )
        self.assertIsNone(render_mock.await_args.kwargs["selected_logo"])
        self.assertEqual(response.headers["x-logo-source"], "provider")
        self.assertNotIn("x-selected-logo-sha256", response.headers)

    async def test_route_returns_machine_readable_errors(self):
        primary = _image_bytes()
        profile = RenderProfile("homestack-default", {}, "digest")
        transport = httpx.ASGITransport(app=main.app)
        path = (
            "/render/selection?profile=homestack-default&type=movie"
            "&tmdb_id=123&imdb_id=tt1234567"
        )
        with (
            patch.object(main._cfg, "ACCESS_KEY", "secret"),
            patch.object(main, "_render_profile", profile),
            patch.object(main, "_text_detector_ready", return_value=False),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                response = await client.post(
                    path,
                    content=_envelope(primary, _image_bytes(size=(12, 5))),
                    headers={
                        "Content-Type": "application/json",
                        "X-Jellyfin-Artwork-Key": "secret",
                    },
                )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["code"],
            "text_detection_unavailable",
        )

    async def test_pipeline_preserves_typed_selection_errors(self):
        primary = _image_bytes()
        logo = _image_bytes(size=(12, 5))
        profile = RenderProfile("homestack-default", {}, "digest")
        rejection = main._SelectionError(
            422,
            "selected_primary_contains_text",
            "Selected Primary contains title text",
        )
        transport = httpx.ASGITransport(app=main.app)
        path = (
            "/render/selection?profile=homestack-default&type=movie"
            "&tmdb_id=123&imdb_id=tt1234567"
        )
        with (
            patch.object(main._cfg, "ACCESS_KEY", "secret"),
            patch.object(main._cfg, "SERVER_TMDB_KEY", "tmdb-key"),
            patch.object(main._cfg, "DISABLE_COMPOSITE_CACHE", True),
            patch.object(main, "_render_profile", profile),
            patch.object(main, "_text_detector_ready", return_value=True),
            patch.object(main, "_HTTP_CLIENT", object()),
            patch.object(
                main,
                "_coalesced_fetch_poster_metadata",
                AsyncMock(side_effect=rejection),
            ),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                response = await client.post(
                    path,
                    content=_envelope(primary, logo),
                    headers={
                        "Content-Type": "application/json",
                        "X-Jellyfin-Artwork-Key": "secret",
                    },
                )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.json()["error"]["code"],
            "selected_primary_contains_text",
        )


if __name__ == "__main__":
    unittest.main()
