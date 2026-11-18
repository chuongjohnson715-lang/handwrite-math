import base64
import io
import unittest

from PIL import Image, ImageDraw

from markdown_to_handwrite.config import ReportConfig, pt_to_px
from markdown_to_handwrite.handwriting import HandwritingEngine
from markdown_to_handwrite.style_profile import analyze_handwriting
from markdown_to_handwrite.web import analyze_handwriting_sample

LINES = [
    "春眠不觉晓处处闻啼鸟",
    "夜来风雨声花落知多少",
    "床前明月光疑是地上霜",
]
SAMPLE_DPI = 200

# 还原精度：笔宽与倾斜用绝对容差，行距用比例容差，都给实测误差留了余量。
STROKE_TOLERANCE = 1.8
LINE_SPACING_TOLERANCE = 0.14
SLANT_TOLERANCE = 2.6
COLOR_TOLERANCE = 42


def make_sample(stroke=16.0, line_spacing=1.55, slant=0.0, ink="#17233b", lines=None):
    """Render a sample with known parameters, so the analyser can be scored against them."""
    size = pt_to_px(16, SAMPLE_DPI)
    config = ReportConfig().handwriting
    config.sdt_stroke_width = stroke
    config.line_spacing = line_spacing
    config.glyph_slant_deg = slant
    config.ink_color = ink
    engine = HandwritingEngine(config, reference_size_px=size, math_reference_size_px=size)
    row_height = round(size * line_spacing)
    rendered = [
        engine.render_line(text, size, 100000, seed_extra=f"style-{index}", return_baseline=True)
        for index, text in enumerate(lines or LINES)
    ]
    width = max(image.width for image, _ in rendered) + 120
    height = row_height * len(rendered) + 120
    canvas = Image.new("RGB", (width, height), "white")
    for index, (image, baseline) in enumerate(rendered):
        canvas.paste(image, (60, 60 + index * row_height - baseline), image)
    return canvas


def png_data_url(image):
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def add_ruled_lines(image, ink=(190, 190, 190)):
    """画浅色印刷横线，模拟带格线的笔记本。

    灰度刻意选在两条阈值之间：宽松掩膜会把它当成墨迹（用于量笔宽），
    严格掩膜会把它挡掉（用于切分行字）。真实笔记本照片就是这个区间。
    """
    canvas = image.copy()
    draw = ImageDraw.Draw(canvas)
    row_height = (canvas.height - 120) / len(LINES)
    for index in range(len(LINES)):
        y = 60 + row_height * (index + 1) - 14
        draw.line((0, y, canvas.width, y), fill=ink, width=3)
    return canvas


class RuledPaperTests(unittest.TestCase):
    def test_ruled_lines_do_not_merge_rows(self):
        """笔记本横线把行间空隙填满时，行切分不能塌成一行。

        这是真实照片最典型的失败场景：一旦按"每行墨迹像素数"切行，印刷横线
        会让整页连成一个巨大的行带，最后只能报出两三行、几个字。
        """
        profile = analyze_handwriting(add_ruled_lines(make_sample()))
        self.assertTrue(profile.usable, profile.warnings)
        self.assertEqual(profile.metrics.rows, len(LINES))
        self.assertGreaterEqual(profile.metrics.characters, 20)

    def test_ruled_lines_do_not_break_stroke_measurement(self):
        plain = analyze_handwriting(make_sample())
        ruled = analyze_handwriting(add_ruled_lines(make_sample()))
        self.assertAlmostEqual(
            ruled.suggested["handwriting.sdt_stroke_width"],
            plain.suggested["handwriting.sdt_stroke_width"],
            delta=2.0,
        )


