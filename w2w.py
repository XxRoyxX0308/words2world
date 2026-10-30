"""words2world command line. Run with Windows Python 3 (numpy + Pillow) from the repo root:

    python w2w.py <command> <world> [...]          (world = a name under worlds/ or a path)

Forge phase (GPU: ComfyUI and TRELLIS take turns; Blender and Unreal must be closed)
  new <world> [--brief FILE]              create the world folder (brief.md, plan/, library/, build/)
  comfy start|stop|status [--lowvram]     ComfyUI in WSL (--lowvram while Unreal / Blender hold VRAM)
  concept <world> <space> --prompt P [--ref IMG ...] [--n 2] [--aspect 1.6]
                                          space concept images -> concepts/<space>/concept_<n>.png
  add <world> <items.json>                create / update library items from a list (see docs/FORMATS.md)
  gen <world> <id> [--prompt P] [--review R] [--ref IMG ...] [--n 1]
                                          next 2D candidate(s) of one item; variants use their parent's pick
  batch <world> [id ...] [--n 1]          one candidate for every item that has none yet
  sheet <world> [id ...] [--overview]     contact sheets (2d/sheet.png) of items without a pick; --overview: the
                                          latest candidate of every unpicked item, 12 per sheet (logs/overview_*.png)
  pick <world> <id> <n> [--why TEXT]      choose candidate n
  reject <world> <id> --why TEXT          drop an item (kept on disk, skipped everywhere)
  matte <world> [id ...]                  stop ComfyUI, remove backgrounds of picks (props, cards, matte decals)
  trellis <world> [id ...]                stop ComfyUI, TRELLIS every matted prop (long: run in the background)
  progress <world>                        TRELLIS progress: done / total, current item, ETA
  decals <world> [id ...]                 picked decal images -> game/ textures (CPU, any phase)

Build phase (Blender + Unreal + ComfyUI --lowvram; never TRELLIS)
  bake <world> [id ...] [--retexture]     headless Blender: props / slabs / cards -> game/ (FBX, LODs, textures)
  surfaces <world> [id ...]               tileable surface materials (CC0 scans or flat) -> game/
  surfaces-search <word ...>              search Poly Haven + ambientCG
  arch <world>                            headless Blender: build/architecture.json -> build/out/arch/
  terrain <world>                         build/terrain.json -> heightmap, splat, foliage, terrain meshes
  scene <world>                           build/scene.json -> build/out/level.json (+ budget.json)
  engine <world> [--shots] [--round N] [--level-only] [--live]
                                          import into Unreal and build the level; --shots renders every camera

Any phase
  status <world> [--all]                  stages per item, queues, what the next phase has to do
  handoff <world>                         facts for the hand-off prompt to the next phase
"""
import glob
import json
import os
import subprocess
import sys
import time
import zlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools"))
from common import REPO, World, config, load, mtime, save, wsl_path  # noqa: E402

GPU = os.path.join(REPO, "gpu")
CONDA = "source ~/miniconda3/etc/profile.d/conda.sh"


# ---------------------------------------------------------------- helpers
def take(args, flag, default=None, many=False):
    vals = []
    while flag in args:
        i = args.index(flag)
        vals.append(args[i + 1])
        del args[i:i + 2]
    if many:
        return vals
    return vals[-1] if vals else default


def flag(args, name):
    if name in args:
        args.remove(name)
        return True
    return False


def wsl(cmd, check=True, capture=False):
    r = subprocess.run(["wsl", "-e", "bash", "-lc", cmd], capture_output=capture, text=True)
    if check and r.returncode != 0:
        sys.exit(f"WSL command failed ({r.returncode}): {cmd}\n{r.stdout or ''}{r.stderr or ''}")
    return r


def running(exe):
    r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {exe}", "/NH"], capture_output=True, text=True)
    return exe.lower() in r.stdout.lower()


