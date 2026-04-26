# extract_hitboxes.py — Technical Explainer

This document explains what the script does, the technical concepts behind each stage,
and the testing strategy used to verify it.

---

## What is a DXF file?

DXF (Drawing Exchange Format) is AutoCAD's open file format for 2D and 3D engineering
drawings. A P&ID (Piping and Instrumentation Diagram) stored as a DXF contains:

- **Geometry entities** — lines, polylines, circles, arcs, blocks — forming the visual
  diagram.
- **Text entities** — labels like `FV101`, `HV201` attached to instruments and valves.

Text entities come in two flavours that the script handles differently:

| Entity | Description | Notable properties |
|--------|-------------|-------------------|
| `TEXT` | Single-line text. Widely supported. | `halign` (0=Left, 1=Center, 2=Right), `valign` (0=Baseline, 1=Bottom, 2=Middle, 3=Top) |
| `MTEXT` | Multi-line, formatted text. Richer but more complex. | No `halign`/`valign` on the DXF attributes — alignment is embedded in the content string |

### DXF coordinate system

DXF uses a **right-handed, Y-up** coordinate system: X goes right, Y goes up, the
origin is wherever the drafter placed it. This is the opposite of screen/pixel
coordinates, where Y increases downward. The coordinate transform in Stage 2 handles
the flip.

### The `insert` point

Every text entity has an **insert point** — the (x, y) coordinate the drafter used to
place the text. For `TEXT`, this is:

- the **left edge of the first character** at the baseline when `halign=0, valign=0`
- the **centre** of the text when `halign=1`
- the **right edge** when `halign=2`
- shifted vertically when `valign` is non-zero

For `MTEXT`, the insert is the **top-left corner** of the text frame by default, but
since alignment metadata is not reliably present on the DXF attribute struct, the
script defaults both to 0 (left/baseline), which is acceptable for the simple
bbox-only use case here.

---

## Stage 1 — DXF Extraction

### `extract_text_entities`

Iterates every entity in the DXF **modelspace** (the main drawing area, distinct from
paper-space layout sheets) and picks out `TEXT` and `MTEXT` entities. Key decisions:

**`getattr(e.dxf, "height", 2.5) or 2.5`** — DXF attributes are optional; a missing
or zero height field falls back to 2.5 drawing-units. The `or 2.5` guard handles the
case where the attribute exists but is explicitly `0`.

**Empty text is skipped** — empty strings and whitespace-only labels are noise, not
instrument tags.

**`halign`/`valign` are `None` for MTEXT** — explicitly stored as `None` rather than
omitted so that downstream code can use a single `entity.get("halign") or 0` pattern
safely.

### `get_dxf_extents`

Returns the axis-aligned bounding box of all drawn geometry.

The DXF header contains `$EXTMIN`/`$EXTMAX` fields that are *supposed* to hold the
drawing extents, but in practice they are unreliable — AutoCAD and third-party
exporters frequently write stale or zeroed values. The script therefore uses
`ezdxf.bbox.extents()`, which **scans the actual entity geometry** to compute a
correct bounding box from scratch. This is slower but accurate.

The returned dict has `x_min, y_min, x_max, y_max, width, height` — all in DXF
drawing units.

---

## Stage 2 — Coordinate Transform

### Why a transform is needed

The tiled viewer is built on **Leaflet.js with `CRS.Simple`** — a flat, non-geographic
coordinate system. Leaflet uses `(lat, lng)` pairs even in this mode, but they are
just (y, x) pixel coordinates with a sign flip on y:

```
lat = -y_pixel
lng =  x_pixel
```

The minus sign on `lat` is Leaflet's way of turning a Y-down pixel grid into a
coordinate where the top-left is the maximum lat and the bottom-left is the minimum
lat (i.e. the most negative value). This is unintuitive but standard for CRS.Simple
maps.

### The scaling math

The tile pyramid was generated at a specific pixel resolution. The `tile_meta.json`
produced by the rasterisation stage records:

| Field | Meaning |
|-------|---------|
| `full_width_px` | Total pixel width of the original rasterised image |
| `full_height_px` | Total pixel height |
| `tile_size` | Side length of each square tile (always 256 in this pipeline) |

Leaflet's CRS.Simple expects coordinates in the same unit as the tile grid. The
tile grid's coordinate range is derived by normalising against the **shorter dimension**:

```python
short_px = min(full_width_px, full_height_px)
coord_w  = full_width_px  * tile_size / short_px
coord_h  = full_height_px * tile_size / short_px
```

For the test fixture (1024×512, tile_size=256):

```
short_px = 512
coord_w  = 1024 * 256 / 512 = 512
coord_h  =  512 * 256 / 512 = 256
```

The DXF drawing spans `[x_min, x_max]` × `[y_min, y_max]`. The scale factors map
drawing units to these tile-grid coordinates:

```
scale_x = coord_w / drawing_width
scale_y = coord_h / drawing_height
```

### `to_leaflet`

```python
px =  (dxf_x - x_min) * scale_x          # shift to origin, then scale
py = coord_h - (dxf_y - y_min) * scale_y  # flip Y: DXF Y-up → screen Y-down
return {"lat": -py, "lng": px}
```

The Y flip works in two steps:
1. `(dxf_y - y_min) * scale_y` maps the DXF Y value into pixel-space where 0 is the
   bottom of the drawing and `coord_h` is the top.
2. Subtracting from `coord_h` inverts it so that 0 is now the top (screen origin).
3. Negating to produce `lat` follows Leaflet's `lat = -y_pixel` convention.

---

## Stage 3 — Bounding Box

### Why bboxes and not just points

The viewer needs to highlight the region of the diagram that corresponds to a label —
a single point is not clickable. The bbox defines a polygon that can be drawn as an
overlay and tested for pointer hit detection.

### Character width estimation

DXF does not store the rendered width of text. The script uses a rough monospace
approximation:

```python
_CHAR_WIDTH = 0.6   # advance width as fraction of cap-height
_PAD        = 0.12  # padding as fraction of cap-height
```

For a text entity with cap-height `h` and `n` characters:

```
raw_w = n * h * 0.6
pad   = h * 0.12
```

This is intentionally simple — a proportional font would need per-character advance
metrics. For instrument tag labels (short, mostly uppercase) the error is acceptable.

### Local offset calculation

The four corners of the box are computed in a **local coordinate frame** relative to
the insert point, then translated to world coordinates. This makes the alignment
logic clean regardless of where in the drawing the entity sits.

**Horizontal (halign):**

| halign | Meaning | lx_min | lx_max |
|--------|---------|--------|--------|
| 0 | Left (default) | `-pad` | `raw_w + pad` |
| 1 | Center | `-raw_w/2 - pad` | `raw_w/2 + pad` |
| 2 | Right | `-raw_w - pad` | `pad` |

In all three cases the insert point is a specific reference edge of the text, and the
offsets position the box accordingly with equal padding on both sides perpendicular to
the reference edge.

**Vertical (valign):**

| valign | Meaning | ly_min | ly_max |
|--------|---------|--------|--------|
| 0 | Baseline (default) | `-h*0.2 - pad` | `h + pad` |
| 1 | Bottom | `-pad` | `h + pad` |
| 2 | Middle | `-h/2 - pad` | `h/2 + pad` |
| 3 | Top | `-h - pad` | `pad` |

The baseline case (0) is special: in typography, descenders on characters like `g`,
`p`, `y` extend approximately 20 % of the cap-height below the baseline. The bbox
therefore extends `h * 0.2` below the insert Y to avoid clipping those characters.
This is `ly_min = -h * 0.2 - pad`, not just `-pad`.

### Corner assembly

With no rotation (the current assumption for all DXFs in this pipeline), the four
corners in DXF world coordinates are:

```
BL = (ix + lx_min, iy + ly_min)
BR = (ix + lx_max, iy + ly_min)
TR = (ix + lx_max, iy + ly_max)
TL = (ix + lx_min, iy + ly_max)
```

These are stored in the order `[BL, BR, TR, TL]` — a counter-clockwise winding in
DXF's Y-up space — then passed through `CoordTransform.corners_to_leaflet` to produce
the final Leaflet (lat, lng) pairs. After the Y-flip, the winding reverses in screen
space, which is fine for polygon rendering.

---

## Stage 4 — Matching

### `build_index`

