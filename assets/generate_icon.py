"""Render the source icon into Windows ICO and tray PNG assets using Pillow."""
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parent
SIZE = 1024


def render() -> Image.Image:
    scale = SIZE / 256
    px = lambda value: round(value * scale)
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((px(8), px(8), px(248), px(248)), radius=px(58), fill="#171717")
    draw.rounded_rectangle((px(9), px(9), px(247), px(247)), radius=px(57), outline="#404040", width=px(2))

    shield = [(px(128), px(39)), (px(196), px(64)), (px(196), px(118)), (px(188), px(151)),
              (px(169), px(177)), (px(128), px(219)), (px(87), px(198)), (px(68), px(172)),
              (px(60), px(139)), (px(60), px(64)), (px(128), px(39))]
    draw.polygon(shield, fill="#262626")
    draw.line(shield, fill="#E5E5E5", width=px(7), joint="curve")

    bars = [(91, 127, 142), (110, 104, 165), (128, 88, 182), (146, 107, 153), (165, 111, 143)]
    for x, top, bottom in bars:
        x, top, bottom = px(x), px(top), px(bottom)
        draw.line((x, top, x, bottom), fill="#FAFAFA", width=px(9))
        radius = px(4.5)
        draw.ellipse((x-radius, top-radius, x+radius, top+radius), fill="#FAFAFA")
        draw.ellipse((x-radius, bottom-radius, x+radius, bottom+radius), fill="#FAFAFA")
    draw.ellipse((px(123), px(180), px(133), px(190)), fill="#20C997")
    return image.resize((512, 512), Image.Resampling.LANCZOS)


if __name__ == "__main__":
    icon = render()
    icon.save(ROOT / "stealth_copilot.png", optimize=True)
    icon.save(ROOT / "stealth_copilot.ico", format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print("Generated stealth_copilot.png and stealth_copilot.ico")
