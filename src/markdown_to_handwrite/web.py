from __future__ import annotations

import argparse
import base64
import binascii
import io
import json
import math
import mimetypes
import re
import secrets
import threading
import time
import webbrowser
from dataclasses import asdict, fields, is_dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from PIL import Image, ImageColor, ImageOps

from .config import ReportConfig, config_from_dict, validate_layout_options
from .glyph_library import GlyphLibrary, build_library
from .glyph_template import COMMON_CHARS, build_template, load_manifest
from .markdown_parser import HeadingBlock, parse_markdown
from .renderer import PROJECT_ROOT, ReportRenderer
from .rectify import RectifyError, rectify
from .ruled_lines import detect_ruled_lines, detect_tilted_lines
from .style_profile import analyze_handwriting

try:  # 可选依赖：只有预览 PDF 背景时才需要
    import fitz
except ImportError:  # pragma: no cover
    fitz = None


STATIC_ROOT = Path(__file__).resolve().parent / "webui"
OUTPUT_ROOT = PROJECT_ROOT / "output" / "webui"
UPLOAD_ROOT = PROJECT_ROOT / "background" / "uploads"
GLYPH_ROOT = PROJECT_ROOT / "glyphs"
# 笔迹档案：一张样本照片 + 量出来的风格参数，作为"我的笔迹"出现在字体下拉里。
STYLE_ROOT = PROJECT_ROOT / "profiles"
# 只有这些键允许写进档案，避免前端塞进任意配置。
STYLE_KEY_WHITELIST = frozenset(
    {
        "handwriting.ink_color",
        "handwriting.glyph_slant_deg",
        "handwriting.sdt_stroke_width",
        "handwriting.line_spacing",
        "handwriting.perturb_theta_sigma",
        "handwriting.baseline_wave_amplitude_ratio",
        "handwriting.elastic_strength_ratio",
    }
)
TEMPLATE_ROOT = PROJECT_ROOT / "output" / "glyph-template"
TEMPLATE_NAME = "common-300"
MAX_REQUEST_BYTES = 4 * 1024 * 1024
# 背景图上传走 base64，体积约为原文件的 1.34 倍，因此单独放宽到 24 MB。
MAX_UPLOAD_REQUEST_BYTES = 24 * 1024 * 1024
MAX_BACKGROUND_BYTES = 16 * 1024 * 1024
UPLOAD_MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "application/pdf": ".pdf",
}
BACKGROUND_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}
# 背景预览图的宽度上限：只用于界面里画书写区域，不需要输出分辨率。
BACKGROUND_PREVIEW_MAX_WIDTH = 1400
# 字迹识别结果的切分可视化宽度上限。
OVERLAY_MAX_WIDTH = 900


def _range(
    path: str,
    label: str,
    minimum: float,
    maximum: float,
    step: float,
    unit: str = "",
    help_text: str = "",
) -> dict[str, Any]:
    return {
        "path": path,
        "label": label,
        "control": "range",
        "min": minimum,
        "max": maximum,
        "step": step,
        "unit": unit,
        "help": help_text,
    }


def _toggle(path: str, label: str, help_text: str = "") -> dict[str, Any]:
    return {"path": path, "label": label, "control": "toggle", "help": help_text}


def _text(path: str, label: str, help_text: str = "", control: str = "text") -> dict[str, Any]:
    return {"path": path, "label": label, "control": control, "help": help_text}


def _nullable_range(
    path: str,
    label: str,
    minimum: float,
    maximum: float,
    step: float,
    unit: str = "px",
    help_text: str = "",
) -> dict[str, Any]:
    item = _range(path, label, minimum, maximum, step, unit, help_text)
    item["control"] = "nullable-range"
    return item