def gpu_exclusive(args):
    """Forge GPU steps need the whole card: refuse while Blender or Unreal run, stop ComfyUI."""
    busy = [e for e in ("UnrealEditor.exe", "blender.exe") if running(e)]
    if busy and not flag(args, "--force"):
        sys.exit(f"{', '.join(busy)} is running: close it first (TRELLIS 1024 / RMBG need the whole 12 GB), "
                 f"or pass --force")
    out = wsl(f"bash {wsl_path(GPU)}/comfy_stop.sh", check=False, capture=True).stdout.strip()
    if "COMFY_STOPPED" not in out:
        sys.exit(f"could not stop ComfyUI: {out}")


def no_trellis():
    r = wsl("pgrep -f '[t]rellis_batch.py' > /dev/null && echo BUSY || echo FREE", check=False, capture=True)
    if "BUSY" in r.stdout:
        sys.exit("TRELLIS is still running in WSL: wait for it (python w2w.py progress <world>) before the build phase")


def comfy_url():
    return config()["comfy_url"]


def require_comfy():
    import comfy
    if not comfy.alive(comfy_url()):
        sys.exit("ComfyUI is not running: python w2w.py comfy start   (build phase: add --lowvram)")


def seed_base(it):
    return it.get("seed_base", zlib.crc32(it.id.encode()) % 100000)


def world_path(world, p):
    """Reference images may be given relative to the world folder."""
    return p if os.path.isabs(p) else (world.p(p) if os.path.exists(world.p(p)) else os.path.abspath(p))


# ---------------------------------------------------------------- commands: setup
def cmd_new(args):
    name = args.pop(0)
    brief = take(args, "--brief")
    root = name if os.path.isabs(name) else os.path.join(REPO, "worlds", name)
    for d in ("plan/spaces", "concepts", "library", "build/out", "shots", "logs", "refs"):
        os.makedirs(os.path.join(root, d), exist_ok=True)
    bpath = os.path.join(root, "brief.md")
    if brief and not os.path.exists(bpath):
        with open(brief, encoding="utf-8") as f, open(bpath, "w", encoding="utf-8") as g:
            g.write(f.read())
    elif not os.path.exists(bpath):
        open(bpath, "w", encoding="utf-8").write("# Brief\n\n(the user's description goes here)\n")
    plan = os.path.join(root, "plan", "world.json")
    if not os.path.exists(plan):
        save(plan, load(os.path.join(REPO, "docs", "templates", "world.json")))
    print(f"world ready: {root}\n  next: edit brief.md and plan/world.json, then plan the spaces (see world-forge)")


def cmd_comfy(args):
    sub = args.pop(0)
    if sub == "start":
        extra = " --lowvram" if flag(args, "--lowvram") else ""
        r = wsl(f"bash {wsl_path(GPU)}/comfy_start.sh{extra}", check=False, capture=True)
        print(r.stdout.strip())
        if "COMFY_READY" not in r.stdout:
            sys.exit(1)
    elif sub == "stop":
        print(wsl(f"bash {wsl_path(GPU)}/comfy_stop.sh", check=False, capture=True).stdout.strip())
    else:
        import comfy
        print("COMFY_RUNNING" if comfy.alive(comfy_url()) else "COMFY_DOWN")


