"""TRELLIS.2 (1024 models by default): 2d/rgba.png of every queued prop -> 3d/model.glb + 3d/preview.png.

usage (WSL, trellis2 env, nothing else on the GPU): python trellis_batch.py <world_dir>
The queue is logs/trellis_queue.json (written by `w2w.py trellis`); without it, every prop whose rgba.png is newer
than its model is done. Finished items are skipped, so a restart continues where it stopped.

The models load once and the queue runs in passes down LADDER: every item at 1024/18 steps, then the ones that failed
at 1024/12, then the ones still failing at 512. A failed item is recorded in 3d/trellis_fail.json ("level" = rungs
failed). A non-CUDA error just moves on; a CUDA error (e.g. "device not ready") breaks the allocator, so the process
exits with code 3 and trellis_loop.sh starts a fresh one, which resumes from the recorded levels.

item.json "trellis" (all optional; defaults depend on tier):
  pipeline_type  "1024_cascade" (default: 512 sparse structure upsampled to the 1024 models), "1024" (dense 1024,
                 more VRAM), "1536_cascade", "512" (fast, coarse)
  faces          LOD0 triangle target of the exported mesh       hero 80000 | prop 30000 | small 10000
  texture        baked texture edge length                       hero 2048  | prop 2048  | small 1024
  high_faces     also write model_high.glb with this many faces for normal baking (hero default 300000)
  seed           default 42; change it to get a different reconstruction of the same image
  steps          sampler steps for all three stages (default: the LADDER rung)
  pipeline_type and steps set here override every rung for that item.
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
# 25 steps (upstream default) hit "CUDA driver error: device not ready" on some items; fewer steps get most through
LADDER = [{"pipeline_type": "1024_cascade", "steps": 18},
          {"pipeline_type": "1024_cascade", "steps": 12},
          {"pipeline_type": "512", "steps": 18}]



def level(it):
    """Rungs of LADDER this image (and seed) already failed at; a new rgba.png or seed starts over."""
    fails = load(it.f("3d", "trellis_fail.json"), {})
    same = fails.get("rgba_mtime") == mtime(it.f("2d", "rgba.png")) and         fails.get("seed") == it.get("trellis", {}).get("seed", 42)
    return fails.get("level", 0) if same else 0


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
    if level(it) >= len(LADDER):
        print(f"[skip] {iid}: failed at every rung (new pick, matte or trellis.seed resets it)", flush=True)
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


def cuda_ok():
    try:
        gc.collect()
        torch.cuda.synchronize()
        torch.cuda.empty_cache()
        return torch.ones(1, device="cuda").add_(1).item() == 2
    except Exception:
        return False


def run(it, rung, tag):
    t0 = time.time()
    cfg = {**DEFAULTS[it.tier], **LADDER[rung], **it.get("trellis", {})}
    print(f"[start] {it.id} {tag} {cfg['pipeline_type']}/{cfg['steps']} {time.strftime('%H:%M:%S')}", flush=True)
    mesh = None
    try:
        os.makedirs(it.f("3d"), exist_ok=True)
        mesh = pipeline.run(
            Image.open(it.f("2d", "rgba.png")),
            seed=cfg.get("seed", 42),
            pipeline_type=cfg["pipeline_type"],
            max_num_tokens=cfg.get("max_num_tokens", 49152),
            sparse_structure_sampler_params={"steps": cfg["steps"]},
            shape_slat_sampler_params={"steps": cfg["steps"]},
            tex_slat_sampler_params={"steps": cfg["steps"]},
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
            json.dump({"seconds": round(secs, 1), "pipeline_type": cfg["pipeline_type"], "steps": cfg["steps"],
                       "faces": cfg["faces"], "texture": cfg["texture"], "seed": cfg.get("seed", 42)}, f)
        print(f"[done] {it.id} in {secs:.0f}s {tag}", flush=True)
        return None
    except Exception as e:
        print(f"[FAIL] {it.id} at {cfg['pipeline_type']}/{cfg['steps']}: {e}", flush=True)
        traceback.print_exc()
        with open(it.f("3d", "trellis_fail.json"), "w") as f:
            json.dump({"level": rung + 1, "rgba_mtime": mtime(it.f("2d", "rgba.png")),
                       "seed": cfg.get("seed", 42), "error": str(e)[:200]}, f)
        return str(e)
    finally:
        mesh = None
        gc.collect()


for rung in range(len(LADDER)):
    batch = [it for it in todo if level(it) == rung and mtime(it.f("3d", "model.glb")) < mtime(it.f("2d", "rgba.png"))]
    if not batch:
        continue
    print(f"[pass {rung + 1}/{len(LADDER)}] {len(batch)} items at {LADDER[rung]['pipeline_type']}/"
          f"{LADDER[rung]['steps']} steps", flush=True)
    for n, it in enumerate(batch, 1):
        err = run(it, rung, f"(pass {rung + 1}: {n}/{len(batch)})")
        # "device not ready" leaves the caching allocator broken (later items die on an INTERNAL ASSERT) even
        # though a small test op still works, so any CUDA error means a fresh process
        if err is not None and ("CUDA" in err or "ASSERT" in err or not cuda_ok()):
            print("[restart] CUDA state broken by the failure; a fresh process resumes from here", flush=True)
            sys.stdout.flush()
            os._exit(3)
        torch.cuda.empty_cache()
