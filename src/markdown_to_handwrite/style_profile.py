"""轻量字迹分析：从一张手写样本里量出可复现的风格参数。

这里不做模型推理，也不建个人字形库。思路是把手写照片当成一张二值图像，
用投影切分出行和字，再量出六个左右的无量纲比例（笔宽、倾斜、行距、字宽、
墨色、规整度），直接映射到现有的渲染参数上。整条链路只用 numpy + Pillow，
一张 A4 样本约 0.3 秒。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# 分析前把样本缩到这个长边，兼顾速度与测量精度。
ANALYSIS_MAX_EDGE = 1400
# 墨迹判定的绝对下限：低于它的一律当纸面噪声，避免白纸上量出满屏"笔画"。
MIN_INK_CONTRAST = 8.0
# 宽松掩膜（量笔宽用）的 Otsu 系数：留一点抗锯齿过渡带。
INK_THRESHOLD_SCALE = 0.55
# 严格掩膜（切分行字、测倾斜用）的系数：把浅色印刷横线、纸纹和 JPEG 噪声挡在外面。
CORE_THRESHOLD_SCALE = 1.0
CORE_INK_CONTRAST = 20.0
# 少于这些内容就认为样本不足，宁可让用户重拍，也不要给出瞎猜的参数。
MIN_ROWS = 2
MIN_CHARACTERS = 12

# 标定系数：用本项目渲染引擎以已知参数生成的样本反推得到，
# 改动渲染引擎的度量方式后需要重新标定（见 tests/test_style_profile.py）。
STROKE_RATIO_REFERENCE = 0.0727  # sdt_stroke_width=16 时，笔宽 / 字高 的实测值
LINE_SPACING_COEFFICIENT = 0.86  # 行距 / 字高 → line_spacing 的换算比例
CHAR_WIDTH_REFERENCE = 0.93  # 常规字宽 / 字高 的参考值，用于判断字形宽窄
# 2×面积/周长 会略微高估笔宽（拐角和端点让周长偏短），这里做一次线性回归校正。
PERIMETER_WIDTH_SCALE = 0.837
PERIMETER_WIDTH_OFFSET = -0.72
# 汉字竖画本身略向右收，未倾斜的样本也会量出约 3° 的偏移，作为零点扣除。
SLANT_ZERO_OFFSET = 3.2


@dataclass
class StyleMetrics:
    slant_deg: float = 0.0  # 整行基线倾斜角，正值右倾
    stroke_ratio: float = 0.0  # 笔宽 / 字高
    line_gap_ratio: float = 0.0  # 行距 / 字高
    char_width_ratio: float = 0.0  # 字宽 / 字高
    spacing_ratio: float = 0.0  # 字间距 / 字宽
    ink_color: str = "#17233b"
    regularity: float = 0.0  # 0 到 1，越高越工整
    rows: int = 0
    characters: int = 0
    slant_sigma_deg: float = 0.0
    height_cv: float = 0.0
    quality: float = 0.0


@dataclass
class StyleProfile:
    metrics: StyleMetrics
    suggested: dict[str, float | str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    overlay: Image.Image | None = None

    @property
    def usable(self) -> bool:
        return not self.warnings


def _runs(flags: np.ndarray) -> list[tuple[int, int]]:
    """Return [start, end) index pairs of consecutive True values."""
    if flags.size == 0:
        return []
    padded = np.concatenate(([False], flags.astype(bool), [False]))
    edges = np.flatnonzero(np.diff(padded.astype(np.int8)))
    return [(int(edges[i]), int(edges[i + 1])) for i in range(0, len(edges), 2)]


def _otsu_threshold(values: np.ndarray) -> float:
    """Otsu threshold over a 0-255 float array."""
    histogram, _ = np.histogram(values, bins=256, range=(0.0, 256.0))
    total = histogram.sum()
    if total <= 0:
        return 0.0
    probability = histogram.astype(np.float64) / total
    omega = np.cumsum(probability)
    mu = np.cumsum(probability * np.arange(256, dtype=np.float64))
    mu_total = mu[-1]
    denominator = omega * (1.0 - omega)
    denominator[denominator <= 1e-9] = 1e-9
    variance = (mu_total * omega - mu) ** 2 / denominator
    return float(np.argmax(variance))


def _denoise(mask: np.ndarray) -> np.ndarray:
    """Drop isolated pixels: an ink pixel needs at least two ink neighbours."""
    padded = np.pad(mask.astype(np.uint8), 1)
    neighbours = np.zeros_like(mask, dtype=np.uint8)
    height, width = mask.shape
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            neighbours += padded[1 + dy : 1 + dy + height, 1 + dx : 1 + dx + width]
    return mask & (neighbours >= 2)


def _ink_mask(image: Image.Image) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """Return (loose mask, core mask, normalised RGB, notes).

    Two masks, on purpose:

    * ``core`` uses a stricter threshold so faint ruled lines, paper texture and
      JPEG noise drop out. Structure analysis (rows, characters, slant) runs on
      it — with the loose mask a printed line every 8 mm fills the gaps between
      handwriting rows and the whole page collapses into one giant "row".
    * ``loose`` keeps the anti-aliased stroke edge. Pen width is measured on it,
      because the strict threshold shaves strokes down to their darkest core and
      every pen width then measures the same.
    """
    notes: list[str] = []
    rgb = np.asarray(image.convert("RGB"), dtype=np.float32)
    gray = np.asarray(image.convert("L"), dtype=np.float32)

    # 深底浅字（白板照片、暗色笔记）先反相，后面所有逻辑都按"深字浅底"处理。
    border = np.concatenate((gray[:3].ravel(), gray[-3:].ravel(), gray[:, :3].ravel(), gray[:, -3:].ravel()))
    if float(np.median(border)) < 110.0:
        gray = 255.0 - gray
        rgb = 255.0 - rgb
        notes.append("检测到深色背景，已自动反相")

    # 用"局部最大值 + 上采样"估计纸面亮度：笔迹比纸暗，取邻域最大值就能把
    # 笔画填平成纸色。这里不能直接大半径高斯模糊——半径一大，粗笔画会被当成
    # 背景吸收掉，量出来的笔宽只剩核心的一半。
    height, width = gray.shape
    small = Image.fromarray(gray.astype(np.uint8)).resize(
        (max(1, width // 8), max(1, height // 8)), Image.Resampling.BILINEAR
    )
    small = small.filter(ImageFilter.MaxFilter(9)).filter(ImageFilter.GaussianBlur(2))
    background = np.asarray(small.resize((width, height), Image.Resampling.BICUBIC), dtype=np.float32)
    diff = background - gray  # 墨迹处为正
    otsu = _otsu_threshold(np.clip(diff, 0.0, 255.0))
    loose = _denoise(diff > max(MIN_INK_CONTRAST, otsu * INK_THRESHOLD_SCALE))
    core = _denoise(diff > max(CORE_INK_CONTRAST, otsu * CORE_THRESHOLD_SCALE))
    return loose, core, rgb, notes


def _components(mask: np.ndarray) -> list[tuple[int, int, int, int, int]]:
    """Connected components via run-length union-find.

    Returns ``(x0, y0, x1, y1, ink_pixels)`` per component. Run-length encoding
    keeps this at a few milliseconds even on a 1600×1200 photo, which a per-pixel
    flood fill could not do in pure Python.
    """
    height = mask.shape[0]
    parent: list[int] = []

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    rows: list[list[tuple[int, int, int]]] = []
    previous: list[tuple[int, int, int]] = []
    for y in range(height):
        row = mask[y]
        runs: list[tuple[int, int, int]] = []
        if row.any():
            padded = np.concatenate(([0], row.astype(np.int8), [0]))
            edges = np.flatnonzero(np.diff(padded))
            for start, end in zip(edges[0::2], edges[1::2]):
                index = len(parent)
                parent.append(index)
                runs.append((int(start), int(end), index))
        if runs and previous:
            i = j = 0
            while i < len(runs) and j < len(previous):
                start, end, index = runs[i]
                p_start, p_end, p_index = previous[j]
                if start < p_end and p_start < end:
                    union(index, p_index)
                if end < p_end:
                    i += 1
                else:
                    j += 1
        rows.append(runs)
        previous = runs

    boxes: dict[int, list[int]] = {}
    for y, runs in enumerate(rows):
        for start, end, index in runs:
            root = find(index)
            box = boxes.get(root)
            if box is None:
                boxes[root] = [start, y, end, y + 1, end - start]
            else:
                box[0] = min(box[0], start)
                box[1] = min(box[1], y)
                box[2] = max(box[2], end)
                box[3] = max(box[3], y + 1)
                box[4] += end - start
    return [(b[0], b[1], b[2], b[3], b[4]) for b in boxes.values()]


def _solid_blocks(
    components: list[tuple[int, int, int, int, int]], mask: np.ndarray
) -> list[tuple[int, int, int, int, int]]:
    """Keep only components big enough to be a character, not a speck of paper."""
    if not components:
        return []
    reference = float(np.percentile([c[3] - c[1] for c in components], 80))
    min_area = max(24, int(mask.size * 0.00002))
    min_height = max(5, int(reference * 0.25))
    return [c for c in components if c[4] >= min_area and (c[3] - c[1]) >= min_height]


def _estimate_stroke_width(mask: np.ndarray) -> float:
    """Estimate pen width as ``2 × area / perimeter``.

    For a long thin stroke area ≈ length × width and perimeter ≈ 2 × length, so
    the ratio cancels the stroke length out and leaves the width. It is a global
    geometric quantity, so unlike run-length medians it does not saturate when
    the mask is anti-aliased or when strokes touch each other.
    """
    area = int(mask.sum())
    if area <= 0:
        return 0.0
    padded = np.pad(mask, 1)
    eroded = (
        padded[1:-1, 1:-1]
        & padded[:-2, 1:-1]
        & padded[2:, 1:-1]
        & padded[1:-1, :-2]
        & padded[1:-1, 2:]
    )
    perimeter = area - int(eroded.sum())
    if perimeter <= 0:
        return 0.0
    raw = 2.0 * area / perimeter
    return max(0.4, raw * PERIMETER_WIDTH_SCALE + PERIMETER_WIDTH_OFFSET)


def _column_centroids(band: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Ink centroid per occupied column, used to fit the baseline slope."""
    heights = band.sum(axis=0)
    columns = np.flatnonzero(heights > 0)
    if columns.size == 0:
        return np.array([]), np.array([])
    rows = np.arange(band.shape[0], dtype=np.float32)[:, None]
    weighted = (band.astype(np.float32) * rows).sum(axis=0)[columns]
    centroids = weighted / np.maximum(1.0, heights[columns].astype(np.float32))
    return columns.astype(np.float32), centroids


