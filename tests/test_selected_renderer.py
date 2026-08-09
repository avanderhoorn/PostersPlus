import base64
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException
from PIL import Image
from starlette.requests import Request
from starlette.responses import Response

import main
from render_profile import RenderProfile, load_render_profile
import text_detect


def _image_bytes(image_format: str, size=(4, 3), color=(12, 34, 56)) -> bytes:
    image = Image.new("RGB", size, color)
    output = io.BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


def _request(body: bytes, content_type: str, query: str = "") -> Request:
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    headers = [
        (b"content-type", content_type.encode("ascii")),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"x-jellyfin-artwork-key", b"secret"),
    ]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/render/selected",
            "raw_path": b"/render/selected",
            "query_string": query.encode("ascii"),
            "headers": headers,
            "client": ("test", 1),
            "server": ("test", 80),
        },
        receive,
    )


class SelectedImageValidationTests(unittest.IsolatedAsyncioTestCase):
    def test_accepts_jpeg_png_and_webp_and_hashes_exact_body(self):
        for media_type, image_format in (
            ("image/jpeg", "JPEG"),
            ("image/png", "PNG"),
            ("image/webp", "WEBP"),
        ):
            with self.subTest(media_type=media_type):
                body = _image_bytes(image_format)
                selected = main._decode_selected_image(body, media_type)
                self.assertEqual(selected.decoded_format, image_format)
                self.assertEqual(selected.sha256, hashlib.sha256(body).hexdigest())
                self.assertEqual(selected.image.size, (4, 3))

    def test_rejects_mime_mismatch(self):
        with self.assertRaises(HTTPException) as raised:
            main._decode_selected_image(_image_bytes("PNG"), "image/jpeg")
        self.assertEqual(raised.exception.status_code, 415)

    def test_rejects_unsupported_content_type(self):
        with self.assertRaises(HTTPException) as raised:
            main._decode_selected_image(_image_bytes("PNG"), "image/gif")
        self.assertEqual(raised.exception.status_code, 415)

    def test_rejects_malformed_image(self):
        with self.assertRaises(HTTPException) as raised:
            main._decode_selected_image(b"not an image", "image/png")
        self.assertEqual(raised.exception.status_code, 400)

    def test_rejects_dimension_and_pixel_limits(self):
        body = _image_bytes("PNG", size=(4, 3))
        with patch.object(main._cfg, "SELECTED_MAX_WIDTH", 3):
            with self.assertRaises(HTTPException) as raised:
                main._decode_selected_image(body, "image/png")
        self.assertEqual(raised.exception.status_code, 413)

        with (
            patch.object(main._cfg, "SELECTED_MAX_WIDTH", 4),
            patch.object(main._cfg, "SELECTED_MAX_HEIGHT", 3),
            patch.object(main._cfg, "SELECTED_MAX_PIXELS", 11),
        ):
            with self.assertRaises(HTTPException) as raised:
                main._decode_selected_image(body, "image/png")
        self.assertEqual(raised.exception.status_code, 413)

    def test_rejects_pillow_decompression_bomb(self):
        body = _image_bytes("PNG", size=(4, 3))
        with patch.object(Image, "MAX_IMAGE_PIXELS", 1):
            with self.assertRaises(HTTPException) as raised:
                main._decode_selected_image(body, "image/png")
        self.assertEqual(raised.exception.status_code, 413)

    async def test_rejects_encoded_body_over_limit(self):
        request = _request(b"12345", "image/png")
        with patch.object(main._cfg, "SELECTED_MAX_BYTES", 4):
            with self.assertRaises(HTTPException) as raised:
                await main._read_selected_body(request)
        self.assertEqual(raised.exception.status_code, 413)

    async def test_exact_body_becomes_shared_renderer_base(self):
        body = _image_bytes("PNG", color=(8, 20, 40))
        query = (
            "profile=homestack-default&type=movie&tmdb_id=123"
            "&imdb_id=tt1234567&quality=4K"
        )
        request = _request(body, "image/png", query)
        profile = RenderProfile(
            name="homestack-default",
            defaults={"primary_client": "jellyfin"},
            digest="profile-digest",
        )
        rendered = Response(content=b"rendered", media_type="image/jpeg")
        render_mock = AsyncMock(return_value=rendered)
        with (
            patch.object(main._cfg, "ACCESS_KEY", "secret"),
            patch.object(main._cfg, "SOURCE_REVISION", "revision-123"),
            patch.object(main, "_render_profile", profile),
            patch.object(main, "_render_poster", render_mock),
        ):
            response = await main.render_selected(
                request=request,
                profile="homestack-default",
                tmdb_id="123",
                imdb_id="tt1234567",
                type="movie",
                quality="4K",
            )

        self.assertIs(response, rendered)
        selected = render_mock.await_args.kwargs["selected_image"]
        self.assertEqual(selected.sha256, hashlib.sha256(body).hexdigest())
        self.assertEqual(selected.image.getpixel((0, 0)), (8, 20, 40, 255))
        self.assertIs(render_mock.await_args.kwargs["profile"], profile)
        self.assertEqual(
            response.headers["x-render-profile-sha256"],
            "profile-digest",
        )
        self.assertEqual(response.headers["x-renderer-revision"], "revision-123")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["pragma"], "no-cache")

    async def test_rejects_source_url_query(self):
        body = _image_bytes("PNG")
        query = (
            "profile=homestack-default&type=movie&tmdb_id=123"
            "&imdb_id=tt1234567&source_url=https%3A%2F%2Fexample.invalid%2Fx"
        )
        request = _request(body, "image/png", query)
        profile = RenderProfile("homestack-default", {}, "digest")
        with (
            patch.object(main._cfg, "ACCESS_KEY", "secret"),
            patch.object(main, "_render_profile", profile),
        ):
            with self.assertRaises(HTTPException) as raised:
                await main.render_selected(
                    request=request,
                    profile="homestack-default",
                    tmdb_id="123",
                    imdb_id="tt1234567",
                )
        self.assertEqual(raised.exception.status_code, 400)


