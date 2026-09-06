import ast
import inspect
from pathlib import Path
import subprocess
import types
import unittest

from PIL import Image, ImageDraw

import main


UPSTREAM_REVISION = "9d84d388a426c90ad439a27e01941538856fb85e"


def _pristine_source(path: str) -> str:
    repository = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        ["git", "show", f"{UPSTREAM_REVISION}:{path}"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )
    return ast.get_source_segment(source, function) or ""


def _primary(size: tuple[int, int]) -> Image.Image:
    width, height = size
    image = Image.new("RGBA", size, (18, 24, 36, 255))
    draw = ImageDraw.Draw(image)
    for index in range(12):
        left = round(width * index / 12)
        right = round(width * (index + 1) / 12) - 1
        draw.rectangle(
            (left, 0, right, height - 1),
            fill=(18 + index * 12, 24 + index * 5, 80 - index * 4, 255),
        )
    draw.rectangle(
        (0, 0, width - 1, max(1, height // 8)),
        fill=(210, 30, 40, 255),
    )
    draw.rectangle(
        (0, height - max(1, height // 7), width - 1, height - 1),
        fill=(25, 70, 210, 255),
    )
    return image


class StockEquivalenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = _pristine_source("main.py")
        pristine = types.ModuleType("postersplus_pristine_9d84d388")
        pristine.__file__ = str(Path(main.__file__).resolve())
        exec(compile(source, f"{UPSTREAM_REVISION}:main.py", "exec"), pristine.__dict__)
        cls.pristine = pristine
        cls.pristine_source = source
        cls.pristine_tmdb_source = _pristine_source("tmdb.py")
        pristine_tmdb = types.ModuleType("postersplus_pristine_tmdb_9d84d388")
        pristine_tmdb.__file__ = str(
            Path(main.__file__).resolve().with_name("tmdb.py")
        )
        exec(
            compile(
                cls.pristine_tmdb_source,
                f"{UPSTREAM_REVISION}:tmdb.py",
                "exec",
            ),
            pristine_tmdb.__dict__,
        )
        cls.pristine_tmdb = pristine_tmdb

    def test_stock_compositor_source_is_unchanged_and_has_no_private_geometry(self):
        current = inspect.getsource(main.build_poster).replace("\r\n", "\n")
        pristine = _function_source(
            self.pristine_source,
            "build_poster",
        ).replace("\r\n", "\n")
        self.assertEqual(current.strip(), pristine.strip())
        for forbidden in (
            "adaptive_square",
            "logo_geometry",
            "quiet_band",
            "runner",
        ):
            self.assertNotIn(forbidden, current)

    def test_stock_normalizer_source_is_unchanged(self):
        current = inspect.getsource(main.normalise_poster).replace("\r\n", "\n")
        pristine = _function_source(
            self.pristine_tmdb_source,
            "normalise_poster",
        ).replace("\r\n", "\n")
        self.assertEqual(current.strip(), pristine.strip())

    def test_noncanonical_selected_assets_match_pristine_stock_pixels(self):
        logo = Image.new("RGBA", (220, 70), (0, 0, 0, 0))
        ImageDraw.Draw(logo).rounded_rectangle(
            (5, 5, 214, 64),
            radius=12,
            fill=(245, 245, 245, 255),
        )
        config_args = {
            "badge_display_mode": 0,
            "bottom_gradient": "off",
            "rating_display_mode": 0,
            "sash_mode": "hidden",
            "show_award_sash": False,
            "top_gradient": "off",
        }
        for size in ((1000, 1500), (900, 1200)):
            with self.subTest(size=size):
                primary = _primary(size)
                current_base = main.normalise_poster(primary.copy())
                pristine_base = self.pristine_tmdb.normalise_poster(
                    primary.copy()
                )
                self.assertEqual(current_base.size, (500, 750))
                self.assertEqual(pristine_base.size, (500, 750))
                self.assertEqual(
                    current_base.tobytes(),
                    pristine_base.tobytes(),
                )

                current = main.build_poster(
                    current_base,
                    "N/A",
                    "Drama",
                    main.RequestConfig(**config_args),
                    logo=logo.copy(),
                    release_year="1999",
                )
                pristine = self.pristine.build_poster(
                    pristine_base,
                    "N/A",
                    "Drama",
                    self.pristine.RequestConfig(**config_args),
                    logo=logo.copy(),
                    release_year="1999",
                )
                self.assertEqual(current.mode, pristine.mode)
                self.assertEqual(current.size, pristine.size)
                self.assertEqual(current.tobytes(), pristine.tobytes())