CONFIG_SECTIONS: list[dict[str, Any]] = [
    {
        "id": "page",
        "title": "页面与分辨率",
        "eyebrow": "PAGE",
        "description": "纸张尺寸、输出精度与四周留白",
        "fields": [
            _range("page.width_mm", "纸张宽度", 120, 420, 1, "mm"),
            _range("page.height_mm", "纸张高度", 120, 594, 1, "mm"),
            _range("page.dpi", "输出 DPI", 96, 600, 12, "dpi", "DPI 越高越清晰，也会显著增加生成时间。"),
            _range("page.margin_top_mm", "上边距", 0, 90, 1, "mm"),
            _range("page.margin_bottom_mm", "下边距", 0, 90, 1, "mm"),
            _range("page.margin_left_mm", "左边距", 0, 70, 1, "mm"),
            _range("page.margin_right_mm", "右边距", 0, 70, 1, "mm"),
        ],
    },
    {
        "id": "background",
        "title": "纸张与背景",
        "eyebrow": "PAPER",
        "description": "底色、纸面纹理与参考背景",
        "fields": [
            {
                "path": "background.style",
                "label": "纸张样式",
                "control": "select",
                "options": [
                    {"value": "plain", "label": "纯色纸"},
                    {"value": "lined", "label": "横线纸"},
                    {"value": "grid", "label": "方格纸"},
                    {"value": "dot", "label": "点阵纸"},
                    {"value": "image", "label": "背景素材"},
                ],
                "help": "纯色、横线、方格和点阵由程序绘制；选择背景素材时才读取背景文件或自动发现素材。",
            },
            _text("background.image", "背景文件", "选择文件后自动切换到“背景素材”；留空时可使用自动发现。", "background-path"),
            {
                "path": "background.writing_areas",
                "label": "书写区域",
                "control": "writing-areas",
                "help": "导入背景后框选一个或多个书写区域，正文按顺序填满它们，用尽后自动换页；留空表示沿用页边距围成的整块区域。",
            },
            _toggle("background.auto_discover", "自动发现背景", "仅在纸张样式为“背景素材”且未指定文件时搜索 background/ 目录。"),
            _text("background.paper_color", "纸张底色", control="color"),
            _text("background.line_color", "辅助线颜色", control="color"),
            _text("background.margin_line_color", "页边线颜色", control="color"),
            _range("background.line_gap_mm", "横线间距", 3, 20, 0.5, "mm"),
            _range("background.grid_size_mm", "方格尺寸", 3, 20, 0.5, "mm"),
            _range("background.dot_gap_mm", "点阵间距", 2, 15, 0.5, "mm"),
            _range("background.dot_radius_px", "点阵半径", 1, 6, 1, "px"),
            _toggle("background.draw_margin_line", "绘制页边线"),
            _toggle(
                "background.snap_ruled_lines",
                "对齐背景横线",
                "开启后行高与段落间距会量化成横线间距的整数倍，正文落在横线之间而不是压在线下。需先点“检测横线”。",
            ),
            _text(
                "background.ruled_lines",
                "横线位置",
                "由“检测横线”自动填写；也可以手动输入归一化 y 坐标（0-1），逗号分隔。",
                "ruled-lines",
            ),
            {
                "path": "background.section_images",
                "label": "分节背景",
                "control": "section-images",
                "help": "按标题给不同章节指定不同底图；命中的节会另起一页。没指定的节沿用上面的全局背景。",
            },
            _range(
                "background.section_level",
                "分节标题级别",
                1,
                3,
                1,
                "级",
                "按几级标题分节：1 表示一级标题，2/3 依次向下。",
            ),
            _range(
                "background.rule_tilt",
                "横线倾斜",
                -0.5,
                0.5,
                0.001,
                "px/px",
                "由“标定横线”自动写入：页面里每向右 1px 横线下降多少 px，0 表示水平；照片拍歪时正文会跟着斜线落笔。",
            ),
            _range(
                "background.rule_tilt_gradient",
                "坡度变化",
                -0.002,
                0.002,
                0.00001,
                "1/px",
                "透视翻拍专用：越往下，横线倾斜再多多少；由“标定横线”自动写入，0 表示整页一个坡度。",
            ),
            _range(
                "background.depth_blur",
                "景深模糊",
                0,
                1,
                0.01,
                "×",
                "只作用于背景照片：书写区域保持锐利，离纸面越远越虚，像浅景深镜头拍的。正文和纸面不会被模糊；0 表示关闭。",
            ),
            _range(
                "background.bokeh",
                "焦外光斑",
                0,
                1,
                0.01,
                "×",
                "从虚化区域的亮部生成圆形光斑，模拟镜头焦外的点光源；需要同时开一点景深模糊才自然。0 表示关闭。",
            ),
            _range(
                "background.vignette",
                "暗角",
                0,
                1,
                0.01,
                "×",
                "四周压暗，把视线收拢到纸面上。只作用于背景照片；0 表示关闭。",
            ),
        ],
    },
    {
        "id": "typography",
        "title": "字体、字号与字距",
        "eyebrow": "TYPE",
        "description": "调整字号、字符间距和间距变化，选择字体",
        "fields": [
            _text("handwriting.font_path", "正文字体", "SDT 中文不使用其轮廓；该字体负责排版度量，以及英文、数字和缺失符号的中心线形状。", "font-path"),
            _text("handwriting.glyph_library", "个人字形库", "由“我的字迹”抄写建库后自动填入；库里有这个字就用你写的字形，没有才回退到上面的字体。", "glyph-library"),
            _text("handwriting.math_font_path", "数学字体", "只决定数学符号的排版度量与中心线形状；留空时沿用正文字体。", "font-path"),
            _text("handwriting.fallback_font_path", "回退字体", "仅在首选字体缺少字符时补齐字形。", "font-path"),
            _range("handwriting.body_font_pt", "正文字号", 7, 32, 0.5, "pt", "控制正文字符边界框与排版尺寸，不改变统一基础笔宽。"),
            _range("handwriting.math_font_pt", "数学字号", 7, 36, 0.5, "pt", "控制公式主字符尺寸；上下标由公式布局按比例缩放，笔宽保持统一。"),
            _range("handwriting.code_font_pt", "代码字号", 6, 28, 0.5, "pt", "只控制代码字符尺寸。"),
            _range("handwriting.h1_font_pt", "一级标题", 12, 48, 0.5, "pt", "只放大字符结构，不额外加粗。"),
            _range("handwriting.h2_font_pt", "二级标题", 10, 40, 0.5, "pt", "只放大字符结构，不额外加粗。"),
            _range("handwriting.h3_font_pt", "三级标题", 9, 34, 0.5, "pt", "只放大字符结构，不额外加粗。"),
            _range("handwriting.line_spacing", "行距倍率", 1.0, 2.5, 0.01, "×", "只控制行框高度，不改变字符形状或笔画粗细。"),
            _range("handwriting.word_spacing_px", "字符间距", -12, 24, 0.5, "px", "在非空格字符的前进量上增减输出像素；负值更紧，正值更疏，不改变空格本身的宽度。"),
            _range("handwriting.advance_jitter_sigma_ratio", "字距随机扰动 σ", 0, 0.1, 0.001, "em", "实际标准差为当前字号乘以该比例；默认 0.01，设为 0 可关闭字符前进量的随机变化。"),
        ],
    },
    {
        "id": "sdt-trajectories",
        "title": "SDT · 轨迹渲染",
        "eyebrow": "STROKE",
        "description": "直接重绘在线笔迹，并统一正文、标题、公式与表格线的笔宽",
        "fields": [
            _toggle("handwriting.sdt_trajectory_enabled", "启用 SDT 轨迹引擎", "中文优先读取 SDT 轨迹；缺失符号自动提取字体中心线。"),
            _text("handwriting.sdt_trajectory_path", "轨迹库路径", "留空时使用项目内置的 6763 字轨迹包。"),
            _range("handwriting.sdt_stroke_width", "统一基础笔宽", 6, 22, 0.5, "SDT", "唯一的 SDT 基础粗细参数；按正文字号换算后，同样用于标题、公式字符、分数线和表格线。"),
            _range("handwriting.sdt_supersample", "抗锯齿采样", 1, 5, 1, "×", "只影响边缘质量和生成速度，不改变布局、形状和目标笔宽。"),
        ],
    },
    {
        "id": "first-layer",
        "title": "字形扰动",
        "eyebrow": "GLYPH",
        "description": "只改变字符和笔画的几何形状，不改变墨色与基础笔宽",
        "fields": [
            _toggle("handwriting.prefer_handright", "优先使用 Handright", "仅在关闭或无法加载 SDT 轨迹引擎时生效。"),
            _range("handwriting.glyph_slant_deg", "整体倾斜", -18, 18, 0.5, "°", "正值右倾、负值左倾；只倾斜字符本身，基线与字距保持不变。"),
            _range("handwriting.perturb_theta_sigma", "字符旋转标准差", 0, 0.06, 0.001, "rad", "所有引擎统一按弧度解释；默认 0.008 rad，约等于 0.46°。"),
            _nullable_range("handwriting.perturb_x_sigma_px", "字符水平偏移", 0, 12, 0.1, "px", "自动时统一使用当前字号的 1.5%。"),
            _nullable_range("handwriting.perturb_y_sigma_px", "正文字符垂直偏移", 0, 12, 0.1, "px", "只控制正文、标题、列表编号和页码字符；自动时使用当前字号的 1.5%。"),
            _range("handwriting.math_perturb_y_sigma_ratio", "公式字符垂直偏移 σ", 0, 0.10, 0.001, "em", "实际标准差等于当前公式字符字号乘以该比例；主字符、上下标及分式组件分别按各自字号计算，不移动分数线或根号线。"),
            _range("handwriting.sdt_coordinate_jitter", "笔画路径扰动", 0, 2.0, 0.05, "SDT", "应用于所有轨迹字符；SDT 汉字与字体中心线符号会先按弧长统一采样。"),
            _range("handwriting.sdt_jitter_correlation", "路径扰动平滑长度", 2, 30, 0.5, "采样点", "越大越平缓；统一采样后不再受原始轨迹点数影响。"),
            _range("handwriting.elastic_strength_ratio", "行内弹性形变", 0, 0.1, 0.001, "em", "栅格化后对整行文字或整组公式施加局部形变。"),
            _range("handwriting.elastic_smoothness_ratio", "弹性平滑尺度", 0.1, 2.0, 0.01, "em", "控制弹性位移场的变化尺度。"),
            _range("handwriting.baseline_wave_amplitude_ratio", "基线起伏", 0, 0.08, 0.001, "em", "统一作用于正文行和公式字符图块。"),
            _range("handwriting.baseline_wave_length_em", "基线波长", 2, 24, 0.5, "em", "越大时整行起伏越舒缓。"),
            _range("handwriting.math_rule_tilt_ratio", "生成线条倾斜", 0, 0.12, 0.001, "em", "只改变分数线、根号横线和表格线的方向。"),
            _range("handwriting.math_rule_wobble_ratio", "生成线条弯曲", 0, 0.05, 0.001, "em", "为公式线和表格线加入低频平缓弯曲。"),
            _range("handwriting.math_rule_jitter_ratio", "生成线条抖动", 0, 0.1, 0.001, "em", "为生成线条加入细小路径扰动；不改变墨色或基础笔宽。"),
            _text("handwriting.seed", "随机种子", "统一控制字形、笔压、墨色和公式线条；相同种子可复现。"),
        ],
    },
    {
        "id": "second-layer",
        "title": "墨迹扰动",
        "eyebrow": "INK",
        "description": "只改变笔压、整体粗细与墨色；同样作用于字符和生成线条",
        "fields": [
            _text("handwriting.ink_color", "统一墨水颜色", "正文、标题、公式字符和生成线条共用。", control="color"),
            _range("handwriting.sdt_width_jitter", "沿笔画笔压变化", 0, 0.35, 0.01, "", "统一作用于 SDT 汉字、中心线符号、公式线和表格线。"),
            _range("handwriting.sdt_taper", "起收笔渐细", 0, 0.6, 0.01, "", "控制每条轨迹及生成线条首尾的收尖程度。"),
            _toggle("handwriting.second_layer_enabled", "启用栅格墨迹后处理", "控制整体字重随机、墨色波动和飞白；关闭时仍保留上面的轨迹级笔压与收笔。"),
            _range("handwriting.stroke_weight_base_scale", "回退字体基础字重", 0.6, 1.6, 0.01, "×", "仅在 Handright/Pillow 回退模式下生效；SDT 模式只使用“统一基础笔宽”。"),
            _range("handwriting.stroke_weight_variation_sigma_ratio", "整体粗细扰动 σ", 0, 0.03, 0.0005, "em", "零均值正态分布的标准差；0 表示关闭。同一公式的字符、分数线、根号线与重音线共享一次连续粗细扰动。"),
            _range("handwriting.ink_density_jitter", "墨水浓淡波动", 0, 0.5, 0.01, "", "统一作用于字符墨迹、公式线与表格线。"),
            _range("handwriting.dry_brush_probability", "飞白概率", 0, 1, 0.01, "", "字符和生成线条使用同一概率。"),
            _range("handwriting.dry_brush_min_opacity", "飞白最低浓度", 0.1, 1, 0.01, "", "值越低，触发飞白时的墨迹越淡。"),
            _range("handwriting.scribble_probability", "涂写概率", 0, 1, 0.01, "", "每一行正文被随手涂写、划掉的概率；0 表示从不涂写。"),
        ],
    },
    {
        "id": "footer",
        "title": "页脚与页码",
        "eyebrow": "FOOTER",
        "description": "控制页面底部页码及其独立预留空间",
        "fields": [
            _toggle("layout.show_page_numbers", "显示底部页码", "关闭后不绘制页码，也不为页脚预留空间。"),
            _range("layout.footer_gap_mm", "页码区域高度", 0, 24, 0.5, "mm", "仅在显示底部页码时参与正文分页。"),
        ],
    },
    {
        "id": "layout",
        "title": "文档布局",
        "eyebrow": "LAYOUT",
        "description": "标题与插图编号、段落节奏、表格与图片留白",
        "fields": [
            _toggle("layout.number_sections", "自动编号章节"),
            _toggle("layout.number_figures", "自动编号插图", "按文档图片顺序从 1 编号，显示“图1：图片标题”；关闭时只显示图片标题。"),
            _toggle("layout.justify_paragraphs", "拉伸至右侧对齐", "正文自然折行且够满的非末行最多拉伸 18%；含公式或 <br> 的段落同样适用，显式换行和段尾不拉伸。"),
            _range("layout.paragraph_gap_mm", "段后间距", 0, 12, 0.1, "mm"),
            _range("layout.heading_gap_before_mm", "标题前间距", 0, 18, 0.5, "mm"),
            _range("layout.heading_gap_after_mm", "标题后间距", 0, 14, 0.5, "mm"),
            _range("layout.formula_gap_mm", "公式上下间距", 0, 14, 0.5, "mm"),
            _range("layout.table_gap_mm", "表格上下间距", 0, 16, 0.5, "mm"),
            _range("layout.table_cell_padding_mm", "单元格内边距", 0, 8, 0.25, "mm"),
            _range("layout.first_line_indent_em", "首行缩进", 0, 6, 0.25, "em"),
            _range("layout.list_indent_mm", "列表缩进", 0, 24, 0.5, "mm"),
            _range("layout.image_placeholder_height_mm", "图片占位高度", 10, 160, 1, "mm"),
            _toggle("layout.image_placeholder_border", "图片占位边框"),
        ],
    },
]


