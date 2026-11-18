from __future__ import annotations

import json
import math
from copy import deepcopy
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from PIL import ImageColor


@dataclass
class PageConfig:
    width_mm: float = 210.0  # 纸张宽度，单位 mm；默认 A4 宽度。
    height_mm: float = 297.0  # 纸张高度，单位 mm；默认 A4 高度。
    dpi: int = 300  # 栅格渲染分辨率；越高越清晰，但生成时间和文件体积也越大。
    margin_top_mm: float = 54.0  # 正文区域上边距，单位 mm。
    margin_bottom_mm: float = 20.0  # 正文区域下边距，单位 mm。
    margin_left_mm: float = 22.0  # 正文区域左边距，单位 mm。
    margin_right_mm: float = 20.0  # 正文区域右边距，单位 mm。


@dataclass
class BackgroundConfig:
    style: str = "lined"  # 纸张样式：plain、lined、grid、dot 或 image。
    image: str | None = None  # 背景图片或 PDF 路径；仅 image 样式使用，None 表示未指定。
    auto_discover: bool = True  # image 样式未指定路径时，是否自动搜索 background/ 素材。
    paper_color: str = "#fffdf4"  # 程序生成纸张的底色，使用十六进制颜色。
    line_color: str = "#d7e4ef"  # 横线、方格线和点阵的颜色。
    margin_line_color: str = "#edb2aa"  # 左侧页边参考线的颜色。
    line_gap_mm: float = 8.0  # 横线纸相邻横线的间距，单位 mm。
    grid_size_mm: float = 8.0  # 方格纸单格边长，单位 mm。
    dot_gap_mm: float = 5.0  # 点阵纸相邻点的间距，单位 mm。
    dot_radius_px: int = 1  # 点阵圆点半径，单位输出像素。
    draw_margin_line: bool = True  # 是否绘制左侧红色页边参考线。
    writing_areas: list[dict[str, float]] = field(default_factory=list)  # 书写区域列表，每项为 {x, y, w, h}，取值为页面宽高的比例；空列表表示沿用页边距构成的整块区域。
    ruled_lines: list[float] = field(default_factory=list)  # 背景横线的归一化 y 位置（0-1），由"检测横线"写入。
    snap_ruled_lines: bool = False  # 是否把正文行基线吸附到 ruled_lines 上，行高与段落间距会被量化成线间距的整数倍。
    rule_tilt: float = 0.0  # 横线的倾斜量：页面像素里每向右 1px 线下降多少 px；0 表示水平。照片拍歪时由标定写入。
    rule_tilt_gradient: float = 0.0  # 透视翻拍的坡度变化：背景图里每往下 1px，倾斜量变化多少；0 表示整页一个坡度。
    depth_blur: float = 0.0  # 背景照片的景深模糊强度（0-1）：书写区保持锐利，越远越虚；只作用于背景层。
    bokeh: float = 0.0  # 背景照片的焦外光斑强度（0-1）：从虚化区域的亮部生成圆形光斑。
    vignette: float = 0.0  # 背景照片的暗角强度（0-1）：四周压暗，突出纸面。
    section_images: list[dict[str, str]] = field(default_factory=list)  # 分节背景：每项 {heading: 标题原文, image: 图片路径}；命中标题时该节另起一页并换底图。
    section_level: int = 1  # 用几级标题分节：1 表示一级标题，2/3 依次向下。


