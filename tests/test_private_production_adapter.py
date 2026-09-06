import base64
from contextlib import ExitStack
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from PIL import Image
from starlette.responses import Response

import artwork_candidates
import main
import text_detect
from private_adapter import SelectionContext, effective_logo_sha256
from render_profile import RenderProfile, RenderProfileError, load_render_profile


UPSTREAM_REVISION = "9d84d388a426c90ad439a27e01941538856fb85e"
RENDERER_REVISION = "a" * 40


def _image_bytes(
    *,
    size: tuple[int, int] = (40, 60),
    color: tuple[int, int, int, int] = (12, 34, 56, 255),
) -> bytes:
    image = Image.new("RGBA", size, color)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _patterned_primary(size: tuple[int, int]) -> bytes:
    width, height = size
    image = Image.new("RGBA", size, (20, 30, 40, 255))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = (
                (x * 17 // max(1, width - 1)) % 256,
                (y * 29 // max(1, height - 1)) % 256,
                ((x + y) * 13 // max(1, width + height - 2)) % 256,
                255,
            )
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _part(body: bytes) -> dict[str, str]:
    return {
        "content_type": "image/png",
        "data": base64.b64encode(body).decode("ascii"),
        "sha256": hashlib.sha256(body).hexdigest(),
    }


def _homestack_envelope(primary: bytes, logo: bytes | None) -> bytes:
    return json.dumps(
        {
            "logo": None if logo is None else _part(logo),
            "primary": _part(primary),
            "schema_version": 1,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _profile() -> RenderProfile:
    return RenderProfile(
        schema_version=2,
        name="homestack-default",
        public_query={"primary_client": "stremio_tv_nuvio"},
        digest="b" * 64,
    )


class ProfileSchemaTests(unittest.TestCase):
    def _write(self, text: str) -> str:
        directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "profile.yml"
        path.write_text(text, encoding="utf-8")
        return str(path)

    def test_schema_two_digest_is_canonical(self):
        first = self._write(
            "schema_version: 2\n"
            "name: homestack-default\n"
            "public_query:\n"
            "  badge_display_mode: 2\n"
            "  primary_client: stremio_tv_nuvio\n"
        )
        second = self._write(
            "public_query: {primary_client: stremio_tv_nuvio, badge_display_mode: 2}\n"
            "name: homestack-default\n"
            "schema_version: 2\n"
        )
        profile_a = load_render_profile(first, main._PROFILE_QUERY_FIELDS)
        profile_b = load_render_profile(second, main._PROFILE_QUERY_FIELDS)
        self.assertEqual(profile_a.digest, profile_b.digest)
        self.assertEqual(
            set(profile_a.__dict__),
            {"schema_version", "name", "public_query", "digest"},
        )

    def test_experiment_and_unsupported_keys_are_forbidden(self):
        experiment = self._write(
            "schema_version: 2\n"
            "name: homestack-default\n"
            "public_query: {}\n"
            "experiment: {adaptive_square: true}\n"
        )
        unsupported = self._write(
            "schema_version: 2\n"
            "name: homestack-default\n"
            "public_query: {access_key: secret}\n"
        )
        with self.assertRaisesRegex(RenderProfileError, "exactly"):
            load_render_profile(experiment, main._PROFILE_QUERY_FIELDS)
        with self.assertRaisesRegex(RenderProfileError, "unsupported"):
            load_render_profile(unsupported, main._PROFILE_QUERY_FIELDS)

    def test_reviewed_homestack_profile_resolves_only_stock_request_fields(self):
        path = Path(__file__).parent / "fixtures" / "homestack-profile-v2.yml"
        profile = load_render_profile(str(path), main._PROFILE_QUERY_FIELDS)
        config = main.build_request_config(profile.public_query)
        self.assertEqual(profile.name, "homestack-default")
        self.assertEqual(config.logo_max_w_ratio, 0.74)
        self.assertEqual(config.logo_max_h_ratio, 0.15)
        self.assertEqual(config.logo_bottom_ratio, 0.10)
        self.assertEqual(config.minimalist_rating_separator, "bullet")
        self.assertEqual(config.movie_weights["imdb"], 0.50)
        self.assertEqual(config.tv_weights["trakt"], 0.80)
        self.assertEqual(config.badge_min_score, 1)
        self.assertEqual(config.shape, "portrait")


class SelectionEnvelopeTests(unittest.TestCase):
    def test_rejects_duplicate_hash_base64_mime_and_transparent_logo(self):
        primary = _image_bytes()
        primary_part = json.dumps(_part(primary), separators=(",", ":"))
        duplicate = (
            '{"schema_version":1,"schema_version":1,'
            f'"primary":{primary_part},"logo":null}}'
        ).encode()
        cases = []

        hash_mismatch = json.loads(_homestack_envelope(primary, None))
        hash_mismatch["primary"]["sha256"] = "0" * 64
        cases.append((json.dumps(hash_mismatch).encode(), "image_hash_mismatch"))

        invalid_base64 = json.loads(_homestack_envelope(primary, None))
        invalid_base64["primary"]["data"] = "***"
        cases.append((json.dumps(invalid_base64).encode(), "invalid_base64"))

        mime_mismatch = json.loads(_homestack_envelope(primary, None))
        mime_mismatch["primary"]["content_type"] = "image/jpeg"
        cases.append((json.dumps(mime_mismatch).encode(), "invalid_image"))

        transparent_logo = _image_bytes(
            size=(80, 20),
            color=(0, 0, 0, 0),
        )
        cases.append(
            (
                _homestack_envelope(primary, transparent_logo),
                "selected_logo_empty",
            )
        )

        with self.assertRaises(main._SelectionError) as raised:
            main._decode_selection_envelope(duplicate)
        self.assertEqual(raised.exception.code, "duplicate_field")
        for body, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(main._SelectionError) as raised:
                    main._decode_selection_envelope(body)
                self.assertEqual(raised.exception.code, code)

    def test_rejects_selected_bytes_over_limit(self):
        primary = _image_bytes(size=(20, 30))
        with patch.object(main._cfg, "SELECTED_MAX_BYTES", len(primary) - 1):
            with self.assertRaises(main._SelectionError) as raised:
                main._decode_selection_envelope(
                    _homestack_envelope(primary, None)
                )
        self.assertEqual(raised.exception.code, "image_too_large")


class ExactCallerContractTests(unittest.IsolatedAsyncioTestCase):
    async def _render(
        self,
        *,
        primary: bytes,
        logo: bytes | None,
        treatment: str,
        effective_hash: str | None,
        include_imdb: bool = True,
        tmdb_id: str = "550",
        imdb_id: str = "tt0137523",
    ) -> httpx.Response:
        async def fake_get_poster(**kwargs):
            context = kwargs["request"].state.selection_context
            context.effective_treatment = treatment
            context.effective_logo_sha256 = effective_hash
            return Response(content=b"rendered", media_type="image/jpeg")

        query = (
            f"profile=homestack-default&tmdb_id={tmdb_id}"
            "&type=movie&quality=4K"
            + (f"&imdb_id={imdb_id}" if include_imdb else "")
        )
        transport = httpx.ASGITransport(app=main.app)
        with (
            patch.object(main._cfg, "ACCESS_KEY", "render-secret"),
            patch.object(main._cfg, "RENDERER_REVISION", RENDERER_REVISION),
            patch.object(main, "_render_profile", _profile()),
            patch.object(main, "_text_detector_ready", return_value=True),
            patch.object(main, "get_poster", AsyncMock(side_effect=fake_get_poster)),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://renderer",
            ) as client:
                return await client.post(
                    f"/render/selection?{query}",
                    content=_homestack_envelope(primary, logo),
                    headers={
                        "Content-Type": "application/json",
                        "X-Jellyfin-Artwork-Key": "render-secret",
                    },
                )

    async def test_exact_homestack_selected_logo_request_and_provenance(self):
        primary = _image_bytes()
        logo = _image_bytes(size=(80, 20), color=(220, 30, 40, 255))
        logo_hash = hashlib.sha256(logo).hexdigest()
        response = await self._render(
            primary=primary,
            logo=logo,
            treatment="selected_logo",
            effective_hash=logo_hash,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-logo-source"], "selected")
        self.assertEqual(response.headers["x-logo-treatment"], "selected_logo")
        self.assertEqual(response.headers["x-selected-logo-sha256"], logo_hash)
        self.assertEqual(response.headers["x-effective-logo-sha256"], logo_hash)
        self.assertEqual(
            response.headers["x-selected-primary-sha256"],
            hashlib.sha256(primary).hexdigest(),
        )
        self.assertEqual(
            response.headers["x-upstream-revision"],
            UPSTREAM_REVISION,
        )
        self.assertEqual(
            response.headers["x-renderer-revision"],
            RENDERER_REVISION,
        )

    async def test_no_selected_logo_reports_provider_logo_or_title_treatment(self):
        primary = _image_bytes()
        provider_hash = "c" * 64
        provider = await self._render(
            primary=primary,
            logo=None,
            treatment="provider_logo",
            effective_hash=provider_hash,
        )
        self.assertEqual(provider.headers["x-logo-source"], "provider")
        self.assertEqual(provider.headers["x-logo-treatment"], "provider_logo")
        self.assertEqual(
            provider.headers["x-effective-logo-sha256"],
            provider_hash,
        )
        self.assertNotIn("x-selected-logo-sha256", provider.headers)

        title = await self._render(
            primary=primary,
            logo=None,
            treatment="title_treatment",
            effective_hash=None,
        )
        self.assertEqual(title.headers["x-logo-source"], "provider")
        self.assertEqual(title.headers["x-logo-treatment"], "title_treatment")
        self.assertNotIn("x-effective-logo-sha256", title.headers)

    async def test_imdb_is_optional_for_upstream_compatible_rendering(self):
        response = await self._render(
            primary=_image_bytes(),
            logo=None,
            treatment="title_treatment",
            effective_hash=None,
            include_imdb=False,
        )
        self.assertEqual(response.status_code, 200)

    def test_current_plugin_schema_two_shapes_are_strict(self):
        request = artwork_candidates.decode_request_body(
            json.dumps(
                {
                    "image_type": "Logo",
                    "imdb_id": "tt0137523",
                    "schema_version": 2,
                    "tmdb_id": "550",
                    "type": "movie",
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )
        self.assertEqual(request.image_type, "Logo")
        response = artwork_candidates._build_response(
            request,
            {"tmdb": "ready", "fanart": "failed"},
            [],
        )
        self.assertEqual(
            set(response),
            {"schema_version", "image_type", "sources", "candidates"},
        )
        with self.assertRaises(artwork_candidates.CandidateDiscoveryError):
            artwork_candidates.decode_request_body(
                b'{"schema_version":2,"type":"movie","tmdb_id":"550",'
                b'"image_type":"Logo","extra":true}'
            )

    def test_schema_two_identifier_boundaries_match_jellyfin(self):
        twenty = "9" * 20
        for image_type, media_type, tmdb_id, imdb_id, tvdb_id in (
            ("Primary", "movie", "1", "tt1", None),
            ("Primary", "movie", twenty, f"tt{twenty}", None),
            ("Logo", "series", "1", "tt1", "1"),
            ("Logo", "series", twenty, f"tt{twenty}", twenty),
        ):
            with self.subTest(image_type=image_type, tmdb_id=tmdb_id):
                payload = {
                    "schema_version": 2,
                    "type": media_type,
                    "tmdb_id": tmdb_id,
                    "imdb_id": imdb_id,
                    "image_type": image_type,
                }
                if tvdb_id is not None:
                    payload["tvdb_id"] = tvdb_id
                parsed = artwork_candidates.decode_request_body(
                    json.dumps(payload).encode()
                )
                self.assertEqual(parsed.tmdb_id, tmdb_id)
                self.assertEqual(parsed.imdb_id, imdb_id)
                self.assertEqual(parsed.tvdb_id, tvdb_id)

        invalid_values = (
            ("tmdb_id", "0" + "9" * 19),
            ("tmdb_id", "١"),
            ("tmdb_id", "9" * 21),
            ("imdb_id", "TT123"),
            ("imdb_id", "tt" + "9" * 21),
            ("imdb_id", "tt١"),
        )
        for image_type in ("Primary", "Logo"):
            for field, value in invalid_values:
                with self.subTest(
                    image_type=image_type,
                    field=field,
                    value=value,
                ):
                    payload = {
                        "schema_version": 2,
                        "type": "series" if image_type == "Logo" else "movie",
                        "tmdb_id": "123",
                        "imdb_id": "tt123",
                        "image_type": image_type,
                    }
                    if image_type == "Logo":
                        payload["tvdb_id"] = "456"
                    payload[field] = value
                    with self.assertRaises(
                        artwork_candidates.CandidateDiscoveryError
                    ):
                        artwork_candidates.decode_request_body(
                            json.dumps(payload).encode()
                        )

        for value in ("0" + "9" * 19, "١", "9" * 21):
            payload = {
                "schema_version": 2,
                "type": "series",
                "tmdb_id": "123",
                "imdb_id": "tt123",
                "tvdb_id": value,
                "image_type": "Logo",
            }
            with self.subTest(tvdb_id=value):
                with self.assertRaises(
                    artwork_candidates.CandidateDiscoveryError
                ):
                    artwork_candidates.decode_request_body(
                        json.dumps(payload).encode()
                    )

    async def test_render_identifier_boundaries_match_jellyfin(self):
        twenty = "9" * 20
        for tmdb_id, imdb_id in (("1", "tt1"), (twenty, f"tt{twenty}")):
            valid = await self._render(
                primary=_image_bytes(),
                logo=None,
                treatment="title_treatment",
                effective_hash=None,
                tmdb_id=tmdb_id,
                imdb_id=imdb_id,
            )
            self.assertEqual(valid.status_code, 200)

        for tmdb_id, imdb_id in (
            ("0" + "9" * 19, "tt123"),
            ("١", "tt123"),
            ("9" * 21, "tt123"),
            ("123", "TT123"),
            ("123", "tt" + "9" * 21),
            ("123", "tt١"),
        ):
            with self.subTest(tmdb_id=tmdb_id, imdb_id=imdb_id):
                response = await self._render(
                    primary=_image_bytes(),
                    logo=None,
                    treatment="title_treatment",
                    effective_hash=None,
                    tmdb_id=tmdb_id,
                    imdb_id=imdb_id,
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.json()["error"]["code"],
                    "invalid_request",
                )


class SelectedPipelineIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def _render_pipeline(
        self,
        *,
        logo: bytes | None,
        provider_logo: Image.Image | None,
        primary: bytes | None = None,
        title: str = "Fight Club",
        ocr_cached: bool | None = False,
        ocr_result: bool | None = False,
        output_format: str = "jpeg",
    ) -> tuple[httpx.Response, AsyncMock, bytes, list[str], list[dict]]:
        primary = primary or _image_bytes(
            size=(500, 750), color=(20, 30, 40, 255)
        )
        metadata = (
            [18],
            True,
            [],
            "1999",
            title,
            "/poster.jpg",
            None,
            {
                "imdb_id": "tt0137523",
                "original_language": "en",
                "original_title": title,
                "original_poster_path": "/poster.jpg",
                "poster_langs": {},
                "tmdb_release_date": "1999-10-15",
                "tmdb_status": "Released",
                "vote_average": 8.4,
                "vote_count": 100,
            },
        )
        profile = RenderProfile(
            schema_version=2,
            name="homestack-default",
            public_query={
                "badge_display_mode": "0",
                "bottom_gradient": "off",
                "rating_display_mode": "0",
                "sash_mode": "hidden",
                "show_award_sash": "false",
                "top_gradient": "off",
            },
            digest="d" * 64,
        )
        logo_fetch = AsyncMock(
            return_value=provider_logo.copy()
            if provider_logo is not None
            else None
        )
        cache_keys: list[str] = []
        detector_calls: list[dict] = []

        def cache_lookup(cache_key: str):
            cache_keys.append(cache_key)
            return ocr_cached

        def detect(_image, **kwargs):
            detector_calls.append(kwargs)
            return ocr_result

        transport = httpx.ASGITransport(app=main.app)
        patches = (
            patch.object(main._cfg, "ACCESS_KEY", "render-secret"),
            patch.object(main._cfg, "RENDERER_REVISION", RENDERER_REVISION),
            patch.object(main._cfg, "SERVER_TMDB_KEY", "tmdb-key"),
            patch.object(main._cfg, "SERVER_MDBLIST_KEYS", []),
            patch.object(main._cfg, "DISABLE_COMPOSITE_CACHE", True),
            patch.object(main._cfg, "TEXTLESS_TEXT_DETECTION", True),
            patch.object(main, "_render_profile", profile),
            patch.object(main, "_HTTP_CLIENT", object()),
            patch.object(main, "_text_detector_ready", return_value=True),
            patch.object(
                main,
                "_coalesced_fetch_poster_metadata",
                AsyncMock(return_value=metadata),
            ),
            patch.object(main, "get_cached_rating", return_value=None),
            patch.object(
                main,
                "get_cached_text_detection",
                side_effect=cache_lookup,
            ),
            patch.object(main, "set_cached_text_detection"),
            patch.object(
                text_detect,
                "poster_has_burned_in_text",
                side_effect=detect,
            ),
            patch.object(main, "_detect_executor", None),
            patch.object(main, "_detect_semaphore", None),
            patch.object(main, "_foreground_detection_count", 0),
            patch.object(main, "_text_detection_inflight", {}),
            patch.object(main, "fetch_logo", logo_fetch),
            patch.object(
                main,
                "fetch_trending_rank",
                AsyncMock(return_value=None),
            ),
            patch.object(
                main,
                "fetch_release_status",
                AsyncMock(return_value=None),
            ),
            patch.object(
                main,
                "fetch_recent_movie_digital_release_date",
                AsyncMock(return_value=None),
            ),
            patch.object(main.tvdb, "tvdb_enabled", return_value=False),
            patch.object(main.imdb_dataset, "is_enabled", return_value=False),
        )
        with ExitStack() as stack:
            for current_patch in patches:
                stack.enter_context(current_patch)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://renderer",
            ) as client:
                response = await client.post(
                    "/render/selection?profile=homestack-default"
                    "&tmdb_id=550&imdb_id=tt0137523&type=movie&quality=4K"
                    f"&output_format={output_format}",
                    content=_homestack_envelope(primary, logo),
                    headers={
                        "Content-Type": "application/json",
                        "X-Jellyfin-Artwork-Key": "render-secret",
                    },
                )
            main._shutdown_detect_executor()
        return response, logo_fetch, primary, cache_keys, detector_calls

    async def test_selected_logo_runs_through_stock_pipeline(self):
        logo = _image_bytes(
            size=(180, 60),
            color=(230, 230, 230, 255),
        )
        response, logo_fetch, primary, _, _ = await self._render_pipeline(
            logo=logo,
            provider_logo=None,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.headers["x-image-sha256"],
            hashlib.sha256(response.content).hexdigest(),
        )
        self.assertEqual(response.headers["x-render-profile-sha256"], "d" * 64)
        self.assertEqual(
            response.headers["content-type"].split(";", 1)[0],
            "image/jpeg",
        )
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["pragma"], "no-cache")
        self.assertEqual(response.headers["x-logo-treatment"], "selected_logo")
        self.assertEqual(
            response.headers["x-effective-logo-sha256"],
            hashlib.sha256(logo).hexdigest(),
        )
        self.assertEqual(
            response.headers["x-selected-primary-sha256"],
            hashlib.sha256(primary).hexdigest(),
        )
        self.assertEqual(
            response.headers["x-selected-logo-sha256"],
            hashlib.sha256(logo).hexdigest(),
        )
        logo_fetch.assert_not_awaited()
        with Image.open(io.BytesIO(response.content)) as rendered:
            self.assertEqual(rendered.size, (500, 750))

    async def test_no_selected_logo_hashes_effective_provider_pixels(self):
        provider_logo = Image.new("RGBA", (180, 60), (240, 240, 240, 255))
        response, logo_fetch, _, _, _ = await self._render_pipeline(
            logo=None,
            provider_logo=provider_logo,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["x-logo-treatment"], "provider_logo")
        self.assertEqual(
            response.headers["x-effective-logo-sha256"],
            effective_logo_sha256(provider_logo),
        )
        logo_fetch.assert_awaited_once()

    async def test_no_provider_logo_reports_title_treatment(self):
        response, logo_fetch, _, _, _ = await self._render_pipeline(
            logo=None,
            provider_logo=None,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["x-logo-treatment"], "title_treatment")
        self.assertNotIn("x-effective-logo-sha256", response.headers)
        logo_fetch.assert_awaited_once()

    async def test_noncanonical_selected_primary_is_stock_normalized(self):
        for size in ((1000, 1500), (900, 1200)):
            primary = _patterned_primary(size)
            with self.subTest(size=size):
                response, _, _, _, _ = await self._render_pipeline(
                    logo=None,
                    provider_logo=None,
                    primary=primary,
                    output_format="png",
                )
                self.assertEqual(response.status_code, 200, response.text)
                with Image.open(io.BytesIO(response.content)) as rendered:
                    self.assertEqual(rendered.size, (500, 750))

    async def test_selected_ocr_uses_poster_source_and_title_scoped_key(self):
        response, _, primary, cache_keys, detector_calls = (
            await self._render_pipeline(
                logo=_image_bytes(
                    size=(180, 60),
                    color=(230, 230, 230, 255),
                ),
                provider_logo=None,
                primary=_patterned_primary((1000, 1500)),
                title="Fight Club",
                ocr_cached=None,
                ocr_result=False,
            )
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertGreaterEqual(len(cache_keys), 2)
        selected_key = cache_keys[0]
        self.assertEqual(cache_keys[1], selected_key)
        self.assertIn("selected-primary-v2", selected_key)
        self.assertIn(hashlib.sha256(primary).hexdigest(), selected_key)
        self.assertIn(
            artwork_candidates.normalized_title_digest(("Fight Club",)),
            selected_key,
        )
        self.assertEqual(len(detector_calls), 1)
        self.assertEqual(detector_calls[0]["source"], "poster")
        self.assertEqual(detector_calls[0]["title"], ("Fight Club",))

    async def test_unknown_selected_logo_ocr_fails_closed(self):
        response, logo_fetch, _, _, detector_calls = await self._render_pipeline(
            logo=_image_bytes(
                size=(180, 60),
                color=(230, 230, 230, 255),
            ),
            provider_logo=None,
            ocr_cached=None,
            ocr_result=None,
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["error"]["code"],
            "text_detection_unavailable",
        )
        self.assertEqual(len(detector_calls), 1)
        logo_fetch.assert_not_awaited()

    async def test_unknown_without_selected_logo_keeps_stock_provider_fallback(self):
        provider_logo = Image.new("RGBA", (180, 60), (240, 240, 240, 255))
        response, logo_fetch, _, _, detector_calls = await self._render_pipeline(
            logo=None,
            provider_logo=provider_logo,
            ocr_cached=None,
            ocr_result=None,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["x-logo-treatment"], "provider_logo")
        self.assertEqual(len(detector_calls), 1)
        logo_fetch.assert_awaited_once()

    async def test_same_selected_bytes_under_different_titles_do_not_share_ocr(self):
        primary = _patterned_primary((1000, 1500))
        logo = _image_bytes(
            size=(180, 60),
            color=(230, 230, 230, 255),
        )
        first = await self._render_pipeline(
            logo=logo,
            provider_logo=None,
            primary=primary,
            title="Fight Club",
            ocr_cached=None,
            ocr_result=False,
        )
        second = await self._render_pipeline(
            logo=logo,
            provider_logo=None,
            primary=primary,
            title="The Matrix",
            ocr_cached=None,
            ocr_result=False,
        )
        self.assertEqual(first[0].status_code, 200)
        self.assertEqual(second[0].status_code, 200)
        self.assertNotEqual(first[3][0], second[3][0])
        self.assertEqual(len(first[4]), 1)
        self.assertEqual(len(second[4]), 1)

    def test_selected_ocr_key_is_title_dependent(self):
        selected_hash = "e" * 64
        first = main._selected_ocr_cache_key(
            selected_hash,
            ("Fight Club",),
        )
        equivalent = main._selected_ocr_cache_key(
            selected_hash,
            ("  FIGHT   CLUB  ",),
        )
        second = main._selected_ocr_cache_key(
            selected_hash,
            ("The Matrix",),
        )
        self.assertEqual(first, equivalent)
        self.assertNotEqual(first, second)


class EffectiveTreatmentTests(unittest.IsolatedAsyncioTestCase):
    def test_provider_hash_covers_effective_pixels_and_dimensions(self):
        first = Image.new("RGBA", (80, 20), (10, 20, 30, 255))
        second = first.resize((40, 10))
        self.assertEqual(effective_logo_sha256(first), effective_logo_sha256(first.copy()))
        self.assertNotEqual(effective_logo_sha256(first), effective_logo_sha256(second))

    async def test_readiness_keeps_upstream_and_renderer_revisions_distinct(self):
        with (
            patch.object(main._cfg, "ACCESS_KEY", "render-secret"),
            patch.object(main._cfg, "RENDERER_REVISION", RENDERER_REVISION),
            patch.object(main, "_render_profile", _profile()),
            patch.object(main, "_HTTP_CLIENT", object()),
            patch.object(main, "_text_detector_ready", return_value=True),
        ):
            response = await main.readiness(
                type("Request", (), {"query_params": {}})()
            )
        payload = json.loads(response.body)
        self.assertEqual(payload["upstream_revision"], UPSTREAM_REVISION)
        self.assertEqual(payload["renderer_revision"], RENDERER_REVISION)
        self.assertNotEqual(
            payload["upstream_revision"],
            payload["renderer_revision"],
        )