_DEFAULT_HANDWRITING = ReportConfig().handwriting


STYLE_PRESETS: dict[str, dict[str, Any]] = {
    "formal": {
        "handwriting.perturb_theta_sigma": 0.004,
        "handwriting.math_perturb_y_sigma_ratio": 0.008,
        "handwriting.sdt_coordinate_jitter": 0.35,
        "handwriting.sdt_jitter_correlation": 14.0,
        "handwriting.elastic_strength_ratio": 0.001,
        "handwriting.elastic_smoothness_ratio": 0.75,
        "handwriting.baseline_wave_amplitude_ratio": 0.008,
        "handwriting.baseline_wave_length_em": 16.0,
        "handwriting.math_rule_tilt_ratio": 0.022,
        "handwriting.math_rule_wobble_ratio": 0.005,
        "handwriting.math_rule_jitter_ratio": 0.025,
        "handwriting.sdt_width_jitter": 0.08,
        "handwriting.sdt_taper": 0.24,
        "handwriting.stroke_weight_variation_sigma_ratio": 0.0020,
        "handwriting.ink_density_jitter": 0.04,
        "handwriting.dry_brush_probability": 0.02,
        "handwriting.dry_brush_min_opacity": 0.86,
        "handwriting.scribble_probability": 0.0,
    },
    "natural": {
        "handwriting.perturb_theta_sigma": _DEFAULT_HANDWRITING.perturb_theta_sigma,
        "handwriting.math_perturb_y_sigma_ratio": _DEFAULT_HANDWRITING.math_perturb_y_sigma_ratio,
        "handwriting.sdt_coordinate_jitter": _DEFAULT_HANDWRITING.sdt_coordinate_jitter,
        "handwriting.sdt_jitter_correlation": _DEFAULT_HANDWRITING.sdt_jitter_correlation,
        "handwriting.elastic_strength_ratio": _DEFAULT_HANDWRITING.elastic_strength_ratio,
        "handwriting.elastic_smoothness_ratio": _DEFAULT_HANDWRITING.elastic_smoothness_ratio,
        "handwriting.baseline_wave_amplitude_ratio": _DEFAULT_HANDWRITING.baseline_wave_amplitude_ratio,
        "handwriting.baseline_wave_length_em": _DEFAULT_HANDWRITING.baseline_wave_length_em,
        "handwriting.math_rule_tilt_ratio": _DEFAULT_HANDWRITING.math_rule_tilt_ratio,
        "handwriting.math_rule_wobble_ratio": _DEFAULT_HANDWRITING.math_rule_wobble_ratio,
        "handwriting.math_rule_jitter_ratio": _DEFAULT_HANDWRITING.math_rule_jitter_ratio,
        "handwriting.sdt_width_jitter": _DEFAULT_HANDWRITING.sdt_width_jitter,
        "handwriting.sdt_taper": _DEFAULT_HANDWRITING.sdt_taper,
        "handwriting.stroke_weight_variation_sigma_ratio": _DEFAULT_HANDWRITING.stroke_weight_variation_sigma_ratio,
        "handwriting.ink_density_jitter": _DEFAULT_HANDWRITING.ink_density_jitter,
        "handwriting.dry_brush_probability": _DEFAULT_HANDWRITING.dry_brush_probability,
        "handwriting.dry_brush_min_opacity": _DEFAULT_HANDWRITING.dry_brush_min_opacity,
        "handwriting.scribble_probability": _DEFAULT_HANDWRITING.scribble_probability,
    },
    "casual": {
        "handwriting.perturb_theta_sigma": 0.014,
        "handwriting.math_perturb_y_sigma_ratio": 0.028,
        "handwriting.sdt_coordinate_jitter": 0.95,
        "handwriting.sdt_jitter_correlation": 7.0,
        "handwriting.elastic_strength_ratio": 0.006,
        "handwriting.elastic_smoothness_ratio": 0.40,
        "handwriting.baseline_wave_amplitude_ratio": 0.036,
        "handwriting.baseline_wave_length_em": 8.0,
        "handwriting.math_rule_tilt_ratio": 0.078,
        "handwriting.math_rule_wobble_ratio": 0.022,
        "handwriting.math_rule_jitter_ratio": 0.082,
        "handwriting.sdt_width_jitter": 0.24,
        "handwriting.sdt_taper": 0.38,
        "handwriting.stroke_weight_variation_sigma_ratio": 0.0055,
        "handwriting.ink_density_jitter": 0.16,
        "handwriting.dry_brush_probability": 0.20,
        "handwriting.dry_brush_min_opacity": 0.58,
        "handwriting.scribble_probability": 0.2,
    },
}


