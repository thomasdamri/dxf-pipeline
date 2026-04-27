# DXF Pipeline

Three-stage pipeline that converts a DXF drawing to Leaflet-compatible map tiles and clickable hitboxes.

```
input.dxf ──► render_svg.py ──► drawing.svg ──► rasterise_tiles.py ──► tiles/ + tile_meta.json
    │                                                                                │
    └──────────────────────────────── extract_hitboxes.py ◄──────────────────────────┘
                                              │
                                        hitboxes.json
```

---

## Prerequisites

Create and activate the conda environment:

```bash
conda env create -f conda/environment.yml
conda activate dxfpipeline
```

Install test dependencies (if not already in the env):

```bash
pip install pytest pytest-cov
```

---

## Running the Pipeline

### All stages at once (recommended)

```bash
python pipeline/run_pipeline.py \
    --dxf diagrams/diagram1.dxf \
    --labels diagrams/labels.txt \
    --out-dir diagrams/
```

Options:

| Flag | Default | Description |
| --- | --- | --- |
| `--dxf` | _(required)_ | Input DXF file |
| `--labels` | _(required)_ | Text file with one label per line |
| `--out-dir` | DXF directory | Directory for all outputs |
| `--max-zoom` | `5` | Maximum tile zoom level |
| `--themes-config` | — | JSON file with per-theme colours |
| `--cluster-gap` | `3.5` | Vertical cluster proximity (× cap-height) |
| `--h-tolerance` | `2.5` | Horizontal cluster gate (× cap-height) |
| `--verbose` | — | Enable debug logging in stage 3 |

Outputs (all under `--out-dir`):

```
{stem}.svg  (or {stem}_{theme}.svg per theme)
tiles/{z}/{x}/{y}.webp
tile_meta.json
hitboxes.json
```

---

### Running stages individually

#### Stage 1 — DXF → SVG

```bash
python pipeline/render_svg.py input.dxf output.svg

# With theme colour overrides (renders one SVG per theme):
python pipeline/render_svg.py input.dxf output.svg --themes-config diagrams/mock_viewer_themes.json
```

Outputs: `output.svg` (or `output_<theme>.svg` per theme)

#### Stage 2 — SVG → Tile Pyramid

```bash
python pipeline/rasterise_tiles.py --svg output.svg

# Custom zoom and output directory:
python pipeline/rasterise_tiles.py --svg output.svg --max-zoom 6 --tiles-dir tiles/ --tile-meta tile_meta.json
```

Outputs: `tiles/{z}/{x}/{y}.webp` + `tile_meta.json`

#### Stage 3 — DXF + Labels → Hitboxes

```bash
python pipeline/extract_hitboxes.py \
    --dxf input.dxf \
    --labels labels.txt \
    --tile-meta tile_meta.json \
    --out hitboxes.json
```

Outputs: `hitboxes.json`

---

## Running Tests

All commands must be run from the `DXF_Pipeline/` directory.

### Run all tests

```bash
pytest
```

This runs all tests, reports coverage, and fails if coverage drops below 100%.

### Run a single test file

```bash
pytest tests/test_render_svg.py
pytest tests/test_rasterise_tiles.py
pytest tests/test_extract_hitboxes.py
pytest tests/test_run_pipeline.py
```

### Skip integration tests (faster, no cairosvg rendering)

```bash
pytest -m "not integration"
```

### Run only integration tests

```bash
pytest -m integration
```

---

## Type Checking

```bash
mypy pipeline/
```

---

## Linting and Formatting

```bash
# Check for lint issues:
ruff check pipeline/ tests/

# Auto-fix safe issues:
ruff check --fix pipeline/ tests/

# Check formatting:
ruff format --check pipeline/ tests/

# Apply formatting:
ruff format pipeline/ tests/
```

---

## Project Layout

```
DXF_Pipeline/
├── conda/
│   └── environment.yml           # conda env definition
├── diagrams/
│   ├── diagram1.dxf              # sample DXF
│   ├── labels.txt                # sample labels list
│   └── mock_viewer_themes.json
├── pipeline/
│   ├── pipeline_types.py         # shared TypedDict definitions
│   ├── render_svg.py             # Stage 1: DXF → SVG
│   ├── rasterise_tiles.py        # Stage 2: SVG → tile pyramid
│   ├── extract_hitboxes.py       # Stage 3: DXF + labels → hitboxes
│   └── run_pipeline.py           # Runner: all three stages in sequence
├── tests/
│   ├── conftest.py               # shared fixtures
│   ├── test_render_svg.py
│   ├── test_rasterise_tiles.py
│   ├── test_extract_hitboxes.py
│   └── test_run_pipeline.py
├── pyproject.toml                # pytest, coverage, mypy, ruff config
└── README.md
```