# ---------------------------------------------------------------- commands: 2D
def cmd_concept(args):
    import comfy
    world = World(args.pop(0))
    space = args.pop(0)
    prompt = take(args, "--prompt")
    refs = [world_path(world, r) for r in take(args, "--ref", many=True)]
    n = int(take(args, "--n", 2))
    aspect = take(args, "--aspect")
    if not prompt:
        sys.exit("--prompt is required: describe this one space (a room, a wall, a corner), not the whole world")
    require_comfy()
    d = world.p("concepts", space)
    os.makedirs(d, exist_ok=True)
    hist_path = os.path.join(d, "prompts.json")
    hist = load(hist_path, [])
    suffix = world.gen("concept_suffix", "")
    if refs:
        suffix = world.gen("concept_edit_suffix", suffix)
    for _ in range(n):
        k = 0
        while os.path.exists(os.path.join(d, f"concept_{k}.png")):
            k += 1
        out = os.path.join(d, f"concept_{k}.png")
        seed = 7000 + k + zlib.crc32(space.encode()) % 1000
        secs = comfy.generate(comfy_url(), f"{prompt} {suffix}".strip(), out, seed=seed,
                              megapixels=world.gen("concept_megapixels", 1.2),
                              aspect=float(aspect) if aspect else (None if refs else 1.6), refs=refs)
        hist.append({"concept": k, "seed": seed, "prompt": prompt, "refs": [os.path.relpath(r, world.root) for r in refs],
                     "seconds": round(secs, 1), "time": time.strftime("%Y-%m-%d %H:%M")})
        save(hist_path, hist)
        world.log({"step": "concept", "space": space, "n": k, "seconds": round(secs, 1)})
        print(f"{space} concept_{k} {secs:.1f}s -> {out}", flush=True)


def cmd_add(args):
    world = World(args.pop(0))
    data = load(args.pop(0))
    items = data["items"] if isinstance(data, dict) else data
    errors = 0
    pending = list(items)
    # parents first: a variant needs its parent on disk to inherit from it
    for _ in range(3):
        later = []
        for spec in pending:
            try:
                world.add_item(spec)
                print(f"ok   {spec['id']}")
            except KeyError:
                later.append(spec)
            except ValueError as e:
                errors += 1
                print(f"FAIL {e}")
        pending = later
    for spec in pending:
        errors += 1
        print(f"FAIL {spec['id']}: variant_of '{spec.get('variant_of')}' does not exist")
    print(f"{len(items) - errors} items written, {errors} errors")


def suffix_for(world, it, edit):
    if it.get("suffix"):
        return it["suffix"]
    sfx = world.gen("suffix", {})
    if edit:
        return sfx.get("edit", "")
    return sfx.get(it.kind, sfx.get("prop", ""))


def aspect_for(it):
    if "aspect" in it.spec:
        return float(it["aspect"])
    if it.kind == "decal":
        w, h = it["size"]
        return w / h
    return 1.0


def generate_one(world, it, prompt=None, review=None, refs=()):
    import comfy
    os.makedirs(it.f("2d"), exist_ok=True)
    hist_path = it.f("2d", "prompts.json")
    hist = load(hist_path, [])
    if review and hist:
        hist[-1]["review"] = review
    all_refs = list(refs)
    parent = it.get("variant_of")
    edit = bool(it.get("edit")) and bool(parent)
    if edit:
        p = world.item(parent)
        if not p.picked() or not os.path.exists(p.picked()):
            sys.exit(f"{it.id} is a variant of {parent}, which has no pick yet: generate and pick the parent first")
        all_refs.insert(0, p.picked())
    all_refs += [world_path(world, r) for r in it.get("refs", [])]
    base = it.get("edit") if edit else it.get("prompt")
    text = prompt or (hist[-1]["prompt"] if hist else base)
    n = len(it.candidates())
    while os.path.exists(it.f("2d", f"cand_{n}.png")):
        n += 1
    seed = seed_base(it) + n
    out = it.f("2d", f"cand_{n}.png")
    full = f"{text} {suffix_for(world, it, edit or bool(all_refs))}".strip()
    secs = comfy.generate(comfy_url(), full, out, seed=seed, megapixels=world.gen("megapixels", 1.0),
                          aspect=aspect_for(it) if not all_refs or it.kind == "decal" else None, refs=all_refs)
    hist.append({"cand": n, "seed": seed, "prompt": text, "prompt_changed": bool(prompt),
                 "refs": [os.path.relpath(r, world.root) for r in all_refs], "seconds": round(secs, 1)})
    save(hist_path, hist)
    if prompt and not edit:
        it.spec["prompt"] = prompt            # the latest prompt is the item's prompt
        it.save()
    elif prompt and edit:
        it.spec["edit"] = prompt
        it.save()
    world.log({"step": "gen", "id": it.id, "cand": n, "seconds": round(secs, 1)})
    print(f"{it.id} cand_{n} seed={seed} {secs:.1f}s -> {out}", flush=True)
    return out


