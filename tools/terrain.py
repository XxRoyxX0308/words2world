"""build/terrain.json -> build/out/terrain/: height.npy + heightmap.png (16-bit), splat.png, foliage.json.
Blender (build_terrain.py) then turns the height field into Nanite terrain tiles.

terrain.json:
{
  "size": [240, 240], "center": [0, 0], "res": 1.0, "seed": 7,
  "base":    {"amplitude": 5, "scale": 90, "octaves": 5, "roughness": 0.5},
  "ridges":  [{"path": [[x, y], ...], "height": 12, "width": 35}],
  "carve":   [{"path": [[x, y], ...], "depth": 1.2, "width": 3.0}],              (streams, ditches)
  "flatten": [{"center": [x, y], "radius": 9, "falloff": 8, "height": 0.0}],     (building pads, clearings)
  "paths":   [{"points": [[x, y], ...], "width": 1.6, "sink": 0.06}],
  "layers":  {"r": "grass_wild", "g": "dirt_path", "b": "rock_mossy", "a": "forest_floor"},   (surface ids)
  "rock_slope": 32,                                   (degrees where rock takes over)
  "foliage": [
    {"id": "pines", "items": {"pine_tall": 3, "pine_young": 1}, "density": 0.01, "min_dist": 4.0,
     "area": {"rect": [x0, y0, x1, y1]} | {"center": [x, y], "radius": r, "inner": r0} | "all",
     "noise": {"scale": 45, "threshold": 0.4}, "slope": [0, 30], "height": [-99, 99],
     "avoid": {"paths": 3.0, "flatten": 1.0, "rects": [[x0, y0, x1, y1], ...], "margin": 1.0},
     "scale": [0.8, 1.3], "align": 0.0, "sink": 0.15, "max": 4000, "cull": [6000, 9000]}
  ]
}
density = instances per square metre before filtering; min_dist = Poisson-disk spacing; align 0 = upright
(trees), 1 = follow the terrain normal (rocks, grass); cull = start / end distance in cm for the engine.
Clusters come from the noise threshold: forests thin out into clearings instead of a uniform carpet.
"""
import math
import os
import zlib

import numpy as np
from scipy import ndimage

import texlib as tx
from common import load, save


# ---------------------------------------------------------------- fields
class Grid:
    def __init__(self, cfg):
        self.w, self.d = cfg.get("size", [200, 200])
        self.cx, self.cy = cfg.get("center", [0, 0])
        self.res = float(cfg.get("res", 1.0))
        self.nx = int(round(self.w / self.res)) + 1
        self.ny = int(round(self.d / self.res)) + 1
        self.x0, self.y0 = self.cx - self.w / 2, self.cy - self.d / 2
        xs = self.x0 + np.arange(self.nx) * self.res
        ys = self.y0 + np.arange(self.ny) * self.res
        self.X, self.Y = np.meshgrid(xs, ys)          # rows = y (south to north), cols = x

    def sample(self, field, x, y):
        """Bilinear lookup of a grid field at world positions."""
        fx = (np.asarray(x) - self.x0) / self.res
        fy = (np.asarray(y) - self.y0) / self.res
        return ndimage.map_coordinates(field, [np.ravel(fy), np.ravel(fx)], order=1, mode="nearest").reshape(np.shape(x))


def fbm(grid, scale, octaves, roughness, rng):
    out = np.zeros_like(grid.X)
    amp, total = 1.0, 0.0
    for k in range(octaves):
        cell = scale / (2 ** k)
        n_x = int(grid.w / cell) + 4
        n_y = int(grid.d / cell) + 4
        lattice = rng.standard_normal((n_y, n_x))
        fy = (grid.Y - grid.y0) / cell + 1
        fx = (grid.X - grid.x0) / cell + 1
        out += amp * ndimage.map_coordinates(lattice, [fy, fx], order=3, mode="wrap")
        total += amp
        amp *= roughness
    return out / total


