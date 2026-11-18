"""从抄写模板的照片里建个人字形库。

流程：模板 manifest 给出每个字的格子坐标 → 照片按页面尺寸归一化 → 逐格裁图 →
只保留比浅灰提示字更深的笔画 → 存成统一尺寸的 RGBA 字形。

这样既不需要 OCR，也不依赖字符切分算法：字和位置是模板定死的。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

# 字形统一存成这个边长，渲染时再按字号缩放。
CELL_SIZE = 128
# 比提示字（浅灰 214）明显更深的像素才算用户写的笔画。
INK_THRESHOLD = 175
# 裁格子时向外扩一点，容纳压线写的笔画。
CELL_PADDING = 0.14


def _otsu(values: np.ndarray) -> float:
    histogram, _ = np.histogram(values, bins=256, range=(0.0, 256.0))
    total = histogram.sum()
    if total <= 0:
        return 128.0
    probability = histogram.astype(np.float64) / total
    omega = np.cumsum(probability)
    mu = np.cumsum(probability * np.arange(256, dtype=np.float64))
    denominator = omega * (1.0 - omega)
    denominator[denominator <= 1e-9] = 1e-9
    variance = (mu[-1] * omega - mu) ** 2 / denominator
    return float(np.argmax(variance))


@dataclass
class Glyph:
    char: str
    image: Image.Image  # RGBA，CELL_SIZE 见方
    coverage: float  # 墨迹像素占比，用来判断这一格是不是空的


def _page_to_canvas(image: Image.Image, page_size: tuple[int, int]) -> Image.Image:
    """把照片对齐到模板页面尺寸。

    只做整页缩放，不做透视校正：模板要求正拍，斜拍会让格子错位，
    与其猜一个透视变换，不如让用户重拍。
    """
    target_w, target_h = page_size
    return image.convert("RGB").resize((target_w, target_h), Image.Resampling.LANCZOS)


def _ink_alpha(cell: Image.Image) -> tuple[np.ndarray, tuple[int, int, int]]:
    """Return the alpha mask of the user's strokes plus their ink colour."""
    gray = np.asarray(cell.convert("L"), dtype=np.float32)
    threshold = min(float(INK_THRESHOLD), _otsu(gray) + 12.0)
    mask = gray < threshold
    # 去掉格子边框：它们正好压在裁切边缘上。
    border = max(2, int(min(cell.size) * 0.04))
    mask[:border, :] = False
    mask[-border:, :] = False
    mask[:, :border] = False
    mask[:, -border:] = False
    if not mask.any():
        return mask, (0, 0, 0)
    rgb = np.asarray(cell.convert("RGB"), dtype=np.float32)[mask]
    luma = rgb.mean(axis=1)
    core = rgb[luma <= np.percentile(luma, 30)]
    ink = tuple(int(round(v)) for v in (core.mean(axis=0) if core.size else rgb.mean(axis=0)))
    return mask, ink  # type: ignore[return-value]


def extract_glyphs(image: Image.Image, manifest: dict, page_index: int) -> tuple[dict[str, Glyph], list[str]]:
    """Slice one template page into glyphs, keyed by character."""
    page_size = (manifest["pageSize"][0], manifest["pageSize"][1])
    canvas = _page_to_canvas(image, page_size)
    width, height = canvas.size
    cells = manifest["pages"][page_index]["cells"]
    glyphs: dict[str, Glyph] = {}
    missing: list[str] = []

    for cell in cells:
        pad_x = cell["w"] * CELL_PADDING
        pad_y = cell["h"] * CELL_PADDING
        box = (
            max(0, round((cell["x"] - pad_x) * width)),
            max(0, round((cell["y"] - pad_y) * height)),
            min(width, round((cell["x"] + cell["w"] + pad_x) * width)),
            min(height, round((cell["y"] + cell["h"] + pad_y) * height)),
        )
        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
            missing.append(cell["char"])
            continue
        crop = canvas.crop(box)
        mask, ink = _ink_alpha(crop)
        coverage = float(mask.sum()) / float(mask.size)
        # 覆盖率太低说明这一格没写，或写得极轻，不能当成字形。
        if coverage < 0.012:
            missing.append(cell["char"])
            continue
        alpha = Image.fromarray((mask * 255).astype(np.uint8), mode="L")
        # 轻微模糊让边缘不那么硬，缩放后更接近真实笔画。
        alpha = alpha.filter(ImageFilter.GaussianBlur(0.6))
        glyph = Image.new("RGBA", crop.size, (*ink, 0))
        glyph.putalpha(alpha)
        glyph = glyph.resize((CELL_SIZE, CELL_SIZE), Image.Resampling.LANCZOS)
        glyphs[cell["char"]] = Glyph(char=cell["char"], image=glyph, coverage=coverage)
    return glyphs, missing


