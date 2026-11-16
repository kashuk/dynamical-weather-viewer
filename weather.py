"""weather.py — data side of the dynamical weather viewer.

Reads NOAA GFS forecasts from dynamical.org (Icechunk/Zarr on S3) and turns
them into small 0.5° texture frames that index.html draws in WebGL.  Every
expensive result is cached under .fused/cache/v1 and handed to the page as a
path for fused.rawUrl().

Actions (main(action=...)):
  cached   -> what is already cached (fast: no heavy imports, no network)
  latest   -> resolve the newest complete GFS run and fetch its temperature layer
  layer    -> fetch one layer (35 frames, 0–102 h every 3 h) for a given run
  basemap  -> build the globe texture: relief from GFS surface pressure,
              land mask + coastlines from Natural Earth
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, ".fused", "cache", "v1")
KEEP_RUNS = 2

W, H = 720, 360                      # 0.5° frames (2×2 block mean of the 0.25° grid)
LEAD_STEP, LEAD_STOP = 3, 105        # lead_time index slice → 0, 3, …, 102 h (35 frames)
MAX_NAN = 0.02                       # a run with more missing cells than this is "not done yet"

MODELS = {
    "gfs": {
        "title": "NOAA GFS",
        "stac": "https://stac.dynamical.org/noaa-gfs-forecast/collection.json",
        "fallback": "https://dynamical-noaa-gfs.s3.us-west-2.amazonaws.com/noaa-gfs-forecast/v0.2.7.icechunk",
        "attribution": "NOAA GFS from dynamical.org",
    },
}

NE_BASE = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
BASEMAP_W, BASEMAP_H = 2048, 1024


def _hypot(a):
    return np.hypot(a[0], a[1])


def _mm_per_hour(a):
    return a[0] * 3600.0


def _hpa(a):
    return a[0] / 100.0


def _first(a):
    return a[0]


# Front → back order of the glass panes is LAYER_ORDER; the globe sits in the middle.
LAYERS = {
    "precip": dict(label="Precipitation", units="mm/h", vars=["precipitation_surface"],
                   vmin=0.0, vmax=8.0, scale="sqrt", depth=8, fn=_mm_per_hour),
    "pressure": dict(label="Sea-level pressure", units="hPa", vars=["pressure_reduced_to_mean_sea_level"],
                     vmin=940.0, vmax=1060.0, scale="linear", depth=16, fn=_hpa),
    "clouds": dict(label="Cloud cover", units="%", vars=["total_cloud_cover_atmosphere"],
                   vmin=0.0, vmax=100.0, scale="linear", depth=8, fn=_first),
    "water": dict(label="Precipitable water", units="kg/m²", vars=["precipitable_water_atmosphere"],
                  vmin=0.0, vmax=70.0, scale="linear", depth=8, fn=_first),
    "wind": dict(label="Wind speed 10 m", units="m/s", vars=["wind_u_10m", "wind_v_10m"],
                 vmin=0.0, vmax=25.0, scale="linear", depth=8, fn=_hypot),
    "temperature": dict(label="Temperature 2 m", units="°C", vars=["temperature_2m"],
                        vmin=-40.0, vmax=45.0, scale="linear", depth=8, fn=_first),
}
LAYER_ORDER = ["precip", "pressure", "clouds", "water", "wind", "temperature"]


# ----------------------------------------------------------------------------- helpers

def _specs():
    return {k: {kk: vv for kk, vv in v.items() if kk != "fn"} for k, v in LAYERS.items()}


def _rel(path):
    """Path relative to the app folder, for fused.rawUrl() in the page."""
    return os.path.relpath(path, HERE).replace(os.sep, "/")


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _save_png(img, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".png", dir=os.path.dirname(path))
    os.close(fd)
    img.save(tmp, format="PNG", compress_level=6)
    os.replace(tmp, path)


def _run_id(init_iso):
    # "2026-11-16T12:00:00" -> "20261116T12"
    return init_iso[:13].replace("-", "").replace(":", "")


def _init_iso(value):
    return str(np.datetime64(value, "s")).replace(" ", "T")


# ----------------------------------------------------------------------------- store

def _store_href(model):
    """Icechunk HTTPS href from the STAC collection (cached 6 h), else the known fallback."""
    m = MODELS[model]
    meta_path = os.path.join(CACHE, f"stac-{model}.json")
    meta = _read_json(meta_path)
    if meta and time.time() - meta.get("at", 0) < 6 * 3600 and meta.get("href"):
        return meta["href"]
    href = m["fallback"]
    try:
        import requests
        r = requests.get(m["stac"], timeout=10)
        r.raise_for_status()
        href = r.json()["assets"]["icechunk-https"]["href"]
    except Exception as e:  # offline or STAC changed shape: the fallback still works
        print(f"stac lookup failed ({e!r}); using fallback href")
    _write_json(meta_path, {"href": href, "at": time.time()})
    return href


def _open(model):
    t = time.time()
    import icechunk
    import xarray as xr
    print(f"imports {time.time() - t:.1f}s")
    t = time.time()
    repo = icechunk.Repository.open(icechunk.http_storage(_store_href(model)))
    ds = xr.open_zarr(repo.readonly_session("main").store, chunks=None, decode_timedelta=True)
    print(f"open {time.time() - t:.1f}s")
    return ds


def _read(ds, var, init_iso):
    t = time.time()
    a = ds[var].sel(init_time=np.datetime64(init_iso)).isel(lead_time=slice(0, LEAD_STOP, LEAD_STEP)).values
    print(f"read {var} {a.shape} {time.time() - t:.1f}s")
    return a.astype(np.float32)


def _lead_hours(ds):
    lt = ds.lead_time.values[0:LEAD_STOP:LEAD_STEP]
    return [int(x) for x in (lt / np.timedelta64(1, "h")).round()]


def _downsample(a):
    """(n, 721, 1440) -> (n, 360, 720) by 2×2 mean; the duplicate south-pole row is dropped."""
    n = a.shape[0]
    return a[:, :H * 2, :].reshape(n, H, 2, W, 2).mean(axis=(2, 4))


# ----------------------------------------------------------------------------- frames

def _encode_frames(vals, spec, out_dir):
    from PIL import Image
    vmin, vmax = spec["vmin"], spec["vmax"]
    n = np.clip((vals - vmin) / (vmax - vmin), 0.0, 1.0)
    if spec["scale"] == "sqrt":
        n = np.sqrt(n)
    paths = []
    for i in range(n.shape[0]):
        path = os.path.join(out_dir, f"f{i:02d}.png")
        if spec["depth"] == 16:
            q = (n[i] * 65535.0 + 0.5).astype(np.uint16)
            rgb = np.stack([(q >> 8).astype(np.uint8), (q & 255).astype(np.uint8), np.zeros_like(q, dtype=np.uint8)], axis=-1)
            _save_png(Image.fromarray(rgb, "RGB"), path)
        else:
            _save_png(Image.fromarray((n[i] * 255.0 + 0.5).astype(np.uint8), "L"), path)
        paths.append(_rel(path))
    return paths


def _write_layer(ds, model, layer, init_iso, arrays):
    spec = LAYERS[layer]
    vals = _downsample(np.nan_to_num(spec["fn"](arrays), nan=spec["vmin"]))
    out_dir = os.path.join(CACHE, model, _run_id(init_iso), layer)
    t = time.time()
    frames = _encode_frames(vals, spec, out_dir)
    print(f"encoded {len(frames)} frames {time.time() - t:.1f}s")
    manifest = {
        "model": model, "layer": layer, "label": spec["label"], "units": spec["units"],
        "vmin": spec["vmin"], "vmax": spec["vmax"], "scale": spec["scale"], "depth": spec["depth"],
        "width": W, "height": H, "init_time": init_iso, "lead_hours": _lead_hours(ds),
        "frames": frames, "built_at": time.time(),
    }
    _write_json(os.path.join(out_dir, "manifest.json"), manifest)
    return manifest


# ----------------------------------------------------------------------------- actions

def cached(model):
    """Everything already on disk for this model, newest run first. Prunes old runs."""
    runs = []
    model_dir = os.path.join(CACHE, model)
    if os.path.isdir(model_dir):
        for name in sorted(os.listdir(model_dir), reverse=True):
            run_dir = os.path.join(model_dir, name)
            if not os.path.isdir(run_dir) or not name[:1].isdigit():
                continue
            layers = {}
            for layer in LAYERS:
                m = _read_json(os.path.join(run_dir, layer, "manifest.json"))
                if m and all(os.path.exists(os.path.join(HERE, p)) for p in m["frames"]):
                    layers[layer] = m
            if layers:
                runs.append({"init_time": next(iter(layers.values()))["init_time"], "layers": layers})
            elif len(runs) >= KEEP_RUNS or not any(os.scandir(run_dir)):
                shutil.rmtree(run_dir, ignore_errors=True)
                continue
            if len(runs) > KEEP_RUNS:
                shutil.rmtree(run_dir, ignore_errors=True)
                runs.pop()
    basemap = _read_json(os.path.join(CACHE, "basemap", "manifest.json"))
    if basemap and not os.path.exists(os.path.join(HERE, basemap["path"])):
        basemap = None
    return {
        "model": model, "title": MODELS[model]["title"], "attribution": MODELS[model]["attribution"],
        "layer_order": LAYER_ORDER, "layers": _specs(), "runs": runs, "basemap": basemap,
    }


def latest(model):
    """Newest run whose first 105 h are complete, plus its temperature layer (which the check reads anyway)."""
    ds = _open(model)
    inits = ds.init_time.values
    for k in range(1, 5):
        init_iso = _init_iso(inits[-k])
        a = _read(ds, "temperature_2m", init_iso)
        nan_frac = float(np.isnan(a).mean())
        print(f"run {init_iso}: {nan_frac:.3%} missing")
        if nan_frac <= MAX_NAN:
            return {"init_time": init_iso, "layer": _write_layer(ds, model, "temperature", init_iso, [a])}
    raise RuntimeError("none of the last four GFS runs is complete yet")


def fetch_layer(model, layer, init_iso):
    if layer not in LAYERS:
        raise ValueError(f"unknown layer {layer!r}; one of {', '.join(LAYERS)}")
    if not init_iso:
        raise ValueError("init is required (an ISO init_time from the latest action)")
    ds = _open(model)
    arrays = [_read(ds, v, init_iso) for v in LAYERS[layer]["vars"]]
    nan_frac = float(np.isnan(arrays[0]).mean())
    if nan_frac > MAX_NAN:
        raise RuntimeError(f"run {init_iso} is still being written ({nan_frac:.1%} missing)")
    return _write_layer(ds, model, layer, init_iso, arrays)


# ----------------------------------------------------------------------------- basemap

def _natural_earth(name):
    path = os.path.join(CACHE, "ne", f"{name}.geojson")
    gj = _read_json(path)
    if gj:
        return gj
    import requests
    r = requests.get(NE_BASE + f"{name}.geojson", timeout=60)
    r.raise_for_status()
    gj = r.json()
    _write_json(path, gj)
    return gj


def _to_px(coords):
    return [((lon + 180.0) / 360.0 * BASEMAP_W, (90.0 - lat) / 180.0 * BASEMAP_H) for lon, lat in coords]


def _rasterize():
    """Land mask and coastlines from Natural Earth 50 m, as two L images."""
    from PIL import Image, ImageDraw
    land = Image.new("L", (BASEMAP_W, BASEMAP_H), 0)
    d = ImageDraw.Draw(land)
    for f in _natural_earth("ne_50m_land")["features"]:
        g = f["geometry"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        for rings in polys:
            for i, ring in enumerate(rings):
                pts = _to_px(ring)
                if len(pts) >= 3:
                    d.polygon(pts, fill=255 if i == 0 else 0)
    coast = Image.new("L", (BASEMAP_W, BASEMAP_H), 0)
    d = ImageDraw.Draw(coast)
    for f in _natural_earth("ne_50m_coastline")["features"]:
        g = f["geometry"]
        lines = g["coordinates"] if g["type"] == "MultiLineString" else [g["coordinates"]]
        for line in lines:
            pts = _to_px(line)
            if len(pts) >= 2:
                d.line(pts, fill=255, width=2)
    return land, coast


def _hillshade(z, cell_m, zfactor=14.0, azimuth=315.0, altitude=45.0):
    """ESRI-style hillshade in 0..1, 0.5 = flat. Rows run north → south."""
    dzdx = (np.roll(z, -1, axis=1) - np.roll(z, 1, axis=1)) / (2.0 * cell_m) * zfactor
    dzdy = (np.roll(z, 1, axis=0) - np.roll(z, -1, axis=0)) / (2.0 * cell_m) * zfactor
    slope = np.arctan(np.hypot(dzdx, dzdy))
    aspect = np.arctan2(dzdy, -dzdx)
    zen = np.radians(90.0 - altitude)
    az = np.radians(360.0 - azimuth + 90.0)
    shade = np.cos(zen) * np.cos(slope) + np.sin(zen) * np.sin(slope) * np.cos(az - aspect)
    return np.clip(shade - np.cos(zen) + 0.5, 0.0, 1.0)


def _box_blur(a, r):
    """Separable box blur; wraps in longitude, clamps at the poles."""
    for axis in (0, 1):
        out = np.zeros_like(a)
        for k in range(-r, r + 1):
            if axis == 1:
                out += np.roll(a, k, axis=1)
            else:
                idx = np.clip(np.arange(a.shape[0]) + k, 0, a.shape[0] - 1)
                out += a[idx]
        a = out / (2 * r + 1)
    return a


def basemap(model):
    from PIL import Image
    land, coast = _rasterize()
    ds = _open(model)
    init_iso = _init_iso(ds.init_time.values[-1])
    sp = ds["pressure_surface"].sel(init_time=np.datetime64(init_iso)).isel(lead_time=0).values.astype(np.float64)
    sp = np.nan_to_num(sp, nan=101325.0)
    elev = np.clip(8400.0 * np.log(101325.0 / np.maximum(sp, 1000.0)), -300.0, 9000.0).astype(np.float32)
    big = Image.fromarray(elev, "F").resize((BASEMAP_W, BASEMAP_H), Image.BICUBIC)
    z = _box_blur(np.asarray(big, dtype=np.float64), 2)
    land_a = np.asarray(land, dtype=np.float64) / 255.0
    cell_m = 40_075_000.0 / BASEMAP_W
    shade = _hillshade(z * land_a, cell_m)
    shade = np.where(land_a > 0.5, shade, 0.5)
    # Height, lightly compressed, for a soft "clay" tint on high ground.
    height = np.clip(np.sqrt(np.clip(z, 0, 6000) / 6000.0), 0, 1) * land_a
    rgba = np.stack([
        (shade * 255 + 0.5).astype(np.uint8),
        np.asarray(coast, dtype=np.uint8),
        np.asarray(land, dtype=np.uint8),
        (height * 255 + 0.5).astype(np.uint8),
    ], axis=-1)
    path = os.path.join(CACHE, "basemap", "basemap.png")
    _save_png(Image.fromarray(rgba, "RGBA"), path)
    manifest = {
        "path": _rel(path), "width": BASEMAP_W, "height": BASEMAP_H,
        "channels": {"r": "hillshade (128 = flat)", "g": "coastline", "b": "land", "a": "height"},
        "relief_from": f"{MODELS[model]['title']} surface pressure, run {init_iso}",
        "coast_from": "Natural Earth 50 m", "built_at": time.time(),
    }
    _write_json(os.path.join(CACHE, "basemap", "manifest.json"), manifest)
    return manifest


# ----------------------------------------------------------------------------- entry

def main(action: str = "cached", model: str = "gfs", layer: str = "temperature", init: str = ""):
    if model not in MODELS:
        raise ValueError(f"unknown model {model!r}")
    os.makedirs(CACHE, exist_ok=True)
    t = time.time()
    try:
        if action == "cached":
            return cached(model)
        if action == "latest":
            return latest(model)
        if action == "layer":
            return fetch_layer(model, layer, init)
        if action == "basemap":
            return basemap(model)
        raise ValueError(f"unknown action {action!r}")
    finally:
        print(f"{action} done in {time.time() - t:.1f}s")
