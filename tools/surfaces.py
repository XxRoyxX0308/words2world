"""Tileable surface materials (library items of kind "surface") -> <item>/game/ (BC / N / ORM + asset.json).

item.json fields:
  source          polyhaven:<asset> | ambientcg:<asset>   (CC0 scans; find them with `w2w.py surfaces-search`)
                  flat                                    (paint, plaster, enamel: colour + roughness + noise)
  res             1k | 2k (default) | 4k                  (scans; tell the user the size before 4k downloads)
  tile_m          metres per texture repeat (default 2)
  tint            [r, g, b] multiplier;  roughness_scale; normal_strength
  color / roughness / metallic / noise                  (flat)
  layer2          id of a second surface blended in by world-space noise against tiling (e.g. a dirtier or
                  mossier version of the same material); layer2_amount 0-1 (default 0.35)
  macro           world-space brightness / roughness variation strength (default 0.15; large outdoor 0.25)
  detail          detail normal strength (default 0.4)
  grime           how strongly the architecture's baked grime (vertex colour: wall bases, corners, under
                  openings) darkens and roughens this surface (default 0.6)

Scans are never used raw: the albedo is clamped to the plausible range and flat colours always get variation.
"""
import io
import json
import os
import urllib.parse
import urllib.request
import zipfile
import zlib

import numpy as np

import texlib as tx
from common import save

UA = {"User-Agent": "words2world"}
PH_API = "https://api.polyhaven.com"
ACG_API = "https://ambientcg.com/api/v2/full_json"
PH_MAPS = {"color": "Diffuse", "normal": "nor_gl", "rough": "Rough", "ao": "AO", "metal": "Metal", "height": "Displacement"}
ACG_MAPS = {"color": "_Color", "normal": "_NormalGL", "rough": "_Roughness", "ao": "_AmbientOcclusion",
            "metal": "_Metalness", "height": "_Displacement"}