class ProfileTests(unittest.TestCase):
    def _write_profile(self, content: str) -> str:
        directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "profile.yml"
        path.write_text(content, encoding="utf-8")
        return str(path)

    def test_profile_digest_is_deterministic_and_defaults_use_request_config(self):
        first = self._write_profile(
            "name: homestack-default\n"
            "defaults:\n"
            "  badge_display_mode: 2\n"
            "  primary_client: jellyfin\n"
        )
        second = self._write_profile(
            "defaults: {primary_client: jellyfin, badge_display_mode: 2}\n"
            "name: homestack-default\n"
        )
        profile_a = load_render_profile(first, set(main._PROFILE_DEFAULT_FIELDS))
        profile_b = load_render_profile(second, set(main._PROFILE_DEFAULT_FIELDS))
        self.assertEqual(profile_a.digest, profile_b.digest)
        config = main.build_request_config(profile_a.defaults)
        self.assertEqual(config.badge_display_mode, 2)
        self.assertEqual(config.bar_bottom_inset, 0.0)
        self.assertEqual(config.sash_badge_inset, 0.0)

    def test_profile_rejects_non_render_fields(self):
        path = self._write_profile(
            "name: homestack-default\n"
            "defaults:\n"
            "  access_key: do-not-accept\n"
        )
        with self.assertRaisesRegex(ValueError, "unsupported"):
            load_render_profile(path, set(main._PROFILE_DEFAULT_FIELDS))

    def test_profile_rejects_non_finite_numbers(self):
        for value in (
            ".nan", ".inf", "-.inf", '"nan"', '"+nan"', '"-nan"', "1e309",
        ):
            with self.subTest(value=value):
                path = self._write_profile(
                    "name: homestack-default\n"
                    "defaults:\n"
                    f"  top_gradient_opacity: {value}\n"
                )
                with self.assertRaisesRegex(ValueError, "finite"):
                    load_render_profile(path, set(main._PROFILE_DEFAULT_FIELDS))

    def test_household_profile_uses_current_render_fields(self):
        path = Path(__file__).parent / "fixtures" / "homestack-profile.yml"
        profile = load_render_profile(str(path), set(main._PROFILE_DEFAULT_FIELDS))
        config = main.build_request_config(profile.defaults)

        self.assertEqual(profile.name, "homestack-default")
        self.assertTrue(config.hide_genre)
        self.assertTrue(config.minimalist_score_out_of_10)
        self.assertTrue(config.frost_reference)
        self.assertEqual(config.movie_weights["letterboxd"], 0.98)
        self.assertEqual(config.tv_weights["tomatoes"], 0.20)


