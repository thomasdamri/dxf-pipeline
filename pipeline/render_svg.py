# render_svg.py
# Renders a DXF to SVG, with optional per-theme colour overrides.
#
# Typical pipeline:
#   1. python render_svg.py    input.dxf              → drawing.svg
#   2. python rasterise_tiles.py --svg drawing.svg    → tiles/ + tile_meta.json
#   3. python extract_manifest.py --dxf input.dxf \  → hitboxes.json
#                                  --labels labels.txt \
#                                  --tile-meta tile_meta.json
#
# Usage:
#   python render_svg.py input.dxf [output.svg] [--text-to-path]
#                                  [--themes-config themes.json]
#
#   --text-to-path      Convert all DXF text/MTEXT to filled outline paths in the
#                       SVG rather than <text> elements.  Use this when your DXF
#                       fonts are not available on the viewing machine, or when you
#                       need pixel-accurate glyph rendering.  Note: extract_manifest
#                       cannot read paths as text, so it will fall back to DXF
#                       coordinate matching (unaffected by this flag).
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
#   svg_manifest.json                             -- list of rendered (theme, svg, background)

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


def _make_settings(text_to_path: bool, logger: logging.Logger) -> Settings:
    """Build render Settings, handling older ezdxf versions gracefully."""
    if not text_to_path:
        return Settings()
    # Modern API (ezdxf >= 1.1): Settings(text_policy=TextPolicy.FILLING)
    try:
        from ezdxf.addons.drawing.properties import TextPolicy  # type: ignore[attr-defined]

        logger.debug("text-to-path: using TextPolicy.FILLING")  # pragma: no cover
        return Settings(text_policy=TextPolicy.FILLING)  # type: ignore[call-arg]  # pragma: no cover
    except (ImportError, AttributeError, TypeError):  # pragma: no cover
        pass
    # Older API: Settings(text_as_paths=True)
    try:  # pragma: no cover
        s = Settings(text_as_paths=True)  # type: ignore[call-arg]
        logger.debug("text-to-path: using text_as_paths=True")
        return s
    except TypeError:  # pragma: no cover
        pass
    # Last resort: show_text attribute
    s = Settings()  # pragma: no cover
    if hasattr(s, "show_text"):  # pragma: no cover
        s.show_text = False
        logger.debug("text-to-path: using show_text=False (fallback)")
    else:  # pragma: no cover
        logger.warning(
            "text-to-path not supported by this ezdxf version "
            "-- text will remain as <text> elements"
        )
    return s  # pragma: no cover


def _render_one(
    dxf_path: str,
    svg_out: str,
    theme_cfg: ThemeConfig | None,
    text_to_path: bool,
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

    settings = _make_settings(text_to_path, logger)

    logger.debug("Rendering SVG...")
    ctx = RenderContext(doc)
    backend = SVGBackend()
    Frontend(ctx, backend).draw_layout(msp)

    page = Page(0, 0, Units.mm, Margins(0, 0, 0, 0))
    svg_string = backend.get_string(page, settings=settings)

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
        "--text-to-path",
        action="store_true",
        help="Convert text/MTEXT to outline paths instead of "
        "<text> elements (font-independent, path-accurate)",
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
    text_to_path = args.text_to_path

    if not Path(dxf_path).exists():
        logger.error("DXF file not found: %s", dxf_path)
        sys.exit(2)

    logger.info("DXF          : %s", dxf_path)
    logger.info("SVG base     : %s", svg_path)
    logger.info("text-to-path : %s", text_to_path)
    if args.themes_config:
        logger.info("themes-config: %s", args.themes_config)

    # ---- Theme loading ------------------------------------------------------

    themes_config: ThemesConfig | None = None
    if args.themes_config:
        with open(args.themes_config, encoding="utf-8") as f:
            themes_config = json.load(f)

    # ---- Render pass(es) ----------------------------------------------------

    manifest: list[dict[str, str | None]] = []

    if themes_config:
        base = svg_path.rsplit(".", 1)[0]
        ext = "." + svg_path.rsplit(".", 1)[1] if "." in svg_path else ".svg"
        for theme_name, theme_cfg in themes_config.items():
            if theme_name.startswith("_"):
                continue  # skip metadata/comment keys
            theme_svg = f"{base}_{theme_name}{ext}"
            logger.info("Theme: %s", theme_name)
            _render_one(dxf_path, theme_svg, theme_cfg, text_to_path, logger)
            manifest.append(
                {
                    "theme": theme_name,
                    "svg": str(Path(theme_svg).resolve()),
                    "background": theme_cfg.get("background", "#ffffff"),
                }
            )
    else:
        _render_one(dxf_path, svg_path, None, text_to_path, logger)
        manifest = [{"theme": None, "svg": str(Path(svg_path).resolve()), "background": "#ffffff"}]

    # ---- Write svg_manifest.json --------------------------------------------

    manifest_path = Path(svg_path).parent / "svg_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    logger.info("SVG manifest : %s", manifest_path)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
