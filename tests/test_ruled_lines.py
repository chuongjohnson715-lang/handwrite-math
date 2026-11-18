import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from markdown_to_handwrite.config import ReportConfig
from markdown_to_handwrite.markdown_parser import parse_markdown
from markdown_to_handwrite.renderer import ReportRenderer, _RichLine
from markdown_to_handwrite.ruled_lines import detect_ruled_lines, detect_tilted_lines, snap_gap


def ruled_page(width=600, height=800, first=100, pitch=40, count=15, colour=(150, 175, 210)) -> Image.Image:
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for index in range(count):
        y = first + index * pitch
        if y >= height:
            break
        draw.line((40, y, width - 40, y), fill=colour, width=2)
    return image


def slanted_page(width=1000, height=1414, first=300, pitch=55, slope=0.06, count=20) -> Image.Image:
    """实拍没摆正的照片：横线每向右 1px 下降 slope 像素。"""
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for index in range(count):
        y = first + index * pitch
        if y + slope * width >= height:
            break
        draw.line((0, y, width, y + slope * width), fill=(150, 175, 210), width=2)
    return image


def wavy_page(width=600, height=800, first=100, pitch=40, count=15) -> Image.Image:
    """模拟透视校正后的细线：每 10 像素左右上下抖动，单行覆盖率只有三成。"""
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for index in range(count):
        y = first + index * pitch
        points = [(x, y + (-2 if (x // 10) % 2 else 2)) for x in range(0, width + 1, 10)]
        draw.line(points, fill=(150, 175, 210), width=1)
    return image


def perspective_page(
    width=900,
    height=1400,
    first=380.0,
    pitch=30.0,
    growth=0.35,
    slope=-0.042,
    slope_growth=-9e-5,
) -> tuple[Image.Image, list[tuple[float, float]]]:
    """斜拍 + 透视的横线纸：越往下线距越大、坡度越陡，还叠着一层浅色透字。

    返回图片和每条线在中线处的高度、坡度。坡度按图片中线（height/2）为基准线性变化，
    和标定结果里的 tilt / tilt_gradient 是同一套定义。
    """
    image = Image.new("RGB", (width, height), (252, 252, 250))
    draw = ImageDraw.Draw(image)
    lines: list[tuple[float, float]] = []
    y = first
    step = pitch
    while y < height - 40:
        lines.append((y, slope + slope_growth * (y - height / 2.0)))
        y += step
        step += growth

    rng = np.random.default_rng(20261029)
    for index in range(len(lines) - 1):
        top, bottom = lines[index][0], lines[index + 1][0]
        for _ in range(10):
            x = float(rng.uniform(30, width - 80))
            length = float(rng.uniform(8, 22))
            yy = float(rng.uniform(top + 4, bottom - 4))
            draw.line((x, yy, x + length, yy + rng.uniform(-1, 1)), fill=(205, 205, 208), width=1)

    for center, line_slope in lines:
        # 线画 3px：实拍里线常有 2~4px 虚边，这种厚度才复现得出"坡度平台"那类选坡错误。
        draw.line(
            (0, center - line_slope * width / 2, width, center + line_slope * width / 2),
            fill=(150, 175, 210),
            width=3,
        )
    return image, lines


def click_points(
    lines: list[tuple[float, float]], width: int, height: int, index: int, jitter: float = 0.0
) -> list[tuple[float, float]]:
    """模拟用户在两条相邻横线各点左右两端；jitter 是没点准的手抖。"""
    points: list[tuple[float, float]] = []
    for k in (index, index + 1):
        y, line_slope = lines[k]
        for x in (width * 0.10, width * 0.90):
            points.append((x / width, (y + line_slope * (x - width / 2) + jitter) / height))
    return points


def strip_band_bottoms(strip) -> list[int]:
    """竖带里每一行墨迹的底边；只算横贯整条带的墨带，避免把半个字的笔画当成一行。"""
    rows = np.flatnonzero(strip.sum(axis=1) > 2)
    if not len(rows):
        return []
    spans = []
    start = previous = int(rows[0])
    for row in rows[1:]:
        row = int(row)
        if row > previous + 8:
            spans.append((start, previous))
            start = row
        previous = row
    spans.append((start, previous))
    return [end for start, end in spans if strip[start : end + 1].any(axis=0).mean() >= 0.5]


class DetectTests(unittest.TestCase):
    def test_finds_evenly_spaced_rules(self):
        result = detect_ruled_lines(ruled_page())
        self.assertTrue(result.usable)
        self.assertGreaterEqual(len(result.positions), 12)
        self.assertAlmostEqual(result.pitch, 40 / 800, places=3)
        self.assertGreater(result.confidence, 0.9)

    def test_first_line_position_matches(self):
        result = detect_ruled_lines(ruled_page(first=100))
        # 线宽 2px，检出的是线的中心（100.5），允许半个线宽。
        self.assertAlmostEqual(result.positions[0], 100.5 / 800, places=3)

    def test_blank_page_has_no_lines(self):
        result = detect_ruled_lines(Image.new("RGB", (600, 800), "white"))
        self.assertEqual(result.positions, [])
        self.assertFalse(result.usable)

    def test_dot_grid_is_not_mistaken_for_rules(self):
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        for y in range(60, 760, 30):
            for x in range(60, 560, 30):
                draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill=(120, 140, 170))
        result = detect_ruled_lines(image)
        self.assertFalse(result.usable)

    def test_uneven_spacing_lowers_confidence(self):
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        for y in (100, 130, 250, 265, 420, 425, 600):
            draw.line((40, y, 560, y), fill=(150, 175, 210), width=2)
        result = detect_ruled_lines(image)
        self.assertLess(result.confidence, 0.75)
        self.assertFalse(result.usable)

    def test_thick_dark_band_is_ignored(self):
        """页眉色块是成片暗区，不能当成横线。"""
        image = ruled_page()
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 600, 40), fill=(90, 100, 120))
        result = detect_ruled_lines(image)
        self.assertTrue(result.usable)
        self.assertGreater(result.positions[0], 40 / 800)

    def test_three_even_bands_are_not_enough_to_snap(self):
        """线圈装订/色块常留下三条等距暗带，数量太少不能拿来吸附。"""
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        for y in (400, 422, 444):
            draw.line((0, y, 600, y), fill=(150, 175, 210), width=2)
        result = detect_ruled_lines(image)
        self.assertEqual(len(result.positions), 3)
        self.assertFalse(result.usable)

    def test_implausible_pitch_is_not_usable(self):
        """线距明显不是纸张横线（这里是 2 mm）时不能吸附。"""
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        for y in (400, 408, 416, 424, 432, 440):
            draw.line((0, y, 600, y), fill=(150, 175, 210), width=2)
        result = detect_ruled_lines(image)
        self.assertEqual(len(result.positions), 6)
        self.assertFalse(result.usable)

    def test_wavy_resampled_rules_are_still_detected(self):
        """摆正后的细线会抖动，单行覆盖率不够，但线本身还在。"""
        image = wavy_page()
        gray = np.asarray(image.convert("L"), dtype=np.float32)
        paper = float(np.percentile(gray, 85))
        raw_span = (gray < paper - 10).sum(axis=1) / gray.shape[1]
        self.assertLess(float(raw_span.max()), 0.5)

        result = detect_ruled_lines(image)
        self.assertTrue(result.usable)
        self.assertGreaterEqual(len(result.positions), 12)
        self.assertAlmostEqual(result.pitch, 40 / 800, places=3)


class PerspectiveDetectTests(unittest.TestCase):
    def setUp(self):
        self.image, self.lines = perspective_page()
        self.width, self.height = self.image.size

    def _detect(self, index=None, jitter=1.5):
        index = len(self.lines) // 2 if index is None else index
        points = click_points(self.lines, self.width, self.height, index, jitter)
        return detect_tilted_lines(self.image, points)

    def test_fixture_is_visibly_not_uniform(self):
        """底座必须真的不平均：等距外推和整页一个坡度在这张图上都差得很远。"""
        actual = np.array([center for center, _ in self.lines])
        uniform = np.linspace(actual[0], actual[-1], len(actual))
        self.assertGreater(float(np.abs(actual - uniform).max()), 8)
        clicked = self.lines[len(self.lines) // 2][1]
        self.assertGreater(abs(self.lines[-1][1] - clicked) * self.width / 2, 10)

    def test_every_line_is_found_at_its_real_height(self):
        result = self._detect()
        self.assertTrue(result.usable)
        self.assertGreaterEqual(len(result.positions), len(self.lines) - 1)
        detected = np.array(result.positions) * self.height
        for center, _ in self.lines:
            self.assertLessEqual(
                float(np.abs(detected - center).min()), 2.5, f"中线 {center}px 的横线没认准：{detected}"
            )
        gaps = np.diff(detected)
        self.assertGreater(float(gaps.max() - gaps.min()), 4, "逐条线距应随透视变大，而不是被抹平成等距")
        self.assertAlmostEqual(result.pitch, float(np.median(gaps)) / self.height, places=5)

    def test_tilt_and_its_gradient_match_the_photo(self):
        result = self._detect()
        # 选坡若拿"坡度平台"上偏下的一格，3px 线会让整页坡度偏到 -0.056。
        self.assertAlmostEqual(result.tilt, -0.042, delta=0.002)
        self.assertAlmostEqual(result.tilt_gradient, -9e-5, delta=2.5e-5)

    def test_clicking_different_rows_gives_the_same_page(self):
        """用户点哪一行都行：整页识别结果不能因为锚点换了就变。"""
        middle = self._detect(len(self.lines) // 2 - 3)
        other = self._detect(len(self.lines) // 2 + 4)
        self.assertTrue(middle.usable and other.usable)
        self.assertAlmostEqual(middle.tilt, other.tilt, delta=0.004)
        self.assertAlmostEqual(middle.tilt_gradient, other.tilt_gradient, delta=2.5e-5)
        for value in other.positions:
            self.assertLessEqual(float(np.abs(np.array(middle.positions) - value).min()), 2.5 / self.height)

    def test_points_that_miss_the_lines_are_rejected(self):
        """点在没有横线的空白处，就认不出线，不能拿噪声硬凑一份标定。"""
        points = [(0.1, 0.05), (0.9, 0.05), (0.1, 0.07), (0.9, 0.07)]
        self.assertFalse(detect_tilted_lines(self.image, points).usable)

    def test_blank_page_has_nothing_to_calibrate(self):
        result = detect_tilted_lines(Image.new("RGB", (900, 1400), "white"), [(0.1, 0.3), (0.9, 0.3), (0.1, 0.4), (0.9, 0.4)])
        self.assertEqual(result.positions, [])
        self.assertFalse(result.usable)


class SnapTests(unittest.TestCase):
    def test_snap_gap_rounds_to_whole_rows(self):
        self.assertEqual(snap_gap(40, 13), 40)
        self.assertEqual(snap_gap(40, 70), 80)
        self.assertEqual(snap_gap(40, 0), 0)

    def test_grid_pitch_and_lines_are_built(self):
        config = ReportConfig()
        config.page.dpi = 150
        config.background.ruled_lines = [0.1 + index * 0.02 for index in range(12)]
        config.background.snap_ruled_lines = True
        renderer = ReportRenderer(config)
        self.assertGreater(renderer.grid_pitch, 0)
        self.assertEqual(len(renderer.grid_lines), 12)

    def test_disabled_snap_leaves_layout_untouched(self):
        config = ReportConfig()
        config.page.dpi = 150
        config.background.ruled_lines = [0.1 + index * 0.02 for index in range(12)]
        config.background.snap_ruled_lines = False
        renderer = ReportRenderer(config)
        self.assertEqual(renderer.grid_pitch, 0)
        self.assertEqual(renderer._snap_grid(37), 37)

    def test_heights_are_quantised(self):
        config = ReportConfig()
        config.page.dpi = 150
        config.background.ruled_lines = [0.1 + index * 0.02 for index in range(12)]
        config.background.snap_ruled_lines = True
        renderer = ReportRenderer(config)
        pitch = renderer.grid_pitch
        for value in (5, 37, 80, 200):
            self.assertEqual(renderer._snap_grid(value) % pitch, 0)
        self.assertEqual(renderer._snap_grid(0), 0)

    def test_first_baseline_lands_on_a_rule(self):
        config = ReportConfig()
        config.page.dpi = 150
        config.background.ruled_lines = [0.1 + index * 0.02 for index in range(12)]
        config.background.snap_ruled_lines = True
        renderer = ReportRenderer(config)
        start = renderer._align_to_grid(renderer.area_top)
        ascent = round(
            __import__("markdown_to_handwrite.config", fromlist=["pt_to_px"]).pt_to_px(
                config.handwriting.body_font_pt, config.page.dpi
            )
            * 0.76
        )
        baseline = start + ascent
        self.assertIn(baseline, renderer.grid_lines)


class TiltTests(unittest.TestCase):
    def test_tilt_is_ignored_without_rules(self):
        """没有横线可吸附时，倾斜量没有意义，不能把正文斜着写。"""
        config = ReportConfig()
        config.background.rule_tilt = 0.2
        renderer = ReportRenderer(config)
        self.assertEqual(renderer.rule_tilt, 0.0)
        self.assertEqual(renderer._tilt_offset(renderer.page_w), 0.0)

    def test_tilted_line_is_not_clipped_at_its_left_edge(self):
        """斜切只在竖向上挪动整行：行首不能被挤出画布，否则一段话每行开头都缺字。"""
        config = ReportConfig()
        config.page.dpi = 150
        config.background.ruled_lines = [0.2 + index * 0.02 for index in range(20)]
        config.background.rule_tilt = 0.08
        config.background.snap_ruled_lines = True
        renderer = ReportRenderer(config)

        # 页面底色本身不透明，量化不了墨迹；换成透明画布，只看落笔的墨。
        renderer.page = Image.new("RGBA", renderer.page.size, (0, 0, 0, 0))
        bar = Image.new("RGBA", (400, 40), (0, 0, 0, 255))
        line = _RichLine([(bar, 32)], 400, 40, 32)
        renderer._paste_tilted_line(line, 0, 500, 400, False)

        ink = np.asarray(renderer.page)[..., 3] > 128
        left = int(ink[:, 0].sum())
        middle = int(ink[:, 200].sum())
        # 同一条 40px 高的墨条，行首应和行中一样高；被挤出画布时只剩一半多。
        self.assertGreaterEqual(middle, 36, f"行中的墨条不该被裁，实际 {middle}px")
        self.assertGreaterEqual(left, middle - 3, f"行首被裁：行首 {left}px / 行中 {middle}px")

    def test_slanted_rules_are_followed_by_text(self):
        """横线在照片里是斜的：每行文字必须跟着斜率走，而不是水平铺在中线。"""
        width, height, first, pitch, slope = 1000, 1414, 300, 55, 0.06
        image = slanted_page(width, height, first, pitch, slope)
        path = Path(tempfile.mkdtemp()) / "slanted-rules.png"
        image.save(path)

        config = ReportConfig()
        config.page.dpi = 150
        config.background.style = "image"
        config.background.image = str(path)
        # 标定记的是页面中线处的线高：图片中线 x=500。
        config.background.ruled_lines = [
            (first + index * pitch + slope * width / 2) / height
            for index in range(int((height - first) / pitch))
            if first + index * pitch + slope * width / 2 < height
        ]
        config.background.rule_tilt = slope
        config.background.snap_ruled_lines = True
        config.handwriting.body_font_pt = 14
        # 涂写会画在基线上方，被当成一条“离线”的墨带，这里只量正文。
        config.handwriting.scribble_probability = 0
        renderer = ReportRenderer(config)
        self.assertGreater(renderer.grid_pitch, 0)

        sentence = (
            "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏"
            "闰余成岁律吕调阳云腾致雨露结为霜金生丽水玉出昆冈"
        )
        # 一整段长文：除末行外每行都铺满整列，量到的是同一批行在页面各处的落笔高度。
        pages = renderer.render(parse_markdown(sentence * 12))

        array = np.asarray(pages[0].convert("RGB"), dtype=np.int16)
        ink = (array[..., 0] < 110) & (array[..., 1] < 110) & (array[..., 2] < 150)
        columns = 12
        distances = []
        first_bands = []
        for index in range(columns):
            # 只在正文列里量：页边距里没有字，量到空带没有意义。
            left = round(renderer.margin_left + renderer.content_w * index / columns)
            right = round(renderer.margin_left + renderer.content_w * (index + 1) / columns)
            bands = strip_band_bottoms(ink[:, left:right])
            self.assertTrue(bands, f"第 {index} 条竖带里没有成行的墨迹")
            x = (left + right) / 2
            # 首行首字缩进，最左/最右那条竖带里首行盖不满，只取中间几条量首行斜率。
            if 1 <= index <= columns - 2:
                first_bands.append((x, bands[0]))
            for bottom in bands:
                # 每条横线在该 x 处的真实高度。
                rule = min(
                    renderer.grid_lines,
                    key=lambda line: abs(line + slope * (x - renderer.page_w / 2) - bottom),
                )
                distances.append(abs(bottom - (rule + slope * (x - renderer.page_w / 2))))

        self.assertGreaterEqual(len(distances), 60, "应检测到足够多的墨带")
        worst = max(distances)
        # 不跟随倾斜时，同一行在左右两端会差 slope × 列宽 ≈ 60px；跟随时应压在线上。
        self.assertLessEqual(worst, 7, f"文字没跟着倾斜的横线走，最大偏差 {worst}px")
        # 首行在页面各处的落笔高度必须连成同一条斜线：拟合斜率和标定的坡度一致。
        xs = np.array([x for x, _ in first_bands], dtype=np.float64)
        ys = np.array([y for _, y in first_bands], dtype=np.float64)
        fitted = float(np.polyfit(xs, ys, 1)[0])
        self.assertAlmostEqual(
            fitted, slope, delta=0.02, msg=f"首行没跟着横线斜，拟合坡度 {fitted:.3f}，应为 {slope}"
        )


class PerspectiveRenderTests(unittest.TestCase):
    """透视照片里的每一条横线坡度都不一样：正文要逐行跟着各自的坡度落笔。"""

    @staticmethod
    def _render(with_gradient: bool = True, columns: int = 12):
        image, lines = perspective_page()
        width, height = image.size
        path = Path(tempfile.mkdtemp()) / "perspective-rules.png"
        image.save(path)

        middle = len(lines) // 2
        detected = detect_tilted_lines(image, click_points(lines, width, height, middle, jitter=1.5))
        assert detected.usable, "底座照片先要能被整页识别"

        config = ReportConfig()
        config.page.dpi = 150
        config.background.style = "image"
        config.background.image = str(path)
        config.background.ruled_lines = detected.positions
        config.background.rule_tilt = detected.tilt
        config.background.rule_tilt_gradient = detected.tilt_gradient if with_gradient else 0.0
        config.background.snap_ruled_lines = True
        config.handwriting.body_font_pt = 14
        config.handwriting.scribble_probability = 0
        renderer = ReportRenderer(config)

        sentence = (
            "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏"
            "闰余成岁律吕调阳云腾致雨露结为霜金生丽水玉出昆冈"
        )
        page = renderer.render(parse_markdown(sentence * 12))[0]

        # 真实线位来自底座照片本身：只做 cover 铺纸的缩放和居中裁切，不读渲染器的中间量。
        scale = max(renderer.page_w / width, renderer.page_h / height)
        offset = (height * scale - renderer.page_h) / 2.0
        truth = [(center * scale - offset, line_slope) for center, line_slope in lines]

        array = np.asarray(page.convert("RGB"), dtype=np.int16)
        ink = (array[..., 0] < 110) & (array[..., 1] < 110) & (array[..., 2] < 150)
        distances = []
        for index in range(columns):
            left = round(renderer.margin_left + renderer.content_w * index / columns)
            right = round(renderer.margin_left + renderer.content_w * (index + 1) / columns)
            x = (left + right) / 2
            for bottom in strip_band_bottoms(ink[:, left:right]):
                # 这条字压在照片里的哪条线上？按该线的真实坡度算它在这个 x 处的高度。
                distances.append(
                    min(abs(bottom - (center + line_slope * (x - renderer.page_w / 2))) for center, line_slope in truth)
                )
        return distances

    def test_text_follows_each_lines_own_slope(self):
        distances = self._render(with_gradient=True)
        self.assertGreaterEqual(len(distances), 40, "应检测到足够多的墨带")
        worst = max(distances)
        self.assertLessEqual(worst, 8, f"文字没落在照片里的横线上，最大偏差 {worst}px")

    def test_ignoring_the_gradient_drifts_off_the_lines(self):
        """反证：整页只用一个坡度时，离页面中线越远的行偏得越多，必须逐行给坡度。"""
        distances = self._render(with_gradient=False)
        self.assertGreaterEqual(len(distances), 40, "应检测到足够多的墨带")
        self.assertGreater(max(distances), 10, f"不用坡度变化也该明显偏线，最大偏差只有 {max(distances)}px")

    @staticmethod
    def _strip_band_bottoms(strip) -> list[int]:
        """竖带里每一行墨迹的底边；只算横贯整条带的墨带，避免把半个字的笔画当成一行。"""
        rows = np.flatnonzero(strip.sum(axis=1) > 2)
        if not len(rows):
            return []
        spans = []
        start = previous = int(rows[0])
        for row in rows[1:]:
            row = int(row)
            if row > previous + 8:
                spans.append((start, previous))
                start = row
            previous = row
        spans.append((start, previous))
        return [end for start, end in spans if strip[start : end + 1].any(axis=0).mean() >= 0.5]


class RenderIntegrationTests(unittest.TestCase):
    def _ink_bands(self, page, threshold=120):
        """返回页面里每一行文字的底边（约等于基线）和墨水行数。"""
        array = np.asarray(page.convert("RGB"), dtype=np.int16)
        ink = (array[..., 0] < threshold) & (array[..., 1] < threshold) & (array[..., 2] < threshold)
        rows = ink.sum(axis=1)
        index = np.flatnonzero(rows > 8)
        if not len(index):
            return []
        bands = []
        start = previous = index[0]
        for row in index[1:]:
            if row > previous + 8:
                bands.append((start, previous))
                start = row
            previous = row
        bands.append((start, previous))
        return [int(end) for _, end in bands]

    def test_every_rendered_line_sits_on_a_rule(self):
        """标题大小不同、行高不是线距整数倍时，正文也必须压线，不能一路漂移。"""
        config = ReportConfig()
        config.page.dpi = 150
        config.handwriting.body_font_pt = 14
        config.background.ruled_lines = [0.12 + index * 0.025 for index in range(30)]
        config.background.snap_ruled_lines = True
        renderer = ReportRenderer(config)
        markdown = "# 一、实验目的\n\n" + "\n\n".join(
            f"第{index}段用于检验基线是否始终落在横线上。" for index in range(8)
        )
        pages = renderer.render(parse_markdown(markdown))
        bands = self._ink_bands(pages[0])
        self.assertGreaterEqual(len(bands), 5, f"应有多行文字，实际墨带 {bands}")
        for bottom in bands:
            distance = min(abs(bottom - line) for line in renderer.grid_lines)
            self.assertLessEqual(distance, 4, f"基线 {bottom} 离线太远，横线 {renderer.grid_lines}")

    def test_image_background_rules_are_mapped_through_cover_crop(self):
        """背景按 cover 铺满页面时会裁掉上下；线上标定必须按同一变换换算，否则整排偏移。"""
        image = ruled_page(width=500, height=900, first=200, pitch=40, count=9)
        path = Path(tempfile.mkdtemp()) / "portrait-rules.png"
        image.save(path)
        config = ReportConfig()
        config.page.dpi = 150
        config.background.style = "image"
        config.background.image = str(path)
        positions = [round(value / 900, 5) for value in (200, 240, 280, 320, 360, 400, 440, 480, 520)]
        config.background.ruled_lines = positions
        config.background.snap_ruled_lines = True
        config.handwriting.body_font_pt = 30
        renderer = ReportRenderer(config)

        # 竖图比页面窄，cover 会按宽度放大并裁掉上下，不能直接按比例乘页面高度。
        scale = renderer.page_w / 500
        offset = (900 * scale - renderer.page_h) / 2
        expected = sorted({round((value * 900 * scale - offset) / renderer.page_h * renderer.page_h) for value in positions})
        self.assertAlmostEqual(renderer.grid_pitch, round(40 * scale), delta=2)
        self.assertEqual(renderer.grid_lines, sorted(expected))

        pages = renderer.render(parse_markdown("第一行文字。\n\n第二行文字。\n\n第三行文字。"))
        bands = self._ink_bands(pages[0], threshold=110)
        self.assertGreaterEqual(len(bands), 3, f"应渲染出多行文字，实际 {bands}")
        for bottom in bands:
            distance = min(abs(bottom - line) for line in renderer.grid_lines)
            self.assertLessEqual(distance, 5, f"基线 {bottom} 没落在横线 {renderer.grid_lines} 上")


if __name__ == "__main__":
    unittest.main()