class TextDetectionModelTests(unittest.TestCase):
    @unittest.skipUnless(text_detect._HAS_RAPIDOCR, "RapidOCR is not installed")
    def test_installed_bundled_models_resolve(self):
        for model_path in (
            text_detect._CLS_MODEL_PATH,
            text_detect._REC_MODEL_PATH,
        ):
            with self.subTest(model_path=model_path):
                self.assertTrue(model_path)
                self.assertTrue(Path(model_path).is_file())

    def test_bundled_model_discovery_tolerates_versioned_names(self):
        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory) / "ch_ppocr_mobile_v2.0_cls_mobile.onnx"
            expected.touch()
            (Path(directory) / "PP-OCRv6_rec_small.onnx").touch()

            self.assertEqual(
                text_detect._find_bundled_model(Path(directory), "cls"),
                str(expected),
            )

    def test_model_readiness_requires_a_loaded_session_pool(self):
        with patch.object(text_detect, "_ocr_pool", None):
            self.assertFalse(text_detect.text_detection_ready())
        with patch.object(text_detect, "_ocr_pool", object()):
            self.assertTrue(text_detect.text_detection_ready())


class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def _get(self, path: str, headers=None):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.get(path, headers=headers)

    async def test_health_alone_is_anonymous(self):
        with (
            patch.object(main._cfg, "ACCESS_KEY", "secret"),
            patch.object(main._cfg, "HEADER_ONLY_AUTH", True),
        ):
            health = await self._get("/health")
            ready = await self._get("/ready")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(ready.status_code, 403)

    async def test_static_assets_remain_available_without_credentials(self):
        with (
            patch.object(main._cfg, "ACCESS_KEY", "secret"),
            patch.object(main._cfg, "HEADER_ONLY_AUTH", True),
        ):
            response = await self._get("/static/favicon.png")
        self.assertEqual(response.status_code, 200)

    async def test_header_auth_and_legacy_query_compatibility(self):
        profile = RenderProfile("homestack-default", {}, "digest")
        with (
            patch.object(main._cfg, "ACCESS_KEY", "secret"),
            patch.object(main._cfg, "HEADER_ONLY_AUTH", False),
            patch.object(main._cfg, "SOURCE_REVISION", "abc123"),
            patch.object(main, "_render_profile", profile),
            patch.object(main, "_text_detector_ready", return_value=True),
        ):
            header = await self._get(
                "/ready",
                headers={"X-Jellyfin-Artwork-Key": "secret"},
            )
            query = await self._get("/ready?access_key=secret")
        self.assertEqual(header.status_code, 200)
        self.assertEqual(query.status_code, 200)
        self.assertEqual(header.json()["renderer_revision"], "abc123")
        self.assertEqual(header.json()["profile"]["digest"], "digest")

    async def test_ready_rejects_failed_text_detector(self):
        profile = RenderProfile("homestack-default", {}, "digest")
        with (
            patch.object(main._cfg, "ACCESS_KEY", "secret"),
            patch.object(main._cfg, "HEADER_ONLY_AUTH", True),
            patch.object(main._cfg, "TEXTLESS_TEXT_DETECTION", True),
            patch.object(main, "_render_profile", profile),
            patch.object(main, "_text_detector_ready", return_value=False),
        ):
            response = await self._get(
                "/ready",
                headers={"X-Jellyfin-Artwork-Key": "secret"},
            )
        self.assertEqual(response.status_code, 503)

    async def test_header_only_mode_rejects_query_key(self):
        with (
            patch.object(main._cfg, "ACCESS_KEY", "secret"),
            patch.object(main._cfg, "HEADER_ONLY_AUTH", True),
        ):
            response = await self._get("/ready?access_key=secret")
        self.assertEqual(response.status_code, 403)

    async def test_non_ascii_key_is_rejected_without_server_error(self):
        with (
            patch.object(main._cfg, "ACCESS_KEY", "secret"),
            patch.object(main._cfg, "HEADER_ONLY_AUTH", False),
        ):
            response = await self._get("/ready?access_key=%C3%A9")
        self.assertEqual(response.status_code, 403)

    async def test_selected_endpoint_never_accepts_query_key(self):
        transport = httpx.ASGITransport(app=main.app)
        path = (
            "/render/selected?access_key=secret&profile=homestack-default"
            "&type=movie&tmdb_id=123&imdb_id=tt1234567"
        )
        with (
            patch.object(main._cfg, "ACCESS_KEY", "secret"),
            patch.object(main._cfg, "HEADER_ONLY_AUTH", False),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                response = await client.post(
                    path,
                    content=_image_bytes("PNG"),
                    headers={"Content-Type": "image/png"},
                )
        self.assertEqual(response.status_code, 403)


class ResponseAndCacheIdentityTests(unittest.TestCase):
    def test_normal_poster_uses_configured_output_format(self):
        with patch.object(main._cfg, "IMAGE_FORMAT", "webp"):
            self.assertEqual(main._resolve_output_format(None), "webp")
        self.assertEqual(main._resolve_output_format("jpeg"), "jpeg")

    def test_output_formats_use_their_own_quality_setting(self):
        image = Image.new("RGBA", (4, 3), (10, 20, 30, 128))
        with (
            patch.object(main._cfg, "JPEG_QUALITY", 91),
            patch.object(main._cfg, "WEBP_QUALITY", 74),
            patch.object(Image.Image, "save") as save,
        ):
            main._encode_rendered_image(image, "jpeg")
            self.assertEqual(save.call_args.kwargs["quality"], 91)
            main._encode_rendered_image(image, "webp")
            self.assertEqual(save.call_args.kwargs["quality"], 74)

    def test_response_mime_and_digest_headers(self):
        image = Image.new("RGBA", (4, 3), (10, 20, 30, 128))
        for output_format, pil_format, mime in (
            ("jpeg", "JPEG", "image/jpeg"),
            ("png", "PNG", "image/png"),
            ("webp", "WEBP", "image/webp"),
        ):
            with self.subTest(output_format=output_format):
                content = main._encode_rendered_image(image, output_format)
                with Image.open(io.BytesIO(content)) as decoded:
                    self.assertEqual(decoded.format, pil_format)
                expected_hex = hashlib.sha256(content).hexdigest()
                expected_b64 = base64.b64encode(
                    hashlib.sha256(content).digest()
                ).decode()
                response = main._image_response(content, output_format)
                self.assertEqual(response.media_type, mime)
                self.assertEqual(response.headers["x-image-sha256"], expected_hex)
                self.assertEqual(
                    response.headers["digest"],
                    f"sha-256={expected_b64}",
                )

    def test_selected_cache_identity_includes_all_context(self):
        base = {
            "base_identity": "settings",
            "selected_sha256": "base-a",
            "profile_digest": "profile-a",
            "renderer_revision": "revision-a",
            "imdb_id": "tt1",
            "tmdb_id": "1",
            "media_type": "movie",
            "quality": "4K",
            "season": 1,
            "episode": 1,
            "output_format": "jpeg",
        }
        original = main._selected_cache_identity(**base)
        for field, replacement in (
            ("selected_sha256", "base-b"),
            ("profile_digest", "profile-b"),
            ("renderer_revision", "revision-b"),
            ("imdb_id", "tt2"),
            ("tmdb_id", "2"),
            ("media_type", "series"),
            ("quality", "1080P"),
            ("season", 2),
            ("episode", 2),
            ("output_format", "png"),
        ):
            changed = dict(base)
            changed[field] = replacement
            self.assertNotEqual(
                original,
                main._selected_cache_identity(**changed),
                field,
            )

    def test_selected_responses_disable_client_conditional_caching(self):
        self.assertIsNone(main._client_cache_key("selected:key", object()))
        self.assertEqual(main._client_cache_key("poster:key", None), "poster:key")


if __name__ == "__main__":
    unittest.main()
