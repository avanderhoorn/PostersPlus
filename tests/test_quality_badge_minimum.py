import unittest
from unittest.mock import patch

from PIL import Image

import main


class QualityBadgeMinimumTests(unittest.TestCase):
    _BACKGROUND = (12, 34, 56, 255)
    _BADGE = (220, 30, 40, 255)
    _BADGE_ORIGIN = (10, 15)

    def _render(self, token: str, minimum: int) -> Image.Image:
        config = main.build_request_config(
            {
                "badge_anchor_x": "0.10",
                "badge_anchor_y": "0.10",
                "badge_display_mode": "2",
                "badge_height": "8",
                "badge_min_score": str(minimum),
                "bottom_gradient": "off",
                "rating_display_mode": "0",
                "sash_mode": "hidden",
                "top_gradient": "off",
            }
        )
        badge = Image.new("RGBA", (12, 8), self._BADGE)
        with patch.object(main, "get_resized_badge", return_value=badge):
            return main.build_poster(
                Image.new("RGBA", (100, 150), self._BACKGROUND),
                "N/A",
                "Drama",
                config,
                quality_tokens=[token],
            )

    def test_request_parser_accepts_threshold_one_and_compatibility_alias(self):
        for field in ("badge_min_score", "combined_badge_min_score"):
            with self.subTest(field=field):
                config = main.build_request_config({field: "1"})
                self.assertEqual(config.badge_min_score, 1)

    def test_request_parser_clamps_threshold_to_supported_range(self):
        self.assertEqual(
            main.build_request_config({"badge_min_score": "0"}).badge_min_score,
            1,
        )
        self.assertEqual(
            main.build_request_config({"badge_min_score": "7"}).badge_min_score,
            6,
        )

    def test_threshold_one_renders_each_score_one_quality_category(self):
        for token in ("1080P", "WEBDL", "HDR10"):
            with self.subTest(token=token):
                rendered = self._render(token, 1)
                self.assertEqual(
                    rendered.getpixel(self._BADGE_ORIGIN),
                    self._BADGE,
                )

    def test_threshold_two_suppresses_1080p_but_preserves_score_two_rendering(self):
        suppressed = self._render("1080P", 2)
        rendered = self._render("4K", 2)

        self.assertEqual(
            suppressed.getpixel(self._BADGE_ORIGIN),
            self._BACKGROUND,
        )
        self.assertEqual(
            rendered.getpixel(self._BADGE_ORIGIN),
            self._BADGE,
        )


if __name__ == "__main__":
    unittest.main()
