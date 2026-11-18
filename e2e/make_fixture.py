"""生成浏览器测试用的字迹样本：固定倾斜、笔宽与墨色，便于断言识别结果。"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from markdown_to_handwrite.config import ReportConfig, pt_to_px  # noqa: E402
from markdown_to_handwrite.handwriting import HandwritingEngine  # noqa: E402

LINES = [
    "春眠不觉晓处处闻啼鸟",
    "夜来风雨声花落知多少",
    "床前明月光疑是地上霜",
    "举头望明月低头思故乡",
]
# 这几个值就是浏览器测试期望被识别出来的参数。
STROKE_WIDTH = 19.0
SLANT_DEG = 9.0
LINE_SPACING = 1.75
INK_COLOR = "#20304d"


def build_sample() -> Image.Image:
    size = pt_to_px(16, 220)
    config = ReportConfig().handwriting
    config.sdt_stroke_width = STROKE_WIDTH
    config.glyph_slant_deg = SLANT_DEG
    config.line_spacing = LINE_SPACING
    config.ink_color = INK_COLOR
    engine = HandwritingEngine(config, reference_size_px=size, math_reference_size_px=size)
    row_height = round(size * LINE_SPACING)
    rendered = [
        engine.render_line(text, size, 100000, seed_extra=f"fixture-{index}", return_baseline=True)
        for index, text in enumerate(LINES)
    ]
    width = max(image.width for image, _ in rendered) + 120
    canvas = Image.new("RGB", (width, row_height * len(rendered) + 120), "white")
    for index, (image, baseline) in enumerate(rendered):
        canvas.paste(image, (60, 60 + index * row_height - baseline), image)
    return canvas


def main() -> None:
    target = ROOT / "e2e" / "fixtures" / "handwriting-sample.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    build_sample().save(target)
    print(f"字迹样本已生成：{target}")


if __name__ == "__main__":
    main()