@dataclass
class HandwritingConfig:
    font_path: str | None = None  # 正文字体路径；None 时按项目字体和系统字体顺序自动选择。
    math_font_path: str | None = None  # 数学字符字体路径；None 时沿用正文字体链。
    fallback_font_path: str | None = None  # 首选字体缺字时使用的额外回退字体路径。
    ink_color: str = "#17233b"  # 正文、公式和生成线条共用的墨水颜色。
    body_font_pt: float = 16.0  # 正文字号，单位 pt。
    math_font_pt: float = 16.0  # 展示公式及行内公式主字符字号，单位 pt。
    code_font_pt: float = 11.5  # 代码块字号，单位 pt。
    h1_font_pt: float = 22.0  # 一级标题字号，单位 pt；不会额外加粗。
    h2_font_pt: float = 17.0  # 二级标题字号，单位 pt；不会额外加粗。
    h3_font_pt: float = 15.0  # 三级标题字号，单位 pt；不会额外加粗。
    line_spacing: float = 1.55  # 行高相对于当前字号的倍数。
    word_spacing_px: float = -1.0  # 非空格字符前进量的附加输出像素；负值收紧，正值放宽。
    advance_jitter_sigma_ratio: float = 0.01  # 字符前进量随机扰动的标准差，相对当前字号；0 表示关闭。
    perturb_theta_sigma: float = 0.008  # 字符旋转角度的正态分布标准差，单位 rad。
    glyph_slant_deg: float = 0.0  # 每个字符的整体倾斜角，正值表示右倾（顶部朝右），单位度；0 表示不倾斜。
    perturb_x_sigma_px: float | None = None  # 字符水平偏移标准差，单位 px；None 时取字号的 1.5%。
    perturb_y_sigma_px: float | None = None  # 正文字符垂直偏移标准差，单位 px；None 时取字号的 1.5%。
    math_perturb_y_sigma_ratio: float = 0.015  # 公式字符垂直偏移标准差，相对各字符当前字号的比例。
    sdt_trajectory_enabled: bool = True  # 是否优先使用 SDT 在线轨迹直接渲染字符。
    sdt_trajectory_path: str | None = None  # SDT 轨迹库路径；None 时使用项目内置轨迹包。
    sdt_stroke_width: float = 16.0  # 统一基础笔画粗细，按 SDT 256 单位画布定义。
    sdt_coordinate_jitter: float = 0.6  # 沿轨迹施加的低频平滑坐标扰动幅度。
    sdt_jitter_correlation: float = 10.0  # 坐标和笔压噪声的平滑相关长度，单位重采样点。
    sdt_width_jitter: float = 0.15  # 沿单条笔画变化的相对笔压标准差。
    sdt_taper: float = 0.30  # 起笔、收笔相对于主体笔宽的渐细比例。
    sdt_supersample: int = 3  # 轨迹渲染超采样倍数，用于改善抗锯齿质量。
    second_layer_enabled: bool = True  # 是否启用栅格化后的形变、字重和墨色后处理。
    elastic_strength_ratio: float = 0.003  # 局部弹性形变幅度，相对于当前字号的比例。
    elastic_smoothness_ratio: float = 0.55  # 弹性位移场的平滑尺度，相对于当前字号的比例。
    baseline_wave_amplitude_ratio: float = 0.018  # 整行基线起伏幅度，相对于当前字号的比例。
    baseline_wave_length_em: float = 12.0  # 基线正弦起伏的波长，单位 em。
    stroke_weight_base_scale: float = 1.0  # 回退字体目标基础字重倍率；SDT 模式不使用。
    stroke_weight_variation_sigma_ratio: float = 0.0035  # 整组文字或公式粗细变化的正态分布标准差，单位 em。
    ink_density_jitter: float = 0.08  # 墨水浓淡沿笔迹缓慢变化的幅度，范围 0 到 1。
    dry_brush_probability: float = 0.08  # 每个文字或线条图块出现轻微飞白的概率。
    dry_brush_min_opacity: float = 0.72  # 飞白区域允许达到的最低不透明度，范围 0 到 1。
    scribble_probability: float = 0.06  # 每一行正文被随手涂写划掉的概率，范围 0 到 1。
    math_rule_tilt_ratio: float = 0.048  # 分数线、根号线和表格线的随机倾斜幅度，单位 em。
    math_rule_wobble_ratio: float = 0.012  # 公式线和表格线的低频弯曲幅度，单位 em。
    math_rule_jitter_ratio: float = 0.050  # 公式线和表格线的细小路径抖动幅度，单位 em。
    seed: int | str | None = 2026  # 全局随机种子；相同值生成可复现结果，None 会作为空种子稳定复现。
    prefer_handright: bool = True  # SDT 不可用或关闭时，是否优先使用 Handright 回退引擎。
    glyph_library: str | None = None  # 个人字形库目录；有该字的字形时优先用它，否则回退到字体。


