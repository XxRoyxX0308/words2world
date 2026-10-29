"""TRELLIS.2 (1024 models by default): 2d/rgba.png of every queued prop -> 3d/model.glb + 3d/preview.png.

usage (WSL, trellis2 env, nothing else on the GPU): python trellis_batch.py <world_dir>
The queue is logs/trellis_queue.json (written by `w2w.py trellis`); without it, every prop whose rgba.png is newer
than its model is done. Finished items are skipped, so a restart continues where it stopped. On the first failure
the process exits with code 3 (CUDA errors poison the context); trellis_loop.sh starts a fresh one.

item.json "trellis" (all optional; defaults depend on tier):
  pipeline_type  "1024_cascade" (default: 512 sparse structure upsampled to the 1024 models), "1024" (dense 1024,
                 more VRAM), "1536_cascade", "512" (fast, coarse)
  faces          LOD0 triangle target of the exported mesh       hero 80000 | prop 30000 | small 10000
  texture        baked texture edge length                       hero 2048  | prop 2048  | small 1024
  high_faces     also write model_high.glb with this many faces for normal baking (hero default 300000)
  seed           default 42; change it to get a different reconstruction of the same image
"""
import os
os.environ["OPENCV_IO_ENABLE_OPENEXR"] = "1"
os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"
import gc
import json
import sys
import time
import traceback

sys.path.insert(0, os.path.expanduser("~/TRELLIS.2"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import numpy as np
import torch
from PIL import Image
from common import World, load, mtime

DEFAULTS = {"hero": {"faces": 80000, "texture": 2048, "high_faces": 300000},
            "prop": {"faces": 30000, "texture": 2048},
            "small": {"faces": 10000, "texture": 1024}}
PIPELINE_TYPE = "1024_cascade"

world = World(sys.argv[1])
queue = load(world.p("logs", "trellis_queue.json"), {}).get("ids")
items = world.items()
todo = []
for iid in (queue if queue else sorted(items)):
    it = items.get(iid)
    if it is None or it.kind != "prop" or it.get("status") == "rejected":
        continue
    rgba, glb = it.f("2d", "rgba.png"), it.f("3d", "model.glb")
    if not os.path.exists(rgba):
        print(f"[skip] {iid}: no rgba.png", flush=True)
        continue
    if mtime(glb) >= mtime(rgba):
        continue
    fails = load(it.f("3d", "trellis_fail.json"), {})
    if fails.get("count", 0) >= 2 and fails.get("rgba_mtime") == mtime(rgba) and \
            fails.get("seed") == it.get("trellis", {}).get("seed", 42):
        print(f"[skip] {iid}: failed twice on this image (new pick, matte or trellis.seed resets it)", flush=True)
        continue
    todo.append(it)
total = len(queue) if queue else len(todo)
print(f"[queue] {len(todo)} to do ({total - len(todo)} of {total} already finished)", flush=True)
if not todo:
    sys.exit(0)

from trellis2.pipelines import Trellis2ImageTo3DPipeline
import o_voxel

pipeline = Trellis2ImageTo3DPipeline.from_pretrained("microsoft/TRELLIS.2-4B")
pipeline.cuda()
envmap = None
try:
    import cv2
    from trellis2.renderers import EnvMap
    from trellis2.utils import render_utils
    hdr = cv2.imread(os.path.expanduser("~/TRELLIS.2/assets/hdri/studio.exr"), cv2.IMREAD_UNCHANGED)
    envmap = EnvMap(torch.tensor(cv2.cvtColor(hdr, cv2.COLOR_BGR2RGB), dtype=torch.float32, device="cuda"))
except Exception as e:
    print(f"[warn] no preview renders: {e}", flush=True)


def export(mesh, path, faces, texture, remesh):
    glb = o_voxel.postprocess.to_glb(
        vertices=mesh.vertices, faces=mesh.faces, attr_volume=mesh.attrs, coords=mesh.coords,
        attr_layout=mesh.layout, voxel_size=mesh.voxel_size, aabb=[[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]],
        decimation_target=faces, texture_size=texture, remesh=remesh, remesh_band=1, remesh_project=0,
        verbose=False)
    glb.export(path)       # PNG textures: Blender builds without WebP glTF support would import untextured


def preview(mesh, path):
    """4 views (shaded on top, normals below) so the forge agent can judge the shape without Blender."""
    res = render_utils.render_snapshot(mesh, resolution=384, r=2, fov=40, nviews=4, envmap=envmap, verbose=False)
    rows = [np.concatenate(res[k], axis=1) for k in ("shaded", "normal") if k in res]
    Image.fromarray(np.concatenate(rows, axis=0)).save(path)


for n, it in enumerate(todo, 1):
    t0 = time.time()
    done_before = total - len(todo) + n - 1
    print(f"[start] {it.id} ({done_before + 1}/{total}) {time.strftime('%H:%M:%S')}", flush=True)
    mesh = None
    cfg = {**DEFAULTS[it.tier], **it.get("trellis", {})}
    try:
        os.makedirs(it.f("3d"), exist_ok=True)
        mesh = pipeline.run(
            Image.open(it.f("2d", "rgba.png")),
            seed=cfg.get("seed", 42),
            pipeline_type=cfg.get("pipeline_type", PIPELINE_TYPE),
            max_num_tokens=cfg.get("max_num_tokens", 49152),
            sparse_structure_sampler_params={"steps": 25},
            shape_slat_sampler_params={"steps": 25},
            tex_slat_sampler_params={"steps": 25},
        )[0]
        mesh.simplify(16777216)  # nvdiffrast limit
        gc.collect()
        torch.cuda.empty_cache()
        if cfg.get("high_faces"):
            # same transform as the low mesh so the two line up for the normal bake; its texture is never used
            export(mesh, it.f("3d", "model_high.glb"), cfg["high_faces"], 512, False)
            gc.collect()
            torch.cuda.empty_cache()
        export(mesh, it.f("3d", "model.glb"), cfg["faces"], cfg["texture"], True)
        if envmap is not None:
            try:
                preview(mesh, it.f("3d", "preview.png"))
            except Exception as e:      # a missing preview must not cost the model
                print(f"[warn] {it.id}: preview failed ({e})", flush=True)
        secs = time.time() - t0
        with open(it.f("3d", "trellis.json"), "w") as f:
            json.dump({"seconds": round(secs, 1), "pipeline_type": cfg.get("pipeline_type", PIPELINE_TYPE),
                       "faces": cfg["faces"], "texture": cfg["texture"], "seed": cfg.get("seed", 42)}, f)
        print(f"[done] {it.id} in {secs:.0f}s ({done_before + 1}/{total})", flush=True)
    except Exception:
        print(f"[FAIL] {it.id}", flush=True)
        traceback.print_exc()
        fail_path = it.f("3d", "trellis_fail.json")
        fails = load(fail_path, {})
        rg = mtime(it.f("2d", "rgba.png"))
        same = fails.get("rgba_mtime") == rg and fails.get("seed") == cfg.get("seed", 42)
        with open(fail_path, "w") as f:
            json.dump({"count": fails.get("count", 0) + 1 if same else 1, "rgba_mtime": rg, "seed": cfg.get("seed", 42)}, f)
        sys.stdout.flush()
        os._exit(3)
    finally:
        mesh = None
        gc.collect()
        torch.cuda.empty_cache()