def _config_paths(value: Any, prefix: str = "") -> set[str]:
    paths: set[str] = set()
    if not is_dataclass(value):
        return paths
    for field_info in fields(value):
        field_value = getattr(value, field_info.name)
        path = f"{prefix}.{field_info.name}" if prefix else field_info.name
        if is_dataclass(field_value):
            paths.update(_config_paths(field_value, path))
        else:
            paths.add(path)
    return paths


def schema_paths() -> set[str]:
    return {item["path"] for section in CONFIG_SECTIONS for item in section["fields"]}


def _relative_to_project(path: Path) -> str:
    """Return a project-relative POSIX path, or the absolute one when outside."""
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _parse_line_points(points: Any) -> list[tuple[float, float]]:
    """校验标定窗口里点出来的四个归一化坐标。"""
    if not isinstance(points, list) or len(points) != 4:
        raise ValueError("横线标定需要四个点：两条相邻横线各点左右两端。")
    parsed: list[tuple[float, float]] = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError("横线标定的点格式不正确。")
        try:
            x, y = float(point[0]), float(point[1])
        except (TypeError, ValueError) as error:
            raise ValueError("横线标定的点必须是数字。") from error
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError("横线标定的点必须落在图片范围内。")
        parsed.append((x, y))
    return parsed


def detect_background_lines(configured_path: str, points: Any = None) -> dict[str, Any]:
    """Detect horizontal rules in the configured background image.

    带 ``points`` 时按用户点中的两条斜线识别整页每一条线的位置和坡度；不带时走平放的整行检测。
    """
    if not configured_path:
        raise ValueError("请先选择一张横线纸背景。")
    path = resolve_project_asset(configured_path)
    if path.suffix.lower() == ".pdf":
        raise ValueError("PDF 背景暂不支持检测横线，请换成图片。")
    try:
        with Image.open(path) as image:
            if points is None:
                result = detect_ruled_lines(image)
                return {
                    "count": len(result.positions),
                    "pitch": round(result.pitch, 5),
                    "confidence": round(result.confidence, 3),
                    "thickness": result.thickness,
                    "usable": result.usable,
                    "positions": [round(position, 5) for position in result.positions],
                }
            tilted = detect_tilted_lines(image, _parse_line_points(points))
    except OSError as error:
        raise ValueError("背景图无法读取。") from error
    return {
        "count": len(tilted.positions),
        "pitch": round(tilted.pitch, 5),
        "confidence": round(tilted.confidence, 3),
        "thickness": 0,
        "usable": tilted.usable,
        "positions": [round(position, 5) for position in tilted.positions],
        "tilt": round(tilted.tilt, 6),
        "tilt_gradient": round(tilted.tilt_gradient, 8),
    }


def rectify_background(configured_path: str, corners: Any) -> dict[str, Any]:
    """把斜拍的背景按四个角拉正，存成新素材并顺手检测横线。"""
    if not configured_path:
        raise ValueError("请先选择一张背景图。")
    if not isinstance(corners, list) or len(corners) != 4:
        raise ValueError("需要四个角点。")
    points: list[tuple[float, float]] = []
    for corner in corners:
        if not isinstance(corner, (list, tuple)) or len(corner) != 2:
            raise ValueError("角点格式不正确。")
        try:
            x, y = float(corner[0]), float(corner[1])
        except (TypeError, ValueError) as error:
            raise ValueError("角点必须是数字。") from error
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError("角点必须在图片范围内。")
        points.append((x, y))

    source = resolve_project_asset(configured_path)
    if source.suffix.lower() == ".pdf":
        raise ValueError("PDF 背景暂不支持摆正，请换成图片。")
    try:
        with Image.open(source) as raw:
            # 浏览器按 EXIF 方向显示照片，Pillow 不会；不先转正的话，用户点的角会错位。
            fixed, ordered = rectify(ImageOps.exif_transpose(raw).convert("RGB"), points)
    except OSError as error:
        raise ValueError("背景图无法读取。") from error

    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    target = UPLOAD_ROOT / f"{source.stem.removesuffix('-摆正')}-摆正.png"
    fixed.save(target)

    detected = detect_ruled_lines(fixed)
    return {
        "path": _relative_to_project(target),
        "size": [fixed.width, fixed.height],
        "corners": [[round(x, 4), round(y, 4)] for x, y in ordered],
        "lines": {
            "count": len(detected.positions),
            "pitch": round(detected.pitch, 5),
            "confidence": round(detected.confidence, 3),
            "usable": detected.usable,
            "positions": [round(position, 5) for position in detected.positions],
        },
    }


