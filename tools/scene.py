"""build/scene.json (+ architecture, terrain, library) -> build/out/level.json + budget.json for the engine importer.

scene.json (all positions in world metres; see docs/FORMATS.md for the full reference):
  instances   [{"name", "item", "pos": [x, y] | [x, y, z], "yaw", "scale", "space"}]
              pos with 2 numbers sits on the terrain (or z = 0 without terrain)
              {"item", "on": "<name of another instance>", "at": [x, y] (parent-local), "level": i, "z": dz}
                 stacks on the parent's top (or its item's "surfaces"[i], e.g. a shelf board); yaw is relative
  arrays      [{"items": {"book_red": 2, "book_blue": 1} | "item", "from", "to", "count", "on", "level",
               "jitter": {"pos": 0.01, "yaw": 4}, "yaw", "space"}]          (rows of books, jars, logs, planks)
  scatter     [{"items": {...}, "rect": [x0, y0, x1, y1], "count", "min_dist", "on", "level", "yaw": "random",
               "space", "seed"}]                                            (clutter on a floor or a table top)
  decals      [{"decal", "pos": [x, y, z], "normal": [nx, ny, nz], "roll", "scale"}]
  decal_rules [{"decals": [...], "anchors": ["wall_base", ...], "sides": "out" | "in" | "both", "every": 1.5,
               "chance": 0.7, "scale": [0.8, 1.3], "roll": 8, "buildings": [...], "seed": 1}]
              anchors: wall_base, wall_top, wall_corner, under_window, door_frame, door_floor, floor_edge,
                       chimney_base, foundation
  lights      [{"type": "point" | "spot" | "rect", "pos": [x, y, z], "lumens", "kelvin", "radius", "shadows",
               "dir", "cone", "size", "color", "fixture": item id, "space"}]
  lighting    {"sun": {"dir", "lux", "kelvin"}, "sky": true, "fog": {...}, "post": {...}, "gi": "dynamic"}
  cameras     [{"id", "pos", "look", "fov", "space"}]
  player_start {"pos", "yaw"}

Instances whose item is not baked yet become magenta placeholder boxes of the item's size, named TEMP_<item>:
the level always shows what is planned, and the next build round replaces them automatically.
"""
import math
import os
import zlib

import numpy as np

import texlib as tx
from common import load, mtime, save
from terrain import height_at, poisson

BUDGET = {"space_tris": 1_500_000, "space_instances": 1500, "space_decals": 80, "shadow_lights": 4}


# ---------------------------------------------------------------- architecture anchors
def rot2(v, yaw):
    c, s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    return (v[0] * c - v[1] * s, v[0] * s + v[1] * c)


def to_world(b, p):
    ox, oy = b.get("origin", [0, 0, 0])[:2]
    oz = (b.get("origin", [0, 0, 0]) + [0, 0, 0])[2]
    x, y = rot2(p[:2], b.get("yaw", 0))
    return [ox + x, oy + y, oz + (p[2] if len(p) > 2 else 0.0)]


