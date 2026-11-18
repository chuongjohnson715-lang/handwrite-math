import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from markdown_to_handwrite.config import ReportConfig
from markdown_to_handwrite.markdown_parser import parse_markdown
from markdown_to_handwrite.renderer import ReportRenderer


def busy_scene(path: Path, width: int = 800, height: int = 1100) -> Path:
    """桌面有细纹理和几个点光源、纸面有淡淡格线：虚化一开，两边都看得出差别。"""
    rng = np.random.default_rng(11)
    base = np.full((height, width, 3), (86, 80, 74), dtype=np.float32)
    base += rng.normal(0, 14, base.shape)
    for y in range(0, height, 11):
        base[y : y + 2, :, :] *= 0.72
    yy, xx = np.mgrid[0:height, 0:width]
    for cx, cy in ((80, 120), (720, 300), (110, 990), (700, 1020)):
        base += np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * 9.0**2)))[..., None] * 190
    image = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))
    paper = Image.fromarray(
        np.clip(244 + rng.normal(0, 3, (860, 620, 3)), 0, 255).astype(np.uint8)
    )
    draw = ImageDraw.Draw(paper)
    for y in range(12, 860, 9):
        draw.line((0, y, 620, y), fill=(224, 227, 232), width=1)
    image.paste(paper, (90, 110))
    image.save(path)
    return path


def scene_renderer(path: Path, *, depth=0.0, bokeh=0.0, vignette=0.0) -> ReportRenderer:
    config = ReportConfig()
    config.page.dpi = 150
    config.background.style = "image"
    config.background.image = str(path)
    config.background.writing_areas = [{"x": 0.14, "y": 0.12, "w": 0.70, "h": 0.72}]
    config.background.depth_blur = depth
    config.background.bokeh = bokeh
    config.background.vignette = vignette
    config.handwriting.body_font_pt = 14
    return ReportRenderer(config)


