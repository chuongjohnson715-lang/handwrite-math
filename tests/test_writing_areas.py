import base64
import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops

from markdown_to_handwrite.config import (
    MAX_WRITING_AREAS,
    ReportConfig,
    config_from_dict,
    validate_writing_areas,
)
from markdown_to_handwrite.markdown_parser import parse_markdown
from markdown_to_handwrite.renderer import PROJECT_ROOT, ReportRenderer
from markdown_to_handwrite.web import resolve_project_asset, save_uploaded_background

# 字形扰动会让笔画略微越过区域边界，核对像素包围盒时保留这点容差。
PIXEL_TOLERANCE = 14


def make_config(areas, **overrides):
    config = ReportConfig()
    config.page.dpi = 96
    config.page.margin_top_mm = 0
    config.page.margin_bottom_mm = 0
    config.page.margin_left_mm = 0
    config.page.margin_right_mm = 0
    config.background.style = "plain"
    config.background.auto_discover = False
    config.background.paper_color = "#ffffff"
    config.background.draw_margin_line = False
    config.background.writing_areas = areas
    config.handwriting.ink_color = "#000000"
    config.handwriting.body_font_pt = 12
    config.handwriting.second_layer_enabled = False
    config.layout.justify_paragraphs = False
    for key, value in overrides.items():
        section, field = key.split("__")
        setattr(getattr(config, section), field, value)
    return config


def ink_bbox(page):
    blank = Image.new("RGB", page.size, (255, 255, 255))
    return ImageChops.difference(page.convert("RGB"), blank).getbbox()


def render(markdown, areas, **overrides):
    config = make_config(areas, **overrides)
    renderer = ReportRenderer(config, base_dir=PROJECT_ROOT)
    return renderer.render(parse_markdown(markdown))


class WritingAreaValidationTests(unittest.TestCase):
    def test_accepts_areas_that_fit_on_the_page(self):
        validate_writing_areas([{"x": 0.1, "y": 0.2, "w": 0.5, "h": 0.3}])

    def test_empty_list_means_default_margins(self):
        validate_writing_areas([])

    def test_rejects_area_outside_the_page(self):
        with self.assertRaisesRegex(ValueError, "超出了页面范围"):
            validate_writing_areas([{"x": 0.8, "y": 0.1, "w": 0.4, "h": 0.2}])

    def test_rejects_zero_sized_area(self):
        with self.assertRaisesRegex(ValueError, "宽高必须大于 0"):
            validate_writing_areas([{"x": 0.1, "y": 0.1, "w": 0.0, "h": 0.2}])

    def test_rejects_non_numeric_coordinates(self):
        with self.assertRaisesRegex(ValueError, "有限数值"):
            validate_writing_areas([{"x": "0.1", "y": 0.1, "w": 0.2, "h": 0.2}])

    def test_rejects_too_many_areas(self):
        areas = [{"x": 0.0, "y": 0.0, "w": 0.1, "h": 0.1}] * (MAX_WRITING_AREAS + 1)
        with self.assertRaisesRegex(ValueError, "最多支持"):
            validate_writing_areas(areas)

    def test_config_from_dict_validates_areas(self):
        with self.assertRaisesRegex(ValueError, "书写区域"):
            config_from_dict({"background": {"writing_areas": [{"x": 0.9, "y": 0.9, "w": 0.5, "h": 0.5}]}})

    def test_default_config_has_no_writing_areas(self):
        self.assertEqual(ReportConfig().background.writing_areas, [])


