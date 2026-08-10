import asyncio
import io
import json
import logging
import sqlite3
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from PIL import Image

import artwork_candidates as candidates
import cache
import main


def _image_bytes(
    *,
    size=(600, 900),
    color=(20, 40, 60),
    image_format="JPEG",
) -> bytes:
    image = Image.new("RGB", size, color)
    output = io.BytesIO()
    image.save(output, format=image_format)
    return output.getvalue()


def _json_response(payload, status=200, headers=None):
    return httpx.Response(
        status,
        json=payload,
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def _settings(**overrides):
    values = {
        "tmdb_api_key": "tmdb-secret",
        "fanart_project_api_key": "fanart-project",
        "fanart_client_key": "fanart-client",
        "max_image_bytes": 2 * 1024 * 1024,
        "max_width": 2000,
        "max_height": 3000,
        "max_pixels": 4_000_000,
        "ocr_box_threshold": 0.70,
    }
    values.update(overrides)
    return candidates.DiscoverySettings(**values)


def _request(media_type="movie", tvdb_id=None):
    return candidates.CandidateRequest(
        media_type=media_type,
        tmdb_id="123",
        imdb_id="tt1234567",
        tvdb_id=tvdb_id,
    )


class StrictSchemaTests(unittest.TestCase):
    def test_accepts_only_the_versioned_exact_shape(self):
        body = json.dumps({
            "schema_version": 1,
            "type": "series",
            "tmdb_id": "123",
            "imdb_id": "tt1234567",
            "tvdb_id": "456",
        }).encode()
        parsed = candidates.decode_request_body(body)
        self.assertEqual(parsed, _request("series", "456"))

        without_tvdb = candidates.decode_request_body(json.dumps({
            "schema_version": 1,
            "type": "movie",
            "tmdb_id": "123",
            "imdb_id": "tt1234567",
        }).encode())
        self.assertIsNone(without_tvdb.tvdb_id)

    def test_rejects_duplicates_unknowns_and_malformed_values(self):
        invalid = (
            b'{"schema_version":1,"type":"movie","type":"series","tmdb_id":"123","imdb_id":"tt1"}',
            b'{"schema_version":1,"type":"movie","tmdb_id":"123","imdb_id":"tt1","limit":8}',
            b'{"schema_version":2,"type":"movie","tmdb_id":"123","imdb_id":"tt1"}',
            b'{"schema_version":1,"type":"tv","tmdb_id":"123","imdb_id":"tt1"}',
            b'{"schema_version":1,"type":"movie","tmdb_id":"0","imdb_id":"tt1"}',
            b'{"schema_version":1,"type":"movie","tmdb_id":"01","imdb_id":"tt1"}',
            b'{"schema_version":1,"type":"movie","tmdb_id":"123","imdb_id":"123"}',
            b'{"schema_version":1,"type":"series","tmdb_id":"123","imdb_id":"tt1","tvdb_id":"x"}',
            b'[]',
            b'not-json',
        )
        for body in invalid:
            with self.subTest(body=body):
                with self.assertRaises(candidates.CandidateDiscoveryError):
                    candidates.decode_request_body(body)


class UrlPolicyTests(unittest.TestCase):
    def test_tmdb_urls_are_constructed_canonically(self):
        self.assertEqual(
            candidates.canonical_tmdb_url("/abc_123.jpg"),
            "https://image.tmdb.org/t/p/original/abc_123.jpg",
        )
        for value in (
            "abc.jpg",
            "/nested/abc.jpg",
            "/abc.svg",
            "/abc.jpg?api_key=secret",
            "//evil.example/abc.jpg",
        ):
            with self.subTest(value=value):
                with self.assertRaises(candidates._CandidateRejected):
                    candidates.canonical_tmdb_url(value)

    def test_fanart_urls_reject_unsafe_or_wrong_source_paths(self):
        valid = "https://assets.fanart.tv/fanart/movies/123/movieposter/456.jpg"
        self.assertEqual(
            candidates.canonical_fanart_url(
                valid,
                media_type="movie",
                tmdb_id="123",
                tvdb_id=None,
            ),
            valid,
        )
        self.assertEqual(
            candidates.canonical_fanart_url(
                "https://assets.fanart.tv:443/fanart/movies/123/movieposter/456.jpg",
                media_type="movie",
                tmdb_id="123",
                tvdb_id=None,
            ),
            valid,
        )
        invalid = (
            valid.replace("https://", "http://"),
            valid.replace("assets.fanart.tv", "evil.example"),
            valid.replace("assets.fanart.tv", "assets.fanart.tv."),
            valid.replace("assets.fanart.tv", "127.0.0.1"),
            valid.replace("https://", "https://user@"),
            valid.replace("assets.fanart.tv", "assets.fanart.tv:444"),
            valid + "?api_key=secret",
            valid + "#fragment",
            valid.replace("/123/", "/999/"),
            valid.replace("/movieposter/", "/background/"),
            valid.replace("456.jpg", "nested/456.jpg"),
        )
        for url in invalid:
            with self.subTest(url=url):
                with self.assertRaises(candidates._CandidateRejected):
                    candidates.canonical_fanart_url(
                        url,
                        media_type="movie",
                        tmdb_id="123",
                        tvdb_id=None,
                    )


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_tmdb_filters_neutral_portraits_ranks_and_caps_at_eight(self):
        posters = []
        for index in range(10):
            posters.append({
                "file_path": f"/poster-{index}.jpg",
                "iso_639_1": None,
                "width": 600 + index,
                "height": 900 + index,
                "vote_average": 5 + index / 10,
                "vote_count": index,
            })
        posters.extend([
            {
                "file_path": "/localized.jpg",
                "iso_639_1": "en",
                "width": 5000,
                "height": 7500,
                "vote_average": 10,
                "vote_count": 9999,
            },
            {
                "file_path": "/landscape.jpg",
                "iso_639_1": None,
                "width": 9000,
                "height": 6000,
                "vote_average": 10,
                "vote_count": 9999,
            },
            {"file_path": "bad.jpg", "iso_639_1": None, "width": 600, "height": 900},
        ])

        async def handler(request):
            self.assertEqual(request.url.params["api_key"], "tmdb-secret")
            self.assertEqual(request.url.params["include_image_language"], "null")
            return _json_response({"posters": posters})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            values = await candidates._tmdb_candidates(
                client,
                _request(),
                _settings(),
            )
        self.assertEqual(len(values), 8)
        self.assertEqual(values[0].url.rsplit("/", 1)[-1], "poster-9.jpg")
        self.assertTrue(all(value.language is None for value in values))

    async def test_source_api_redirects_are_failures(self):
        async def handler(_request):
            return httpx.Response(
                302,
                headers={"Location": "https://api.example.invalid/redirect"},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(candidates._SourceFailure) as raised:
                await candidates._tmdb_candidates(
                    client,
                    _request(),
                    _settings(),
                )
        self.assertEqual(raised.exception.reason, "redirect")

    async def test_fanart_uses_separate_keys_and_verified_series_tvdb_path(self):
        seen_query = {}

        async def handler(request):
            seen_query.update(dict(request.url.params))
            self.assertEqual(request.url.path, "/v3/tv/456")
            return _json_response({
                "tvposter": [
                    {
                        "url": "https://assets.fanart.tv/fanart/tv/456/tvposter/clean.jpg",
                        "lang": "00",
                        "likes": "12",
                    },
                    {
                        "url": "https://assets.fanart.tv/fanart/tv/456/tvposter/local.jpg",
                        "lang": "en",
                        "likes": "99",
                    },
                ]
            })

        identity = candidates._Identity(("Title",), True)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            values = await candidates._fanart_candidates(
                client,
                _request("series", "456"),
                identity,
                _settings(),
            )
        self.assertEqual(
            seen_query,
            {"api_key": "fanart-project", "client_key": "fanart-client"},
        )
        self.assertEqual(len(values), 1)
        self.assertNotIn("key", values[0].url)

    async def test_series_fanart_is_skipped_without_verified_tvdb_identity(self):
        handler = AsyncMock()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            values = await candidates._fanart_candidates(
                client,
                _request("series"),
                candidates._Identity(("Title",), False),
                _settings(),
            )
        self.assertEqual(values, [])
        handler.assert_not_awaited()

    async def test_identity_consistency_rejects_mixed_ids(self):
        async def handler(_request):
            return _json_response({
                "id": 123,
                "title": "Expected",
                "original_title": "Expected",
                "external_ids": {"imdb_id": "tt9999999", "tvdb_id": 456},
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(candidates.CandidateDiscoveryError) as raised:
                await candidates._fetch_identity(client, _request(), _settings())
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.code, "mixed_identity")


class ImageAndOcrTests(unittest.IsolatedAsyncioTestCase):
    def test_image_decoder_enforces_type_dimensions_pixels_and_portrait_window(self):
        settings = _settings()
        image, width, height = candidates._decode_screening_image(
            _image_bytes(),
            "image/jpeg",
            settings,
        )
        image.close()
        self.assertEqual((width, height), (600, 900))

        cases = (
            (_image_bytes(size=(900, 600)), "image/jpeg", settings),
            (_image_bytes(size=(400, 900)), "image/jpeg", settings),
            (_image_bytes(), "image/png", settings),
            (_image_bytes(size=(600, 900)), "image/jpeg", _settings(max_pixels=10)),
        )
        for body, content_type, case_settings in cases:
            with self.subTest(content_type=content_type, settings=case_settings):
                with self.assertRaises(candidates._CandidateRejected):
                    candidates._decode_screening_image(
                        body,
                        content_type,
                        case_settings,
                    )

    async def test_redirect_and_oversized_image_bytes_are_rejected(self):
        candidate = candidates.SourceCandidate(
            "tmdb",
            "https://image.tmdb.org/t/p/original/a.jpg",
            600,
            900,
            None,
        )

        async def redirect(_request):
            return httpx.Response(302, headers={"Location": "https://evil.example/x"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(redirect)) as client:
            with self.assertRaises(candidates._CandidateRejected):
                await candidates._fetch_screening_image(
                    client,
                    candidate,
                    _settings(),
                )

        body = _image_bytes()

        async def too_large(_request):
            return httpx.Response(
                200,
                content=body,
                headers={
                    "Content-Type": "image/jpeg",
                    "Content-Length": str(len(body)),
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(too_large)) as client:
            with self.assertRaises(candidates._CandidateRejected):
                await candidates._fetch_screening_image(
                    client,
                    candidate,
                    _settings(max_image_bytes=len(body) - 1),
                )

    async def test_ocr_positive_is_rejected_and_unavailable_is_fail_closed(self):
        image = Image.new("RGBA", (600, 900), "black")
        digest = "a" * 64
        with (
            patch.object(candidates, "get_cached_text_detection", return_value=None),
            patch.object(candidates, "text_detection_ready", return_value=True),
            patch.object(candidates, "poster_has_burned_in_text", return_value=True),
            patch.object(candidates, "set_cached_text_detection") as cache_write,
        ):
            self.assertTrue(
                await candidates._ocr_has_text(
                    image,
                    digest,
                    ("Title",),
                    _settings(),
                )
            )
            cache_write.assert_called_once()

        with (
            patch.object(candidates, "get_cached_text_detection", return_value=None),
            patch.object(candidates, "text_detection_ready", return_value=False),
        ):
            with self.assertRaises(candidates._DetectionUnavailable):
                await candidates._ocr_has_text(
                    image,
                    digest,
                    ("Title",),
                    _settings(),
                )
        image.close()


class DiscoveryPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        candidates._search_admission.reset_for_tests()

    async def _discover_with_patches(
        self,
        *,
        tmdb_values=None,
        fanart_values=None,
        tmdb_failure=False,
        fanart_failure=False,
        fanart_enabled=True,
        ocr_result=False,
    ):
        tmdb_values = [] if tmdb_values is None else tmdb_values
        fanart_values = [] if fanart_values is None else fanart_values

        async def load_tmdb(*_args):
            if tmdb_failure:
                raise candidates._SourceFailure("failed")
            return tmdb_values

        async def load_fanart(*_args):
            if fanart_failure:
                raise candidates._SourceFailure("failed")
            return fanart_values

        settings = _settings()
        if not fanart_enabled:
            settings = _settings(
                fanart_project_api_key="",
                fanart_client_key="",
            )
        client = Mock()
        with (
            patch.object(
                candidates,
                "_fetch_identity",
                AsyncMock(return_value=candidates._Identity(("Title",), True)),
            ),
            patch.object(candidates, "_tmdb_candidates", side_effect=load_tmdb),
            patch.object(candidates, "_fanart_candidates", side_effect=load_fanart),
            patch.object(candidates, "get_cached_artwork_candidates", return_value=None),
            patch.object(candidates, "set_cached_artwork_candidates") as cache_write,
            patch.object(candidates, "text_detection_ready", return_value=True),
            patch.object(
                candidates,
                "_fetch_screening_image",
                AsyncMock(side_effect=lambda _client, item, _settings: (
                    item.url.encode(),
                    Image.new("RGBA", (600, 900), "black"),
                    600,
                    900,
                )),
            ),
            patch.object(
                candidates,
                "_ocr_has_text",
                AsyncMock(side_effect=ocr_result if isinstance(ocr_result, Exception) else None,
                          return_value=ocr_result if not isinstance(ocr_result, Exception) else False),
            ),
        ):
            result = await candidates.discover_candidates(
                client,
                _request("series", "456"),
                settings=settings,
            )
        return result, cache_write

    async def test_exact_dedupe_per_source_ranking_and_alternation(self):
        tmdb_values = [
            candidates.SourceCandidate(
                "tmdb",
                f"https://image.tmdb.org/t/p/original/tmdb-{index}.jpg",
                600,
                900,
                None,
                rating=10 - index,
                votes=100 - index,
                ordinal=index,
            )
            for index in range(5)
        ]
        fanart_values = [
            candidates.SourceCandidate(
                "fanart",
                f"https://assets.fanart.tv/fanart/tv/456/tvposter/fanart-{index}.jpg",
                600,
                900,
                None,
                likes=100 - index,
                ordinal=index,
            )
            for index in range(5)
        ]
        (response, outcome), cache_write = await self._discover_with_patches(
            tmdb_values=tmdb_values,
            fanart_values=fanart_values,
        )
        self.assertEqual(outcome, "complete")
        self.assertEqual(len(response["candidates"]), 8)
        self.assertEqual(
            [item["source"] for item in response["candidates"]],
            ["tmdb", "fanart"] * 4,
        )
        cache_write.assert_called_once()
        self.assertEqual(cache_write.call_args.args[2], "complete")

    async def test_exact_sha_dedupe_keeps_first_ranked_source_copy(self):
        duplicate = b"same bytes"
        items = [
            candidates.SourceCandidate(
                "tmdb",
                "https://image.tmdb.org/t/p/original/a.jpg",
                600,
                900,
                None,
            ),
            candidates.SourceCandidate(
                "fanart",
                "https://assets.fanart.tv/fanart/tv/456/tvposter/a.jpg",
                600,
                900,
                None,
            ),
        ]

        async def fetch(_client, _item, _settings):
            return duplicate, Image.new("RGBA", (600, 900), "black"), 600, 900

        with (
            patch.object(candidates, "_fetch_screening_image", side_effect=fetch),
            patch.object(candidates, "_ocr_has_text", AsyncMock(return_value=False)),
        ):
            seen = set()
            work = {"ocr": 0}
            first = await candidates._screen_source(
                Mock(), [items[0]], titles=("Title",), settings=_settings(),
                seen_digests=seen, work=work,
            )
            second = await candidates._screen_source(
                Mock(), [items[1]], titles=("Title",), settings=_settings(),
                seen_digests=seen, work=work,
            )
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(work["ocr"], 1)

    async def test_no_clean_art_returns_200_empty_without_fallback(self):
        item = candidates.SourceCandidate(
            "tmdb",
            "https://image.tmdb.org/t/p/original/title.jpg",
            600,
            900,
            None,
        )
        (response, outcome), cache_write = await self._discover_with_patches(
            tmdb_values=[item],
            fanart_enabled=False,
            ocr_result=True,
        )
        self.assertEqual(response["candidates"], [])
        self.assertEqual(outcome, "empty")
        self.assertEqual(cache_write.call_args.args[2], "empty")

    async def test_partial_source_failure_is_200_and_cached_briefly(self):
        item = candidates.SourceCandidate(
            "tmdb",
            "https://image.tmdb.org/t/p/original/clean.jpg",
            600,
            900,
            None,
        )
        (response, outcome), cache_write = await self._discover_with_patches(
            tmdb_values=[item],
            fanart_failure=True,
        )
        self.assertEqual(response["sources"]["tmdb"], "ready")
        self.assertEqual(response["sources"]["fanart"], "failed")
        self.assertEqual(outcome, "partial")
        self.assertEqual(cache_write.call_args.args[2], "partial")

    async def test_all_configured_sources_failed_is_503_and_not_cached(self):
        with self.assertRaises(candidates.CandidateDiscoveryError) as raised:
            await self._discover_with_patches(
                tmdb_failure=True,
                fanart_failure=True,
            )
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.code, "all_sources_failed")

    async def test_detection_unavailable_is_503_and_not_cached(self):
        item = candidates.SourceCandidate(
            "tmdb",
            "https://image.tmdb.org/t/p/original/clean.jpg",
            600,
            900,
            None,
        )
        with self.assertRaises(candidates.CandidateDiscoveryError) as raised:
            await self._discover_with_patches(
                tmdb_values=[item],
                fanart_enabled=False,
                ocr_result=candidates._DetectionUnavailable(),
            )
        self.assertEqual(raised.exception.code, "text_detection_unavailable")

    async def test_cold_empty_search_is_not_cached_when_detector_is_unavailable(self):
        with (
            patch.object(candidates, "get_cached_artwork_candidates", return_value=None),
            patch.object(candidates, "text_detection_ready", return_value=False),
            patch.object(candidates, "_fetch_identity", AsyncMock()) as identity,
            patch.object(candidates, "set_cached_artwork_candidates") as cache_write,
        ):
            with self.assertRaises(candidates.CandidateDiscoveryError) as raised:
                await candidates.discover_candidates(
                    Mock(),
                    _request(),
                    settings=_settings(),
                )
        self.assertEqual(raised.exception.code, "text_detection_unavailable")
        identity.assert_not_awaited()
        cache_write.assert_not_called()

    async def test_work_ceiling_never_exceeds_sixteen_ocr_attempts(self):
        values = [
            candidates.SourceCandidate(
                "tmdb",
                f"https://image.tmdb.org/t/p/original/{index}.jpg",
                600,
                900,
                None,
            )
            for index in range(20)
        ]
        fetch = AsyncMock(side_effect=lambda _client, item, _settings: (
            item.url.encode(),
            Image.new("RGBA", (600, 900), "black"),
            600,
            900,
        ))
        ocr = AsyncMock(return_value=True)
        with (
            patch.object(candidates, "_fetch_screening_image", fetch),
            patch.object(candidates, "_ocr_has_text", ocr),
        ):
            work = {"ocr": 0}
            result = await candidates._screen_source(
                Mock(),
                values,
                titles=("Title",),
                settings=_settings(),
                seen_digests=set(),
                work=work,
            )
        self.assertEqual(result, [])
        self.assertEqual(work["ocr"], candidates.MAX_OCR_ATTEMPTS)
        self.assertEqual(ocr.await_count, candidates.MAX_OCR_ATTEMPTS)

    async def test_valid_result_cache_skips_all_upstream_work(self):
        cached = {
            "schema_version": 1,
            "sources": {"tmdb": "ready", "fanart": "disabled"},
            "candidates": [{
                "source": "tmdb",
                "url": "https://image.tmdb.org/t/p/original/clean.jpg",
                "width": 600,
                "height": 900,
                "language": None,
            }],
        }
        with (
            patch.object(candidates, "get_cached_artwork_candidates", return_value=cached),
            patch.object(candidates, "_fetch_identity", AsyncMock()) as identity,
        ):
            response, outcome = await candidates.discover_candidates(
                Mock(),
                _request(),
                settings=_settings(fanart_project_api_key="", fanart_client_key=""),
            )
        self.assertEqual(response, cached)
        self.assertEqual(outcome, "cache")
        identity.assert_not_awaited()

    async def test_deadline_is_a_typed_uncached_503(self):
        async def slow(*_args):
            await asyncio.sleep(0.05)

        with (
            patch.object(candidates, "SEARCH_DEADLINE_SECONDS", 0.001),
            patch.object(candidates, "get_cached_artwork_candidates", return_value=None),
            patch.object(candidates, "text_detection_ready", return_value=True),
            patch.object(candidates, "_fetch_identity", side_effect=slow),
            patch.object(candidates, "set_cached_artwork_candidates") as cache_write,
        ):
            with self.assertRaises(candidates.CandidateDiscoveryError) as raised:
                await candidates.discover_candidates(
                    Mock(),
                    _request(),
                    settings=_settings(),
                )
        self.assertEqual(raised.exception.code, "deadline_exceeded")
        cache_write.assert_not_called()


class AdmissionControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_inflight_and_thirty_starts_per_minute_are_hard_limits(self):
        admission = candidates._SearchAdmission()
        self.assertIsNone(await admission.start(now=1.0))
        self.assertIsNone(await admission.start(now=1.0))
        self.assertEqual(await admission.start(now=1.0), "concurrency_limited")
        await admission.finish()
        await admission.finish()

        admission = candidates._SearchAdmission()
        for index in range(candidates.MAX_SEARCH_STARTS_PER_MINUTE):
            self.assertIsNone(await admission.start(now=2.0 + index / 100))
            await admission.finish()
        self.assertEqual(await admission.start(now=3.0), "rate_limited")
        self.assertIsNone(await admission.start(now=63.0))


class CandidateCacheTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("""
            CREATE TABLE artwork_candidate_cache (
                cache_key TEXT PRIMARY KEY,
                response_json TEXT NOT NULL,
                result_class TEXT NOT NULL,
                cached_at INTEGER NOT NULL
            )
        """)

    def tearDown(self):
        self.connection.close()

    def test_complete_empty_and_partial_ttls_are_distinct(self):
        response = {
            "schema_version": 1,
            "sources": {"tmdb": "ready", "fanart": "disabled"},
            "candidates": [],
        }
        with (
            patch.object(cache, "get_db", return_value=self.connection),
            patch.object(cache.time, "time", return_value=1000),
        ):
            cache.set_cached_artwork_candidates("complete", response, "complete")
            cache.set_cached_artwork_candidates("empty", response, "empty")
            cache.set_cached_artwork_candidates("partial", response, "partial")

        with (
            patch.object(cache, "get_db", return_value=self.connection),
            patch.object(cache.time, "time", return_value=1901),
        ):
            self.assertEqual(
                cache.get_cached_artwork_candidates(
                    "complete", complete_ttl=86400, partial_ttl=900,
                ),
                response,
            )
            self.assertEqual(
                cache.get_cached_artwork_candidates(
                    "empty", complete_ttl=86400, partial_ttl=900,
                ),
                response,
            )
            self.assertIsNone(
                cache.get_cached_artwork_candidates(
                    "partial", complete_ttl=86400, partial_ttl=900,
                )
            )

    def test_failure_classes_are_never_cacheable(self):
        with self.assertRaises(ValueError):
            cache.set_cached_artwork_candidates("failure", {}, "failed")


class EndpointContractTests(unittest.IsolatedAsyncioTestCase):
    async def _post(self, headers=None, params=""):
        transport = httpx.ASGITransport(app=main.app)
        body = {
            "schema_version": 1,
            "type": "movie",
            "tmdb_id": "123",
            "imdb_id": "tt1234567",
        }
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.post(
                f"/v1/artwork/candidates{params}",
                json=body,
                headers=headers,
            )

    async def test_discovery_auth_is_separate_from_render_auth(self):
        success = {
            "schema_version": 1,
            "sources": {"tmdb": "ready", "fanart": "disabled"},
            "candidates": [],
        }
        with (
            patch.object(main._cfg, "JELLYFIN_ARTWORK_DISCOVERY_KEY", "discover"),
            patch.object(main._cfg, "ACCESS_KEY", "render"),
            patch.object(main, "_HTTP_CLIENT", Mock()),
            patch.object(
                main,
                "discover_candidates",
                AsyncMock(return_value=(success, "empty")),
            ),
        ):
            discovery = await self._post({
                "X-Jellyfin-Artwork-Discovery-Key": "discover",
            })
            render_key = await self._post({
                "X-Jellyfin-Artwork-Key": "render",
            })
            query_key = await self._post(params="?access_key=discover")

            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                render_route = await client.post(
                    "/render/selected?profile=x&tmdb_id=123&imdb_id=tt1234567",
                    content=_image_bytes(),
                    headers={
                        "Content-Type": "image/jpeg",
                        "X-Jellyfin-Artwork-Discovery-Key": "discover",
                    },
                )

        self.assertEqual(discovery.status_code, 200)
        self.assertEqual(discovery.headers["cache-control"], "no-store")
        self.assertEqual(render_key.status_code, 403)
        self.assertEqual(query_key.status_code, 403)
        self.assertEqual(render_route.status_code, 403)

    async def test_discovery_secret_must_not_equal_render_secret(self):
        with (
            patch.object(main._cfg, "JELLYFIN_ARTWORK_DISCOVERY_KEY", "same"),
            patch.object(main._cfg, "ACCESS_KEY", "same"),
        ):
            response = await self._post({
                "X-Jellyfin-Artwork-Discovery-Key": "same",
            })
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                render_response = await client.get(
                    "/ready",
                    headers={"X-Jellyfin-Artwork-Key": "same"},
                )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(render_response.status_code, 403)

    async def test_strict_content_type_and_query_contract(self):
        with (
            patch.object(main._cfg, "JELLYFIN_ARTWORK_DISCOVERY_KEY", "discover"),
            patch.object(main, "_HTTP_CLIENT", Mock()),
        ):
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                wrong_type = await client.post(
                    "/v1/artwork/candidates",
                    content=b"{}",
                    headers={
                        "Content-Type": "text/plain",
                        "X-Jellyfin-Artwork-Discovery-Key": "discover",
                    },
                )
                query = await self._post(
                    {"X-Jellyfin-Artwork-Discovery-Key": "discover"},
                    params="?limit=1",
                )
        self.assertEqual(wrong_type.status_code, 415)
        self.assertEqual(query.status_code, 400)
        self.assertEqual(wrong_type.headers["cache-control"], "no-store")

    async def test_request_body_is_bounded(self):
        with patch.object(
            main._cfg,
            "JELLYFIN_ARTWORK_DISCOVERY_KEY",
            "discover",
        ):
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/v1/artwork/candidates",
                    content=b"{" + b" " * candidates.MAX_BODY_BYTES + b"}",
                    headers={
                        "Content-Type": "application/json",
                        "X-Jellyfin-Artwork-Discovery-Key": "discover",
                    },
                )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_safe_logs_contain_only_aggregate_outcome(self):
        success = {
            "schema_version": 1,
            "sources": {"tmdb": "ready", "fanart": "ready"},
            "candidates": [],
        }
        with (
            patch.object(main._cfg, "JELLYFIN_ARTWORK_DISCOVERY_KEY", "discover"),
            patch.object(main, "_HTTP_CLIENT", Mock()),
            patch.object(
                main,
                "discover_candidates",
                AsyncMock(return_value=(success, "empty")),
            ),
            self.assertLogs(main.logger, level="INFO") as logs,
        ):
            response = await self._post({
                "X-Jellyfin-Artwork-Discovery-Key": "discover",
            })
        self.assertEqual(response.status_code, 200)
        combined = "\n".join(logs.output)
        self.assertIn("type=movie", combined)
        self.assertNotIn("tt1234567", combined)
        self.assertNotIn("tmdb_id", combined)
        self.assertNotIn("discover-secret", combined)

    def test_access_log_filter_removes_discovery_query_identity(self):
        record = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            (
                "test",
                "POST",
                "/v1/artwork/candidates?tmdb_id=123&imdb_id=tt1234567",
                "1.1",
                400,
            ),
            None,
        )
        self.assertTrue(main._TruncateUrlFilter().filter(record))
        self.assertEqual(record.args[2], "/v1/artwork/candidates?<redacted>")


if __name__ == "__main__":
    unittest.main()
