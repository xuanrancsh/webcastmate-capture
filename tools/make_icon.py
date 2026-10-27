# -*- coding: utf-8 -*-
"""Generate src/assets/icon.ico (run once, commit the result)."""
import os

from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "src", "assets", "icon.ico")

S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# rounded dark plate
pad = 26
d.rounded_rectangle([pad, pad, S - pad, S - pad], radius=104,
                    fill=(10, 12, 18, 255))

# gradient camera body (approximated with vertical bands)
x0, y0, x1, y1 = 118, 168, 330, 356
grad = Image.new("RGBA", (S, S), (0, 0, 0, 0))
gd = ImageDraw.Draw(grad)
for i in range(y1 - y0):
    t = i / float(max(1, y1 - y0 - 1))
    r = int(90 + (168 - 90) * t)
    g = int(162 + (85 - 162) * t)
    b = int(255 + (247 - 255) * t)
    gd.line([(x0, y0 + i), (x1, y0 + i)], fill=(r, g, b, 255))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([x0, y0, x1, y1], radius=36, fill=255)
img.paste(grad, (0, 0), mask)

# lens notch
d = ImageDraw.Draw(img)
d.ellipse([172, 210, 276, 314], fill=(10, 12, 18, 255))
d.ellipse([196, 234, 252, 290], fill=(235, 242, 255, 255))

# viewfinder horn
d.polygon([(346, 224), (416, 190), (416, 334), (346, 300)],
          fill=(168, 85, 247, 255))

# thin glass highlight
d.rounded_rectangle([pad + 1, pad + 1, S - pad - 1, S - pad - 1], radius=103,
                    outline=(255, 255, 255, 26), width=2)

os.makedirs(os.path.dirname(OUT), exist_ok=True)
img.save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                     (64, 64), (128, 128), (256, 256)])
print("wrote", OUT)