def _shear_sharpness(band: np.ndarray, angle_deg: float) -> float:
    """Sharpness of the column profile after shearing the band by ``angle_deg``."""
    height, width = band.shape
    shift = math.tan(math.radians(angle_deg))
    margin = int(abs(shift) * height) + 2
    profile = np.zeros(width + 2 * margin, dtype=np.float32)
    for y in range(height):
        row = band[y]
        if not row.any():
            continue
        offset = int(round(shift * (y - height / 2.0))) + margin
        profile[offset : offset + width] += row
    # 竖画对齐时列投影会出现又高又窄的峰，一阶差分的平方和最大。
    return float(np.sum(np.diff(profile) ** 2))


def _row_slant_degrees(band: np.ndarray) -> float | None:
    """Estimate the glyph slant of one text row.

    Leaning characters make their vertical strokes lean too. Shearing the row by
    the right angle straightens those strokes, which shows up as the sharpest
    possible column profile. Unlike centroid fitting this is a whole-row
    statistic, so it survives messy character splitting and odd glyph shapes.
    """
    counts = band.sum(axis=1)
    rows = np.flatnonzero(counts > 0)
    if rows.size < 12:
        return None
    trimmed = band[rows[0] : rows[-1] + 1]
    if trimmed.shape[0] < 12 or trimmed.shape[1] < 12:
        return None
    coarse = range(-18, 19)
    scores = {angle: _shear_sharpness(trimmed, angle) for angle in coarse}
    best = max(scores, key=scores.get)
    # 在最优角度附近用 0.25° 步长细化，避免只有整数精度。
    fine = [best + step * 0.25 for step in (-3, -2, -1, 0, 1, 2, 3)]
    refined = max(fine, key=lambda angle: _shear_sharpness(trimmed, angle))
    return float(refined) - SLANT_ZERO_OFFSET


