"""Library items -> engine-ready files in <item>/game/ (build phase, Blender), plus a report of what needs a look.

  batch (headless):  python w2w.py bake <world> [id ...] [--retexture]
                     = blender -b --factory-startup -P blender/bake_items.py -- <world_dir> [--retexture] [id ...]
  tune  (live Blender through the Blender MCP, one item at a time, then `look`):
        import runpy; g = runpy.run_path(r"<repo>/blender/bake_items.py")
        print(g["tune"](r"<world_dir>", "pine_chair", decimate="planar", cage=0.012))
        Runs in a scene called W2W_Tune (other scenes untouched). Write good values into the item's "bake" block
        or the world's plan "bake_presets".

Kinds handled here:
  prop  3d/model.glb (TRELLIS) -> clean (inner shells, interior faces, flipped normals), real size, pivot at the
        bottom centre, yaw_fix, LOD0 budget, AO / curvature / normal bakes, weathering by "condition",
        LODs, UCX collision -> SM_<id>.fbx (+ _LOD<n>.fbx), T_SM_<id>_{BC,N,ORM}.png, preview.png
  slab  picked 2D image on a thin framed board (posters, photos, paintings, screens); pivot at the back bottom
  card  RGBA cut-out (grass, fern, flowers, small plants) on crossed quads; masked two-sided foliage material
        item "card": {"style": "cross" | "flat" | "fan", "planes": 3}

Status per item in the report: ok (trust it) | review (hero tier: always look) | check (flags) | fail.

Item fields used: size {"height" | "width" | "depth"} (metres; several = per-axis), tier hero|prop|small,
preset clutter|hard_surface|organic|hero, bake {...overrides}, condition new|used|worn|ruined (or wear / dirt /
rough_var / bare_metal), yaw_fix, collision box|convex|none, emissive, aspect + frame (slabs), surfaces [heights of
shelf levels, for stacking in scene.json].
"""
import math
import os
import sys
import time
import traceback

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import texlib as tx
from bake_io import img_read, img_write
from common import LOD_RATIOS, LOD_SCREEN, TEXEL_DENSITY, World, load, save

LIVE = not bpy.app.background

# Every key can be overridden in plan/world.json "bake_presets": {"<preset>": {...}} and per item in "bake".
#   hidden     delete enclosed inner shells   interior  delete interior faces
#   normals    auto | recalc | keep
#   remesh     voxel size as a fraction of the object size (0 = off; forces uv "smart")
#   faces      LOD0 triangle budget (0 = keep TRELLIS's count)
#   decimate   planar (dissolve flat areas first: hard surface) | collapse (organic)
#   uv         keep | smart          cage / ray   bake cage extrusion / max ray distance, fraction of size
#   delight    0-1 flatten lighting baked into the albedo      albedo_normal  micro normal strength
PRESETS = {
    "clutter": {"tier": "auto", "interior": True, "normals": "auto", "remesh": 0.0, "faces": 0,
                "decimate": "collapse", "planar_angle": 4.0, "uv": "keep", "cage": 0.01, "ray": 0.03,
                "delight": 0.0, "albedo_normal": 0.5},
    "hard_surface": {"tier": "auto", "interior": True, "normals": "auto", "remesh": 0.0, "faces": 0,
                     "decimate": "planar", "planar_angle": 4.0, "uv": "keep", "cage": 0.01, "ray": 0.03,
                     "delight": 0.2, "albedo_normal": 0.6},
    "organic": {"tier": "auto", "interior": True, "normals": "auto", "remesh": 0.0, "faces": 0,
                "decimate": "collapse", "planar_angle": 4.0, "uv": "keep", "cage": 0.015, "ray": 0.04,
                "delight": 0.3, "albedo_normal": 0.8},
    "hero": {"tier": "review", "interior": True, "normals": "auto", "remesh": 0.0, "faces": 0,
             "decimate": "planar", "planar_angle": 2.0, "uv": "keep", "cage": 0.008, "ray": 0.025,
             "delight": 0.25, "albedo_normal": 0.6},
}
CATEGORY_PRESET = {"small": "clutter", "prop": "hard_surface", "hero": "hero"}
# report thresholds
AO_BLACK_MAX = 0.10        # share of used texels that are never lit (AO < 0.02)
NORMAL_BAD_MAX = 0.04      # share of used texels whose baked normal tilts more than ~40 deg
INVERTED_MAX = 0.03        # share of sampled faces that face into the mesh
LOD_DRIFT_MAX = 0.04       # bounding-box change of the last LOD relative to LOD0


# ---------------------------------------------------------------- scene helpers
def reset():
    """Headless: a factory-empty file. Live: a dedicated W2W_Tune scene, emptied (other scenes untouched)."""
    if LIVE:
        scene = bpy.data.scenes.get("W2W_Tune") or bpy.data.scenes.new("W2W_Tune")
        for o in list(scene.objects):
            bpy.data.objects.remove(o)
        bpy.context.window.scene = scene
    else:
        bpy.ops.wm.read_factory_settings(use_empty=True)
        scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 64
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        for kind in ("OPTIX", "CUDA", "HIP", "METAL", "ONEAPI"):
            try:
                prefs.compute_device_type = kind
                prefs.get_devices()
                if any(d.type == kind for d in prefs.devices):
                    for d in prefs.devices:
                        d.use = True
                    scene.cycles.device = "GPU"
                    break
            except TypeError:
                continue
    except Exception:
        pass


def select_only(*objs):
    if bpy.context.object and bpy.context.object.mode != "OBJECT":
        bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]


def edit_op(obj, *ops):
    """Run mesh operators in edit mode on obj, starting with nothing selected."""
    select_only(obj)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="DESELECT")
    for op in ops:
        op()
    bpy.ops.object.mode_set(mode="OBJECT")


