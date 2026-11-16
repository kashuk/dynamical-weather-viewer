# Weather layers

An exploded 3D stack of weather fields from the latest NOAA GFS run, read
straight from [dynamical.org](https://dynamical.org): a white relief globe
sits in the middle of six glass slices (precipitation, sea-level pressure,
cloud cover, precipitable water, 10 m wind, 2 m temperature). Each slice shows
the hemisphere facing it, so the slices line up with the globe behind them.

## Using it

- **Drag** to orbit the whole stack, **shift-drag** (or right-drag) to turn the
  globe, **wheel** or pinch to zoom, **double-click** or `R` to reset.
- **Play** (or `Space`) runs the 0–102 h forecast through every slice at once;
  `←`/`→` step 3 h; the slider scrubs.
- **Click a layer** to hide or show it; hover one to lift it out of the stack.
- **Spread** fans the slices apart; **Spin** slowly turns the globe.

Everything in the view (frame, camera, spread, facing longitude, hidden layers)
lives in the URL, so a link reproduces what you see.

## How it works

- `weather.py` opens the GFS forecast Icechunk store listed in dynamical.org's
  STAC catalog, picks the newest run whose first 105 h are complete, and
  writes each layer as 35 small PNG frames (0.5°, one per 3 h) under
  `.fused/cache/v1/`. Reads stay well under the 60 s call limit because one
  Zarr chunk already holds 105 lead times.
- The globe's relief is derived from GFS surface pressure (a hypsometric
  estimate of terrain height, hill-shaded); coastlines and the land mask come
  from Natural Earth 50 m.
- `index.html` is plain WebGL 2 with no libraries. Each slice is a thin glass
  slab; its fragment shader projects the hemisphere orthographically onto the
  slab and colours the sampled value with a per-layer ramp (one hue for
  magnitudes, blue–grey–red for temperature, isobars every 4 hPa for
  pressure). Frames are cross-faded during playback.
- On open, the page shows whatever run is cached, then checks dynamical.org for
  a newer one in the background and swaps it in when all layers have arrived.

## Design notes

The brief was a visual like a stack of glass panes around a clay globe. Of the
three ways considered — flat equirectangular maps on wide panes, a whole-earth
azimuthal projection on square panes, or an orthographic hemisphere matched to
the globe — the last keeps every pane geometrically consistent with the globe
from any angle (the back face of each slab shows the opposite hemisphere), so
it was chosen even though it leaves the corners of each pane as clear glass.

Data: NOAA GFS from dynamical.org, CC BY 4.0.