def polyline_distance(grid_x, grid_y, pts):
    pts = np.asarray(pts, np.float64)
    best = np.full(np.shape(grid_x), np.inf)
    for a, b in zip(pts[:-1], pts[1:]):
        ab = b - a
        t = np.clip(((grid_x - a[0]) * ab[0] + (grid_y - a[1]) * ab[1]) / max(ab @ ab, 1e-9), 0, 1)
        dx, dy = grid_x - (a[0] + t * ab[0]), grid_y - (a[1] + t * ab[1])
        best = np.minimum(best, np.sqrt(dx * dx + dy * dy))
    return best


def smoothstep(e0, e1, x):
    t = np.clip((x - e0) / max(e1 - e0, 1e-9), 0, 1)
    return t * t * (3 - 2 * t)


def heightfield(cfg, grid):
    rng = np.random.default_rng(cfg.get("seed", 1))
    b = cfg.get("base", {})
    h = fbm(grid, b.get("scale", 90), b.get("octaves", 5), b.get("roughness", 0.5), rng) * b.get("amplitude", 4)
    for r in cfg.get("ridges", []):
        d = polyline_distance(grid.X, grid.Y, r["path"])
        h += r.get("height", 10) * np.exp(-(d / r.get("width", 30)) ** 2)
    for c in cfg.get("carve", []):
        d = polyline_distance(grid.X, grid.Y, c["path"])
        h -= c.get("depth", 1.0) * np.exp(-(d / c.get("width", 3)) ** 2)
    path_mask = np.zeros_like(h)
    for p in cfg.get("paths", []):
        d = polyline_distance(grid.X, grid.Y, p["points"])
        w = p.get("width", 1.6)
        m = 1 - smoothstep(w * 0.5, w * 1.6, d)
        smooth = ndimage.gaussian_filter(h, sigma=max(w * 1.5 / grid.res, 1))
        h = h * (1 - m) + (smooth - p.get("sink", 0.06)) * m
        path_mask = np.maximum(path_mask, 1 - smoothstep(w * 0.35, w * 0.9, d))
    flat_mask = np.zeros_like(h)
    for f in cfg.get("flatten", []):
        d = np.hypot(grid.X - f["center"][0], grid.Y - f["center"][1])
        target = f.get("height", float(grid.sample(h, f["center"][0], f["center"][1])))
        m = 1 - smoothstep(f["radius"], f["radius"] + f.get("falloff", 6), d)
        h = h * (1 - m) + target * m
        flat_mask = np.maximum(flat_mask, m)
    return h, path_mask, flat_mask


def slope_deg(grid, h):
    gy, gx = np.gradient(h, grid.res)
    return np.degrees(np.arctan(np.hypot(gx, gy))), gx, gy


def splat(cfg, grid, h, path_mask, flat_mask, slope):
    rng = np.random.default_rng(cfg.get("seed", 1) + 11)
    patches = fbm(grid, 18, 4, 0.55, rng)
    forest = fbm(grid, 35, 3, 0.5, rng)
    rock = smoothstep(cfg.get("rock_slope", 32) - 6, cfg.get("rock_slope", 32) + 6, slope)
    dirt = np.clip(np.maximum(path_mask, smoothstep(0.55, 1.1, patches) * 0.8), 0, 1)
    dirt = np.maximum(dirt, flat_mask * 0.25)
    floor = smoothstep(-0.1, 0.5, forest) * (1 - dirt)
    grass = np.ones_like(h)
    w = np.stack([grass * (1 - rock) * (1 - dirt) * (1 - floor), dirt * (1 - rock), rock, floor * (1 - rock)], -1)
    w /= np.maximum(w.sum(-1, keepdims=True), 1e-6)
    return w, forest


