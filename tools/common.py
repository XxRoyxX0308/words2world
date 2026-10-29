"""Shared paths, config and the item library used by every words2world tool (standard library only).

A world is one folder (default worlds/<name>/):

  brief.md                   what the user asked for (text); refs/ holds any reference images they gave
  plan/world.json            style bible: look, palette, prompt suffixes, spaces, GPU settings
  plan/spaces/<space>.json   one per space: what it is, concept picks, takeaways, which items live there
  concepts/<space>/          ComfyUI space concepts (concept_<n>.png + prompts.json)
  library/<category>/<family>/<id>/
      item.json              the spec (kind, prompt, size, variant_of, pick, ...)
      2d/                    cand_<n>.png, prompts.json, sheet.png, rgba.png, review.png
      3d/                    model.glb (+ model_high.glb), preview.png      (TRELLIS)
      game/                  engine-ready files + asset.json                  (Blender / CPU, build phase)
  build/
      scene.json             what the engine level contains (hand-written by the build agent)
      architecture.json      the buildings, as parametric parts
      terrain.json           terrain + foliage rules
      out/                   generated: arch/, terrain/, level.json, anchors.json, budget.json
  shots/<round>/             engine review screenshots
  log.jsonl                  timing of every generation / bake / import

World convention: metres, +X east, +Y north, +Z up, ground at z = 0 before terrain, yaw in degrees counter-clockwise
seen from above, an object's front faces -Y at yaw 0.
"""
import glob
import json
import os
import re
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KINDS = ("prop", "slab", "decal", "card", "surface")
TIERS = ("hero", "prop", "small")
ID_RE = re.compile(r"^[a-z0-9]+(_[a-z0-9]+)*$")

DEFAULT_CONFIG = {
    "blender": "C:/Program Files/Blender Foundation/Blender 5.2/blender.exe",
    "unreal_editor": "C:/Program Files/Epic Games/UE_5.6/Engine/Binaries/Win64/UnrealEditor.exe",
    "unreal_project": "",
    "comfy_url": "http://127.0.0.1:8188",
    "wsl_conda": "~/miniconda3/etc/profile.d/conda.sh",
    "wsl_trellis": "~/TRELLIS.2",
    "wsl_comfy": "~/ComfyUI",
}

# texel density targets (px per metre) and LOD ratios per tier, see docs/ART_RULES.md
TEXEL_DENSITY = {"hero": 1024, "prop": 512, "small": 512}
LOD_RATIOS = {"hero": [1, 0.5, 0.25, 0.1], "prop": [1, 0.5, 0.2], "small": [1, 0.35]}
LOD_SCREEN = [1.0, 0.4, 0.15, 0.05]


def load(path, default=None):
    if not os.path.exists(path):
        if default is not None:
            return default
        raise FileNotFoundError(path)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def config():
    return {**DEFAULT_CONFIG, **load(os.path.join(REPO, "w2w.config.json"), {})}


def wsl_path(p):
    """D:\\a\\b -> /mnt/d/a/b (paths already in WSL form are returned unchanged)."""
    p = os.path.abspath(p) if not p.startswith("/") else p
    if len(p) > 1 and p[1] == ":":
        return "/mnt/" + p[0].lower() + p[2:].replace("\\", "/")
    return p


def mtime(path):
    return os.path.getmtime(path) if os.path.exists(path) else 0.0


class Item:
    """One library entry. Its stage is derived from the files on disk, never stored, so it cannot go stale."""

    def __init__(self, world, json_path):
        self.world = world
        self.path = json_path
        self.dir = os.path.dirname(json_path)
        self.spec = load(json_path)
        self.id = self.spec["id"]

    def __getitem__(self, key):
        return self.spec[key]

    def get(self, key, default=None):
        return self.spec.get(key, default)

    @property
    def kind(self):
        return self.spec.get("kind", "prop")

    @property
    def tier(self):
        return self.spec.get("tier", "prop")

    @property
    def rel(self):
        """category/family/id"""
        return os.path.relpath(self.dir, self.world.p("library")).replace("\\", "/")

    def f(self, *parts):
        return os.path.join(self.dir, *parts)

    def save(self):
        save(self.path, self.spec)

    # ---- files
    def candidates(self):
        out = []
        for c in glob.glob(self.f("2d", "cand_*.png")):
            m = re.match(r"cand_(\d+)\.png$", os.path.basename(c))
            if m:
                out.append((int(m.group(1)), c))
        return [c for _, c in sorted(out)]

    def picked(self):
        pick = self.spec.get("pick")
        return None if pick is None else self.f("2d", f"cand_{pick}.png")

    def needs_matte(self):
        if self.kind == "decal":
            return self.spec.get("alpha", "luma") == "matte"
        return self.kind in ("prop", "card")

    def forge_done(self):
        """True when the GPU phase has produced everything the build phase needs."""
        if self.kind == "surface":
            return True
        if self.picked() is None or not os.path.exists(self.picked()):
            return False
        if self.needs_matte() and mtime(self.f("2d", "rgba.png")) < mtime(self.picked()):
            return False
        if self.kind == "prop" and mtime(self.f("3d", "model.glb")) < mtime(self.f("2d", "rgba.png")):
            return False
        return True

    def stage(self):
        """planned | drafting | picked | matted | ready | baked | rejected

        ready = the forge phase is finished for this item, the build phase still has to process it.
        baked = game/asset.json exists and is newer than every source it was made from."""
        if self.spec.get("status") == "rejected":
            return "rejected"
        game = mtime(self.f("game", "asset.json"))
        if self.kind == "surface":
            return "baked" if game else "ready"
        if self.forge_done():
            src = max(mtime(self.picked()), mtime(self.f("2d", "rgba.png")), mtime(self.f("3d", "model.glb")),
                      mtime(self.path) if self.kind in ("decal", "slab", "card") else 0)
            return "baked" if game >= src else "ready"
        if self.picked() and os.path.exists(self.picked()):
            if self.needs_matte() and os.path.exists(self.f("2d", "rgba.png")) and \
                    mtime(self.f("2d", "rgba.png")) >= mtime(self.picked()):
                return "matted"
            return "picked"
        return "drafting" if self.candidates() else "planned"

    def asset(self):
        """game/asset.json (None until baked)."""
        return load(self.f("game", "asset.json"), {}) or None


