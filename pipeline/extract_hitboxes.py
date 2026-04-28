"""
extract_hitboxes.py
───────────────────
Stage 3 (exact-match + cluster): generate hitboxes.json from a DXF file and a labels list.

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
from collections import defaultdict
from pathlib import Path
from typing import TypedDict

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Types
# ──────────────────────────────────────────────


class DxfEntity(TypedDict):
    text: str
    insert: tuple[float, float]
    height: float
    width_factor: float  # DXF group code 41; 1.0 = normal, <1 condensed, >1 expanded
    halign: int | None
    valign: int | None
    layer: str
    type: str  # "TEXT" | "MTEXT"


class LatLng(TypedDict):
    lat: float
    lng: float


class HitboxBbox(TypedDict):
    leaflet: dict[str, list[LatLng]]  # {"corners": [TL, TR, BR, BL]}


class HitboxRecord(TypedDict):
    label: str
    found: bool
    clustered: bool  # True when matched via spatial cluster
    leaflet: LatLng | None
    bbox: HitboxBbox | None


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
            entities.append(
                {
                    "text": text,
                    "insert": (round(e.dxf.insert.x, 4), round(e.dxf.insert.y, 4)),
                    "height": getattr(e.dxf, "height", 2.5) or 2.5,
                    "width_factor": getattr(e.dxf, "width", 1.0) or 1.0,
                    "halign": getattr(e.dxf, "halign", 0),
                    "valign": getattr(e.dxf, "valign", 0),
                    "layer": getattr(e.dxf, "layer", "0") or "0",
                    "type": "TEXT",
                }
            )
        elif etype == "MTEXT":
            text = e.plain_text().strip()  # type: ignore[attr-defined]
            if not text:
                continue
            entities.append(
                {
                    "text": text,
                    "insert": (round(e.dxf.insert.x, 4), round(e.dxf.insert.y, 4)),
                    "height": getattr(e.dxf, "char_height", 2.5) or 2.5,
                    "width_factor": 1.0,
                    "halign": None,
                    "valign": None,
                    "layer": getattr(e.dxf, "layer", "0") or "0",
                    "type": "MTEXT",
                }
            )

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
        "x_min": x_min,
        "y_min": y_min,
        "x_max": x_max,
        "y_max": y_max,
        "width": x_max - x_min,
        "height": y_max - y_min,
    }


# ──────────────────────────────────────────────
# 2.  Coordinate Transform
# ──────────────────────────────────────────────


class CoordTransform:
    """DXF (Y-up) → Leaflet CRS.Simple (lat = -y_px, lng = x_px)."""

    def __init__(self, dxf_extents: dict, tile_meta: dict) -> None:
        full_w = tile_meta["full_width_px"]
        full_h = tile_meta["full_height_px"]
        tile_sz = tile_meta["tile_size"]
        short_px = min(full_w, full_h)
        coord_w = full_w * tile_sz / short_px
        coord_h = full_h * tile_sz / short_px
        self._dxf = dxf_extents
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

_CHAR_WIDTH = 0.6  # advance width / cap-height (simple monospace estimate)
_PAD = 0.12  # padding as fraction of cap-height


def _entity_dxf_corners(entity: DxfEntity) -> list[tuple[float, float]] | None:
    """Return the four DXF-space bbox corners for an entity, or None if height is zero."""
    h = entity["height"]
    if h <= 0.0:
        return None

    raw_w = len(entity["text"]) * h * _CHAR_WIDTH * (entity.get("width_factor") or 1.0)
    pad = h * _PAD
    ix, iy = entity["insert"]
    halign = entity.get("halign") or 0
    valign = entity.get("valign") or 0

    if halign == 1:  # Center
        lx_min, lx_max = -raw_w / 2 - pad, raw_w / 2 + pad
    elif halign == 2:  # Right
        lx_min, lx_max = -raw_w - pad, pad
    else:  # Left / Aligned / Fit / default
        lx_min, lx_max = -pad, raw_w + pad

    if valign == 1:  # Bottom
        ly_min, ly_max = -pad, h + pad
    elif valign == 2:  # Middle
        ly_min, ly_max = -h / 2 - pad, h / 2 + pad
    elif valign == 3:  # Top
        ly_min, ly_max = -h - pad, pad
    else:  # Baseline / default: descenders ≈ 20 % below insert
        ly_min, ly_max = -h * 0.2 - pad, h + pad

    return [
        (ix + lx_min, iy + ly_min),
        (ix + lx_max, iy + ly_min),
        (ix + lx_max, iy + ly_max),
        (ix + lx_min, iy + ly_max),
    ]


def compute_bbox(entity: DxfEntity, transform: CoordTransform) -> HitboxBbox | None:
    """Return Leaflet HitboxBbox for an entity, or None if height is zero."""
    corners = _entity_dxf_corners(entity)
    if corners is None:
        return None
    return {"leaflet": {"corners": transform.corners_to_leaflet(corners)}}


def _entity_centre(entity: DxfEntity) -> tuple[float, float]:
    """Return the approximate DXF-space centre of a text entity's bounding box."""
    corners = _entity_dxf_corners(entity)
    if corners:
        xs = [c[0] for c in corners]
        ys = [c[1] for c in corners]
        return (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
    return entity["insert"]


# ──────────────────────────────────────────────
# 3.5  Spatial Clustering
# ──────────────────────────────────────────────

_DEFAULT_CLUSTER_GAP = 3.5  # × cap-height  (vertical)
_DEFAULT_H_TOLERANCE = 2.5  # × cap-height  (horizontal gate)


def build_clusters(
    entities: list[DxfEntity],
    gap_factor: float = _DEFAULT_CLUSTER_GAP,
    h_tolerance: float = _DEFAULT_H_TOLERANCE,
) -> list[list[DxfEntity]]:
    """
    Single-linkage spatial clustering of text entities.

    Returns clusters with ≥2 members sorted in reading order
    (descending Y first, then ascending X — DXF is Y-up so higher Y = higher on page).

    Proximity is checked on each axis independently:
      vertical   : dy <= gap_factor  × max(hi, hj)
      horizontal : dx <= h_tolerance × max(hi, hj)
    """
    n = len(entities)
    if n == 0:
        return []

    centres = [_entity_centre(e) for e in entities]

    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        pi, pj = find(i), find(j)
        if pi != pj:
            parent[pi] = pj

    for i in range(n):
        hi = entities[i].get("height", 0.0) or 0.0
        for j in range(i + 1, n):
            hj = entities[j].get("height", 0.0) or 0.0
            scale = max(hi, hj, 0.001)
            cx1, cy1 = centres[i]
            cx2, cy2 = centres[j]
            dy = abs(cy2 - cy1)
            dx = abs(cx2 - cx1)
            if dy <= gap_factor * scale and dx <= h_tolerance * scale:
                union(i, j)

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)

    clusters = []
    for members in groups.values():
        if len(members) < 2:
            continue
        sorted_members = sorted(members, key=lambda i: (-round(centres[i][1], 2), centres[i][0]))
        clusters.append([entities[i] for i in sorted_members])

    return clusters