# ---------------------------------------------------------------- scatter
def poisson(ok, bounds, min_dist, want, rng):
    """Dart throwing: random candidates filtered by ok(x, y) (vectorised), then greedily thinned so no two points
    are closer than min_dist. Gives the even-but-random spacing of Poisson-disk sampling."""
    x0, y0, x1, y1 = bounds
    n = int(min(max(want * 8, 2000), 600000))
    c = np.column_stack([rng.uniform(x0, x1, n), rng.uniform(y0, y1, n)])
    c = c[ok(c[:, 0], c[:, 1])]
    cell = min_dist / math.sqrt(2)
    occupied = {}
    keep = []
    r2 = min_dist * min_dist
    for x, y in c:
        gx, gy = int((x - x0) / cell), int((y - y0) / cell)
        clear = True
        for yy in range(gy - 2, gy + 3):
            for xx in range(gx - 2, gx + 3):
                q = occupied.get((xx, yy))
                if q is not None and (q[0] - x) ** 2 + (q[1] - y) ** 2 < r2:
                    clear = False
                    break
            if not clear:
                break
        if clear:
            occupied[(gx, gy)] = (x, y)
            keep.append((x, y))
            if len(keep) >= want:
                break
    return np.array(keep) if keep else np.zeros((0, 2))


def area_bounds(area, grid):
    if area in (None, "all"):
        return (grid.x0, grid.y0, grid.x0 + grid.w, grid.y0 + grid.d), lambda x, y: np.ones(np.shape(x), bool)
    if "rect" in area:
        x0, y0, x1, y1 = area["rect"]
        return (x0, y0, x1, y1), lambda x, y: (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1)
    (cx, cy), r = area["center"], area["radius"]
    inner = area.get("inner", 0.0)
    return (cx - r, cy - r, cx + r, cy + r), \
        lambda x, y: (np.hypot(x - cx, y - cy) <= r) & (np.hypot(x - cx, y - cy) >= inner)


def scatter(cfg, grid, h, slope, gx, gy, path_dist, flat_cfg, weights, forest):
    rng = np.random.default_rng(cfg.get("seed", 1) + 101)
    layers = []
    for L in cfg.get("foliage", []):
        bounds, inside = area_bounds(L.get("area", "all"), grid)
        md = L.get("min_dist", 2.0)
        area_m2 = (bounds[2] - bounds[0]) * (bounds[3] - bounds[1])
        want = min(int(L.get("density", 0.01) * area_m2), L.get("max", 20000))
        noise = L.get("noise")
        nfield = fbm(grid, noise.get("scale", 40), 3, 0.5, np.random.default_rng(zlib.crc32(L["id"].encode()))) \
            if noise else None
        avoid = L.get("avoid", {})

        def ok(x, y):
            m = inside(x, y)
            m &= (x >= grid.x0) & (x <= grid.x0 + grid.w) & (y >= grid.y0) & (y <= grid.y0 + grid.d)
            s = grid.sample(slope, x, y)
            lo, hi = L.get("slope", [0, 90])
            m &= (s >= lo) & (s <= hi)
            z = grid.sample(h, x, y)
            lo, hi = L.get("height", [-1e9, 1e9])
            m &= (z >= lo) & (z <= hi)
            if nfield is not None:
                m &= grid.sample(nfield, x, y) > noise.get("threshold", 0.0)
            if avoid.get("paths") and path_dist is not None:
                m &= grid.sample(path_dist, x, y) > avoid["paths"]
            if avoid.get("flatten") is not None:
                for f in flat_cfg:
                    m &= np.hypot(x - f["center"][0], y - f["center"][1]) > f["radius"] + avoid["flatten"]
            mg = avoid.get("margin", 0.0)
            for x0, y0, x1, y1 in avoid.get("rects", []):
                m &= ~((x >= x0 - mg) & (x <= x1 + mg) & (y >= y0 - mg) & (y <= y1 + mg))
            if L.get("on_layer") is not None:                 # e.g. ferns only where the forest-floor layer is
                ch = "rgba".index(L["on_layer"])
                m &= grid.sample(weights[..., ch], x, y) > L.get("on_layer_min", 0.4)
            return m

        pts = poisson(ok, bounds, md, want, rng)
        names = list(L["items"])
        wts = np.array([L["items"][n] for n in names], np.float64)
        choice = rng.choice(len(names), len(pts), p=wts / wts.sum()) if len(pts) else np.zeros(0, int)
        z = grid.sample(h, pts[:, 0], pts[:, 1]) if len(pts) else np.zeros(0)
        lo, hi = L.get("scale", [0.85, 1.15])
        sc = rng.uniform(lo, hi, len(pts))
        yaw = rng.uniform(0, 360, len(pts))
        align = L.get("align", 0.0)
        if len(pts):
            ddx, ddy = grid.sample(gx, pts[:, 0], pts[:, 1]), grid.sample(gy, pts[:, 0], pts[:, 1])
            n = np.stack([-ddx, -ddy, np.ones(len(pts))], -1)
            n /= np.linalg.norm(n, axis=1, keepdims=True)
            up = np.array([0, 0, 1.0]) * (1 - align) + n * align
            up /= np.linalg.norm(up, axis=1, keepdims=True)
        else:
            up = np.zeros((0, 3))
        sink = L.get("sink", 0.05)
        inst = {}
        for i in range(len(pts)):
            inst.setdefault(names[choice[i]], []).append(
                [round(float(pts[i, 0]), 3), round(float(pts[i, 1]), 3), round(float(z[i] - sink * sc[i]), 3),
                 round(float(yaw[i]), 1), round(float(sc[i]), 3)] + [round(float(v), 3) for v in up[i]])
        layers.append({"id": L["id"], "cull": L.get("cull", [0, 0]), "shadows": L.get("shadows", True), "instances": inst,
                       "count": int(len(pts))})
        print(f"foliage {L['id']}: {len(pts)} instances "
              f"({', '.join(f'{k} {len(v)}' for k, v in inst.items())}) of {want} wanted")
    return layers


