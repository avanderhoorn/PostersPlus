import asyncio
import gzip
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

    def test_cached_contract_rejects_legacy_statuses_and_extra_candidate_fields(self):
        base = {
            "schema_version": 1,
            "sources": {"tmdb": "ready", "fanart": "failed"},
            "candidates": [{
                "source": "tmdb",
                "url": "https://image.tmdb.org/t/p/original/clean.jpg",
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


if __name__ == "__main__":
    unittest.main()