def cmd_gen(args):
    world = World(args.pop(0))
    it = world.item(args.pop(0))
    prompt, review = take(args, "--prompt"), take(args, "--review")
    refs = [world_path(world, r) for r in take(args, "--ref", many=True)]
    n = int(take(args, "--n", 1))
    require_comfy()
    for i in range(n):
        generate_one(world, it, prompt if i == 0 else None, review if i == 0 else None, refs)


def cmd_batch(args):
    world = World(args.pop(0))
    n = int(take(args, "--n", 1))
    only = set(args)
    require_comfy()
    items = world.items()
    # parents before variants (a variant is generated from its parent's pick)
    order = sorted(items.values(), key=lambda it: (bool(it.get("variant_of")), it.rel))
    for it in order:
        if only and it.id not in only or it.kind == "surface" or it.get("status") == "rejected":
            continue
        if it.candidates() and not only:
            continue
        if it.get("variant_of") and it.get("edit"):
            p = items.get(it["variant_of"])
            if not p or not p.picked() or not os.path.exists(p.picked()):
                print(f"[wait] {it.id}: parent {it['variant_of']} has no pick yet")
                continue
        for _ in range(n):
            generate_one(world, it)


def cmd_sheet(args):
    import sheets
    world = World(args.pop(0))
    overview = flag(args, "--overview")
    only = set(args)
    made = []
    if overview:     # latest candidate of every unpicked item, 12 per sheet, labelled with the item id
        todo = [it for it in sorted(world.items().values(), key=lambda i: i.rel)
                if it.candidates() and it.get("pick") is None and it.get("status") != "rejected"
                and (not only or it.id in only)]
        for k in range(0, len(todo), 12):
            chunk = todo[k:k + 12]
            out = world.p("logs", f"overview_{k // 12:02d}.png")
            sheets.contact([it.candidates()[-1] for it in chunk], out, cell=420, cols=4,
                           names=[f"{it.id} ({len(it.candidates()) - 1})" for it in chunk])
            made.append(out)
        print("\n".join(made) if made else "no unpicked candidates")
        return
    for it in world.items().values():
        if it.kind == "surface" or it.get("status") == "rejected":
            continue
        if only and it.id not in only or (not only and it.get("pick") is not None):
            continue
        if it.candidates():
            sheets.contact(it.candidates(), it.f("2d", "sheet.png"), labels="cand_")
            made.append(it.f("2d", "sheet.png"))
    for space in sorted(os.listdir(world.p("concepts"))) if os.path.isdir(world.p("concepts")) else []:
        cs = sorted(glob.glob(world.p("concepts", space, "concept_*.png")),
                    key=lambda p: int(os.path.basename(p)[8:-4]))
        if cs and (not only or space in only):
            sheets.contact(cs, world.p("concepts", space, "sheet.png"), labels="concept_", cell=640)
            made.append(world.p("concepts", space, "sheet.png"))
    print("\n".join(made) if made else "no candidates without a pick")


def cmd_pick(args):
    world = World(args.pop(0))
    it = world.item(args.pop(0))
    n = int(args.pop(0))
    why = take(args, "--why", "")
    if not os.path.exists(it.f("2d", f"cand_{n}.png")):
        sys.exit(f"{it.id}: no cand_{n}.png")
    it.spec["pick"] = n
    it.spec["pick_why"] = why
    it.save()
    print(f"{it.id}: picked cand_{n} -> stage {it.stage()}")


def cmd_reject(args):
    world = World(args.pop(0))
    it = world.item(args.pop(0))
    it.spec["status"] = "rejected"
    it.spec["note"] = take(args, "--why", it.get("note", ""))
    it.save()
    print(f"{it.id}: rejected")


