"""Picked decal images -> library/.../<id>/game/ (RGBA base colour with soft edges, normal, asset.json).

Decal item fields (item.json):
  size        [width, height] in metres (required)
  alpha       "luma"  dark marks on white -> opacity from darkness (stains, leaks, soot, cracks, moss streaks)
              "matte" RMBG cut-out (light or coloured shapes: paper notes, photos, leaves, paint, labels)
  roughness   0.15 wet leak ... 0.9 dust (default 0.6)
  opacity     overall strength (default 1)
  normal_strength  relief from the alpha (default 1.5; cracks 3, stains 0.5)
  res         longest side in pixels (default 1024)
  place       hint for automatic placement: wall_base | wall_corner | under_window | ceiling | floor | floor_edge |
              roof | any (used by build/scene.json "decal_rules")
"""
import os

import numpy as np

import texlib as tx
from common import save


def build(world, only=()):
    n = 0
    for it in world.items().values():
        if it.kind != "decal" or it.get("status") == "rejected" or (only and it.id not in only):
            continue
        if not it.forge_done():
            print(f"{it.id}: not ready ({it.stage()}), skipped")
            continue
        if not only and it.stage() == "baked":
            continue
        res = int(it.get("res", 1024))
        if it.get("alpha", "luma") == "matte":
            rgba = tx.load(it.f("2d", "rgba.png"), 4)
            color, a = rgba[..., :3], rgba[..., 3]
        else:
            color, a = tx.alpha_from_luma(tx.load(it.picked()))
        w_m, h_m = it["size"]
        aspect = w_m / h_m
        w, h = (res, max(64, round(res / aspect))) if aspect >= 1 else (max(64, round(res * aspect)), res)
        a = a * tx.edge_fade(*a.shape) * float(it.get("opacity", 1.0))
        color = tx.dilate_color(color, a)
        rgba = tx.resize(np.concatenate([color, a[..., None]], axis=2), (w, h))
        normal = tx.normal_from_height(tx.blur(rgba[..., 3], 1.5), strength=float(it.get("normal_strength", 1.5)),
                                       wrap=False)
        out = it.f("game")
        rough = float(it.get("roughness", 0.6))
        files = tx.write_maps(out, it.id, rgba, normal, 1.0, rough, 0.0)
        save(os.path.join(out, "asset.json"), {
            "kind": "decal", "id": it.id, "rel": it.rel, "size_m": [w_m, h_m],
            "textures": {"bc": files["bc"], "n": files["n"]}, "place": it.get("place", "any"),
            "params": {"roughness": rough, "opacity": float(it.get("opacity", 1.0))}})
        n += 1
        print(f"{it.id}: {w}x{h}, coverage {float((rgba[..., 3] > 0.5).mean()):.2f} -> {out}")
    print(f"{n} decals written")