def build_library(
    images: list[Image.Image],
    manifest: dict,
    output_dir: str | Path,
    name: str = "我的字迹",
) -> dict:
    """Build and persist a glyph library from template photos."""
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    collected: dict[str, Glyph] = {}
    missing: list[str] = []
    for index, image in enumerate(images):
        if index >= len(manifest["pages"]):
            break
        glyphs, gaps = extract_glyphs(image, manifest, index)
        collected.update(glyphs)
        missing.extend(gaps)

    entries: dict[str, dict] = {}
    heights: list[float] = []
    for char, glyph in collected.items():
        filename = f"U+{ord(char):04X}.png"
        glyph.image.save(target / filename)
        alpha = np.asarray(glyph.image.split()[-1], dtype=np.float32) / 255.0
        rows = np.flatnonzero(alpha.max(axis=1) > 0.15)
        ink_height = float(rows[-1] - rows[0] + 1) / CELL_SIZE if rows.size else 0.0
        if ink_height > 0:
            heights.append(ink_height)
        entries[char] = {"file": filename, "coverage": round(glyph.coverage, 4), "height": round(ink_height, 4)}

    # 字高占格子的比例，渲染时用它把字形缩放到目标字号。
    reference = float(np.median(heights)) if heights else 0.7
    index = {
        "name": name,
        "cellSize": CELL_SIZE,
        "referenceHeight": round(reference, 4),
        "chars": entries,
        "missing": sorted(set(missing)),
    }
    (target / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return index


class GlyphLibrary:
    """Loaded glyph library: character -> normalised RGBA image."""

    def __init__(self, directory: Path, index: dict):
        self.directory = directory
        self.index = index
        self.cell_size = int(index.get("cellSize", CELL_SIZE))
        self.reference_height = float(index.get("referenceHeight", 0.7))
        self._cache: dict[str, Image.Image] = {}

    @classmethod
    def load(cls, directory: str | Path) -> "GlyphLibrary | None":
        path = Path(directory)
        index_path = path / "index.json"
        if not index_path.is_file():
            return None
        return cls(path, json.loads(index_path.read_text(encoding="utf-8")))

    def __contains__(self, char: str) -> bool:
        return char in self.index.get("chars", {})

    def __len__(self) -> int:
        return len(self.index.get("chars", {}))

    def coverage(self) -> float:
        """Share of common Chinese characters this library can actually draw."""
        from .glyph_template import COMMON_CHARS

        if not self.index.get("chars"):
            return 0.0
        common = set(COMMON_CHARS)
        hits = sum(1 for char in common if char in self)
        return hits / max(1, len(common))

    def glyph(self, char: str) -> Image.Image | None:
        entry = self.index.get("chars", {}).get(char)
        if not entry:
            return None
        cached = self._cache.get(char)
        if cached is not None:
            return cached
        path = self.directory / entry["file"]
        if not path.is_file():
            return None
        image = Image.open(path).convert("RGBA")
        self._cache[char] = image
        return image

    def height_ratio(self, char: str) -> float:
        """Ink height of this glyph inside its cell, as a fraction of the cell."""
        entry = self.index.get("chars", {}).get(char)
        if not entry:
            return self.reference_height
        return float(entry.get("height") or self.reference_height)
