"""FLUX.2 klein 4B through a running ComfyUI: text -> image, and (text + reference images) -> image.

Only the Apache-2.0 klein 4B weights are used; the 9B weights are non-commercial and deliberately not selectable.
Standard library only (Pillow is used when it is installed, to read reference image sizes).

  generate(server, prompt, out, seed, megapixels, aspect, refs=[...])
    refs = []          plain text-to-image
    refs = [a.png]     edit / variation: the image is encoded and attached as a reference latent
                       ("the same cabinet with both doors open", "isolate the armchair on a white background",
                        "this room, but with shelves full of jars along the back wall")
    refs = [a, b, ...] several references are chained (klein supports multiple ReferenceLatent nodes)
"""
import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

MODEL = "flux2-klein-4b"
UNET = "flux-2-klein-4b-fp8.safetensors"
CLIP = "qwen_3_4b.safetensors"
VAE = "flux2-vae.safetensors"


def http_json(url, payload=None, timeout=60):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return json.loads(body) if body else {}


def alive(server):
    try:
        http_json(f"{server}/system_stats", timeout=3)
        return True
    except Exception:
        return False


def upload(server, path):
    """POST an image to ComfyUI's input folder; returns the name LoadImage expects."""
    boundary = uuid.uuid4().hex
    name = f"w2w_{uuid.uuid4().hex[:8]}_{os.path.basename(path)}"
    ctype = mimetypes.guess_type(path)[0] or "image/png"
    with open(path, "rb") as f:
        blob = f.read()
    parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
             f"Content-Type: {ctype}\r\n\r\n".encode() + blob + b"\r\n",
             f"--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n".encode(),
             f"--{boundary}--\r\n".encode()]
    req = urllib.request.Request(f"{server}/upload/image", data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        info = json.loads(r.read())
    return f"{info['subfolder']}/{info['name']}" if info.get("subfolder") else info["name"]


def image_aspect(path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.width / im.height
    except Exception:
        return 1.0


def build_graph(prompt, seed, steps, width, height, ref_names=(), ref_mp=1.0):
    g = {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": UNET, "weight_dtype": "default"}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP, "type": "flux2", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": VAE}},
        "latent": {"class_type": "EmptyFlux2LatentImage", "inputs": {"width": width, "height": height, "batch_size": 1}},
        "sigmas": {"class_type": "Flux2Scheduler", "inputs": {"steps": steps, "width": width, "height": height}},
        "text": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["clip", 0], "text": prompt}},
    }
    cond = ["text", 0]
    for i, name in enumerate(ref_names):
        g[f"load{i}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        g[f"scale{i}"] = {"class_type": "ImageScaleToTotalPixels",
                          "inputs": {"image": [f"load{i}", 0], "upscale_method": "lanczos", "megapixels": ref_mp,
                                     "resolution_steps": 16}}
        g[f"enc{i}"] = {"class_type": "VAEEncode", "inputs": {"pixels": [f"scale{i}", 0], "vae": ["vae", 0]}}
        g[f"ref{i}"] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": cond, "latent": [f"enc{i}", 0]}}
        cond = [f"ref{i}", 0]
    g.update({
        "zero": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": cond}},
        "guider": {"class_type": "CFGGuider", "inputs": {"model": ["unet", 0], "positive": cond, "negative": ["zero", 0], "cfg": 1.0}},
        "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": seed}},
        "sampler": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "sample": {"class_type": "SamplerCustomAdvanced",
                   "inputs": {"noise": ["noise", 0], "guider": ["guider", 0], "sampler": ["sampler", 0],
                              "sigmas": ["sigmas", 0], "latent_image": ["latent", 0]}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sample", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "SaveImage", "inputs": {"images": ["decode", 0], "filename_prefix": "w2w"}},
    })
    return g


def generate(server, prompt, out, seed=1, steps=4, megapixels=1.0, aspect=None, refs=(), timeout=1800):
    """Write one image to out; returns seconds. aspect = width / height (default: first reference's, else 1)."""
    t0 = time.time()
    if aspect is None:
        aspect = image_aspect(refs[0]) if refs else 1.0
    height = int(round((megapixels * 1e6 / aspect) ** 0.5 / 16)) * 16
    width = int(round(height * aspect / 16)) * 16
    names = [upload(server, r) for r in refs]
    graph = build_graph(prompt, seed, steps, width, height, names, megapixels)
    try:
        pid = http_json(f"{server}/prompt", {"prompt": graph, "client_id": uuid.uuid4().hex})["prompt_id"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"ComfyUI rejected the workflow: {e.read().decode()[:2000]}")
    while True:
        hist = http_json(f"{server}/history/{pid}").get(pid)
        if hist and hist.get("status", {}).get("completed"):
            break
        if hist and hist.get("status", {}).get("status_str") == "error":
            raise RuntimeError(f"ComfyUI execution error: {json.dumps(hist['status'])[:2000]}")
        if time.time() - t0 > timeout:
            raise TimeoutError("ComfyUI did not finish in time")
        time.sleep(0.5)
    img = hist["outputs"]["save"]["images"][0]
    q = urllib.parse.urlencode({"filename": img["filename"], "subfolder": img["subfolder"], "type": img["type"]})
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with urllib.request.urlopen(f"{server}/view?{q}") as r, open(out, "wb") as f:
        f.write(r.read())
    return time.time() - t0


def free(server):
    """Unload models from VRAM (ComfyUI keeps running)."""
    http_json(f"{server}/free", {"unload_models": True, "free_memory": True})
