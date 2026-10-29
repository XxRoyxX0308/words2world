"""Background removal (RMBG-2.0) of picked images -> 2d/rgba.png + 2d/review.png (original | cut-out on magenta).

Runs for props, cards and decals with "alpha": "matte" whose rgba.png is missing or older than the pick.
usage (WSL, trellis2 env, ComfyUI stopped): python matte.py <world_dir> [item_id ...]
"""
import os
import sys

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.expanduser("~/TRELLIS.2"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
from common import World, mtime

world = World(sys.argv[1])
only = set(sys.argv[2:])
todo = []
for it in world.items().values():
    if only and it.id not in only or it.get("status") == "rejected" or not it.needs_matte():
        continue
    if not it.picked() or not os.path.exists(it.picked()):
        if only:
            print(f"{it.id}: no pick yet, skipped")
        continue
    if not only and mtime(it.f("2d", "rgba.png")) >= mtime(it.picked()):
        continue
    todo.append(it)
if not todo:
    print("nothing to matte")
    sys.exit(0)

from trellis2.pipelines.rembg import BiRefNet
rembg = BiRefNet("briaai/RMBG-2.0")
rembg.cuda()

for it in todo:
    rgb = Image.open(it.picked()).convert("RGB")
    rgba = np.asarray(rembg(rgb.copy())).copy()
    alpha = rgba[:, :, 3]
    alpha[alpha < 24] = 0
    Image.fromarray(rgba).save(it.f("2d", "rgba.png"))
    a = alpha[:, :, None].astype(np.float32) / 255
    on_magenta = (rgba[:, :, :3] * a + np.array((255, 0, 255), np.float32) * (1 - a)).astype(np.uint8)
    im = Image.fromarray(np.concatenate([np.asarray(rgb), on_magenta], axis=1))
    im.thumbnail((1400, 700), Image.LANCZOS)
    im.save(it.f("2d", "review.png"))
    cover = float((alpha > 127).mean())
    warn = "  <- check: almost nothing left" if cover < 0.03 else ("  <- check: background kept?" if cover > 0.9 else "")
    print(f"{it.id}: cand_{it['pick']} coverage {cover:.2f}{warn}", flush=True)
