"""
rasterise_tiles.py
──────────────────
Converts a DXF-derived SVG → high-res PNG → XYZ tile pyramid for Leaflet.

Reads  : drawing.svg
Writes : tiles/{z}/{x}/{y}.png
         tile_meta.json

Dependencies:
    conda install -c conda-forge cairosvg pillow

Usage:
    python rasterise_tiles.py --svg drawing.svg
    python rasterise_tiles.py --svg drawing.svg --max-zoom 6
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import math
import re
from pathlib import Path

import cairosvg
from PIL import Image
from pipeline_types import TileMeta

logger = logging.getLogger(__name__)

TILE_SIZE = 256
DEFAULT_MAX_ZOOM = 5
CAIRO_MAX_DIM = 32000


def _read_svg_viewbox(svg_path: str) -> tuple[float, float]:
    with open(svg_path, encoding="utf-8") as f:
        head = f.read(4096)
    m = re.search(r'viewBox="([^"]+)"', head)
    if m:
        parts = [float(x) for x in m.group(1).split()]
        if len(parts) == 4:
            return parts[2], parts[3]
    mw = re.search(r'\bwidth="([\d.]+)', head)
    mh = re.search(r'\bheight="([\d.]+)', head)
    if mw and mh:
        return float(mw.group(1)), float(mh.group(1))
    raise ValueError(f"Could not read SVG dimensions from {svg_path}")


def _png_to_image(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data)).convert("RGBA")


def _count_tiles(w: int, h: int, max_zoom: int) -> int:
    short = min(w, h)
    return sum(
        max(1, math.ceil(w * (1 << z) / short)) * max(1, math.ceil(h * (1 << z) / short))
        for z in range(max_zoom + 1)
    )


def _generate_tiles(img: Image.Image, out_dir: Path, max_zoom: int, tile_sz: int):
    full_w, full_h = img.size
    short = min(full_w, full_h)
    total_tiles = _count_tiles(full_w, full_h, max_zoom)
    written = 0

    for z in range(max_zoom + 1):
        cols = max(1, math.ceil(full_w * (1 << z) / short))
        rows = max(1, math.ceil(full_h * (1 << z) / short))
        scale_factor = (1 << z) * tile_sz / short
        target_w = min(round(full_w * scale_factor), cols * tile_sz)
        target_h = min(round(full_h * scale_factor), rows * tile_sz)

        scaled = img.resize((target_w, target_h), Image.Resampling.LANCZOS)

        canvas = Image.new("RGBA", (cols * tile_sz, rows * tile_sz), (0, 0, 0, 0))
        canvas.paste(scaled, (0, 0), mask=scaled.split()[3])

        for tx in range(cols):
            tile_dir = out_dir / str(z) / str(tx)
            tile_dir.mkdir(parents=True, exist_ok=True)
            for ty in range(rows):
                left = tx * tile_sz
                upper = ty * tile_sz
                tile = canvas.crop((left, upper, left + tile_sz, upper + tile_sz))
                tile.save(tile_dir / f"{ty}.webp", "WEBP", quality=90, method=6)
                written += 1

        logger.debug(
            "z=%d  %3dx%-3d tiles  [%d/%d  %.0f%%]",
            z,
            cols,
            rows,
            written,
            total_tiles,
            100 * written / total_tiles,
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Rasterise DXF SVG to tile pyramid using cairosvg.")
    p.add_argument("--svg", required=True, metavar="FILE")
    p.add_argument("--max-zoom", type=int, default=DEFAULT_MAX_ZOOM, metavar="N")
    p.add_argument("--tiles-dir", default="tiles", metavar="DIR")
    p.add_argument("--tile-meta", default="tile_meta.json", metavar="FILE")
    p.add_argument("--tile-size", type=int, default=TILE_SIZE, metavar="PX")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    args = parse_args(argv)
    tile_sz = args.tile_size
    max_zoom = args.max_zoom

    vb_w, vb_h = _read_svg_viewbox(args.svg)

    short_vb = min(vb_w, vb_h)
    cols_max = max(1, math.ceil(vb_w * (1 << max_zoom) / short_vb))
    rows_max = max(1, math.ceil(vb_h * (1 << max_zoom) / short_vb))
    full_w_px = cols_max * tile_sz
    full_h_px = rows_max * tile_sz

    logger.info("SVG viewBox  : %.2f x %.2f mm", vb_w, vb_h)
    logger.info(
        "Max zoom     : %d  (grid %dx%d, target %d x %d px)",
        max_zoom,
        cols_max,
        rows_max,
        full_w_px,
        full_h_px,
    )

    svg_bytes = Path(args.svg).read_bytes()
    # "none" prevents cairosvg letterboxing that causes transparent margins and seams at strip boundaries.
    svg_bytes = re.sub(rb'preserveAspectRatio="[^"]*"', b"", svg_bytes)
    svg_bytes = re.sub(rb"<svg\b", b'<svg preserveAspectRatio="none"', svg_bytes, count=1)
    Image.MAX_IMAGE_PIXELS = full_w_px * full_h_px + 1  # silence decompression-bomb warning

    n_strips = math.ceil(full_w_px / CAIRO_MAX_DIM)
    if n_strips == 1:
        logger.info("Rasterising  : %s -> %d x %d px ...", args.svg, full_w_px, full_h_px)
        full_img = _png_to_image(
            cairosvg.svg2png(bytestring=svg_bytes, output_width=full_w_px, output_height=full_h_px)
        )
    else:
        logger.info(
            "Rasterising  : %s -> %d x %d px in %d strips ...",
            args.svg,
            full_w_px,
            full_h_px,
            n_strips,
        )
        full_img = Image.new("RGBA", (full_w_px, full_h_px), (0, 0, 0, 0))
        vb_per_px = vb_w / full_w_px
        vb_m = re.search(rb'viewBox="[^"]*"', svg_bytes)
        if not vb_m:
            raise ValueError("viewBox attribute not found in SVG bytes — cannot do strip rendering")
        vb_start, vb_end = vb_m.start(), vb_m.end()
        for i in range(n_strips):
            x0_px = i * CAIRO_MAX_DIM
            x1_px = min(x0_px + CAIRO_MAX_DIM, full_w_px)
            strip_w_px = x1_px - x0_px
            new_vb = f'viewBox="{x0_px * vb_per_px:.4f} 0 {strip_w_px * vb_per_px:.4f} {vb_h:.4f}"'
            patched = svg_bytes[:vb_start] + new_vb.encode() + svg_bytes[vb_end:]
            logger.info("  strip %d/%d  x=%d..%d px", i + 1, n_strips, x0_px, x1_px)
            strip_img = _png_to_image(
                cairosvg.svg2png(
                    bytestring=patched, output_width=strip_w_px, output_height=full_h_px
                )
            )
            full_img.paste(strip_img, (x0_px, 0))

    tiles_dir = Path(args.tiles_dir)
    tiles_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Tiling into  : %s/", tiles_dir)
    _generate_tiles(full_img, tiles_dir, max_zoom, tile_sz)

    short_px = min(full_w_px, full_h_px)
    tile_meta: TileMeta = {
        "max_zoom": max_zoom,
        "tile_size": tile_sz,
        "full_width_px": full_w_px,
        "full_height_px": full_h_px,
        "leaflet_bounds": [
            [-round(full_h_px * tile_sz / short_px, 4), 0],
            [0, round(full_w_px * tile_sz / short_px, 4)],
        ],
    }

    meta_path = Path(args.tile_meta)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(tile_meta, indent=2))
    logger.info("Tile meta    : %s", args.tile_meta)

    total_tiles = _count_tiles(full_w_px, full_h_px, max_zoom)
    logger.info(
        "Done: %d x %d px  |  zoom 0-%d  |  %d tiles  |  %s/",
        full_w_px,
        full_h_px,
        max_zoom,
        total_tiles,
        tiles_dir,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
