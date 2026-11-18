from __future__ import annotations

import sys
import math
import random
import statistics
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps
from reportlab.pdfgen import canvas

try:
    import fitz
except ImportError:  # pragma: no cover - optional until a PDF background is used
    fitz = None

from .config import ReportConfig, color, mm_to_px, pt_to_px
from .handwriting import (
    HandwritingEngine,
    _stable_seed,
    starts_with_forbidden_line_punctuation,
    wrap_text,
)
from .math_renderer import FormulaRenderer, LatexRenderError
from .markdown_parser import (
    Block,
    CodeBlock,
    FormulaBlock,
    HeadingBlock,
    ImageBlock,
    InlinePart,
    ListBlock,
    PageBreakBlock,
    ParagraphBlock,
    RuleBlock,
    TableBlock,
)
from .typography import westernize_punctuation

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKGROUND_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}


@dataclass
class _RichLine:
    visuals: list[tuple[Image.Image, int]]
    width: int
    height: int
    baseline: int
    wrapped: bool = False


class ReportRenderer:
    def __init__(self, config: ReportConfig, base_dir: str | Path = "."):
        self.config = config
        self.base_dir = Path(base_dir)
        self.dpi = config.page.dpi
        self.page_w = mm_to_px(config.page.width_mm, self.dpi)
        self.page_h = mm_to_px(config.page.height_mm, self.dpi)
        self.page_w_pt = config.page.width_mm / 25.4 * 72
        self.page_h_pt = config.page.height_mm / 25.4 * 72
        self.margin_left = mm_to_px(config.page.margin_left_mm, self.dpi)
        self.margin_right = mm_to_px(config.page.margin_right_mm, self.dpi)
        self.margin_top = mm_to_px(config.page.margin_top_mm, self.dpi)
        self.margin_bottom = mm_to_px(config.page.margin_bottom_mm, self.dpi)
        self.content_w = self.page_w - self.margin_left - self.margin_right
        footer_gap = config.layout.footer_gap_mm if config.layout.show_page_numbers else 0
        self.bottom_limit = self.page_h - self.margin_bottom - mm_to_px(footer_gap, self.dpi)
        # 页面边距只描述纸张本身：背景线条、页边参考线和页脚都按它绘制。
        # margin_left / margin_top / content_w / bottom_limit 表示"当前书写区域"，
        # 未设置书写区域时二者完全相等，因此旧行为保持不变。
        self.page_margin_left = self.margin_left
        self.page_margin_right = self.margin_right
        self.page_margin_top = self.margin_top
        self.page_margin_bottom = self.margin_bottom
        self.page_content_w = self.content_w
        self.page_bottom_limit = self.bottom_limit
        self.areas = self._resolve_writing_areas()
        self.area_index = 0
        self.area_top = self.margin_top
        self.background_path = self._resolve_background_path()
        self.background_pdf = self._open_background_pdf()
        # 分节背景：标题命中 section_images 时换一张底图，并从新的一页开始。
        # current_background_path 才是每页真正使用的底图，background_path 只是全局默认。
        self.current_background_path = self.background_path
        self.section_images = self._resolve_section_images()
        self.section_level = self._clamp_section_level(config.background.section_level)
        self.engine = HandwritingEngine(
            config.handwriting,
            reference_size_px=pt_to_px(config.handwriting.body_font_pt, self.dpi),
            math_reference_size_px=pt_to_px(config.handwriting.math_font_pt, self.dpi),
        )
        self.formula_renderer = FormulaRenderer(self.engine, seed=config.handwriting.seed)
        self.pages: list[Image.Image] = []
        self.page: Image.Image
        self.draw: ImageDraw.ImageDraw
        # 横线吸附：把行高和所有垂直间距量化成线间距的整数倍，每行就都落在
        # 同一条横线上。不做网格量化时 grid_pitch 为 0，一切照旧。
        self.grid_pitch = 0
        self.grid_lines: list[int] = []
        self._grid_baseline: int | None = None
        # 标定记录的 y 是页面中线处的线高；横线倾斜时，x 每偏离中线 1px 线就偏移 rule_tilt。
        # 透视翻拍里越往下的线越陡，坡度按基线高度再叠上 rule_tilt_gradient。
        self.rule_tilt = self._clamp_tilt(config.background.rule_tilt)
        self.rule_tilt_gradient = self._page_tilt_gradient(config.background.rule_tilt_gradient)
        if config.background.snap_ruled_lines and len(config.background.ruled_lines) >= 3:
            positions = self._page_rule_positions(config.background.ruled_lines)
            lines = sorted({round(position * self.page_h) for position in positions})
            gaps = [later - earlier for earlier, later in zip(lines, lines[1:]) if later > earlier]
            if gaps:
                pitch = int(statistics.median(gaps))
                if pitch >= 8:
                    self.grid_pitch = pitch
                    self.grid_lines = lines
        if not self.grid_pitch:
            self.rule_tilt = 0.0
            self.rule_tilt_gradient = 0.0
        # 背景光影：对焦距离蒙版和暗角只跟页面尺寸有关，一次渲染算一遍。
        self._distance_cache: Image.Image | None = None
        self._vignette_cache: list = [0.0, None]
        self.y = self.margin_top
        self.section_counters = [0, 0, 0, 0, 0, 0]
        self.figure_counter = 0
        self._new_page()

    def render(self, blocks: list[Block]) -> list[Image.Image]:
        page_break_pending = False
        for block in blocks:
            if isinstance(block, PageBreakBlock):
                page_break_pending = True
                continue
            if page_break_pending:
                if self.y > self.area_top:
                    self._new_page()
                page_break_pending = False
            if isinstance(block, HeadingBlock):
                self._maybe_switch_section(block)
                self._draw_heading(block)
            elif isinstance(block, ParagraphBlock):
                self._draw_rich_paragraph(block.parts, no_indent=block.no_indent)
            elif isinstance(block, FormulaBlock):
                self._draw_formula(block.text)
            elif isinstance(block, TableBlock):
                self._draw_table(block)
            elif isinstance(block, ImageBlock):
                self._draw_image_placeholder(block)
            elif isinstance(block, ListBlock):
                self._draw_list(block)
            elif isinstance(block, CodeBlock):
                self._draw_code(block)
            elif isinstance(block, RuleBlock):
                self._draw_rule()
        if self.config.layout.show_page_numbers:
            self._draw_footers()
        return self.pages

    def save_pdf(self, output_path: str | Path) -> Path:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        pdf = canvas.Canvas(str(output), pagesize=(self.page_w_pt, self.page_h_pt))
        for page in self.pages:
            pdf.drawInlineImage(page.convert("RGB"), 0, 0, width=self.page_w_pt, height=self.page_h_pt)
            pdf.showPage()
        pdf.save()
        return output

    def _resolve_writing_areas(self) -> list[tuple[int, int, int, int]]:
        """Map normalized writing areas onto page pixels, in flow order."""
        areas: list[tuple[int, int, int, int]] = []
        for area in self.config.background.writing_areas:
            left = round(float(area["x"]) * self.page_w)
            top = round(float(area["y"]) * self.page_h)
            right = round((float(area["x"]) + float(area["w"])) * self.page_w)
            bottom = round((float(area["y"]) + float(area["h"])) * self.page_h)
            left = max(0, min(self.page_w - 1, left))
            top = max(0, min(self.page_h - 1, top))
            right = max(left + 1, min(self.page_w, right))
            bottom = max(top + 1, min(self.page_h, bottom))
            areas.append((left, top, right, bottom))
        return areas

    def _apply_area(self) -> None:
        """Point the layout cursor at the active writing area."""
        if not self.areas:
            # 未设置书写区域时，几何量在 __init__ 里已经等于页边距，这里不做覆盖，
            # 以免外部对 content_w 的显式调整在换页时被重置。
            return
        left, top, right, bottom = self.areas[self.area_index]
        self.margin_left = left
        self.area_top = top
        self.content_w = right - left
        self.bottom_limit = bottom

    @staticmethod
    def _clamp_tilt(value: object) -> float:
        """横线倾斜量取页面像素坡度，限制在 ±0.5（约 26°）内。"""
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return 0.0
        return max(-0.5, min(0.5, float(value)))

    def _page_tilt_gradient(self, value: object) -> float:
        """把图片像素里的坡度变化换算成页面像素，并限制在 ±0.0008/px 内。"""
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return 0.0
        cover = self._background_cover()
        gradient = float(value) / cover[0] if cover is not None else float(value)
        return max(-0.0008, min(0.0008, gradient))

    def _line_tilt(self, baseline_y: float) -> float:
        """这条基线所在横线的坡度：透视照片里越靠近纸面的下端，横线越陡。"""
        if not self.rule_tilt_gradient:
            return self.rule_tilt
        return self.rule_tilt + self.rule_tilt_gradient * (baseline_y - self.page_h / 2.0)

    def _tilt_offset(self, x: float, width: float = 0.0, baseline_y: float | None = None) -> float:
        """横线在 ``x`` 处相对页面中线的偏移量。

        标定得到的线高记在中线上，整行跟着倾斜时每个字块要按自己的 x 加减这段偏移。
        """
        tilt = self.rule_tilt if baseline_y is None else self._line_tilt(baseline_y)
        if not tilt:
            return 0.0
        return tilt * (x + width / 2.0 - self.page_w / 2.0)

    def _background_cover(self) -> tuple[float, float, int] | None:
        """背景图按 cover 铺满页面时的缩放、上下裁切量和原图高度。"""
        path = self.current_background_path
        if path is None or path.suffix.lower() == ".pdf":
            return None
        try:
            with Image.open(path) as image:
                image_w, image_h = image.size
        except OSError:
            return None
        if image_w <= 0 or image_h <= 0:
            return None
        scale = max(self.page_w / image_w, self.page_h / image_h)
        return scale, (image_h * scale - self.page_h) / 2.0, image_h

    def _page_rule_positions(self, positions: list[float]) -> list[float]:
        """把标定用的图片坐标换算成页面坐标。

        背景图按 cover 铺满纸张（ImageOps.fit），会裁掉较长的那一边；用户是在
        整张图上点的线，竖图直接按比例乘页面高度会把整排线推偏，必须走同一套变换。
        """
        cover = self._background_cover()
        if cover is None:
            return positions
        scale, offset_y, image_h = cover
        return [(position * image_h * scale - offset_y) / self.page_h for position in positions]

    def _snap_grid(self, value: int) -> int:
        """把一段垂直距离量化成横线间距的整数倍（至少一格）。"""
        if not self.grid_pitch or value <= 0:
            return value
        steps = max(1, int(round(value / self.grid_pitch)))
        return steps * self.grid_pitch

    def _snap_baseline(self, y: float, baseline: int) -> int:
        """把这一行的实际基线挪到最近的横线上。

        只量化行高不够：标题、公式、表格和图注的推进量不是线距的整数倍，
        误差会一直传到后面每一行。直接在落笔前按真实基线对齐，多高的字号都能压线。
        对齐以页面中线为准：倾斜的横线由落字时按字块 x 逐个偏移，行与行的间距才会保持线距。
        """
        if not self.grid_pitch or not self.grid_lines or baseline <= 0:
            return round(y)
        target = round(y) + baseline
        if self._grid_baseline is not None and abs(target - self._grid_baseline) <= 1:
            return round(y)
        # 上一行占住的那条线不能重复用，否则两行会叠在一起。
        floor = -1 if self._grid_baseline is None else self._grid_baseline + self.grid_pitch * 0.75
        candidates = [line for line in self.grid_lines if line >= floor]
        if not candidates:
            return round(y)
        nearest = min(candidates, key=lambda line: abs(line - target))
        self._grid_baseline = nearest
        return round(nearest - baseline)

    def _align_to_grid(self, top: int) -> int:
        """把首行基线挪到 ``top`` 之后的第一条横线上。"""
        if not self.grid_pitch or not self.grid_lines:
            return top
        ascent = round(pt_to_px(self.config.handwriting.body_font_pt, self.dpi) * 0.76)
        for line in self.grid_lines:
            if line - ascent >= top:
                return line - ascent
        return top

    def _new_page(self) -> None:
        self.page = self._make_background()
        self.draw = ImageDraw.Draw(self.page)
        self.pages.append(self.page)
        self.area_index = 0
        self._apply_area()
        self._grid_baseline = None
        self.y = self._align_to_grid(self.area_top)

    def _next_writing_area(self) -> None:
        """Move to the next area on this page, or start a new page when exhausted."""
        if self.area_index + 1 < len(self.areas):
            self.area_index += 1
            self._apply_area()
            self._grid_baseline = None
            self.y = self._align_to_grid(self.area_top)
            return
        self._new_page()

    def _maybe_switch_section(self, block: HeadingBlock) -> None:
        """标题命中分节背景时，换底图并另起一页。

        分节背景只对"这一节之后的所有页"生效；没命中的标题沿用当前底图。
        命中时如果当前页已经写了字就开新页，还没写字就直接换掉这一页的底。
        """
        if not self.section_images or block.level != self.section_level:
            return
        heading = "".join(part.text for part in block.parts).strip()
        target = self.section_images.get(heading)
        if target is None or target == self.current_background_path:
            return
        self.current_background_path = target
        if self.y > self.area_top:
            self._new_page()
        elif self.pages:
            # 首页/空页：背景在 _new_page 时已经画好了，退掉重画。
            self.pages.pop()
            self._new_page()

    def _apply_scene_light(self, background: Image.Image) -> Image.Image:
        """给背景照片补上真实场景的光学感：焦外虚化、光斑和暗角。

        只动背景层。正文、涂写和标定出来的横线都画在背景之后，始终保持锐利。
        """
        bg = self.config.background
        depth = self._clamp_unit(bg.depth_blur)
        bokeh = self._clamp_unit(bg.bokeh)
        vignette = self._clamp_unit(bg.vignette)
        if not (depth or bokeh or vignette):
            return background

        image = background
        if depth or bokeh:
            distance = self._distance_mask()
            if depth:
                radius = max(1.0, self.dpi / 300.0 * 11.0 * depth)
                near_blur = image.filter(ImageFilter.GaussianBlur(radius))
                far_blur = image.filter(ImageFilter.GaussianBlur(radius * 2.6))
                image = Image.composite(near_blur, image, distance)
                # 离纸面更远的地方再压一层更重的虚化，近景和远景才分得开。
                farther = distance.point(lambda value: max(0, min(255, (value - 90) * 255 // 165)))
                image = Image.composite(far_blur, image, farther)
            if bokeh:
                image = self._paint_bokeh(image, distance, bokeh)
        if vignette:
            image = self._darken_corners(image, vignette)
        return image

    @staticmethod
    def _clamp_unit(value: object) -> float:
        """光斑、暗角这类强度取 0-1，非法值按关闭处理。"""
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return 0.0
        return max(0.0, min(1.0, float(value)))

    def _distance_mask(self) -> Image.Image:
        """离纸面有多远：0 是纸面（保持锐利），255 是远景（虚化）。

        对焦范围直接取书写区域：用户是在纸面上框的，那里必须最清楚。
        """
        if self._distance_cache is not None:
            return self._distance_cache
        scale = 8
        small_w, small_h = max(1, self.page_w // scale), max(1, self.page_h // scale)
        near = Image.new("L", (small_w, small_h), 0)
        draw = ImageDraw.Draw(near)
        grow_x = round(self.page_w * 0.035 / scale)
        grow_y = round(self.page_h * 0.035 / scale)
        rects = self.areas or [
            (
                self.page_margin_left,
                self.page_margin_top,
                self.page_w - self.page_margin_right,
                self.page_h - self.page_margin_bottom,
            )
        ]
        for left, top, right, bottom in rects:
            draw.rounded_rectangle(
                (left // scale - grow_x, top // scale - grow_y, right // scale + grow_x, bottom // scale + grow_y),
                radius=max(1, round(self.page_w * 0.025 / scale)),
                fill=255,
            )
        feather = max(2, round(self.page_w * 0.05 / scale))
        far = ImageOps.invert(near.filter(ImageFilter.GaussianBlur(feather)))
        self._distance_cache = far.resize((self.page_w, self.page_h), Image.Resampling.BILINEAR)
        return self._distance_cache

    def _paint_bokeh(self, image: Image.Image, distance: Image.Image, strength: float) -> Image.Image:
        """在虚化的远景里点出焦外光斑。

        光斑只来自焦外的**点**光源，所以在亮部里再挑“比周围明显更亮”的峰值：
        整片亮着的屏幕、白墙不会长出光斑。
        """
        cell = 8
        small_size = (max(1, self.page_w // cell), max(1, self.page_h // cell))
        luma_img = image.convert("L").resize(small_size, Image.Resampling.BOX)
        luma = np.asarray(luma_img, dtype=np.float32)
        local = np.asarray(luma_img.filter(ImageFilter.GaussianBlur(cell * 3)), dtype=np.float32)
        far = np.asarray(distance.resize(small_size, Image.Resampling.BILINEAR), dtype=np.float32)
        highlight = luma - local
        ys, xs = np.nonzero((highlight > 6) & (luma > 100) & (far > 120))
        if not len(xs):
            return image
        order = np.argsort(highlight[ys, xs])[::-1]
        limit = int(4 + 22 * strength)
        gap = self.page_w * 0.07
        spots: list[tuple[int, int]] = []
        for rank in order:
            x, y = int(xs[rank]) * cell, int(ys[rank]) * cell
            if any((x - sx) ** 2 + (y - sy) ** 2 < gap * gap for sx, sy in spots):
                continue
            spots.append((x, y))
            if len(spots) >= limit:
                break

        layer = Image.new("RGB", image.size, (0, 0, 0))
        rng = random.Random(_stable_seed(self.config.handwriting.seed, "bokeh"))
        for x, y in spots:
            radius = max(2, round(self.page_w * (0.008 + 0.010 * strength) * rng.uniform(0.7, 1.4)))
            colour = image.getpixel((min(image.width - 1, x), min(image.height - 1, y)))
            # 亮背景上如果直接叠满，光斑会顶到 255 变成白圆片；按本地剩多少余量收一收。
            headroom = max(0.0, 1.0 - luma[min(small_size[1] - 1, y // cell), min(small_size[0] - 1, x // cell)] / 255.0)
            sprite = self._bokeh_sprite(radius, colour, strength, 0.3 + 0.7 * headroom)
            left, top = x - radius, y - radius
            window = (
                max(0, left),
                max(0, top),
                min(image.width, left + sprite.width),
                min(image.height, top + sprite.height),
            )
            if window[2] <= window[0] or window[3] <= window[1]:
                continue
            source = sprite.crop((window[0] - left, window[1] - top, window[2] - left, window[3] - top))
            layer.paste(ImageChops.add(layer.crop(window), source), window[:2])
        return ImageChops.add(image.convert("RGB"), layer).convert("RGBA")

    @staticmethod
    def _bokeh_sprite(radius: int, colour: tuple[int, ...], strength: float, scale: float = 1.0) -> Image.Image:
        """一枚焦外光斑：中间略平、边缘更亮，外面一圈软过渡。"""
        size = radius * 2 + 1
        yy, xx = np.mgrid[0:size, 0:size]
        rr = np.hypot(xx - radius, yy - radius) / max(1, radius)
        edge = np.clip((1.0 - rr) / 0.16, 0.0, 1.0)
        rim = 0.42 + 0.58 * np.exp(-(((rr - 0.80) / 0.15) ** 2))
        level = edge * rim * (0.10 + 0.30 * strength) * scale
        tint = np.array(colour[:3], dtype=np.float32) * 0.55 + 255.0 * 0.45
        rgb = tint[None, None, :] * level[..., None]
        return Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB")

    def _darken_corners(self, image: Image.Image, strength: float) -> Image.Image:
        """四周压暗，把视线收到纸面上。"""
        if self._vignette_cache[0] != strength:
            width, height = max(2, self.page_w // 8), max(2, self.page_h // 8)
            xs = np.linspace(-1.0, 1.0, width, dtype=np.float32)
            ys = np.linspace(-1.0, 1.0, height, dtype=np.float32)
            falloff = np.clip((ys[:, None] ** 2 + xs[None, :] ** 2) / 2.0, 0.0, 1.0) ** 1.3
            level = np.clip(1.0 - strength * 0.55 * falloff, 0.0, 1.0)
            shade = Image.fromarray((level * 255).astype(np.uint8), "L").resize(
                (self.page_w, self.page_h), Image.Resampling.BICUBIC
            )
            self._vignette_cache[:] = [strength, shade.convert("RGB")]
        return ImageChops.multiply(image.convert("RGB"), self._vignette_cache[1]).convert("RGBA")

    def _make_background(self) -> Image.Image:
        bg_config = self.config.background
        if self.current_background_path:
            image = self._read_background_page(len(self.pages))
            fitted = ImageOps.fit(image, (self.page_w, self.page_h), method=Image.Resampling.LANCZOS)
            return self._apply_scene_light(fitted.convert("RGBA"))

        page = Image.new("RGBA", (self.page_w, self.page_h), color(bg_config.paper_color, 255))
        draw = ImageDraw.Draw(page)
        line_color = color(bg_config.line_color, 160)
        style = bg_config.style.lower()
        if style == "lined":
            gap = mm_to_px(bg_config.line_gap_mm, self.dpi)
            for y in range(self.page_margin_top, self.page_h - self.page_margin_bottom + 1, gap):
                draw.line((self.page_margin_left // 2, y, self.page_w - self.page_margin_right // 2, y), fill=line_color, width=1)
        elif style == "grid":
            gap = mm_to_px(bg_config.grid_size_mm, self.dpi)
            for x in range(self.page_margin_left // 2, self.page_w - self.page_margin_right // 2 + 1, gap):
                draw.line((x, self.page_margin_top // 2, x, self.page_h - self.page_margin_bottom // 2), fill=line_color, width=1)
            for y in range(self.page_margin_top // 2, self.page_h - self.page_margin_bottom // 2 + 1, gap):
                draw.line((self.page_margin_left // 2, y, self.page_w - self.page_margin_right // 2, y), fill=line_color, width=1)
        elif style == "dot":
            gap = mm_to_px(bg_config.dot_gap_mm, self.dpi)
            radius = max(1, bg_config.dot_radius_px)
            for x in range(self.page_margin_left // 2, self.page_w - self.page_margin_right // 2 + 1, gap):
                for y in range(self.page_margin_top // 2, self.page_h - self.page_margin_bottom // 2 + 1, gap):
                    draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=line_color)
        if bg_config.draw_margin_line and style in {"lined", "grid", "dot"}:
            x = self.page_margin_left - mm_to_px(3.0, self.dpi)
            draw.line((x, self.page_margin_top // 2, x, self.page_h - self.page_margin_bottom // 2), fill=color(bg_config.margin_line_color, 150), width=2)
        return page

    def _resolve_background_path(self) -> Path | None:
        configured = self.config.background.image
        if configured:
            return self._resolve_configured_path(configured)
        # Generated paper styles must remain authoritative. Automatic asset
        # discovery is only meaningful when the user explicitly selects the
        # image-backed paper mode.
        if self.config.background.style.lower() != "image" or not self.config.background.auto_discover:
            return None
        checked: set[Path] = set()
        for directory in (
            self.base_dir / "background",
            Path.cwd() / "background",
            PROJECT_ROOT / "background",
        ):
            resolved = directory.resolve()
            if resolved in checked or not resolved.is_dir():
                continue
            checked.add(resolved)
            assets = sorted(
                (item for item in resolved.iterdir() if item.is_file() and item.suffix.lower() in BACKGROUND_EXTENSIONS),
                key=lambda item: item.name.casefold(),
            )
            if assets:
                return assets[0]
        return None

    def _resolve_configured_path(self, configured: str) -> Path:
        path = Path(configured)
        candidates = [path] if path.is_absolute() else [
            self.base_dir / path,
            Path.cwd() / path,
            PROJECT_ROOT / path,
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        raise FileNotFoundError(f"Background file not found: {configured}")

    @staticmethod
    def _clamp_section_level(value: object) -> int:
        """分节标题级别限制在 1-3，非法值退回一级标题。"""
        if isinstance(value, bool) or not isinstance(value, int):
            return 1
        return min(3, max(1, value))

    def _resolve_section_images(self) -> dict[str, Path]:
        """标题原文 -> 该节底图。空标题或空路径的条目直接跳过。"""
        mapping: dict[str, Path] = {}
        for entry in self.config.background.section_images:
            if not isinstance(entry, dict):
                continue
            heading = str(entry.get("heading", "")).strip()
            image = str(entry.get("image", "")).strip()
            if not heading or not image:
                continue
            path = self._resolve_configured_path(image)
            if path.suffix.lower() == ".pdf":
                raise ValueError("分节背景只支持图片，不支持 PDF。")
            mapping[heading] = path
        return mapping

    def _open_background_pdf(self):
        if self.background_path is None or self.background_path.suffix.lower() != ".pdf":
            return None
        if fitz is None:
            raise RuntimeError("PDF backgrounds require PyMuPDF. Install it with: python -m pip install PyMuPDF")
        document = fitz.open(str(self.background_path))
        if document.page_count < 1:
            document.close()
            raise ValueError(f"PDF background has no pages: {self.background_path}")
        return document

    def _read_background_page(self, page_index: int) -> Image.Image:
        path = self.current_background_path or self.background_path
        if path is None:
            raise FileNotFoundError("No background file is configured.")
        if self.background_pdf is not None and path == self.background_path:
            source_page = self.background_pdf.load_page(page_index % self.background_pdf.page_count)
            scale = self.dpi / 72.0
            pixmap = source_page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        return Image.open(path).convert("RGB")

    def _ensure_space(self, height: int) -> None:
        if self.y + height <= self.bottom_limit:
            return
        if not self.areas:
            # 未设置书写区域时保持旧分页行为：直接开新页。
            self._new_page()
            return
        if self.y > self.area_top:
            # 当前区域已经写过内容，换到下一个区域；区域用尽则开新页。
            self._next_writing_area()
            return
        # 区域一开始就放不下这个块：继续向后找更宽裕的区域；全部放不下时留在原地溢出，
        # 避免在同一次排版里反复换页。
        while self.area_index + 1 < len(self.areas):
            self._next_writing_area()
            if self.y + height <= self.bottom_limit:
                return

    def _draw_heading(self, block: HeadingBlock) -> None:
        level = min(max(block.level, 1), 6)
        parts = list(block.parts)
        if self.config.layout.number_sections and level <= 3:
            parts.insert(0, InlinePart("text", f"{self._next_section_number(level)} "))
        font_pt = {
            1: self.config.handwriting.h1_font_pt,
            2: self.config.handwriting.h2_font_pt,
            3: self.config.handwriting.h3_font_pt,
        }.get(level, self.config.handwriting.body_font_pt)
        size = pt_to_px(font_pt, self.dpi)
        line_h = self._snap_grid(round(size * self.config.handwriting.line_spacing))
        gap_before = self._snap_grid(mm_to_px(self.config.layout.heading_gap_before_mm if len(self.pages) > 1 or self.y > self.area_top else 0, self.dpi))
        gap_after = self._snap_grid(mm_to_px(self.config.layout.heading_gap_after_mm, self.dpi))
        lines = self._layout_rich_parts(
            parts,
            size,
            self.content_w,
            line_h,
            seed_extra=f"heading:{len(self.pages)}:{self.y}",
        )
        total_h = gap_before + sum(line.height for line in lines) + gap_after
        self._ensure_space(total_h)
        self.y += gap_before
        align = "center" if level == 1 else "left"
        for line in lines:
            self._paste_rich_line(line, self.margin_left, self.y, self.content_w, align=align)
            self.y += line.height
        self.y += gap_after

    def _next_section_number(self, level: int) -> str:
        self.section_counters[level - 1] += 1
        for index in range(level, len(self.section_counters)):
            self.section_counters[index] = 0
        return ".".join(str(value) for value in self.section_counters[:level] if value)

    def _draw_paragraph(self, text: str) -> None:
        if not text:
            return
        parts: list[InlinePart] = []
        for index, line in enumerate(text.split("\n")):
            if index:
                parts.append(InlinePart("hardbreak"))
            parts.append(InlinePart("text", line))
        self._draw_rich_paragraph(parts)

    def _draw_rich_paragraph(self, parts: list[InlinePart], no_indent: bool = False) -> None:
        size = pt_to_px(self.config.handwriting.body_font_pt, self.dpi)
        normal_line_h = self._snap_grid(round(size * self.config.handwriting.line_spacing))
        gap = self._snap_grid(mm_to_px(self.config.layout.paragraph_gap_mm, self.dpi))
        indent = 0 if no_indent else min(max(0, self.content_w - 1), round(size * self.config.layout.first_line_indent_em))
        lines = self._layout_rich_parts(
            parts,
            size,
            self.content_w,
            normal_line_h,
            seed_extra=f"paragraph:{len(self.pages)}:{self.y}",
            first_line_width=self.content_w - indent,
        )
        for line_index, line in enumerate(lines):
            self._ensure_space(line.height)
            # 区域切换会改变可用宽度，因此缩进与行宽必须在分页判断之后重新计算。
            indent_now = 0 if no_indent else min(max(0, self.content_w - 1), indent)
            is_first = line_index == 0
            x = self.margin_left + (indent_now if is_first else 0)
            width = self.content_w - (indent_now if is_first else 0)
            self._paste_rich_line(
                line, x, self.y, width,
                justify=self.config.layout.justify_paragraphs and line.wrapped and line.width >= width * 0.78,
                scribble_size=size,
            )
            self.y += line.height
        self.y += gap

    def _layout_rich_parts(
        self,
        parts: list[InlinePart],
        size: int,
        max_width: int,
        normal_line_h: int,
        seed_extra: str,
        first_line_width: int | None = None,
    ) -> list[_RichLine]:
        """Lay out mixed handwriting and LaTeX for every inline-capable block."""
        # A group is a word/character/formula together with its closing
        # punctuation. It moves to the next line as a unit whenever possible.
        groups: list[list[tuple[str, object]]] = []
        for part_index, part in enumerate(parts):
            if part.kind in {"break", "hardbreak"}:
                groups.append([(part.kind, "")])
            elif part.kind == "math":
                try:
                    box = self.formula_renderer.render_inline(
                        part.text, size, max(1, max_width),
                        seed_extra=f"{seed_extra}:math:{part_index}",
                    )
                except LatexRenderError as error:
                    self._report_formula_error(part.text, error)
                    box = self.formula_renderer.blank_inline(part.text, size, max(1, max_width))
                groups.append([("math", box)])
            else:
                for unit in _inline_units(westernize_punctuation(part.text)):
                    if starts_with_forbidden_line_punctuation(unit) and groups and groups[-1][0][0] not in {"break", "hardbreak"}:
                        groups[-1].append(("text", unit))
                    else:
                        groups.append([("text", unit)])

        pending = deque(groups)
        lines: list[_RichLine] = []
        measurements: dict[str, float] = {}

        def measure(text: str) -> float:
            if text not in measurements:
                measurements[text] = self.engine.measure(text, size)
            return measurements[text]

        def nominal_width(items: list[tuple[str, object]]) -> float:
            width = 0.0
            buffer = ""
            for kind, value in items:
                if kind == "text":
                    buffer += str(value)
                else:
                    width += measure(buffer) + value.width
                    buffer = ""
            return width + measure(buffer)

        def flatten(selected):
            return [item for group in selected for item in group]

        def whitespace(group) -> bool:
            return all(kind == "text" and not str(value).strip() for kind, value in group)

        while pending:
            line_width = max(1, first_line_width if not lines and first_line_width is not None else max_width)
            selected: list[list[tuple[str, object]]] = []
            while pending:
                group = pending[0]
                if group[0][0] in {"break", "hardbreak"}:
                    break
                if not selected and whitespace(group):
                    pending.popleft()
                    continue
                if selected and nominal_width(flatten([*selected, group])) > line_width:
                    break
                selected.append(pending.popleft())
                if nominal_width(flatten(selected)) > line_width:
                    break
            while selected and whitespace(selected[-1]):
                selected.pop()
            if not selected:
                if pending and pending[0][0][0] in {"break", "hardbreak"}:
                    if pending.popleft()[0][0] == "hardbreak":
                        lines.append(_RichLine([], 0, normal_line_h, round(size * 0.76)))
                continue

            line_seed = f"{seed_extra}:line:{len(lines)}"
            line = self._render_rich_items(flatten(selected), size, line_width, normal_line_h, line_seed)
            # Font overhang, ink perturbations and image padding all contribute
            # to the visible width. Reflow using that width before pasting.
            while line.width > line_width and len(selected) > 1:
                pending.appendleft(selected.pop())
                while selected and whitespace(selected[-1]):
                    pending.appendleft(selected.pop())
                line = self._render_rich_items(flatten(selected), size, line_width, normal_line_h, line_seed)
            if line.width > line_width and all(kind == "text" for kind, _ in selected[0]):
                text = "".join(str(value) for _, value in selected[0])
                # An overlong word must be splittable, including its first line.
                low, high, cut = 1, len(text) - 1, 0
                while low <= high:
                    middle = (low + high) // 2
                    candidate = self._render_rich_items([("text", text[:middle])], size, line_width, normal_line_h, line_seed)
                    if candidate.width <= line_width:
                        cut = middle
                        low = middle + 1
                    else:
                        high = middle - 1
                cut = max(1, cut)
                preferred = cut
                while preferred > 0 and starts_with_forbidden_line_punctuation(text[preferred:]):
                    preferred -= 1
                # A punctuation sequence longer than a whole line is an
                # unavoidable exception to the usual no-leading-punctuation rule.
                cut = preferred or cut
                if cut < len(text):
                    pending.appendleft([("text", text[cut:])])
                    line = self._render_rich_items([("text", text[:cut])], size, line_width, normal_line_h, line_seed)
            if line.width > line_width:
                # A single formula or glyph may exceed even an empty line.
                line = self._fit_rich_line(line, line_width, normal_line_h)
            while pending and whitespace(pending[0]):
                pending.popleft()
            if pending and pending[0][0][0] in {"break", "hardbreak"}:
                pending.popleft()
            else:
                line.wrapped = bool(pending)
            lines.append(line)
        return lines

    def _render_rich_items(
        self, items: list[tuple[str, object]], size: int, max_width: int,
        normal_line_h: int, seed_extra: str,
    ) -> _RichLine:
        visuals: list[tuple[Image.Image, int]] = []
        text_buffer = ""

        def blank_space(text: str) -> None:
            if text:
                width = max(1, math.ceil(self.engine.measure(text, size)))
                visuals.append((Image.new("RGBA", (width, size)), round(size * 0.76)))

        def flush_text() -> None:
            nonlocal text_buffer
            if not text_buffer:
                return
            core = text_buffer.strip()
            if not core:
                blank_space(text_buffer)
            else:
                leading = len(text_buffer) - len(text_buffer.lstrip())
                trailing = len(text_buffer) - len(text_buffer.rstrip())
                blank_space(text_buffer[:leading])
                image, baseline = self.engine.render_line(
                    core, size, max_width,
                    seed_extra=f"{seed_extra}:text:{len(visuals)}", return_baseline=True,
                )
                visuals.append((image, baseline))
                if trailing:
                    blank_space(text_buffer[-trailing:])
            text_buffer = ""

        for kind, value in items:
            if kind == "text":
                text_buffer += str(value)
            else:
                flush_text()
                visuals.append((value.image, value.baseline))
        flush_text()
        baseline = max((base for _, base in visuals), default=round(size * 0.76))
        descent = max((image.height - base for image, base in visuals), default=normal_line_h - baseline)
        return _RichLine(visuals, sum(image.width for image, _ in visuals), max(normal_line_h, baseline + descent), baseline)

    @staticmethod
    def _fit_rich_line(line: _RichLine, width: int, normal_line_h: int) -> _RichLine:
        if len(line.visuals) == 1:
            image = line.visuals[0][0]
        else:
            image = Image.new("RGBA", (line.width, line.height))
            x = 0
            for visual, baseline in line.visuals:
                image.alpha_composite(visual, (x, line.baseline - baseline))
                x += visual.width
        scale = width / line.width
        image = image.resize((width, max(1, round(image.height * scale))), Image.Resampling.LANCZOS)
        baseline = max(1, round(line.baseline * scale))
        return _RichLine([(image, baseline)], width, max(normal_line_h, image.height), baseline)

    def _paste_rich_line(
        self,
        line: _RichLine,
        x: int,
        y: int,
        max_width: int,
        align: str = "left",
        justify: bool = False,
        scribble_size: int | None = None,
        snap: bool = True,
    ) -> None:
        if align == "center":
            x += max(0, (max_width - line.width) // 2)
        elif align == "right":
            x += max(0, max_width - line.width)
        stretched = justify and align == "left" and line.width > 0 and 1.0 < max_width / line.width <= 1.18
        span = max_width if stretched else line.width
        if snap:
            y = self._snap_baseline(y, line.baseline)
        if (self.rule_tilt or self.rule_tilt_gradient) and line.visuals:
            self._paste_tilted_line(line, x, y, span, stretched)
        else:
            scale = max_width / line.width if stretched else 1.0
            cursor_x = float(x)
            for image, item_baseline in line.visuals:
                if scale != 1.0:
                    glyph_w = max(1, round(image.width * scale))
                    glyph_h = max(1, round(image.height * scale))
                    if (glyph_w, glyph_h) != image.size:
                        image = image.resize((glyph_w, glyph_h), Image.Resampling.BICUBIC)
                    item_baseline = round(item_baseline * scale)
                self.page.alpha_composite(
                    image, (round(cursor_x), round(y + line.baseline - item_baseline))
                )
                cursor_x += image.width
        if scribble_size:
            self._scribble_line(x, y + line.baseline, span, scribble_size, seed_extra=f"{len(self.pages)}:{x}:{y}")

    def _paste_tilted_line(self, line: _RichLine, x: float, y: float, span: int, stretched: bool) -> None:
        """把整行墨迹按横线斜率斜切后落笔。

        一行 CJK 通常是一整块栅格；逐字挪会有接缝，整行做一次竖直斜切既保住原本的
        笔迹扰动，又能让每个字都压在倾斜的横线上。
        """
        image = Image.new("RGBA", (line.width, line.height))
        baseline = line.baseline
        absolute_baseline = y + line.baseline
        tilt = self._line_tilt(absolute_baseline)
        cursor = 0
        for visual, item_baseline in line.visuals:
            image.alpha_composite(visual, (cursor, baseline - item_baseline))
            cursor += visual.width
        width = span
        if stretched:
            scale = span / line.width
            image = image.resize((width, max(1, round(line.height * scale))), Image.Resampling.BICUBIC)
            baseline = max(1, round(baseline * scale))
        pad = int(math.ceil(abs(tilt) * width)) + 2
        stage = Image.new(
            "RGBA", (width, image.height + pad * 2), color(self.config.handwriting.ink_color, 0)
        )
        stage.alpha_composite(image, (0, 0))
        sheared = stage.transform(
            stage.size,
            Image.Transform.AFFINE,
            # 逆映射：输出 (xo, yo) 取自 y = yo - tilt * (xo - W/2) - pad，内容整体下移 pad 留在画布内。
            (1, 0, 0, -tilt, 1, -pad + tilt * width / 2),
            resample=Image.Resampling.BICUBIC,
        )
        # 斜切以这一行自己的中线为准，吸附以页面中线为准：两者差多少在这里补回来。
        offset = y + line.baseline - baseline - pad + self._tilt_offset(x, width, absolute_baseline)
        self.page.alpha_composite(sheared, (round(x), round(offset)))

    def _scribble_line(
        self, x: float, baseline_y: float, width: int, size: int, seed_extra: str
    ) -> None:
        """按概率在刚写好的一行上叠一手"划掉"式涂写。"""
        probability = self.config.handwriting.scribble_probability
        if probability <= 0 or width <= 0 or size <= 0:
            return
        rng = random.Random(_stable_seed(self.config.handwriting.seed, "scribble", seed_extra))
        if rng.random() >= min(1.0, probability):
            return

        center_y = baseline_y - size * rng.uniform(0.32, 0.42)
        amplitude = size * rng.uniform(0.12, 0.20)
        pen = max(1.0, size * rng.uniform(0.05, 0.072))
        # 笔迹两端留出笔宽和重采样余量，避免涂写越过正文列宽蹭到页边。
        inset = min(max(pen, 3.0) + 1.0, width / 4)
        usable = width - inset * 2
        if usable <= 1:
            return
        span = min(usable, max(size * 2.4, usable * rng.uniform(0.22, 0.55)))
        span_x = x + inset + rng.uniform(0, max(0.0, usable - span))
        margin = int(math.ceil(pen)) + 2
        tile_w = int(math.ceil(span)) + margin * 2
        # 斜线纸上的涂写也要跟着线走，画布按倾斜后的最大落差预留。
        tilt = self._line_tilt(baseline_y)
        # 竖向上限由振幅、倾斜和手抖共同决定，画布按最坏情况预留。
        reach = amplitude * 1.7 + 0.035 * span + pen + abs(tilt) * tile_w / 2
        tile_h = int(math.ceil(reach * 2)) + 4
        center_local = tile_h / 2
        # 基线在涂写自身中线处的真实高度：外层的 baseline_y 是这一行起笔处的高度。
        center_y += tilt * (span_x - margin + tile_w / 2 - self.page_w / 2)

        # 笔画在 3 倍画布上绘制再缩小，等价于超采样抗锯齿；几笔叠加才有"涂掉"的实感。
        supersample = 3
        tile = Image.new("RGBA", (tile_w * supersample, tile_h * supersample))
        draw = ImageDraw.Draw(tile)
        ink = color(self.config.handwriting.ink_color, 228)
        strokes = rng.randint(2, 3)
        for index in range(strokes):
            phase = rng.uniform(0, math.tau)
            cycle_length = size * rng.uniform(0.4, 0.7)
            drift = rng.uniform(-0.35, 0.35) * amplitude
            slope = rng.uniform(-0.07, 0.07) * span
            stroke_amplitude = amplitude * rng.uniform(0.5, 1.2)
            stroke_pen = pen * rng.uniform(0.75, 1.1)
            offset_y = (index - (strokes - 1) / 2) * pen * 1.5
            reverse = rng.random() < 0.45
            modulation_phase = rng.uniform(0, math.tau)
            tremor_phase = rng.uniform(0, math.tau)
            start = rng.uniform(0, 0.08) * span
            end = span - rng.uniform(0, 0.08) * span
            samples = max(16, int((end - start) / 2))
            points = []
            for step in range(samples + 1):
                t = step / samples
                # 正弦载波叠低频包络和高频手抖，避免出现机械的标准波浪线。
                modulation = 0.85 + 0.15 * math.sin(math.tau * 1.7 * t + modulation_phase)
                tremor = 0.12 * amplitude * math.sin(math.tau * 9.0 * t + tremor_phase)
                point_x = margin + (start + (end - start) * (1 - t if reverse else t))
                point_y = (
                    center_local
                    + offset_y
                    + drift * t
                    + slope * (t - 0.5)
                    + stroke_amplitude * modulation * math.sin(math.tau * (end - start) / cycle_length * t + phase)
                    + tremor
                    + tilt * (point_x - tile_w / 2)
                )
                points.append((point_x * supersample, point_y * supersample))
            draw.line(
                points,
                fill=ink,
                width=max(1, round(stroke_pen * supersample)),
                joint="curve",
            )
        tile = tile.resize((tile_w, tile_h), Image.Resampling.LANCZOS)
        self.page.alpha_composite(
            tile, (round(span_x) - margin, round(center_y - center_local))
        )

    def _render_formula_image(self, text: str, seed_extra: str) -> Image.Image:
        size = pt_to_px(self.config.handwriting.math_font_pt, self.dpi)
        try:
            return self.formula_renderer.render(
                text,
                size,
                self.content_w - mm_to_px(18, self.dpi),
                seed_extra=seed_extra,
            )
        except LatexRenderError as error:
            self._report_formula_error(text, error)
            return self.formula_renderer.blank_display(size)

    def _report_formula_error(self, latex: str, error: LatexRenderError) -> None:
        compact = " ".join(latex.split())
        print(f"[latex-render-error] {compact} ({error})", file=sys.stderr)

    def _formula_height(self, image: Image.Image) -> int:
        gap = mm_to_px(self.config.layout.formula_gap_mm, self.dpi)
        return gap * 2 + image.height

    def _draw_formula(self, text: str, image: Image.Image | None = None) -> None:
        if not text:
            return
        gap = mm_to_px(self.config.layout.formula_gap_mm, self.dpi)
        image = image or self._render_formula_image(text, seed_extra=f"{len(self.pages)}:{self.y}")
        total_h = self._formula_height(image)
        self._ensure_space(total_h)
        self.y += gap
        x = self.margin_left + (self.content_w - image.width) // 2
        self.page.alpha_composite(image, (x, self.y))
        self.y += image.height
        self.y += gap

    def _draw_list(self, block: ListBlock) -> None:
        size = pt_to_px(self.config.handwriting.body_font_pt, self.dpi)
        normal_line_h = self._snap_grid(round(size * self.config.handwriting.line_spacing))
        gap = self._snap_grid(mm_to_px(self.config.layout.paragraph_gap_mm, self.dpi))
        indent = mm_to_px(self.config.layout.list_indent_mm, self.dpi)
        for offset, item in enumerate(block.items):
            prefix = f"{block.start + offset}. " if block.ordered else "- "
            lines = self._layout_rich_parts(
                item,
                size,
                self.content_w - indent,
                normal_line_h,
                seed_extra=f"list:{len(self.pages)}:{self.y}:{offset}",
            )
            if not lines:
                lines = [_RichLine([], 0, normal_line_h, round(size * 0.76))]
            prefix_image = self.engine.render_line(
                prefix,
                size,
                indent,
                seed_extra=f"list-prefix:{len(self.pages)}:{self.y}:{offset}",
            )
            prefix_baseline = round(prefix_image.height * 0.76)
            for line_index, line in enumerate(lines):
                self._ensure_space(line.height)
                content_x = self.margin_left + indent
                content_width = self.content_w - indent
                paste_y = self._snap_baseline(self.y, line.baseline)
                if line_index == 0:
                    prefix_x = content_x - prefix_image.width
                    prefix_dy = self._tilt_offset(prefix_x, prefix_image.width, paste_y + line.baseline)
                    prefix_y = paste_y + line.baseline - prefix_baseline + prefix_dy
                    self.page.alpha_composite(prefix_image, (round(prefix_x), round(prefix_y)))
                self._paste_rich_line(line, content_x, paste_y, content_width)
                self.y += line.height
        self.y += gap

    def _draw_code(self, block: CodeBlock) -> None:
        size = pt_to_px(self.config.handwriting.code_font_pt, self.dpi)
        line_h = self._snap_grid(round(size * self.config.handwriting.line_spacing))
        pad = mm_to_px(2.0, self.dpi)
        lines: list[str] = []
        for raw in westernize_punctuation(block.text).splitlines() or [""]:
            lines.extend(wrap_text(self.engine, raw, size, self.content_w - 2 * pad))
        height = len(lines) * line_h + 2 * pad
        self._ensure_space(height + pad)
        x0 = self.margin_left
        y0 = self.y
        x1 = self.margin_left + self.content_w
        y1 = y0 + height
        self._rough_rect(x0, y0, x1, y1, color(self.config.handwriting.ink_color, 90), width=1)
        self.y += pad
        for line in lines:
            self._paste_text(line, x0 + pad, self.y, size, self.content_w - 2 * pad)
            self.y += line_h
        self.y = y1 + pad

    def _draw_image_placeholder(self, block: ImageBlock) -> None:
        height = mm_to_px(self.config.layout.image_placeholder_height_mm, self.dpi)
        caption_h = mm_to_px(9.0, self.dpi)
        self._ensure_space(height + caption_h)
        x0 = self.margin_left
        y0 = self.y
        x1 = self.margin_left + self.content_w
        y1 = y0 + height
        if self.config.layout.image_placeholder_border:
            self._rough_rect(x0, y0, x1, y1, color(self.config.handwriting.ink_color, 90), width=1)
        self.figure_counter += 1
        title = block.alt or block.src or "插图空白"
        caption = f"图{self.figure_counter}：{title}" if self.config.layout.number_figures else title
        size = pt_to_px(self.config.handwriting.body_font_pt - 1, self.dpi)
        self._paste_text(
            caption, x0, y1 + mm_to_px(1.5, self.dpi), size, self.content_w,
            align="center", preserve_punctuation=True,
        )
        self.y = y1 + caption_h

    def _draw_table(self, block: TableBlock) -> None:
        if not block.headers and not block.rows:
            return
        self.y += mm_to_px(self.config.layout.table_gap_mm / 2, self.dpi)
        size = pt_to_px(self.config.handwriting.body_font_pt - 1, self.dpi)
        line_h = self._snap_grid(round(size * self.config.handwriting.line_spacing))
        pad = mm_to_px(self.config.layout.table_cell_padding_mm, self.dpi)
        col_count = max(len(block.headers), *(len(row) for row in block.rows)) if block.rows else len(block.headers)
        widths = self._table_widths(block, col_count, size)
        header_layout = (
            self._table_row_layout(block.headers, widths, size, line_h, pad, "table:header")
            if block.headers
            else None
        )
        header_height = header_layout[1] if header_layout else 0
        if block.headers:
            self._ensure_space(header_height + line_h)
            self._draw_table_row(widths, header_layout, pad, header=True)
        draw_top = not block.headers
        for row_index, row in enumerate(block.rows):
            row_layout = self._table_row_layout(
                row,
                widths,
                size,
                line_h,
                pad,
                f"table:row:{row_index}",
            )
            row_height = row_layout[1]
            if self.y + row_height > self.bottom_limit:
                self._next_writing_area()
                if block.headers:
                    self._draw_table_row(widths, header_layout, pad, header=True)
                draw_top = not block.headers
            self._draw_table_row(widths, row_layout, pad, header=False, draw_top=draw_top)
            draw_top = False
        self.y += mm_to_px(self.config.layout.table_gap_mm, self.dpi)

    def _table_widths(self, block: TableBlock, col_count: int, size: int) -> list[int]:
        if col_count == 0:
            return []
        if self.content_w < col_count:
            raise ValueError("当前页面宽度不足以容纳这么多列，请减少列数或增大页面宽度。")
        pad = max(0, mm_to_px(self.config.layout.table_cell_padding_mm, self.dpi))
        minimum = [1.0 + 2 * pad] * col_count
        preferred = minimum.copy()
        formula_widths: dict[str, int] = {}
        text_widths: dict[str, float] = {}
        # Handwriting rasters retain four transparent pixels on each side;
        # include that footprint so a short header need not wrap by one pixel.
        ink_slack = max(8, math.ceil(size * 0.15))

        def text_width(text: str) -> float:
            if text not in text_widths:
                text_widths[text] = self.engine.measure(text, size) + (ink_slack if text.strip() else 0)
            return text_widths[text]

        def formula_width(latex: str) -> int:
            if latex not in formula_widths:
                # Measuring must not advance the live renderer's geometry or
                # ink RNGs. A fixed per-formula seed also makes column order
                # irrelevant to the width estimate.
                probe = FormulaRenderer(self.engine, seed=f"{self.config.handwriting.seed}:table:{latex}")
                try:
                    box = probe.render_inline(latex, size, self.content_w, seed_extra="table:measure")
                except LatexRenderError:
                    # The actual cell render reports the error once.
                    box = probe.blank_inline(latex, size, self.content_w)
                formula_widths[latex] = box.width
            return formula_widths[latex]

        for row in [block.headers, *block.rows]:
            for index, cell in enumerate(row[:col_count]):
                line_width = 0.0
                text_buffer = ""

                def flush_text() -> None:
                    nonlocal text_buffer, line_width
                    if text_buffer:
                        line_width += text_width(text_buffer)
                        for unit in _inline_units(text_buffer):
                            if unit.strip():
                                minimum[index] = max(minimum[index], text_width(unit) + 2 * pad)
                        text_buffer = ""

                for part in cell:
                    if part.kind in {"break", "hardbreak"}:
                        flush_text()
                        preferred[index] = max(preferred[index], line_width + 2 * pad)
                        line_width = 0.0
                    elif part.kind == "math":
                        flush_text()
                        width = formula_width(part.text)
                        line_width += width
                        minimum[index] = max(minimum[index], width + 2 * pad)
                    else:
                        text_buffer += westernize_punctuation(part.text)
                flush_text()
                preferred[index] = max(preferred[index], line_width + 2 * pad)

        # Demands wider than the whole table cannot be satisfied without
        # wrapping or scaling, and must not dominate the other columns.
        preferred = [min(self.content_w, max(want, need)) for want, need in zip(preferred, minimum)]
        minimum = [min(need, want) for need, want in zip(minimum, preferred)]
        base = mm_to_px(18, self.dpi)
        if base * col_count > self.content_w:
            base = max(1.0, self.content_w / (2 * col_count))
        widths = [min(float(base), want) for want in preferred]
        remaining = self.content_w - sum(widths)

        def grow_towards(targets: list[float]) -> None:
            nonlocal remaining
            active = [i for i in range(col_count) if targets[i] > widths[i] + 1e-9]
            while active and remaining > 1e-9:
                step = min(remaining / len(active), min(targets[i] - widths[i] for i in active))
                for i in active:
                    widths[i] += step
                remaining = max(0.0, remaining - step * len(active))
                active = [i for i in active if targets[i] > widths[i] + 1e-9]

        # Equal increments with satisfied columns frozen protect short fields
        # and formulas before spending the remaining room on long paragraphs.
        grow_towards(minimum)
        grow_towards(preferred)
        widths = [width + remaining / col_count for width in widths]
        rounded = [max(1, math.floor(width)) for width in widths]
        remainder = self.content_w - sum(rounded)
        order = sorted(range(col_count), key=lambda i: widths[i] - rounded[i], reverse=True)
        for i in order[:remainder]:
            rounded[i] += 1
        return rounded

    def _table_content_geometry(self, width: int, pad: int) -> tuple[int, int]:
        # Narrow tables sacrifice horizontal padding before squeezing their
        # content below one em. Layout and pasting must use the same budget.
        size = pt_to_px(self.config.handwriting.body_font_pt - 1, self.dpi)
        horizontal_pad = min(max(0, pad), max(0, (width - min(width, size)) // 2))
        return horizontal_pad, max(1, width - 2 * horizontal_pad)

    def _table_row_layout(
        self,
        row: list[list[InlinePart]],
        widths: list[int],
        size: int,
        line_h: int,
        pad: int,
        seed_extra: str,
    ) -> tuple[list[list[_RichLine]], int]:
        cells: list[list[_RichLine]] = []
        content_height = line_h
        for index, width in enumerate(widths):
            cell = row[index] if index < len(row) else []
            _, available = self._table_content_geometry(width, pad)
            lines = self._layout_rich_parts(
                cell,
                size,
                available,
                line_h,
                seed_extra=f"{seed_extra}:cell:{index}",
            )
            cells.append(lines)
            content_height = max(content_height, sum(line.height for line in lines))
        return cells, content_height + 2 * pad

    def _draw_table_row(
        self,
        widths: list[int],
        layout: tuple[list[list[_RichLine]], int],
        pad: int,
        header: bool,
        draw_top: bool = True,
    ) -> None:
        cells, height = layout
        x_positions = [self.margin_left]
        for width in widths:
            x_positions.append(x_positions[-1] + width)
        y0 = self.y
        y1 = self.y + height
        ink = color(self.config.handwriting.ink_color, 200 if header else 170)
        if draw_top:
            self._rough_line(x_positions[0], y0, x_positions[-1], y0, ink, width=2 if header else 1)
        self._rough_line(x_positions[0], y1, x_positions[-1], y1, ink, width=1)
        for x in x_positions:
            self._rough_line(x, y0, x, y1, ink, width=1)
        for index, width in enumerate(widths):
            y = y0 + pad
            horizontal_pad, available = self._table_content_geometry(width, pad)
            for line in cells[index]:
                # 表格的行高是统一算出来的，逐线吸附会把行挤错位。
                self._paste_rich_line(line, x_positions[index] + horizontal_pad, y, available, snap=False)
                y += line.height
        self.y = y1

    def _draw_rule(self) -> None:
        gap = mm_to_px(4.0, self.dpi)
        self._ensure_space(gap)
        self.y += gap

    def _paste_text(
        self,
        text: str,
        x: int,
        y: int,
        size: int,
        max_width: int,
        align: str = "left",
        math: bool = False,
        justify: bool = False,
        preserve_punctuation: bool = False,
    ) -> None:
        if not preserve_punctuation:
            text = westernize_punctuation(text)
        image = self.engine.render_line(text, size, max_width, seed_extra=f"{len(self.pages)}:{x}:{y}", math=math)
        if image.width > max_width:
            scale = max_width / image.width
            image = image.resize((max_width, max(1, round(image.height * scale))), Image.Resampling.LANCZOS)
        if justify and align == "left" and image.width > 1:
            stretch = max_width / image.width
            if 1.0 < stretch <= 1.18:
                image = image.resize((max_width, image.height), Image.Resampling.BICUBIC)
        if align == "center":
            x = x + max(0, (max_width - image.width) // 2)
        elif align == "right":
            x = x + max(0, max_width - image.width)
        self.page.alpha_composite(image, (round(x), round(y)))

    def _should_justify_paragraph_line(
        self,
        line: str,
        line_index: int,
        line_count: int,
        size: int,
        width: int,
    ) -> bool:
        if not self.config.layout.justify_paragraphs or line_index >= line_count - 1:
            return False
        measured = self.engine.measure(line, size)
        return measured >= width * 0.78

    def _rough_line(self, x0: int, y0: int, x1: int, y1: int, fill: tuple[int, ...], width: int = 1) -> None:
        size = pt_to_px(self.config.handwriting.body_font_pt, self.dpi)
        if y0 == y1:
            self.formula_renderer.draw_horizontal_rule(
                self.page,
                x0,
                y0,
                x1,
                size,
                width=width,
                fill=fill,
                anchor_ends=True,
                variation_scale=0.7,
            )
            return
        self.formula_renderer.draw_rule(
            self.page,
            x0,
            y0,
            x1,
            y1,
            size,
            width=width,
            fill=fill,
            anchor_ends=True,
            variation_scale=0.7,
        )

    def _rough_rect(self, x0: int, y0: int, x1: int, y1: int, fill: tuple[int, ...], width: int = 1) -> None:
        self._rough_line(x0, y0, x1, y0, fill, width)
        self._rough_line(x1, y0, x1, y1, fill, width)
        self._rough_line(x1, y1, x0, y1, fill, width)
        self._rough_line(x0, y1, x0, y0, fill, width)

    def _draw_footers(self) -> None:
        size = pt_to_px(9.5, self.dpi)
        footer_y = self.page_h - mm_to_px(self.config.page.margin_bottom_mm / 2 + 2, self.dpi)
        for index, page in enumerate(self.pages, start=1):
            self.page = page
            label = f"- {index} -"
            image = self.engine.render_line(label, size, self.page_content_w, seed_extra=f"footer-{index}")
            x = (self.page_w - image.width) // 2
            page.alpha_composite(image, (x, footer_y))


def _inline_units(text: str) -> list[str]:
    units: list[str] = []
    buffer = ""
    for char in text:
        if char.isspace():
            if buffer:
                units.append(buffer)
                buffer = ""
            units.append(" ")
        elif ord(char) < 128 and (char.isalnum() or char in "._-/^=+"):
            buffer += char
        else:
            if buffer:
                units.append(buffer)
                buffer = ""
            units.append(char)
    if buffer:
        units.append(buffer)
    return units
