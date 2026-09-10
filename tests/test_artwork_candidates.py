import asyncio
import gzip
import hashlib
import hmac
import io
import json
import logging
import sqlite3
import threading
import time
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
    body = json.dumps(payload).encode()
    return httpx.Response(
        status,
        stream=httpx.ByteStream(body),
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


def _request(media_type="movie", tvdb_id=None, imdb_id="tt1234567"):
    return candidates.CandidateRequest(
        media_type=media_type,
        tmdb_id="123",
        imdb_id=imdb_id,
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

        tmdb_only = candidates.decode_request_body(json.dumps({
            "schema_version": 1,
            "type": "movie",
            "tmdb_id": "123",
        }).encode())
        self.assertEqual(tmdb_only, _request(imdb_id=None))

        explicit_nulls = candidates.decode_request_body(json.dumps({
            "schema_version": 1,
            "type": "series",
            "tmdb_id": "123",
            "imdb_id": None,
            "tvdb_id": None,
        }).encode())
        self.assertEqual(explicit_nulls, _request("series", None, None))

        tvdb_without_imdb = candidates.decode_request_body(json.dumps({
            "schema_version": 1,
            "type": "series",
            "tmdb_id": "123",
            "tvdb_id": "456",
        }).encode())
        self.assertEqual(tvdb_without_imdb, _request("series", "456", None))

    def test_rejects_duplicates_unknowns_and_malformed_values(self):
        invalid = (
            b'{"schema_version":1,"type":"movie","type":"series","tmdb_id":"123","imdb_id":"tt1"}',
            b'{"schema_version":1,"type":"movie","tmdb_id":"123","imdb_id":"tt1","limit":8}',
            b'{"schema_version":1,"type":"movie","imdb_id":"tt1"}',
            b'{"schema_version":2,"type":"movie","tmdb_id":"123","imdb_id":"tt1"}',
            b'{"schema_version":1,"type":"tv","tmdb_id":"123","imdb_id":"tt1"}',
            b'{"schema_version":1,"type":"movie","tmdb_id":"0","imdb_id":"tt1"}',
            b'{"schema_version":1,"type":"movie","tmdb_id":"01","imdb_id":"tt1"}',
            b'{"schema_version":1,"type":"movie","tmdb_id":"123","imdb_id":"123"}',
            b'{"schema_version":1,"type":"movie","tmdb_id":"123","imdb_id":123}',
            b'{"schema_version":1,"type":"movie","tmdb_id":"123","tvdb_id":"456"}',
            b'{"schema_version":1,"type":"series","tmdb_id":"123","imdb_id":"tt1","tvdb_id":"x"}',
            b'[]',
            b'not-json',
        )
        for body in invalid:
            with self.subTest(body=body):
                with self.assertRaises(candidates.CandidateDiscoveryError):
                    candidates.decode_request_body(body)

    def test_movie_rejects_non_null_tvdb_identity(self):
        with self.assertRaises(candidates.CandidateDiscoveryError) as raised:
            candidates.decode_request_body(json.dumps({
                "schema_version": 1,
                "type": "movie",
                "tmdb_id": "123",
                "tvdb_id": "456",
            }).encode())
        self.assertEqual(raised.exception.code, "invalid_tvdb_id")


class UrlPolicyTests(unittest.TestCase):
    def test_tmdb_urls_are_constructed_canonically(self):
        self.assertEqual(
            candidates.canonical_tmdb_url("/abc_123.jpg"),
            "https://image.tmdb.org/t/p/w500/abc_123.jpg",
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
        legacy = "https://assets.fanart.tv/fanart/chocolat-5fd9cd5bcc022.jpg"
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
                legacy,
                media_type="movie",
                tmdb_id="392",
                tvdb_id=None,
            ),
            legacy,
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
            "https://assets.fanart.tv/fanart/nested/chocolat-5fd9cd5bcc022.jpg",
            "https://assets.fanart.tv/fanart/chocolat-5fd9cd5bcc022.svg",
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
    def test_fanart_accepts_either_official_key_tier(self):
        project_only = _settings(fanart_client_key="")
        self.assertTrue(project_only.fanart_enabled)
        self.assertEqual(
            project_only.fanart_query_params(),
            {"api_key": "fanart-project"},
        )

        client_only = _settings(fanart_project_api_key="")
        self.assertTrue(client_only.fanart_enabled)
        self.assertEqual(
            client_only.fanart_query_params(),
            {"client_key": "fanart-client"},
        )

        disabled = _settings(
            fanart_project_api_key="",
            fanart_client_key="",
        )
        self.assertFalse(disabled.fanart_enabled)
        self.assertEqual(disabled.fanart_query_params(), {})

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
        self.assertTrue(all("/t/p/w500/" in value.url for value in values))
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

    async def test_fanart_accepts_legacy_flat_asset_paths(self):
        async def handler(request):
            self.assertEqual(request.url.path, "/v3/movies/123")
            return _json_response({
                "movieposter": [
                    {
                        "url": (
                            "https://assets.fanart.tv/fanart/"
                            "chocolat-5fd9cd5bcc022.jpg"
                        ),
                        "lang": "00",
                        "likes": "2",
                    },
                ]
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            values = await candidates._fanart_candidates(
                client,
                _request(),
                candidates._Identity(("Chocolat",), False),
                _settings(fanart_project_api_key=""),
            )
        self.assertEqual(len(values), 1)
        self.assertEqual(
            values[0].url,
            "https://assets.fanart.tv/fanart/chocolat-5fd9cd5bcc022.jpg",
        )

    async def test_series_fanart_returns_no_candidates_without_verified_tvdb_identity(self):
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

    async def test_identity_allows_omitted_ids_but_checks_any_supplied_tvdb(self):
        async def handler(_request):
            return _json_response({
                "id": 123,
                "title": "Expected",
                "original_title": "Expected",
                "external_ids": {"imdb_id": "tt1234567", "tvdb_id": 456},
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            identity = await candidates._fetch_identity(
                client,
                _request(imdb_id=None),
                _settings(),
            )
            self.assertFalse(identity.tvdb_verified)

            with self.assertRaises(candidates.CandidateDiscoveryError) as raised:
                await candidates._fetch_identity(
                    client,
                    _request("series", "999", None),
                    _settings(),
                )
        self.assertEqual(raised.exception.code, "mixed_identity")

    async def test_tmdb_only_identity_does_not_require_external_id_metadata(self):
        async def handler(_request):
            return _json_response({
                "id": 123,
                "title": "Expected",
                "original_title": "Expected",
            })

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            identity = await candidates._fetch_identity(
                client,
                _request(imdb_id=None),
                _settings(),
            )
        self.assertEqual(identity.titles, ("Expected",))
        self.assertFalse(identity.tvdb_verified)


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
            with self.assertRaises(candidates._ScreeningFailure):
                await candidates._fetch_screening_image(
                    client,
                    candidate,
                    _settings(),
                )

        async def bad_status(_request):
            return httpx.Response(503)

        async with httpx.AsyncClient(transport=httpx.MockTransport(bad_status)) as client:
            with self.assertRaises(candidates._ScreeningFailure):
                await candidates._fetch_screening_image(
                    client,
                    candidate,
                    _settings(),
                )

        async def transport_failure(request):
            raise httpx.ConnectError("unavailable", request=request)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport_failure)
        ) as client:
            with self.assertRaises(candidates._ScreeningFailure):
                await candidates._fetch_screening_image(
                    client,
                    candidate,
                    _settings(),
                )

        body = _image_bytes()

        async def too_large(_request):
            return httpx.Response(
                200,
                stream=httpx.ByteStream(body),
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

    async def test_content_encoding_is_rejected_before_decompression(self):
        compressed = gzip.compress(_image_bytes())

        async def handler(_request):
            return httpx.Response(
                200,
                stream=httpx.ByteStream(compressed),
                headers={
                    "Content-Type": "image/jpeg",
                    "Content-Encoding": "gzip",
                    "Content-Length": str(len(compressed)),
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with self.assertRaises(candidates._SourceFailure) as raised:
                await candidates._bounded_response_bytes(
                    client,
                    "https://image.tmdb.org/t/p/original/a.jpg",
                    params=None,
                    max_bytes=len(compressed) + 1,
                )
        self.assertEqual(raised.exception.reason, "content_encoding")

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
                    {},
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
                    {},
                )
        image.close()

    def test_ocr_cache_key_includes_normalized_title_identity(self):
        first = candidates._ocr_cache_key(
            "a" * 64,
            ("  The   TITLE ", "Original"),
            _settings(),
        )
        equivalent = candidates._ocr_cache_key(
            "a" * 64,
            ("original", "the title"),
            _settings(),
        )
        different = candidates._ocr_cache_key(
            "a" * 64,
            ("Different Title",),
            _settings(),
        )
        self.assertEqual(first, equivalent)
        self.assertNotEqual(first, different)
        self.assertIn(candidates.OCR_TITLE_POLICY_REVISION, first)

    def test_ocr_worker_has_no_queue_and_shutdown_does_not_join_active_call(self):
        worker = candidates._SingleOcrWorker()
        release = threading.Event()
        started = threading.Event()

        def blocking():
            started.set()
            release.wait(2)
            return False

        future = worker.submit(blocking)
        self.assertTrue(started.wait(1))
        with self.assertRaises(candidates._DetectionUnavailable):
            worker.submit(lambda: False)
        before = time.monotonic()
        worker.shutdown()
        self.assertLess(time.monotonic() - before, 0.1)
        release.set()
        self.assertFalse(future.result(timeout=1))


class DiscoveryPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        candidates._search_admission.reset_for_tests()
        candidates._candidate_inflight.clear()
        candidates._ocr_worker.reset_for_tests()

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
        self.assertEqual(
            set(response),
            {"schema_version", "sources", "candidates"},
        )
        self.assertEqual(set(response["sources"]), {"tmdb", "fanart"})
        self.assertTrue(
            set(response["sources"].values()) <= {"ready", "failed"}
        )
        self.assertTrue(all(
            set(item) == {"source", "url", "width", "height", "language"}
            for item in response["candidates"]
        ))
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
            first, first_reliable = await candidates._screen_source(
                Mock(), [items[0]], titles=("Title",), settings=_settings(),
                seen_digests=seen, work=work,
            )
            second, second_reliable = await candidates._screen_source(
                Mock(), [items[1]], titles=("Title",), settings=_settings(),
                seen_digests=seen, work=work,
            )
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertTrue(first_reliable)
        self.assertTrue(second_reliable)
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
            ocr_result=True,
        )
        self.assertEqual(response["candidates"], [])
        self.assertEqual(outcome, "empty")
        self.assertEqual(cache_write.call_args.args[2], "empty")

    async def test_tmdb_candidate_discovery_works_without_external_ids(self):
        item = candidates.SourceCandidate(
            "tmdb",
            "https://image.tmdb.org/t/p/original/clean.jpg",
            600,
            900,
            None,
        )
        with (
            patch.object(
                candidates,
                "_fetch_identity",
                AsyncMock(return_value=candidates._Identity(("Title",), False)),
            ),
            patch.object(candidates, "_tmdb_candidates", AsyncMock(return_value=[item])),
            patch.object(candidates, "_fanart_candidates", AsyncMock(return_value=[])),
            patch.object(candidates, "get_cached_artwork_candidates", return_value=None),
            patch.object(candidates, "set_cached_artwork_candidates"),
            patch.object(candidates, "text_detection_ready", return_value=True),
            patch.object(
                candidates,
                "_fetch_screening_image",
                AsyncMock(return_value=(
                    b"clean",
                    Image.new("RGBA", (600, 900), "black"),
                    600,
                    900,
                )),
            ),
            patch.object(candidates, "_ocr_has_text", AsyncMock(return_value=False)),
        ):
            response, outcome = await candidates.discover_candidates(
                Mock(),
                _request(imdb_id=None),
                settings=_settings(),
            )
        self.assertEqual(outcome, "complete")
        self.assertEqual(len(response["candidates"]), 1)
        self.assertEqual(response["candidates"][0]["source"], "tmdb")

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

    async def test_transient_screening_failure_discards_unreliable_source(self):
        tmdb_items = [
            candidates.SourceCandidate(
                "tmdb",
                f"https://image.tmdb.org/t/p/original/tmdb-{index}.jpg",
                600,
                900,
                None,
            )
            for index in range(2)
        ]
        fanart_item = candidates.SourceCandidate(
            "fanart",
            "https://assets.fanart.tv/fanart/tv/456/tvposter/fanart.jpg",
            600,
            900,
            None,
        )

        async def fetch(_client, item, _settings):
            if item.source == "tmdb" and item.url.endswith("tmdb-1.jpg"):
                raise candidates._ScreeningFailure()
            return (
                b"shared-clean",
                Image.new("RGBA", (600, 900), "black"),
                600,
                900,
            )

        with (
            patch.object(
                candidates,
                "_fetch_identity",
                AsyncMock(return_value=candidates._Identity(("Title",), True)),
            ),
            patch.object(candidates, "_tmdb_candidates", AsyncMock(return_value=tmdb_items)),
            patch.object(candidates, "_fanart_candidates", AsyncMock(return_value=[fanart_item])),
            patch.object(candidates, "_fetch_screening_image", side_effect=fetch),
            patch.object(candidates, "_ocr_has_text", AsyncMock(return_value=False)),
            patch.object(candidates, "get_cached_artwork_candidates", return_value=None),
            patch.object(candidates, "set_cached_artwork_candidates", return_value=True) as cache_write,
            patch.object(candidates, "text_detection_ready", return_value=True),
        ):
            response, outcome = await candidates.discover_candidates(
                Mock(),
                _request("series", "456"),
                settings=_settings(),
            )
        self.assertEqual(
            response["sources"],
            {"tmdb": "failed", "fanart": "ready"},
        )
        self.assertEqual(
            [item["source"] for item in response["candidates"]],
            ["fanart"],
        )
        self.assertEqual(outcome, "partial")
        self.assertEqual(cache_write.call_args.args[2], "partial")

    async def test_all_screening_transport_failures_return_uncached_503(self):
        item = candidates.SourceCandidate(
            "tmdb",
            "https://image.tmdb.org/t/p/original/tmdb.jpg",
            600,
            900,
            None,
        )
        with (
            patch.object(
                candidates,
                "_fetch_identity",
                AsyncMock(return_value=candidates._Identity(("Title",), False)),
            ),
            patch.object(candidates, "_tmdb_candidates", AsyncMock(return_value=[item])),
            patch.object(
                candidates,
                "_fetch_screening_image",
                AsyncMock(side_effect=candidates._ScreeningFailure()),
            ),
            patch.object(candidates, "get_cached_artwork_candidates", return_value=None),
            patch.object(candidates, "set_cached_artwork_candidates") as cache_write,
            patch.object(candidates, "text_detection_ready", return_value=True),
        ):
            with self.assertRaises(candidates.CandidateDiscoveryError) as raised:
                await candidates.discover_candidates(
                    Mock(),
                    _request(imdb_id=None),
                    settings=_settings(
                        fanart_project_api_key="",
                        fanart_client_key="",
                    ),
                )
        self.assertEqual(raised.exception.code, "all_sources_failed")
        cache_write.assert_not_called()

    async def test_same_key_searches_coalesce_before_cache_write(self):
        started = asyncio.Event()
        release = asyncio.Event()
        item = candidates.SourceCandidate(
            "tmdb",
            "https://image.tmdb.org/t/p/original/clean.jpg",
            600,
            900,
            None,
        )

        async def identity(*_args):
            started.set()
            await release.wait()
            return candidates._Identity(("Title",), True)

        with (
            patch.object(candidates, "_fetch_identity", side_effect=identity) as identity_mock,
            patch.object(candidates, "_tmdb_candidates", AsyncMock(return_value=[item])),
            patch.object(candidates, "_fanart_candidates", AsyncMock(return_value=[])),
            patch.object(
                candidates,
                "_fetch_screening_image",
                AsyncMock(return_value=(
                    b"clean",
                    Image.new("RGBA", (600, 900), "black"),
                    600,
                    900,
                )),
            ),
            patch.object(candidates, "_ocr_has_text", AsyncMock(return_value=False)),
            patch.object(candidates, "get_cached_artwork_candidates", return_value=None),
            patch.object(candidates, "set_cached_artwork_candidates", return_value=True) as cache_write,
            patch.object(candidates, "text_detection_ready", return_value=True),
        ):
            first = asyncio.create_task(
                candidates.discover_candidates(Mock(), _request("series", "456"), settings=_settings())
            )
            await started.wait()
            second = asyncio.create_task(
                candidates.discover_candidates(Mock(), _request("series", "456"), settings=_settings())
            )
            await asyncio.sleep(0)
            self.assertEqual(identity_mock.await_count, 1)
            release.set()
            first_result, second_result = await asyncio.gather(first, second)
        self.assertEqual(first_result, second_result)
        self.assertEqual(cache_write.call_count, 1)
        self.assertEqual(cache_write.call_args.args[2], "complete")
        self.assertEqual(candidates._search_admission.active, 0)

    async def test_cache_io_runs_off_loop_and_deadline_cannot_return_success(self):
        loop_thread = threading.get_ident()
        cache_threads = []

        def cache_read(*_args, **_kwargs):
            cache_threads.append(threading.get_ident())
            return None

        def cache_write(*_args, **_kwargs):
            cache_threads.append(threading.get_ident())
            return True

        with (
            patch.object(
                candidates,
                "_fetch_identity",
                AsyncMock(return_value=candidates._Identity(("Title",), True)),
            ),
            patch.object(candidates, "_tmdb_candidates", AsyncMock(return_value=[])),
            patch.object(candidates, "_fanart_candidates", AsyncMock(return_value=[])),
            patch.object(candidates, "get_cached_artwork_candidates", side_effect=cache_read),
            patch.object(candidates, "set_cached_artwork_candidates", side_effect=cache_write),
            patch.object(candidates, "text_detection_ready", return_value=True),
        ):
            await candidates.discover_candidates(
                Mock(),
                _request("series", "456"),
                settings=_settings(),
            )
        self.assertEqual(len(cache_threads), 2)
        self.assertTrue(all(thread_id != loop_thread for thread_id in cache_threads))

        def slow_cache_write(*_args, **_kwargs):
            time.sleep(0.05)
            return True

        with (
            patch.object(candidates, "SEARCH_DEADLINE_SECONDS", 0.005),
            patch.object(
                candidates,
                "_fetch_identity",
                AsyncMock(return_value=candidates._Identity(("Title",), True)),
            ),
            patch.object(candidates, "_tmdb_candidates", AsyncMock(return_value=[])),
            patch.object(candidates, "_fanart_candidates", AsyncMock(return_value=[])),
            patch.object(candidates, "get_cached_artwork_candidates", return_value=None),
            patch.object(candidates, "set_cached_artwork_candidates", side_effect=slow_cache_write),
            patch.object(candidates, "text_detection_ready", return_value=True),
        ):
            with self.assertRaises(candidates.CandidateDiscoveryError) as raised:
                await candidates.discover_candidates(
                    Mock(),
                    candidates.CandidateRequest("series", "999", None, "456"),
                    settings=_settings(),
                )
        self.assertEqual(raised.exception.code, "deadline_exceeded")
        await asyncio.sleep(0.06)

    async def test_timed_out_ocr_holds_admission_and_rejects_new_scan(self):
        release = threading.Event()
        started = threading.Event()
        item = candidates.SourceCandidate(
            "tmdb",
            "https://image.tmdb.org/t/p/original/clean.jpg",
            600,
            900,
            None,
        )

        def blocking_ocr(*_args, **_kwargs):
            started.set()
            release.wait(2)
            return False

        async def fetch(*_args):
            return (
                b"clean",
                Image.new("RGBA", (600, 900), "black"),
                600,
                900,
            )

        with (
            patch.object(candidates, "SEARCH_DEADLINE_SECONDS", 0.02),
            patch.object(
                candidates,
                "_fetch_identity",
                AsyncMock(return_value=candidates._Identity(("Title",), False)),
            ) as identity,
            patch.object(candidates, "_tmdb_candidates", AsyncMock(return_value=[item])),
            patch.object(candidates, "_fetch_screening_image", side_effect=fetch),
            patch.object(candidates, "get_cached_artwork_candidates", return_value=None),
            patch.object(candidates, "set_cached_artwork_candidates", return_value=True),
            patch.object(candidates, "get_cached_text_detection", return_value=None),
            patch.object(candidates, "set_cached_text_detection"),
            patch.object(candidates, "text_detection_ready", return_value=True),
            patch.object(candidates, "poster_has_burned_in_text", side_effect=blocking_ocr) as ocr,
        ):
            with self.assertRaises(candidates.CandidateDiscoveryError) as first:
                await candidates.discover_candidates(
                    Mock(),
                    _request(imdb_id=None),
                    settings=_settings(
                        fanart_project_api_key="",
                        fanart_client_key="",
                    ),
                )
            self.assertEqual(first.exception.code, "deadline_exceeded")
            self.assertTrue(started.is_set())
            self.assertEqual(candidates._search_admission.active, 1)

            with self.assertRaises(candidates.CandidateDiscoveryError) as second:
                await candidates.discover_candidates(
                    Mock(),
                    candidates.CandidateRequest("movie", "999", None, None),
                    settings=_settings(
                        fanart_project_api_key="",
                        fanart_client_key="",
                    ),
                )
            self.assertEqual(second.exception.code, "text_detection_unavailable")
            self.assertEqual(ocr.call_count, 1)
            self.assertEqual(identity.await_count, 1)
            self.assertEqual(candidates._search_admission.active, 1)

            release.set()
            for _ in range(100):
                if candidates._search_admission.active == 0:
                    break
                await asyncio.sleep(0.01)
        self.assertEqual(candidates._search_admission.active, 0)

    async def test_unavailable_fanart_contract_reports_failed(self):
        with (
            patch.object(
                candidates,
                "_fetch_identity",
                AsyncMock(return_value=candidates._Identity(("Title",), False)),
            ),
            patch.object(candidates, "_tmdb_candidates", AsyncMock(return_value=[])),
            patch.object(candidates, "_fanart_candidates", AsyncMock()) as fanart,
            patch.object(candidates, "get_cached_artwork_candidates", return_value=None),
            patch.object(candidates, "set_cached_artwork_candidates"),
            patch.object(candidates, "text_detection_ready", return_value=True),
        ):
            response, outcome = await candidates.discover_candidates(
                Mock(),
                _request("series"),
                settings=_settings(),
            )
            missing_keys_response, missing_keys_outcome = (
                await candidates.discover_candidates(
                    Mock(),
                    _request("movie"),
                    settings=_settings(
                        fanart_project_api_key="",
                        fanart_client_key="",
                    ),
                )
            )
        self.assertEqual(
            response["sources"],
            {"tmdb": "ready", "fanart": "failed"},
        )
        self.assertEqual(outcome, "partial")
        self.assertEqual(
            missing_keys_response["sources"],
            {"tmdb": "ready", "fanart": "failed"},
        )
        self.assertEqual(missing_keys_outcome, "partial")
        fanart.assert_not_awaited()

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
            result, reliable = await candidates._screen_source(
                Mock(),
                values,
                titles=("Title",),
                settings=_settings(),
                seen_digests=set(),
                work=work,
            )
        self.assertEqual(result, [])
        self.assertTrue(reliable)
        self.assertEqual(work["ocr"], candidates.MAX_OCR_ATTEMPTS)
        self.assertEqual(ocr.await_count, candidates.MAX_OCR_ATTEMPTS)

    async def test_valid_result_cache_skips_all_upstream_work(self):
        cached = {
            "schema_version": 1,
            "sources": {"tmdb": "ready", "fanart": "failed"},
            "candidates": [{
                "source": "tmdb",
                "url": "https://image.tmdb.org/t/p/w500/clean.jpg",
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

    def test_cached_contract_rejects_legacy_statuses_and_extra_candidate_fields(self):
        base = {
            "schema_version": 1,
            "sources": {"tmdb": "ready", "fanart": "failed"},
            "candidates": [{
                "source": "tmdb",
                "url": "https://image.tmdb.org/t/p/w500/clean.jpg",
                "width": 600,
                "height": 900,
                "language": None,
            }],
        }
        legacy = json.loads(json.dumps(base))
        legacy["sources"]["fanart"] = "disabled"
        self.assertIsNone(
            candidates._validate_cached_response(
                legacy,
                _request(),
                _settings(),
            )
        )
        extra = json.loads(json.dumps(base))
        extra["candidates"][0]["sha256"] = "a" * 64
        self.assertIsNone(
            candidates._validate_cached_response(
                extra,
                _request(),
                _settings(),
            )
        )

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
            "sources": {"tmdb": "ready", "fanart": "failed"},
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

    def test_candidate_cache_write_rolls_back_after_deadline(self):
        response = {
            "schema_version": 1,
            "sources": {"tmdb": "ready", "fanart": "failed"},
            "candidates": [],
        }
        with (
            patch.object(cache, "get_db", return_value=self.connection),
            patch.object(cache.time, "monotonic", side_effect=[0.0, 2.0]),
        ):
            written = cache.set_cached_artwork_candidates(
                "late",
                response,
                "partial",
                deadline_monotonic=1.0,
            )
        self.assertFalse(written)
        self.assertIsNone(
            self.connection.execute(
                "SELECT cache_key FROM artwork_candidate_cache WHERE cache_key = 'late'"
            ).fetchone()
        )

    def test_partial_write_cannot_overwrite_fresh_complete_result(self):
        complete = {
            "schema_version": 1,
            "sources": {"tmdb": "ready", "fanart": "ready"},
            "candidates": [],
        }
        partial = {
            "schema_version": 1,
            "sources": {"tmdb": "ready", "fanart": "failed"},
            "candidates": [],
        }
        with (
            patch.object(cache, "get_db", return_value=self.connection),
            patch.object(cache.time, "time", side_effect=[1000, 1001]),
        ):
            cache.set_cached_artwork_candidates("same", complete, "complete")
            cache.set_cached_artwork_candidates("same", partial, "partial")
        row = self.connection.execute(
            """
            SELECT response_json, result_class
            FROM artwork_candidate_cache
            WHERE cache_key = 'same'
            """
        ).fetchone()
        self.assertEqual(json.loads(row[0]), complete)
        self.assertEqual(row[1], "complete")


class DiscoveryCredentialTests(unittest.TestCase):
    def test_unset_explicit_key_derives_domain_separated_hmac(self):
        expected = hmac.new(
            b"render-secret",
            b"jellyfin-artwork-discovery-v1",
            hashlib.sha256,
        ).hexdigest()
        with (
            patch.object(main._cfg, "ACCESS_KEY", "render-secret"),
            patch.object(main._cfg, "JELLYFIN_ARTWORK_DISCOVERY_KEY", ""),
        ):
            self.assertEqual(candidates.effective_discovery_key(), expected)
            self.assertTrue(candidates.discovery_key_matches(expected))
            self.assertFalse(candidates.discovery_key_matches("render-secret"))
            self.assertTrue(main._access_key_matches("render-secret"))
            self.assertFalse(main._access_key_matches(expected))

    def test_explicit_key_overrides_derivation_and_supports_standalone(self):
        derived = hmac.new(
            b"render-secret",
            b"jellyfin-artwork-discovery-v1",
            hashlib.sha256,
        ).hexdigest()
        with (
            patch.object(main._cfg, "ACCESS_KEY", "render-secret"),
            patch.object(main._cfg, "JELLYFIN_ARTWORK_DISCOVERY_KEY", "dedicated"),
        ):
            self.assertEqual(candidates.effective_discovery_key(), "dedicated")
            self.assertTrue(candidates.discovery_key_matches("dedicated"))
            self.assertFalse(candidates.discovery_key_matches(derived))

        with (
            patch.object(main._cfg, "ACCESS_KEY", None),
            patch.object(main._cfg, "JELLYFIN_ARTWORK_DISCOVERY_KEY", "standalone"),
        ):
            self.assertEqual(candidates.effective_discovery_key(), "standalone")
            self.assertTrue(candidates.discovery_key_matches("standalone"))


class EndpointContractTests(unittest.IsolatedAsyncioTestCase):
    async def _post(self, headers=None, params="", body=None):
        transport = httpx.ASGITransport(app=main.app)
        if body is None:
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

    async def test_endpoint_accepts_tmdb_only_and_explicit_nulls(self):
        success = {
            "schema_version": 1,
            "sources": {"tmdb": "ready", "fanart": "failed"},
            "candidates": [],
        }
        discover = AsyncMock(return_value=(success, "partial"))
        with (
            patch.object(main._cfg, "JELLYFIN_ARTWORK_DISCOVERY_KEY", "discover"),
            patch.object(main._cfg, "ACCESS_KEY", "render"),
            patch.object(main, "_HTTP_CLIENT", Mock()),
            patch.object(main, "discover_candidates", discover),
        ):
            tmdb_only = await self._post(
                {"X-Jellyfin-Artwork-Discovery-Key": "discover"},
                body={
                    "schema_version": 1,
                    "type": "movie",
                    "tmdb_id": "123",
                },
            )
            explicit_nulls = await self._post(
                {"X-Jellyfin-Artwork-Discovery-Key": "discover"},
                body={
                    "schema_version": 1,
                    "type": "series",
                    "tmdb_id": "123",
                    "imdb_id": None,
                    "tvdb_id": None,
                },
            )
        self.assertEqual(tmdb_only.status_code, 200)
        self.assertEqual(explicit_nulls.status_code, 200)
        first_request = discover.await_args_list[0].args[1]
        second_request = discover.await_args_list[1].args[1]
        self.assertIsNone(first_request.imdb_id)
        self.assertIsNone(first_request.tvdb_id)
        self.assertIsNone(second_request.imdb_id)
        self.assertIsNone(second_request.tvdb_id)

    async def test_discovery_auth_is_separate_from_render_auth(self):
        success = {
            "schema_version": 1,
            "sources": {"tmdb": "ready", "fanart": "failed"},
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
                    "/render/selection?profile=x&tmdb_id=123&imdb_id=tt1234567",
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

    async def test_derived_discovery_key_cannot_authenticate_render_routes(self):
        success = {
            "schema_version": 1,
            "sources": {"tmdb": "ready", "fanart": "failed"},
            "candidates": [],
        }
        derived = hmac.new(
            b"render-secret",
            b"jellyfin-artwork-discovery-v1",
            hashlib.sha256,
        ).hexdigest()
        with (
            patch.object(main._cfg, "JELLYFIN_ARTWORK_DISCOVERY_KEY", ""),
            patch.object(main._cfg, "ACCESS_KEY", "render-secret"),
            patch.object(main, "_HTTP_CLIENT", Mock()),
            patch.object(main, "_text_detector_ready", return_value=True),
            patch.object(
                main,
                "discover_candidates",
                AsyncMock(return_value=(success, "partial")),
            ),
        ):
            discovery = await self._post({
                "X-Jellyfin-Artwork-Discovery-Key": derived,
            })
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                render_with_access = await client.get(
                    "/ready",
                    headers={"X-Jellyfin-Artwork-Key": "render-secret"},
                )
                render_with_derived = await client.get(
                    "/ready",
                    headers={"X-Jellyfin-Artwork-Key": derived},
                )
        self.assertEqual(discovery.status_code, 200)
        self.assertEqual(render_with_access.status_code, 503)
        self.assertEqual(render_with_derived.status_code, 403)

    async def test_discovery_auth_is_enforced_with_asgi_root_path(self):
        success = {
            "schema_version": 1,
            "sources": {"tmdb": "ready", "fanart": "failed"},
            "candidates": [],
        }
        with (
            patch.object(main._cfg, "JELLYFIN_ARTWORK_DISCOVERY_KEY", "discover"),
            patch.object(main._cfg, "ACCESS_KEY", "render"),
            patch.object(main, "_HTTP_CLIENT", Mock()),
            patch.object(
                main,
                "discover_candidates",
                AsyncMock(return_value=(success, "partial")),
            ),
        ):
            transport = httpx.ASGITransport(
                app=main.app,
                root_path="/mounted",
            )
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test/mounted",
            ) as client:
                denied = await client.post(
                    "/v1/artwork/candidates",
                    json={
                        "schema_version": 1,
                        "type": "movie",
                        "tmdb_id": "123",
                    },
                )
                allowed = await client.post(
                    "/v1/artwork/candidates",
                    json={
                        "schema_version": 1,
                        "type": "movie",
                        "tmdb_id": "123",
                    },
                    headers={
                        "X-Jellyfin-Artwork-Discovery-Key": "discover",
                    },
                )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.headers["cache-control"], "no-store")

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


class TypedSchemaTests(unittest.TestCase):
    def test_schema_two_requires_explicit_image_type(self):
        primary = candidates.decode_request_body(json.dumps({
            "schema_version": 2,
            "type": "movie",
            "tmdb_id": "123",
            "image_type": "Primary",
        }).encode())
        self.assertEqual(primary.schema_version, 2)
        self.assertEqual(primary.image_type, "Primary")
        self.assertFalse(primary.is_logo)

        logo = candidates.decode_request_body(json.dumps({
            "schema_version": 2,
            "type": "series",
            "tmdb_id": "123",
            "tvdb_id": "456",
            "image_type": "Logo",
        }).encode())
        self.assertEqual(logo.schema_version, 2)
        self.assertEqual(logo.image_type, "Logo")
        self.assertTrue(logo.is_logo)
        self.assertEqual(logo.tvdb_id, "456")

    def test_schema_one_stays_primary_only_and_rejects_image_type(self):
        parsed = candidates.decode_request_body(json.dumps({
            "schema_version": 1,
            "type": "movie",
            "tmdb_id": "123",
        }).encode())
        self.assertEqual(parsed.schema_version, 1)
        self.assertEqual(parsed.image_type, "Primary")

        with self.assertRaises(candidates.CandidateDiscoveryError):
            candidates.decode_request_body(json.dumps({
                "schema_version": 1,
                "type": "movie",
                "tmdb_id": "123",
                "image_type": "Primary",
            }).encode())

    def test_schema_two_rejects_missing_bad_or_case_variant_image_type(self):
        invalid = (
            b'{"schema_version":2,"type":"movie","tmdb_id":"123"}',
            b'{"schema_version":2,"type":"movie","tmdb_id":"123","image_type":"logo"}',
            b'{"schema_version":2,"type":"movie","tmdb_id":"123","image_type":"PRIMARY"}',
            b'{"schema_version":2,"type":"movie","tmdb_id":"123","image_type":"Banner"}',
            b'{"schema_version":2,"type":"movie","tmdb_id":"123","image_type":1}',
            b'{"schema_version":2,"type":"movie","tmdb_id":"123","image_type":null}',
            b'{"schema_version":2,"type":"movie","tmdb_id":"123","image_type":"Logo","limit":8}',
            b'{"schema_version":3,"type":"movie","tmdb_id":"123","image_type":"Logo"}',
        )
        for body in invalid:
            with self.subTest(body=body):
                with self.assertRaises(candidates.CandidateDiscoveryError):
                    candidates.decode_request_body(body)

    def test_image_type_error_is_typed(self):
        with self.assertRaises(candidates.CandidateDiscoveryError) as raised:
            candidates.decode_request_body(json.dumps({
                "schema_version": 2,
                "type": "movie",
                "tmdb_id": "123",
                "image_type": "logo",
            }).encode())
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.code, "invalid_image_type")


class LogoUrlPolicyTests(unittest.TestCase):
    def test_movie_and_tv_logo_resources_are_canonical(self):
        movie = candidates.canonical_fanart_url(
            "https://assets.fanart.tv/fanart/movies/123/hdmovielogo/x.png",
            media_type="movie",
            tmdb_id="123",
            tvdb_id=None,
            resource="hdmovielogo",
        )
        self.assertTrue(movie.endswith("/hdmovielogo/x.png"))
        tv = candidates.canonical_fanart_url(
            "https://assets.fanart.tv/fanart/tv/456/clearlogo/y.png",
            media_type="series",
            tmdb_id="123",
            tvdb_id="456",
            resource="clearlogo",
        )
        self.assertTrue(tv.endswith("/clearlogo/y.png"))

    def test_logo_resource_mismatch_and_cross_media_are_rejected(self):
        cases = (
            # path segment does not match the requested resource
            dict(
                url="https://assets.fanart.tv/fanart/movies/123/hdmovielogo/x.png",
                media_type="movie",
                tmdb_id="123",
                tvdb_id=None,
                resource="movieposter",
            ),
            # tv logo resource requested for a movie
            dict(
                url="https://assets.fanart.tv/fanart/movies/123/hdtvlogo/x.png",
                media_type="movie",
                tmdb_id="123",
                tvdb_id=None,
                resource="hdtvlogo",
            ),
            # unknown resource segment
            dict(
                url="https://assets.fanart.tv/fanart/movies/123/evil/x.png",
                media_type="movie",
                tmdb_id="123",
                tvdb_id=None,
                resource="evil",
            ),
        )
        for case in cases:
            with self.subTest(resource=case["resource"]):
                with self.assertRaises(candidates._CandidateRejected):
                    candidates.canonical_fanart_url(
                        case["url"],
                        media_type=case["media_type"],
                        tmdb_id=case["tmdb_id"],
                        tvdb_id=case["tvdb_id"],
                        resource=case["resource"],
                    )

    def test_landscape_aspect_gate(self):
        self.assertTrue(candidates._is_landscape(800, 310))
        self.assertFalse(candidates._is_landscape(600, 900))
        self.assertFalse(candidates._is_landscape(500, 500))
        self.assertFalse(candidates._is_landscape(4000, 100))


def _logo_request(media_type="movie", tvdb_id=None, imdb_id="tt1234567"):
    return candidates.CandidateRequest(
        media_type=media_type,
        tmdb_id="123",
        imdb_id=imdb_id,
        tvdb_id=tvdb_id,
        schema_version=2,
        image_type="Logo",
    )


class LogoAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_tmdb_logos_filter_landscape_skip_svg_rank_and_cap(self):
        logos = [
            {
                "file_path": f"/logo-{index}.png",
                "iso_639_1": "en",
                "width": 400 + index,
                "height": 155,
                "vote_average": 5 + index / 10,
                "vote_count": index,
            }
            for index in range(10)
        ]
        logos.extend([
            {"file_path": "/vector.svg", "iso_639_1": None, "width": 800, "height": 310},
            {"file_path": "/portrait.png", "iso_639_1": None, "width": 300, "height": 900},
            {"file_path": "/neutral.png", "iso_639_1": None, "width": 900, "height": 320},
        ])

        async def handler(request):
            self.assertEqual(request.url.params["api_key"], "tmdb-secret")
            self.assertEqual(request.url.params["include_image_language"], "en,null")
            return _json_response({"logos": logos})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            values = await candidates._tmdb_logo_candidates(
                client,
                _logo_request(),
                _settings(),
            )
        self.assertEqual(len(values), 8)
        self.assertTrue(all(v.image_type == "Logo" for v in values))
        self.assertTrue(all(v.source == "tmdb" for v in values))
        self.assertTrue(all("/t/p/w500/" in v.url for v in values))
        # Largest by pixel area ranks first; the 900x320 neutral logo wins.
        self.assertEqual(values[0].url.rsplit("/", 1)[-1], "neutral.png")
        self.assertIsNone(values[0].language)
        # No SVG or portrait leaked in.
        joined = " ".join(v.url for v in values)
        self.assertNotIn("vector.svg", joined)
        self.assertNotIn("portrait.png", joined)
        # Language-tagged logos preserve their language.
        english = [v for v in values if v.url.endswith("logo-9.png")]
        self.assertEqual(english[0].language, "en")

    async def test_fanart_logos_prefer_hd_dedupe_and_normalize_lang(self):
        payload = {
            "hdmovielogo": [
                {
                    "url": "https://assets.fanart.tv/fanart/movies/123/hdmovielogo/a.png",
                    "lang": "en",
                    "likes": "5",
                    "width": 400,
                    "height": 155,
                },
                {
                    # exact duplicate URL is de-duplicated
                    "url": "https://assets.fanart.tv/fanart/movies/123/hdmovielogo/a.png",
                    "lang": "en",
                    "likes": "5",
                    "width": 400,
                    "height": 155,
                },
            ],
            "movielogo": [
                {
                    # larger, but a lower-quality fallback resource
                    "url": "https://assets.fanart.tv/fanart/movies/123/movielogo/b.png",
                    "lang": "00",
                    "likes": "99",
                    "width": 800,
                    "height": 310,
                },
            ],
        }

        async def handler(request):
            self.assertEqual(request.url.path, "/v3/movies/123")
            return _json_response(payload)

        identity = candidates._Identity(("Title",), True)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            values = await candidates._fanart_logo_candidates(
                client,
                _logo_request(),
                identity,
                _settings(),
            )
        self.assertEqual(len(values), 2)
        # HD-specific resource ranks ahead of the larger fallback logo.
        self.assertTrue(values[0].url.endswith("/hdmovielogo/a.png"))
        self.assertEqual(values[0].tier, 0)
        self.assertEqual(values[0].language, "en")
        self.assertTrue(values[1].url.endswith("/movielogo/b.png"))
        self.assertEqual(values[1].tier, 1)
        # Fanart neutral marker "00" normalizes to null language.
        self.assertIsNone(values[1].language)
        self.assertTrue(all(v.image_type == "Logo" for v in values))

    async def test_fanart_series_logos_use_tv_resources(self):
        payload = {
            "hdtvlogo": [
                {
                    "url": "https://assets.fanart.tv/fanart/tv/456/hdtvlogo/a.png",
                    "lang": "en",
                    "likes": "5",
                    "width": 800,
                    "height": 310,
                },
            ],
            "clearlogo": [
                {
                    "url": "https://assets.fanart.tv/fanart/tv/456/clearlogo/b.png",
                    "lang": "en",
                    "likes": "5",
                    "width": 800,
                    "height": 310,
                },
            ],
        }

        async def handler(request):
            self.assertEqual(request.url.path, "/v3/tv/456")
            return _json_response(payload)

        identity = candidates._Identity(("Title",), True)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            values = await candidates._fanart_logo_candidates(
                client,
                _logo_request("series", "456"),
                identity,
                _settings(),
            )
        self.assertEqual(
            [v.url.rsplit("/", 2)[-2] for v in values],
            ["hdtvlogo", "clearlogo"],
        )

    async def test_series_fanart_logos_require_verified_tvdb(self):
        handler = AsyncMock()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            values = await candidates._fanart_logo_candidates(
                client,
                _logo_request("series"),
                candidates._Identity(("Title",), False),
                _settings(),
            )
        self.assertEqual(values, [])
        handler.assert_not_awaited()


class LogoScreeningTests(unittest.IsolatedAsyncioTestCase):
    async def test_screen_source_skips_ocr_for_logos(self):
        logos = [
            candidates.SourceCandidate(
                "tmdb",
                f"https://image.tmdb.org/t/p/original/logo-{index}.png",
                800,
                310,
                "en",
                image_type="Logo",
            )
            for index in range(2)
        ]

        async def fetch(_client, item, _settings):
            return (
                item.url.encode(),
                Image.new("RGBA", (800, 310), "black"),
                800,
                310,
            )

        ocr = AsyncMock(return_value=True)
        with (
            patch.object(candidates, "_fetch_screening_image", side_effect=fetch),
            patch.object(candidates, "_ocr_has_text", ocr),
        ):
            accepted, reliable = await candidates._screen_source(
                Mock(),
                logos,
                titles=("Title",),
                settings=_settings(),
                seen_digests=set(),
                work={"ocr": 0},
            )
        self.assertEqual(len(accepted), 2)
        self.assertTrue(reliable)
        self.assertTrue(all(c.sha256 for c in accepted))
        ocr.assert_not_awaited()

    def test_logo_decoder_enforces_landscape_window(self):
        settings = _settings()
        image, width, height = candidates._decode_screening_image(
            _image_bytes(size=(800, 310), image_format="PNG"),
            "image/png",
            settings,
            image_type="Logo",
        )
        image.close()
        self.assertEqual((width, height), (800, 310))
        with self.assertRaises(candidates._CandidateRejected):
            candidates._decode_screening_image(
                _image_bytes(size=(600, 900)),
                "image/jpeg",
                settings,
                image_type="Logo",
            )


class TypedDiscoveryPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        candidates._search_admission.reset_for_tests()
        candidates._candidate_inflight.clear()
        candidates._ocr_worker.reset_for_tests()

    async def _discover_logo(self, *, tmdb_values, fanart_values, text_ready=False):
        ocr = AsyncMock(return_value=False)
        with (
            patch.object(
                candidates,
                "_fetch_identity",
                AsyncMock(return_value=candidates._Identity(("Title",), True)),
            ),
            patch.object(
                candidates,
                "_tmdb_logo_candidates",
                AsyncMock(return_value=tmdb_values),
            ),
            patch.object(
                candidates,
                "_fanart_logo_candidates",
                AsyncMock(return_value=fanart_values),
            ),
            patch.object(candidates, "get_cached_artwork_candidates", return_value=None),
            patch.object(candidates, "set_cached_artwork_candidates") as cache_write,
            patch.object(candidates, "text_detection_ready", return_value=text_ready),
            patch.object(
                candidates,
                "_fetch_screening_image",
                AsyncMock(side_effect=lambda _client, item, _settings: (
                    item.url.encode(),
                    Image.new("RGBA", (item.width, item.height), "black"),
                    item.width,
                    item.height,
                )),
            ),
            patch.object(candidates, "_ocr_has_text", ocr),
        ):
            result = await candidates.discover_candidates(
                Mock(),
                _logo_request("series", "456"),
                settings=_settings(),
            )
        return result, cache_write, ocr

    async def test_logo_discovery_returns_typed_v2_response_without_ocr(self):
        tmdb_values = [
            candidates.SourceCandidate(
                "tmdb",
                f"https://image.tmdb.org/t/p/original/tmdb-{index}.png",
                800,
                310,
                "en",
                rating=10 - index,
                votes=100 - index,
                ordinal=index,
                image_type="Logo",
            )
            for index in range(2)
        ]
        fanart_values = [
            candidates.SourceCandidate(
                "fanart",
                f"https://assets.fanart.tv/fanart/tv/456/hdtvlogo/fanart-{index}.png",
                800,
                310,
                None,
                likes=50 - index,
                ordinal=index,
                image_type="Logo",
            )
            for index in range(2)
        ]
        # text detection deliberately unavailable to prove Logo never gates on OCR
        (response, outcome), cache_write, ocr = await self._discover_logo(
            tmdb_values=tmdb_values,
            fanart_values=fanart_values,
            text_ready=False,
        )
        self.assertEqual(outcome, "complete")
        self.assertEqual(response["schema_version"], 2)
        self.assertEqual(response["image_type"], "Logo")
        self.assertEqual(
            set(response),
            {"schema_version", "image_type", "sources", "candidates"},
        )
        self.assertEqual(set(response["sources"]), {"tmdb", "fanart"})
        self.assertEqual(len(response["candidates"]), 4)
        self.assertEqual(
            [c["source"] for c in response["candidates"]],
            ["tmdb", "fanart"] * 2,
        )
        self.assertTrue(all(
            set(c) == {"source", "url", "width", "height", "language"}
            for c in response["candidates"]
        ))
        # Logos are landscape and may carry language text.
        self.assertTrue(all(c["width"] > c["height"] for c in response["candidates"]))
        ocr.assert_not_awaited()
        cache_write.assert_called_once()
        self.assertEqual(cache_write.call_args.args[2], "complete")

    async def test_v2_primary_preserves_portrait_behaviour(self):
        item = candidates.SourceCandidate(
            "tmdb",
            "https://image.tmdb.org/t/p/original/clean.jpg",
            600,
            900,
            None,
        )
        with (
            patch.object(
                candidates,
                "_fetch_identity",
                AsyncMock(return_value=candidates._Identity(("Title",), False)),
            ),
            patch.object(candidates, "_tmdb_candidates", AsyncMock(return_value=[item])),
            patch.object(candidates, "_fanart_candidates", AsyncMock(return_value=[])),
            patch.object(candidates, "get_cached_artwork_candidates", return_value=None),
            patch.object(candidates, "set_cached_artwork_candidates"),
            patch.object(candidates, "text_detection_ready", return_value=True),
            patch.object(
                candidates,
                "_fetch_screening_image",
                AsyncMock(return_value=(
                    b"clean",
                    Image.new("RGBA", (600, 900), "black"),
                    600,
                    900,
                )),
            ),
            patch.object(candidates, "_ocr_has_text", AsyncMock(return_value=False)) as ocr,
        ):
            request = candidates.CandidateRequest(
                media_type="movie",
                tmdb_id="123",
                imdb_id=None,
                tvdb_id=None,
                schema_version=2,
                image_type="Primary",
            )
            response, outcome = await candidates.discover_candidates(
                Mock(),
                request,
                settings=_settings(),
            )
        self.assertEqual(outcome, "complete")
        self.assertEqual(response["schema_version"], 2)
        self.assertEqual(response["image_type"], "Primary")
        self.assertEqual(len(response["candidates"]), 1)
        self.assertEqual(response["candidates"][0]["language"], None)
        ocr.assert_awaited()

    async def test_v2_primary_still_requires_text_detection(self):
        with (
            patch.object(candidates, "get_cached_artwork_candidates", return_value=None),
            patch.object(candidates, "text_detection_ready", return_value=False),
            patch.object(candidates, "_fetch_identity", AsyncMock()) as identity,
        ):
            request = candidates.CandidateRequest(
                media_type="movie",
                tmdb_id="123",
                imdb_id=None,
                tvdb_id=None,
                schema_version=2,
                image_type="Primary",
            )
            with self.assertRaises(candidates.CandidateDiscoveryError) as raised:
                await candidates.discover_candidates(
                    Mock(),
                    request,
                    settings=_settings(),
                )
        self.assertEqual(raised.exception.code, "text_detection_unavailable")
        identity.assert_not_awaited()

    def test_logo_cache_contract_validates_landscape_and_language(self):
        request = _logo_request("series", "456")
        valid = {
            "schema_version": 2,
            "image_type": "Logo",
            "sources": {"tmdb": "ready", "fanart": "ready"},
            "candidates": [
                {
                    "source": "tmdb",
                    "url": "https://image.tmdb.org/t/p/w500/logo.png",
                    "width": 800,
                    "height": 310,
                    "language": "en",
                },
                {
                    "source": "fanart",
                    "url": "https://assets.fanart.tv/fanart/tv/456/clearlogo/b.png",
                    "width": 800,
                    "height": 310,
                    "language": None,
                },
            ],
        }
        self.assertEqual(
            candidates._validate_cached_response(
                json.loads(json.dumps(valid)),
                request,
                _settings(),
            ),
            valid,
        )
        # A portrait logo candidate is rejected.
        portrait = json.loads(json.dumps(valid))
        portrait["candidates"][0]["width"] = 300
        portrait["candidates"][0]["height"] = 900
        self.assertIsNone(
            candidates._validate_cached_response(portrait, request, _settings())
        )
        # A v1-shaped cache (no image_type) does not satisfy a v2 request.
        wrong_shape = json.loads(json.dumps(valid))
        wrong_shape.pop("image_type")
        wrong_shape["schema_version"] = 1
        self.assertIsNone(
            candidates._validate_cached_response(wrong_shape, request, _settings())
        )
        # A Primary request must not accept a landscape (logo) cache.
        primary_request = _request("series", "456")
        self.assertIsNone(
            candidates._validate_cached_response(
                json.loads(json.dumps(valid)),
                primary_request,
                _settings(),
            )
        )


class TypedEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def _post(self, body, headers=None, params=""):
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.post(
                f"/v1/artwork/candidates{params}",
                json=body,
                headers=headers,
            )

    async def test_endpoint_dispatches_typed_logo_request(self):
        success = {
            "schema_version": 2,
            "image_type": "Logo",
            "sources": {"tmdb": "ready", "fanart": "ready"},
            "candidates": [],
        }
        discover = AsyncMock(return_value=(success, "empty"))
        with (
            patch.object(main._cfg, "JELLYFIN_ARTWORK_DISCOVERY_KEY", "discover"),
            patch.object(main._cfg, "ACCESS_KEY", "render"),
            patch.object(main, "_HTTP_CLIENT", Mock()),
            patch.object(main, "discover_candidates", discover),
        ):
            response = await self._post(
                {
                    "schema_version": 2,
                    "type": "series",
                    "tmdb_id": "123",
                    "tvdb_id": "456",
                    "image_type": "Logo",
                },
                headers={"X-Jellyfin-Artwork-Discovery-Key": "discover"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["image_type"], "Logo")
        dispatched = discover.await_args_list[0].args[1]
        self.assertEqual(dispatched.image_type, "Logo")
        self.assertEqual(dispatched.schema_version, 2)

    async def test_endpoint_rejects_bad_image_type(self):
        discover = AsyncMock()
        with (
            patch.object(main._cfg, "JELLYFIN_ARTWORK_DISCOVERY_KEY", "discover"),
            patch.object(main, "_HTTP_CLIENT", Mock()),
            patch.object(main, "discover_candidates", discover),
        ):
            response = await self._post(
                {
                    "schema_version": 2,
                    "type": "movie",
                    "tmdb_id": "123",
                    "image_type": "logo",
                },
                headers={"X-Jellyfin-Artwork-Discovery-Key": "discover"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_image_type")
        discover.assert_not_awaited()


class TypedResponseContractTests(unittest.TestCase):
    def _selected(self, image_type):
        return [
            candidates.SourceCandidate(
                "tmdb",
                "https://image.tmdb.org/t/p/original/x.jpg",
                800 if image_type == "Logo" else 600,
                310 if image_type == "Logo" else 900,
                "en" if image_type == "Logo" else None,
                image_type=image_type,
            )
        ]

    def test_v2_response_is_self_describing_with_exact_top_level_fields(self):
        for image_type in ("Primary", "Logo"):
            with self.subTest(image_type=image_type):
                request = candidates.CandidateRequest(
                    media_type="series",
                    tmdb_id="123",
                    imdb_id=None,
                    tvdb_id="456",
                    schema_version=2,
                    image_type=image_type,
                )
                response = candidates._build_response(
                    request,
                    {"tmdb": "ready", "fanart": "ready"},
                    self._selected(image_type),
                )
                self.assertEqual(
                    list(response),
                    ["schema_version", "image_type", "sources", "candidates"],
                )
                self.assertEqual(response["schema_version"], 2)
                # image_type in the response always equals the v2 request.
                self.assertEqual(response["image_type"], image_type)

    def test_v1_response_shape_is_unchanged(self):
        request = candidates.CandidateRequest(
            media_type="movie",
            tmdb_id="123",
            imdb_id=None,
            tvdb_id=None,
        )
        response = candidates._build_response(
            request,
            {"tmdb": "ready", "fanart": "failed"},
            self._selected("Primary"),
        )
        self.assertEqual(
            list(response),
            ["schema_version", "sources", "candidates"],
        )
        self.assertEqual(response["schema_version"], 1)
        self.assertNotIn("image_type", response)

    def test_cache_validation_requires_image_type_to_match_v2_request(self):
        logo_request = _logo_request("series", "456")
        mismatched = {
            "schema_version": 2,
            "image_type": "Primary",
            "sources": {"tmdb": "ready", "fanart": "ready"},
            "candidates": [],
        }
        # A cached Primary-typed body must not satisfy a Logo request.
        self.assertIsNone(
            candidates._validate_cached_response(
                mismatched,
                logo_request,
                _settings(),
            )
        )
        matched = json.loads(json.dumps(mismatched))
        matched["image_type"] = "Logo"
        self.assertEqual(
            candidates._validate_cached_response(
                matched,
                logo_request,
                _settings(),
            ),
            matched,
        )


if __name__ == "__main__":
    unittest.main()
