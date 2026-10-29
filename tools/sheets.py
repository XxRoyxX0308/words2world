"""Contact sheets: many images in one numbered grid, so a reviewer can compare them in one Read."""
import math
import os
import re

from PIL import Image, ImageDraw


def contact(paths, out, labels="", cell=512, cols=None, names=None):
    cols = cols or min(len(paths), 4)
    rows = math.ceil(len(paths) / cols)
    sheet = Image.new("RGB", (cell * cols, cell * rows), (30, 30, 30))
    for i, p in enumerate(paths):
        im = Image.open(p).convert("RGB")
        im.thumbnail((cell, cell), Image.LANCZOS)
        tile = Image.new("RGB", (cell, cell), (45, 45, 45))
        tile.paste(im, ((cell - im.width) // 2, (cell - im.height) // 2))
        m = re.search(r"(\d+)\.png$", os.path.basename(p))
        label = (m.group(1) if m else os.path.splitext(os.path.basename(p))[0]) if labels else \
            os.path.splitext(os.path.basename(p))[0]
        if names:
            label = names[i]
        ImageDraw.Draw(tile).text((8, 6), label, fill=(255, 0, 255), font_size=max(18, cell // 14))
        sheet.paste(tile, ((i % cols) * cell, (i // cols) * cell))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    sheet.save(out)
    return out
