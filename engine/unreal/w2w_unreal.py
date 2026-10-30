"""Unreal Engine 5 importer: <world>/build/out/level.json -> /Game/W2W/<world>/ and a level, plus review shots.

Needs the Python Editor Script Plugin and Editor Scripting Utilities (both ship with the engine).

  unattended (python w2w.py engine <world> [--shots]):
      UnrealEditor.exe <Game.uproject> -nosplash
          -ExecCmds="py <repo>/engine/unreal/w2w_unreal.py --world <world_dir> --round r03 --shots --exit-when-done"
  inside an open editor (Output Log > Python, or the unreal-mcp python tool):
      py "<repo>/engine/unreal/w2w_unreal.py" --world <world_dir> --round r03 --shots

Content: /Game/W2W/Master (shared master materials), /Game/W2W/<world>/Library/<category>/<family>/<id>/ (the same
tree as the library on disk), /Arch, /Terrain, /Maps/<world>. Only these folders are written.
Re-running is safe: unchanged source files are skipped (metadata tag w2w_src); level actors tagged W2W are
rebuilt; anything without the tag (added by hand) is left alone.

Axes: level.json is metres, right-handed Z-up. Unreal is centimetres, left-handed: x = 100 x, y = -100 y,
z = 100 z, yaw_ue = -yaw. FBX files come from Blender with the matching axis settings.
"""
import argparse
import json
import math
import os
import re
import time

import unreal

AT = unreal.AssetToolsHelpers.get_asset_tools()
EAL = unreal.EditorAssetLibrary
MEL = unreal.MaterialEditingLibrary
LOG_FILE = None


def log(msg):
    unreal.log(f"[w2w] {msg}")
    if LOG_FILE:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(msg + "\n")


# ---------------------------------------------------------------- conversion
def loc(p):
    return unreal.Vector(p[0] * 100.0, -p[1] * 100.0, p[2] * 100.0)


def vec(d):
    return unreal.Vector(d[0], -d[1], d[2])


def rot_from_dir(d, up=None):
    if up is None:
        return unreal.MathLibrary.make_rot_from_x(vec(d))
    return unreal.MathLibrary.make_rot_from_xz(vec(d), vec(up))


def yaw_rot(yaw):
    return unreal.Rotator(roll=0.0, pitch=0.0, yaw=-yaw)


def rot_yaw_up(yaw, up):
    """Rotation with the object's up axis along `up` and its heading `yaw` (our frame) -> Unreal rotator."""
    ux, uy, uz = up
    n = math.sqrt(ux * ux + uy * uy + uz * uz) or 1.0
    ux, uy, uz = ux / n, uy / n, uz / n
    c, s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    d = c * ux + s * uy
    x = (c - ux * d, s - uy * d, -uz * d)
    xn = math.sqrt(sum(v * v for v in x)) or 1.0
    x = tuple(v / xn for v in x)
    y = (uy * x[2] - uz * x[1], uz * x[0] - ux * x[2], ux * x[1] - uy * x[0])
    # Unreal's local +Y is our -Y (the axis mirror): passing vec(y) would hand it a reflected basis (upside down)
    return unreal.MathLibrary.make_rotation_from_axes(vec(x), vec(tuple(-v for v in y)), vec((ux, uy, uz)))


def safe(name):
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


# ---------------------------------------------------------------- import helpers
def asset_path(folder, name):
    return f"{folder}/{name}.{name}"


def up_to_date(path, src):
    if not EAL.does_asset_exist(path):
        return False
    a = unreal.load_asset(path)
    return a is not None and EAL.get_metadata_tag(a, "w2w_src") == f"{os.path.getmtime(src):.0f}"


def mark(asset, src):
    EAL.set_metadata_tag(asset, "w2w_src", f"{os.path.getmtime(src):.0f}")
    EAL.save_loaded_asset(asset)


def import_task(src, folder, name, options=None):
    task = unreal.AssetImportTask()
    task.filename = src
    task.destination_path = folder
    task.destination_name = name
    task.automated = True
    task.replace_existing = True
    task.save = False
    if options:
        task.options = options
    AT.import_asset_tasks([task])
    paths = list(task.imported_object_paths)
    if not paths:
        raise RuntimeError(f"import failed: {src}")
    return unreal.load_asset(paths[0])


def texture(src, folder, kind):
    """kind: color | normal | data"""
    name = os.path.splitext(os.path.basename(src))[0]
    path = asset_path(folder, name)
    fresh = not up_to_date(path, src)
    tex = import_task(src, folder, name) if fresh else unreal.load_asset(path)
    # Unreal guesses the type from the pixels (a bluish base colour becomes a normal map): always set it
    want = {"normal": unreal.TextureCompressionSettings.TC_NORMALMAP,
            "data": unreal.TextureCompressionSettings.TC_MASKS}.get(kind, unreal.TextureCompressionSettings.TC_DEFAULT)
    srgb = kind == "color"
    if fresh or tex.get_editor_property("compression_settings") != want or tex.get_editor_property("srgb") != srgb:
        tex.set_editor_property("compression_settings", want)
        tex.set_editor_property("srgb", srgb)
        if kind == "normal":
            tex.set_editor_property("flip_green_channel", True)      # files are OpenGL, Unreal is DirectX
        tex.set_editor_property("lod_group", unreal.TextureGroup.TEXTUREGROUP_WORLD_NORMAL_MAP if kind == "normal"
                                else unreal.TextureGroup.TEXTUREGROUP_WORLD)
        fresh = True
    if fresh:
        mark(tex, src)
    return tex


