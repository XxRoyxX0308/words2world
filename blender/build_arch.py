"""build/architecture.json -> build/out/arch/ (one FBX per building group, preview renders, manifest.json).

usage: python w2w.py arch <world>   (= blender -b --factory-startup -P blender/build_arch.py -- <world_dir> [building ...])

Buildings are made of parametric parts instead of rooms-as-boxes, so walls get thickness, trims, relief and
irregular stone where the real thing would have them. Every part is in building-local metres (+X east, +Y north,
+Z up); the building's origin / yaw place it in the world. Paths of walls run counter-clockwise seen from
above, so the right-hand side of the direction of travel is the outside.

Part types (all take "surface" = a library surface id used as the material slot, optional "group" = which FBX
it goes into (default "main"), optional "seed"):
  wall      solid wall along "path" (closed: true for a loop): height, thickness, z, openings, plinth, cornice,
            belts, pilasters, siding, corner_boards
  logwall   stacked round logs along "path": height, log_d, overhang (log ends past the corners), course_offset
            (0 / 0.5: set 0.5 on the walls that cross at a corner so the logs interleave), chink (surface),
            end_surface (end grain), openings
  slab      extruded polygon "poly" from z - thickness to z (floors, decks, platforms, steps); edge (fascia)
  roof      gable | shed over "rect" [x0, y0, x1, y1]: ridge x | y, eave_z, pitch, overhang, thickness,
            shingles {row, tab: [min, max], lift_deg}, ridge_cap, fascia, rafter_tails {every, size},
            gable_fill (surface of the wall triangles at the gable ends)
  beam      "from" -> "to" with "size" [w, h] or "radius" (round): posts, rafters, purlins, braces, lintels
  stairs    "from" [x, y, z], "dir" (deg, 0 = +Y), width, rise, run, steps, stringers
  railing   along "path" at z: height, post_every, post_size, baluster_every, baluster_size, rails
  chimney   "pos" [x, y], "size" [w, d], z0, z1, shoulder {z, inset}, cap, irregular stone courses
  foundation  stone skirt along "poly": z0, height, thickness (irregular stones on a mortar core)
  chunks    irregular rock / rubble pieces: "along" [[x, y, z], [x, y, z]] or "at" [[x, y, z], ...], count,
            size [min, max], spread, sink (fraction buried): breaks up straight bases and corners
  pipe      "path" [[x, y, z], ...], radius, brackets (every n m, toward "wall_side" [nx, ny]): conduits,
            gutters, downspouts, stove pipes
  box       "center", "size" [x, y, z], "yaw": window boxes, shelves, built-ins, anything rectangular

Openings (wall / logwall): {"at": distance along the segment, "seg": segment index (default 0), "width",
"height", "sill" (0 for doors), "kind": "door" | "window" | "hole", "frame": {"width", "depth", "surface"},
"lintel": {"height", "extend", "surface"}, "sill_board": true, "glass": true, "mullions": [cols, rows]}

All sharp edges get a small bevel (defaults.bevel, default 12 mm) and weighted normals; vertex colour R carries
"grime" (wall bases, corners) for the engine material; UVs are in metres (material tiling = 1 / tile_m).
"""
import json
import math
import os
import sys
import time

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bake_io import img_read, img_write
from common import World, load, save

UP = Vector((0, 0, 1))


