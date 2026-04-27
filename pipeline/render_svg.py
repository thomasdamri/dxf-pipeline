# render_svg.py
# Renders a DXF to SVG, with optional per-theme colour overrides.
#
# Usage:
#   python render_svg.py input.dxf [output.svg]
#                                  [--themes-config themes.json]
#
#   --themes-config     JSON file defining one or more named themes.  Each theme
#                       specifies a background colour and optional per-layer colour
#                       overrides.  When provided, one SVG is rendered per theme
#                       and named <output_stem>_<theme>.svg.  If omitted, a single
#                       SVG is rendered with default DXF colours.
#
# themes.json example:
#   {
#     "light": { "background": "#FFFFFF", "layers": { "Pipes": "#000000" } },
#     "dark":  { "background": "#1A1A2E", "layers": { "Pipes": "#E0E0E0" } }
#   }
#
# Outputs:
#   output.svg (or output_<theme>.svg per theme)  -- vector SVG via ezdxf SVGBackend

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf.addons.drawing import Frontend, RenderContext
from ezdxf.addons.drawing.layout import Margins, Page, Settings, Units
from ezdxf.addons.drawing.svg import SVGBackend
from ezdxf.bbox import extents as bbox_extents
from pipeline_types import ThemeConfig, ThemesConfig

# ---- Helpers ----------------------------------------------------------------


def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    """Convert '#RRGGBB' (or 'RRGGBB') to an (R, G, B) int tuple."""
    h = hex_str.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


def _apply_theme(doc: Any, theme_cfg: ThemeConfig, logger: logging.Logger) -> None:
    """Mutate *doc* in-place to apply background and layer colour overrides."""
    import ezdxf.addons.drawing.properties as _props

    if "background" in theme_cfg:
        _props.MODEL_SPACE_BG_COLOR = theme_cfg["background"]
    for layer_name, hex_color in theme_cfg.get("layers", {}).items():
        if doc.layers.has_entry(layer_name):
            doc.layers.get(layer_name).rgb = _hex_to_rgb(hex_color)
        else:
            logger.warning("layer '%s' not found in DXF — colour override skipped", layer_name)


def _render_one(
    dxf_path: str,
    svg_out: str,
    theme_cfg: ThemeConfig | None,
    logger: logging.Logger,
) -> None:
    """Load DXF, optionally apply theme colours, render to SVG, write file."""
    doc = ezdxf.readfile(dxf_path)  # fresh load ensures no cross-theme mutation
    msp = doc.modelspace()

    if theme_cfg is not None:
        _apply_theme(doc, theme_cfg, logger)

    # DXF extents via entity bbox scan (never trust $EXTMIN/$EXTMAX)
    logger.debug("Scanning entity extents...")
    bbox = bbox_extents(msp)
    if bbox is None or not bbox.has_data:
        logger.error("Could not determine drawing extents")
        sys.exit(1)

    dxf_x_min, dxf_y_min = bbox.extmin.x, bbox.extmin.y
    dxf_x_max, dxf_y_max = bbox.extmax.x, bbox.extmax.y
    dxf_w = dxf_x_max - dxf_x_min
    dxf_h = dxf_y_max - dxf_y_min
    logger.debug(
        "DXF extents: x=[%.4f, %.4f]  y=[%.4f, %.4f]", dxf_x_min, dxf_x_max, dxf_y_min, dxf_y_max
    )
    logger.debug("DXF size: %.4f x %.4f units", dxf_w, dxf_h)

    logger.debug("Rendering SVG...")
    ctx = RenderContext(doc)
    backend = SVGBackend()
    Frontend(ctx, backend).draw_layout(msp)

    page = Page(0, 0, Units.mm, Margins(0, 0, 0, 0))
    svg_string = backend.get_string(page, settings=Settings())

    with open(svg_out, "w", encoding="utf-8") as f:
        f.write(svg_string)
    logger.info("SVG written  : %s", svg_out)


# ---- CLI --------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Render DXF to SVG with optional theme colour overrides."
    )
    parser.add_argument(
        "dxf",
        nargs="?",
        default="test_diagram.dxf",
        help="Input DXF file (default: test_diagram.dxf)",
    )
    parser.add_argument(
        "svg", nargs="?", default=None, help="Output SVG file (default: <dxf_stem>.svg)"
    )
    parser.add_argument(
        "--themes-config",
        default=None,
        metavar="FILE",
        help="JSON file with per-theme background + layer colours. "
        "Renders one SVG per theme when provided.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        force=True,
    )
    logger = logging.getLogger(__name__)

    dxf_path = args.dxf
    svg_path = args.svg or (dxf_path.rsplit(".", 1)[0] + ".svg")

    if not Path(dxf_path).exists():
        logger.error("DXF file not found: %s", dxf_path)
        sys.exit(2)

    logger.info("DXF          : %s", dxf_path)
    logger.info("SVG base     : %s", svg_path)
    if args.themes_config:
        logger.info("themes-config: %s", args.themes_config)

    # ---- Theme loading ------------------------------------------------------

    themes_config: ThemesConfig | None = None
    if args.themes_config:
        with open(args.themes_config, encoding="utf-8") as f:
            themes_config = json.load(f)

    # ---- Render pass(es) ----------------------------------------------------

    if themes_config:
        base = svg_path.rsplit(".", 1)[0]
        ext = "." + svg_path.rsplit(".", 1)[1] if "." in svg_path else ".svg"
        for theme_name, theme_cfg in themes_config.items():
            if theme_name.startswith("_"):
                continue  # skip metadata/comment keys
            theme_svg = f"{base}_{theme_name}{ext}"
            logger.info("Theme: %s", theme_name)
            _render_one(dxf_path, theme_svg, theme_cfg, logger)
    else:
        _render_one(dxf_path, svg_path, None, logger)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