# ---------------------------------------------------------------- commands: GPU
def cmd_matte(args):
    world = World(args.pop(0))
    gpu_exclusive(args)
    wsl(f"{CONDA}; conda activate trellis2; python {wsl_path(GPU)}/matte.py {wsl_path(world.root)} {' '.join(args)}")
    print("Read every 2d/review.png listed above before running trellis.")


def cmd_trellis(args):
    world = World(args.pop(0))
    gpu_exclusive(args)
    items = world.items()
    if args:
        ids = args
    else:
        ids = [it.id for it in items.values() if it.kind == "prop" and it.stage() == "matted"]
    if not ids:
        sys.exit("nothing to model: no prop is at stage 'matted'")
    os.makedirs(world.p("logs"), exist_ok=True)
    save(world.p("logs", "trellis_queue.json"), {"ids": ids, "started": time.time(),
                                                 "started_at": time.strftime("%Y-%m-%d %H:%M")})
    log = world.p("logs", "trellis.log")
    print(f"TRELLIS: {len(ids)} props, log {log}", flush=True)
    world.log({"step": "trellis_start", "count": len(ids)})
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} queue {len(ids)} =====\n")
    r = wsl(f"bash {wsl_path(GPU)}/trellis_loop.sh {wsl_path(world.root)} 40 >> {wsl_path(log)} 2>&1", check=False)
    world.log({"step": "trellis_end", "exit": r.returncode})
    progress(world)
    sys.exit(r.returncode)


def progress(world):
    q = load(world.p("logs", "trellis_queue.json"), {})
    ids = q.get("ids", [])
    if not ids:
        print("no TRELLIS queue")
        return
    items = world.items(refresh=True)
    done, secs, failed = [], [], []
    for iid in ids:
        it = items.get(iid)
        if it and mtime(it.f("3d", "model.glb")) >= mtime(it.f("2d", "rgba.png")) > 0:
            done.append(iid)
            secs.append(load(it.f("3d", "trellis.json"), {}).get("seconds", 0))
        elif it and os.path.exists(it.f("3d", "trellis_fail.json")):
            failed.append(f"{iid} ({load(it.f('3d', 'trellis_fail.json'), {}).get('level', 1)}/3)")
    lines = open(world.p("logs", "trellis.log"), encoding="utf-8", errors="replace").read().splitlines() \
        if os.path.exists(world.p("logs", "trellis.log")) else []
    tail = [l for l in lines if l.startswith("[")]
    current = next((l.split()[1] for l in reversed(tail) if l.startswith("[start]")), None)
    finished = any(l.startswith(("[all done]", "[gave up")) for l in tail[-3:])
    alive = "BUSY" in wsl("pgrep -f '[t]rellis_batch.py' > /dev/null && echo BUSY || echo IDLE", check=False,
                          capture=True).stdout
    elapsed = (time.time() - q.get("started", time.time())) / 60
    left = len(ids) - len(done)
    avg = sum(secs) / len(secs) / 60 if secs else None
    eta = f"~{left * avg:.0f} min left" if avg and left else ""
    state = "running" if alive else ("finished" if finished or not left else "NOT RUNNING")
    print(f"TRELLIS {state}: {len(done)}/{len(ids)} done, {elapsed:.0f} min elapsed"
          f"{f', {avg:.1f} min/item' if avg else ''} {eta}".rstrip())
    if alive and current and current not in done:
        print(f"  now: {current}")
    if failed:
        print(f"  failed (rungs of 1024/18, 1024/12, 512 failed): {', '.join(failed)}")
    if not alive and left:
        print(f"  not finished: {', '.join(i for i in ids if i not in done)}")


def cmd_progress(args):
    progress(World(args.pop(0)))


def cmd_decals(args):
    import decals
    world = World(args.pop(0))
    decals.build(world, set(args))