# ---------------------------------------------------------------- geometry accumulator
class Geo:
    """One mesh being built: bmesh + material slots by surface id + metre-based UVs."""

    def __init__(self, name):
        self.name = name
        self.bm = bmesh.new()
        self.uv = self.bm.loops.layers.uv.new("UVMap")
        self.slots = []

    def slot(self, surf):
        if surf not in self.slots:
            self.slots.append(surf)
        return self.slots.index(surf)

    def _uv_planar(self, f):
        n = f.normal
        if abs(n.z) < 0.999:
            t = UP.cross(n).normalized()
            b = n.cross(t).normalized()
        else:
            t, b = Vector((1, 0, 0)), Vector((0, 1, 0))
        for loop in f.loops:
            co = loop.vert.co
            loop[self.uv].uv = (co.dot(t), co.dot(b))

    def face(self, verts, surf, uv=True):
        f = self.bm.faces.new(verts)
        f.material_index = self.slot(surf)
        f.normal_update()
        if uv:
            self._uv_planar(f)
        return f

    def box(self, center, size, surf, xdir=(1, 0, 0), zdir=(0, 0, 1)):
        """Oriented box: size = (along xdir, along ydir = zdir x xdir, along zdir)."""
        x = Vector(xdir).normalized()
        z = Vector(zdir).normalized()
        y = z.cross(x).normalized()
        z = x.cross(y).normalized()
        c = Vector(center)
        hx, hy, hz = (s / 2 for s in size)
        if min(hx, hy, hz) <= 1e-5:
            return
        v = [self.bm.verts.new(c + x * sx * hx + y * sy * hy + z * sz * hz)
             for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
        # indices: (sx, sy, sz) -> 4*sx' + 2*sy' + sz'
        for quad in ((0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)):
            self.face([v[i] for i in quad], surf)

    def beam(self, a, b, size, surf):
        a, b = Vector(a), Vector(b)
        d = b - a
        if d.length < 1e-4:
            return
        x = d.normalized()
        z = UP if abs(x.dot(UP)) < 0.95 else Vector((0, 1, 0))
        z = (z - x * z.dot(x)).normalized()
        self.box((a + b) / 2, (d.length, size[0], size[1]), surf, x, z)

    def cylinder(self, a, b, r, surf, sides=12, cap_surf=None, r_end=None):
        a, b = Vector(a), Vector(b)
        d = b - a
        L = d.length
        if L < 1e-4:
            return
        x = d.normalized()
        u = UP.cross(x) if abs(x.dot(UP)) < 0.95 else Vector((1, 0, 0)).cross(x)
        u.normalize()
        w = x.cross(u).normalized()
        r_end = r if r_end is None else r_end
        ring_a, ring_b = [], []
        for i in range(sides):
            ang = 2 * math.pi * i / sides
            off = u * math.cos(ang) + w * math.sin(ang)
            ring_a.append(self.bm.verts.new(a + off * r))
            ring_b.append(self.bm.verts.new(b + off * r_end))
        circ = 2 * math.pi * r
        for i in range(sides):
            j = (i + 1) % sides
            f = self.face([ring_a[i], ring_a[j], ring_b[j], ring_b[i]], surf, uv=False)
            v0, v1 = circ * i / sides, circ * (i + 1) / sides
            for loop, co in zip(f.loops, ((0, v0), (0, v1), (L, v1), (L, v0))):
                loop[self.uv].uv = co
        self.face(list(reversed(ring_a)), cap_surf or surf)
        self.face(ring_b, cap_surf or surf)

    def prism(self, pts, z0, z1, surf):
        """Vertical extrusion of a counter-clockwise polygon [(x, y), ...] from z0 to z1."""
        bot = [self.bm.verts.new((p[0], p[1], z0)) for p in pts]
        top = [self.bm.verts.new((p[0], p[1], z1)) for p in pts]
        self.face(list(reversed(bot)), surf)
        self.face(top, surf)
        n = len(pts)
        for i in range(n):
            j = (i + 1) % n
            self.face([bot[i], bot[j], top[j], top[i]], surf)

    def solid(self, front, back, surf):
        """Extrude a planar polygon (list of 3D points) to a parallel copy (lists must match)."""
        fa = [self.bm.verts.new(p) for p in front]
        fb = [self.bm.verts.new(p) for p in back]
        self.face(list(reversed(fa)), surf)
        self.face(fb, surf)
        n = len(front)
        for i in range(n):
            j = (i + 1) % n
            self.face([fa[i], fa[j], fb[j], fb[i]], surf)

    def hull(self, pts, surf):
        tmp = bmesh.new()
        vs = [tmp.verts.new(p) for p in pts]
        res = bmesh.ops.convex_hull(tmp, input=vs)
        for g in res.get("geom_interior", []) + res.get("geom_unused", []):
            if isinstance(g, bmesh.types.BMVert) and g.is_valid:
                tmp.verts.remove(g)
        mapping = {}
        for v in tmp.verts:
            mapping[v] = self.bm.verts.new(v.co)
        for f in tmp.faces:
            try:
                self.face([mapping[v] for v in f.verts], surf)
            except ValueError:
                pass
        tmp.free()


# ---------------------------------------------------------------- helpers
def segments(part):
    pts = [Vector((p[0], p[1], 0)) for p in part["path"]]
    segs = list(zip(pts[:-1], pts[1:]))
    if part.get("closed"):
        segs.append((pts[-1], pts[0]))
    return segs


def frame_of(a, b):
    d = (b - a)
    L = d.length
    d.normalize()
    out = Vector((d.y, -d.x, 0))        # right-hand side = outside for counter-clockwise paths
    return d, out, L


def openings_of(part, seg_index):
    return sorted([o for o in part.get("openings", []) if o.get("seg", 0) == seg_index], key=lambda o: o["at"])


def intervals_without(L, cuts):
    """[0, L] minus the given (lo, hi) cuts -> remaining intervals."""
    out, x = [], 0.0
    for lo, hi in sorted(cuts):
        lo, hi = max(lo, 0.0), min(hi, L)
        if lo > x + 1e-4:
            out.append((x, lo))
        x = max(x, hi)
    if L > x + 1e-4:
        out.append((x, L))
    return out


def opening_trim(g, part, a, d, out, op, z, t, surf_default):
    """Frame, sill board, lintel, glass and mullions of one opening."""
    w, h, sill = op["width"], op["height"], op.get("sill", 0.0 if op.get("kind") == "door" else 0.9)
    c = a + d * op["at"]
    fr = op.get("frame", {"width": 0.09, "depth": 0.04})
    fw, fd = fr.get("width", 0.09), fr.get("depth", 0.04)
    fs = fr.get("surface", surf_default)
    depth = t + 2 * fd
    if fw > 0 and op.get("kind", "window") != "hole":
        zb, zt = z + sill, z + sill + h
        door = op.get("kind") == "door"
        jb, jt = (zb, zt + fw) if door else (zb - fw, zt + fw)
        for side in (-1, 1):   # jambs
            g.box(c + d * side * (w / 2 + fw / 2) + UP * ((jb + jt) / 2), (fw, depth, jt - jb), fs, d)
        g.box(c + UP * (zt + fw / 2), (w + 2 * fw, depth, fw), fs, d)              # head
        if op.get("kind") != "door":
            g.box(c + UP * (zb - fw / 2), (w + 2 * fw, depth, fw), fs, d)          # bottom of the frame
    if op.get("sill_board", op.get("kind") == "window"):
        g.box(c + out * (t / 2 + fd + 0.03) + UP * (z + sill - fw - 0.02),
              (w + 2 * fw + 0.12, 0.09, 0.04), fs, d)
    li = op.get("lintel")
    if li:
        lh = li.get("height", 0.18)
        ext = li.get("extend", 0.15)
        g.box(c + UP * (z + sill + h + fw + lh / 2), (w + 2 * fw + 2 * ext, t + 2 * li.get("depth", fd + 0.02), lh),
              li.get("surface", fs), d)
    if op.get("glass", op.get("kind") == "window"):
        g.box(c + UP * (z + sill + h / 2), (w, 0.012, h), "glass", d)
        cols, rows = op.get("mullions", [2, 2])
        mw = 0.035
        for i in range(1, cols):
            g.box(c + d * (-w / 2 + w * i / cols) + UP * (z + sill + h / 2), (mw, 0.05, h), fs, d)
        for j in range(1, rows):
            g.box(c + UP * (z + sill + h * j / rows), (w, 0.05, mw), fs, d)


# ---------------------------------------------------------------- parts
def part_wall(g, part, rng):
    H, t, z = part["height"], part.get("thickness", 0.2), part.get("z", 0.0)
    surf = part["surface"]
    plinth, cornice = part.get("plinth"), part.get("cornice")
    for si, (a, b) in enumerate(segments(part)):
        d, out, L = frame_of(a, b)
        ops = openings_of(part, si)
        ext0 = t / 2 if (si > 0 or part.get("closed")) else 0.0
        ext1 = t / 2 if (si < len(segments(part)) - 1 or part.get("closed")) else 0.0
        cuts = [(o["at"] - o["width"] / 2, o["at"] + o["width"] / 2) for o in ops]
        for lo, hi in intervals_without(L, cuts):
            lo2 = lo - (ext0 if lo <= 1e-4 else 0)
            hi2 = hi + (ext1 if hi >= L - 1e-4 else 0)
            g.box(a + d * ((lo2 + hi2) / 2) + UP * (z + H / 2), (hi2 - lo2, t, H), surf, d)
        for o in ops:
            sill = o.get("sill", 0.0 if o.get("kind") == "door" else 0.9)
            c = a + d * o["at"]
            if sill > 0:
                g.box(c + UP * (z + sill / 2), (o["width"], t, sill), surf, d)
            top = sill + o["height"]
            if top < H:
                g.box(c + UP * (z + (top + H) / 2), (o["width"], t, H - top), surf, d)
            opening_trim(g, part, a, d, out, o, z, t, part.get("trim_surface", surf))
        doors = [(o["at"] - o["width"] / 2, o["at"] + o["width"] / 2) for o in ops if o.get("kind") == "door"]
        if plinth:
            ph, pd = plinth.get("height", 0.35), plinth.get("depth", 0.04)
            for lo, hi in intervals_without(L, doors):
                lo2, hi2 = lo - (ext0 + pd if lo <= 1e-4 else 0), hi + (ext1 + pd if hi >= L - 1e-4 else 0)
                g.box(a + d * ((lo2 + hi2) / 2) + UP * (z + ph / 2), (hi2 - lo2, t + 2 * pd, ph),
                      plinth.get("surface", surf), d)
                g.box(a + d * ((lo2 + hi2) / 2) + UP * (z + ph + 0.015), (hi2 - lo2, t + pd, 0.03),
                      plinth.get("surface", surf), d)          # stepped cap: two shadow lines instead of one
        if cornice:
            ch, cd = cornice.get("height", 0.16), cornice.get("depth", 0.07)
            lo2, hi2 = -ext0 - cd, L + ext1 + cd
            g.box(a + d * ((lo2 + hi2) / 2) + UP * (z + H - ch / 2), (hi2 - lo2, t + 2 * cd, ch),
                  cornice.get("surface", surf), d)
            g.box(a + d * ((lo2 + hi2) / 2) + UP * (z + H - ch - 0.03), (hi2 - lo2 + 0 * cd, t + cd, 0.06),
                  cornice.get("surface", surf), d)
        for belt in part.get("belts", []):
            bh, bd = belt.get("height", 0.08), belt.get("depth", 0.03)
            for lo, hi in intervals_without(L, [(o["at"] - o["width"] / 2 - 0.1, o["at"] + o["width"] / 2 + 0.1)
                                               for o in ops if o.get("sill", 0) <= belt["z"] <= o.get("sill", 0) + o["height"]]):
                g.box(a + d * ((lo + hi) / 2) + UP * (z + belt["z"]), (hi - lo, t + 2 * bd, bh),
                      belt.get("surface", surf), d)
        pil = part.get("pilasters")
        if pil:
            every, pw, pd = pil.get("every", 2.0), pil.get("width", 0.24), pil.get("depth", 0.06)
            z0 = (plinth or {}).get("height", 0.0)
            z1 = H - (cornice or {}).get("height", 0.0)
            n = max(1, round(L / every))
            for k in range(n + 1):
                at = L * k / n
                if any(o["at"] - o["width"] / 2 - pw < at < o["at"] + o["width"] / 2 + pw for o in ops):
                    continue
                sides = {"out": [1], "in": [-1], "both": [1, -1]}[pil.get("sides", "out")]
                for s in sides:
                    g.box(a + d * at + out * s * (t / 2 + pd / 2) + UP * (z + (z0 + z1) / 2), (pw, pd, z1 - z0),
                          pil.get("surface", surf), d)
        sid = part.get("siding")
        if sid:
            siding(g, part, sid, a, d, out, L, ops, z, H, t, rng, (plinth or {}).get("height", 0.0),
                   (cornice or {}).get("height", 0.0))
        cb = part.get("corner_boards")
        if cb:
            for at, s in ((0.0, -1), (L, 1)):
                g.box(a + d * (at + s * (t / 2 + 0.005)) + out * (t / 2) + UP * (z + H / 2),
                      (cb.get("width", 0.12), 0.03, H), cb.get("surface", surf), d)


def siding(g, part, sid, a, d, out, L, ops, z, H, t, rng, z_lo, top_trim):
    kind, size, dep = sid.get("kind", "lap"), sid.get("size", 0.18), sid.get("depth", 0.022)
    surf = sid.get("surface", part["surface"])
    if kind == "lap":                       # overlapping horizontal boards, each tilted: real shadow lines
        zz = z_lo
        while zz < H - top_trim - 1e-3:
            bh = min(size * rng.uniform(0.97, 1.03), H - top_trim - zz)
            blocked = [(o["at"] - o["width"] / 2, o["at"] + o["width"] / 2) for o in ops
                       if o.get("sill", 0) < zz + bh and o.get("sill", 0) + o["height"] > zz]
            tilt = math.radians(sid.get("lift_deg", 4))
            zdir = Vector((0, 0, 1)) * math.cos(tilt) - out * math.sin(tilt)
            for lo, hi in intervals_without(L, blocked):
                g.box(a + d * ((lo + hi) / 2) + out * (t / 2 + dep / 2 + math.sin(tilt) * bh / 2) +
                      UP * (z + zz + bh / 2), (hi - lo, dep, bh * 1.04), surf, d, zdir)
            zz += bh
    else:                                   # board and batten: vertical battens over the wall face
        n = max(1, int(L / size))
        for k in range(n + 1):
            at = L * k / n
            for lo_z, hi_z in intervals_without(H - top_trim - z_lo, [(o.get("sill", 0) - z_lo, o.get("sill", 0) + o["height"] - z_lo)
                                                                     for o in ops if abs(o["at"] - at) < o["width"] / 2 + 0.05]):
                g.box(a + d * at + out * (t / 2 + dep / 2) + UP * (z + z_lo + (lo_z + hi_z) / 2),
                      (0.06, dep, hi_z - lo_z), surf, d)


def part_logwall(g, part, rng):
    H, z, D = part["height"], part.get("z", 0.0), part.get("log_d", 0.26)
    surf, end_surf = part["surface"], part.get("end_surface", part["surface"])
    over = part.get("overhang", 0.3)
    pitch = D * 0.88                     # logs sit in a scribed notch: courses overlap a little
    offset = part.get("course_offset", 0.0) * pitch
    jit = part.get("jitter", 0.025)
    segs = segments(part)
    for si, (a, b) in enumerate(segs):
        d, out, L = frame_of(a, b)
        ops = openings_of(part, si)
        ext0 = ext1 = over                # saddle-notched corners: every log end sticks out past the corner
        k = 0
        zc = z + D / 2 + offset
        while zc + D / 2 <= z + H + D * 0.3:
            r = D / 2 * (1 + rng.uniform(-jit, jit) * 4)
            blocked = [(o["at"] - o["width"] / 2, o["at"] + o["width"] / 2) for o in ops
                       if z + o.get("sill", 0) < zc + r * 0.7 and z + o.get("sill", 0) + o["height"] > zc - r * 0.7]
            for lo, hi in intervals_without(L, blocked):
                lo2 = lo - (ext0 * rng.uniform(0.85, 1.15) if lo <= 1e-4 else 0)
                hi2 = hi + (ext1 * rng.uniform(0.85, 1.15) if hi >= L - 1e-4 else 0)
                sag = Vector((0, 0, rng.uniform(-0.01, 0.01)))
                p0 = a + d * lo2 + UP * zc + out * rng.uniform(-jit, jit)
                p1 = a + d * hi2 + UP * zc + out * rng.uniform(-jit, jit) + sag
                g.cylinder(p0, p1, r, surf, sides=12, cap_surf=end_surf, r_end=r * rng.uniform(0.94, 1.0))
            if part.get("chink"):
                cz = zc + pitch / 2
                if cz < z + H:
                    for lo, hi in intervals_without(L, blocked):
                        for s in (1, -1):
                            g.box(a + d * ((lo + hi) / 2) + out * s * D * 0.22 + UP * cz, (hi - lo, 0.04, D * 0.22),
                                  part["chink"], d)
            zc += pitch
            k += 1
        for o in ops:
            opening_trim(g, part, a, d, out, o, z, D * 0.9, part.get("trim_surface", surf))


def part_slab(g, part, rng):
    pts = part["poly"]
    z, t = part.get("z", 0.0), part.get("thickness", 0.2)
    g.prism(pts, z - t, z, part["surface"])
    edge = part.get("edge")
    if edge:
        n = len(pts)
        for i in range(n):
            a, b = Vector((*pts[i], 0)), Vector((*pts[(i + 1) % n], 0))
            d, out, L = frame_of(a, b)
            eh = edge.get("height", t + 0.06)
            g.box(a + d * (L / 2) + out * 0.015 + UP * (z - eh / 2 + 0.01), (L + 0.03, 0.03, eh),
                  edge.get("surface", part["surface"]), d)


def part_box(g, part, rng):
    yaw = math.radians(part.get("yaw", 0))
    g.box(part["center"], part["size"], part["surface"], (math.cos(yaw), math.sin(yaw), 0))


def part_beam(g, part, rng):
    if "radius" in part:
        g.cylinder(part["from"], part["to"], part["radius"], part["surface"], sides=part.get("sides", 10))
    else:
        g.beam(part["from"], part["to"], part.get("size", [0.15, 0.15]), part["surface"])


def part_roof(g, part, rng):
    x0, y0, x1, y1 = part["rect"]
    ridge = part.get("ridge", "x")
    if ridge == "y":                     # build along x, then swap axes at the end
        x0, y0, x1, y1 = y0, x0, y1, x1
    kind = part.get("kind", "gable")
    pitch = math.radians(part.get("pitch", 35))
    o = part.get("overhang", 0.5)
    og = part.get("gable_overhang", o)
    t = part.get("thickness", 0.14)
    ez = part["eave_z"]
    surf = part["surface"]
    sub = Geo("tmp")
    span = (y1 - y0) / 2 if kind == "gable" else (y1 - y0)
    rise = span * math.tan(pitch)
    planes = [(-1, y0, y0 + span)] if kind == "shed" else [(-1, y0, (y0 + y1) / 2), (1, y1, (y0 + y1) / 2)]
    xa, xb = x0 - og, x1 + og
    for side, y_eave, y_ridge in planes:
        # slope runs from the eave (low) to the ridge (high); side -1 = south slope, 1 = north slope
        dy = y_ridge - y_eave
        sgn = 1 if dy > 0 else -1
        run = abs(dy) + o
        L = run / math.cos(pitch)
        slope_dir = Vector((0, sgn * math.cos(pitch), math.sin(pitch)))
        normal = Vector((0, -sgn * math.sin(pitch), math.cos(pitch)))
        start = Vector((0, y_eave - sgn * o, ez - o * math.tan(pitch)))        # outer eave line, top of roof
        centre = start + slope_dir * (L / 2) - normal * (t / 2)
        sub.box((Vector(((xa + xb) / 2, 0, 0)) + centre), (xb - xa, L, t), part.get("deck_surface", surf),
                (1, 0, 0), normal)
        sh = part.get("shingles")
        if sh:
            row = sh.get("row", 0.22)
            tab_lo, tab_hi = sh.get("tab", [0.22, 0.45])
            lift = math.radians(sh.get("lift_deg", 5))
            n_rows = int(L / row)
            for r_i in range(n_rows):
                s0 = L - (r_i + 1) * row          # rows from the ridge down: lower rows lie on top visually
                x = xa - rng.uniform(0, tab_hi)
                while x < xb:
                    w = min(rng.uniform(tab_lo, tab_hi), xb - x)
                    if w > 0.05:
                        tilt = lift + rng.uniform(-0.02, 0.02)
                        nz = normal * math.cos(tilt) - slope_dir * math.sin(tilt)
                        c = start + slope_dir * (s0 + row * 0.7) + normal * (0.012 + math.sin(tilt) * row * 0.7)
                        sub.box(Vector((max(x, xa) + w / 2, 0, 0)) + c,
                                (w - 0.012, row * 1.4, 0.022 * rng.uniform(0.8, 1.25)),
                                sh.get("surface", surf), (1, 0, 0), nz)
                    x += w
        if part.get("rafter_tails"):
            rt = part["rafter_tails"]
            every, (rw, rh) = rt.get("every", 0.6), rt.get("size", [0.07, 0.14])
            xs = np.arange(x0 + rw, x1, every)
            for x in xs:
                p_wall = Vector((x, y_eave + sgn * 0.0, ez)) - normal * (t + rh / 2)
                p_out = start + Vector((x, 0, 0)) - normal * (t + rh / 2) + slope_dir * 0.03
                sub.beam(p_out, p_wall + slope_dir * 0.4, (rw, rh), rt.get("surface", surf))
        fas = part.get("fascia")
        if fas is not False:
            fs = (fas or {}).get("surface", part.get("trim_surface", surf))
            fh = (fas or {}).get("height", 0.18)
            sub.box(start - normal * (t + fh / 2 - 0.03) + Vector(((xa + xb) / 2, -sgn * 0.02, 0)),
                    (xb - xa, 0.035, fh), fs, (1, 0, 0))
            for xe in (xa, xb):              # rake boards along the gable edges
                a_ = start + Vector((xe, 0, 0)) - normal * (fh / 2 - 0.03)
                b_ = a_ + slope_dir * L
                sub.beam(a_, b_, (0.035, fh), fs)
    if kind == "gable":
        if part.get("ridge_cap", True):
            rz = ez + rise
            sub.beam((xa - 0.02, (y0 + y1) / 2, rz + 0.03), (xb + 0.02, (y0 + y1) / 2, rz + 0.03), (0.2, 0.08),
                     part.get("ridge_surface", part.get("trim_surface", surf)))
        gf = part.get("gable_fill")
        if gf:
            wt = gf.get("thickness", 0.18)
            for xe, s in ((x0, 1), (x1, -1)):
                tri = [Vector((xe, y0, ez)), Vector((xe, y1, ez)), Vector((xe, (y0 + y1) / 2, ez + rise - t))]
                sub.solid([p + Vector((-wt / 2, 0, 0)) for p in tri], [p + Vector((wt / 2, 0, 0)) for p in tri],
                          gf.get("surface", surf))
    if ridge == "y":                     # swap x <-> y (mirror) and flip winding back
        for v in sub.bm.verts:
            v.co = Vector((v.co.y, v.co.x, v.co.z))
        bmesh.ops.reverse_faces(sub.bm, faces=sub.bm.faces)
    merge(g, sub)


def part_stairs(g, part, rng):
    yaw = math.radians(part.get("dir", 0))
    fwd = Vector((-math.sin(yaw), math.cos(yaw), 0))
    side = Vector((fwd.y, -fwd.x, 0))
    base = Vector(part["from"])
    w, rise, run, n = part.get("width", 1.0), part.get("rise", 0.18), part.get("run", 0.28), part["steps"]
    surf = part["surface"]
    for i in range(n):
        c = base + fwd * (run * i + run / 2) + UP * (rise * (i + 1) - 0.025)
        g.box(c, (w, run + 0.03, 0.05), surf, side)
        if part.get("risers", True):
            g.box(base + fwd * (run * i + 0.01) + UP * (rise * i + rise / 2), (w - 0.06, 0.02, rise), surf, side)
    if part.get("stringers", True):
        for s in (-1, 1):
            a = base + side * s * (w / 2 + 0.03) + UP * 0.05
            b = a + fwd * (run * n) + UP * (rise * n)
            g.beam(a, b, (0.05, 0.25), part.get("stringer_surface", surf))


def part_railing(g, part, rng):
    z, h = part.get("z", 0.0), part.get("height", 0.95)
    ps, pe = part.get("post_size", 0.1), part.get("post_every", 1.6)
    bs, be = part.get("baluster_size", 0.035), part.get("baluster_every", 0.14)
    surf = part["surface"]
    for a, b in segments(part):
        d, out, L = frame_of(a, b)
        n = max(1, round(L / pe))
        for k in range(n + 1):
            g.box(a + d * (L * k / n) + UP * (z + h / 2 + 0.05), (ps, ps, h + 0.1), surf, d)
        for rz in part.get("rails", [h - 0.03, 0.12]):
            g.box(a + d * (L / 2) + UP * (z + rz), (L, 0.06, 0.05), surf, d)
        if be:
            m = int(L / be)
            for k in range(1, m):
                at = L * k / m
                if min(abs(at - L * j / n) for j in range(n + 1)) < ps:
                    continue
                g.box(a + d * at + UP * (z + (0.12 + h - 0.03) / 2), (bs, bs, h - 0.15), surf, d)


def stone_ring(g, rng, corners, z0, z1, depth, surf, mortar):
    """Irregular stone courses along a closed polygon of (x, y) corners, on a recessed mortar core."""
    n = len(corners)
    zz = z0
    while zz < z1 - 0.02:
        ch = min(rng.uniform(0.16, 0.3), z1 - zz)
        for i in range(n):
            a, b = Vector((*corners[i], 0)), Vector((*corners[(i + 1) % n], 0))
            d, out, L = frame_of(a, b)
            x = -rng.uniform(0, 0.2)
            while x < L:
                w = min(rng.uniform(0.25, 0.55), L - x)
                if w > 0.08:
                    c = a + d * (max(x, 0) + w / 2) + out * (rng.uniform(-0.015, 0.02) - depth / 2 + 0.03) + \
                        UP * (zz + ch / 2 + rng.uniform(-0.01, 0.01))
                    sub_yaw = rng.uniform(-0.04, 0.04)
                    dd = Matrix.Rotation(sub_yaw, 3, "Z") @ d
                    g.box(c, (w - 0.02, depth * rng.uniform(0.8, 1.0), ch - 0.02), surf, dd)
                x += w
        zz += ch
    # mortar core slightly smaller than the stones' outer face
    g.prism([tuple(p) for p in shrink(corners, 0.015)], z0, z1 - 0.01, mortar)


def shrink(corners, inset):
    pts = [Vector((*c, 0)) for c in corners]
    n = len(pts)
    out = []
    for i in range(n):
        p, a, b = pts[i], pts[i - 1], pts[(i + 1) % n]
        n1 = Vector(((p - a).y, -(p - a).x, 0)).normalized()
        n2 = Vector(((b - p).y, -(b - p).x, 0)).normalized()
        m = (n1 + n2).normalized()
        out.append((p - m * inset / max(m.dot(n1), 0.3)).to_2d())
    return out


def part_chimney(g, part, rng):
    (cx, cy), (w, dpt) = part["pos"], part["size"]
    z0, z1 = part.get("z0", 0.0), part["z1"]
    surf, mortar = part["surface"], part.get("mortar", part["surface"])
    sh = part.get("shoulder")
    rect = lambda w_, d_: [(cx - w_ / 2, cy - d_ / 2), (cx + w_ / 2, cy - d_ / 2), (cx + w_ / 2, cy + d_ / 2),
                           (cx - w_ / 2, cy + d_ / 2)]
    if sh:
        stone_ring(g, rng, rect(w, dpt), z0, sh["z"], 0.3, surf, mortar)
        iw, idp = w - 2 * sh.get("inset", 0.2), dpt - 2 * sh.get("inset", 0.2)
        stone_ring(g, rng, rect(iw, idp), sh["z"], z1, 0.25, surf, mortar)
        g.box((cx, cy, sh["z"] + 0.04), (w + 0.04, dpt + 0.04, 0.08), part.get("cap_surface", surf))
    else:
        iw, idp = w, dpt
        stone_ring(g, rng, rect(w, dpt), z0, z1, 0.3, surf, mortar)
    if part.get("cap", True):
        g.box((cx, cy, z1 + 0.05), (iw + 0.12, idp + 0.12, 0.1), part.get("cap_surface", surf))
        g.box((cx, cy, z1 + 0.25), (0.22, 0.22, 0.3), part.get("flue_surface", part.get("cap_surface", surf)))


def part_foundation(g, part, rng):
    stone_ring(g, rng, part["poly"], part.get("z0", -0.2), part.get("z0", -0.2) + part["height"],
               part.get("thickness", 0.35), part["surface"], part.get("mortar", part["surface"]))


def rock(g, rng, centre, size, surf, flat=0.7):
    pts = []
    for _ in range(14):
        v = Vector(rng.normal(size=3)).normalized()
        v.x *= size * rng.uniform(0.7, 1.0)
        v.y *= size * rng.uniform(0.6, 1.0)
        v.z *= size * flat * rng.uniform(0.6, 1.0)
        pts.append(Vector(centre) + v)
    g.hull(pts, surf)


def part_chunks(g, part, rng):
    lo, hi = part.get("size", [0.12, 0.35])
    sink = part.get("sink", 0.35)
    spread = part.get("spread", 0.3)
    if "at" in part:
        anchors = [Vector(p) for p in part["at"]]
        for p in anchors:
            for _ in range(part.get("count", 4)):
                s = rng.uniform(lo, hi)
                off = Vector((rng.uniform(-spread, spread), rng.uniform(-spread, spread), 0))
                rock(g, rng, p + off + UP * (s * 0.7 * (0.5 - sink)), s, part["surface"])
    else:
        a, b = (Vector(p) for p in part["along"])
        for _ in range(part.get("count", 12)):
            s = rng.uniform(lo, hi)
            t = rng.uniform(0, 1)
            side = Vector(((b - a).y, -(b - a).x, 0)).normalized() * rng.uniform(-spread, spread)
            rock(g, rng, a.lerp(b, t) + side + UP * (s * 0.7 * (0.5 - sink)), s, part["surface"])


def part_pipe(g, part, rng):
    r = part.get("radius", 0.03)
    pts = [Vector(p) for p in part["path"]]
    surf = part["surface"]
    for a, b in zip(pts[:-1], pts[1:]):
        g.cylinder(a, b, r, surf, sides=10)
    for p in pts[1:-1]:                    # joints: short fat sleeves
        g.cylinder(p - UP * r * 1.2, p + UP * r * 1.2, r * 1.25, surf, sides=10)
    br = part.get("brackets")
    if br:
        ws = Vector((*part.get("wall_side", [0, 1]), 0)).normalized()
        for a, b in zip(pts[:-1], pts[1:]):
            L = (b - a).length
            for k in range(1, max(2, int(L / br) + 1)):
                p = a.lerp(b, k / max(1, int(L / br) + 1))
                g.box(p + ws * r * 1.5, (r * 2.6, r * 3, r * 1.2), surf, (ws.y, -ws.x, 0))


PARTS = {"wall": part_wall, "logwall": part_logwall, "slab": part_slab, "box": part_box, "beam": part_beam,
         "roof": part_roof, "stairs": part_stairs, "railing": part_railing, "chimney": part_chimney,
         "foundation": part_foundation, "chunks": part_chunks, "pipe": part_pipe}


def merge(dst, src):
    mapping = {}
    for v in src.bm.verts:
        mapping[v] = dst.bm.verts.new(v.co)
    for f in src.bm.faces:
        nf = dst.bm.faces.new([mapping[v] for v in f.verts])
        nf.material_index = dst.slot(src.slots[f.material_index])
        for l_src, l_dst in zip(f.loops, nf.loops):
            l_dst[dst.uv].uv = l_src[src.uv].uv
    src.bm.free()


# ---------------------------------------------------------------- finishing
def finish(g, bevel, corners, ground_z, rng):
    me = bpy.data.meshes.new(g.name)
    bmesh.ops.recalc_face_normals(g.bm, faces=g.bm.faces)     # every primitive is a closed solid
    g.bm.normal_update()
    g.bm.to_mesh(me)
    g.bm.free()
    obj = bpy.data.objects.new(g.name, me)
    bpy.context.collection.objects.link(obj)
    for s in g.slots:
        me.materials.append(bpy.data.materials.get(s) or bpy.data.materials.new(s))
    bpy.ops.object.select_all(action="DESELECT")
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    if bevel > 0:
        mod = obj.modifiers.new("bevel", "BEVEL")
        mod.width = bevel
        mod.segments = 1
        mod.limit_method = "ANGLE"
        mod.angle_limit = math.radians(40)
        mod.use_clamp_overlap = True
        bpy.ops.object.modifier_apply(modifier=mod.name)
    try:
        bpy.ops.object.shade_smooth_by_angle(angle=math.radians(35))
    except Exception:
        bpy.ops.object.shade_smooth()
    try:
        wn = obj.modifiers.new("wn", "WEIGHTED_NORMAL")
        wn.keep_sharp = True
        bpy.ops.object.modifier_apply(modifier=wn.name)
    except Exception:
        pass
    tri = obj.modifiers.new("tri", "TRIANGULATE")
    bpy.ops.object.modifier_apply(modifier=tri.name)
    # grime in vertex colour R: wall bases and building corners (the engine darkens / roughens there)
    co = np.empty(len(me.vertices) * 3, np.float32)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    zrel = co[:, 2] - ground_z
    grime = np.clip(1 - zrel / 0.9, 0, 1) ** 1.6
    if corners:
        cs = np.array(corners, np.float32)
        dist = np.min(np.linalg.norm(co[:, None, :2] - cs[None, :, :], axis=2), axis=1)
        grime = np.maximum(grime, np.clip(1 - dist / 0.45, 0, 1) ** 2 * 0.6)
    noise = np.sin(co[:, 0] * 3.1 + co[:, 1] * 1.7) * np.sin(co[:, 2] * 2.3 + co[:, 0] * 0.9)
    grime = np.clip(grime * (0.75 + 0.25 * noise) + rng.uniform(0, 0.05, len(grime)), 0, 1)
    attr = me.color_attributes.new("Col", "BYTE_COLOR", "POINT")
    rgba = np.ones((len(grime), 4), np.float32)
    rgba[:, 0] = grime
    rgba[:, 1] = 0
    rgba[:, 2] = 0
    attr.data.foreach_set("color", rgba.ravel())
    return obj


def export_fbx(obj, path):
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.export_scene.fbx(filepath=path, use_selection=True, object_types={"MESH"}, apply_unit_scale=True,
                             apply_scale_options="FBX_SCALE_NONE", axis_forward="-Z", axis_up="Y",
                             bake_space_transform=True, mesh_smooth_type="FACE", use_tspace=True,
                             use_mesh_modifiers=True, add_leaf_bones=False, path_mode="STRIP",
                             embed_textures=False, colors_type="LINEAR")


def surface_colour(name):
    h = (sum(map(ord, name)) * 37 % 1000) / 1000
    base = {"glass": (0.6, 0.75, 0.85)}.get(name)
    return base or (0.35 + 0.4 * h, 0.3 + 0.35 * ((h * 7) % 1), 0.25 + 0.3 * ((h * 13) % 1))


def preview(objs, path, centre, radius):
    scene = bpy.context.scene
    for engine in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
        try:
            scene.render.engine = engine
            break
        except TypeError:
            continue
    for o in objs:
        for slot in o.material_slots:
            m = slot.material
            m.use_nodes = True
            bsdf = next(n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
            bsdf.inputs["Base Color"].default_value = (*surface_colour(m.name), 1)
            bsdf.inputs["Roughness"].default_value = 0.7
    world = bpy.data.worlds.new("w")
    world.use_nodes = True
    next(n for n in world.node_tree.nodes if n.type == "BACKGROUND").inputs["Color"].default_value = (0.5, 0.55, 0.6, 1)
    scene.world = world
    sun = bpy.data.objects.new("sun", bpy.data.lights.new("sun", "SUN"))
    sun.data.energy = 4
    sun.rotation_euler = (math.radians(50), 0, math.radians(35))
    scene.collection.objects.link(sun)
    cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
    scene.collection.objects.link(cam)
    scene.camera = cam
    cam.data.angle = math.radians(55)
    scene.render.resolution_x, scene.render.resolution_y = 960, 640
    tiles = []
    for yaw, pitch in ((-35, 22), (145, 22), (-120, 10), (60, 35)):
        dirn = Vector((math.sin(math.radians(yaw)) * math.cos(math.radians(pitch)),
                       -math.cos(math.radians(yaw)) * math.cos(math.radians(pitch)), math.sin(math.radians(pitch))))
        cam.location = Vector(centre) + dirn * radius / math.tan(math.radians(27)) * 1.05
        cam.rotation_euler = (-dirn).to_track_quat("-Z", "Y").to_euler()
        scene.render.filepath = path + f".{yaw}.png"
        bpy.ops.render.render(write_still=True)
        tiles.append(img_read(scene.render.filepath)[..., :3])
        os.remove(scene.render.filepath)
    img_write(path, np.concatenate([np.concatenate(tiles[:2], 1), np.concatenate(tiles[2:], 1)], 0))
    bpy.data.objects.remove(sun)
    bpy.data.objects.remove(cam)


def build(world_dir, only=()):
    world = World(world_dir)
    spec = load(world.p("build", "architecture.json"))
    defaults = spec.get("defaults", {})
    out_dir = world.out("arch")
    os.makedirs(out_dir, exist_ok=True)
    manifest = load(os.path.join(out_dir, "manifest.json"), {"meshes": []})
    keep = [m for m in manifest["meshes"] if only and m["building"] not in only]
    bpy.ops.wm.read_factory_settings(use_empty=True)
    for b in spec["buildings"]:
        if only and b["id"] not in only:
            continue
        t0 = time.time()
        groups = {}
        corners = []
        for i, part in enumerate(b["parts"]):
            fn = PARTS.get(part.get("type"))
            if fn is None:
                raise ValueError(f"{b['id']} part {i}: unknown type {part.get('type')}")
            g = groups.setdefault(part.get("group", "main"), Geo(f"SM_{b['id']}_{part.get('group', 'main')}"))
            rng = np.random.default_rng(part.get("seed", i * 7919 + len(b["id"])))
            fn(g, part, rng)
            if part["type"] in ("wall", "logwall"):
                corners += [tuple(p[:2]) for p in part["path"]]
        objs = []
        for gname, g in groups.items():
            obj = finish(g, b.get("bevel", defaults.get("bevel", 0.012)), corners, b.get("ground_z", 0.0),
                         np.random.default_rng(3))
            fbx = os.path.join(out_dir, f"{g.name}.fbx")
            export_fbx(obj, fbx)
            tris = len(obj.data.polygons)
            dims = obj.dimensions
            keep.append({"name": g.name, "file": fbx, "building": b["id"], "group": gname,
                         "origin": b.get("origin", [0, 0, 0]), "yaw": b.get("yaw", 0),
                         "slots": [s.name for s in obj.data.materials], "tris": tris,
                         "bounds_m": [round(dims.x, 2), round(dims.y, 2), round(dims.z, 2)],
                         "space": b.get("space", b["id"])})
            objs.append(obj)
            print(f"{g.name}: {tris:,} tris, slots {[s.name for s in obj.data.materials]}", flush=True)
        lo = Vector([min(min((o.matrix_world @ Vector(c))[k] for c in o.bound_box) for o in objs) for k in range(3)])
        hi = Vector([max(max((o.matrix_world @ Vector(c))[k] for c in o.bound_box) for o in objs) for k in range(3)])
        preview(objs, os.path.join(out_dir, f"preview_{b['id']}.png"), (lo + hi) / 2, (hi - lo).length / 2)
        for o in objs:
            bpy.data.objects.remove(o)
        print(f"{b['id']}: {time.time() - t0:.1f}s, preview -> {os.path.join(out_dir, 'preview_' + b['id'] + '.png')}",
              flush=True)
        world.log({"step": "arch", "building": b["id"], "seconds": round(time.time() - t0, 1)})
    save(os.path.join(out_dir, "manifest.json"), {"meshes": keep})


if __name__ == "__main__":
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    build(argv[0], set(argv[1:]))