def _split_tall_band(
    profile: np.ndarray, top: int, bottom: int, target_height: float, depth: int = 0
) -> list[tuple[int, int]]:
    """Recursively cut a band that is clearly taller than one text row."""
    if bottom - top <= target_height * 1.55 or depth > 3:
        return [(top, bottom)]
    segment = profile[top:bottom]
    margin = max(3, int((bottom - top) * 0.18))
    inner = segment[margin : len(segment) - margin]
    if inner.size < 4:
        return [(top, bottom)]
    cut = top + margin + int(np.argmin(inner))
    if cut - top < target_height * 0.45 or bottom - cut < target_height * 0.45:
        return [(top, bottom)]
    return _split_tall_band(profile, top, cut, target_height, depth + 1) + _split_tall_band(
        profile, cut, bottom, target_height, depth + 1
    )


def _row_bands(mask: np.ndarray, blocks: list[tuple[int, int, int, int, int]]) -> list[tuple[int, int]]:
    """Split the sample into text rows.

    Counting ink pixels per scanline fails on real photos: hand-written descenders
    and in-line annotations keep the gaps between rows non-empty, so a printed
    ruled line plus dense writing merges a dozen rows into one band. Counting how
    many *character blocks* cover each scanline instead gives a profile where a
    row peaks at 20–40 and the gaps drop to single digits, which separates cleanly.
    Bands that are still too tall get cut at their shallowest point.
    """
    height = mask.shape[0]
    if not blocks:
        return []
    # 用 80 分位而不是中位数当字高：断笔、标点和偏旁会产生大量矮块，中位数会把
    # 字高低估近一半，于是行内几像素的缝隙都被当成行间隔，一行被切成两三行。
    char_height = float(np.percentile([b[3] - b[1] for b in blocks], 80))
    if char_height <= 0:
        return []
    coverage = np.zeros(height, dtype=np.float32)
    for x0, y0, x1, y1, _ in blocks:
        coverage[y0:y1] += 1
    if coverage.max() <= 0:
        return []
    window = max(3, int(char_height * 0.15))
    smoothed = np.convolve(coverage, np.ones(window) / window, mode="same")
    threshold = max(2.0, float(smoothed.max()) * 0.25)
    bands: list[tuple[int, int]] = []
    for start, end in _runs(smoothed > threshold):
        if end - start < char_height * 0.4:
            continue
        bands.extend(_split_tall_band(smoothed, start, end, char_height * 1.1))
    # 行内空隙（字与字之间只剩一两笔相连）会让一行裂成几段，间隙远小于字高时并回去。
    # 阈值必须明显小于真实行距：笔记本上相邻两行的间隙常常只有字高的三分之一。
    merge_gap = char_height * 0.18
    merged: list[tuple[int, int]] = []
    for start, end in bands:
        if merged and start - merged[-1][1] <= merge_gap:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return [(a, b) for a, b in merged if b - a >= char_height * 0.5]


