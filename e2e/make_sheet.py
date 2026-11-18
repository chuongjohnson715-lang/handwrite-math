"""生成"已填写的抄写单"，供浏览器测试上传建库。

用项目自带的行书字体模拟另一个人的笔迹，这样字形库与内置字体的差异肉眼可见。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from markdown_to_handwrite.glyph_template import COMMON_CHARS, build_template  # noqa: E402

FONT = ROOT / "font" / "AnJingChenYingBiXingShu-2.ttf"
INK = (18, 24, 46)


def main() -> None:
    template_dir = ROOT / "output" / "glyph-template"
    manifest_path = template_dir / "common-300.json"
    if not manifest_path.is_file():
        build_template(template_dir, name="common-300", chars=COMMON_CHARS)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    page_path = template_dir / manifest["pages"][0]["file"]
    page = Image.open(page_path).convert("RGB")
    filled = page.copy()
    draw = ImageDraw.Draw(filled)
    cells = manifest["pages"][0]["cells"]
    font = ImageFont.truetype(str(FONT), int(cells[0]["w"] * filled.width * 0.72))
    width, height = filled.size
    for cell in cells:
        centre = ((cell["x"] + cell["w"] / 2) * width, (cell["y"] + cell["h"] / 2) * height)
        draw.text(centre, cell["char"], fill=INK, font=font, anchor="mm")

    target = ROOT / "e2e" / "fixtures" / "filled-sheet.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    filled.save(target)
    print(f"已生成填写样本：{target}")


if __name__ == "__main__":
    main()