def available_glyph_libraries() -> list[dict[str, Any]]:
    """List built personal glyph libraries, newest first."""
    libraries: list[dict[str, Any]] = []
    if not GLYPH_ROOT.is_dir():
        return libraries
    for directory in GLYPH_ROOT.iterdir():
        index_path = directory / "index.json"
        if not directory.is_dir() or not index_path.is_file():
            continue
        try:
            library = GlyphLibrary.load(directory)
        except (OSError, ValueError):
            continue
        if library is None:
            continue
        libraries.append(
            {
                "label": library.index.get("name", directory.name),
                "value": _relative_to_project(directory),
                "count": len(library),
                "coverage": round(library.coverage(), 3),
                "builtAt": index_path.stat().st_mtime,
            }
        )
    libraries.sort(key=lambda item: item["builtAt"], reverse=True)
    return libraries


def ensure_template() -> dict:
    """Generate the copy sheet on first use and return its manifest."""
    manifest_path = TEMPLATE_ROOT / f"{TEMPLATE_NAME}.json"
    if manifest_path.is_file():
        return load_manifest(manifest_path)
    return build_template(TEMPLATE_ROOT, name=TEMPLATE_NAME, chars=COMMON_CHARS)


def build_glyph_library_from_uploads(data_urls: list[str], name: str) -> dict[str, Any]:
    """Turn photos of the filled copy sheet into a personal glyph library."""
    if not data_urls:
        raise ValueError("请先上传填好的抄写单。")
    if len(data_urls) > 4:
        raise ValueError("抄写单最多 4 页。")
    safe_name = _sanitize_stem(name) or "我的字迹"
    manifest = ensure_template()
    if len(data_urls) > len(manifest["pages"]):
        raise ValueError(f"这份抄写单只有 {len(manifest['pages'])} 页。")

    images = []
    for data_url in data_urls:
        raw, extension = decode_data_url(data_url)
        if extension == ".pdf":
            raise ValueError("抄写单请上传图片。")
        try:
            with Image.open(io.BytesIO(raw)) as image:
                images.append(image.convert("RGB"))
        except OSError as error:
            raise ValueError("抄写单图片无法读取，请换一张 JPG 或 PNG。") from error

    index = build_library(images, manifest, GLYPH_ROOT / safe_name, name=safe_name)
    if not index["chars"]:
        raise ValueError("没有从抄写单里切出字形，请确认拍的是这份模板、字写在格子里。")
    library = GlyphLibrary.load(GLYPH_ROOT / safe_name)
    return {
        "name": safe_name,
        "path": _relative_to_project(GLYPH_ROOT / safe_name),
        "count": len(index["chars"]),
        "missing": index["missing"][:20],
        "coverage": round(library.coverage() if library else 0.0, 3),
    }


def available_style_profiles() -> list[dict[str, Any]]:
    """List saved handwriting profiles, newest first."""
    profiles: list[dict[str, Any]] = []
    if not STYLE_ROOT.is_dir():
        return profiles
    for directory in STYLE_ROOT.iterdir():
        index_path = directory / "index.json"
        if not directory.is_dir() or not index_path.is_file():
            continue
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        profiles.append(
            {
                "label": index.get("name", directory.name),
                "value": _relative_to_project(directory),
                "style": index.get("style", {}),
                "metrics": index.get("metrics", {}),
                "glyphLibrary": index.get("glyphLibrary"),
                "hasSample": bool(index.get("sample")),
                "createdAt": index_path.stat().st_mtime,
            }
        )
    profiles.sort(key=lambda item: item["createdAt"], reverse=True)
    return profiles


def save_style_profile(payload: dict[str, Any]) -> dict[str, Any]:
    """Persist one sample photo plus its measured style as a reusable handwriting type."""
    raw_name = payload.get("name")
    name = _sanitize_stem(raw_name if isinstance(raw_name, str) else "") or "我的笔迹"
    suggested = payload.get("suggested")
    if not isinstance(suggested, dict) or not suggested:
        raise ValueError("没有可保存的风格参数，请先识别一张样本。")

    style: dict[str, Any] = {}
    for key, value in suggested.items():
        if key not in STYLE_KEY_WHITELIST:
            continue
        if isinstance(value, (int, float, str)):
            style[key] = value
    if not style:
        raise ValueError("风格参数为空，无法保存。")

    target = STYLE_ROOT / name
    target.mkdir(parents=True, exist_ok=True)

    sample_name = None
    data_url = payload.get("data")
    if isinstance(data_url, str) and data_url:
        raw, extension = decode_data_url(data_url)
        if extension == ".pdf":
            raise ValueError("样本请上传图片。")
        sample_name = f"sample{extension}"
        (target / sample_name).write_bytes(raw)
        for stale in target.glob("sample.*"):
            if stale.name != sample_name:
                stale.unlink()

    metrics = payload.get("metrics")
    index = {
        "name": name,
        "style": style,
        "metrics": metrics if isinstance(metrics, dict) else {},
        "sample": sample_name,
        "glyphLibrary": payload.get("glyphLibrary") or None,
    }
    (target / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "name": name,
        "path": _relative_to_project(target),
        "style": style,
        "hasSample": bool(sample_name),
    }


def available_assets() -> dict[str, list[dict[str, Any]]]:
    fonts = []
    for path in sorted((PROJECT_ROOT / "font").glob("*")):
        if path.is_file() and path.suffix.lower() in {".ttf", ".otf", ".ttc"}:
            fonts.append({"label": path.name, "value": path.relative_to(PROJECT_ROOT).as_posix()})

    backgrounds = []
    candidates = list((PROJECT_ROOT / "background").glob("*"))
    if UPLOAD_ROOT.is_dir():
        candidates.extend(UPLOAD_ROOT.glob("*"))
    candidates.extend(path for path in PROJECT_ROOT.iterdir() if path.is_file())
    for path in sorted(set(candidates)):
        if path.suffix.lower() in BACKGROUND_EXTENSIONS:
            backgrounds.append({"label": path.name, "value": path.relative_to(PROJECT_ROOT).as_posix()})
    return {
        "fonts": fonts,
        "backgrounds": backgrounds,
        "glyphLibraries": available_glyph_libraries(),
        "styleProfiles": available_style_profiles(),
    }


def resolve_project_asset(configured: str) -> Path:
    """Resolve a user-supplied asset path, restricted to the project directory."""
    raw = (configured or "").strip()
    if not raw:
        raise FileNotFoundError("未指定背景文件。")
    path = Path(raw)
    candidates = [path] if path.is_absolute() else [PROJECT_ROOT / path, Path.cwd() / path]
    root = PROJECT_ROOT.resolve()
    for candidate in candidates:
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as error:
            raise ValueError("背景文件必须位于项目目录内。") from error
        return resolved
    raise FileNotFoundError(f"找不到背景文件：{raw}")