def import_joined(path, name):
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=path)
    new = [o for o in bpy.data.objects if o not in before]
    meshes = [o for o in new if o.type == "MESH"]
    select_only(*meshes)
    for o in meshes:
        mw = o.matrix_world.copy()
        o.parent = None
        o.matrix_world = mw
    if len(meshes) > 1:
        bpy.ops.object.join()
    obj = bpy.context.view_layer.objects.active
    for o in new:
        if o != obj:
            bpy.data.objects.remove(o)
    obj.rotation_mode = "XYZ"
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    obj.name = obj.data.name = name
    return obj


def duplicate(obj, name):
    o = obj.copy()
    o.data = obj.data.copy()
    o.name = o.data.name = name
    bpy.context.collection.objects.link(o)
    return o


def tri_count(obj):
    return sum(len(p.vertices) - 2 for p in obj.data.polygons)


def bbox(obj):
    lo = Vector([min(v.co[i] for v in obj.data.vertices) for i in range(3)])
    hi = Vector([max(v.co[i] for v in obj.data.vertices) for i in range(3)])
    return lo, hi


def normalise_matrix(obj, spec):
    """Matrix that applies yaw_fix, scales to the real size and puts the pivot at the bottom centre."""
    rot = Matrix.Rotation(math.radians(spec.get("yaw_fix", 0)), 4, "Z")
    pts = [rot @ v.co for v in obj.data.vertices]
    lo = Vector([min(p[i] for p in pts) for i in range(3)])
    hi = Vector([max(p[i] for p in pts) for i in range(3)])
    dims = hi - lo
    given = {axis: spec[key] / max(dims[axis], 1e-6)
             for key, axis in (("height_m", 2), ("width_m", 0), ("depth_m", 1)) if key in spec}
    if not given:
        raise ValueError(f"{spec['id']}: item.json needs size {height: ...} (or width / depth)")
    # one size: uniform scale. Several sizes: each given axis is scaled to its own size (TRELLIS fattens thin
    # or oddly proportioned objects into near-cubes), the others follow the first given one
    first = given[next(a for a in (2, 0, 1) if a in given)]
    sx, sy, sz = (given.get(a, first) for a in (0, 1, 2))
    centre = Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z))
    return Matrix.Diagonal((sx, sy, sz, 1.0)) @ Matrix.Translation(-centre) @ rot


# ---------------------------------------------------------------- cleanup
def inverted_ratio(obj, samples=2000):
    """Share of faces whose normal points into the mesh (a ray along the normal hits a back face)."""
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.normal_update()
    tree = BVHTree.FromBMesh(bm)
    faces = list(bm.faces)
    rng = np.random.default_rng(0)
    pick = rng.choice(len(faces), min(samples, len(faces)), replace=False) if faces else []
    bad = 0
    eps = max(obj.dimensions) * 1e-4
    for i in pick:
        f = faces[i]
        c, n = f.calc_center_median(), f.normal
        hit = tree.ray_cast(c + n * eps, n, max(obj.dimensions) * 2)
        bad += hit[0] is not None and hit[1].dot(n) > 0
    bm.free()
    return bad / max(len(pick), 1)


def hidden_shells(bm, size, samples=60, rays=12):
    """Faces of connected components that no ray can leave: closed shells TRELLIS leaves inside the model.
    They are never seen but cost triangles and texture space (half of a model, measured on real outputs)."""
    bm.faces.ensure_lookup_table()
    tree = BVHTree.FromBMesh(bm)
    comp = [-1] * len(bm.faces)
    groups = []
    for f in bm.faces:
        if comp[f.index] >= 0:
            continue
        stack, group = [f], []
        comp[f.index] = len(groups)
        while stack:
            g = stack.pop()
            group.append(g)
            for e in g.edges:
                for h in e.link_faces:
                    if comp[h.index] < 0:
                        comp[h.index] = len(groups)
                        stack.append(h)
        groups.append(group)
    if len(groups) < 2:
        return []
    rng = np.random.default_rng(0)
    reach = size * 3
    hidden = []
    for group in groups:
        escaped = 0
        for i in rng.choice(len(group), min(samples, len(group)), replace=False):
            f = group[i]
            c = f.calc_center_median()
            for _ in range(rays):
                d = Vector(rng.normal(size=3)).normalized()
                if d.dot(f.normal) < 0:
                    d = -d
                escaped += tree.ray_cast(c + d * size * 1e-4, d, reach)[0] is None
                if escaped:
                    break
            if escaped:
                break
        if not escaped:
            hidden.extend(group)
    if len(hidden) == len(bm.faces):
        return []
    return hidden


