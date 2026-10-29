"""Texture helpers shared by surfaces, decals, terrain and the Blender bake (numpy; Pillow only inside load/save).

Canonical maps written to game/ folders:
  T_<name>_BC.png    base colour, sRGB (RGBA when the item has transparency: decals, cards)
  T_<name>_N.png     tangent-space normal, OpenGL (+Y up); the Unreal importer flips green
  T_<name>_ORM.png   linear: R = ambient occlusion, G = roughness, B = metallic
Blender imports this module too (it ships numpy): keep it free of other dependencies.
"""
import os

import numpy as np

ALBEDO_SRGB_RANGE = (30 / 255, 240 / 255)   # real albedo is never pure black or pure white
ALBEDO_MAX_SATURATION = 0.85

CONDITION = {  # wear (edges), dirt (cavities and the bottom), rough_var (macro roughness breakup)
    "new": {"wear": 0.08, "dirt": 0.1, "rough_var": 0.6},
    "used": {"wear": 0.35, "dirt": 0.4, "rough_var": 0.8},
    "worn": {"wear": 0.7, "dirt": 0.75, "rough_var": 1.0},
    "ruined": {"wear": 1.0, "dirt": 1.0, "rough_var": 1.0},
}


def load(path, channels=3):
    from PIL import Image
    im = Image.open(path)
    mode = {1: "L", 3: "RGB", 4: "RGBA"}[channels]
    a = np.asarray(im.convert(mode), dtype=np.float32) / 255.0
    return a if channels > 1 else a[:, :, None]


def save(path, a, bits=8):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    a = np.clip(a, 0, 1)
    if a.ndim == 3 and a.shape[2] == 1:
        a = a[:, :, 0]
    if bits == 16:
        if a.ndim != 2:
            raise ValueError("16-bit output is single channel only")
        Image.fromarray((a * 65535 + 0.5).astype(np.uint16)).save(path)
    else:
        Image.fromarray((a * 255 + 0.5).astype(np.uint8)).save(path)
    return path


def resize(a, size):
    from PIL import Image
    w, h = (size, size) if isinstance(size, int) else size
    if a.ndim == 2:
        a = a[:, :, None]
    if a.shape[1] == w and a.shape[0] == h:
        return a
    chans = [np.asarray(Image.fromarray(a[:, :, c].astype(np.float32), "F").resize((w, h), Image.LANCZOS))
             for c in range(a.shape[2])]
    return np.clip(np.stack(chans, axis=2), 0, 1)


def pow2_at_most(n, lo=128, hi=4096):
    p = lo
    while p * 2 <= min(n, hi):
        p *= 2
    return p


def luminance(rgb):
    return rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152 + rgb[..., 2] * 0.0722


def clamp_albedo(rgb, lo=ALBEDO_SRGB_RANGE[0], hi=ALBEDO_SRGB_RANGE[1], max_sat=ALBEDO_MAX_SATURATION):
    """Remap brightness into [lo, hi] (contrast kept) and pull saturation above max_sat back toward grey."""
    rgb = np.clip(rgb[..., :3], 0, 1)
    lum = luminance(rgb)[..., None]
    out = rgb * ((lo + lum * (hi - lo)) / np.maximum(lum, 1e-4))
    mx, mn = out.max(axis=-1, keepdims=True), out.min(axis=-1, keepdims=True)
    sat = (mx - mn) / np.maximum(mx, 1e-4)
    k = np.where(sat > max_sat, max_sat / np.maximum(sat, 1e-4), 1.0)
    grey = luminance(out)[..., None]
    return np.clip(grey + (out - grey) * k, 0, 1)


def normal_from_height(h, strength=2.0, wrap=True):
    """Tangent-space OpenGL normal map from a height field in [0, 1]."""
    h = h[..., 0] if h.ndim == 3 else h
    if not wrap:
        h = np.pad(h, 1, mode="edge")
    dx = (np.roll(h, -1, axis=1) - np.roll(h, 1, axis=1)) * 0.5 * strength * h.shape[1] / 256
    dy = (np.roll(h, -1, axis=0) - np.roll(h, 1, axis=0)) * 0.5 * strength * h.shape[0] / 256
    if not wrap:
        dx, dy = dx[1:-1, 1:-1], dy[1:-1, 1:-1]
    n = np.stack([-dx, dy, np.ones_like(dx)], axis=-1)   # image rows go down, OpenGL +Y goes up
    n /= np.linalg.norm(n, axis=-1, keepdims=True)
    return n * 0.5 + 0.5


def tileable_noise(size, feature_px, seed=0, octaves=4):
    """Seamlessly tiling fractal noise in [0, 1] (white noise low-passed in the frequency domain)."""
    rng = np.random.default_rng(seed)
    f = np.sqrt(np.fft.fftfreq(size)[None, :] ** 2 + np.fft.fftfreq(size)[:, None] ** 2)
    out = np.zeros((size, size), np.float32)
    amp, px = 1.0, feature_px
    for _ in range(octaves):
        spec = np.fft.fft2(rng.standard_normal((size, size)))
        out += amp * np.real(np.fft.ifft2(spec * np.exp(-(f * max(px, 1.0)) ** 2))).astype(np.float32)
        amp *= 0.5
        px /= 2
    out -= out.min()
    return out / max(out.max(), 1e-6)


