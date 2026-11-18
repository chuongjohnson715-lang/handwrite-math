"""生成一张透视翻拍样张：越往下线距越大、横线越陡。

同时写出标定用的几何参数，浏览器脚本据此在两条相邻线上点四个端点，再拿真实线位验算正文。
"""

import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

FIXTURES = Path(__file__).parent / "fixtures"
WIDTH, HEIGHT = 1000, 1414
FIRST = 300.0
PITCH = 40.0
GROWTH = 0.5
SLOPE = -0.02
SLOPE_GROWTH = -7e-5


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)

    lines = []
    y, step = FIRST, PITCH
    while y < HEIGHT - 40:
        lines.append({"y": round(y, 2), "slope": round(SLOPE + SLOPE_GROWTH * (y - HEIGHT / 2), 6)})
        y += step
        step += GROWTH

    rng = np.random.default_rng(20261029)
    for index in range(len(lines) - 1):
        top, bottom = lines[index]["y"], lines[index + 1]["y"]
        for _ in range(8):
            x = float(rng.uniform(30, WIDTH - 80))
            length = float(rng.uniform(10, 24))
            gy = float(rng.uniform(top + 5, bottom - 5))
            draw.line((x, gy, x + length, gy + rng.uniform(-1, 1)), fill=(205, 205, 208), width=1)

    for line in lines:
        center, slope = line["y"], line["slope"]
        draw.line(
            (0, center - slope * WIDTH / 2, WIDTH, center + slope * WIDTH / 2),
            fill=(150, 175, 210),
            width=3,
        )

    image.save(FIXTURES / "perspective-lines.png")
    middle = len(lines) // 2
    (FIXTURES / "perspective-lines.json").write_text(
        json.dumps(
            {
                "width": WIDTH,
                "height": HEIGHT,
                "tilt": SLOPE,
                "tilt_gradient": SLOPE_GROWTH,
                "lines": lines,
                "anchors": [middle, middle + 1],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"saved {FIXTURES / 'perspective-lines.png'} {image.size}，{len(lines)} 条线")


if __name__ == "__main__":
    main()
