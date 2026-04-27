"""
run_pipeline.py
───────────────
Runs the full three-stage DXF-to-viewer pipeline in a single command.

  Stage 1 — render_svg.py       DXF → SVG
  Stage 2 — rasterise_tiles.py  SVG → XYZ tile pyramid + tile_meta.json
  Stage 3 — extract_hitboxes.py DXF + labels → hitboxes.json

Usage:
    python run_pipeline.py \\
        --dxf drawing.dxf \\
        --labels labels.txt \\
        [--out-dir out/]          # default: directory containing the DXF
        [--max-zoom 5]
        [--themes-config themes.json]
        [--cluster-gap 3.5]
        [--h-tolerance 2.5]
        [--verbose]

Outputs (all under --out-dir):
    {stem}.svg  (or {stem}_{theme}.svg per theme)
    tiles/{z}/{x}/{y}.webp
    tile_meta.json
    hitboxes.json
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import extract_hitboxes
import rasterise_tiles
import render_svg


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the full DXF → SVG → tiles → hitboxes pipeline.")
    p.add_argument("--dxf", required=True, metavar="FILE", help="Input DXF file")
    p.add_argument("--labels", required=True, metavar="FILE", help="Labels list (one per line)")
    p.add_argument(
        "--out-dir",
        default=None,
        metavar="DIR",
        help="Output directory (default: directory containing the DXF)",
    )
    p.add_argument(
        "--max-zoom", type=int, default=5, metavar="N", help="Max tile zoom level (default: 5)"
    )
    p.add_argument(
        "--themes-config",
        default=None,
        metavar="FILE",
        help="JSON file with per-theme colours; passed through to render_svg",
    )
    p.add_argument(
        "--cluster-gap",
        type=float,
        default=3.5,
        metavar="N",
        help="Vertical clustering threshold × cap-height (default: 3.5)",
    )
    p.add_argument(
        "--h-tolerance",
        type=float,
        default=2.5,
        metavar="N",
        help="Horizontal clustering gate × cap-height (default: 2.5)",
    )
    p.add_argument("--verbose", action="store_true", help="Enable debug logging in stage 3")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    logger = logging.getLogger(__name__)

    dxf = Path(args.dxf).resolve()
    out_dir = Path(args.out_dir) if args.out_dir else dxf.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = dxf.stem
    svg_base = out_dir / f"{stem}.svg"
    tile_meta = out_dir / "tile_meta.json"

    # ── Stage 1: DXF → SVG ──────────────────────────────────────────────────
    logger.info("=== Stage 1: render_svg ===")
    render_argv = [str(dxf), str(svg_base)]
    if args.themes_config:
        render_argv += ["--themes-config", args.themes_config]
        themes = json.loads(Path(args.themes_config).read_text(encoding="utf-8"))
        first_theme = next(n for n in themes if not n.startswith("_"))
        svg_for_tiles = str(out_dir / f"{stem}_{first_theme}.svg")
    else:
        svg_for_tiles = str(svg_base)
    render_svg.main(render_argv)

    # ── Stage 2: SVG → tile pyramid ─────────────────────────────────────────
    logger.info("=== Stage 2: rasterise_tiles ===")
    rasterise_tiles.main(
        [
            "--svg",
            svg_for_tiles,
            "--max-zoom",
            str(args.max_zoom),
            "--tiles-dir",
            str(out_dir / "tiles"),
            "--tile-meta",
            str(tile_meta),
        ]
    )

    # ── Stage 3: DXF + labels → hitboxes ────────────────────────────────────
    logger.info("=== Stage 3: extract_hitboxes ===")
    extract_argv = [
        "--dxf",
        str(dxf),
        "--labels",
        args.labels,
        "--tile-meta",
        str(tile_meta),
        "--out",
        str(out_dir / "hitboxes.json"),
        "--cluster-gap",
        str(args.cluster_gap),
        "--h-tolerance",
        str(args.h_tolerance),
    ]
    if args.verbose:
        extract_argv.append("--verbose")
    extract_hitboxes.main(extract_argv)


if __name__ == "__main__":  # pragma: no cover
    main()