def edges(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    """高频能量：相邻像素差的均值。虚化会把它压下去，锐利笔画会保留。"""
    crop = np.asarray(image.convert("L").crop(box), dtype=np.float32)
    return float(np.abs(np.diff(crop, axis=0)).mean() + np.abs(np.diff(crop, axis=1)).mean())


def ink_edges(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    """只量笔画边缘的梯度：把纸面纹理排除掉，虚化笔画才会让这个数变小。"""
    gray = np.asarray(image.convert("L").crop(box), dtype=np.float32)
    ink = gray < 120
    gradient = np.abs(np.diff(gray, axis=0))[:, :-1] + np.abs(np.diff(gray, axis=1))[:-1, :]
    touching = ink[:-1, :-1] | ink[1:, :-1] | ink[:-1, 1:] | ink[1:, 1:]
    if not touching.any():
        return 0.0
    return float(gradient[touching].mean())


class SceneLightTests(unittest.TestCase):
    def setUp(self):
        self.path = busy_scene(Path(tempfile.mkdtemp()) / "desk-scene.png")
        self.markdown = parse_markdown("景深测试：这一段文字必须保持锐利，不能被背景的虚化带走。" * 6)

    def _page(self, **kwargs) -> tuple[Image.Image, ReportRenderer]:
        renderer = scene_renderer(self.path, **kwargs)
        return renderer.render(self.markdown)[0], renderer

    def _bare_page(self, **kwargs) -> tuple[Image.Image, ReportRenderer]:
        """不排字的一页：只看纸面和桌面本身，避免文字的高频掩盖背景的虚化。"""
        renderer = scene_renderer(self.path, **kwargs)
        return renderer.render([])[0], renderer

    def test_zero_strength_leaves_background_untouched(self):
        """默认全关：背景一个像素都不动，老配置的输出不能变。"""
        renderer = scene_renderer(self.path)
        source = Image.new("RGBA", renderer.page.size, (120, 110, 100, 255))
        result = renderer._apply_scene_light(source)
        self.assertTrue(np.array_equal(np.asarray(source), np.asarray(result)))

    def test_paper_texture_stays_sharp_while_far_scene_blurs(self):
        """景深只虚远景：纸面的纹理要保持原样，桌面纹路必须明显变糊。"""
        plain, renderer = self._bare_page()
        shot, _ = self._bare_page(depth=0.8)
        left, top, right, bottom = renderer.areas[0]
        paper_box = (left + 30, top + 30, right - 30, bottom - 30)
        desk_box = (0, 0, renderer.page_w // 2, renderer.page_h // 7)

        paper_plain, paper_shot = edges(plain, paper_box), edges(shot, paper_box)
        desk_plain, desk_shot = edges(plain, desk_box), edges(shot, desk_box)
        self.assertGreater(paper_plain, 0.8, "纸面上应该有可量的纹理")
        self.assertGreaterEqual(
            paper_shot, paper_plain * 0.85, f"纸面被虚化了：{paper_plain:.2f} → {paper_shot:.2f}"
        )
        self.assertLess(desk_shot, desk_plain * 0.6, f"桌面没虚：{desk_plain:.2f} → {desk_shot:.2f}")

    def test_ink_stays_sharp_when_depth_blur_is_on(self):
        """字是画在纸上的，景深再重也不能把笔画糊掉。"""
        plain, renderer = self._page(depth=0.0)
        shot, _ = self._page(depth=1.0)
        left, top, right, bottom = renderer.areas[0]
        text_box = (left + 20, top + 20, right - 20, (top + bottom) // 2)
        ink_pixels = np.asarray(plain.convert("L").crop(text_box)) < 120
        self.assertGreater(int(ink_pixels.sum()), 500, "取样区域里应该有字")
        plain_edges = ink_edges(plain, text_box)
        self.assertGreater(plain_edges, 4.0, "取样区域里应该有字")
        self.assertGreaterEqual(
            ink_edges(shot, text_box), plain_edges * 0.98, f"笔画被虚化：{plain_edges:.2f} → {ink_edges(shot, text_box):.2f}"
        )

    def test_bokeh_only_lights_the_out_of_focus_scene(self):
        """光斑是焦外的东西：同样的虚化下，桌面要多出光斑，纸面上不能多出一片光。"""
        plain, renderer = self._page(depth=0.8)
        shot, _ = self._page(depth=0.8, bokeh=1.0)
        plain_array = np.asarray(plain.convert("L"), dtype=np.int16)
        shot_array = np.asarray(shot.convert("L"), dtype=np.int16)
        gained = (shot_array - plain_array) > 25

        left, top, right, bottom = renderer.areas[0]
        inside = gained[top + 20 : bottom - 20, left + 20 : right - 20].sum()
        outside = gained.sum() - inside
        self.assertGreater(outside, 2000, f"桌面上没看到光斑，只亮了 {outside}px")
        self.assertLess(inside, 200, f"光斑洒到纸面上了：{inside}px")

    def test_vignette_darkens_corners_but_not_the_paper(self):
        """暗角只压四周：角落变暗，纸面中间基本不动。"""
        plain, renderer = self._page()
        shot, _ = self._page(vignette=0.9)
        corner = (0, 0, renderer.page_w // 6, renderer.page_h // 8)
        left, top, right, bottom = renderer.areas[0]
        middle = (
            (left + right) // 2 - 60,
            (top + bottom) // 2 - 60,
            (left + right) // 2 + 60,
            (top + bottom) // 2 + 60,
        )

        def mean(image, box):
            return float(np.asarray(image.convert("L").crop(box), dtype=np.float32).mean())

        self.assertLess(mean(shot, corner), mean(plain, corner) * 0.8, "角落没有压暗")
        self.assertAlmostEqual(
            mean(shot, middle) / mean(plain, middle), 1.0, delta=0.04, msg="纸面中间被暗角压暗了"
        )


if __name__ == "__main__":
    unittest.main()