def _sanitize_stem(name: str) -> str:
    stem = Path(name or "").stem.strip()
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", stem)
    stem = stem.strip(". ")[:48]
    return stem or "background"


def _unique_upload_path(stem: str, extension: str) -> Path:
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    candidate = UPLOAD_ROOT / f"{stem}{extension}"
    index = 2
    while candidate.exists():
        candidate = UPLOAD_ROOT / f"{stem}-{index}{extension}"
        index += 1
    return candidate


def decode_data_url(data_url: str, allow_pdf: bool = False) -> tuple[bytes, str]:
    """Decode a base64 data URL into raw bytes plus a file extension."""
    if not isinstance(data_url, str) or not data_url.startswith("data:"):
        raise ValueError("图片数据格式不正确。")
    header, _, payload = data_url.partition(",")
    meta = header[len("data:") :]
    if ";base64" not in meta.lower():
        raise ValueError("图片数据必须使用 base64 编码。")
    if not payload:
        raise ValueError("图片内容为空。")
    mime = meta.split(";", 1)[0].strip().lower()
    extension = UPLOAD_MIME_EXTENSIONS.get(mime)
    if extension is None or (extension == ".pdf" and not allow_pdf):
        allowed = "PNG、JPEG、WebP、BMP 图片或 PDF" if allow_pdf else "PNG、JPEG、WebP、BMP 图片"
        raise ValueError(f"仅支持{allowed}。")
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("图片数据无法解码，请重新选择文件。") from error
    if not raw:
        raise ValueError("图片内容为空。")
    if len(raw) > MAX_BACKGROUND_BYTES:
        raise ValueError("图片超过 16 MB，请先压缩后再上传。")
    return raw, extension


def save_uploaded_background(filename: str, data_url: str) -> dict[str, str]:
    """Persist a data-URL encoded background image and return its asset entry."""
    raw, extension = decode_data_url(data_url, allow_pdf=True)
    if extension == ".pdf" and not raw.lstrip().startswith(b"%PDF"):
        raise ValueError("PDF 背景文件内容不正确。")

    target = _unique_upload_path(_sanitize_stem(filename), extension)
    target.write_bytes(raw)
    relative = target.relative_to(PROJECT_ROOT).as_posix()
    return {
        "path": relative,
        "label": Path(filename or target.name).name or target.name,
        "url": f"/api/background-preview?path={relative}",
    }


def analyze_handwriting_sample(data_url: str) -> dict[str, Any]:
    """Measure a handwriting sample and return metrics, suggestions and an overlay."""
    raw, extension = decode_data_url(data_url)
    if extension == ".pdf":
        raise ValueError("字迹样本请上传图片。")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            profile = analyze_handwriting(image)
    except OSError as error:
        raise ValueError("这张图片无法读取，请换一张 JPG 或 PNG 再试。") from error

    overlay = None
    if profile.overlay is not None:
        preview = profile.overlay
        if preview.width > OVERLAY_MAX_WIDTH:
            scale = OVERLAY_MAX_WIDTH / preview.width
            preview = preview.resize(
                (OVERLAY_MAX_WIDTH, max(1, round(preview.height * scale))),
                Image.Resampling.LANCZOS,
            )
        buffer = io.BytesIO()
        preview.save(buffer, "PNG", compress_level=6)
        overlay = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")

    return {
        "metrics": asdict(profile.metrics),
        "suggested": profile.suggested,
        "notes": profile.notes,
        "warnings": profile.warnings,
        "usable": profile.usable,
        "overlay": overlay,
    }


def background_preview_bytes(path: Path) -> tuple[bytes, str]:
    """Return a browser-friendly preview of a background file."""
    if path.suffix.lower() == ".pdf":
        if fitz is None:  # pragma: no cover - 取决于环境
            raise RuntimeError("预览 PDF 背景需要 PyMuPDF，请先安装后再试。")
        document = fitz.open(str(path))
        try:
            if document.page_count < 1:
                raise ValueError("PDF 背景没有任何页面。")
            page = document.load_page(0)
            width = max(1.0, float(page.rect.width))
            scale = max(0.2, min(2.0, BACKGROUND_PREVIEW_MAX_WIDTH / width))
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            return pixmap.tobytes("png"), "image/png"
        finally:
            document.close()
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return path.read_bytes(), content_type


def available_examples() -> list[dict[str, str]]:
    examples = []
    for path in sorted((PROJECT_ROOT / "examples").glob("*.md")):
        examples.append({"label": path.stem, "value": path.name})
    return examples


def heading_outline(markdown: str, level: int = 1) -> list[str]:
    """按指定标题级别列出文档大纲，供“分节背景”逐节指定底图。"""
    if not isinstance(markdown, str):
        raise ValueError("Markdown 内容格式不正确。")
    headings: list[str] = []
    for block in parse_markdown(markdown):
        if isinstance(block, HeadingBlock) and block.level == level:
            text = "".join(part.text for part in block.parts).strip()
            if text:
                headings.append(text)
    return headings


def build_bootstrap() -> dict[str, Any]:
    ensure_template()
    return {
        "config": asdict(ReportConfig()),
        "sections": CONFIG_SECTIONS,
        "presets": STYLE_PRESETS,
        "assets": available_assets(),
        "examples": available_examples(),
        "template": {"name": TEMPLATE_NAME, "charCount": len(set(COMMON_CHARS))},
    }


def validate_config(config: ReportConfig) -> None:
    validate_layout_options(config)
    if not 4.0 <= config.handwriting.sdt_stroke_width <= 32.0:
        raise ValueError("SDT 统一笔画粗细必须在 4 到 32 之间。")
    if not 1 <= config.handwriting.sdt_supersample <= 8:
        raise ValueError("SDT 抗锯齿采样必须在 1 到 8 之间。")
    if not 0.25 <= config.handwriting.stroke_weight_base_scale <= 3.0:
        raise ValueError("基础字重倍率必须在 0.25 到 3.0 之间。")
    if not 0.0 <= config.handwriting.stroke_weight_variation_sigma_ratio <= 0.1:
        raise ValueError("整体粗细扰动 σ 必须在 0 到 0.1 em 之间。")
    if not 0.0 <= config.handwriting.math_perturb_y_sigma_ratio <= 0.25:
        raise ValueError("公式字符垂直偏移 σ 必须在 0 到 0.25 em 之间。")
    if not 72 <= config.page.dpi <= 1200:
        raise ValueError("DPI 必须在 72 到 1200 之间。")
    if config.page.width_mm <= 0 or config.page.height_mm <= 0:
        raise ValueError("页面宽高必须大于 0。")
    if config.page.margin_left_mm + config.page.margin_right_mm >= config.page.width_mm:
        raise ValueError("左右边距之和不能超过页面宽度。")
    if config.page.margin_top_mm + config.page.margin_bottom_mm >= config.page.height_mm:
        raise ValueError("上下边距之和不能超过页面高度。")
    if config.background.style not in {"plain", "lined", "grid", "dot", "image"}:
        raise ValueError("不支持的背景样式。")
    if not isinstance(config.background.section_level, int) or not 1 <= config.background.section_level <= 3:
        raise ValueError("分节标题级别必须在 1 到 3 之间。")
    seen_sections: set[str] = set()
    for entry in config.background.section_images:
        if not isinstance(entry, dict):
            raise ValueError("分节背景格式不正确。")
        heading = str(entry.get("heading", "")).strip()
        image = str(entry.get("image", "")).strip()
        if not heading or not image:
            raise ValueError("分节背景必须同时包含标题和图片。")
        if heading in seen_sections:
            raise ValueError(f"分节背景重复指定了标题：{heading}")
        seen_sections.add(heading)
        if resolve_project_asset(image).suffix.lower() == ".pdf":
            raise ValueError("分节背景只支持图片，不支持 PDF。")
    for value in (
        config.background.paper_color,
        config.background.line_color,
        config.background.margin_line_color,
        config.handwriting.ink_color,
    ):
        ImageColor.getrgb(value)
    for path in schema_paths():
        section_name, field_name = path.split(".", 1)
        value = getattr(getattr(config, section_name), field_name)
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"参数 {path} 不是有限数值。")


