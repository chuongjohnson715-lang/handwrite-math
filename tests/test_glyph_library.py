import base64
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from markdown_to_handwrite.config import ReportConfig, pt_to_px
from markdown_to_handwrite.glyph_library import GlyphLibrary, build_library, extract_glyphs
from markdown_to_handwrite.glyph_template import COMMON_CHARS, build_template
from markdown_to_handwrite.handwriting import HandwritingEngine

KAI_FONT = "C:/Windows/Fonts/STKAITI.TTF"
XINGSHU_FONT = "font/AnJingChenYingBiXingShu-2.ttf"

_TEMPLATE_DIR: tempfile.TemporaryDirectory | None = None
_MANIFEST: dict = {}
_PAGE: Image.Image | None = None
_FILLED: Image.Image | None = None


def _fill_template(page: Image.Image, cells: list[dict], font_path: str = KAI_FONT, ink=(24, 32, 56)) -> Image.Image:
    """Simulate a user copying the sheet: write each character into its cell."""
    filled = page.copy()
    draw = ImageDraw.Draw(filled)
    cell_px = round(cells[0]["w"] * filled.width)
    font = ImageFont.truetype(font_path, int(cell_px * 0.74))
    width, height = filled.size
    for cell in cells:
        centre = ((cell["x"] + cell["w"] / 2) * width, (cell["y"] + cell["h"] / 2) * height)
        draw.text(centre, cell["char"], fill=ink, font=font, anchor="mm")
    return filled


def setUpModule():
    """模板生成一次就够，每个用例重建会让整个文件跑上分钟级。

    测试用 150 DPI：格子坐标是页面比例，与分辨率无关，但整页图小了四倍。
    """
    global _TEMPLATE_DIR, _MANIFEST, _PAGE, _FILLED
    _TEMPLATE_DIR = tempfile.TemporaryDirectory()
    _MANIFEST = build_template(_TEMPLATE_DIR.name, name="t", dpi=150)
    _PAGE = Image.open(Path(_TEMPLATE_DIR.name) / _MANIFEST["pages"][0]["file"]).convert("RGB")
    _FILLED = _fill_template(_PAGE, _MANIFEST["pages"][0]["cells"])


def tearDownModule():
    if _TEMPLATE_DIR is not None:
        _TEMPLATE_DIR.cleanup()


def build_temp_library(font_path: str = KAI_FONT) -> Path:
    """Build a library in a throwaway directory from the shared filled sheet."""
    assert _TEMPLATE_DIR is not None and _MANIFEST and _PAGE is not None
    target = Path(_TEMPLATE_DIR.name) / f"lib-{abs(hash(font_path))}"
    if not (target / "index.json").is_file():
        filled = _FILLED if font_path == KAI_FONT else _fill_template(_PAGE, _MANIFEST["pages"][0]["cells"], font_path)
        build_library([filled], _MANIFEST, target, name="测试")
    return target


class TemplateTests(unittest.TestCase):
    def test_each_character_gets_its_own_cell(self):
        self.assertEqual(_MANIFEST["charCount"], len(set(COMMON_CHARS)))
        total_cells = sum(len(page["cells"]) for page in _MANIFEST["pages"])
        self.assertEqual(total_cells, _MANIFEST["charCount"])

    def test_cells_stay_inside_the_page(self):
        for cell in _MANIFEST["pages"][0]["cells"]:
            self.assertLessEqual(cell["x"] + cell["w"], 1.0001)
            self.assertLessEqual(cell["y"] + cell["h"], 1.0001)

    def test_template_file_is_written(self):
        assert _TEMPLATE_DIR is not None
        for page in _MANIFEST["pages"]:
            self.assertTrue((Path(_TEMPLATE_DIR.name) / page["file"]).is_file())


class LibraryBuildTests(unittest.TestCase):
    def test_extracts_one_glyph_per_written_cell(self):
        assert _TEMPLATE_DIR is not None
        index = build_library([_FILLED], _MANIFEST, Path(_TEMPLATE_DIR.name) / "count-lib", name="测试")
        self.assertEqual(len(index["chars"]), _MANIFEST["charCount"])
        self.assertEqual(index["missing"], [])
        self.assertGreater(index["referenceHeight"], 0.2)

    def test_hint_characters_are_not_mistaken_for_ink(self):
        """浅灰提示字必须被滤掉，否则每个字形都会带上印刷体的轮廓。"""
        assert _PAGE is not None
        glyphs, _ = extract_glyphs(_PAGE, _MANIFEST, 0)
        self.assertEqual(glyphs, {})

    def test_blank_sheet_produces_no_glyphs(self):
        assert _PAGE is not None and _TEMPLATE_DIR is not None
        index = build_library([_PAGE], _MANIFEST, Path(_TEMPLATE_DIR.name) / "blank-lib")
        self.assertEqual(index["chars"], {})

    def test_coverage_counts_common_characters(self):
        library = GlyphLibrary.load(build_temp_library())
        self.assertIsNotNone(library)
        self.assertGreater(library.coverage(), 0.95)


class RenderTests(unittest.TestCase):
    def _render(self, text: str, library: str | None) -> Image.Image:
        size = pt_to_px(18, 200)
        config = ReportConfig().handwriting
        config.body_font_pt = 18
        config.glyph_library = library
        config.second_layer_enabled = False
        engine = HandwritingEngine(config, reference_size_px=size, math_reference_size_px=size)
        return engine.render_line(text, size, 100000, seed_extra="lib")

    def test_personal_glyph_replaces_the_font(self):
        library = build_temp_library(XINGSHU_FONT)
        plain = self._render("我们的学习", None)
        personal = self._render("我们的学习", str(library))
        self.assertNotEqual(plain.tobytes(), personal.tobytes())

    def test_characters_outside_the_library_still_render(self):
        # 「龟」「龘」不在常用字表里，必须回退到字体而不是留空。
        image = self._render("龟龘", str(build_temp_library()))
        box = image.getbbox()
        self.assertIsNotNone(box)
        self.assertGreater(box[2] - box[0], 10)

    def test_missing_library_directory_does_not_crash(self):
        image = self._render("我们的学习", "glyphs/不存在的库")
        self.assertIsNotNone(image.getbbox())


class WebIntegrationTests(unittest.TestCase):
    def test_build_endpoint_payload_round_trip(self):
        """接口层跑通，但库要落到临时目录：往 Desktop 写两百个 PNG 会被安全软件拖到分钟级。"""
        from unittest.mock import patch

        from markdown_to_handwrite import web as web_module

        assert _TEMPLATE_DIR is not None and _FILLED is not None
        buffer = Path(_TEMPLATE_DIR.name) / "filled.png"
        _FILLED.save(buffer)
        data = "data:image/png;base64," + base64.b64encode(buffer.read_bytes()).decode()

        glyph_root = Path(_TEMPLATE_DIR.name) / "glyphs"
        template_root = Path(_TEMPLATE_DIR.name)
        with patch.object(web_module, "GLYPH_ROOT", glyph_root), patch.object(web_module, "TEMPLATE_ROOT", template_root):
            result = web_module.build_glyph_library_from_uploads([data], "测试字迹")
            self.assertGreater(result["count"], 150)
            self.assertTrue(any(item["label"] == "测试字迹" for item in web_module.available_glyph_libraries()))
            self.assertTrue((glyph_root / "测试字迹" / "index.json").is_file())


if __name__ == "__main__":
    unittest.main()