def _cluster_rows(cluster: list[DxfEntity]) -> tuple[list[str], list[str]] | None:
    """
    Split cluster entities into top-row and bottom-row token lists.
    Y values are rounded to 1dp to absorb small jitter; highest Y = top row.
    Returns (top_tokens, bottom_tokens), or None when fewer than 2 distinct rows exist.
    """
    centres = [_entity_centre(e) for e in cluster]
    y_vals = [round(cy, 1) for _, cy in centres]
    unique_ys = sorted(set(y_vals), reverse=True)
    if len(unique_ys) < 2:
        return None
    top_y, bot_y = unique_ys[0], unique_ys[1]
    top_tokens = [e["text"].strip() for e, y in zip(cluster, y_vals, strict=True) if y == top_y]
    bottom_tokens = [e["text"].strip() for e, y in zip(cluster, y_vals, strict=True) if y == bot_y]
    return top_tokens, bottom_tokens


def _inverted_t_variants(cluster: list[DxfEntity]) -> set[str]:
    """
    One top token + two or more bottom tokens → pair each bottom with the top.

    Layout:    "FV"          ← top
           "12"    "54"      ← bottom siblings
    Produces: {"FV12", "FV 12", "FV54", "FV 54"}
    """
    if len(cluster) < 3:
        return set()
    rows = _cluster_rows(cluster)
    if rows is None:
        return set()
    top_tokens, bottom_tokens = rows
    if len(top_tokens) != 1 or len(bottom_tokens) < 2:
        return set()
    top = top_tokens[0]
    return {f"{top}{bt}" for bt in bottom_tokens} | {f"{top} {bt}" for bt in bottom_tokens}