def fbx_options():
    ui = unreal.FbxImportUI()
    ui.set_editor_property("import_mesh", True)
    ui.set_editor_property("import_textures", False)
    ui.set_editor_property("import_materials", False)
    ui.set_editor_property("import_as_skeletal", False)
    ui.set_editor_property("import_animations", False)
    ui.set_editor_property("mesh_type_to_import", unreal.FBXImportType.FBXIT_STATIC_MESH)
    sm = ui.static_mesh_import_data
    sm.set_editor_property("combine_meshes", True)
    sm.set_editor_property("auto_generate_collision", False)
    sm.set_editor_property("generate_lightmap_u_vs", False)
    sm.set_editor_property("convert_scene", True)
    sm.set_editor_property("force_front_x_axis", False)
    sm.set_editor_property("import_uniform_scale", 1.0)
    sm.set_editor_property("normal_import_method", unreal.FBXNormalImportMethod.FBXNIM_IMPORT_NORMALS_AND_TANGENTS)
    sm.set_editor_property("vertex_color_import_option", unreal.VertexColorImportOption.REPLACE)
    return ui


# ---------------------------------------------------------------- material graphs
class Graph:
    def __init__(self, mat):
        self.mat = mat
        self.y = 0

    def node(self, cls, x=-600, **props):
        e = MEL.create_material_expression(self.mat, cls, x, self.y)
        self.y += 140
        for k, v in props.items():
            e.set_editor_property(k, v)
        return e

    def const(self, v):
        return self.node(unreal.MaterialExpressionConstant, -1400, r=float(v))

    def scalar(self, name, value):
        return self.node(unreal.MaterialExpressionScalarParameter, -1400, parameter_name=name, default_value=value)

    def vector(self, name, value):
        return self.node(unreal.MaterialExpressionVectorParameter, -1400, parameter_name=name,
                         default_value=unreal.LinearColor(*value))

    def tex(self, name, sampler, default, uv=None, shared=False):
        e = self.node(unreal.MaterialExpressionTextureSampleParameter2D, -1000, parameter_name=name,
                      sampler_type=sampler, texture=default)
        if shared:     # shared wrap samplers lift the 16-sampler limit (terrain blends 13 textures)
            e.set_editor_property("sampler_source", unreal.SamplerSourceMode.SSM_WRAP_WORLD_GROUP_SETTINGS)
        if uv is not None:
            self.link(uv, "", e, "UVs")
        return e

    def op(self, cls, a, b, ao="", bo="", x=-500):
        e = self.node(cls, x)
        self.link(a, ao, e, "A")
        self.link(b, bo, e, "B")
        return e

    def mul(self, a, b, ao="", bo=""):
        return self.op(unreal.MaterialExpressionMultiply, a, b, ao, bo)

    def add(self, a, b, ao="", bo=""):
        return self.op(unreal.MaterialExpressionAdd, a, b, ao, bo)

    def sub(self, a, b, ao="", bo=""):
        return self.op(unreal.MaterialExpressionSubtract, a, b, ao, bo)

    def lerp(self, a, b, alpha, ao="", bo="", alo=""):
        e = self.node(unreal.MaterialExpressionLinearInterpolate, -300)
        self.link(a, ao, e, "A")
        self.link(b, bo, e, "B")
        self.link(alpha, alo, e, "Alpha")
        return e

    def one(self, cls, a, ao=""):
        e = self.node(cls, -400)
        self.link(a, ao, e, "")
        return e

    def mask(self, src, out, r=False, g=False, b=False, a=False):
        e = self.node(unreal.MaterialExpressionComponentMask, -700, r=r, g=g, b=b, a=a)
        self.link(src, out, e, "")
        return e

    def append(self, a, b, ao="", bo=""):
        e = self.node(unreal.MaterialExpressionAppendVector, -250)
        self.link(a, ao, e, "A")
        self.link(b, bo, e, "B")
        return e

    @staticmethod
    def link(a, ao, b, bi):
        if not MEL.connect_material_expressions(a, ao, b, bi):
            raise RuntimeError(f"cannot connect {a.get_name()}.{ao} -> {b.get_name()}.{bi}")

    def out(self, e, o, prop):
        if not MEL.connect_material_property(e, o, prop):
            raise RuntimeError(f"cannot connect to {prop}")

    def world_uv(self, size_param, default_m):
        """World XY / (size in metres * 100)."""
        wp = self.node(unreal.MaterialExpressionWorldPosition, -1600)
        xy = self.mask(wp, "", r=True, g=True)
        return self.op(unreal.MaterialExpressionDivide, xy, self.mul(self.scalar(size_param, default_m), self.const(100.0)))

    def macro_uv(self):
        """(x + 0.7 z, y + 0.7 z) / (MacroSize * 100): floors and walls in every direction vary."""
        wp = self.node(unreal.MaterialExpressionWorldPosition, -1600)
        xy = self.mask(wp, "", r=True, g=True)
        z = self.mul(self.mask(wp, "", b=True), self.const(0.7))
        return self.op(unreal.MaterialExpressionDivide, self.add(xy, z),
                       self.mul(self.scalar("MacroSize", 8.0), self.const(100.0)))

    def detail_blend(self, n1, detail_tex, uv):
        """Whiteout blend: normalize(n1.xy + s * n2.xy, n1.z)."""
        duv = self.mul(uv, self.scalar("DetailTiling", 4.0))
        n2 = self.tex("DetailNormal", unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL, detail_tex, duv)
        xy2 = self.mul(self.mask(n2, "RGB", r=True, g=True), self.scalar("DetailStrength", 0.4))
        xy = self.add(self.mask(n1, "", r=True, g=True), xy2)
        return self.one(unreal.MaterialExpressionNormalize, self.append(xy, self.mask(n1, "", b=True)))