# ---------------------------------------------------------------- commands: build phase
def blender(script, *extra):
    exe = config()["blender"]
    if not os.path.exists(exe):
        sys.exit(f"Blender not found at {exe}: set 'blender' in w2w.config.json")
    cmd = [exe, "-b", "--factory-startup", "-P", os.path.join(REPO, "blender", script), "--", *extra]
    print("> " + " ".join(cmd[-4:]), flush=True)
    r = subprocess.run(cmd)
    if r.returncode != 0:
        sys.exit(f"Blender exited with {r.returncode}")


def cmd_bake(args):
    world = World(args.pop(0))
    no_trellis()
    blender("bake_items.py", world.root, *args)
    print(f"report: {world.p('build', 'out', 'bake_report.md')}")


def cmd_surfaces(args):
    import surfaces
    world = World(args.pop(0))
    surfaces.build(world, set(args))


def cmd_surfaces_search(args):
    import surfaces
    surfaces.search(args)


def cmd_arch(args):
    world = World(args.pop(0))
    no_trellis()
    blender("build_arch.py", world.root, *args)


def cmd_terrain(args):
    import terrain
    world = World(args.pop(0))
    no_trellis()
    terrain.build(world)
    blender("build_terrain.py", world.root)


def cmd_scene(args):
    import scene
    world = World(args.pop(0))
    scene.compile_level(world)


def cmd_engine(args):
    import scene
    world = World(args.pop(0))
    shots = flag(args, "--shots")
    level_only = flag(args, "--level-only")
    live = flag(args, "--live")
    rnd = take(args, "--round")
    no_trellis()
    scene.compile_level(world)
    cfg = config()
    script = os.path.join(REPO, "engine", "unreal", "w2w_unreal.py").replace("\\", "/")
    if rnd is None:
        rnd = str(len(glob.glob(world.p("shots", "r*"))) + 1)
    py_args = f"--world {world.root.replace(chr(92), '/')} --round r{int(rnd):02d}"
    py_args += " --shots" if shots else ""
    py_args += " --level-only" if level_only else ""
    if live:
        print("run this in the open editor (Output Log > Python, or the unreal-mcp python tool):")
        print(f'  py "{script}" {py_args}')
        return
    if running("UnrealEditor.exe"):
        sys.exit("Unreal Editor is already open: close it, or run the importer inside it with --live")
    if not cfg["unreal_project"] or not os.path.exists(cfg["unreal_project"]):
        sys.exit("set 'unreal_project' in w2w.config.json")
    arg_line = f'"{cfg["unreal_project"]}" -unattended -nosplash -ExecCmds="py {script} {py_args} --exit-when-done"'
    print(f"> UnrealEditor {arg_line}\n  (the editor window opens and closes itself; do not close it)", flush=True)
    t0 = time.time()
    subprocess.run(f'"{cfg["unreal_editor"]}" {arg_line}', shell=True)
    log = world.out("unreal_import.log")
    print(open(log, encoding="utf-8", errors="replace").read() if os.path.exists(log) else "no import log written")
    world.log({"step": "engine", "seconds": round(time.time() - t0, 1), "shots": shots})
    shot_dir = world.p("shots", f"r{int(rnd):02d}")
    pngs = sorted(glob.glob(os.path.join(shot_dir, "*.png")))
    pngs = [p for p in pngs if not p.endswith("_sheet.png")]
    if shots and pngs:
        import sheets
        sheets.contact(pngs, os.path.join(shot_dir, "_sheet.png"), cell=640, cols=3)
        print(f"{len(pngs)} shots; overview {os.path.join(shot_dir, '_sheet.png')} (Read every shot, not only the sheet)")


# ---------------------------------------------------------------- commands: status / hand-off
def summarize(world):
    items = world.items(refresh=True)
    by_stage, by_cat = {}, {}
    for it in items.values():
        st = it.stage()
        by_stage.setdefault(st, []).append(it)
        by_cat.setdefault(it.rel.split("/")[0], {}).setdefault(st, 0)
        by_cat[it.rel.split("/")[0]][st] += 1
    return items, by_stage, by_cat