class World:
    def __init__(self, name_or_path):
        p = name_or_path
        if not (os.path.isdir(p) and (os.path.exists(os.path.join(p, "plan")) or os.path.exists(os.path.join(p, "brief.md")))):
            p = os.path.join(REPO, "worlds", name_or_path)
        self.root = os.path.abspath(p)
        self.name = os.path.basename(self.root)
        self.plan = load(self.p("plan", "world.json"), {})
        self._items = None

    def p(self, *parts):
        return os.path.join(self.root, *parts)

    def out(self, *parts):
        return self.p("build", "out", *parts)

    def gen(self, key, default=None):
        """plan.generation.<key>"""
        return self.plan.get("generation", {}).get(key, default)

    # ---- library
    def items(self, refresh=False):
        if self._items is None or refresh:
            self._items = {}
            for path in sorted(glob.glob(self.p("library", "*", "*", "*", "item.json"))):
                it = Item(self, path)
                if it.id in self._items:
                    raise ValueError(f"duplicate item id {it.id}: {self._items[it.id].dir} and {it.dir}")
                self._items[it.id] = it
        return self._items

    def item(self, iid):
        try:
            return self.items()[iid]
        except KeyError:
            raise KeyError(f"no item '{iid}' in {self.p('library')}") from None

    def add_item(self, spec):
        """Create or update library/<category>/<family>/<id>/item.json. Existing fields not in spec are kept
        (pick, bake overrides, notes written by an earlier agent). A variant inherits category, family, kind,
        tier, size and condition from variant_of unless it sets them."""
        spec = dict(spec)
        iid = spec.get("id", "")
        if not ID_RE.match(iid):
            raise ValueError(f"bad id '{iid}': lowercase letters, digits and single underscores")
        parent = spec.get("variant_of")
        if parent:
            ps = self.item(parent).spec
            for k in ("category", "family", "kind", "tier", "size", "condition", "collision", "spaces", "alpha",
                      "roughness", "aspect", "frame"):
                if k not in spec and k in ps:
                    spec[k] = ps[k]
        for k in ("category", "family"):
            if not spec.get(k) or not ID_RE.match(spec[k]):
                raise ValueError(f"{iid}: '{k}' is required (lowercase, underscores)")
        spec.setdefault("kind", "prop")
        if spec["kind"] not in KINDS:
            raise ValueError(f"{iid}: kind must be one of {KINDS}")
        if spec["kind"] in ("prop", "slab", "card") and not spec.get("size"):
            raise ValueError(f"{iid}: 'size' is required, e.g. {{\"height\": 0.75}} (metres)")
        if spec["kind"] == "decal" and not spec.get("size"):
            raise ValueError(f"{iid}: decals need 'size': [width, height] in metres")
        if spec["kind"] != "surface" and not spec.get("prompt") and not spec.get("edit"):
            raise ValueError(f"{iid}: needs 'prompt' (or 'edit' + 'variant_of')")
        existing = self.items().get(iid)
        path = os.path.join(self.p("library"), spec["category"], spec["family"], iid, "item.json")
        if existing and os.path.normcase(existing.path) != os.path.normcase(path):
            raise ValueError(f"{iid} already exists at {existing.rel}; move the folder by hand or pick another id")
        merged = {**(existing.spec if existing else {}), **spec}
        merged.setdefault("created", time.strftime("%Y-%m-%d %H:%M"))
        save(path, merged)
        self._items = None
        return path

    def log(self, record):
        record = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), **record}
        with open(self.p("log.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ---- spaces
    def spaces(self):
        out = {}
        for path in sorted(glob.glob(self.p("plan", "spaces", "*.json"))):
            s = load(path)
            out[s.get("id", os.path.splitext(os.path.basename(path))[0])] = s
        return out
