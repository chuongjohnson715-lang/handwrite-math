"""生成一张桌面场景样张：纸面有细横线纹理，四周桌面有噪点纹理和几个点光源。

用来验证背景光影（景深 / 光斑 / 暗角）：纸面必须保持锐利，桌面必须糊掉、亮出光斑。
纸面只占中间一块，四周留出足够桌面，光斑才有地方出现。
"""

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

FIXTURES = Path(__file__).parent / "fixtures"


def main() -> None:
    width, height = 1000, 1400
    rng = np.random.default_rng(20261029)
    base = np.full((height, width, 3), (78, 84, 92), dtype=np.float32)
    base += rng.normal(0, 16, base.shape)
    for y in range(0, height, 13):
        base[y : y + 2, :, :] *= 0.7
    yy, xx = np.mgrid[0:height, 0:width]
    for cx, cy in ((100, 120), (900, 180), (120, 1300), (880, 1250), (500, 80)):
        base += np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * 10.0**2)))[..., None] * 200
    scene = Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))

    paper_w, paper_h = 600, 980
    paper = Image.fromarray(
        np.clip(243 + rng.normal(0, 4, (paper_h, paper_w, 3)), 0, 255).astype(np.uint8)
    )
    draw = ImageDraw.Draw(paper)
    for y in range(14, paper_h, 9):
        draw.line((0, y, paper_w, y), fill=(222, 226, 233), width=1)
    scene.paste(paper, (200, 210))

    target = FIXTURES / "desk-scene.png"
    scene.save(target)
    print(f"saved {target} {scene.size}")


if __name__ == "__main__":
    main()