def blur(a, radius):
    """Separable box blur (two passes ~ gaussian), wrapping at the borders."""
    r = max(1, int(radius))
    for axis in (0, 1):
        for _ in range(2):
            c = np.cumsum(np.concatenate([a.take(range(-r - 1, 0), axis=axis), a, a.take(range(0, r), axis=axis)],
                                         axis=axis), axis=axis)
            n = a.shape[axis]
            a = (c.take(range(2 * r + 1, 2 * r + 1 + n), axis=axis) - c.take(range(0, n), axis=axis)) / (2 * r + 1)
    return a


def delight(rgb, strength, radius):
    """Flatten broad lighting baked into a generated albedo (lit top / dark underside). 0 = off."""
    if strength <= 0:
        return rgb
    lum = luminance(rgb)
    gain = (lum.mean() / np.maximum(blur(lum, radius), 1e-3)) ** strength
    return np.clip(rgb * np.clip(gain, 0.5, 2.0)[..., None], 0, 1)


def weather(albedo, rough, metal, ao, edge, cavity, noise, height, wear, dirt, rough_var, bare_metal=0.0):
    """The 'not plastic' pass: dirt in cavities / near the ground, worn bright edges, roughness breakup."""
    cav = np.clip(np.maximum(cavity * 1.3, (1 - ao) * 1.2), 0, 1)
    dirt_mask = np.clip(cav + (1 - height) ** 4 * 0.7 + (noise - 0.5) * 0.8, 0, 1) * dirt
    edge_mask = np.clip(edge * 1.6 * (0.4 + noise), 0, 1) * wear
    grey = luminance(albedo)[..., None]
    dirty = (grey * 0.6 + albedo * 0.4) * np.array([0.62, 0.58, 0.52], np.float32)
    out = albedo + (dirty - albedo) * (dirt_mask[..., None] * 0.7)
    out = out + (np.minimum(out * 1.2 + 0.04, 1) - out) * (edge_mask[..., None] * 0.6)
    spread = float(rough.std())
    r = rough + (noise - 0.5) * (0.22 + max(0.0, 0.05 - spread) * 4) * rough_var + dirt_mask * 0.3 - edge_mask * 0.25
    m = np.maximum(metal, edge_mask * bare_metal)
    return np.clip(out, 0, 1), np.clip(r, 0.04, 1), np.clip(m, 0, 1)


def _g(a):
    return a if a.ndim == 3 else a[:, :, None]


def pack_orm(ao, rough, metal):
    return np.concatenate([_g(ao), _g(rough), _g(metal)], axis=2)


def write_maps(out_dir, name, bc, normal=None, ao=None, rough=None, metal=None, size=None, clamp=True):
    """Write T_<name>_BC / _N / _ORM. bc is sRGB RGB(A) float; the rest are linear planes or scalars.
    Returns {"bc", "n", "orm"} file names."""
    if size:
        bc = resize(bc, size)
    h, w = bc.shape[:2]

    def plane(v, default):
        v = default if v is None else v
        if np.isscalar(v):
            return np.full((h, w, 1), float(v), np.float32)
        v = _g(np.asarray(v, np.float32))
        return resize(v, (w, h)) if v.shape[:2] != (h, w) else v

    ao, rough, metal = plane(ao, 1.0), plane(rough, 0.6), plane(metal, 0.0)
    rgb = clamp_albedo(bc[..., :3]) if clamp else bc[..., :3]
    out = {"bc": f"T_{name}_BC.png", "n": f"T_{name}_N.png", "orm": f"T_{name}_ORM.png"}
    save(os.path.join(out_dir, out["bc"]), np.concatenate([rgb, bc[..., 3:4]], axis=2) if bc.shape[2] == 4 else rgb)
    save(os.path.join(out_dir, out["orm"]), pack_orm(ao, rough, metal))
    if normal is None:
        normal = np.concatenate([np.full((4, 4, 2), 0.5, np.float32), np.ones((4, 4, 1), np.float32)], axis=2)
    elif normal.shape[:2] != (h, w):
        normal = resize(normal, (w, h))
    save(os.path.join(out_dir, out["n"]), normal)
    return out


def edge_fade(h, w, frac=0.08):
    """1 inside, falling to 0 at the image border so a decal never shows a square edge."""
    y = np.minimum(np.arange(h), np.arange(h)[::-1]) / max(h * frac, 1)
    x = np.minimum(np.arange(w), np.arange(w)[::-1]) / max(w * frac, 1)
    return np.clip(np.minimum(y[:, None], x[None, :]), 0, 1) ** 1.5


def alpha_from_luma(rgb):
    """Dark marks on a white background -> (colour, alpha)."""
    a = np.clip((1 - luminance(rgb) - 0.04) / 0.5, 0, 1) ** 0.8
    color = (rgb - (1 - a[..., None])) / np.maximum(a[..., None], 1e-3)
    return np.clip(color, 0, 1), a


def dilate_color(rgb, alpha, iterations=8):
    """Bleed opaque colours into transparent pixels so mipmaps of cut-outs do not get dark / white halos."""
    rgb = rgb.copy()
    known = alpha > 0.5
    for _ in range(iterations):
        acc = np.zeros_like(rgb)
        cnt = np.zeros(alpha.shape, np.float32)
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            k = np.roll(known, (dy, dx), (0, 1))
            acc += np.roll(rgb, (dy, dx), (0, 1)) * k[..., None]
            cnt += k
        grow = (~known) & (cnt > 0)
        rgb[grow] = acc[grow] / cnt[grow][:, None]
        known = known | grow
    return rgb
