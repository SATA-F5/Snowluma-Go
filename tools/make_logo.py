"""Generate the SnowLuma Go logo (run once; not part of the plugin runtime)."""

import math
import os

from PIL import Image, ImageDraw, ImageFilter

SIZE = 512
OUT = os.path.join(os.path.dirname(__file__), "logo.png")

img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

# Rounded gradient tile
grad = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
gd = ImageDraw.Draw(grad)
top = (91, 140, 255)
bottom = (139, 92, 246)
for y in range(SIZE):
    t = y / (SIZE - 1)
    color = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3)) + (255,)
    gd.line([(0, y), (SIZE, y)], fill=color)

mask = Image.new("L", (SIZE, SIZE), 0)
md = ImageDraw.Draw(mask)
md.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=118, fill=255)
img.paste(grad, (0, 0), mask)

# Soft inner highlight
glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
gdw = ImageDraw.Draw(glow)
gdw.ellipse([-140, -220, SIZE + 80, 200], fill=(255, 255, 255, 38))
img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(60)))

# Snowflake
cx = cy = SIZE / 2
arm = 150
draw_layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(draw_layer)
for i in range(6):
    angle = math.radians(i * 60)
    dx, dy = math.cos(angle), math.sin(angle)
    x2, y2 = cx + dx * arm, cy + dy * arm
    d.line([(cx, cy), (x2, y2)], fill=(255, 255, 255, 245), width=17)
    # branches at 60% and 82%
    for frac, length in ((0.58, 62), (0.82, 40)):
        bx, by = cx + dx * arm * frac, cy + dy * arm * frac
        for side in (-1, 1):
            ba = angle + side * math.radians(52)
            d.line([(bx, by), (bx + math.cos(ba) * length, by + math.sin(ba) * length)],
                   fill=(255, 255, 255, 225), width=13)

# Glow behind the flake
glow2 = draw_layer.filter(ImageFilter.GaussianBlur(26))
img.alpha_composite(glow2)
img.alpha_composite(draw_layer)

# Center dot
d2 = ImageDraw.Draw(img)
d2.ellipse([cx - 20, cy - 20, cx + 20, cy + 20], fill=(255, 255, 255, 250))

img.save(OUT)
print("wrote", OUT, img.size)