def clean(obj, p, stats):
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    size = max(obj.dimensions)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=size * 1e-5)
    if p.get("hidden", True):
        hidden = hidden_shells(bm, size)
        bmesh.ops.delete(bm, geom=hidden, context="FACES")
        stats["hidden_faces_removed"] = len(hidden)
    loose = [v for v in bm.verts if not v.link_faces]
    bmesh.ops.delete(bm, geom=loose, context="VERTS")
    bm.to_mesh(obj.data)
    bm.free()
    if p["interior"]:
        before = len(obj.data.polygons)
        edit_op(obj, bpy.ops.mesh.select_interior_faces, lambda: bpy.ops.mesh.delete(type="FACE"))
        stats["interior_faces_removed"] = before - len(obj.data.polygons)
    if p["normals"] in ("auto", "recalc"):
        bad = inverted_ratio(obj)
        stats["inverted_before"] = round(bad, 3)
        stats["normals"] = "kept"
        if p["normals"] == "recalc" or bad > 0.01:
            backup = obj.data.copy()
            edit_op(obj, lambda: bpy.ops.mesh.select_all(action="SELECT"),
                    lambda: bpy.ops.mesh.normals_make_consistent(inside=False))
            after = inverted_ratio(obj)
            if p["normals"] == "auto" and after >= bad:      # recalculating can make open shells worse
                old = obj.data
                obj.data = backup
                bpy.data.meshes.remove(old)
                after = bad
            else:
                stats["normals"] = "recalculated"
                bpy.data.meshes.remove(backup)
            bad = after
        stats["inverted_after"] = round(bad, 3)
    if p["remesh"] > 0:
        mod = obj.modifiers.new("remesh", "REMESH")
        mod.mode = "VOXEL"
        mod.voxel_size = p["remesh"] * size
        select_only(obj)
        bpy.ops.object.modifier_apply(modifier=mod.name)
        stats["remeshed"] = True
    if p["faces"] and tri_count(obj) > p["faces"]:
        reduce(obj, p["faces"], p)
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bmesh.ops.triangulate(bm, faces=bm.faces, quad_method="BEAUTY", ngon_method="BEAUTY")
    for f in bm.faces:
        f.smooth = True
    for e in bm.edges:
        e.smooth = e.is_manifold and e.calc_face_angle(0) <= math.radians(40)
    bm.to_mesh(obj.data)
    bm.free()


def reduce(obj, target_tris, p):
    """Bring obj down to about target_tris: planar dissolve first for hard surfaces, then collapse."""
    select_only(obj)
    if p["decimate"] == "planar":
        mod = obj.modifiers.new("planar", "DECIMATE")
        mod.decimate_type = "DISSOLVE"
        mod.angle_limit = math.radians(p["planar_angle"])
        mod.use_dissolve_boundaries = False
        bpy.ops.object.modifier_apply(modifier=mod.name)
        tri = obj.modifiers.new("tri", "TRIANGULATE")
        bpy.ops.object.modifier_apply(modifier=tri.name)
    now = tri_count(obj)
    if now > target_tris:
        mod = obj.modifiers.new("collapse", "DECIMATE")
        mod.ratio = target_tris / now
        mod.use_collapse_triangulate = True
        bpy.ops.object.modifier_apply(modifier=mod.name)


# ---------------------------------------------------------------- textures and bakes
def curvature_attribute(obj):
    """Per-vertex convexity (edges) and concavity (cavities) from signed dihedral angles, smoothed twice."""
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    bm.verts.ensure_lookup_table()
    curv = np.zeros(len(bm.verts), np.float32)
    for v in bm.verts:
        angles = [e.calc_face_angle_signed(0.0) for e in v.link_edges if e.is_manifold]
        curv[v.index] = sum(angles) / len(angles) if angles else 0.0
    nbrs = [[e.other_vert(v).index for e in v.link_edges] for v in bm.verts]
    for _ in range(2):
        curv = np.array([(curv[i] + curv[n].mean()) / 2 if n else curv[i] for i, n in enumerate(nbrs)], np.float32)
    bm.free()
    # low-poly meshes have small dihedral angles everywhere: only the strongest ~10% count as edges / cavities
    pos, neg = curv[curv > 0], -curv[curv < 0]
    ps = max(np.percentile(pos, 90) if len(pos) else 0, 0.25)
    ns = max(np.percentile(neg, 90) if len(neg) else 0, 0.25)
    attr = obj.data.color_attributes.new("w2w_curv", "FLOAT_COLOR", "POINT")
    rgba = np.zeros((len(curv), 4), np.float32)
    rgba[:, 0] = np.clip((curv - ps * 0.35) / (ps * 0.65), 0, 1)
    rgba[:, 1] = np.clip((-curv - ns * 0.35) / (ns * 0.65), 0, 1)
    rgba[:, 3] = 1
    attr.data.foreach_set("color", rgba.ravel())


def texture_size(obj, tier, src_size):
    """Edge length giving the category's texel density over this mesh's UV layout (power of two)."""
    me = obj.data
    uv = me.uv_layers.active.data
    area3d = uv_area = 0.0
    for poly in me.polygons:
        area3d += poly.area
        pts = [uv[i].uv for i in poly.loop_indices]
        for k in range(1, len(pts) - 1):
            a, b = pts[k] - pts[0], pts[k + 1] - pts[0]
            uv_area += abs(a.x * b.y - a.y * b.x) / 2
    want = TEXEL_DENSITY[tier] * math.sqrt(area3d / max(uv_area, 1e-6))
    size = 2 ** round(math.log2(max(want, 1)))
    size = int(min(max(size, 256), 4096, max(src_size, 512)))
    return size, round(size * math.sqrt(uv_area / max(area3d, 1e-9)))


def emission_material(name, build):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    emit = nt.nodes.new("ShaderNodeEmission")
    nt.links.new(emit.outputs["Emission"], out.inputs["Surface"])
    build(nt, emit.inputs["Color"])
    return m


def mask_material(height):
    """R = convex edges, G = cavities (curvature attribute), B = 3D noise in object space."""
    def build(nt, color_in):
        attr = nt.nodes.new("ShaderNodeAttribute")
        attr.attribute_name = "w2w_curv"
        sep = nt.nodes.new("ShaderNodeSeparateColor")
        comb = nt.nodes.new("ShaderNodeCombineColor")
        noise = nt.nodes.new("ShaderNodeTexNoise")
        noise.inputs["Scale"].default_value = 3.0 / max(height, 0.05)
        noise.inputs["Detail"].default_value = 6
        coord = nt.nodes.new("ShaderNodeTexCoord")
        nt.links.new(coord.outputs["Object"], noise.inputs["Vector"])
        nt.links.new(attr.outputs["Color"], sep.inputs["Color"])
        nt.links.new(sep.outputs[0], comb.inputs[0])
        nt.links.new(sep.outputs[1], comb.inputs[1])
        nt.links.new(noise.outputs["Fac"], comb.inputs[2])
        nt.links.new(comb.outputs["Color"], color_in)
    return emission_material("w2w_masks", build)


