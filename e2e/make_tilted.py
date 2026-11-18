"""生成两张"拍歪"的横线纸样张：一张给摆正流程用，一张给手动标定横线用。"""

from pathlib import Path

from PIL import Image, ImageDraw

FIXTURES = Path(__file__).parent / "fixtures"


def sheet(name: str, slope: float, first: int, pitch: int, width: int, height: int) -> None:
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    for index in range(24):
        y = first + index * pitch
        if y + slope * width >= height:
            break
        draw.line((0, y, width, y + slope * width), fill=(150, 175, 210), width=3)
    target = FIXTURES / name
    image.save(target)
    print(f"saved {target} {image.size}")


sheet("tilted-lines.png", 0.018, 90, 55, 1000, 1414)
# 手动标定的样张要斜得明显：不跟随倾斜时，正文在左右两端会差 0.06 × 页宽。
sheet("skewed-lines.png", 0.06, 300, 55, 1000, 1414)