class StyleProfileRecoveryTests(unittest.TestCase):
    def test_recovers_stroke_width_and_line_spacing(self):
        profile = analyze_handwriting(make_sample(stroke=16.0, line_spacing=1.55))
        suggested = profile.suggested
        self.assertAlmostEqual(suggested["handwriting.sdt_stroke_width"], 16.0, delta=STROKE_TOLERANCE)
        self.assertAlmostEqual(suggested["handwriting.line_spacing"], 1.55, delta=LINE_SPACING_TOLERANCE)

    def test_recovers_a_bold_narrow_hand(self):
        profile = analyze_handwriting(make_sample(stroke=20.0, line_spacing=2.0, slant=10.0))
        suggested = profile.suggested
        self.assertGreater(suggested["handwriting.sdt_stroke_width"], 18.0)
        self.assertAlmostEqual(suggested["handwriting.line_spacing"], 2.0, delta=LINE_SPACING_TOLERANCE)

    def test_recovers_a_thin_loose_hand(self):
        profile = analyze_handwriting(make_sample(stroke=11.0, line_spacing=1.25, slant=-8.0))
        suggested = profile.suggested
        self.assertLess(suggested["handwriting.sdt_stroke_width"], 13.5)
        self.assertAlmostEqual(suggested["handwriting.line_spacing"], 1.25, delta=LINE_SPACING_TOLERANCE)

    def test_slant_keeps_its_sign_and_magnitude(self):
        for expected in (-10.0, 8.0):
            profile = analyze_handwriting(make_sample(slant=expected))
            measured = profile.suggested["handwriting.glyph_slant_deg"]
            self.assertAlmostEqual(measured, expected, delta=SLANT_TOLERANCE, msg=f"输入 {expected}°")

    def test_upright_sample_is_not_reported_as_slanted(self):
        profile = analyze_handwriting(make_sample(slant=0.0))
        self.assertLess(abs(profile.suggested["handwriting.glyph_slant_deg"]), SLANT_TOLERANCE)

    def test_recovers_ink_colour(self):
        profile = analyze_handwriting(make_sample(ink="#8b1a1a"))
        red, green, blue = profile.metrics.ink_color[1:3], profile.metrics.ink_color[3:5], profile.metrics.ink_color[5:7]
        red, green, blue = int(red, 16), int(green, 16), int(blue, 16)
        self.assertLess(abs(red - 0x8B), COLOR_TOLERANCE)
        self.assertLess(abs(green - 0x1A), COLOR_TOLERANCE)
        self.assertLess(abs(blue - 0x1A), COLOR_TOLERANCE)
        self.assertGreater(red, green + 60, "墨色应明显偏红")

    def test_counts_rows_and_characters(self):
        profile = analyze_handwriting(make_sample())
        self.assertEqual(profile.metrics.rows, len(LINES))
        self.assertGreaterEqual(profile.metrics.characters, 20)


class StyleProfileGuardTests(unittest.TestCase):
    def test_blank_image_is_rejected_with_a_reason(self):
        blank = Image.new("RGB", (800, 600), "white")
        profile = analyze_handwriting(blank)
        self.assertFalse(profile.usable)
        self.assertTrue(profile.warnings)
        self.assertFalse(profile.suggested)

    def test_too_few_characters_is_rejected(self):
        profile = analyze_handwriting(make_sample(lines=["春眠不觉晓", "处处闻啼鸟"]))
        self.assertFalse(profile.usable)
        self.assertIn("样本太少", profile.warnings[0])

    def test_inverted_sample_is_flipped_automatically(self):
        """深底浅字（白板笔、暗色笔记）也应能量出同样的参数。"""
        normal = analyze_handwriting(make_sample(stroke=18.0))
        inverted = analyze_handwriting(Image.eval(make_sample(stroke=18.0), lambda value: 255 - value))
        self.assertAlmostEqual(
            inverted.suggested["handwriting.sdt_stroke_width"],
            normal.suggested["handwriting.sdt_stroke_width"],
            delta=1.5,
        )
        self.assertTrue(any("反相" in note for note in inverted.notes))

    def test_overlay_matches_detected_content(self):
        profile = analyze_handwriting(make_sample())
        self.assertIsNotNone(profile.overlay)
        self.assertEqual(profile.overlay.size, make_sample().size)


class AnalyzeSampleEndpointTests(unittest.TestCase):
    def test_returns_metrics_suggestions_and_overlay(self):
        result = analyze_handwriting_sample(png_data_url(make_sample()))
        self.assertTrue(result["usable"])
        self.assertIn("handwriting.glyph_slant_deg", result["suggested"])
        self.assertIn("quality", result["metrics"])
        self.assertTrue(result["overlay"].startswith("data:image/png;base64,"))
        self.assertTrue(result["notes"])

    def test_rejects_pdf_samples(self):
        with self.assertRaisesRegex(ValueError, "仅支持"):
            analyze_handwriting_sample("data:application/pdf;base64,JVBERi0xLjQK")

    def test_rejects_broken_image_bytes(self):
        payload = base64.b64encode(b"not-really-a-png").decode()
        with self.assertRaisesRegex(ValueError, "无法读取"):
            analyze_handwriting_sample(f"data:image/png;base64,{payload}")


class GlyphSlantRenderTests(unittest.TestCase):
    def test_slant_changes_the_rendered_shape(self):
        def render(slant):
            size = pt_to_px(16, 200)
            config = ReportConfig().handwriting
            config.glyph_slant_deg = slant
            config.seed = 7
            engine = HandwritingEngine(config, reference_size_px=size, math_reference_size_px=size)
            return engine.render_line("永字八法", size, 4000)

        upright = render(0.0)
        leaning = render(14.0)
        self.assertNotEqual(upright.tobytes(), leaning.tobytes())
        # 倾斜只绕字符自身中心旋转，不改变字距，因此行宽基本不变。
        self.assertLess(abs(leaning.width - upright.width), upright.width * 0.12)


if __name__ == "__main__":
    unittest.main()