@dataclass
class LayoutConfig:
    number_sections: bool = False  # 是否自动为一至三级标题添加章节编号。
    number_figures: bool = False  # 是否按文档图片顺序从 1 编号；关闭时只显示图片标题。
    justify_paragraphs: bool = True  # 正文自然折行且够满的非末行向右拉伸，最多 18%；显式换行和段尾不拉伸。
    show_page_numbers: bool = False  # 是否在页面底部绘制页码。
    paragraph_gap_mm: float = 2.2  # 段落结束后的垂直间距，单位 mm。
    heading_gap_before_mm: float = 5.0  # 标题前的垂直间距，单位 mm。
    heading_gap_after_mm: float = 3.0  # 标题后的垂直间距，单位 mm。
    formula_gap_mm: float = 3.0  # 展示公式上下两侧的留白，单位 mm。
    table_gap_mm: float = 4.0  # 表格与上下正文之间的间距，单位 mm。
    table_cell_padding_mm: float = 2.0  # 表格单元格内容与边框的内边距，单位 mm。
    first_line_indent_em: float = 2.0  # 普通段落首行缩进，单位 em。
    list_indent_mm: float = 7.0  # 有序和无序列表内容的左缩进，单位 mm。
    image_placeholder_height_mm: float = 65.0  # 图片占位框高度，单位 mm。
    image_placeholder_border: bool = True  # 是否绘制图片占位框边框。
    footer_gap_mm: float = 8.0  # 启用页码时为页脚预留的正文禁入高度，单位 mm。


@dataclass
class ReportConfig:
    page: PageConfig = field(default_factory=PageConfig)  # 页面尺寸、分辨率和页边距。
    background: BackgroundConfig = field(default_factory=BackgroundConfig)  # 纸张背景及辅助线样式。
    handwriting: HandwritingConfig = field(default_factory=HandwritingConfig)  # 字体、轨迹及两层笔迹扰动。
    layout: LayoutConfig = field(default_factory=LayoutConfig)  # 标题、段落、公式、表格和页脚布局。


def load_config(path: str | Path | None = None) -> ReportConfig:
    config = ReportConfig()
    if path is None:
        return config
    data = _migrate_config(json.loads(Path(path).read_text(encoding="utf-8")))
    _update_dataclass(config, data)
    validate_layout_options(config)
    return config


def config_from_dict(data: dict[str, Any]) -> ReportConfig:
    """Build a report configuration from an already decoded JSON object."""
    if not isinstance(data, dict):
        raise TypeError("Config must be a JSON object.")
    data = _migrate_config(data)
    config = ReportConfig()
    _update_dataclass(config, data)
    validate_layout_options(config)
    return config


def validate_layout_options(config: ReportConfig) -> None:
    """Validate shared spacing and layout controls for JSON, CLI, and web entry points."""
    for value, minimum, maximum, label in (
        (config.handwriting.word_spacing_px, -12.0, 24.0, "字符间距"),
        (config.handwriting.advance_jitter_sigma_ratio, 0.0, 0.1, "字距随机扰动 σ"),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not minimum <= value <= maximum
        ):
            raise ValueError(f"{label}必须是 {minimum:g} 到 {maximum:g} 之间的有限数值。")
    if not isinstance(config.layout.justify_paragraphs, bool):
        raise ValueError("拉伸至右侧对齐必须是布尔值 true 或 false。")
    if not isinstance(config.layout.number_figures, bool):
        raise ValueError("自动编号插图必须是布尔值 true 或 false。")
    tilt = config.background.rule_tilt
    if (
        isinstance(tilt, bool)
        or not isinstance(tilt, (int, float))
        or not math.isfinite(tilt)
        or not -0.5 <= tilt <= 0.5
    ):
        raise ValueError("横线倾斜必须是 -0.5 到 0.5 之间的有限数值（每向右 1px 线下降多少 px）。")
    gradient = config.background.rule_tilt_gradient
    if (
        isinstance(gradient, bool)
        or not isinstance(gradient, (int, float))
        or not math.isfinite(gradient)
        or not -0.002 <= gradient <= 0.002
    ):
        raise ValueError("横线坡度变化必须是 -0.002 到 0.002 之间的有限数值（每往下 1px 坡度变化多少）。")
    validate_writing_areas(config.background.writing_areas)