def fetch(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
        return r.read()


def ph_files(asset, res):
    info = json.loads(fetch(f"{PH_API}/files/{asset}"))
    out = {}
    for key, ph in PH_MAPS.items():
        entry = info.get(ph, {}).get(res, {})
        f = entry.get("png") if key == "height" else None
        f = f or entry.get("jpg") or entry.get("png")
        if f:
            out[key] = f
    return out


def acg_zip(asset, res):
    q = urllib.parse.urlencode({"id": asset, "include": "downloadData"})
    found = json.loads(fetch(f"{ACG_API}?{q}"))["foundAssets"]
    if not found:
        raise KeyError(f"ambientCG has no asset {asset}")
    downloads = found[0]["downloadFolders"]["default"]["downloadFiletypeCategories"]["zip"]["downloads"]
    want = f"{res.upper()}-JPG"
    for d in downloads:
        if d["attribute"] == want:
            return d
    raise KeyError(f"{asset}: no {want} download (have {[d['attribute'] for d in downloads]})")


def search(words):
    words = [w.lower() for w in words]
    assets = json.loads(fetch(f"{PH_API}/assets?t=textures"))
    hits = []
    for aid, a in assets.items():
        hay = " ".join([aid, a.get("name", "")] + a.get("categories", []) + a.get("tags", [])).lower()
        if all(w in hay for w in words):
            hits.append((a.get("download_count", 0), aid, a.get("name", "")))
    print("Poly Haven:")
    for _, aid, name in sorted(hits, reverse=True)[:15]:
        print(f"  polyhaven:{aid:34s} {name}")
    q = urllib.parse.urlencode({"type": "Material", "q": " ".join(words), "limit": 15, "sort": "Popular"})
    print("ambientCG:")
    for a in json.loads(fetch(f"{ACG_API}?{q}")).get("foundAssets", []):
        print(f"  ambientcg:{a['assetId']:34s} {a.get('displayName', '')}")


def download(source, res, dest):
    lib, asset = source.split(":", 1)
    os.makedirs(dest, exist_ok=True)
    paths = {}
    if lib == "polyhaven":
        for key, f in ph_files(asset, res).items():
            path = os.path.join(dest, f"{key}{os.path.splitext(f['url'])[1]}")
            if not os.path.exists(path) or os.path.getsize(path) != f["size"]:
                open(path, "wb").write(fetch(f["url"]))
            paths[key] = path
        url = f"https://polyhaven.com/a/{asset}"
    elif lib == "ambientcg":
        marker = os.path.join(dest, f"{asset}_{res}.done")
        if not os.path.exists(marker):
            z = zipfile.ZipFile(io.BytesIO(fetch(acg_zip(asset, res)["downloadLink"])))
            for name in z.namelist():
                for key, suffix in ACG_MAPS.items():
                    if os.path.splitext(name)[0].endswith(suffix):
                        open(os.path.join(dest, key + os.path.splitext(name)[1]), "wb").write(z.read(name))
            open(marker, "w").close()
        for key in ACG_MAPS:
            for ext in (".jpg", ".png"):
                if os.path.exists(os.path.join(dest, key + ext)):
                    paths[key] = os.path.join(dest, key + ext)
        url = f"https://ambientcg.com/view?id={asset}"
    else:
        raise ValueError(f"unknown source library {lib}")
    open(os.path.join(dest, "SOURCE.txt"), "w").write(f"{url}\nLicense: CC0\n")
    return paths


def build_scanned(it, out):
    res = it.get("res", "2k")
    maps = download(it["source"], res, it.f("src"))
    if "color" not in maps:
        raise RuntimeError(f"{it.id}: source has no colour map")
    bc = tx.load(maps["color"]) * np.array(it.get("tint", [1, 1, 1]), np.float32)
    normal = tx.load(maps["normal"]) if "normal" in maps else None
    if normal is not None and it.get("normal_strength", 1.0) != 1.0:
        xy = (normal[..., :2] - 0.5) * it["normal_strength"]
        z = np.sqrt(np.clip(1 - (xy ** 2).sum(-1, keepdims=True), 0, 1))
        normal = np.concatenate([xy + 0.5, z * 0.5 + 0.5], axis=-1)
    rough = tx.load(maps["rough"], 1) * it.get("roughness_scale", 1.0) if "rough" in maps else 0.7
    ao = tx.load(maps["ao"], 1) if "ao" in maps else 1.0
    metal = tx.load(maps["metal"], 1) if "metal" in maps else float(it.get("metallic", 0.0))
    return tx.write_maps(out, it.id, bc, normal, ao, rough, metal), bc.shape[1]


def build_flat(it, out, size=1024):
    var = float(it.get("noise", 0.08))
    seed = zlib.crc32(it.id.encode())
    n1, n2 = tx.tileable_noise(size, size / 3, seed=seed), tx.tileable_noise(size, size / 40, seed=seed + 1)
    color = np.array(it.get("color", [0.7, 0.7, 0.7]), np.float32)
    bc = color[None, None, :] * (1 + (n1[..., None] - 0.5) * var * 2 + (n2[..., None] - 0.5) * var * 0.6)
    rough = np.clip(it.get("roughness", 0.5) + (n2 - 0.5) * var * 2 + (n1 - 0.5) * var * 3, 0.03, 1)
    normal = tx.normal_from_height(n2, strength=float(it.get("normal_strength", 0.6)))
    return tx.write_maps(out, it.id, bc, normal, 1.0, rough, float(it.get("metallic", 0.0))), size


def build(world, only=()):
    for it in world.items().values():
        if it.kind != "surface" or it.get("status") == "rejected" or (only and it.id not in only):
            continue
        if not only and it.stage() == "baked":
            continue
        out = it.f("game")
        src = it.get("source", "flat")
        files, px = build_flat(it, out) if src == "flat" else build_scanned(it, out)
        tile = float(it.get("tile_m", 2.0))
        info = {"kind": "surface", "id": it.id, "rel": it.rel, "textures": files, "tile_m": tile,
                "texel_density": round(px / tile), "source": src,
                "params": {"macro": float(it.get("macro", 0.15)), "detail": float(it.get("detail", 0.4)),
                           "grime": float(it.get("grime", 0.6)), "layer2": it.get("layer2"),
                           "layer2_amount": float(it.get("layer2_amount", 0.35))}}
        save(os.path.join(out, "asset.json"), info)
        warn = "  (below 512 px/m: soft up close; raise res or lower tile_m)" if info["texel_density"] < 512 else ""
        print(f"{it.id}: {src} -> {info['texel_density']} px/m{warn}")
