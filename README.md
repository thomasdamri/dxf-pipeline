# DXF Pipeline

Two-stage pipeline that converts a DXF drawing to a Leaflet-compatible XYZ tile pyramid.

```
input.dxf  →  render_svg.py  →  drawing.svg  →  rasterise_tiles.py  →  tiles/ + tile_meta.json
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

### Stage 1 — DXF → SVG

```bash
python pipeline/render_svg.py input.dxf output.svg

# With theme colour overrides (renders one SVG per theme):
python pipeline/render_svg.py input.dxf output.svg --themes-config diagrams/mock_viewer_themes.json

# Convert text to outline paths (font-independent):
python pipeline/render_svg.py input.dxf output.svg --text-to-path
```

Outputs: `output.svg` (or `output_<theme>.svg` per theme) + `svg_manifest.json`

### Stage 2 — SVG → Tile Pyramid

```bash
python pipeline/rasterise_tiles.py --svg output.svg

# Custom zoom and output directory:
python pipeline/rasterise_tiles.py --svg output.svg --max-zoom 6 --tiles-dir tiles/ --tile-meta tile_meta.json
```

Outputs: `tiles/{z}/{x}/{y}.webp` + `tile_meta.json`

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
│   └── environment.yml       # conda env definition
├── diagrams/
│   ├── diagram1.dxf          # sample DXF
│   └── mock_viewer_themes.json
├── pipeline/
│   ├── pipeline_types.py     # shared TypedDict definitions
│   ├── render_svg.py         # Stage 1: DXF → SVG
│   └── rasterise_tiles.py    # Stage 2: SVG → tile pyramid
├── tests/
│   ├── conftest.py           # shared fixtures
│   ├── test_render_svg.py
│   └── test_rasterise_tiles.py
├── pyproject.toml            # pytest, coverage, mypy, ruff config
└── README.md
```