def anchors(arch, kinds, spacing, sides, only_buildings=None):
    """World-space decal anchors on the buildings: (kind, pos, normal, up, depth)."""
    out = []
    for b in arch.get("buildings", []):
        if only_buildings and b["id"] not in only_buildings:
            continue
        rot_n = lambda n: list(rot2(n, b.get("yaw", 0))) + [0.0]
        for part in b["parts"]:
            t = part.get("thickness", part.get("log_d", 0.26))
            if part["type"] in ("wall", "logwall"):
                pts = part["path"] + ([part["path"][0]] if part.get("closed") else [])
                z0, H = part.get("z", 0.0), part["height"]
                for si, (a, c) in enumerate(zip(pts[:-1], pts[1:])):
                    a, c = np.array(a, float), np.array(c, float)
                    L = float(np.linalg.norm(c - a))
                    d = (c - a) / L
                    outn = np.array([d[1], -d[0]])
                    ops = [o for o in part.get("openings", []) if o.get("seg", 0) == si]

                    def blocked(at, z, pad=0.25):
                        return any(abs(at - o["at"]) < o["width"] / 2 + pad and
                                   o.get("sill", 0) - pad < z - z0 < o.get("sill", 0) + o["height"] + pad for o in ops)

                    for side in ({"out": [1], "in": [-1], "both": [1, -1]}[sides]):
                        n = outn * side
                        off = n * (t / 2)

                        def put(kind, at, z, normal=None, up=(0, 0, 1)):
                            p = a + d * at + off
                            out.append((kind, to_world(b, [p[0], p[1], z]), normal or rot_n(n), list(up),
                                        max(0.3, t)))
                        if "wall_base" in kinds:
                            for at in np.arange(spacing / 2, L, spacing):
                                if not blocked(at, z0 + 0.3):
                                    put("wall_base", at, z0 + 0.35)
                        if "wall_top" in kinds:
                            for at in np.arange(spacing / 2, L, spacing):
                                if not blocked(at, z0 + H - 0.4):
                                    put("wall_top", at, z0 + H - 0.45)
                        if "wall_corner" in kinds:
                            for at in (0.3, L - 0.3):
                                put("wall_corner", at, z0 + H * 0.45)
                        for o in ops:
                            if "under_window" in kinds and o.get("kind", "window") == "window":
                                put("under_window", o["at"], z0 + o.get("sill", 0.9) - 0.35)
                            if "door_frame" in kinds and o.get("kind") == "door":
                                for s_ in (-1, 1):
                                    put("door_frame", o["at"] + s_ * (o["width"] / 2 + 0.25), z0 + 1.1)
                            if "door_floor" in kinds and o.get("kind") == "door":
                                p = a + d * o["at"] + n * 0.7
                                out.append(("door_floor", to_world(b, [p[0], p[1], z0 + 0.05]), [0, 0, 1],
                                            rot_n(d.tolist()), 0.4))
                        if "floor_edge" in kinds and side == -1:
                            for at in np.arange(spacing / 2, L, spacing):
                                p = a + d * at + n * (t / 2 + 0.35)
                                out.append(("floor_edge", to_world(b, [p[0], p[1], z0 + 0.05]), [0, 0, 1],
                                            rot_n(d.tolist()), 0.4))
            elif part["type"] == "chimney" and "chimney_base" in kinds:
                (cx, cy), (w, dd) = part["pos"], part["size"]
                for n, off in (((1, 0), (w / 2, 0)), ((-1, 0), (-w / 2, 0)), ((0, 1), (0, dd / 2)), ((0, -1), (0, -dd / 2))):
                    out.append(("chimney_base", to_world(b, [cx + off[0], cy + off[1], part.get("z0", 0) + 0.6]),
                                rot_n(n), [0, 0, 1], 0.4))
            elif part["type"] == "foundation" and "foundation" in kinds:
                poly = part["poly"]
                for a, c in zip(poly, poly[1:] + poly[:1]):
                    a, c = np.array(a, float), np.array(c, float)
                    L = float(np.linalg.norm(c - a))
                    d = (c - a) / L
                    n = np.array([d[1], -d[0]])
                    for at in np.arange(spacing / 2, L, spacing):
                        p = a + d * at + n * part.get("thickness", 0.35) / 2
                        out.append(("foundation", to_world(b, [p[0], p[1], part.get("z0", -0.2) + part["height"] * 0.6]),
                                    rot_n(n.tolist()), [0, 0, 1], 0.5))
    return [a for a in out if a[0] in kinds]


