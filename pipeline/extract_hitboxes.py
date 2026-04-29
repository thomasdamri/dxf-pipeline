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

from pipeline_types import DxfEntity, HitboxRecord, LatLng

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 1.  DXF Extraction
# ──────────────────────────────────────────────


def extract_text_entities(dxf_path: str) -> list[DxfEntity]:
    """Extract all TEXT/MTEXT entities from DXF modelspace."""
    try:
        import ezdxf
        from ezdxf.bbox import extents as _bbox_extents
    except ImportError:  # pragma: no cover
        sys.exit("ezdxf not installed: pip install ezdxf")

    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    entities: list[DxfEntity] = []

    def _entity_bbox(e) -> tuple[float, float, float, float] | None:
        try:
            bb = _bbox_extents([e], fast=False)
            if bb and bb.has_data:
                return (bb.extmin.x, bb.extmin.y, bb.extmax.x, bb.extmax.y)
        except Exception:  # pragma: no cover
            pass
        return None  # pragma: no cover

    for e in msp:
        etype = e.dxftype()
        if etype == "TEXT":
            text = (e.dxf.text or "").strip()
            if not text:
                continue
            entities.append(
                {
                    "text": text,
                    "type": "TEXT",
                    "layer": getattr(e.dxf, "layer", "0") or "0",
                    "dxf_bbox": _entity_bbox(e),
                }
            )
        elif etype == "MTEXT":
            text = e.plain_text().strip()  # type: ignore[attr-defined]
            if not text:
                continue
            entities.append(
                {
                    "text": text,
                    "type": "MTEXT",
                    "layer": getattr(e.dxf, "layer", "0") or "0",
                    "dxf_bbox": _entity_bbox(e),
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

_PAD = 0.12  # padding as fraction of bbox height


def _padded_corners(
    xmin: float, ymin: float, xmax: float, ymax: float
) -> list[tuple[float, float]]:
    pad = (ymax - ymin) * _PAD
    return [
        (xmin - pad, ymin - pad),
        (xmax + pad, ymin - pad),
        (xmax + pad, ymax + pad),
        (xmin - pad, ymax + pad),
    ]


def _entity_dxf_corners(entity: DxfEntity) -> list[tuple[float, float]] | None:
    """Return the four padded DXF-space bbox corners, or None if dxf_bbox unavailable."""
    stored = entity.get("dxf_bbox")
    return None if stored is None else _padded_corners(*stored)


def compute_bbox(entity: DxfEntity, transform: CoordTransform) -> dict[str, list[LatLng]] | None:
    """Return Leaflet bbox corners for an entity, or None if dxf_bbox unavailable."""
    corners = _entity_dxf_corners(entity)
    if corners is None:
        return None
    return {"corners": transform.corners_to_leaflet(corners)}


def _entity_centre(entity: DxfEntity) -> tuple[float, float]:
    """Return the DXF-space centre of a text entity's bounding box."""
    stored = entity.get("dxf_bbox")
    if stored is not None:
        xmin, ymin, xmax, ymax = stored
        return (xmin + xmax) / 2, (ymin + ymax) / 2
    return 0.0, 0.0


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
        bb_i = entities[i].get("dxf_bbox")
        hi = (bb_i[3] - bb_i[1]) if bb_i else 0.001
        for j in range(i + 1, n):
            bb_j = entities[j].get("dxf_bbox")
            hj = (bb_j[3] - bb_j[1]) if bb_j else 0.001
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
) -> dict[str, list[DxfEntity]]:
    """
    Build a lookup: joined_text → cluster (first match wins).

    Variants indexed per cluster:
      - no separator:   "TCV901"
      - space:          "TCV 901"
      - inverted-T:     top + each discrete bottom sibling
    Also indexes upper-case keys for case-insensitive fallback.
    """
    clusters = build_clusters(entities, gap_factor, h_tolerance)
    index: dict[str, list[DxfEntity]] = {}

    for cluster in clusters:
        parts = [e["text"].strip() for e in cluster]
        variants: set[str] = {
            "".join(parts),
            " ".join(parts),
        }
        variants |= _inverted_t_variants(cluster)

        for v in variants:
            if v and v not in index:
                index[v] = cluster
                index[v.upper()] = cluster

    return index


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
    """Build a HitboxRecord from a cluster match using the union of member dxf_bboxes."""
    stored = [b for e in cluster if (b := e.get("dxf_bbox")) is not None]
    if stored:
        xmin = min(b[0] for b in stored)
        ymin = min(b[1] for b in stored)
        xmax = max(b[2] for b in stored)
        ymax = max(b[3] for b in stored)
        bbox: dict[str, list[LatLng]] | None = {
            "corners": transform.corners_to_leaflet(_padded_corners(xmin, ymin, xmax, ymax))
        }
    else:  # pragma: no cover
        bbox = None

    return {
        "label": label,
        "found": True,
        "clustered": True,
        "bbox": bbox,
    }


def build_hitboxes(
    labels: list[str],
    index: dict[str, DxfEntity],
    transform: CoordTransform,
    cluster_index: dict[str, list[DxfEntity]] | None = None,
) -> list[HitboxRecord]:
    """Return a HitboxRecord for each label resolved via exact or cluster match."""
    hitboxes: list[HitboxRecord] = []
    ci = cluster_index or {}

    for label in labels:
        key = label.strip()
        entity = index.get(key)

        if entity is not None:
            hitboxes.append(
                {
                    "label": label,
                    "found": True,
                    "clustered": False,
                    "bbox": compute_bbox(entity, transform),
                }
            )
            continue

        cluster = ci.get(key) or ci.get(key.upper())
        if cluster:
            hitboxes.append(_cluster_to_hitbox(label, cluster, transform))

    return hitboxes


# ──────────────────────────────────────────────
# 5.  CLI
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


if __name__ == "__main__":
    main()
