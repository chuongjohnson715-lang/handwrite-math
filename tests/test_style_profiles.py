import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from markdown_to_handwrite import web as web_module

SAMPLE_STYLE = {
    "handwriting.ink_color": "#20304d",
    "handwriting.glyph_slant_deg": 7.5,
    "handwriting.sdt_stroke_width": 19.4,
    "handwriting.line_spacing": 1.71,
}


def png_data_url(size=(160, 120), colour=(30, 40, 70)) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


class StyleProfileTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self._patch = patch.object(web_module, "STYLE_ROOT", self.root / "profiles")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._temp.cleanup()

    def test_saves_style_and_sample(self):
        result = web_module.save_style_profile(
            {"name": "楷书", "data": png_data_url(), "suggested": SAMPLE_STYLE, "metrics": {"rows": 3}}
        )
        self.assertEqual(result["name"], "楷书")
        self.assertTrue(result["hasSample"])
        directory = self.root / "profiles" / "楷书"
        self.assertTrue((directory / "index.json").is_file())
        self.assertTrue((directory / "sample.png").is_file())
        index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["style"]["handwriting.glyph_slant_deg"], 7.5)

    def test_saved_profile_shows_up_in_the_list(self):
        web_module.save_style_profile({"name": "行书", "suggested": SAMPLE_STYLE})
        profiles = web_module.available_style_profiles()
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["label"], "行书")
        self.assertEqual(profiles[0]["style"]["handwriting.sdt_stroke_width"], 19.4)
        self.assertFalse(profiles[0]["hasSample"])

    def test_rejects_unknown_style_keys(self):
        """前端不能借这个接口往配置里塞任意字段。"""
        result = web_module.save_style_profile(
            {
                "name": "注入",
                "suggested": {**SAMPLE_STYLE, "page.dpi": 9999, "handwriting.font_path": "../../etc/passwd"},
            }
        )
        self.assertNotIn("page.dpi", result["style"])
        self.assertNotIn("handwriting.font_path", result["style"])
        self.assertEqual(set(result["style"]), set(SAMPLE_STYLE))

    def test_empty_style_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "风格参数"):
            web_module.save_style_profile({"name": "空", "suggested": {}})

    def test_name_is_sanitised(self):
        result = web_module.save_style_profile({"name": "../../逃逸", "suggested": SAMPLE_STYLE})
        saved = self.root / "profiles" / result["name"]
        self.assertTrue(saved.is_dir())
        self.assertNotIn("..", result["name"])

    def test_same_name_overwrites_instead_of_duplicating(self):
        web_module.save_style_profile({"name": "我的笔迹", "suggested": SAMPLE_STYLE})
        web_module.save_style_profile(
            {"name": "我的笔迹", "suggested": {**SAMPLE_STYLE, "handwriting.sdt_stroke_width": 12.0}}
        )
        profiles = web_module.available_style_profiles()
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["style"]["handwriting.sdt_stroke_width"], 12.0)

    def test_stale_sample_of_other_format_is_removed(self):
        web_module.save_style_profile({"name": "换格式", "data": png_data_url(), "suggested": SAMPLE_STYLE})
        jpeg = io.BytesIO()
        Image.new("RGB", (80, 80), (10, 10, 10)).save(jpeg, "JPEG")
        web_module.save_style_profile(
            {
                "name": "换格式",
                "data": "data:image/jpeg;base64," + base64.b64encode(jpeg.getvalue()).decode(),
                "suggested": SAMPLE_STYLE,
            }
        )
        directory = self.root / "profiles" / "换格式"
        samples = sorted(path.name for path in directory.glob("sample.*"))
        self.assertEqual(samples, ["sample.jpg"])

    def test_broken_sample_does_not_block_saving_style(self):
        with self.assertRaisesRegex(ValueError, "无法解码|仅支持"):
            web_module.save_style_profile({"name": "坏图", "data": "data:image/png;base64,@@@", "suggested": SAMPLE_STYLE})


class AssetWiringTests(unittest.TestCase):
    def test_assets_include_style_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(web_module, "STYLE_ROOT", Path(directory) / "profiles"):
                web_module.save_style_profile({"name": "测试", "suggested": SAMPLE_STYLE})
                assets = web_module.available_assets()
                self.assertIn("styleProfiles", assets)
                self.assertEqual(len(assets["styleProfiles"]), 1)


if __name__ == "__main__":
    unittest.main()