class WritingAreaLayoutTests(unittest.TestCase):
    def test_text_stays_inside_a_single_area(self):
        areas = [{"x": 0.1, "y": 0.1, "w": 0.4, "h": 0.2}]
        pages = render("这是测试文本。", areas)
        self.assertEqual(len(pages), 1)
        page = pages[0]
        box = ink_bbox(page)
        self.assertIsNotNone(box)
        left = round(0.1 * page.width)
        top = round(0.1 * page.height)
        right = round(0.5 * page.width)
        bottom = round(0.3 * page.height)
        self.assertGreaterEqual(box[0], left - PIXEL_TOLERANCE)
        self.assertGreaterEqual(box[1], top - PIXEL_TOLERANCE)
        self.assertLessEqual(box[2], right + PIXEL_TOLERANCE)
        self.assertLessEqual(box[3], bottom + PIXEL_TOLERANCE)

    def test_first_area_is_filled_before_the_second(self):
        areas = [
            {"x": 0.05, "y": 0.05, "w": 0.9, "h": 0.12},
            {"x": 0.05, "y": 0.55, "w": 0.9, "h": 0.12},
        ]
        pages = render("短句。", areas)
        box = ink_bbox(pages[0])
        self.assertIsNotNone(box)
        second_top = round(0.55 * pages[0].height)
        self.assertLess(box[3], second_top)

    def test_overflow_flows_into_the_next_area(self):
        areas = [
            {"x": 0.05, "y": 0.05, "w": 0.9, "h": 0.1},
            {"x": 0.05, "y": 0.55, "w": 0.9, "h": 0.1},
        ]
        markdown = "\n\n".join(f"第{index}段内容。" for index in range(1, 21))
        pages = render(markdown, areas)
        first = pages[0]
        box = ink_bbox(first)
        self.assertIsNotNone(box)
        second_top = round(0.55 * first.height)
        self.assertGreaterEqual(box[3], second_top - PIXEL_TOLERANCE)

    def test_area_exhaustion_starts_a_new_page(self):
        areas = [{"x": 0.05, "y": 0.05, "w": 0.9, "h": 0.08}]
        markdown = "\n\n".join(f"第{index}段内容。" for index in range(1, 31))
        pages = render(markdown, areas)
        self.assertGreater(len(pages), 1)
        for page in pages:
            box = ink_bbox(page)
            if box is None:
                continue
            self.assertLessEqual(box[3], round(0.13 * page.height) + PIXEL_TOLERANCE)

    def test_without_areas_layout_uses_page_margins(self):
        pages = render("这是测试文本。", [])
        box = ink_bbox(pages[0])
        self.assertIsNotNone(box)
        self.assertLess(box[1], 60)


class BackgroundUploadTests(unittest.TestCase):
    def test_saves_png_data_url_into_upload_directory(self):
        image = Image.new("RGB", (8, 8), (200, 40, 40))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        data = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()

        asset = save_uploaded_background("我的信纸.png", data)
        stored = PROJECT_ROOT / asset["path"]
        try:
            self.assertTrue(stored.is_file())
            self.assertTrue(asset["path"].startswith("background/uploads/"))
            self.assertEqual(asset["label"], "我的信纸.png")
            self.assertIn("/api/background-preview?path=", asset["url"])
        finally:
            stored.unlink(missing_ok=True)

    def test_rejects_unsupported_mime_type(self):
        with self.assertRaisesRegex(ValueError, "仅支持"):
            save_uploaded_background("note.txt", "data:text/plain;base64,aGVsbG8=")

    def test_rejects_missing_payload(self):
        with self.assertRaisesRegex(ValueError, "为空"):
            save_uploaded_background("empty.png", "data:image/png;base64,")

    def test_rejects_paths_outside_the_project(self):
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "paper.png"
            outside.write_bytes(b"not-an-image")
            with self.assertRaisesRegex(ValueError, "项目目录内"):
                resolve_project_asset(str(outside))

    def test_resolves_uploaded_background_for_preview(self):
        image = Image.new("RGB", (8, 8), (40, 40, 200))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        data = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
        asset = save_uploaded_background("preview.png", data)
        try:
            resolved = resolve_project_asset(asset["path"])
            self.assertEqual(resolved, (PROJECT_ROOT / asset["path"]).resolve())
        finally:
            (PROJECT_ROOT / asset["path"]).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