def scene_placeholders(world):
    sc = load(world.p("build", "scene.json"), {})
    items = world.items()
    temp = {}
    for inst in sc.get("instances", []):
        it = items.get(inst.get("item"))
        if it is None or it.stage() != "baked":
            temp[inst.get("item")] = temp.get(inst.get("item"), 0) + 1
    return temp


STAGE_ORDER = ["planned", "drafting", "picked", "matted", "ready", "baked", "rejected"]


def cmd_status(args):
    world = World(args.pop(0))
    show_all = flag(args, "--all")
    items, by_stage, by_cat = summarize(world)
    print(f"world {world.name}: {len(items)} items")
    print("  " + "  ".join(f"{s} {len(by_stage.get(s, []))}" for s in STAGE_ORDER if by_stage.get(s)))
    print("\nby category:")
    for cat in sorted(by_cat):
        print(f"  {cat:16s} " + "  ".join(f"{s} {n}" for s, n in sorted(by_cat[cat].items(),
                                                                         key=lambda x: STAGE_ORDER.index(x[0]))))
    families = {}
    for it in items.values():
        families.setdefault(os.path.dirname(it.rel), []).append(it.id)
    print(f"\n{len(families)} families, {sum(len(v) > 1 for v in families.values())} with variants")
    for st in STAGE_ORDER:
        group = by_stage.get(st, [])
        if group and (show_all or st not in ("baked", "rejected")):
            print(f"\n[{st}]")
            for it in sorted(group, key=lambda i: i.rel):
                extra = f"  (variant of {it['variant_of']})" if it.get("variant_of") else ""
                print(f"  {it.rel}  {it.kind}{extra}")
    temp = scene_placeholders(world)
    if temp:
        print(f"\nplaceholders in build/scene.json waiting for models: "
              + ", ".join(f"{k} x{v}" for k, v in sorted(temp.items())))
    spaces = world.spaces()
    if spaces:
        print("\nspaces:")
        for sid, s in spaces.items():
            ids = s.get("items", [])
            have = sum(1 for i in ids if i in items and items[i].stage() == "baked")
            print(f"  {sid:20s} {len(ids)} items listed, {have} baked, concepts picked {s.get('concept_picks', [])}")


def cmd_handoff(args):
    world = World(args.pop(0))
    items, by_stage, _ = summarize(world)
    forge_todo = [it.id for st in ("planned", "drafting", "picked", "matted") for it in by_stage.get(st, [])]
    build_todo = [it.id for it in by_stage.get("ready", [])]
    print(json.dumps({
        "world": world.name, "path": world.root, "items": len(items),
        "stages": {s: len(v) for s, v in by_stage.items()},
        "forge_todo": forge_todo, "build_todo": build_todo,
        "placeholders": scene_placeholders(world),
        "spaces": list(world.spaces()),
        "scene_exists": os.path.exists(world.p("build", "scene.json")),
        "last_shots": sorted(glob.glob(world.p("shots", "r*")))[-1:] or None,
    }, indent=2, ensure_ascii=False))


COMMANDS = {
    "new": cmd_new, "comfy": cmd_comfy, "concept": cmd_concept, "add": cmd_add, "gen": cmd_gen,
    "batch": cmd_batch, "sheet": cmd_sheet, "pick": cmd_pick, "reject": cmd_reject, "matte": cmd_matte,
    "trellis": cmd_trellis, "progress": cmd_progress, "decals": cmd_decals, "bake": cmd_bake,
    "surfaces": cmd_surfaces, "surfaces-search": cmd_surfaces_search, "arch": cmd_arch, "terrain": cmd_terrain,
    "scene": cmd_scene, "engine": cmd_engine, "status": cmd_status, "handoff": cmd_handoff,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(__doc__)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    COMMANDS[sys.argv[1]](sys.argv[2:])