def _row_baselines(
    blocks: list[tuple[int, int, int, int, int]], char_height: float, image_height: int
) -> list[float]:
    """Locate text rows as peaks in the histogram of block baselines.

    Row spacing has to survive glyph noise: individual block bottoms wobble by a
    third of a character height depending on whether the character has a
    descender. Binning those bottoms and smoothing turns every row into one wide
    peak, so the distance between peaks is the real line pitch. Clustering the
    raw bottoms instead splits a row apart or welds two rows together.
    """
    if not blocks or char_height <= 0:
        return []
    bottoms = np.asarray([block[3] for block in blocks], dtype=np.float32)
    bin_width = max(1.0, char_height / 18.0)
    edges = np.arange(0.0, image_height + bin_width, bin_width)
    if edges.size < 3:
        return []
    histogram, _ = np.histogram(bottoms, bins=edges)
    window = max(3, int(round(char_height * 0.4 / bin_width)))
    smoothed = np.convolve(histogram.astype(np.float32), np.ones(window) / window, mode="same")
    # 行距小于 1.2 倍字高时两行会叠在一起，真实手写不会这么密；这个下限同时
    # 挡掉了同一行里因下伸笔画产生的次峰。
    min_distance = max(2, int(round(char_height * 1.2 / bin_width)))
    peaks: list[int] = []
    for index in range(1, len(smoothed) - 1):
        if smoothed[index] <= 0 or smoothed[index] < smoothed[index - 1] or smoothed[index] <= smoothed[index + 1]:
            continue
        if peaks and index - peaks[-1] < min_distance:
            # 保留更高的那个峰，避免同一行的两个次峰都被算成行。
            if smoothed[index] > smoothed[peaks[-1]]:
                peaks[-1] = index
            continue
        peaks.append(index)
    return [float(edges[index] + bin_width / 2.0) for index in peaks]


