"""
extract_hitboxes.py
───────────────────
Stage 3 (exact-match): generate hitboxes.json from a DXF file and a labels list.
Clustering support will be added in a future step.

Coordinate transform: DXF (Y-up) → Leaflet CRS.Simple (lat = -y_px, lng = x_px)

Usage:
    python extract_hitboxes.py \
        --dxf drawing.dxf \
        --labels labels.txt \
        --tile-meta tile_meta.json \
        --out hitboxes.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Types
# ──────────────────────────────────────────────

class DxfEntity(TypedDict):
    text:   str
    insert: tuple[float, float]
    height: float
    halign: int | None
    valign: int | None
    layer:  str
    type:   str  # "TEXT" | "MTEXT"


class LatLng(TypedDict):
    lat: float
    lng: float


class HitboxBbox(TypedDict):
    leaflet: dict[str, list[LatLng]]   # {"corners": [TL, TR, BR, BL]}


class HitboxRecord(TypedDict):
    label:   str
    found:   bool
    leaflet: LatLng | None
    bbox:    HitboxBbox | None


# ──────────────────────────────────────────────
# 1.  DXF Extraction
# ──────────────────────────────────────────────

def extract_text_entities(dxf_path: str) -> list[DxfEntity]:
    """Extract all TEXT/MTEXT entities from DXF modelspace."""
    try:
        import ezdxf
    except ImportError:  # pragma: no cover
        sys.exit("ezdxf not installed: pip install ezdxf")

    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    entities: list[DxfEntity] = []

    for e in msp:
        etype = e.dxftype()
        if etype == "TEXT":
            text = (e.dxf.text or "").strip()
            if not text:
                continue
            entities.append({
                "text":   text,
                "insert": (round(e.dxf.insert.x, 4), round(e.dxf.insert.y, 4)),
                "height": getattr(e.dxf, "height", 2.5) or 2.5,
                "halign": getattr(e.dxf, "halign", 0),
                "valign": getattr(e.dxf, "valign", 0),
                "layer":  getattr(e.dxf, "layer", "0") or "0",
                "type":   "TEXT",
            })
        elif etype == "MTEXT":
            text = e.plain_text().strip()
            if not text:
                continue
            entities.append({
                "text":   text,
                "insert": (round(e.dxf.insert.x, 4), round(e.dxf.insert.y, 4)),
                "height": getattr(e.dxf, "char_height", 2.5) or 2.5,
                "halign": None,
                "valign": None,
                "layer":  getattr(e.dxf, "layer", "0") or "0",
                "type":   "MTEXT",
            })

    return entities


def get_dxf_extents(dxf_path: str) -> dict:
    """Return drawing extents by scanning entity bboxes (never trust DXF header)."""
    try:
        import ezdxf
        from ezdxf.bbox import extents as bbox_extents
    except ImportError:  # pragma: no cover
        sys.exit("ezdxf not installed: pip install ezdxf")

    doc = ezdxf.readfile(dxf_path)
    bbox = bbox_extents(doc.modelspace())
    if bbox is None or not bbox.has_data:
        raise ValueError(f"No geometry found in {dxf_path}")

    x_min, y_min = bbox.extmin.x, bbox.extmin.y
    x_max, y_max = bbox.extmax.x, bbox.extmax.y
    return {
        "x_min": x_min, "y_min": y_min,
        "x_max": x_max, "y_max": y_max,
        "width":  x_max - x_min,
        "height": y_max - y_min,
    }


# ──────────────────────────────────────────────
# 2.  Coordinate Transform
# ──────────────────────────────────────────────

class CoordTransform:
    """DXF (Y-up) → Leaflet CRS.Simple (lat = -y_px, lng = x_px)."""

    def __init__(self, dxf_extents: dict, tile_meta: dict) -> None:
        full_w   = tile_meta["full_width_px"]
        full_h   = tile_meta["full_height_px"]
        tile_sz  = tile_meta["tile_size"]
        short_px = min(full_w, full_h)
        coord_w  = full_w * tile_sz / short_px
        coord_h  = full_h * tile_sz / short_px
        self._dxf     = dxf_extents
        self._scale_x = coord_w / dxf_extents["width"]
        self._scale_y = coord_h / dxf_extents["height"]
        self._coord_h = coord_h

    def to_leaflet(self, dxf_x: float, dxf_y: float) -> LatLng:
        px = (dxf_x - self._dxf["x_min"]) * self._scale_x
        py = self._coord_h - (dxf_y - self._dxf["y_min"]) * self._scale_y
        return {"lat": round(-py, 4), "lng": round(px, 4)}

    def corners_to_leaflet(self, corners: list[tuple[float, float]]) -> list[LatLng]:
        return [self.to_leaflet(x, y) for x, y in corners]


# ──────────────────────────────────────────────
# 3.  Bounding Box
# ──────────────────────────────────────────────

_CHAR_WIDTH = 0.6   # advance width / cap-height (simple monospace estimate)
_PAD        = 0.12  # padding as fraction of cap-height


def compute_bbox(entity: DxfEntity, transform: CoordTransform) -> HitboxBbox | None:
    """Return Leaflet HitboxBbox for an entity, or None if height is zero."""
    h = entity["height"]
    if h <= 0.0:
        return None

    raw_w = len(entity["text"]) * h * _CHAR_WIDTH
    pad   = h * _PAD
    ix, iy = entity["insert"]
    halign = entity.get("halign") or 0
    valign = entity.get("valign") or 0
    # MTEXT sets halign/valign=None; both default to 0 (left/baseline) — acceptable for this simple implementation

    # Local X offsets relative to insert (pre-rotation)
    if halign == 1:    # Center
        lx_min, lx_max = -raw_w / 2 - pad,  raw_w / 2 + pad
    elif halign == 2:  # Right
        lx_min, lx_max = -raw_w - pad,  pad
    else:              # Left / Aligned / Fit / default
        lx_min, lx_max = -pad,  raw_w + pad

    # Local Y offsets (DXF Y-up)
    if valign == 1:    # Bottom
        ly_min, ly_max = -pad,  h + pad
    elif valign == 2:  # Middle
        ly_min, ly_max = -h / 2 - pad,  h / 2 + pad
    elif valign == 3:  # Top
        ly_min, ly_max = -h - pad,  pad
    else:              # Baseline / default: descenders ≈ 20 % below insert
        ly_min, ly_max = -h * 0.2 - pad,  h + pad

    corners_dxf = [
        (ix + lx_min, iy + ly_min),
        (ix + lx_max, iy + ly_min),
        (ix + lx_max, iy + ly_max),
        (ix + lx_min, iy + ly_max),
    ]
    return {"leaflet": {"corners": transform.corners_to_leaflet(corners_dxf)}}


# ──────────────────────────────────────────────
# 4.  Matching
# ──────────────────────────────────────────────

def build_index(entities: list[DxfEntity]) -> dict[str, DxfEntity]:
    """Map text → first entity with that exact text (case-sensitive)."""
    idx: dict[str, DxfEntity] = {}
    for e in entities:
        key = e["text"].strip()
        if key not in idx:
            idx[key] = e
    return idx


def build_hitboxes(
    labels:    list[str],
    index:     dict[str, DxfEntity],
    transform: CoordTransform | None,
) -> list[HitboxRecord]:
    """Return a HitboxRecord for each label found in the index."""
    hitboxes: list[HitboxRecord] = []
    for label in labels:
        entity = index.get(label.strip())
        if entity is None:
            continue
        leaflet = transform.to_leaflet(*entity["insert"]) if transform else None
        bbox    = compute_bbox(entity, transform) if transform else None
        hitboxes.append({
            "label":   label,
            "found":   True,
            "leaflet": leaflet,
            "bbox":    bbox,
        })
    return hitboxes


# ──────────────────────────────────────────────
# 5.  CLI
# ──────────────────────────────────────────────

def load_labels(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return [
            line.strip()
            for line in f
            if line.strip() and not line.startswith("#")
        ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate hitboxes.json from DXF + labels list (exact match)."
    )
    p.add_argument("--dxf",       required=True,
                   help="Path to .dxf file")
    p.add_argument("--labels",    required=True,
                   help="Text file with one label per line")
    p.add_argument("--tile-meta", default=None, metavar="FILE",
                   help="tile_meta.json from rasterise_tiles.py "
                        "(provides Leaflet scale; omit to skip coordinate output)")
    p.add_argument("--out",       default="hitboxes.json", metavar="FILE",
                   help="Output path for hitboxes.json (default: hitboxes.json)")
    p.add_argument("--verbose",   action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
    )

    labels = load_labels(args.labels)
    logger.info("Labels loaded: %d", len(labels))

    entities = extract_text_entities(args.dxf)
    logger.info("DXF text entities: %d", len(entities))

    index = build_index(entities)
    logger.debug("Index size: %d unique texts", len(index))

    transform: CoordTransform | None = None
    if args.tile_meta:
        with open(args.tile_meta, encoding="utf-8") as f:
            tile_meta = json.load(f)
        extents = get_dxf_extents(args.dxf)
        transform = CoordTransform(extents, tile_meta)
        logger.info("CoordTransform ready (scale_x=%.4f)", transform._scale_x)
    else:
        logger.warning("No --tile-meta — Leaflet coords will be null")

    hitboxes = build_hitboxes(labels, index, transform)

    not_found = [lbl for lbl in labels if index.get(lbl.strip()) is None]
    logger.info("Matched: %d / %d", len(hitboxes), len(labels))
    if not_found:
        logger.warning("Unmatched (%d): %s", len(not_found), not_found[:10])

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(hitboxes, f, indent=2, ensure_ascii=False)
    logger.info("Written: %s (%d records)", out_path, len(hitboxes))


if __name__ == "__main__":
    main()