class Importer:
    def __init__(self, level, world_dir, rebuild_materials=False):
        self.lv = level
        self.world = world_dir
        self.wname = safe(level["name"])
        self.root = "/Game/W2W"
        self.wroot = f"{self.root}/{self.wname}"
        self.rebuild = rebuild_materials
        self.mi = {}           # surface / decal id -> MaterialInstanceConstant
        self.meshes = {}       # item id -> StaticMesh
        unreal.SystemLibrary.execute_console_command(None, "Interchange.FeatureFlags.Import.FBX 0")

    # ---- masters
    def master(self, name, build, setup=None):
        folder = f"{self.root}/Master"
        path = asset_path(folder, name)
        if EAL.does_asset_exist(path) and not self.rebuild:
            return unreal.load_asset(path)
        if EAL.does_asset_exist(path):
            EAL.delete_asset(path)
        mat = AT.create_asset(name, folder, unreal.Material, unreal.MaterialFactoryNew())
        if setup:
            setup(mat)
        build(Graph(mat))
        MEL.layout_material_expressions(mat)
        MEL.recompile_material(mat)
        EAL.save_loaded_asset(mat)
        log(f"master material {name}")
        return mat

    def shared(self):
        s, folder = self.lv["shared"], f"{self.root}/Shared"
        self.t_macro = texture(s["macro"], folder, "data")
        self.t_detail = texture(s["detail_n"], folder, "normal")
        self.t_bc = texture(s["default_bc"], folder, "color")
        self.t_n = texture(s["default_n"], folder, "normal")
        self.t_orm = texture(s["default_orm"], folder, "data")

    def masters(self):
        C, N, M = (unreal.MaterialSamplerType.SAMPLERTYPE_COLOR, unreal.MaterialSamplerType.SAMPLERTYPE_NORMAL,
                   unreal.MaterialSamplerType.SAMPLERTYPE_MASKS)
        P = unreal.MaterialProperty

        def prop(g):
            bc = g.tex("BaseColor", C, self.t_bc)
            n = g.tex("Normal", N, self.t_n)
            orm = g.tex("ORM", M, self.t_orm)
            col = g.mul(bc, g.vector("Tint", (1, 1, 1, 1)), "RGB", "")
            g.out(col, "", P.MP_BASE_COLOR)
            g.out(n, "RGB", P.MP_NORMAL)
            g.out(orm, "R", P.MP_AMBIENT_OCCLUSION)
            g.out(g.mul(orm, g.scalar("RoughnessScale", 1.0), "G", ""), "", P.MP_ROUGHNESS)
            g.out(orm, "B", P.MP_METALLIC)
            g.out(g.mul(col, g.scalar("Emissive", 0.0)), "", P.MP_EMISSIVE_COLOR)

        def foliage(g):
            bc = g.tex("BaseColor", C, self.t_bc)
            n = g.tex("Normal", N, self.t_n)
            orm = g.tex("ORM", M, self.t_orm)
            g.out(g.mul(bc, g.vector("Tint", (1, 1, 1, 1)), "RGB", ""), "", P.MP_BASE_COLOR)
            g.out(bc, "A", P.MP_OPACITY_MASK)
            g.out(n, "RGB", P.MP_NORMAL)
            g.out(orm, "R", P.MP_AMBIENT_OCCLUSION)
            g.out(orm, "G", P.MP_ROUGHNESS)

        def foliage_setup(mat):
            mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_MASKED)
            mat.set_editor_property("two_sided", True)
            mat.set_editor_property("opacity_mask_clip_value", 0.4)

        def surface(g):
            uv = g.mul(g.node(unreal.MaterialExpressionTextureCoordinate, -1600), g.scalar("Tiling", 0.5))
            uv2 = g.mul(g.node(unreal.MaterialExpressionTextureCoordinate, -1600), g.scalar("Tiling2", 0.5))
            macro = g.tex("Macro", M, self.t_macro, g.macro_uv())
            strength = g.scalar("MacroStrength", 0.15)
            # layer 2 (a dirtier / mossier / different-batch version) where the world-space mask is high
            amount = g.scalar("Layer2Amount", 0.0)
            alpha = g.one(unreal.MaterialExpressionSaturate,
                          g.mul(g.add(g.sub(macro, g.const(1.0), "B", ""), amount), g.const(5.0)))
            bc = g.lerp(g.tex("BaseColor", C, self.t_bc, uv), g.tex("BaseColor2", C, self.t_bc, uv2), alpha,
                        "RGB", "RGB")
            orm = g.lerp(g.tex("ORM", M, self.t_orm, uv), g.tex("ORM2", M, self.t_orm, uv2), alpha, "RGB", "RGB")
            nrm = g.lerp(g.tex("Normal", N, self.t_n, uv), g.tex("Normal2", N, self.t_n, uv2), alpha, "RGB", "RGB")
            bright = g.lerp(g.sub(g.const(1.0), strength), g.add(g.const(1.0), strength), macro, "", "", "R")
            # grime from the architecture's vertex colour R: darker, browner, rougher at bases and corners
            grime = g.mul(g.node(unreal.MaterialExpressionVertexColor, -1600), g.scalar("Grime", 0.6), "R", "")
            col = g.mul(g.mul(g.mul(bc, g.vector("Tint", (1, 1, 1, 1)), "", "RGB"), bright),
                        g.lerp(g.const(1.0), g.vector("GrimeColor", (0.55, 0.5, 0.42, 1)), grime, "", "RGB"))
            rough = g.add(g.add(g.mul(g.mask(orm, "", g=True), g.scalar("RoughnessScale", 1.0)),
                                g.mul(g.sub(macro, g.const(0.5), "G", ""), strength)),
                          g.mul(grime, g.const(0.25)))
            g.out(col, "", P.MP_BASE_COLOR)
            g.out(g.detail_blend(nrm, self.t_detail, uv), "", P.MP_NORMAL)
            g.out(g.mask(orm, "", r=True), "", P.MP_AMBIENT_OCCLUSION)
            g.out(rough, "", P.MP_ROUGHNESS)
            g.out(g.mask(orm, "", b=True), "", P.MP_METALLIC)

        def terrain(g):
            splat = g.tex("Splat", M, self.t_orm, None, shared=True)
            w_r, w_g, w_b = (g.mask(splat, "RGB", **{c: True}) for c in ("r", "g", "b"))
            w_a = g.one(unreal.MaterialExpressionSaturate,
                        g.sub(g.sub(g.sub(g.const(1.0), w_r), w_g), w_b))
            ws = [w_r, w_g, w_b, w_a]
            bc_sum = orm_sum = n_sum = None
            for i, w in enumerate(ws):
                uv = g.world_uv(f"Tile{i}", 3.0)
                bc = g.mul(g.tex(f"BC{i}", C, self.t_bc, uv, shared=True), w, "RGB", "")
                orm = g.mul(g.tex(f"ORM{i}", M, self.t_orm, uv, shared=True), w, "RGB", "")
                n = g.mul(g.tex(f"N{i}", N, self.t_n, uv, shared=True), w, "RGB", "")
                bc_sum = bc if bc_sum is None else g.add(bc_sum, bc)
                orm_sum = orm if orm_sum is None else g.add(orm_sum, orm)
                n_sum = n if n_sum is None else g.add(n_sum, n)
            macro = g.tex("Macro", M, self.t_macro, g.macro_uv(), shared=True)
            strength = g.scalar("MacroStrength", 0.25)
            bright = g.lerp(g.sub(g.const(1.0), strength), g.add(g.const(1.0), strength), macro, "", "", "R")
            g.out(g.mul(bc_sum, bright), "", P.MP_BASE_COLOR)
            g.out(g.one(unreal.MaterialExpressionNormalize, n_sum), "", P.MP_NORMAL)
            g.out(g.mask(orm_sum, "", r=True), "", P.MP_AMBIENT_OCCLUSION)
            g.out(g.add(g.mask(orm_sum, "", g=True), g.mul(g.sub(macro, g.const(0.5), "G", ""), strength)), "",
                  P.MP_ROUGHNESS)

        def glass(g):
            g.out(g.vector("Tint", (0.85, 0.9, 0.88, 1)), "", P.MP_BASE_COLOR)
            g.out(g.scalar("Opacity", 0.18), "", P.MP_OPACITY)
            g.out(g.scalar("Roughness", 0.06), "", P.MP_ROUGHNESS)
            g.out(g.scalar("Specular", 0.5), "", P.MP_SPECULAR)

        def glass_setup(mat):
            mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_TRANSLUCENT)
            mat.set_editor_property("two_sided", True)
            mat.set_editor_property("translucency_lighting_mode",
                                    unreal.TranslucencyLightingMode.TLM_SURFACE_PER_PIXEL_LIGHTING)

        def decal(g):
            bc = g.tex("BaseColor", C, self.t_bc)
            n = g.tex("Normal", N, self.t_n)
            g.out(bc, "RGB", P.MP_BASE_COLOR)
            g.out(g.mul(bc, g.scalar("Opacity", 1.0), "A", ""), "", P.MP_OPACITY)
            g.out(n, "RGB", P.MP_NORMAL)
            g.out(g.scalar("Roughness", 0.6), "", P.MP_ROUGHNESS)

        def decal_setup(mat):
            mat.set_editor_property("material_domain", unreal.MaterialDomain.MD_DEFERRED_DECAL)
            mat.set_editor_property("blend_mode", unreal.BlendMode.BLEND_TRANSLUCENT)

        def placeholder(g):
            c = g.vector("Color", (0.9, 0.1, 0.8, 1))
            g.out(c, "", P.MP_BASE_COLOR)
            g.out(g.mul(c, g.const(0.4)), "", P.MP_EMISSIVE_COLOR)
            g.out(g.const(0.8), "", P.MP_ROUGHNESS)

        self.m_prop = self.master("M_W2W_Prop", prop)
        self.m_foliage = self.master("M_W2W_Foliage", foliage, foliage_setup)
        self.m_surface = self.master("M_W2W_Surface", surface)
        self.m_terrain = self.master("M_W2W_Terrain", terrain)
        self.m_glass = self.master("M_W2W_Glass", glass, glass_setup)
        self.m_decal = self.master("M_W2W_Decal", decal, decal_setup)
        self.m_placeholder = self.master("M_W2W_Placeholder", placeholder)

    def instance(self, name, folder, parent, textures=None, scalars=None, vectors=None):
        path = asset_path(folder, name)
        mi = unreal.load_asset(path) if EAL.does_asset_exist(path) else \
            AT.create_asset(name, folder, unreal.MaterialInstanceConstant, unreal.MaterialInstanceConstantFactoryNew())
        MEL.set_material_instance_parent(mi, parent)
        for k, v in (textures or {}).items():
            MEL.set_material_instance_texture_parameter_value(mi, k, v)
        for k, v in (scalars or {}).items():
            MEL.set_material_instance_scalar_parameter_value(mi, k, float(v))
        for k, v in (vectors or {}).items():
            MEL.set_material_instance_vector_parameter_value(mi, k, unreal.LinearColor(*(list(v) + [1.0])[:4]))
        EAL.save_loaded_asset(mi)
        return mi

    def lib_folder(self, iid):
        return f"{self.wroot}/Library/{self.lv['library'][iid]['rel']}"

    # ---- surfaces and decals
    def surface_textures(self, iid):
        e = self.lv["library"][iid]
        a, folder = e["asset"], self.lib_folder(iid)
        t = a["textures"]
        return {"bc": texture(os.path.join(e["dir"], t["bc"]), folder, "color"),
                "n": texture(os.path.join(e["dir"], t["n"]), folder, "normal"),
                "orm": texture(os.path.join(e["dir"], t["orm"]), folder, "data")}, a

    def surfaces(self):
        lib = self.lv["library"]
        for iid, e in lib.items():
            if e["kind"] != "surface":
                continue
            tx, a = self.surface_textures(iid)
            prm = a.get("params", {})
            texs = {"BaseColor": tx["bc"], "Normal": tx["n"], "ORM": tx["orm"],
                    "BaseColor2": tx["bc"], "Normal2": tx["n"], "ORM2": tx["orm"]}
            scal = {"Tiling": 1.0 / a.get("tile_m", 2.0), "Tiling2": 1.0 / a.get("tile_m", 2.0),
                    "MacroStrength": prm.get("macro", 0.15), "DetailStrength": prm.get("detail", 0.4),
                    "Grime": prm.get("grime", 0.6), "Layer2Amount": 0.0}
            l2 = prm.get("layer2")
            if l2 and l2 in lib:
                tx2, a2 = self.surface_textures(l2)
                texs.update({"BaseColor2": tx2["bc"], "Normal2": tx2["n"], "ORM2": tx2["orm"]})
                scal.update({"Tiling2": 1.0 / a2.get("tile_m", 2.0), "Layer2Amount": prm.get("layer2_amount", 0.35)})
            self.mi[iid] = self.instance(f"MI_{iid}", self.lib_folder(iid), self.m_surface, texs, scal)
        self.mi["glass"] = self.instance("MI_glass", f"{self.wroot}/Arch", self.m_glass)
        self.mi["_fallback"] = self.instance("MI_fallback", f"{self.wroot}/Arch", self.m_surface)
        self.mi["_placeholder"] = self.instance("MI_placeholder", f"{self.wroot}/Arch", self.m_placeholder)

    def decals(self):
        for iid, e in self.lv["library"].items():
            if e["kind"] != "decal":
                continue
            a, folder = e["asset"], self.lib_folder(iid)
            texs = {"BaseColor": texture(os.path.join(e["dir"], a["textures"]["bc"]), folder, "color"),
                    "Normal": texture(os.path.join(e["dir"], a["textures"]["n"]), folder, "normal")}
            self.mi[iid] = self.instance(f"MI_{iid}", folder, self.m_decal, texs,
                                         {"Roughness": a["params"]["roughness"], "Opacity": a["params"]["opacity"]})

    # ---- meshes
    def mesh(self, src, folder, name, nanite, lods=(), collision="item"):
        path = asset_path(folder, name)
        fresh = not up_to_date(path, src)
        m = import_task(src, folder, name, fbx_options()) if fresh else unreal.load_asset(path)
        if fresh:
            ns = m.get_editor_property("nanite_settings")
            ns.set_editor_property("enabled", nanite)
            m.set_editor_property("nanite_settings", ns)
            if not nanite and lods:
                sms = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
                for i, f in enumerate(lods, 1):
                    sms.import_lod(m, i, f)
            if collision == "complex":
                bs = m.get_editor_property("body_setup")
                if bs:
                    bs.set_editor_property("collision_trace_flag", unreal.CollisionTraceFlag.CTF_USE_COMPLEX_AS_SIMPLE)
            mark(m, src)
        return m, fresh

    def items(self):
        n = 0
        for iid, e in sorted(self.lv["library"].items()):
            if e["kind"] not in ("prop", "slab", "card"):
                continue
            a, folder = e["asset"], self.lib_folder(iid)
            lods = [os.path.join(e["dir"], l["file"]) for l in a["lods"][1:]]
            m, fresh = self.mesh(os.path.join(e["dir"], a["lods"][0]["file"]), folder, a["name"], a.get("nanite", True), lods)
            for slot in a.get("slots", []):
                name = "MI_" + slot["name"][2:]
                texs = {}
                for key, param, kind in (("bc", "BaseColor", "color"), ("n", "Normal", "normal"), ("orm", "ORM", "data")):
                    f = (slot.get("textures") or {}).get(key)
                    if f:
                        texs[param] = texture(os.path.join(e["dir"], f), folder, kind)
                prm = slot.get("params", {})
                parent = self.m_foliage if slot.get("shader") == "foliage" else self.m_prop
                scal = {} if parent == self.m_foliage else {"Emissive": prm.get("emissive", 0.0)}
                if "roughness" in prm and parent == self.m_prop:
                    scal["RoughnessScale"] = prm["roughness"] / 0.6
                mi = self.instance(name, folder, parent, texs, scal, {"Tint": prm["tint"]} if "tint" in prm else {})
                idx = m.get_material_index(slot["name"])
                if idx >= 0:
                    m.set_material(idx, mi)
            self.meshes[iid] = m
            n += fresh
        log(f"library meshes: {len(self.meshes)} ({n} imported or updated)")

    def arch(self):
        out = []
        for a in self.lv["arch"]:
            folder = f"{self.wroot}/Arch"
            has_glass = "glass" in a["slots"]
            m, _ = self.mesh(a["file"], folder, a["name"], nanite=not has_glass, collision="complex")
            for slot in a["slots"]:
                mi = self.mi.get(slot) or self.mi["_fallback"]
                idx = m.get_material_index(slot)
                if idx >= 0:
                    m.set_material(idx, mi)
            out.append((a, m))
        return out

    def terrain(self):
        t = self.lv.get("terrain")
        if not t:
            return []
        folder = f"{self.wroot}/Terrain"
        texs = {"Splat": texture(t["splat"], folder, "data")}
        scal = {}
        for i, ch in enumerate("rgba"):
            sid = t.get("layers", {}).get(ch)
            if sid and sid in self.lv["library"]:
                tx, a = self.surface_textures(sid)
                texs.update({f"BC{i}": tx["bc"], f"N{i}": tx["n"], f"ORM{i}": tx["orm"]})
                scal[f"Tile{i}"] = a.get("tile_m", 3.0)
        mi = self.instance("MI_terrain", folder, self.m_terrain, texs, scal)
        meshes = []
        for tile in t["tiles"]:
            m, _ = self.mesh(tile["file"], folder, tile["name"], nanite=True, collision="complex")
            m.set_material(0, mi)
            meshes.append(m)
        return meshes

    # ---- level
    def build_level(self, arch_meshes, terrain_meshes):
        lv = self.lv
        les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
        path = f"{self.wroot}/Maps/{self.wname}"
        if EAL.does_asset_exist(path):
            les.load_level(path)
        else:
            les.new_level(path)
        for a in eas.get_all_level_actors():
            if "W2W" in [str(t) for t in a.tags]:
                eas.destroy_actor(a)

        def tag(actor, name, folder):
            actor.set_actor_label(name)
            actor.set_folder_path(f"W2W/{folder}")
            actor.tags = ["W2W"]
            return actor

        def static(mesh, pos, rot, name, folder, scale=1.0, mobility=unreal.ComponentMobility.STATIC):
            actor = eas.spawn_actor_from_object(mesh, loc(pos), rot)
            actor.set_actor_scale3d(unreal.Vector(scale, scale, scale) if not isinstance(scale, (list, tuple))
                                    else unreal.Vector(*scale))
            actor.static_mesh_component.set_mobility(mobility)
            return tag(actor, name, folder)

        for a, m in arch_meshes:
            static(m, a["pos"] if len(a["pos"]) == 3 else a["pos"] + [0], yaw_rot(a["yaw"]), a["name"], f"Arch/{a['space']}")
        for m in terrain_meshes:
            static(m, [0, 0, 0], unreal.Rotator(), m.get_name(), "Terrain")

        cube = unreal.load_asset("/Engine/BasicShapes/Cube.Cube")
        temp = 0
        for inst in lv["instances"]:
            if inst.get("placeholder"):
                w, d, h = inst["size"]
                p = list(inst["pos"])
                p[2] += h / 2
                act = static(cube, p, yaw_rot(inst["yaw"]), f"TEMP_{inst['item']}_{inst['name']}",
                             f"{inst['space']}/TEMP", [w, d, h])
                act.static_mesh_component.set_material(0, self.mi["_placeholder"])
                temp += 1
                continue
            m = self.meshes.get(inst["item"])
            if m is None:
                continue
            static(m, inst["pos"], yaw_rot(inst["yaw"]), inst["name"], inst["space"], inst.get("scale", 1.0))

        self.foliage(eas, tag, cube)
        dynamic = lv.get("lighting", {}).get("gi", "dynamic") == "dynamic"
        for l in lv["lights"]:
            cls = {"point": unreal.PointLight, "spot": unreal.SpotLight, "rect": unreal.RectLight}[l["type"]]
            actor = eas.spawn_actor_from_class(cls, loc(l["pos"]), rot_from_dir(l["dir"]))
            comp = actor.get_component_by_class(unreal.LocalLightComponent)
            comp.set_mobility(unreal.ComponentMobility.MOVABLE if dynamic else unreal.ComponentMobility.STATIONARY)
            comp.set_editor_property("intensity_units", unreal.LightUnits.LUMENS)
            comp.set_intensity(l["lumens"])
            comp.set_attenuation_radius(l["radius"] * 100)
            comp.set_editor_property("use_temperature", True)
            comp.set_temperature(l["kelvin"])
            comp.set_cast_shadows(bool(l["shadows"]))
            comp.set_volumetric_scattering_intensity(l.get("volumetric", 1.0))
            if l.get("color"):
                comp.set_light_color(unreal.LinearColor(*l["color"], 1.0))
            if l["type"] == "spot":
                comp.set_outer_cone_angle(l["cone"] / 2)
                comp.set_inner_cone_angle(l["cone"] / 3)
            if l["type"] == "rect":
                comp.set_source_width(l["size"][0] * 100)
                comp.set_source_height(l["size"][1] * 100)
            if l["type"] == "point" and l.get("source_radius"):
                comp.set_editor_property("source_radius", l["source_radius"] * 100)
            tag(actor, l["name"], f"{l['space']}/Lights")
        self.environment(eas, tag)
        for d in lv["decals"]:
            mi = self.mi.get(d["decal"])
            if mi is None:
                continue
            n = d["normal"]
            actor = eas.spawn_actor_from_class(unreal.DecalActor, loc(d["pos"]), rot_from_dir([-n[0], -n[1], -n[2]], d["up"]))
            actor.set_decal_material(mi)
            actor.decal.set_editor_property("decal_size", unreal.Vector(d["depth"] * 50, d["size"][0] * 50, d["size"][1] * 50))
            tag(actor, d["name"], "Decals")
        cams = {}
        for c in lv["cameras"]:
            actor = eas.spawn_actor_from_class(unreal.CameraActor, loc(c["pos"]), rot_from_dir(c["dir"]))
            actor.camera_component.set_editor_property("field_of_view", c["fov"])
            cams[c["id"]] = tag(actor, f"Cam_{c['id']}", f"{c.get('space', 'outdoor')}/Cameras")
        if lv.get("player_start"):
            ps = lv["player_start"]
            p = list(ps["pos"])
            p[2] += 0.92
            tag(eas.spawn_actor_from_class(unreal.PlayerStart, loc(p), yaw_rot(ps.get("yaw", 0))), "PlayerStart", "Gameplay")
        les.save_current_level()
        unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)
        log(f"level {path}: {len(lv['instances'])} instances ({temp} placeholders), {len(lv['lights'])} lights, "
            f"{len(lv['decals'])} decals, {len(arch_meshes)} arch meshes, {len(terrain_meshes)} terrain tiles")
        return cams

    def foliage(self, eas, tag, cube):
        sds = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
        lib = unreal.SubobjectDataBlueprintFunctionLibrary
        for layer in self.lv.get("foliage", []):
            for iid, entry in layer["meshes"].items():
                mesh = cube if entry.get("placeholder") else self.meshes.get(iid)
                if mesh is None:
                    continue
                rows = entry["rows"]
                transforms = []
                for r in rows:
                    x, y, z, yaw, s = r[:5]
                    up = r[5:8] if len(r) >= 8 else (0, 0, 1)
                    if entry.get("placeholder"):
                        w, d, h = entry["size"]
                        scale = unreal.Vector(w * s, d * s, h * s)
                        z += h * s / 2
                    else:
                        scale = unreal.Vector(s, s, s)
                    transforms.append(unreal.Transform(loc([x, y, z]), rot_yaw_up(yaw, up), scale))
                actor = eas.spawn_actor_from_class(unreal.StaticMeshActor, unreal.Vector(0, 0, 0), unreal.Rotator())
                actor.static_mesh_component.set_mobility(unreal.ComponentMobility.STATIC)
                label = f"Foliage_{layer['id']}_{iid}"
                try:
                    root = sds.k2_gather_subobject_data_for_instance(actor)[0]
                    params = unreal.AddNewSubobjectParams(parent_handle=root,
                                                          new_class=unreal.HierarchicalInstancedStaticMeshComponent,
                                                          blueprint_context=None)
                    handle, fail = sds.add_new_subobject(params)
                    comp = lib.get_object(lib.get_data(handle))
                    if comp is None:
                        raise RuntimeError(f"no component ({fail})")
                    comp.set_static_mesh(mesh)
                    comp.set_mobility(unreal.ComponentMobility.STATIC)
                    if entry.get("placeholder"):
                        comp.set_material(0, self.mi["_placeholder"])
                    start, end = layer.get("cull", [0, 0])
                    if end:
                        comp.set_editor_property("instance_start_cull_distance", int(start))
                        comp.set_editor_property("instance_end_cull_distance", int(end))
                    comp.set_cast_shadow(bool(layer.get("shadows", True)))
                    comp.add_instances(transforms, False, True)
                    tag(actor, label, f"Foliage/{layer['id']}")
                except Exception as e:     # older engines: one actor per instance (auto-instanced at render time)
                    eas.destroy_actor(actor)
                    log(f"foliage {label}: HISM failed ({e}); spawning up to 3000 actors instead")
                    for i, t in enumerate(transforms[:3000]):
                        a = eas.spawn_actor_from_object(mesh, t.translation, t.rotation.rotator())
                        a.set_actor_scale3d(t.scale3d)
                        tag(a, f"{label}_{i}", f"Foliage/{layer['id']}")
                log(f"foliage {label}: {len(transforms)} instances")

    def environment(self, eas, tag):
        L = self.lv.get("lighting", {})
        dynamic = L.get("gi", "dynamic") == "dynamic"
        sun = L.get("sun")
        if sun:
            actor = eas.spawn_actor_from_class(unreal.DirectionalLight, unreal.Vector(0, 0, 2000), rot_from_dir(sun["dir"]))
            comp = actor.get_component_by_class(unreal.DirectionalLightComponent)
            comp.set_mobility(unreal.ComponentMobility.MOVABLE if dynamic else unreal.ComponentMobility.STATIONARY)
            comp.set_intensity(sun.get("lux", 75000))
            comp.set_editor_property("use_temperature", True)
            comp.set_temperature(sun.get("kelvin", 5800))
            comp.set_cast_shadows(True)
            comp.set_editor_property("atmosphere_sun_light", True)
            tag(actor, "Sun", "Environment")
        if L.get("sky", True):
            tag(eas.spawn_actor_from_class(unreal.SkyAtmosphere, unreal.Vector(), unreal.Rotator()), "SkyAtmosphere", "Environment")
            sky = eas.spawn_actor_from_class(unreal.SkyLight, unreal.Vector(0, 0, 2000), unreal.Rotator())
            comp = sky.get_component_by_class(unreal.SkyLightComponent)
            comp.set_mobility(unreal.ComponentMobility.MOVABLE if dynamic else unreal.ComponentMobility.STATIONARY)
            comp.set_editor_property("real_time_capture", dynamic)
            tag(sky, "SkyLight", "Environment")
        if L.get("clouds"):
            try:
                tag(eas.spawn_actor_from_class(unreal.VolumetricCloud, unreal.Vector(), unreal.Rotator()), "Clouds", "Environment")
            except Exception as e:
                log(f"clouds skipped: {e}")
        fog = L.get("fog")
        if fog:
            actor = eas.spawn_actor_from_class(unreal.ExponentialHeightFog, unreal.Vector(), unreal.Rotator())
            comp = actor.get_component_by_class(unreal.ExponentialHeightFogComponent)
            comp.set_fog_density(fog.get("density", 0.02))
            comp.set_fog_height_falloff(fog.get("height_falloff", 0.2))
            comp.set_volumetric_fog(fog.get("volumetric", True))
            if fog.get("color"):
                comp.set_fog_inscattering_color(unreal.LinearColor(*fog["color"], 1.0))
            tag(actor, "HeightFog", "Environment")
        post = L.get("post", {})
        ppv = eas.spawn_actor_from_class(unreal.PostProcessVolume, unreal.Vector(), unreal.Rotator())
        ppv.set_editor_property("unbound", True)
        s = ppv.get_editor_property("settings")

        def setp(name, value):
            s.set_editor_property(f"override_{name}", True)
            s.set_editor_property(name, value)

        setp("dynamic_global_illumination_method", unreal.DynamicGlobalIlluminationMethod.LUMEN if dynamic
             else unreal.DynamicGlobalIlluminationMethod.NONE)
        setp("reflection_method", unreal.ReflectionMethod.LUMEN if dynamic else unreal.ReflectionMethod.SCREEN_SPACE)
        if "exposure" in post:
            lo, hi = post["exposure"]
            setp("auto_exposure_min_brightness", float(lo))
            setp("auto_exposure_max_brightness", float(hi))
        if "exposure_bias" in post:              # EV; replaces the project's default bias
            setp("auto_exposure_bias", float(post["exposure_bias"]))
        for key, prop in (("bloom", "bloom_intensity"), ("vignette", "vignette_intensity"),
                          ("grain", "film_grain_intensity"), ("white_balance", "white_temp"),
                          ("ao", "ambient_occlusion_intensity")):
            if key in post:
                setp(prop, float(post[key]))
        if "saturation" in post:
            setp("color_saturation", unreal.Vector4(1, 1, 1, float(post["saturation"])))
        if "contrast" in post:
            setp("color_contrast", unreal.Vector4(1, 1, 1, float(post["contrast"])))
        ppv.set_editor_property("settings", s)
        tag(ppv, "PostProcess", "Environment")

    def check_project(self):
        sl = unreal.SystemLibrary
        if not sl.get_console_variable_bool_value("r.GenerateMeshDistanceFields"):
            log("WARNING project setting Generate Mesh Distance Fields is off: Lumen has no GI data (interiors "
                "render black). Ask the user before changing Project Settings > Rendering.")

    def shots(self, cams, out, done):
        """Fly the viewport to each camera (from a tick callback), let exposure settle, take a screenshot."""
        os.makedirs(out, exist_ok=True)
        unreal.AutomationLibrary.finish_loading_before_screenshot()
        les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
        les.editor_set_viewport_realtime(True)
        order = list(cams.items())
        state = {"i": -1, "next": 0.0, "t0": time.time()}
        key = les.get_active_viewport_config_key()

        def tick(_dt):
            now = time.time()
            if now < state["next"]:
                return
            try:
                step(now)
            except Exception:
                import traceback
                unreal.unregister_slate_post_tick_callback(state["handle"])
                log("shots FAILED\n" + traceback.format_exc())
                done()

        def step(now):
            if state["i"] >= 0 and not state.get("shot"):
                cid, _ = order[state["i"]]
                unreal.AutomationLibrary.take_high_res_screenshot(1600, 900, os.path.join(out, f"{cid}.png"))
                state["shot"] = True
                state["next"] = now + 1.5
                return
            state["shot"] = False
            state["i"] += 1
            if state["i"] >= len(order):
                unreal.unregister_slate_post_tick_callback(state["handle"])
                state["handle"] = unreal.register_slate_post_tick_callback(finish_tick)
                state["next"] = now
                return
            cam = order[state["i"]][1]
            les.set_level_viewport_camera_info(cam.get_actor_location(), cam.get_actor_rotation(), key)
            les.set_level_viewport_fov(cam.camera_component.field_of_view, key)
            state["next"] = now + 4

        def finish_tick(_dt):
            if time.time() < state["next"]:
                return
            unreal.unregister_slate_post_tick_callback(state["handle"])
            log(f"shots -> {out} ({len(order)} cameras, {time.time() - state['t0']:.0f} s)")
            done()

        state["handle"] = unreal.register_slate_post_tick_callback(tick)


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", required=True)
    ap.add_argument("--round", default="r01")
    ap.add_argument("--shots", action="store_true")
    ap.add_argument("--level-only", action="store_true")
    ap.add_argument("--rebuild-materials", action="store_true")
    ap.add_argument("--exit-when-done", action="store_true")   # not "--quit": UE reads -quit anywhere on its command line
    a = ap.parse_args(argv)
    global LOG_FILE
    LOG_FILE = os.path.join(a.world, "build", "out", "unreal_import.log")
    open(LOG_FILE, "w").close()

    def finish():
        log("finished")
        if a.exit_when_done:
            unreal.SystemLibrary.quit_editor()

    try:
        with open(os.path.join(a.world, "build", "out", "level.json"), encoding="utf-8") as f:
            level = json.load(f)
        imp = Importer(level, a.world, a.rebuild_materials)
        imp.check_project()
        imp.shared()
        imp.masters()
        imp.surfaces()
        imp.decals()
        if a.level_only:
            for iid, e in level["library"].items():
                if e["kind"] in ("prop", "slab", "card"):
                    p = asset_path(imp.lib_folder(iid), e["asset"]["name"])
                    if EAL.does_asset_exist(p):
                        imp.meshes[iid] = unreal.load_asset(p)
        else:
            imp.items()
        arch = imp.arch()
        terr = imp.terrain()
        unreal.EditorLoadingAndSavingUtils.save_dirty_packages(True, True)
        cams = imp.build_level(arch, terr)
        if a.shots:
            return imp.shots(cams, os.path.join(a.world, "shots", a.round), finish)
        finish()
    except Exception:
        import traceback
        log("FAILED\n" + traceback.format_exc())
        if a.exit_when_done:
            unreal.SystemLibrary.quit_editor()
        raise


main(__import__("sys").argv[1:])