def _character_boxes(band: np.ndarray, band_top: int) -> list[tuple[int, int, int, int]]:
    """Return (x0, y0, x1, y1) boxes in sample coordinates for one row."""
    column_ink = band.sum(axis=0)
    min_width = max(3, int(band.shape[0] * 0.12))
    boxes: list[tuple[int, int, int, int]] = []
    for start, end in _runs(column_ink > 0):
        if end - start < min_width:
            continue
        rows = np.flatnonzero(band[:, start:end].any(axis=1))
        if rows.size == 0:
            continue
        boxes.append((start, band_top + int(rows[0]), end, band_top + int(rows[-1]) + 1))
    return boxes


def _hex_color(rgb: np.ndarray) -> str:
    channels = [int(round(max(0.0, min(255.0, value)))) for value in rgb]
    return "#{:02x}{:02x}{:02x}".format(*channels)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def analyze_handwriting(image: Image.Image, draw_overlay: bool = True) -> StyleProfile:
    """Measure a handwriting sample and suggest renderer parameters."""
    working = image.convert("RGB")
    long_edge = max(working.size)
    if long_edge > ANALYSIS_MAX_EDGE:
        scale = ANALYSIS_MAX_EDGE / long_edge
        working = working.resize(
            (max(1, round(working.width * scale)), max(1, round(working.height * scale))),
            Image.Resampling.LANCZOS,
        )

    loose, core, rgb, notes = _ink_mask(working)
    blocks = _solid_blocks(_components(core), core)
    metrics = StyleMetrics()
    warnings: list[str] = []

    bands = _row_bands(core, blocks)
    metrics.rows = len(bands)
    if not bands:
        warnings.append("样本里没有找到清晰的文字，请换一张对比更明显的照片。")
        return StyleProfile(metrics=metrics, notes=notes, warnings=warnings, overlay=_draw_overlay(working, [], [], 0.0))

    slants: list[float] = []
    char_heights: list[float] = []
    char_widths: list[float] = []
    gaps: list[float] = []
    row_centres: list[float] = []
    all_boxes: list[tuple[int, int, int, int]] = []
    row_boxes: list[tuple[int, int, int, int]] = []

    for top, bottom in bands:
        band = core[top:bottom]
        boxes = _character_boxes(band, top)
        if len(boxes) < 2:
            continue
        row_height = bottom - top
        # 过滤被切碎或粘成一团的框，它们会把字高和倾斜统计带偏。
        valid = [
            box
            for box in boxes
            if (box[3] - box[1]) >= row_height * 0.4 and (box[2] - box[0]) >= row_height * 0.2
        ]
        if len(valid) < 2:
            valid = boxes
        row_boxes.append((valid[0][0], top, valid[-1][2], bottom))
        all_boxes.extend(valid)
        row_centres.append((top + bottom) / 2.0)
        row_slant = _row_slant_degrees(band)
        if row_slant is not None and abs(row_slant) < 45.0:
            slants.append(row_slant)
        for box in valid:
            char_heights.append(box[3] - box[1])
            char_widths.append(box[2] - box[0])
        for index in range(len(valid) - 1):
            gaps.append(valid[index + 1][0] - valid[index][2])

    metrics.characters = len(all_boxes)
    if metrics.characters < MIN_CHARACTERS or metrics.rows < MIN_ROWS:
        warnings.append(
            f"只识别到 {metrics.rows} 行 / {metrics.characters} 个字，样本太少，"
            f"建议至少写 {MIN_ROWS} 行、{MIN_CHARACTERS} 个字再试。"
        )
        return StyleProfile(
            metrics=metrics,
            notes=notes,
            warnings=warnings,
            overlay=_draw_overlay(working, row_boxes, all_boxes, 0.0) if draw_overlay else None,
        )

    median_height = float(np.median(char_heights))
    metrics.slant_deg = float(np.median(slants)) if slants else 0.0
    metrics.slant_sigma_deg = float(np.std(slants)) if len(slants) > 1 else 0.0
    metrics.char_width_ratio = float(np.median(char_widths)) / max(1.0, median_height)
    metrics.spacing_ratio = float(np.median(gaps)) / max(1.0, float(np.median(char_widths)))

    # 行数与行距以"字块底边直方图的峰"为准：行带偶尔会把紧挨的两行并成一个。
    row_reference = float(np.percentile([b[3] - b[1] for b in blocks], 80)) if blocks else median_height
    baselines = _row_baselines(blocks, row_reference, core.shape[0])
    if len(baselines) > 1:
        metrics.rows = len(baselines)
        metrics.line_gap_ratio = float(np.median(np.diff(baselines))) / max(1.0, median_height)
    elif len(row_centres) > 1:
        row_gaps = np.diff(row_centres)
        metrics.line_gap_ratio = float(np.median(row_gaps)) / max(1.0, median_height)

    # 笔宽只在"字符块外接框"内统计宽松掩膜：框外可能是印刷横线，会把笔宽带偏。
    stroke_region = np.zeros_like(loose)
    for x0, y0, x1, y1, _ in blocks:
        stroke_region[y0:y1, x0:x1] = True
    metrics.stroke_ratio = _estimate_stroke_width(loose & stroke_region) / max(1.0, median_height)

    heights = np.asarray(char_heights, dtype=np.float32)
    metrics.height_cv = float(np.std(heights) / max(1.0, float(np.mean(heights))))

    ink_pixels = rgb[core]
    if ink_pixels.size:
        luma = ink_pixels.mean(axis=1)
        # 只取最深的 3%：抗锯齿边缘会明显冲淡墨色，用均值会得到偏灰的颜色。
        cutoff = float(np.percentile(luma, 3))
        deep = ink_pixels[luma <= cutoff]
        metrics.ink_color = _hex_color(deep.mean(axis=0) if deep.size else ink_pixels.mean(axis=0))

    # 规整度：行倾斜越分散、字高越参差，分数越低。
    slant_spread = _clamp(metrics.slant_sigma_deg / 6.0, 0.0, 1.0)
    height_spread = _clamp(metrics.height_cv / 0.35, 0.0, 1.0)
    metrics.regularity = round(_clamp(1.0 - (slant_spread + height_spread) / 2.0, 0.0, 1.0), 3)

    # 质量：内容量、对比度、规整度三者的加权，用于提示用户样本是否够用。
    content_score = _clamp(metrics.characters / 80.0, 0.0, 1.0)
    contrast = float(np.median(255.0 - np.asarray(working.convert("L"), dtype=np.float32)[core])) / 255.0
    metrics.quality = round(_clamp(0.45 * content_score + 0.35 * contrast + 0.20 * metrics.regularity, 0.0, 1.0), 3)

    suggested = _suggest(metrics)
    notes.extend(_describe(metrics, suggested))
    if metrics.quality < 0.35:
        warnings.append("样本对比度或字数偏低，识别结果可能不够准，建议重新拍一张。")

    overlay = None
    if draw_overlay:
        overlay = _draw_overlay(working, row_boxes, all_boxes, metrics.slant_deg)
    return StyleProfile(metrics=metrics, suggested=suggested, notes=notes, warnings=warnings, overlay=overlay)


