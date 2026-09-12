"""Generate the ByteSqueeze worker icon without storing binary source assets."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def main(output: str) -> None:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGBA", (256, 256), "#090d12")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((14, 14, 242, 242), radius=54, fill="#111821", outline="#293640", width=5)
    draw.rounded_rectangle((38, 38, 218, 218), radius=43, fill="#46cede")
    font_path = Path("C:/Windows/Fonts/segoeuib.ttf")
    font = ImageFont.truetype(str(font_path), 112) if font_path.is_file() else ImageFont.load_default()
    draw.text((64, 45), "B", font=font, fill="#071014")
    for x in (148, 174, 200):
        draw.ellipse((x - 8, 190, x + 8, 206), fill="#071014")
    image.save(path, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: make_windows_icon.py OUTPUT.ico")
    main(sys.argv[1])
