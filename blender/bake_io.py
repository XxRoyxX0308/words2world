"""Image IO through bpy (Blender's Python has numpy but no Pillow)."""
import bpy
import numpy as np


def img_read(path, size=None):
    im = bpy.data.images.load(path, check_existing=False)
    if size and tuple(im.size) != (size, size):
        im.scale(size, size)
    w, h = im.size
    px = np.empty(w * h * 4, np.float32)
    im.pixels.foreach_get(px)
    bpy.data.images.remove(im)
    return px.reshape(h, w, 4)[::-1].copy()          # bpy stores rows bottom-up


def img_write(path, a):
    a = np.clip(a, 0, 1).astype(np.float32)
    if a.ndim == 2:
        a = a[:, :, None]
    h, w, c = a.shape
    rgba = np.ones((h, w, 4), np.float32)
    rgba[..., :3] = a[..., :3] if c >= 3 else a[..., :1]
    if c == 4:
        rgba[..., 3] = a[..., 3]
    im = bpy.data.images.new("w2w_out", w, h, alpha=(c == 4))
    im.pixels.foreach_set(rgba[::-1].ravel())
    im.filepath_raw = path
    im.file_format = "PNG"
    im.save()
    bpy.data.images.remove(im)
