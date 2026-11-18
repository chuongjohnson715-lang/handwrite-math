"""把斜拍的照片摆正：给定纸张四个角，做透视变换拉成正矩形。

为什么需要：横线对齐假设线是水平的，但手持拍摄的纸几乎总有 1–2° 的倾斜和
透视。一张 A4 宽 1240px 上偏 2° 就是 40px，比半个字还高，怎么调行高都会压线。
"""

from __future__ import annotations

import math

import numpy as np
from PIL import Image

# 四边形面积至少占整图的这个比例，否则多半是点错了角。
MIN_AREA_RATIO = 0.15
# 对边长度差异超过这个比例，说明角点顺序错了（把对角线当边）。
MAX_EDGE_RATIO = 3.0


class RectifyError(ValueError):
    """角点不合法。"""


def _order_corners(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """按左上、右上、右下、左下排序，容忍用户乱序点击。"""
    if len(points) != 4:
        raise RectifyError("需要四个角点。")
    ordered = sorted(points, key=lambda p: p[1])  # 先按 y 分上下两组
    top = sorted(ordered[:2], key=lambda p: p[0])
    bottom = sorted(ordered[2:], key=lambda p: p[0])
    return [top[0], top[1], bottom[1], bottom[0]]


def _polygon_area(points: list[tuple[float, float]]) -> float:
    total = 0.0
    for index in range(len(points)):
        x0, y0 = points[index]
        x1, y1 = points[(index + 1) % len(points)]
        total += x0 * y1 - x1 * y0
    return abs(total) / 2.0


def _perspective_coeffs(
    source: list[tuple[float, float]], target: list[tuple[float, float]]
) -> list[float]:
    """求把 source 映射到 target 的八个透视系数。

    PIL 的 ``transform`` 要求的是"输出坐标 → 输入坐标"，所以调用方要把
    目标矩形当 source 传进来。
    """
    matrix: list[list[float]] = []
    values: list[float] = []
    for (x, y), (u, v) in zip(source, target):
        matrix.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        matrix.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        values.extend([u, v])
    try:
        coeffs = np.linalg.solve(np.asarray(matrix, dtype=np.float64), np.asarray(values, dtype=np.float64))
    except np.linalg.LinAlgError as error:  # 四点共线或重合
        raise RectifyError("四个角点无法构成四边形，请重新点。") from error
    if not np.all(np.isfinite(coeffs)):
        raise RectifyError("角点位置异常，请重新点。")
    return coeffs.tolist()


def rectify(image: Image.Image, corners: list[tuple[float, float]]) -> tuple[Image.Image, list[tuple[float, float]]]:
    """把 ``corners`` 圈出的四边形拉成正矩形。

    ``corners`` 是归一化坐标（0-1），返回校正后的图片和实际使用的角点顺序。
    """
    width, height = image.size
    pixels = [(x * width, y * height) for x, y in corners]
    ordered = _order_corners(pixels)

    area_ratio = _polygon_area(ordered) / float(width * height)
    if area_ratio < MIN_AREA_RATIO:
        raise RectifyError("圈出的区域太小，请沿纸张四边点角。")

    top_edge = math.dist(ordered[0], ordered[1])
    bottom_edge = math.dist(ordered[3], ordered[2])
    left_edge = math.dist(ordered[0], ordered[3])
    right_edge = math.dist(ordered[1], ordered[2])
    for first, second, label in (
        (top_edge, bottom_edge, "上下两边"),
        (left_edge, right_edge, "左右两边"),
    ):
        shorter = min(first, second)
        if shorter <= 1 or max(first, second) / shorter > MAX_EDGE_RATIO:
            raise RectifyError(f"{label}长度差太多，角点可能点错了位置。")

    out_w = max(16, int(round((top_edge + bottom_edge) / 2.0)))
    out_h = max(16, int(round((left_edge + right_edge) / 2.0)))

    # 目标矩形的四个角（左上、右上、右下、左下）
    target = [(0.0, 0.0), (float(out_w), 0.0), (float(out_w), float(out_h)), (0.0, float(out_h))]
    coeffs = _perspective_coeffs(target, ordered)
    rectified = image.convert("RGB").transform(
        (out_w, out_h),
        Image.Transform.PERSPECTIVE,
        coeffs,
        Image.Resampling.BICUBIC,
    )
    normalised = [(x / width, y / height) for x, y in ordered]
    return rectified, normalised


def tilt_degrees(image: Image.Image) -> float:
    """估计图片里横线的倾斜角（正值为右端更低）。

    在左右两条竖带里各取暗度曲线，找左带的若干个峰，再在右带附近找对应的峰，
    偏移取中位数。比"全图最强行"稳：后者在左右两边常常落在不同的东西上
    （页眉边缘、装订孔），算出来的角度没有意义。
    """
    gray = np.asarray(image.convert("L"), dtype=np.float32)
    height, width = gray.shape
    if width < 160 or height < 160:
        return 0.0
    band = max(20, width // 14)
    left = gray[:, width // 12 : width // 12 + band]
    right = gray[:, -width // 12 - band : -width // 12]

    def darkness(strip: np.ndarray) -> np.ndarray:
        paper = float(np.percentile(strip, 85))
        return (strip < paper - 8).sum(axis=1) / strip.shape[1]

    left_profile = darkness(left)
    right_profile = darkness(right)
    # 只在中段找峰：页眉色块和页脚阴影不是横线。
    low, high = int(height * 0.15), int(height * 0.9)
    span = high - low
    if span < 40:
        return 0.0
    smoothed = np.convolve(left_profile[low:high], np.ones(3) / 3.0, mode="same")
    threshold = float(smoothed.max()) * 0.5
    search = max(20, int(span * 0.04))
    offsets: list[float] = []
    for index in range(1, len(smoothed) - 1):
        if smoothed[index] < threshold or smoothed[index] < smoothed[index - 1] or smoothed[index] <= smoothed[index + 1]:
            continue
        centre = low + index
        lo = max(0, centre - search)
        hi = min(height, centre + search)
        match = lo + int(np.argmax(right_profile[lo:hi]))
        offsets.append(match - centre)
    if not offsets:
        return 0.0
    span_px = width - 2 * (width // 12)
    return math.degrees(math.atan2(float(np.median(offsets)), span_px))
