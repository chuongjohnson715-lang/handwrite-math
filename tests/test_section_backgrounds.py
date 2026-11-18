import tempfile
import unittest
from pathlib import Path

from PIL import Image

from markdown_to_handwrite.config import ReportConfig
from markdown_to_handwrite.markdown_parser import parse_markdown
from markdown_to_handwrite.renderer import ReportRenderer


class SectionBackgroundTests(unittest.TestCase):
    def _config(self, root: Path, red: Path, blue: Path | None = None) -> ReportConfig:
        config = ReportConfig()
        config.page.dpi = 72
        config.page.width_mm = 210
        config.page.height_mm = 297
        config.background.style = "image"
        config.background.image = str(red)
        config.background.auto_discover = False
        config.background.section_level = 1
        if blue is not None:
            config.background.section_images = [{"heading": "第二节", "image": str(blue)}]
        return config

    def test_heading_switches_background_and_starts_new_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            red = root / "red.png"
            blue = root / "blue.png"
            Image.new("RGB", (200, 300), (255, 0, 0)).save(red)
            Image.new("RGB", (200, 300), (0, 0, 255)).save(blue)

            config = self._config(root, red, blue)
            pages = ReportRenderer(config, base_dir=root).render(
                parse_markdown("# 第一节\n\n甲\n\n# 第二节\n\n乙\n")
            )

            self.assertEqual(len(pages), 2)
            self.assertEqual(pages[0].getpixel((5, 5))[:3], (255, 0, 0))
            self.assertEqual(pages[1].getpixel((5, 5))[:3], (0, 0, 255))

    def test_heading_without_mapping_keeps_global_background(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            red = root / "red.png"
            Image.new("RGB", (200, 300), (255, 0, 0)).save(red)

            config = self._config(root, red)
            pages = ReportRenderer(config, base_dir=root).render(
                parse_markdown("# 第一节\n\n甲\n\n# 第二节\n\n乙\n")
            )

            self.assertEqual(len(pages), 1)
            self.assertEqual(pages[0].getpixel((5, 5))[:3], (255, 0, 0))

    def test_section_images_reject_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            red = root / "red.png"
            pdf = root / "paper.pdf"
            Image.new("RGB", (200, 300), (255, 0, 0)).save(red)
            pdf.write_bytes(b"%PDF-1.4")

            config = self._config(root, red)
            config.background.section_images = [{"heading": "第二节", "image": str(pdf)}]

            with self.assertRaises(ValueError):
                ReportRenderer(config, base_dir=root)


if __name__ == "__main__":
    unittest.main()