def build_cluster_index(
    entities: list[DxfEntity],
    gap_factor: float = _DEFAULT_CLUSTER_GAP,
    h_tolerance: float = _DEFAULT_H_TOLERANCE,
) -> dict[str, list[list[DxfEntity]]]:
    """
    Build a lookup: joined_text → [cluster, cluster, ...]

    Variants indexed per cluster:
      - no separator:   "TCV901"
      - space:          "TCV 901"
      - inverted-T:     top + each discrete bottom sibling
    Also indexes upper-case keys for case-insensitive fallback.
    """
    clusters = build_clusters(entities, gap_factor, h_tolerance)
    index: dict[str, list[list[DxfEntity]]] = defaultdict(list)

    for cluster in clusters:
        parts = [e["text"].strip() for e in cluster]
        variants: set[str] = {
            "".join(parts),
            " ".join(parts),
        }
        variants |= _inverted_t_variants(cluster)

        for v in variants:
            if v:
                index[v].append(cluster)
                index[v.upper()].append(cluster)

    return dict(index)


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


def _cluster_to_hitbox(
    label: str,
    cluster: list[DxfEntity],
    transform: CoordTransform,
) -> HitboxRecord:
    """Build a HitboxRecord from a cluster match using the AABB of all member bboxes."""
    all_corners: list[tuple[float, float]] = []
    for e in cluster:
        corners = _entity_dxf_corners(e)
        if corners:
            all_corners.extend(corners)

    if all_corners:
        xs = [c[0] for c in all_corners]
        ys = [c[1] for c in all_corners]
        cx = (min(xs) + max(xs)) / 2
        cy = (min(ys) + max(ys)) / 2
        leaflet = transform.to_leaflet(cx, cy)
        aabb = [
            (min(xs), min(ys)),
            (max(xs), min(ys)),
            (max(xs), max(ys)),
            (min(xs), max(ys)),
        ]
        bbox: HitboxBbox | None = {"leaflet": {"corners": transform.corners_to_leaflet(aabb)}}
    else:  # pragma: no cover — all cluster members have zero height
        leaflet = None
        bbox = None

    return {
        "label": label,
        "found": True,
        "clustered": True,
        "leaflet": leaflet,
        "bbox": bbox,
    }


def build_hitboxes(
    labels: list[str],
    index: dict[str, DxfEntity],
    transform: CoordTransform,
    cluster_index: dict[str, list[list[DxfEntity]]] | None = None,
) -> list[HitboxRecord]:
    """Return a HitboxRecord for each label resolved via exact or cluster match."""
    hitboxes: list[HitboxRecord] = []
    ci = cluster_index or {}

    for label in labels:
        key = label.strip()
        entity = index.get(key)

        if entity is not None:
            leaflet = transform.to_leaflet(*entity["insert"])
            bbox = compute_bbox(entity, transform)
            hitboxes.append(
                {
                    "label": label,
                    "found": True,
                    "clustered": False,
                    "leaflet": leaflet,
                    "bbox": bbox,
                }
            )
            continue

        cluster_hits = ci.get(key) or ci.get(key.upper())
        if cluster_hits:
            hitboxes.append(_cluster_to_hitbox(label, cluster_hits[0], transform))

    return hitboxes