def height_material(height):
    def build(nt, color_in):
        coord = nt.nodes.new("ShaderNodeTexCoord")
        sep = nt.nodes.new("ShaderNodeSeparateXYZ")
        div = nt.nodes.new("ShaderNodeMath")
        div.operation = "DIVIDE"
        div.inputs[1].default_value = max(height, 1e-3)
        nt.links.new(coord.outputs["Object"], sep.inputs["Vector"])
        nt.links.new(sep.outputs["Z"], div.inputs[0])
        nt.links.new(div.outputs[0], color_in)
    return emission_material("w2w_height", build)


def white_material():
    return emission_material("w2w_white", lambda nt, c: setattr(c, "default_value", (1, 1, 1, 1)))


def image_material(image):
    def build(nt, color_in):
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = image
        nt.links.new(tex.outputs["Color"], color_in)
    return emission_material("w2w_img", build)


def bake(obj, kind, size, path, high=None, material_override=None, margin=16, cage=0.01, ray=0.03):
    img = bpy.data.images.new(f"bake_{kind}", size, size, alpha=False, float_buffer=False)
    img.colorspace_settings.name = "Non-Color"
    saved = [s.material for s in obj.material_slots]
    if material_override:
        for s in obj.material_slots:
            s.material = material_override
    mats = {s.material for s in obj.material_slots}
    for m in mats:
        node = m.node_tree.nodes.new("ShaderNodeTexImage")
        node.image = img
        m.node_tree.nodes.active = node
    scene = bpy.context.scene
    scene.render.bake.margin = margin
    if high:
        select_only(high, obj)
        bpy.context.view_layer.objects.active = obj
        scene.render.bake.use_selected_to_active = True
        scene.render.bake.cage_extrusion = max(obj.dimensions) * cage
        scene.render.bake.max_ray_distance = max(obj.dimensions) * ray
    else:
        select_only(obj)
        scene.render.bake.use_selected_to_active = False
    scene.cycles.samples = 1 if kind == "EMIT" else 64
    bpy.ops.object.bake(type=kind, normal_space="TANGENT", use_clear=True)
    img.filepath_raw = path
    img.file_format = "PNG"
    img.save()
    for m in mats:
        m.node_tree.nodes.remove(m.node_tree.nodes.active)
    for s, m in zip(obj.material_slots, saved):
        s.material = m
    bpy.data.images.remove(img)


def gltf_images(obj):
    """(base colour image, metallic-roughness image or None) from an imported glTF material."""
    mat = obj.material_slots[0].material
    bsdf = next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")

    def source(socket):
        stack = [socket]
        while stack:
            s = stack.pop()
            for link in s.links:
                if link.from_node.type == "TEX_IMAGE":
                    return link.from_node.image
                stack.extend(i for i in link.from_node.inputs if i.is_linked)
        return None

    return source(bsdf.inputs["Base Color"]), source(bsdf.inputs["Roughness"])


def set_single_material(obj, name):
    for _ in range(len(obj.material_slots)):
        obj.data.materials.pop()
    obj.data.materials.append(bpy.data.materials.new(name))


# ---------------------------------------------------------------- LODs, collision, export
def make_lods(obj, base, ratios, p):
    lods = [obj]
    tris0 = tri_count(obj)
    for i, r in enumerate(ratios[1:], 1):
        o = duplicate(obj, f"{base}_LOD{i}")
        reduce(o, max(12, int(tris0 * r)), p)
        lods.append(o)
    obj.name = obj.data.name = base
    return lods


def make_collision(obj, base, kind):
    if kind == "none":
        return None
    bm = bmesh.new()
    if kind == "convex":
        src = bmesh.new()
        src.from_mesh(obj.data)
        bmesh.ops.convex_hull(src, input=src.verts)
        hull = bpy.data.meshes.new("hull")
        src.to_mesh(hull)
        src.free()
        tmp = bpy.data.objects.new("hull", hull)
        bpy.context.collection.objects.link(tmp)
        if len(hull.polygons) > 120:
            mod = tmp.modifiers.new("dec", "DECIMATE")
            mod.ratio = 120 / len(hull.polygons)
            select_only(tmp)
            bpy.ops.object.modifier_apply(modifier=mod.name)
        bm.from_mesh(tmp.data)
        bmesh.ops.convex_hull(bm, input=bm.verts)
        bpy.data.objects.remove(tmp)
    else:
        lo, hi = bbox(obj)
        bmesh.ops.create_cube(bm, size=1.0, matrix=Matrix.Translation((lo + hi) / 2) @ Matrix.Diagonal((*(hi - lo), 1)))
    me = bpy.data.meshes.new(f"UCX_{base}_00")
    bm.to_mesh(me)
    bm.free()
    col = bpy.data.objects.new(f"UCX_{base}_00", me)
    bpy.context.collection.objects.link(col)
    return col


def export_fbx(objs, path):
    select_only(*objs)
    bpy.ops.export_scene.fbx(filepath=path, use_selection=True, object_types={"MESH"}, apply_unit_scale=True,
                             apply_scale_options="FBX_SCALE_NONE", axis_forward="-Z", axis_up="Y",
                             bake_space_transform=True, mesh_smooth_type="FACE", use_tspace=True,
                             use_mesh_modifiers=True, add_leaf_bones=False, path_mode="STRIP",
                             embed_textures=False)


