"""生成"抄写模板"：格子里印浅灰提示字，用户照着写一遍，就能建个人字形库。

为什么要有模板：从自由书写的照片里切字，既切不干净（粘连、横线、批注），
也没法知道切出来的块是哪个字——那需要 OCR。模板把"哪个字、在哪个格子"
提前定死，切分与对齐都变成查表，零猜测。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 常用 300 字：覆盖日常文本约 70%，抄写量控制在两页内。
COMMON_CHARS = (
    "的一是了我不人在他有这为之大来以个中上们到说国和地也子时道出而要于就下得可你年生"
    "自会那后能对着事其里所去行过家十用发天如然作方成者多日都三小军二无同么经法当起与"
    "好看学进种将还分此心前面又定见只主没公从想意把被给但很知间样美点情什两新内因些实"
    "比方等问力它件长手最关重体相题活教几提解名真干次处条化气五第位造入先身思完报山各"
    "话深取观水色马向头全明老门改机系边做四展直眼社求队元带合建才已通并原东立及快速"
)

TEMPLATE_DPI = 300
CELL_MM = 14.0
MARGIN_MM = 12.0
HEADER_MM = 12.0
HINT_COLOR = (214, 214, 214)
GRID_COLOR = (196, 196, 196)


@dataclass
class TemplatePage:
    image: Image.Image
    cells: list[dict]


def _font(size_px: int, path: str | None = None) -> ImageFont.FreeTypeFont:
    candidates = [
        path,
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/STKAITI.TTF",
        "C:/Windows/Fonts/msyh.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size_px)
            except OSError:
                continue
    return ImageFont.load_default()


def _layout(chars: list[str], dpi: int) -> tuple[int, int, int, int, int, int, int, int]:
    mm = dpi / 25.4
    page_w = round(210 * mm)
    page_h = round(297 * mm)
    margin = round(MARGIN_MM * mm)
    header = round(HEADER_MM * mm)
    cell = round(CELL_MM * mm)
    cols = max(1, (page_w - 2 * margin) // cell)
    rows = max(1, (page_h - 2 * margin - header) // cell)
    return page_w, page_h, margin, header, cell, cols, rows, mm


def render_template(chars: list[str], dpi: int = TEMPLATE_DPI) -> list[TemplatePage]:
    """Render the copy sheet; cells carry normalised coordinates for later slicing."""
    page_w, page_h, margin, header, cell, cols, rows, mm = _layout(chars, dpi)
    per_page = cols * rows
    hint_font = _font(round(cell * 0.62))
    header_font = _font(round(4.6 * mm))
    pages: list[TemplatePage] = []

    for page_index in range(0, max(1, (len(chars) + per_page - 1) // per_page)):
        image = Image.new("RGB", (page_w, page_h), "white")
        draw = ImageDraw.Draw(image)
        top = margin + header
        draw.text(
            (margin, margin),
            "请用平时写字的笔，一个字写一格，写在格子里即可（不用描满浅灰字）",
            fill=(120, 120, 120),
            font=header_font,
        )
        cells: list[dict] = []
        for slot in range(per_page):
            index = page_index * per_page + slot
            if index >= len(chars):
                break
            col = slot % cols
            row = slot // cols
            x0 = margin + col * cell
            y0 = top + row * cell
            draw.rectangle((x0, y0, x0 + cell, y0 + cell), outline=GRID_COLOR, width=1)
            char = chars[index]
            # 提示字压在格子中央偏下，用户直接覆盖写；颜色很浅，二值化时能滤掉。
            draw.text(
                (x0 + cell / 2, y0 + cell / 2),
                char,
                fill=HINT_COLOR,
                font=hint_font,
                anchor="mm",
            )
            cells.append(
                {
                    "char": char,
                    "x": round(x0 / page_w, 5),
                    "y": round(y0 / page_h, 5),
                    "w": round(cell / page_w, 5),
                    "h": round(cell / page_h, 5),
                }
            )
        pages.append(TemplatePage(image=image, cells=cells))
    return pages


def build_template(
    output_dir: str | Path,
    name: str = "common-300",
    chars: str | list[str] | None = None,
    dpi: int = TEMPLATE_DPI,
) -> dict:
    """Write the template pages as PNG plus a manifest, and return the manifest."""
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    unique: list[str] = []
    seen: set[str] = set()
    for char in chars if chars is not None else COMMON_CHARS:
        if char in seen or not char.strip():
            continue
        seen.add(char)
        unique.append(char)

    pages = render_template(unique, dpi=dpi)
    files: list[str] = []
    for index, page in enumerate(pages, start=1):
        path = target / f"{name}-{index}.png"
        page.image.save(path)
        files.append(path.name)

    manifest = {
        "name": name,
        "dpi": dpi,
        "pageSize": [pages[0].image.width, pages[0].image.height],
        "pages": [{"file": files[i], "cells": page.cells} for i, page in enumerate(pages)],
        "charCount": len(unique),
    }
    (target / f"{name}.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def load_manifest(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))