def _suggest(metrics: StyleMetrics) -> dict[str, float | str]:
    """Map measured ratios onto renderer parameters."""
    stroke_width = _clamp(16.0 * metrics.stroke_ratio / STROKE_RATIO_REFERENCE, 6.0, 22.0)
    line_spacing = _clamp(metrics.line_gap_ratio * LINE_SPACING_COEFFICIENT, 1.0, 2.5)
    # 越不规整，随机扰动越大；规整度 1.0 时接近工整，0.3 时接近随性。
    irregular = _clamp(1.0 - metrics.regularity, 0.0, 1.0)
    return {
        "handwriting.ink_color": metrics.ink_color,
        "handwriting.glyph_slant_deg": round(_clamp(metrics.slant_deg, -18.0, 18.0), 1),
        "handwriting.sdt_stroke_width": round(stroke_width, 1),
        "handwriting.line_spacing": round(line_spacing, 2),
        "handwriting.perturb_theta_sigma": round(_clamp(0.004 + irregular * 0.022, 0.0, 0.06), 4),
        "handwriting.baseline_wave_amplitude_ratio": round(_clamp(0.006 + irregular * 0.03, 0.0, 0.08), 4),
        "handwriting.elastic_strength_ratio": round(_clamp(0.001 + irregular * 0.005, 0.0, 0.1), 4),
    }


