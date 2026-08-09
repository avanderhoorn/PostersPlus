import os
import tempfile
import unittest
from unittest.mock import patch

import cache
import main


class CachePathTests(unittest.TestCase):
    def test_safe_cache_path_rejects_sibling_prefix(self):
        with tempfile.TemporaryDirectory() as parent:
            base = os.path.join(parent, "cache")
            os.mkdir(base)
            with self.assertRaises(ValueError):
                cache._safe_cache_path(base, "../cache-other/file")

    def test_safe_cache_path_rejects_absolute_path(self):
        with tempfile.TemporaryDirectory() as base:
            with self.assertRaises(ValueError):
                cache._safe_cache_path(base, "/tmp/elsewhere")

    def test_atomic_write_replaces_complete_file(self):
        with tempfile.TemporaryDirectory() as base:
            path = os.path.join(base, "poster")
            cache._atomic_write(path, b"first")
            cache._atomic_write(path, b"second")
            with open(path, "rb") as stored:
                self.assertEqual(stored.read(), b"second")
            self.assertFalse(any(name.startswith(".tmp-") for name in os.listdir(base)))


class RenderSignatureTests(unittest.TestCase):
    def test_visual_setting_changes_server_signature(self):
        original = main._cfg.JPEG_QUALITY
        try:
            before = main._server_render_signature()
            main._cfg.JPEG_QUALITY = original - 1
            after = main._server_render_signature()
            self.assertNotEqual(before, after)
        finally:
            main._cfg.JPEG_QUALITY = original


class SelectedCacheInvalidationTests(unittest.TestCase):
    def test_selected_key_preserves_invalidation_fields(self):
        key = main._selected_final_cache_key("tt123", "456", "movie", "abc")
        try:
            with cache._composite_l1_lock:
                cache._composite_l1[key] = b"poster"
            with (
                patch.object(cache, "COMPOSITE_MEM_ENTRIES", 1),
                patch.object(cache, "get_db"),
            ):
                cache.invalidate_final_posters("456", "movie")
            self.assertNotIn(key, cache._composite_l1)
        finally:
            with cache._composite_l1_lock:
                cache._composite_l1.pop(key, None)


if __name__ == "__main__":
    unittest.main()
