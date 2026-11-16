---
name: dynamical-weather-viewer
description: Exploded 3D stack of NOAA GFS weather layers from dynamical.org. Fetch a forecast run as texture frames, or list what is cached.
---
# Dynamical weather viewer

The page draws a white relief globe with glass slices (temperature, wind,
clouds, precipitation, precipitable water, sea-level pressure) and plays a
forecast through them. Frames live under `.fused/cache/v1/` and are
rebuildable from dynamical.org at any time.

## weather.py
Reads NOAA GFS runs from the dynamical.org Icechunk store; writes only under `.fused/cache/v1`.
- Args: `action` (str, default `"cached"`): one of `cached`, `latest`, `layer`, `basemap`;
  `model` (str, default `"gfs"`); `layer` (str, default `"temperature"`: `precip`, `pressure`, `clouds`, `water`, `wind`, `temperature`);
  `init` (str, default `""`, ISO init time such as `2026-11-16T12:00:00`, required by `layer`);
  `known` (str, default `""`, the init time already on screen: `latest` answers without reading when nothing newer exists)
- Returns:
  - `cached` → `{"runs": [{"init_time": str, "layers": {id: manifest}}], "basemap": manifest|null, "layer_order": [...], "layers": {id: spec}}` (fast, no network)
  - `latest` → `{"init_time": str, "layer": manifest|null}` for the newest run whose first 105 h are complete (about 25 s; `layer` is null when `known` is still the newest)
  - `layer` → one layer manifest `{"layer", "label", "units", "vmin", "vmax", "scale", "depth", "lead_hours": [...], "frames": [png paths], ...}` (about 20–40 s)
  - `basemap` → `{"path": png, "width", "height", "channels": {...}}` for the globe texture (about 25 s)
- Example: `{"action":"py","app":"dynamical-weather-viewer","file":"weather.py","args":{"action":"layer","layer":"wind","init":"2026-11-16T12:00:00"}}`