# ──────────────────────────────────────────────
# 5.  Debug SVG
# ──────────────────────────────────────────────

_SVG_MAX_DIM = 4000  # longest side of the debug SVG in pixels


def _svg_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def write_debug_svg(
    entities: list[DxfEntity],
    labels: list[str],
    index: dict[str, DxfEntity],
    cluster_index: dict[str, list[list[DxfEntity]]],
    extents: dict,
    out_path: str,
) -> None:
    """Render all text-entity bboxes into an SVG for debugging hitbox placement.

    Colour key:
      green  — exact label match
      blue   — cluster label match
      gray   — unmatched DXF entity (not in labels list)
    """
    x_min, y_min = extents["x_min"], extents["y_min"]
    dxf_w, dxf_h = extents["width"], extents["height"]

    if dxf_w <= 0 or dxf_h <= 0:
        logger.warning("Cannot write debug SVG: zero-size extents")
        return

    if dxf_w >= dxf_h:
        svg_w = _SVG_MAX_DIM
        svg_h = int(_SVG_MAX_DIM * dxf_h / dxf_w)
    else:
        svg_h = _SVG_MAX_DIM
        svg_w = int(_SVG_MAX_DIM * dxf_w / dxf_h)

    scale = svg_w / dxf_w

    def to_svg(dxf_x: float, dxf_y: float) -> tuple[float, float]:
        return (dxf_x - x_min) * scale, (extents["y_max"] - dxf_y) * scale

    def rect_from_corners(corners: list[tuple[float, float]]) -> tuple[float, float, float, float]:
        pts = [to_svg(x, y) for x, y in corners]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)

    # Classify every entity
    exact_ids: set[int] = set()
    cluster_ids: set[int] = set()
    not_found: list[str] = []

    for label in labels:
        key = label.strip()
        entity = index.get(key)
        if entity is not None:
            exact_ids.add(id(entity))
        else:
            hits = cluster_index.get(key) or cluster_index.get(key.upper())
            if hits:
                for e in hits[0]:
                    cluster_ids.add(id(e))
            else:
                not_found.append(label)

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{svg_w}" height="{svg_h}" viewBox="0 0 {svg_w} {svg_h}">',
        f'<rect width="{svg_w}" height="{svg_h}" fill="#1a1a1a"/>',
        '<g id="entities">',
    ]

    for e in entities:
        corners = _entity_dxf_corners(e)
        if corners is None:
            continue
        rx, ry, rw, rh = rect_from_corners(corners)
        eid = id(e)
        if eid in exact_ids:
            fill, stroke, text_fill = "rgba(0,200,80,0.30)", "#00c850", "#00ff88"
        elif eid in cluster_ids:
            fill, stroke, text_fill = "rgba(60,140,255,0.30)", "#3c8cff", "#80bfff"
        else:
            fill, stroke, text_fill = "rgba(180,180,180,0.12)", "#666", "#999"

        parts.append(
            f'<rect x="{rx:.2f}" y="{ry:.2f}" width="{rw:.2f}" height="{rh:.2f}"'
            f' fill="{fill}" stroke="{stroke}" stroke-width="0.8"/>'
        )
        font_px = max(5.0, min(14.0, rh * 0.55))
        label_txt = _svg_escape(e["text"])
        cx, cy = rx + rw / 2, ry + rh / 2
        parts.append(
            f'<text x="{cx:.2f}" y="{cy:.2f}" font-size="{font_px:.1f}" font-family="monospace"'
            f' text-anchor="middle" dominant-baseline="middle" fill="{text_fill}">{label_txt}</text>'
        )

    parts.append("</g>")

    # Legend
    legend = [
        ("rgba(0,200,80,0.30)", "#00c850", "#00ff88", "Exact match"),
        ("rgba(60,140,255,0.30)", "#3c8cff", "#80bfff", "Cluster match"),
        ("rgba(180,180,180,0.12)", "#666", "#999", "Unmatched entity"),
    ]
    lx, ly = 12, 12
    for fill, stroke, text_fill, desc in legend:
        parts.append(
            f'<rect x="{lx}" y="{ly}" width="16" height="10" fill="{fill}" stroke="{stroke}" stroke-width="0.8"/>'
        )
        parts.append(
            f'<text x="{lx + 22}" y="{ly + 9}" font-size="11" font-family="sans-serif" fill="{text_fill}">{desc}</text>'
        )
        ly += 17

    if not_found:
        preview = ", ".join(not_found[:8]) + ("…" if len(not_found) > 8 else "")
        parts.append(
            f'<text x="{lx}" y="{ly + 12}" font-size="10" font-family="monospace" fill="#ff6666">'
            f'Not found ({len(not_found)}): {_svg_escape(preview)}</text>'
        )

    parts.append("</svg>")

    Path(out_path).write_text("\n".join(parts), encoding="utf-8")
    logger.info("Debug SVG written: %s", out_path)