# ---------------------------------------------------------------- shared textures
def shared_textures(world):
    """Macro variation (R brightness, G roughness, B layer-2 blend mask), detail normal, neutral defaults."""
    out = world.out("shared")
    files = {"macro": "T_Macro.png", "detail_n": "T_DetailNormal_N.png", "default_bc": "T_Default_BC.png",
             "default_n": "T_Default_N.png", "default_orm": "T_Default_ORM.png"}
    if all(os.path.exists(os.path.join(out, f)) for f in files.values()):
        return {k: os.path.join(out, f) for k, f in files.items()}
    macro = np.stack([tx.tileable_noise(1024, 256, seed=11), tx.tileable_noise(1024, 96, seed=12),
                      tx.tileable_noise(1024, 160, seed=13, octaves=5)], axis=2)
    tx.save(os.path.join(out, files["macro"]), macro)
    grain = tx.tileable_noise(512, 3, seed=21, octaves=3)
    tx.save(os.path.join(out, files["detail_n"]), tx.normal_from_height(grain, strength=1.2))
    tx.write_maps(out, "Default", np.full((4, 4, 3), 0.5, np.float32), None, 1.0, 0.6, 0.0, clamp=False)
    return {k: os.path.join(out, f) for k, f in files.items()}


# ---------------------------------------------------------------- compile
def compile_level(world):
    sc = load(world.p("build", "scene.json"), {})
    arch = load(world.p("build", "architecture.json"), {"buildings": []})
    items = world.items(refresh=True)
    ground = height_at(world)
    warnings = []
    used = set()

    def item_size(iid):
        it = items.get(iid)
        if it is None:
            return [0.5, 0.5, 0.5]
        a = it.asset() if it.stage() == "baked" else None
        if a and a.get("bounds_m"):
            return a["bounds_m"]
        s = it.get("size", {}) if isinstance(it.get("size"), dict) else {}
        h = s.get("height") or (s.get("width", 0.5) / it.get("aspect", 1.0) if it.kind == "slab" else 0.5)
        w = s.get("width") or (h * it.get("aspect", 1.0) if it.kind == "slab" else h * 0.8)
        dpt = 0.03 if it.kind == "slab" else (s.get("depth") or w)
        return [w, dpt, h]

    def top_of(inst, level):
        it = items.get(inst["item"])
        surf = (it.get("surfaces") if it else None) or []
        if level is not None and level < len(surf):
            return surf[level]
        return item_size(inst["item"])[2] * inst.get("scale", 1.0)

    # expand arrays and scatter into plain instances
    raw = list(sc.get("instances", []))
    for k, arr in enumerate(sc.get("arrays", [])):
        rng = np.random.default_rng(arr.get("seed", k + 1))
        names = [arr["item"]] if "item" in arr else list(arr["items"])
        wts = np.array([1.0] if "item" in arr else [arr["items"][n] for n in names])
        a, b = np.array(arr["from"], float), np.array(arr["to"], float)
        jit = arr.get("jitter", {})
        for i in range(arr["count"]):
            t = i / max(arr["count"] - 1, 1)
            p = a + (b - a) * t + rng.uniform(-1, 1, len(a)) * jit.get("pos", 0.0)
            inst = {k2: v for k2, v in arr.items() if k2 in ("on", "level", "space", "z", "scale")}
            inst.update({"item": names[rng.choice(len(names), p=wts / wts.sum())],
                         "yaw": arr.get("yaw", 0) + rng.uniform(-1, 1) * jit.get("yaw", 0),
                         "name": f"{arr.get('name', 'array' + str(k))}_{i:02d}"})
            inst["at" if "on" in arr else "pos"] = [round(float(v), 4) for v in p]
            raw.append(inst)
    for k, scat in enumerate(sc.get("scatter", [])):
        rng = np.random.default_rng(scat.get("seed", 100 + k))
        names = list(scat["items"])
        wts = np.array([scat["items"][n] for n in names], float)
        x0, y0, x1, y1 = scat["rect"]
        pts = poisson(lambda x, y: np.ones(np.shape(x), bool), (x0, y0, x1, y1), scat.get("min_dist", 0.4),
                      scat["count"], rng)
        if len(pts) < scat["count"]:
            warnings.append(f"scatter {k}: only {len(pts)} of {scat['count']} fit (min_dist {scat.get('min_dist', 0.4)})")
        for i, (x, y) in enumerate(pts):
            inst = {k2: v for k2, v in scat.items() if k2 in ("on", "level", "space", "z", "scale")}
            yaw = rng.uniform(0, 360) if scat.get("yaw", "random") == "random" else scat["yaw"] + rng.uniform(-8, 8)
            inst.update({"item": names[rng.choice(len(names), p=wts / wts.sum())], "yaw": round(float(yaw), 1),
                         "name": f"{scat.get('name', 'scatter' + str(k))}_{i:02d}"})
            inst["at" if "on" in scat else "pos"] = [round(float(x), 4), round(float(y), 4)]
            raw.append(inst)

    named = {}
    for i, inst in enumerate(raw):
        inst.setdefault("name", f"{inst['item']}_{i:03d}")
        if inst["name"] in named:
            inst["name"] = f"{inst['name']}_{i}"
        named[inst["name"]] = inst
    resolved = {}

    def resolve(inst, depth=0):
        if inst["name"] in resolved:
            return resolved[inst["name"]]
        if depth > 12:
            raise ValueError(f"instance {inst['name']}: 'on' chain too deep or circular")
        if "on" in inst:
            parent = named.get(inst["on"])
            if parent is None:
                raise ValueError(f"instance {inst['name']}: 'on' parent {inst['on']} does not exist")
            pr = resolve(parent, depth + 1)
            lx, ly = inst.get("at", [0, 0])[:2]
            dx, dy = rot2((lx, ly), pr["yaw"])
            pos = [pr["pos"][0] + dx, pr["pos"][1] + dy, pr["pos"][2] + top_of(parent, inst.get("level")) +
                   inst.get("z", 0.0)]
            yaw = pr["yaw"] + inst.get("yaw", 0)
            space = inst.get("space", pr["space"])
        else:
            p = inst["pos"]
            z = p[2] if len(p) > 2 else ((ground(p[0], p[1]) if ground else 0.0) + inst.get("z", 0.0))
            pos, yaw, space = [p[0], p[1], z], inst.get("yaw", 0), inst.get("space", "outdoor")
        r = {"name": inst["name"], "item": inst["item"], "pos": [round(v, 4) for v in pos], "yaw": round(yaw, 2),
             "scale": inst.get("scale", 1.0), "space": space}
        resolved[inst["name"]] = r
        return r

    instances = []
    for inst in raw:
        r = resolve(inst)
        it = items.get(r["item"])
        if it is None:
            warnings.append(f"{r['name']}: item {r['item']} is not in the library (placeholder)")
        if it is None or it.stage() != "baked":
            r["placeholder"] = True
            r["size"] = item_size(r["item"])
        else:
            used.add(r["item"])
        instances.append(r)

    # lights (+ fixtures)
    lights = []
    for i, l in enumerate(sc.get("lights", [])):
        l = {"type": "point", "lumens": 800, "kelvin": 3000, "radius": 8, "shadows": False, "dir": [0, 0, -1],
             "cone": 60, "size": [0.5, 0.5], "space": "outdoor", "name": f"light_{i:02d}", **l}
        lights.append(l)
        if l.get("fixture"):
            instances.append({"name": f"{l['name']}_fixture", "item": l["fixture"], "pos": l["pos"], "yaw": l.get("yaw", 0),
                              "scale": 1.0, "space": l["space"]})
            it = items.get(l["fixture"])
            if it is None or it.stage() != "baked":
                instances[-1].update({"placeholder": True, "size": item_size(l["fixture"])})
            else:
                used.add(l["fixture"])

    # decals: explicit + rules
    decals = []

    def decal_entry(did, pos, normal, up, depth, scale, roll, name):
        it = items.get(did)
        if it is None or it.stage() != "baked":
            warnings.append(f"decal {did} not processed yet (run w2w.py decals): skipped")
            return
        used.add(did)
        w, h = it.asset()["size_m"]
        n = np.array(normal, float)
        n /= np.linalg.norm(n)
        u = np.array(up, float)
        u = u - n * (u @ n)
        if np.linalg.norm(u) < 1e-4:
            u = np.array([0, 1.0, 0]) if abs(n[2]) > 0.9 else np.array([0, 0, 1.0])
        u /= np.linalg.norm(u)
        if roll:
            r = math.radians(roll)
            u = u * math.cos(r) + np.cross(n, u) * math.sin(r)
        decals.append({"name": name, "decal": did, "pos": [round(float(v), 4) for v in pos],
                       "normal": [round(float(v), 4) for v in n], "up": [round(float(v), 4) for v in u],
                       "size": [w * scale, h * scale], "depth": depth})

    for i, d in enumerate(sc.get("decals", [])):
        decal_entry(d["decal"], d["pos"], d.get("normal", [0, 0, 1]), d.get("up", [0, 0, 1] if abs(
            d.get("normal", [0, 0, 1])[2]) < 0.9 else [0, 1, 0]), d.get("depth", 0.3), d.get("scale", 1.0),
            d.get("roll", 0), d.get("name", f"decal_{i:03d}"))
    for k, rule in enumerate(sc.get("decal_rules", [])):
        rng = np.random.default_rng(rule.get("seed", 500 + k))
        found = anchors(arch, set(rule["anchors"]), rule.get("every", 1.5), rule.get("sides", "out"),
                        rule.get("buildings"))
        lo, hi = rule.get("scale", [0.8, 1.3])
        n_before = len(decals)
        for j, (kind, pos, normal, up, depth) in enumerate(found):
            if rng.uniform() > rule.get("chance", 0.7):
                continue
            did = rule["decals"][rng.integers(len(rule["decals"]))]
            roll = rng.uniform(-1, 1) * (180 if abs(normal[2]) > 0.9 else rule.get("roll", 8))
            decal_entry(did, pos, normal, up, depth, rng.uniform(lo, hi), roll, f"rule{k}_{kind}_{j:03d}")
        print(f"decal rule {k} {rule['anchors']}: {len(decals) - n_before} decals on {len(found)} anchors")

    # architecture + terrain + foliage
    arch_out = load(world.out("arch", "manifest.json"), {"meshes": []})["meshes"]
    terr = load(world.out("terrain", "manifest.json"), {}) or None
    foliage = load(world.out("terrain", "foliage.json"), {"layers": []})["layers"]
    fol_out = []
    for L in foliage:
        meshes = {}
        for iid, rows in L["instances"].items():
            it = items.get(iid)
            if it is None or it.stage() != "baked":
                warnings.append(f"foliage {L['id']}: {iid} not baked, {len(rows)} instances shown as placeholders")
                meshes[iid] = {"placeholder": True, "size": item_size(iid), "rows": rows[:400]}
            else:
                used.add(iid)
                meshes[iid] = {"rows": rows}
        fol_out.append({"id": L["id"], "cull": L.get("cull", [0, 0]), "shadows": L.get("shadows", True),
                        "meshes": meshes})
    surfaces_needed = set()
    for m in arch_out:
        surfaces_needed.update(s for s in m["slots"] if s != "glass")
    if terr:
        surfaces_needed.update(terr.get("layers", {}).values())
    for sid in list(surfaces_needed):
        it = items.get(sid)
        if it is None or it.kind != "surface":
            warnings.append(f"surface {sid} is used by the architecture / terrain but is not a library surface")
            continue
        if it.stage() != "baked":
            warnings.append(f"surface {sid} not built yet (w2w.py surfaces): engine uses a grey fallback")
            continue
        used.add(sid)
        l2 = (it.asset() or {}).get("params", {}).get("layer2")
        if l2 and l2 in items and items[l2].stage() == "baked":
            used.add(l2)

    library = {}
    for iid in sorted(used):
        it = items[iid]
        library[iid] = {"kind": it.kind, "rel": it.rel, "dir": it.f("game"), "asset": it.asset()}

    cameras = list(sc.get("cameras", []))
    if not cameras:
        for b in arch.get("buildings", []):
            o = b.get("origin", [0, 0, 0])
            for k, (dx, dy) in enumerate(((-9, -11), (11, 9))):
                cameras.append({"id": f"auto_{b['id']}_{k}", "pos": [o[0] + dx, o[1] + dy, 2.2],
                                "look": [o[0] + 3, o[1] + 2.5, 1.8], "space": b["id"]})
    for c in cameras:
        c.setdefault("fov", 70)
        d = np.array(c["look"], float) - np.array(c["pos"], float)
        c["dir"] = [round(float(v), 4) for v in d / max(np.linalg.norm(d), 1e-6)]

    ps = sc.get("player_start")
    if ps and len(ps["pos"]) == 2:
        ps = {**ps, "pos": [ps["pos"][0], ps["pos"][1], ground(*ps["pos"]) if ground else 0.0]}

    warnings = list(dict.fromkeys(warnings))
    level = {"name": sc.get("name", world.name), "world": world.root, "library": library,
             "shared": shared_textures(world),
             "arch": [{"name": m["name"], "file": m["file"], "pos": m["origin"], "yaw": m["yaw"],
                       "slots": m["slots"], "space": m.get("space", m["building"])} for m in arch_out],
             "terrain": terr, "foliage": fol_out, "instances": instances, "decals": decals, "lights": lights,
             "lighting": sc.get("lighting", {}), "cameras": cameras, "player_start": ps}
    save(world.out("level.json"), level)

    # budget
    spaces = {}
    for r in instances:
        s = spaces.setdefault(r["space"], {"instances": 0, "tris": 0, "placeholders": 0, "decals": 0, "shadow_lights": 0})
        s["instances"] += 1
        if r.get("placeholder"):
            s["placeholders"] += 1
            s["tris"] += 12
        else:
            s["tris"] += library[r["item"]]["asset"]["lods"][0]["tris"]
    for d in decals:
        nearest = min(instances, key=lambda r: (r["pos"][0] - d["pos"][0]) ** 2 + (r["pos"][1] - d["pos"][1]) ** 2) \
            if instances else None
        spaces.setdefault(nearest["space"] if nearest else "outdoor",
                          {"instances": 0, "tris": 0, "placeholders": 0, "decals": 0, "shadow_lights": 0})["decals"] += 1
    for l in lights:
        if l.get("shadows"):
            spaces.setdefault(l["space"], {"instances": 0, "tris": 0, "placeholders": 0, "decals": 0,
                                           "shadow_lights": 0})["shadow_lights"] += 1
    for name, s in spaces.items():
        for key, lim in (("tris", "space_tris"), ("instances", "space_instances"), ("decals", "space_decals"),
                         ("shadow_lights", "shadow_lights")):
            if s[key] > BUDGET[lim]:
                warnings.append(f"BUDGET {name}: {key} {s[key]:,} > {BUDGET[lim]:,}")
    budget = {"spaces": spaces, "arch_tris": sum(m.get("tris", 0) for m in arch_out),
              "foliage_instances": sum(len(v["rows"]) for L in fol_out for v in L["meshes"].values()),
              "decals": len(decals), "instances": len(instances),
              "placeholders": sum(1 for r in instances if r.get("placeholder")), "warnings": warnings}
    save(world.out("budget.json"), budget)
    for w in warnings:
        print("WARNING", w)
    print(f"level.json: {len(instances)} instances ({budget['placeholders']} placeholders), {len(decals)} decals, "
          f"{len(lights)} lights, {len(arch_out)} arch meshes, {budget['foliage_instances']} foliage, "
          f"{len(cameras)} cameras -> {world.out('level.json')}")
    return level