MAX_WRITING_AREAS = 12
# 比例坐标的浮点误差容忍度：0.3333 + 0.6667 之类的加法不应被判为越界。
_AREA_EPSILON = 1e-6


def validate_writing_areas(areas: Any) -> None:
    """Reject writing areas that cannot be mapped onto the page."""
    if not isinstance(areas, list):
        raise ValueError("书写区域必须是列表。")
    if len(areas) > MAX_WRITING_AREAS:
        raise ValueError(f"书写区域最多支持 {MAX_WRITING_AREAS} 个。")
    for index, area in enumerate(areas, start=1):
        if not isinstance(area, dict):
            raise ValueError(f"第 {index} 个书写区域格式不正确。")
        values: dict[str, float] = {}
        for key in ("x", "y", "w", "h"):
            value = area.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"第 {index} 个书写区域的 {key} 必须是有限数值。")
            values[key] = float(value)
        if not (0.0 - _AREA_EPSILON <= values["x"] <= 1.0 + _AREA_EPSILON):
            raise ValueError(f"第 {index} 个书写区域的横向起点必须位于 0 到 1 之间。")
        if not (0.0 - _AREA_EPSILON <= values["y"] <= 1.0 + _AREA_EPSILON):
            raise ValueError(f"第 {index} 个书写区域的纵向起点必须位于 0 到 1 之间。")
        if values["w"] <= 0.0 or values["h"] <= 0.0:
            raise ValueError(f"第 {index} 个书写区域的宽高必须大于 0。")
        if values["x"] + values["w"] > 1.0 + _AREA_EPSILON or values["y"] + values["h"] > 1.0 + _AREA_EPSILON:
            raise ValueError(f"第 {index} 个书写区域超出了页面范围。")


def _migrate_config(data: dict[str, Any]) -> dict[str, Any]:
    """Translate the former probability-plus-amplitude weight model to sigma."""
    migrated = deepcopy(data)
    handwriting = migrated.get("handwriting")
    if not isinstance(handwriting, dict):
        return migrated

    legacy_ratio = handwriting.pop("stroke_weight_variation_ratio", None)
    legacy_probability = handwriting.pop("stroke_weight_variation_probability", None)
    if (
        "stroke_weight_variation_sigma_ratio" not in handwriting
        and (legacy_ratio is not None or legacy_probability is not None)
    ):
        amplitude = max(0.0, float(0.012 if legacy_ratio is None else legacy_ratio))
        probability = min(1.0, max(0.0, float(0.22 if legacy_probability is None else legacy_probability)))
        # Var[p * Uniform(-a, a)] = p*a^2/3. Preserve the old RMS
        # strength while replacing its point mass at zero with N(0, sigma^2).
        handwriting["stroke_weight_variation_sigma_ratio"] = amplitude * math.sqrt(probability / 3.0)
    return migrated


def _update_dataclass(target: Any, data: dict[str, Any]) -> None:
    known_fields = {field.name for field in fields(target)}
    for key, value in data.items():
        if key not in known_fields:
            raise ValueError(f"Unknown config field: {key}")
        current = getattr(target, key)
        if is_dataclass(current) and isinstance(value, dict):
            _update_dataclass(current, value)
        else:
            setattr(target, key, value)


def mm_to_px(mm: float, dpi: int) -> int:
    return round(mm / 25.4 * dpi)


def pt_to_px(pt: float, dpi: int) -> int:
    return max(1, round(pt / 72.0 * dpi))


def color(value: str, alpha: int | None = None) -> tuple[int, ...]:
    rgb = ImageColor.getrgb(value)
    if alpha is None:
        return rgb
    return (*rgb, alpha)