Produces a `dict[str, DxfEntity]` keyed by the entity's text content (stripped of
whitespace). The first occurrence of a given text wins — if a label appears twice in
the DXF the first placed instance is used and the rest silently ignored. This is a
deliberate policy: DXFs sometimes contain duplicate labels in legend boxes or title
blocks; the main diagram entity is usually drawn first.

### `build_hitboxes`

Iterates the caller-supplied labels list, looks each up in the index, and returns only
the matched entries. **Unmatched labels are silently dropped** — they do not appear in
the output at all. The CLI logs a warning listing them. This design means the output
file only contains actionable records; the caller does not need to filter `found: false`
entries.

---

## Testing Strategy

### Fixtures (`conftest.py`)

Two shared fixtures eliminate boilerplate across test classes:

**`minimal_dxf`** — a synthetic DXF built with `ezdxf` at test time. It contains
exactly two `TEXT` entities (`FV101`, `HV201`) and one `LWPOLYLINE`. Building it
programmatically avoids a committed binary test asset that could silently become stale,
and makes the fixture's exact content visible to anyone reading the tests.

**`minimal_tile_meta`** — a hand-crafted dict with round numbers chosen so that all
scale factors work out to clean values (scale_x = scale_y = 2.56). This makes
expected output values easy to reason about without a calculator.

### Test organisation

Tests are grouped into one class per public function, matching the five pipeline stages.
Each class is self-contained: it imports only the function it tests and uses only the
fixtures and helpers it needs. This makes it obvious which function a failing test
belongs to.

### What each class tests

| Class | Strategy |
|-------|----------|
| `TestExtractTextEntities` | Structural assertions (keys present, types correct) plus behaviour tests for edge cases: empty text, MTEXT-specific fields (`halign`/`valign` are `None`), non-text entities skipped. Uses both the shared `minimal_dxf` fixture and inline `tmp_path` DXFs for cases that require specific entity types. |
| `TestGetDxfExtents` | Structural checks on the return dict, positivity guards, and a dedicated empty-DXF test verifying that `ValueError` is raised (the only error path in this module). |
| `TestCoordTransform` | Direct mathematical verification: known input corners are mapped and the output values are checked against hand-computed expected values. `pytest.approx` is used throughout to handle floating-point rounding. The four cardinal points of the DXF space (origin, max corner, centre) are tested individually. |
| `TestComputeBbox` | Two types of test: **structural** (4 corners, lat/lng keys present) and **relational** (insert point is inside / at the edge of the box depending on alignment mode). Relational tests are more robust than exact-value tests because they don't break if `_CHAR_WIDTH` or `_PAD` constants are tuned. |
| `TestBuildIndex` | Exercises the first-occurrence-wins deduplication, whitespace stripping, and empty input. Straightforward dict assertions. |
| `TestBuildHitboxes` | Covers the found/not-found split, null coords when `transform=None`, correct key set on output records, and whitespace stripping on the label lookup. |
| `TestLoadLabels` | Pure I/O: comment lines, blank lines, whitespace stripping, empty file. Uses `tmp_path` to write controlled input files. |
| `TestParseArgs` | CLI contract: required args, defaults, all optionals, and `SystemExit` on missing required args. These protect against accidental interface breakage. |
| `TestMain` | Integration: runs the full pipeline end-to-end via the `main()` function. Verifies the output file is created, contains the expected labels, and that coords are null without `--tile-meta` and populated with it. Also covers the `--verbose` flag and nested output directory creation. |

### Relational vs exact-value assertions

Most bbox tests assert **relational properties** rather than exact numbers:

```python
# Instead of:
assert min(lngs) == pytest.approx(-0.3072)

# The tests do:
assert min(lngs) < insert_ll["lng"]   # bbox extends to the left of insert
```

This is intentional. The exact extent of the bbox depends on `_CHAR_WIDTH` and `_PAD`,
which are tuning constants — changing them is expected and shouldn't require updating
dozens of magic numbers in tests. The relational tests verify the *geometry is
correct* (left-aligned text extends rightward from the insert, centre-aligned straddles
it, etc.) regardless of exact width.

### Coverage

The project is configured with `--cov-fail-under=100` for `extract_hitboxes.py`. Every
reachable line in the module is exercised by at least one test. The `# pragma: no cover`
annotations on the two `ImportError` fallbacks are the only explicit exclusions — they
guard against a missing `ezdxf` install and cannot be reached in a correctly set up
test environment.