# ---------------------------------------------------------------- image slabs
def build_slab(spec, base):
    h = spec.get("height_m") or spec["width_m"] / spec.get("aspect", 1.0)
    w = spec.get("width_m") or h * spec.get("aspect", 1.0)
    fr = {"depth": 0.03, "border": 0.02, "color": [0.04, 0.04, 0.045], **spec.get("frame", {})}
    d, b = fr["depth"], fr["border"]
    bm = bmesh.new()
    # frame: a box behind the picture, slightly larger than it; picture plane 1 mm in front of the frame
    bmesh.ops.create_cube(bm, size=1.0, matrix=Matrix.Translation((0, -d / 2, (h + 2 * b) / 2)) @
                          Matrix.Diagonal((w + 2 * b, d, h + 2 * b, 1)))
    for f in bm.faces:
        f.material_index = 1
    v = [bm.verts.new(p) for p in ((-w / 2, -d - 0.001, b), (w / 2, -d - 0.001, b),
                                   (w / 2, -d - 0.001, b + h), (-w / 2, -d - 0.001, b + h))]
    face = bm.faces.new(v)
    face.material_index = 0
    uv = bm.loops.layers.uv.new("UVMap")
    for loop, co in zip(face.loops, ((0, 0), (1, 0), (1, 1), (0, 1))):
        loop[uv].uv = co
    bmesh.ops.triangulate(bm, faces=bm.faces)
    me = bpy.data.meshes.new(base)
    bm.to_mesh(me)
    bm.free()
    obj = bpy.data.objects.new(base, me)
    bpy.context.collection.objects.link(obj)
    me.materials.append(bpy.data.materials.new(f"M_{spec['id']}"))
    me.materials.append(bpy.data.materials.new(f"M_{spec['id']}_Frame"))
    return obj, fr


