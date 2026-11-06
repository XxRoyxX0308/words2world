"""Support surfaces of baked props: where can something stand on this model, and at what height.

    blender -b --factory-startup -P blender/support_map.py -- <world_dir> [id ...]      (python w2w.py supports <world>)

For every baked prop (or the given ids) whose game/support.json is older than its LOD0 FBX: import the LOD0 FBX
(engine frame: front -Y, Z up, pivot at the bottom centre), cast vertical rays down through a grid over its bounds
and keep every hit on an upward-facing face. game/support.json:

    {"bounds": [w, d, h], "grid": [nx, ny], "x0", "y0", "dx", "dy",
     "cells": [[z, z, ...] per cell, row-major from (x0, y0)], "levels": [heights under >= 4 % of the cells],
     "boards": [heights under >= 20 % of the cells: table tops and shelf boards]}

tools/scene.py uses it for "on": a child stands on the real table top or shelf board under its centre instead of on
the bounding box top or a guessed "surfaces" height (models with a raised back panel, a hutch or shelves whose
boards came out at other heights made children float).
"""
import json
import os
import sys

import bpy
from mathutils import Vector
from mathutils.bvhtree import BVHTree

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
world = argv[0]
only = set(argv[1:])
N = 40                      # cells along the longer side


def items():
    lib = os.path.join(world, "library")
    for cat in sorted(os.listdir(lib)):
        for fam in sorted(os.listdir(os.path.join(lib, cat))):
            for iid in sorted(os.listdir(os.path.join(lib, cat, fam))):
                d = os.path.join(lib, cat, fam, iid)
                a = os.path.join(d, "game", "asset.json")
                if os.path.exists(a) and (not only or iid in only):
                    yield iid, d, a


done = 0
for iid, d, apath in items():
    a = json.load(open(apath, encoding="utf-8"))
    if a.get("kind") != "prop":
        continue
    fbx = os.path.join(d, "game", a["lods"][0]["file"])
    out = os.path.join(d, "game", "support.json")
    if not os.path.exists(fbx) or (not only and os.path.exists(out) and os.path.getmtime(out) > os.path.getmtime(fbx)):
        continue
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=fbx)
    verts, polys = [], []
    for ob in bpy.context.scene.objects:
        if ob.type != "MESH" or ob.name.startswith("UCX_"):
            continue
        me = ob.evaluated_get(bpy.context.evaluated_depsgraph_get()).to_mesh()
        base = len(verts)
        verts += [ob.matrix_world @ v.co for v in me.vertices]
        polys += [[base + i for i in p.vertices] for p in me.polygons]
    if not polys:
        continue
    tree = BVHTree.FromPolygons(verts, polys)
    xs, ys, zs = [v.x for v in verts], [v.y for v in verts], [v.z for v in verts]
    x0, x1, y0, y1, z1 = min(xs), max(xs), min(ys), max(ys), max(zs)
    w, dd = x1 - x0, y1 - y0
    step = max(w, dd) / N
    nx, ny = max(1, round(w / step)), max(1, round(dd / step))
    dx, dy = w / nx, dd / ny
    cells, hist = [], {}
    for j in range(ny):
        for i in range(nx):
            p = Vector((x0 + (i + 0.5) * dx, y0 + (j + 0.5) * dy, z1 + 0.5))
            hits, guard = [], 0
            while guard < 24:
                guard += 1
                loc, nrm, _, _ = tree.ray_cast(p, Vector((0, 0, -1)), 50)
                if loc is None:
                    break
                if nrm.z > 0.6 and (not hits or hits[-1] - loc.z > 0.01):
                    hits.append(round(loc.z, 3))
                p = loc - Vector((0, 0, 0.004))
            cells.append(hits)
            for z in hits:
                k = round(z / 0.02)
                hist[k] = hist.get(k, 0) + 1
    bins = sorted(hist)                     # 2 cm bins -> levels (>= 4 % of the cells) and boards (>= 20 %)
    groups = []
    for k in bins:
        if groups and k - groups[-1][-1] <= 2:
            groups[-1].append(k)
        else:
            groups.append([k])
    levels, boards = [], []
    for g in groups:
        n = sum(hist[k] for k in g)
        z = round(max(g, key=lambda k: hist[k]) * 0.02, 3)
        if z <= 0.03:
            continue
        if n >= 0.04 * len(cells):
            levels.append(z)
        if n >= 0.2 * len(cells):
            boards.append(z)
    json.dump({"bounds": [round(w, 4), round(dd, 4), round(z1, 4)], "grid": [nx, ny], "x0": round(x0, 4),
               "y0": round(y0, 4), "dx": round(dx, 5), "dy": round(dy, 5), "cells": cells, "levels": levels, "boards": boards},
              open(out, "w"), separators=(",", ":"))
    done += 1
    print(f"support {iid}: {nx}x{ny} cells, boards {boards} levels {levels}", flush=True)
print(f"support maps written: {done}")
