"""
Shared pytest fixtures for DXFPipeline tests.
"""

from pathlib import Path

import pytest

# ── path helpers ────────────────────────────────────────────────────────────

TESTS_DIR = Path(__file__).parent
PIPELINE_DIR = TESTS_DIR.parent / "pipeline"


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def minimal_dxf(tmp_path):
    """
    Tiny synthetic DXF with 2 TEXT entities and 1 LWPOLYLINE.
    Built directly via ezdxf — no dependency on test_diagram.dxf.
    Returns a Path.
    """
    import ezdxf

    doc = ezdxf.new(dxfversion="R2010")
    msp = doc.modelspace()

    # Explicitly register layers in the layer table so has_entry() finds them
    doc.layers.new("TAGS")
    doc.layers.new("EQUIP")
    doc.layers.new("OUTLINE")

    msp.add_text(
        "FV101",
        dxfattribs={
            "insert": (10.0, 20.0),
            "height": 2.5,
            "layer": "TAGS",
        },
    )
    msp.add_text(
        "HV201",
        dxfattribs={
            "insert": (50.0, 60.0),
            "height": 2.5,
            "layer": "EQUIP",
        },
    )
    msp.add_lwpolyline(
        points=[(0, 0), (100, 0), (100, 80), (0, 80)],
        dxfattribs={"layer": "OUTLINE", "closed": True},
    )

    out = tmp_path / "minimal.dxf"
    doc.saveas(str(out))
    return out


@pytest.fixture()
def simple_svg(tmp_path):
    """Minimal valid SVG with a known viewBox of 200×100 mm."""
    svg = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" '
        'viewBox="0 0 200 100" width="200mm" height="100mm">\n'
        '  <rect x="10" y="10" width="80" height="60" fill="none" stroke="black"/>\n'
        "</svg>\n"
    )
    out = tmp_path / "simple.svg"
    out.write_text(svg, encoding="utf-8")
    return out


@pytest.fixture()
def minimal_png(tmp_path):
    """
    512×256 white PNG.  Dimensions are fixed — tile-count assertions depend on them.
    """
    from PIL import Image

    img = Image.new("RGB", (512, 256), (255, 255, 255))
    out = tmp_path / "test.png"
    img.save(str(out), "PNG")
    return out


@pytest.fixture()
def minimal_tile_meta():
    """
    Hand-crafted tile_meta dict with round numbers for CoordTransform tests.

    Chosen values:
      full_width_px=1024, full_height_px=512, tile_size=256
      → short_px=512, coord_w=512, coord_h=256

    DXF extents (used alongside this fixture): x=[0,200], y=[0,100]
      → scale_x = 512/200 = 2.56
      → scale_y = 256/100 = 2.56
    """
    return {
        "max_zoom": 3,
        "tile_size": 256,
        "full_width_px": 1024,
        "full_height_px": 512,
        "leaflet_bounds": [[-256.0, 0], [0, 512.0]],
    }
