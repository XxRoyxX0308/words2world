"""build/out/terrain/height.npy -> terrain tiles (FBX, Nanite in the engine) + manifest.json.

usage: python w2w.py terrain <world>   (runs tools/terrain.py first, then this in headless Blender)
UV0 spans the whole terrain (0..1, v = 1 at the north edge, matching splat.png); the engine material tiles the
layer textures in world space and blends them with the splat map. Vertex colour R = 0 (no grime on terrain).
"""
import os
import sys

import bmesh
import bpy
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
from common import World, load, save


def build(world_dir):
    world = World(world_dir)
    out = world.out("terrain")
    meta = load(os.path.join(out, "terrain_meta.json"))
    h = np.load(os.path.join(out, "height.npy"))
    ny, nx = h.shape
    res = meta["res"]
    x0, y0 = meta["origin"]
    tile = max(4, int(round(meta.get("tile_m", 64) / res)))
    bpy.ops.wm.read_factory_settings(use_empty=True)
    tiles = []
    for j0 in range(0, ny - 1, tile):
        for i0 in range(0, nx - 1, tile):
            j1, i1 = min(j0 + tile, ny - 1), min(i0 + tile, nx - 1)
            name = f"SM_terrain_{i0 // tile}_{j0 // tile}"
            bm = bmesh.new()
            uv = bm.loops.layers.uv.new("UVMap")
            verts = {}
            for j in range(j0, j1 + 1):
                for i in range(i0, i1 + 1):
                    verts[(i, j)] = bm.verts.new((x0 + i * res, y0 + j * res, float(h[j, i])))
            for j in range(j0, j1):
                for i in range(i0, i1):
                    f = bm.faces.new([verts[(i, j)], verts[(i + 1, j)], verts[(i + 1, j + 1)], verts[(i, j + 1)]])
                    for loop in f.loops:
                        ii = round((loop.vert.co.x - x0) / res)
                        jj = round((loop.vert.co.y - y0) / res)
                        loop[uv].uv = (ii / (nx - 1), jj / (ny - 1))
            bmesh.ops.triangulate(bm, faces=bm.faces, quad_method="SHORT_EDGE")
            me = bpy.data.meshes.new(name)
            bm.to_mesh(me)
            bm.free()
            for p in me.polygons:
                p.use_smooth = True
            me.materials.append(bpy.data.materials.new("terrain"))
            obj = bpy.data.objects.new(name, me)
            bpy.context.collection.objects.link(obj)
            bpy.ops.object.select_all(action="DESELECT")
            obj.select_set(True)
            bpy.context.view_layer.objects.active = obj
            path = os.path.join(out, f"{name}.fbx")
            bpy.ops.export_scene.fbx(filepath=path, use_selection=True, object_types={"MESH"}, apply_unit_scale=True,
                                     apply_scale_options="FBX_SCALE_NONE", axis_forward="-Z", axis_up="Y",
                                     bake_space_transform=True, mesh_smooth_type="FACE", use_tspace=True,
                                     add_leaf_bones=False, path_mode="STRIP")
            tiles.append({"name": name, "file": path, "tris": len(me.polygons)})
            bpy.data.objects.remove(obj)
    save(os.path.join(out, "manifest.json"), {"tiles": tiles, "splat": os.path.join(out, "splat.png"),
                                              "layers": meta.get("layers", {}), "size": meta["size"],
                                              "origin": meta["origin"]})
    print(f"terrain: {len(tiles)} tiles, {sum(t['tris'] for t in tiles):,} tris -> {out}")


if __name__ == "__main__":
    build(sys.argv[sys.argv.index("--") + 1])
