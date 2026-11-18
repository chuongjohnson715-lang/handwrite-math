import unittest

from PIL import Image

from markdown_to_handwrite.config import ReportConfig, mm_to_px
from markdown_to_handwrite.markdown_parser import parse_markdown
from markdown_to_handwrite.renderer import ReportRenderer

BODY = "这是一段用来检查涂写效果的正文，写得比较满才能看出笔画有没有被划掉。"


def render_page(probability: float, seed: int = 2026) -> Image.Image:
    config = ReportConfig()
    config.page.dpi = 96
    config.handwriting.seed = seed
    config.handwriting.scribble_probability = probability
    renderer = ReportRenderer(config)
    return renderer.render(parse_markdown("# 标题\n\n" + BODY + "\n\n" + BODY))[0]


def dark_pixels(page: Image.Image) -> int:
    return sum(1 for value in page.convert("L").getdata() if value < 90)


class ScribbleTests(unittest.TestCase):
    def test_certain_probability_adds_ink_on_top_of_the_text(self):
        plain = render_page(0.0)
        scribbled = render_page(1.0)

        self.assertGreater(dark_pixels(scribbled), dark_pixels(plain))
        self.assertFalse(plain.tobytes() == scribbled.tobytes())

    def test_zero_probability_keeps_the_page_deterministic(self):
        self.assertEqual(render_page(0.0).tobytes(), render_page(0.0).tobytes())

    def test_same_seed_reproduces_the_same_scribbles(self):
        self.assertEqual(render_page(1.0).tobytes(), render_page(1.0).tobytes())

    def test_different_seeds_scribe_differently(self):
        self.assertNotEqual(render_page(1.0, seed=7).tobytes(), render_page(1.0, seed=8).tobytes())

    def test_scribbles_stay_inside_the_text_column(self):
        plain = render_page(0.0)
        scribbled = render_page(1.0)
        left = mm_to_px(22.0, 96)
        right = scribbled.width - mm_to_px(20.0, 96)

        self.assertEqual(
            plain.crop((0, 0, left, plain.height)).tobytes(),
            scribbled.crop((0, 0, left, scribbled.height)).tobytes(),
        )
        self.assertEqual(
            plain.crop((right, 0, plain.width, plain.height)).tobytes(),
            scribbled.crop((right, 0, scribbled.width, scribbled.height)).tobytes(),
        )