class RenderBusyError(RuntimeError):
    pass


# 预览与下载共用同一份栅格：预览图按渲染分辨率原样保存，不再缩放压缩。
PREVIEW_MAX_DPI = 600
PREVIEW_MIN_DPI = 150
# 单页渲染时间预算：预览按上一轮实测速度自适应选 DPI，超预算就下次自动降档。
PREVIEW_PAGE_BUDGET_SECONDS = 5.0
# 渲染耗时随 DPI 的增长远慢于平方（SDT 轨迹计算是固定开销），用 1.2 次幂估算才不会误降清晰度。
PREVIEW_DPI_TIME_EXPONENT = 1.2
# 预览图编码：PNG 无损，保存结果与 PDF 内嵌栅格逐像素一致。
PREVIEW_IMAGE_SUFFIX = ".png"
PREVIEW_PNG_COMPRESS_LEVEL = 6


class RenderService:
    def __init__(self, output_root: Path = OUTPUT_ROOT):
        self.output_root = output_root
        self.lock = threading.Lock()
        self.speed_probe: tuple[int, float] | None = None

    def _choose_preview_dpi(self, requested: int) -> int:
        """按用户设定的 DPI 渲染；只有在上一轮实测超预算时才降档。"""
        target = max(1, min(int(requested), PREVIEW_MAX_DPI))
        if self.speed_probe is None:
            return target
        probe_dpi, seconds_per_page = self.speed_probe
        if probe_dpi <= 0 or seconds_per_page <= 0:
            return target
        # 按实测速度估算这一档要花多久，超预算才降档。
        estimated = seconds_per_page * (target / probe_dpi) ** PREVIEW_DPI_TIME_EXPONENT
        if estimated <= PREVIEW_PAGE_BUDGET_SECONDS:
            return target
        predicted = probe_dpi * (PREVIEW_PAGE_BUDGET_SECONDS / seconds_per_page) ** (
            1 / PREVIEW_DPI_TIME_EXPONENT
        )
        return int(max(PREVIEW_MIN_DPI, min(target, predicted)))

    def render(self, markdown: str, config_data: dict[str, Any], mode: str = "preview") -> dict[str, Any]:
        if mode not in {"preview", "full"}:
            mode = "preview"
        if not self.lock.acquire(blocking=False):
            raise RenderBusyError("已有文档正在生成，请稍候。")
        try:
            config = config_from_dict(config_data)
            if mode == "preview":
                # 只下调、不上调：用户手动设得更低时尊重用户设置。
                config.page.dpi = self._choose_preview_dpi(config.page.dpi)
            validate_config(config)
            blocks = parse_markdown(markdown)
            renderer = ReportRenderer(config, base_dir=PROJECT_ROOT)
            started = time.perf_counter()
            pages = renderer.render(blocks)
            seconds_per_page = (time.perf_counter() - started) / max(1, len(pages))
            self.speed_probe = (config.page.dpi, seconds_per_page)

            job_id = secrets.token_hex(8)
            job_root = self.output_root / job_id
            job_root.mkdir(parents=True, exist_ok=False)
            # PDF 与预览图取自同一批栅格，因此下载到的文件与屏幕所见完全一致。
            pdf_path = renderer.save_pdf(job_root / "markdown-to-handwrite.pdf")
            preview_urls = []
            preview_size = (0, 0)
            for index, page in enumerate(pages, start=1):
                preview = page.convert("RGB")
                preview_name = f"page-{index}{PREVIEW_IMAGE_SUFFIX}"
                preview.save(
                    job_root / preview_name,
                    "PNG",
                    compress_level=PREVIEW_PNG_COMPRESS_LEVEL,
                )
                preview_urls.append(f"/output/{job_id}/{preview_name}")
                preview_size = preview.size
            return {
                "jobId": job_id,
                "mode": mode,
                "dpi": config.page.dpi,
                "supersample": config.handwriting.sdt_supersample,
                "secondsPerPage": round(seconds_per_page, 2),
                "pageCount": len(pages),
                "pages": preview_urls,
                "pixelWidth": preview_size[0],
                "pixelHeight": preview_size[1],
                "pdf": f"/output/{job_id}/{pdf_path.name}",
            }
        finally:
            self.lock.release()


def _safe_child(root: Path, relative: str) -> Path | None:
    try:
        candidate = (root / relative).resolve()
        candidate.relative_to(root.resolve())
        return candidate
    except (OSError, ValueError):
        return None


