import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw

from markdown_to_handwrite import web as web_module
from markdown_to_handwrite.rectify import RectifyError, rectify, tilt_degrees

LINE_COLOUR = (150, 175, 210)


def slanted_page(width=600, height=800, pitch=40, slope=0.03) -> Image.Image:
    """画一张"拍歪了"的横线纸：每条线从左到右逐渐下沉。"""
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for index in range(18):
        y = 100 + index * pitch
        draw.line((0, y, width, y + slope * width), fill=LINE_COLOUR, width=2)
    return image


def corners_for(slope: float, width=600, height=800) -> list[tuple[float, float]]:
    """圈住斜线纸的四边形（归一化）。"""
    drop = slope  # 右端相对左端下沉的比例（因为归一化 x 跨度就是 1）
    return [(0.0, 0.04), (1.0, 0.04 + drop), (1.0, 0.96 + drop), (0.0, 0.96)]


class RectifyTests(unittest.TestCase):
    def test_slanted_lines_become_horizontal(self):
        image = slanted_page()
        self.assertGreater(abs(tilt_degrees(image)), 1.0)
        fixed, _ = rectify(image, corners_for(0.03))
        self.assertLess(abs(tilt_degrees(fixed)), 0.6)

    def test_corner_order_does_not_matter(self):
        image = slanted_page()
        ordered_a, points_a = rectify(image, corners_for(0.03))
        shuffled = corners_for(0.03)
        shuffled = [shuffled[2], shuffled[0], shuffled[3], shuffled[1]]
        ordered_b, points_b = rectify(image, shuffled)
        self.assertEqual(ordered_a.size, ordered_b.size)
        self.assertEqual(points_a, points_b)

    def test_output_size_follows_the_quad(self):
        image = slanted_page(width=600, height=800)
        fixed, _ = rectify(image, corners_for(0.0))
        # 圈住 92% 的高度，输出高度应接近 800*0.92
        self.assertAlmostEqual(fixed.height, round(800 * 0.92), delta=4)

    def test_tiny_quad_is_rejected(self):
        with self.assertRaisesRegex(RectifyError, "太小"):
            rectify(slanted_page(), [(0.4, 0.4), (0.5, 0.4), (0.5, 0.5), (0.4, 0.5)])

    def test_mismatched_edges_are_rejected(self):
        # 上边很长、下边极短：多半是把对角线当成了边
        with self.assertRaisesRegex(RectifyError, "长度差太多"):
            rectify(slanted_page(), [(0.0, 0.1), (0.99, 0.1), (0.5, 0.9), (0.49, 0.9)])

    def test_degenerate_quad_is_rejected(self):
        with self.assertRaisesRegex(RectifyError, "无法构成四边形|太小"):
            rectify(slanted_page(), [(0.2, 0.2), (0.2, 0.2), (0.8, 0.8), (0.8, 0.8)])

    def test_wrong_point_count_is_rejected(self):
        with self.assertRaisesRegex(RectifyError, "四个角点"):
            rectify(slanted_page(), [(0.0, 0.0), (1.0, 0.0)])


class RectifyApiTests(unittest.TestCase):
    def test_saves_rectified_image_and_detects_lines(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "tilted.png"
            slanted_page().save(source)
            uploads = root / "uploads"

            with patch.object(web_module, "UPLOAD_ROOT", uploads), patch.object(
                web_module, "PROJECT_ROOT", root
            ):
                result = web_module.rectify_background(str(source), [list(point) for point in corners_for(0.03)])

            target = root / result["path"]
            self.assertTrue(target.is_file())
            with Image.open(target) as saved:
                self.assertEqual(result["size"], list(saved.size))
            self.assertGreaterEqual(len(result["corners"]), 4)
            # 校正后的图应该能自动检出横线
            self.assertGreater(result["lines"]["count"], 10)

    def test_rejects_out_of_range_corner(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "a.png"
            slanted_page().save(source)
            with self.assertRaisesRegex(ValueError, "图片范围内"):
                web_module.rectify_background(str(source), [[0.0, 0.0], [1.5, 0.0], [1.0, 1.0], [0.0, 1.0]])

    def test_rejects_wrong_corner_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "a.png"
            slanted_page().save(source)
            with self.assertRaisesRegex(ValueError, "四个角点"):
                web_module.rectify_background(str(source), [[0.0, 0.0], [1.0, 0.0]])

    def test_applies_exif_orientation_before_warping(self):
        """浏览器按 EXIF 方向显示照片；摆正必须基于同一个方向，否则角点全错。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "手机.jpg"
            slanted_page().save(source, exif=self._exif_with_orientation(6))
            uploads = root / "uploads"
            corners = [[0.05, 0.05], [0.95, 0.05], [0.95, 0.95], [0.05, 0.95]]

            with patch.object(web_module, "UPLOAD_ROOT", uploads), patch.object(
                web_module, "PROJECT_ROOT", root
            ):
                result = web_module.rectify_background(str(source), corners)

            # 原图 600×800，EXIF 6 表示要顺时针转 90°，显示尺寸是 800×600。
            self.assertGreater(result["size"][0], result["size"][1])

    def test_repeated_rectify_does_not_pile_up_suffixes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "纸.png"
            slanted_page().save(source)
            uploads = root / "uploads"

            with patch.object(web_module, "UPLOAD_ROOT", uploads), patch.object(
                web_module, "PROJECT_ROOT", root
            ):
                first = web_module.rectify_background(str(source), corners_for(0.03))
                second = web_module.rectify_background(first["path"], corners_for(0.03))

            self.assertEqual(first["path"], "uploads/纸-摆正.png")
            self.assertEqual(second["path"], "uploads/纸-摆正.png")

    @staticmethod
    def _exif_with_orientation(value: int) -> Image.Exif:
        exif = Image.Exif()
        exif[274] = value
        return exif


if __name__ == "__main__":
    unittest.main()
