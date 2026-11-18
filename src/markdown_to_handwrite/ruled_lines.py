"""检测横线纸背景里的横线位置，供正文基线吸附。

做法是行投影：一条横线会横跨整页，所以它所在的那几行几乎全是"暗像素"，
而文字行只占两三成。按这个比例卡阈值，就能把线和内容分开，不需要 OpenCV。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image

# 一条横线要横跨至少这个比例的页面宽度。
MIN_SPAN_RATIO = 0.5
# 比纸面暗多少才算墨迹/线（灰度级）。
MIN_CONTRAST = 10.0
# 线只有几像素厚；更厚的成片暗区是页眉色块或插图，不能当线。
MAX_THICKNESS_PX = 8
# 少于这么多条线就不值得吸附（一条线定不了间距）。
MIN_LINES = 3
# 吸附至少要这么多条：三四条往往只是装订孔、页眉色块之类的巧合等距暗带。
MIN_USABLE_LINES = 5
# 可信的线距范围，按整页高度归一化；笔记本横线一般在 5–15 mm，超出就是误检。
MIN_PITCH = 0.012
MAX_PITCH = 0.06
# 透视校正后的细线会沿竖直方向抖动 1–2 像素，单行里的暗像素可能只剩三成。
# 统计行覆盖率前先在 ±POOL_RADIUS 行内取并集，否则校正过的图会一条线都检不出。
POOL_RADIUS = 2


@dataclass
class RuledLines:
    positions: list[float]  # 归一化 y（0-1，相对图片高度），自上而下
    pitch: float  # 相邻线的间距，归一化（0-1）
    confidence: float  # 间距一致性：1 表示完全等距
    thickness: int  # 线宽，像素

    @property
    def usable(self) -> bool:
        return (
            len(self.positions) >= MIN_USABLE_LINES
            and self.confidence >= 0.75
            and MIN_PITCH <= self.pitch <= MAX_PITCH
        )


def _runs(flags: np.ndarray) -> list[tuple[int, int]]:
    if flags.size == 0:
        return []
    padded = np.concatenate(([False], flags.astype(bool), [False]))
    edges = np.flatnonzero(np.diff(padded.astype(np.int8)))
    return [(int(edges[i]), int(edges[i + 1])) for i in range(0, len(edges), 2)]


def _vertical_union(mask: np.ndarray, radius: int) -> np.ndarray:
    if radius <= 0:
        return mask
    padded = np.pad(mask, ((radius, radius), (0, 0)), constant_values=False)
    pooled = np.zeros_like(mask)
    for offset in range(radius * 2 + 1):
        pooled |= padded[offset : offset + mask.shape[0]]
    return pooled


def detect_ruled_lines(image: Image.Image) -> RuledLines:
    """Find horizontal rules in a paper background."""
    gray = np.asarray(image.convert("L"), dtype=np.float32)
    height, width = gray.shape
    if height < 40 or width < 40:
        return RuledLines([], 0.0, 0.0, 0)

    # 纸面亮度取 85 分位：大部分像素是纸，线只占很小一部分。
    paper = float(np.percentile(gray, 85))
    dark = gray < paper - MIN_CONTRAST
    reach = _vertical_union(dark, POOL_RADIUS)
    span = reach.sum(axis=1) / float(width)
    groups = _runs(span >= MIN_SPAN_RATIO)

    positions: list[float] = []
    thicknesses: list[int] = []
    for start, end in groups:
        # 并集把线的上下各撑开 POOL_RADIUS 行，厚度要还原。
        thickness = end - start - POOL_RADIUS * 2
        if thickness > MAX_THICKNESS_PX:
            continue
        positions.append((start + end - 1) / 2.0 / height)
        thicknesses.append(max(1, thickness))

    if len(positions) < MIN_LINES:
        return RuledLines(positions, 0.0, 0.0, max(thicknesses, default=0))

    gaps = np.diff(positions)
    # 用中位数当间距：偶发的空行（分段稿纸）不该带偏整体。
    pitch = float(np.median(gaps))
    # 一致性：落在中位数 ±25% 内的间距占比。
    close = np.abs(gaps - pitch) <= pitch * 0.25
    confidence = float(close.mean())
    return RuledLines(positions, pitch, confidence, int(np.median(thicknesses)))


@dataclass
class TiltedLines:
    """斜拍照片里逐条识别出来的横线。

    拍照有透视：同一页上排线距 30px、下排 40px，坡度也是上缓下陡，等距外推会越写越偏。
    这里存下**每一条线**的真实高度和整页的坡度变化，渲染器按实际线位逐行吸附、逐行倾斜。
    """

    tilt: float  # 图片中线高度处的坡度 dy/dx：每向右 1px，线下降多少 px
    positions: list[float]  # 归一化 y（0-1，相对图片高度），取图片水平中线处的高度
    pitch: float  # 相邻线距的中位数，归一化
    confidence: float  # 间距一致性：透视的渐变量不影响它是"真线"
    tilt_gradient: float = 0.0  # 每往下 1px，坡度变化多少（透视：越往下越陡）

    @property
    def usable(self) -> bool:
        return (
            len(self.positions) >= MIN_USABLE_LINES
            and self.confidence >= 0.6
            and MIN_PITCH <= self.pitch <= MAX_PITCH
        )


# 斜线识别只在用户点出来的那段宽度里做：点常落在纸边或线圈上，竖带收进来只用纸面中间。
SLICE_MARGIN = 0.15
# 扫描坡度时一条竖带最多取这么多列：够定出坡度，又不至于把一张大图拖成几十秒。
SCAN_COLUMNS = 400
# 亮度掉到锚点纸面亮度的这个比例以下，说明已经离开纸（桌面、屏幕、书页边缘）。
PAPER_LEVEL_RATIO = 0.7
# 和锚点处的纸面颜色差多少以内才算同一张纸：屏幕、键盘、桌面常和纸一样亮，只能靠颜色分。
PAPER_COLOR_TOLERANCE = 45.0
# 透视带来的坡度变化上限：每向下 1px 坡度最多变这么多。
MAX_TILT_GRADIENT = 0.0005
# 判定是否真压线时容许的间距起伏（间距随透视渐变，所以比平面检测松）。
TILT_PITCH_TOLERANCE = 0.35
# 逐行扫描坡度的范围与步长：翻拍页面里上下两端坡度能差 0.08，必须让每条线选自己的坡度。
SLOPE_SCAN_RANGE = 0.12
SLOPE_SCAN_STEP = 0.004


def _slope_scan(
    gray: np.ndarray, x0: int, x1: int, slopes: list[float], stride: int = 1
) -> tuple[np.ndarray, np.ndarray] | None:
    """在每个候选坡度下把竖带拉平，逐行给出最锐的坡度和它的锐度。

    ``strength`` 用列中位数：透字只占少数列，中位数看不见它们，峰的边缘更干净。
    但中位数在真坡度附近会饱和成一大片平台，argmax 会停在平台最靠下的一格，
    所以坡度另用峰位 ±2 行的 |dy| 之和来挑：线压准了才在窄窗里聚成尖峰。
    """
    height, width = gray.shape
    xs = np.arange(max(1, x0), min(width - 1, x1), stride, dtype=np.float32)
    if xs.size < 8:
        return None
    rows = np.arange(height, dtype=np.float32)[:, None]
    columns = xs[None, :].astype(np.int32)
    shift = (xs - width / 2.0)[None, :]
    sharp = []
    energy = []
    for slope in slopes:
        yy = rows + slope * shift
        np.clip(yy, 0, height - 1, out=yy)
        band = gray[yy.astype(np.int32), columns]
        edges = np.abs(np.gradient(band, axis=0))
        sharp.append(np.convolve(np.median(edges, axis=1), np.ones(3) / 3, mode="same"))
        # 截幅再求和：一两列浓墨压不倒横贯整带的线，求和又不会像中位数那样饱和。
        energy.append(np.convolve(np.minimum(edges, 60.0).sum(axis=1), np.ones(5), mode="same"))
    return (
        np.stack(sharp).max(axis=0),
        np.asarray(slopes, dtype=np.float64)[np.stack(energy).argmax(axis=0)],
    )


def _band_peaks(signal: np.ndarray) -> list[int]:
    """竖带逐行锐度里每条线的边缘峰。

    信号是逐行 |dy| 的列中位数：透字、涂改只占一部分列，中位数看不见它们，
    细线却横贯整条带，中位数会把它的边缘留下来。
    """
    threshold = max(0.6, float(np.percentile(signal, 60)))
    peaks: list[int] = []
    for y in range(3, len(signal) - 3):
        if signal[y] >= signal[y - 3 : y + 4].max() and signal[y] > threshold:
            # 一条线有上下两个边缘，间距几像素的要并成一个。
            if peaks and y - peaks[-1] <= 6:
                if signal[y] > signal[peaks[-1]]:
                    peaks[-1] = y
            else:
                peaks.append(y)
    if peaks:
        strength = np.array([signal[y] for y in peaks])
        # 只有微弱起伏的峰多半是纹理，留下成不了链的噪声没意义。
        floor = max(0.8, 0.3 * float(np.median(strength)))
        peaks = [y for y, value in zip(peaks, strength) if value >= floor]
    return peaks


def _walk(peaks: list[int], anchors: list[int], pitch: float, height: int) -> dict[int, float]:
    """从点中的两条线出发，按线距一上一下把整页的线串起来。

    返回 ``k -> 切变后的行号``：点中的两条线是 0 和 1，往上递减、往下递增。
    """
    chain = {0: float(anchors[0]), 1: float(anchors[1])}
    step = float(pitch)
    grid = np.asarray(peaks, dtype=np.float32)

    def nearest(y: float, expect: float) -> float | None:
        window = grid[np.abs(grid - expect) <= 0.42 * step]
        if not window.size:
            return None
        return float(window[np.argmin(np.abs(window - expect))])

    for direction, index, start in ((1, 1, chain[1]), (-1, 0, chain[0])):
        y, k = start, index
        while 4 < y < height - 4:
            found = nearest(y, y + direction * step)
            skipped = False
            if found is None:
                # 有的线太浅漏检了：允许隔一条再对上，序号照样占位。
                found = nearest(y, y + direction * 2 * step)
                skipped = found is not None
            if found is None:
                break
            k += direction * (2 if skipped else 1)
            chain[k] = found
            if not skipped:
                step = 0.6 * step + 0.4 * abs(found - y)
            y = found
    return chain


def detect_tilted_lines(image: Image.Image, points: list[tuple[float, float]]) -> TiltedLines:
    """按用户点的两条相邻横线，识别整页每一条线的真实位置、坡度和坡度变化。

    ``points`` 是四个归一化图片坐标：前两个点一条线，后两个点另一条线，每条线各点左右两端。
    """
    gray = np.asarray(image.convert("L"), dtype=np.float32)
    height, width = gray.shape
    if len(points) != 4 or height < 60 or width < 60:
        return TiltedLines(0.0, [], 0.0, 0.0)

    pixels = [(float(x) * width, float(y) * height) for x, y in points]
    slopes = []
    for start, end in ((0, 1), (2, 3)):
        (x0, y0), (x1, y1) = pixels[start], pixels[end]
        if abs(x1 - x0) < width * 0.1:
            return TiltedLines(0.0, [], 0.0, 0.0)
        slopes.append((y1 - y0) / (x1 - x0))
    tilt = (slopes[0] + slopes[1]) / 2.0
    if abs(tilt) > 0.4 or abs(slopes[0] - slopes[1]) > 0.06:
        return TiltedLines(0.0, [], 0.0, 0.0)

    center_x = width / 2.0
    first, second = pixels[0], pixels[2]
    # 每条线用它自己被点出来的坡度换算到中线高度：透视里上下相邻两条的坡度并不相同。
    clicked = sorted(
        (
            first[1] + slopes[0] * (center_x - first[0]),
            second[1] + slopes[1] * (center_x - second[0]),
        )
    )
    pitch = clicked[1] - clicked[0]
    if pitch < 6:
        return TiltedLines(0.0, [], 0.0, 0.0)

    left = min(x for x, _ in pixels)
    right = max(x for x, _ in pixels)
    margin = (right - left) * SLICE_MARGIN
    inner_left, inner_right = left + margin, right - margin
    if inner_right - inner_left < 60:
        return TiltedLines(0.0, [], 0.0, 0.0)

    slopes_scan = np.arange(-SLOPE_SCAN_RANGE, SLOPE_SCAN_RANGE + 1e-9, SLOPE_SCAN_STEP) + tilt
    stride = max(1, int(round((inner_right - inner_left) / SCAN_COLUMNS)))
    scan = _slope_scan(gray, int(inner_left), int(inner_right), [float(v) for v in slopes_scan], stride)
    if scan is None:
        return TiltedLines(0.0, [], 0.0, 0.0)
    strength, row_slopes = scan
    peaks = _band_peaks(strength)
    if len(peaks) < MIN_USABLE_LINES:
        return TiltedLines(0.0, [], 0.0, 0.0)

    anchors: list[int] = []
    for value in clicked:
        nearest = min(peaks, key=lambda p: abs(p - value))
        if abs(nearest - value) > 0.5 * pitch:
            return TiltedLines(0.0, [], 0.0, 0.0)
        anchors.append(nearest)
    if anchors[1] - anchors[0] < 0.6 * pitch:
        return TiltedLines(0.0, [], 0.0, 0.0)

    # 整条竖带一起扫描时，只有坡度对上了线才会聚成尖峰；峰所在的行就是这条线在中线处的高度。
    found: dict[int, tuple[float, float]] = {
        k: (float(row), float(row_slopes[int(row)]))
        for k, row in _walk(peaks, anchors, pitch, height).items()
    }

    # 隔一条漏检时补在中间：渲染器按线位吸附，中间空一格正文就会跳行。
    for k in sorted(found):
        if k + 1 not in found and k + 2 in found:
            head, tail = found[k], found[k + 2]
            found[k + 1] = ((head[0] + tail[0]) / 2.0, (head[1] + tail[1]) / 2.0)

    sample_x = (inner_left + inner_right) / 2.0
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32)

    def paper_patch(y: float, slope: float) -> tuple[float, np.ndarray] | None:
        row = int(round(y + slope * (sample_x - center_x)))
        patch = rgb[max(0, row - 10) : row + 11, max(0, int(sample_x) - 30) : int(sample_x) + 30]
        if not patch.size:
            return None
        return float(np.median(patch[..., :3])), np.median(patch.reshape(-1, 3), axis=0)

    patches = {k: paper_patch(y, slope) for k, (y, slope) in found.items()}
    reference_level = max(patches.get(0, (0.0, None))[0], patches.get(1, (0.0, None))[0])
    reference_color = patches.get(0, (0.0, None))[1]
    if reference_color is None:
        reference_color = patches.get(1, (0.0, None))[1]
    ordered = sorted(found.items())
    if reference_level > 0 and reference_color is not None:
        ordered = [
            (k, value)
            for k, value in ordered
            if patches[k] is not None
            and patches[k][0] >= reference_level * PAPER_LEVEL_RATIO
            and float(np.linalg.norm(patches[k][1] - reference_color)) <= PAPER_COLOR_TOLERANCE
        ]
    if len(ordered) < MIN_USABLE_LINES:
        return TiltedLines(0.0, [], 0.0, 0.0)

    ys = np.array([value[0] for _, value in ordered], dtype=np.float64)
    line_slopes = np.array([value[1] for _, value in ordered], dtype=np.float64)
    gaps = np.diff(ys)
    pitch = float(np.median(gaps))
    if not MIN_PITCH * height <= pitch <= MAX_PITCH * height:
        return TiltedLines(0.0, [], 0.0, 0.0)
    confidence = float((np.abs(gaps - pitch) <= pitch * TILT_PITCH_TOLERANCE).mean())

    gradient = 0.0
    tilt_center = float(np.median(line_slopes))
    if len(ordered) >= 4 and float(np.ptp(line_slopes)) > 1e-4:
        # 透视里坡度随高度线性变化；先拟合一遍，剔掉量歪的单条线再拟合。
        fit, offset = np.polyfit(ys, line_slopes, 1)
        residual = line_slopes - (fit * ys + offset)
        keep = np.abs(residual) <= max(0.02, 3.0 * float(np.median(np.abs(residual))))
        if keep.sum() >= 4:
            fit, _ = np.polyfit(ys[keep], line_slopes[keep], 1)
        gradient = float(np.clip(fit, -MAX_TILT_GRADIENT, MAX_TILT_GRADIENT))
        # 截断后的坡度重算偏移，保证中线处的坡度与 positions 用的是同一条拟合线。
        tilt_center = gradient * height / 2.0 + float(np.median(line_slopes - gradient * ys))

    positions = [round(float(y) / height, 5) for y in ys]
    return TiltedLines(tilt_center, positions, pitch / height, confidence, gradient)


def snap_gap(pitch_px: int, target_px: int) -> int:
    """把任意间距量化成横线间距的整数倍，至少一格；0 保持 0（表示不留空）。"""
    if pitch_px <= 0 or target_px <= 0:
        return target_px
    return max(pitch_px, int(round(target_px / pitch_px)) * pitch_px)