# ---------------------------------------------------------------- preview
def preview(obj, bc_path, out_path, slot_tints=()):
    scene = bpy.context.scene
    for o in scene.objects:
        o.hide_render = o != obj
    engine_before = scene.render.engine
    for engine in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
        try:
            scene.render.engine = engine
            break
        except TypeError:
            continue
    mat = bpy.data.materials.new("preview")
    mat.use_nodes = True
    bsdf = next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
    tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
    tex.image = bpy.data.images.load(bc_path, check_existing=False)
    mat.node_tree.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = 0.55
    saved = [s.material for s in obj.material_slots]
    obj.material_slots[0].material = mat
    for s, tint in zip(obj.material_slots[1:], slot_tints):
        s.material = bpy.data.materials.new("preview_tint")
        s.material.use_nodes = True
        b = next(n for n in s.material.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
        b.inputs["Base Color"].default_value = (*tint, 1)
    world = bpy.data.worlds.new("w")
    world.use_nodes = True
    bg = next(n for n in world.node_tree.nodes if n.type == "BACKGROUND")
    bg.inputs["Color"].default_value = (0.35, 0.38, 0.42, 1)
    scene.world = world
    sun = bpy.data.objects.new("sun", bpy.data.lights.new("sun", "SUN"))
    sun.data.energy = 3.5
    sun.rotation_euler = (math.radians(50), 0, math.radians(30))
    scene.collection.objects.link(sun)
    cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
    scene.collection.objects.link(cam)
    scene.camera = cam
    scene.render.resolution_x = scene.render.resolution_y = 512
    lo, hi = bbox(obj)
    centre, radius = (lo + hi) / 2, (hi - lo).length / 2
    tiles = []
    for yaw, pitch in ((-35, 20), (145, 20), (-90, 5), (0, 5)):   # front 3/4, back 3/4, side, front
        dist = radius / math.tan(math.radians(25)) * 1.2
        direction = Vector((math.sin(math.radians(yaw)) * math.cos(math.radians(pitch)),
                            -math.cos(math.radians(yaw)) * math.cos(math.radians(pitch)),
                            math.sin(math.radians(pitch))))
        cam.location = centre + direction * dist
        cam.rotation_euler = (-direction).to_track_quat("-Z", "Y").to_euler()
        cam.data.angle = math.radians(50)
        scene.render.filepath = out_path + f".{yaw}.png"
        bpy.ops.render.render(write_still=True)
        tiles.append(img_read(scene.render.filepath)[..., :3])
        os.remove(scene.render.filepath)
    img_write(out_path, np.concatenate([np.concatenate(tiles[:2], 1), np.concatenate(tiles[2:], 1)], 0))
    for s, m in zip(obj.material_slots, saved):
        s.material = m
    for o in (sun, cam):
        bpy.data.objects.remove(o)
    for o in scene.objects:
        o.hide_render = False
    scene.render.engine = engine_before


# ---------------------------------------------------------------- cards (foliage cut-outs)
def build_card(spec, base, aspect):
    """Crossed vertical quads (cross / fan) or one quad (flat), pivot at the bottom centre, UV = whole image."""
    h = spec.get("height_m") or spec["width_m"] / aspect
    w = h * aspect
    style = spec.get("card", {}).get("style", "cross")
    planes = 1 if style == "flat" else int(spec.get("card", {}).get("planes", 3))
    bm = bmesh.new()
    uv = bm.loops.layers.uv.new("UVMap")
    rng = np.random.default_rng(len(base))
    for i in range(planes):
        yaw = math.pi * i / planes + (rng.uniform(-0.2, 0.2) if planes > 1 else 0)
        tilt = math.radians(14) if style == "fan" and planes > 1 else 0.0
        c, s_ = math.cos(yaw), math.sin(yaw)
        top_off = Vector((-s_, c, 0)) * math.sin(tilt) * h * (1 if i % 2 else -1)
        pts = [Vector((-w / 2 * c, -w / 2 * s_, 0)), Vector((w / 2 * c, w / 2 * s_, 0)),
               Vector((w / 2 * c, w / 2 * s_, h)) + top_off, Vector((-w / 2 * c, -w / 2 * s_, h)) + top_off]
        f = bm.faces.new([bm.verts.new(p) for p in pts])
        for loop, co in zip(f.loops, ((0, 0), (1, 0), (1, 1), (0, 1))):
            loop[uv].uv = co
    bmesh.ops.triangulate(bm, faces=bm.faces)
    me = bpy.data.meshes.new(base)
    bm.to_mesh(me)
    bm.free()
    try:   # normals pointing up: lit grass reads soft instead of showing every crossed plane
        me.normals_split_custom_set_from_vertices([(0.0, 0.0, 1.0)] * len(me.vertices))
    except Exception:
        pass
    obj = bpy.data.objects.new(base, me)
    bpy.context.collection.objects.link(obj)
    me.materials.append(bpy.data.materials.new(f"M_{spec['id']}"))
    return obj


# ---------------------------------------------------------------- one item
def legacy_spec(it):
    """The item.json fields in the flat form the helpers above use (size dict -> height_m / width_m / depth_m)."""
    s = dict(it.spec)
    size = it.get("size", {})
    if isinstance(size, dict):
        for k in ("height", "width", "depth"):
            if k in size:
                s[f"{k}_m"] = float(size[k])
    return s


def preset_for(world, it, spec, overrides=None):
    name = spec.get("preset") or CATEGORY_PRESET.get(it.tier, "hard_surface")
    p = {**PRESETS[name], **world.plan.get("bake_presets", {}).get(name, {}), **spec.get("bake", {}),
         **(overrides or {})}
    p["preset"] = name
    return p


def process_card(it, spec, base):
    rgba_src = img_read(it.f("2d", "rgba.png"))
    ys, xs = np.nonzero(rgba_src[..., 3] > 0.1)
    if not len(xs):
        raise RuntimeError("rgba.png is empty")
    pad = 4
    crop = rgba_src[max(ys.min() - pad, 0):ys.max() + pad, max(xs.min() - pad, 0):xs.max() + pad]
    obj = build_card(spec, base, crop.shape[1] / crop.shape[0])
    size = tx.pow2_at_most(max(crop.shape[:2]), 256, 1024)
    # square texture: the UVs span the whole image on a quad of the right aspect (no Pillow inside Blender)
    tmp = it.f("3d", "bake", "card_crop.png")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    img_write(tmp, crop)
    crop = img_read(tmp, size)
    alpha = crop[..., 3]
    bc = np.concatenate([tx.dilate_color(tx.clamp_albedo(crop[..., :3]), alpha, 16), alpha[..., None]], axis=2)
    lum = tx.luminance(bc[..., :3])
    normal = tx.normal_from_height(lum - tx.blur(lum, size / 64), strength=0.8, wrap=False)
    # darker toward the base: fakes the self-shadowing inside a real clump
    ao = np.broadcast_to(np.linspace(1.0, 0.45, size)[:, None], (size, size)).copy()
    density = round(size / max(spec.get("height_m") or spec.get("width_m", 1), 0.1))
    return obj, bc, normal, ao, np.full((size, size), 0.75, np.float32), 0.0, density


def process(world, it, retexture=False, overrides=None):
    t0 = time.time()
    spec = legacy_spec(it)
    pid, base = it.id, f"SM_{it.id}"
    bake_dir = it.f("3d", "bake")
    out = it.f("game")
    os.makedirs(bake_dir, exist_ok=True)
    os.makedirs(out, exist_ok=True)
    tier = it.tier
    p = preset_for(world, it, spec, overrides)
    cond = {**tx.CONDITION[spec.get("condition", "new")],
            **{k: spec[k] for k in ("wear", "dirt", "rough_var") if k in spec}}
    stats = {"id": pid, "rel": it.rel, "kind": it.kind, "preset": p["preset"], "tier": p["tier"], "params": p,
             "flags": []}
    reset()
    slab, card = it.kind == "slab", it.kind == "card"
    slots = []
    shader = "prop"

    if slab:
        obj, frame = build_slab(spec, base)
        src = img_read(it.picked())
        size = tx.pow2_at_most(max(src.shape[:2]), 256, 2048)
        bc = tx.clamp_albedo(img_read(it.picked(), size)[..., :3], lo=20 / 255, hi=245 / 255, max_sat=0.95)
        density = round(size / max(spec.get("height_m") or spec["width_m"], 0.1))
        rough, metal, ao, normal = np.full(bc.shape[:2], 0.35 if spec.get("emissive") else 0.6), 0.0, 1.0, None
        slots.append({"name": f"M_{pid}_Frame", "params": {"tint": frame["color"], "roughness": 0.4}})
        stats["faces_source"] = tri_count(obj)
    elif card:
        obj, bc, normal, ao, rough, metal, density = process_card(it, spec, base)
        stats["faces_source"] = tri_count(obj)
        shader = "foliage"
    else:
        obj = import_joined(it.f("3d", "model.glb"), base)
        m = normalise_matrix(obj, spec)
        obj.data.transform(m)
        stats["faces_source"] = len(obj.data.polygons)
        bc_img, mr_img = gltf_images(obj)
        new_uv = p["uv"] == "smart" or p["remesh"] > 0
        source = duplicate(obj, "source") if new_uv else None      # keeps the original UVs for the transfer
        clean(obj, p, stats)
        if new_uv:
            edit_op(obj, lambda: bpy.ops.mesh.select_all(action="SELECT"),
                    lambda: bpy.ops.uv.smart_project(angle_limit=math.radians(66), island_margin=0.004))
        src_size = bc_img.size[0] if bc_img else 1024
        size, density = texture_size(obj, tier, src_size)
        bc_src, mr_src = os.path.join(bake_dir, "src_bc.png"), os.path.join(bake_dir, "src_mr.png")
        stamp = os.path.join(bake_dir, f"bakes_{size}.done")
        if not (retexture and os.path.exists(stamp)):
            if new_uv:     # colour and roughness-metal moved onto the new UV layout
                for img, path in ((bc_img, bc_src), (mr_img, mr_src)):
                    if img is None:
                        continue
                    mat = image_material(img)
                    for s in source.material_slots:
                        s.material = mat
                    bake(obj, "EMIT", size, path, high=source, cage=p["cage"], ray=p["ray"])
            else:
                bc_img.save(filepath=bc_src)
                if mr_img:
                    mr_img.save(filepath=mr_src)
            if source:     # it would occlude the AO bake
                bpy.data.objects.remove(source)
                source = None
            curvature_attribute(obj)
            height = obj.dimensions.z
            wrld = bpy.data.worlds.new("bake")
            wrld.light_settings.distance = max(obj.dimensions) * 0.25   # AO reach: contact, not whole-object
            bpy.context.scene.world = wrld
            bake(obj, "EMIT", size, os.path.join(bake_dir, "coverage.png"), material_override=white_material(), margin=0)
            # flat shading for AO: interpolated normals on a low-poly mesh send rays into neighbouring faces
            smooth = [f.use_smooth for f in obj.data.polygons]
            obj.data.polygons.foreach_set("use_smooth", [False] * len(smooth))
            bake(obj, "AO", size, os.path.join(bake_dir, "ao.png"))
            obj.data.polygons.foreach_set("use_smooth", smooth)
            bake(obj, "EMIT", size, os.path.join(bake_dir, "masks.png"), material_override=mask_material(height))
            bake(obj, "EMIT", size, os.path.join(bake_dir, "height.png"), material_override=height_material(height))
            high_path = it.f("3d", "model_high.glb")
            npath = os.path.join(bake_dir, "normal.png")
            if os.path.exists(npath):
                os.remove(npath)
            if os.path.exists(high_path):
                high = import_joined(high_path, "high")
                high.data.transform(m)
                bake(obj, "NORMAL", size, npath, high=high, cage=p["cage"], ray=p["ray"])
                bpy.data.objects.remove(high)
            for f in os.listdir(bake_dir):
                if f.startswith("bakes_"):
                    os.remove(os.path.join(bake_dir, f))
            open(stamp, "w").close()
        if source:
            bpy.data.objects.remove(source)
        coverage = img_read(os.path.join(bake_dir, "coverage.png"), size)[..., 0] > 0.5
        bc = img_read(bc_src, size)[..., :3]
        mr = img_read(mr_src, size) if os.path.exists(mr_src) else None
        rough = mr[..., 1] if mr is not None else np.full((size, size), 0.6, np.float32)
        metal = mr[..., 2] if mr is not None else np.zeros((size, size), np.float32)
        ao = img_read(os.path.join(bake_dir, "ao.png"), size)[..., 0]
        masks = img_read(os.path.join(bake_dir, "masks.png"), size)
        height = img_read(os.path.join(bake_dir, "height.png"), size)[..., 0]
        bc = tx.delight(bc, p["delight"], size / 16)
        bc, rough, metal = tx.weather(bc, rough, metal, ao, masks[..., 0], masks[..., 1], masks[..., 2], height,
                                      cond["wear"], cond["dirt"], cond["rough_var"], spec.get("bare_metal", 0.0))
        npath = os.path.join(bake_dir, "normal.png")
        if os.path.exists(npath):
            normal = img_read(npath, size)[..., :3]
            tilt = normal[..., 2][coverage] < 0.75          # blue < 0.75: tilted more than ~40 deg off the surface
            stats["normal_bad"] = round(float(tilt.mean()), 3) if tilt.size else 0.0
            if stats["normal_bad"] > NORMAL_BAD_MAX:
                stats["flags"].append(f"normal bake errors on {stats['normal_bad']:.0%} of texels: adjust cage / ray")
        else:   # no high-poly: a faint micro normal from albedo detail keeps the surface from looking moulded
            lum = tx.luminance(bc)
            normal = tx.normal_from_height(lum - tx.blur(lum, size / 128), strength=p["albedo_normal"])
        black = ao[coverage] < 0.02 if coverage.any() else np.zeros(1, bool)
        stats["ao_black"] = round(float(black.mean()), 3)
        if stats["ao_black"] > AO_BLACK_MAX:
            stats["flags"].append(f"{stats['ao_black']:.0%} of the surface is never lit: overlapping parts, double "
                                  f"walls or a deep cavity (try remesh; wasted texture space)")
        if stats.get("inverted_after", 0) > INVERTED_MAX:
            stats["flags"].append(f"{stats['inverted_after']:.0%} of faces point inward (vanish with backface culling)")
        set_single_material(obj, f"M_{pid}")

    files = {"bc": f"T_{base}_BC.png", "n": f"T_{base}_N.png", "orm": f"T_{base}_ORM.png"}
    if not slab and not card:
        bc = tx.clamp_albedo(bc)
    img_write(os.path.join(out, files["bc"]), bc)
    h, w = bc.shape[:2]
    plane = lambda v: np.full((h, w), float(v), np.float32) if np.isscalar(v) else v
    img_write(os.path.join(out, files["orm"]), tx.pack_orm(plane(ao), plane(rough), plane(metal)))
    flat = np.dstack([np.full((4, 4), .5)] * 2 + [np.ones((4, 4))])
    img_write(os.path.join(out, files["n"]), normal if normal is not None else flat)
    slots.insert(0, {"name": f"M_{pid}", "textures": files, "shader": shader,
                     "params": {"emissive": float(spec.get("emissive", 0))}})

    ratios = [1.0] if slab or card else LOD_RATIOS[tier]
    lods = make_lods(obj, base, ratios, p)
    col_kind = "none" if card else ("box" if slab else spec.get("collision", "none" if tier == "small" else "box"))
    col = make_collision(lods[-1], base, col_kind)
    for f in os.listdir(out):
        if f.endswith(".fbx"):
            os.remove(os.path.join(out, f))
    lod_info = []
    for i, o in enumerate(lods):
        fname = f"{base}.fbx" if i == 0 else f"{base}_LOD{i}.fbx"
        export_fbx([o] + ([col] if i == 0 and col else []), os.path.join(out, fname))
        lod_info.append({"file": fname, "tris": tri_count(o), "screen_size": LOD_SCREEN[i]})
    if len(lods) > 1:
        (a0, b0), (a1, b1) = bbox(lods[0]), bbox(lods[-1])
        d0, d1 = b0 - a0, b1 - a1
        drift = max(max(d1[i] / max(d0[i], 1e-6), d0[i] / max(d1[i], 1e-6)) for i in range(3)) - 1
        stats["lod_drift"] = round(drift, 3)
        if drift > LOD_DRIFT_MAX:
            stats["flags"].append(f"last LOD changes the silhouette by {drift:.0%}")
    if not card:
        preview(lods[0], os.path.join(out, files["bc"]), os.path.join(out, "preview.png"),
                [sl["params"]["tint"] for sl in slots[1:]])
    if density < TEXEL_DENSITY[tier] / 2 and not card:
        stats["flags"].append(f"low texel density {density} px/m (source texture too small for the size)")
    dims = lods[0].dimensions
    save(os.path.join(out, "asset.json"), {
        "kind": it.kind, "id": pid, "rel": it.rel, "name": base, "tier": tier,
        "mount": "wall" if slab else "floor", "bounds_m": [round(dims.x, 3), round(dims.y, 3), round(dims.z, 3)],
        "pivot": "back_bottom" if slab else "bottom_center", "lods": lod_info,
        "collision": None if col is None else col.name, "slots": slots, "surfaces": it.get("surfaces", []),
        "texture_size": int(w), "texel_density": int(density), "condition": cond,
        "nanite": not (slab or card)})
    stats.update({"faces_lod": [l["tris"] for l in lod_info], "texture": int(w), "texel_density": int(density),
                  "seconds": round(time.time() - t0, 1),
                  "status": "check" if stats["flags"] else ("review" if p["tier"] == "review" else "ok")})
    print(f"[{stats['status']}] {pid}: {stats['faces_source']} -> {lod_info[0]['tris']} tris, LODs "
          f"{[l['tris'] for l in lod_info[1:]]}, {w}px ({density} px/m) {'; '.join(stats['flags'])}", flush=True)
    return stats


# ---------------------------------------------------------------- report
def write_report(world, entries):
    path = world.p("logs", "bake_report.json")
    report = load(path, {})
    report.update({e["id"]: e for e in entries})
    save(path, report)
    order = {"fail": 0, "check": 1, "review": 2, "ok": 3}
    lines = ["# Bake report", "",
             "Read game/preview.png of every `fail`, `check` and `review` row; `ok` rows need no look.", "",
             "| status | item | kind | preset / tier | source faces | LOD0 | LODs | texture | px/m | AO black | normal bad | flags |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for e in sorted(report.values(), key=lambda e: (order.get(e["status"], 9), e.get("rel", e["id"]))):
        if e["status"] == "fail":
            lines.append(f"| fail | {e.get('rel', e['id'])} | | | | | | | | | | {e.get('error', '')} |")
            continue
        lines.append(f"| {e['status']} | {e.get('rel', e['id'])} | {e.get('kind', 'prop')} | {e['preset']} / {e['tier']} | "
                     f"{e['faces_source']:,} | {e['faces_lod'][0]:,} | {', '.join(f'{t:,}' for t in e['faces_lod'][1:])} | "
                     f"{e['texture']} | {e['texel_density']} | {e.get('ao_black', '')} | {e.get('normal_bad', '')} | "
                     f"{'; '.join(e['flags'])} |")
    md = world.out("bake_report.md")
    os.makedirs(os.path.dirname(md), exist_ok=True)
    open(md, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    counts = {s: sum(e["status"] == s for e in report.values()) for s in order}
    print(f"report: {counts} -> {md}", flush=True)


def run(world_dir, ids=(), retexture=False):
    world = World(world_dir)
    entries = []
    for it in world.items().values():
        if it.kind not in ("prop", "slab", "card") or it.get("status") == "rejected":
            continue
        if ids and it.id not in ids:
            continue
        if not it.forge_done():
            if ids:
                print(f"[skip] {it.id}: forge not finished (stage {it.stage()})", flush=True)
            continue
        if not ids and not retexture and it.stage() == "baked":
            continue
        try:
            entries.append(process(world, it, retexture))
        except Exception as e:
            traceback.print_exc()
            print(f"[fail] {it.id}: {e}", flush=True)
            entries.append({"id": it.id, "rel": it.rel, "status": "fail", "error": str(e).replace("|", "/")})
    if entries:
        write_report(world, entries)
        world.log({"step": "bake", "count": len(entries), "seconds": round(sum(e.get("seconds", 0) for e in entries), 1)})
    else:
        print("nothing to bake (every finished item is up to date)")
    return entries


def tune(world_dir, item_id, retexture=False, **overrides):
    """Live tuning (Blender MCP): process one item with temporary overrides, leave it in the W2W_Tune scene."""
    world = World(world_dir)
    entry = process(world, world.item(item_id), retexture, overrides)
    write_report(world, [entry])
    return {k: v for k, v in entry.items() if k != "params"}


if __name__ == "__main__" and not LIVE:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    run(argv[0], {a for a in argv[1:] if not a.startswith("--")}, "--retexture" in argv)