def _describe(metrics: StyleMetrics, suggested: dict[str, float | str]) -> list[str]:
    """Human-readable summary shown in the UI next to the measured values."""
    notes = [
        f"共 {metrics.rows} 行、{metrics.characters} 个字，样本质量 {metrics.quality:.0%}。",
        f"整体倾斜 {metrics.slant_deg:+.1f}°（相对内置字体，正值更右倾），行距约 {metrics.line_gap_ratio:.2f} 倍字高。",
        f"笔宽约为字高的 {metrics.stroke_ratio:.1%}，规整度 {metrics.regularity:.0%}。",
    ]
    width_delta = metrics.char_width_ratio - CHAR_WIDTH_REFERENCE
    if width_delta < -0.12:
        notes.append("字形偏瘦长，渲染时保持现有字体宽度即可。")
    elif width_delta > 0.12:
        notes.append("字形偏扁宽，渲染时保持现有字体宽度即可。")
    if metrics.spacing_ratio < 0.05:
        notes.append("字与字挨得很紧，可在“字符间距”里给一点负值。")
    elif metrics.spacing_ratio > 0.28:
        notes.append("字距偏松，可在“字符间距”里给一点正值。")
    return notes


def _draw_overlay(
    image: Image.Image,
    row_boxes: list[tuple[int, int, int, int]],
    char_boxes: list[tuple[int, int, int, int]],
    slant_deg: float,
) -> Image.Image:
    """Draw the detected rows, characters and slanted centre line for a sanity check."""
    canvas = image.convert("RGB").copy()
    draw = ImageDraw.Draw(canvas, "RGBA")
    for x0, y0, x1, y1 in char_boxes:
        draw.rectangle((x0, y0, x1 - 1, y1 - 1), outline=(0, 122, 255, 110), width=1)
    angle = math.radians(slant_deg)
    for x0, y0, x1, y1 in row_boxes:
        draw.rectangle((x0 - 2, y0 - 2, x1 + 1, y1 + 1), outline=(52, 199, 89, 220), width=2)
        centre_y = (y0 + y1) / 2.0
        half = (x1 - x0) / 2.0
        draw.line(
            (
                x0,
                centre_y - math.tan(angle) * half,
                x1,
                centre_y + math.tan(angle) * half,
            ),
            fill=(255, 59, 48, 200),
            width=2,
        )
    return canvas


def analyze_file(path: str | Path) -> StyleProfile:
    with Image.open(path) as image:
        return analyze_handwriting(image)