# ──────────────────────────────────────────────
# 6.  CLI
# ──────────────────────────────────────────────


def load_labels(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate hitboxes.json from DXF + labels list (exact + cluster match)."
    )
    p.add_argument("--dxf", required=True, help="Path to .dxf file")
    p.add_argument("--labels", required=True, help="Text file with one label per line")
    p.add_argument(
        "--tile-meta",
        required=True,
        metavar="FILE",
        help="tile_meta.json from rasterise_tiles.py (provides Leaflet scale)",
    )
    p.add_argument(
        "--out",
        default="hitboxes.json",
        metavar="FILE",
        help="Output path for hitboxes.json (default: hitboxes.json)",
    )
    p.add_argument(
        "--cluster-gap",
        type=float,
        default=_DEFAULT_CLUSTER_GAP,
        metavar="N",
        help=f"Vertical proximity threshold for clustering "
        f"(× cap-height, default {_DEFAULT_CLUSTER_GAP})",
    )
    p.add_argument(
        "--h-tolerance",
        type=float,
        default=_DEFAULT_H_TOLERANCE,
        metavar="N",
        help=f"Horizontal proximity gate for clustering "
        f"(× cap-height, default {_DEFAULT_H_TOLERANCE})",
    )
    p.add_argument(
        "--svg-out",
        metavar="FILE",
        help="Write a debug SVG with all entity bboxes and match highlights (optional)",
    )
    p.add_argument("--verbose", action="store_true")
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
    cluster_index = build_cluster_index(entities, args.cluster_gap, args.h_tolerance)
    logger.debug("Index size: %d unique texts, %d cluster variants", len(index), len(cluster_index))

    with open(args.tile_meta, encoding="utf-8") as f:
        tile_meta = json.load(f)
    extents = get_dxf_extents(args.dxf)
    transform = CoordTransform(extents, tile_meta)
    logger.info("CoordTransform ready (scale_x=%.4f)", transform._scale_x)

    hitboxes = build_hitboxes(labels, index, transform, cluster_index)

    not_found = [
        lbl
        for lbl in labels
        if index.get(lbl.strip()) is None
        and not (cluster_index.get(lbl.strip()) or cluster_index.get(lbl.strip().upper()))
    ]
    logger.info(
        "Matched: %d / %d  (cluster: %d)",
        len(hitboxes),
        len(labels),
        sum(1 for h in hitboxes if h.get("clustered")),
    )
    if not_found:
        logger.warning("Unmatched (%d): %s", len(not_found), not_found[:10])

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(hitboxes, f, indent=2, ensure_ascii=False)
    logger.info("Written: %s (%d records)", out_path, len(hitboxes))

    if args.svg_out:
        write_debug_svg(entities, labels, index, cluster_index, extents, args.svg_out)


if __name__ == "__main__":
    main()