# ---------------------------------------------------------------- build
def build(world):
    cfg = load(world.p("build", "terrain.json"))
    out = world.out("terrain")
    os.makedirs(out, exist_ok=True)
    grid = Grid(cfg)
    h, path_mask, flat_mask = heightfield(cfg, grid)
    slope, gx, gy = slope_deg(grid, h)
    weights, forest = splat(cfg, grid, h, path_mask, flat_mask, slope)
    path_dist = None
    if cfg.get("paths"):
        path_dist = np.min(np.stack([polyline_distance(grid.X, grid.Y, p["points"]) - p.get("width", 1.6) / 2
                                     for p in cfg["paths"]]), axis=0)
    np.save(os.path.join(out, "height.npy"), h.astype(np.float32))
    lo, hi = float(h.min()), float(h.max())
    tx.save(os.path.join(out, "heightmap.png"), ((h - lo) / max(hi - lo, 1e-6))[::-1], bits=16)
    # splat image: row 0 = north edge (image convention); UVs in build_terrain.py use the same orientation
    tx.save(os.path.join(out, "splat.png"), weights[::-1])
    layers = scatter(cfg, grid, h, slope, gx, gy, path_dist, cfg.get("flatten", []), weights, forest)
    save(os.path.join(out, "foliage.json"), {"layers": layers})
    meta = {"size": [grid.w, grid.d], "center": [grid.cx, grid.cy], "res": grid.res, "origin": [grid.x0, grid.y0],
            "shape": [grid.ny, grid.nx], "z_min": lo, "z_max": hi, "layers": cfg.get("layers", {}),
            "tile_m": cfg.get("tile_m", 64)}
    save(os.path.join(out, "terrain_meta.json"), meta)
    print(f"terrain {grid.w}x{grid.d} m at {grid.res} m, z {lo:.1f} .. {hi:.1f} m -> {out}")
    world.log({"step": "terrain", "foliage": sum(l["count"] for l in layers)})


def height_at(world):
    """Callable (x, y) -> terrain z, or None when there is no terrain yet."""
    path = world.out("terrain", "height.npy")
    meta = load(world.out("terrain", "terrain_meta.json"), {})
    if not os.path.exists(path) or not meta:
        return None
    h = np.load(path)
    x0, y0 = meta["origin"]
    res = meta["res"]

    def f(x, y):
        fx, fy = (x - x0) / res, (y - y0) / res
        return float(ndimage.map_coordinates(h, [[fy], [fx]], order=1, mode="nearest")[0])
    return f