class WebApp:
    def __init__(self, output_root: Path = OUTPUT_ROOT):
        self.render_service = RenderService(output_root)

    def handler_class(self) -> type[BaseHTTPRequestHandler]:
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "HandwrittenReport/0.2"

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = unquote(parsed.path)
                if path == "/api/health":
                    self._send_json({"status": "ok"})
                elif path == "/api/bootstrap":
                    self._send_json(build_bootstrap())
                elif path == "/api/background-preview":
                    query = parse_qs(parsed.query)
                    self._serve_background_preview((query.get("path") or [""])[0])
                elif path.startswith("/api/glyph-template/"):
                    self._serve_template(path.removeprefix("/api/glyph-template/"))
                elif path.startswith("/api/examples/"):
                    self._serve_example(path.removeprefix("/api/examples/"))
                elif path.startswith("/output/"):
                    self._serve_output(path.removeprefix("/output/"))
                else:
                    self._serve_static("index.html" if path in {"/", ""} else path.lstrip("/"))

            def do_POST(self) -> None:  # noqa: N802
                route = urlparse(self.path).path
                if route == "/api/upload-background":
                    self._handle_background_upload()
                    return
                if route == "/api/analyze-handwriting":
                    self._handle_style_analysis()
                    return
                if route == "/api/build-glyphs":
                    self._handle_build_glyphs()
                    return
                if route == "/api/save-style":
                    self._handle_save_style()
                    return
                if route == "/api/detect-lines":
                    self._handle_detect_lines()
                    return
                if route == "/api/rectify-background":
                    self._handle_rectify()
                    return
                if route == "/api/outline":
                    self._handle_outline()
                    return
                if route != "/api/render":
                    self._send_json({"error": "接口不存在。"}, HTTPStatus.NOT_FOUND)
                    return
                try:
                    payload = self._read_json()
                    markdown = payload.get("markdown", "")
                    if not isinstance(markdown, str) or not markdown.strip():
                        raise ValueError("请输入要生成的 Markdown 内容。")
                    mode = "full" if payload.get("mode") == "full" else "preview"
                    result = app.render_service.render(markdown, payload.get("config", {}), mode=mode)
                    self._send_json(result)
                except RenderBusyError as error:
                    self._send_json({"error": str(error)}, HTTPStatus.CONFLICT)
                except (TypeError, ValueError, KeyError, OSError) as error:
                    self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                except Exception as error:  # pragma: no cover - final HTTP boundary
                    print(f"[web-render-error] {error}")
                    self._send_json({"error": "生成失败，请检查内容、字体和背景配置。"}, HTTPStatus.INTERNAL_SERVER_ERROR)

            def _handle_background_upload(self) -> None:
                try:
                    payload = self._read_json(MAX_UPLOAD_REQUEST_BYTES)
                    filename = payload.get("filename", "")
                    data = payload.get("data", "")
                    if not isinstance(filename, str) or not isinstance(data, str):
                        raise ValueError("上传内容格式不正确。")
                    self._send_json({"asset": save_uploaded_background(filename, data)})
                except (TypeError, ValueError, OSError) as error:
                    self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

            def _handle_rectify(self) -> None:
                try:
                    payload = self._read_json(MAX_REQUEST_BYTES)
                    path = payload.get("path", "")
                    if not isinstance(path, str):
                        raise ValueError("背景路径格式不正确。")
                    result = rectify_background(path, payload.get("corners"))
                    self._send_json({"rectified": result, "backgrounds": available_assets()["backgrounds"]})
                except RectifyError as error:
                    self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                except (TypeError, ValueError, OSError) as error:
                    self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

            def _handle_detect_lines(self) -> None:
                try:
                    payload = self._read_json(MAX_REQUEST_BYTES)
                    path = payload.get("path", "")
                    if not isinstance(path, str):
                        raise ValueError("背景路径格式不正确。")
                    self._send_json(detect_background_lines(path, payload.get("points")))
                except (TypeError, ValueError, OSError) as error:
                    self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

            def _handle_outline(self) -> None:
                try:
                    payload = self._read_json()
                    markdown = payload.get("markdown", "")
                    level = payload.get("level", 1)
                    if isinstance(level, bool) or not isinstance(level, int) or not 1 <= level <= 3:
                        raise ValueError("分节标题级别必须在 1 到 3 之间。")
                    self._send_json({"headings": heading_outline(markdown, level)})
                except (TypeError, ValueError, OSError) as error:
                    self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

            def _handle_save_style(self) -> None:
                try:
                    payload = self._read_json(MAX_UPLOAD_REQUEST_BYTES)
                    result = save_style_profile(payload)
                    self._send_json({"profile": result, "profiles": available_style_profiles()})
                except (TypeError, ValueError, OSError) as error:
                    self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

            def _handle_build_glyphs(self) -> None:
                try:
                    payload = self._read_json(MAX_UPLOAD_REQUEST_BYTES)
                    pages = payload.get("pages", [])
                    if not isinstance(pages, list) or not pages or not all(isinstance(page, str) for page in pages):
                        raise ValueError("请上传填好的抄写单照片。")
                    name = payload.get("name")
                    result = build_glyph_library_from_uploads(pages, name if isinstance(name, str) else "我的字迹")
                    self._send_json({"library": result, "libraries": available_glyph_libraries()})
                except (TypeError, ValueError, OSError) as error:
                    self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

            def _serve_template(self, name: str) -> None:
                target = TEMPLATE_ROOT / Path(name).name
                if not target.is_file():
                    self._send_json({"error": "模板文件不存在。"}, HTTPStatus.NOT_FOUND)
                    return
                self._send_bytes(target.read_bytes(), "image/png")

            def _handle_style_analysis(self) -> None:
                try:
                    payload = self._read_json(MAX_UPLOAD_REQUEST_BYTES)
                    data = payload.get("data", "")
                    if not isinstance(data, str):
                        raise ValueError("上传内容格式不正确。")
                    self._send_json(analyze_handwriting_sample(data))
                except (TypeError, ValueError, OSError) as error:
                    self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

            def _serve_background_preview(self, configured: str) -> None:
                try:
                    path = resolve_project_asset(configured)
                except (FileNotFoundError, ValueError) as error:
                    self._send_json({"error": str(error)}, HTTPStatus.NOT_FOUND)
                    return
                try:
                    body, content_type = background_preview_bytes(path)
                except (ValueError, RuntimeError) as error:
                    self._send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                    return
                self._send_bytes(body, content_type)

            def _read_json(self, max_bytes: int = MAX_REQUEST_BYTES) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > max_bytes:
                    limit_mb = max(1, max_bytes // (1024 * 1024))
                    raise ValueError(f"请求内容为空或超过 {limit_mb} MB。")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("请求必须是 JSON 对象。")
                return payload

            def _serve_example(self, name: str) -> None:
                path = _safe_child(PROJECT_ROOT / "examples", name)
                if path is None or path.suffix.lower() != ".md" or not path.is_file():
                    self._send_json({"error": "示例不存在。"}, HTTPStatus.NOT_FOUND)
                    return
                self._send_bytes(path.read_bytes(), "text/markdown; charset=utf-8")

            def _serve_output(self, relative: str) -> None:
                path = _safe_child(app.render_service.output_root, relative)
                if path is None or not path.is_file():
                    self._send_json({"error": "文件不存在。"}, HTTPStatus.NOT_FOUND)
                    return
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                headers = {}
                if path.suffix.lower() == ".pdf":
                    headers["Content-Disposition"] = 'attachment; filename="markdown-to-handwrite.pdf"'
                self._send_bytes(path.read_bytes(), content_type, headers)

            def _serve_static(self, relative: str) -> None:
                path = _safe_child(STATIC_ROOT, relative)
                if path is None or not path.is_file():
                    self._send_json({"error": "页面不存在。"}, HTTPStatus.NOT_FOUND)
                    return
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                self._send_bytes(path.read_bytes(), content_type)

            def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._send_bytes(body, "application/json; charset=utf-8", status=status)

            def _send_bytes(
                self,
                body: bytes,
                content_type: str,
                headers: dict[str, str] | None = None,
                status: HTTPStatus = HTTPStatus.OK,
            ) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format_string: str, *args: object) -> None:
                print(f"[web] {self.address_string()} - {format_string % args}")

        return Handler


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Launch the MarkdownToHandwrite WebUI.")
    parser.add_argument("--host", default="127.0.0.1", help="Address to bind (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8765, help="Port to bind (default: 8765).")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the UI in the default browser.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    app = WebApp()
    server = ThreadingHTTPServer((args.host, args.port), app.handler_class())
    url = f"http://{args.host}:{server.server_port}"
    print(f"MarkdownToHandwrite WebUI: {url}")
    if not args.no_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nWebUI stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
