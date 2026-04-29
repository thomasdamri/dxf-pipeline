"""
Unit + integration tests for extract_hitboxes.py.
Uses conftest fixtures: minimal_dxf, minimal_tile_meta.
"""

import json

import pytest
from extract_hitboxes import (
    CoordTransform,
    _cluster_rows,
    _entity_centre,
    _entity_dxf_corners,
    _inverted_t_variants,
    build_cluster_index,
    build_clusters,
    build_hitboxes,
    build_index,
    compute_bbox,
    extract_text_entities,
    get_dxf_extents,
    load_labels,
    main,
    parse_args,
)
from pipeline_types import DxfEntity

# ─────────────────────────────────────────────────────────────
# extract_text_entities
# ─────────────────────────────────────────────────────────────


class TestExtractTextEntities:
    def test_returns_list(self, minimal_dxf):
        result = extract_text_entities(str(minimal_dxf))
        assert isinstance(result, list)

    def test_extracts_both_text_entities(self, minimal_dxf):
        # minimal_dxf has TEXT "FV101" and TEXT "HV201"
        texts = [e["text"] for e in extract_text_entities(str(minimal_dxf))]
        assert "FV101" in texts
        assert "HV201" in texts

    def test_entity_has_required_keys(self, minimal_dxf):
        result = extract_text_entities(str(minimal_dxf))
        assert len(result) > 0
        for key in ("text", "type", "layer", "dxf_bbox"):
            assert key in result[0], f"Missing key: {key}"

    def test_entity_has_no_legacy_keys(self, minimal_dxf):
        result = extract_text_entities(str(minimal_dxf))
        assert len(result) > 0
        for key in ("insert", "height", "width_factor", "halign", "valign"):
            assert key not in result[0], f"Unexpected legacy key present: {key}"

    def test_dxf_bbox_is_4tuple_or_none(self, minimal_dxf):
        result = extract_text_entities(str(minimal_dxf))
        for e in result:
            val = e["dxf_bbox"]
            assert val is None or (isinstance(val, tuple) and len(val) == 4)

    def test_type_field_is_text_for_text_entity(self, minimal_dxf):
        result = extract_text_entities(str(minimal_dxf))
        assert all(e["type"] == "TEXT" for e in result)

    def test_mtext_entity_extracted(self, tmp_path):
        import ezdxf

        doc = ezdxf.new(dxfversion="R2010")
        doc.modelspace().add_mtext(
            "MTEXT_LABEL", dxfattribs={"insert": (5.0, 5.0), "char_height": 2.5}
        )
        dxf_path = tmp_path / "mtext.dxf"
        doc.saveas(str(dxf_path))

        texts = [e["text"] for e in extract_text_entities(str(dxf_path))]
        assert "MTEXT_LABEL" in texts

    def test_mtext_type_field(self, tmp_path):
        import ezdxf

        doc = ezdxf.new(dxfversion="R2010")
        doc.modelspace().add_mtext("MLABEL", dxfattribs={"insert": (0, 0), "char_height": 2.5})
        dxf_path = tmp_path / "mtext_type.dxf"
        doc.saveas(str(dxf_path))

        result = extract_text_entities(str(dxf_path))
        mtext_types = [e["type"] for e in result if e["text"] == "MLABEL"]
        assert mtext_types == ["MTEXT"]

    def test_empty_text_entities_excluded(self, tmp_path):
        import ezdxf

        doc = ezdxf.new(dxfversion="R2010")
        msp = doc.modelspace()
        msp.add_text("", dxfattribs={"insert": (0, 0), "height": 2.5})
        msp.add_text("   ", dxfattribs={"insert": (1, 0), "height": 2.5})
        dxf_path = tmp_path / "empty_text.dxf"
        doc.saveas(str(dxf_path))

        assert extract_text_entities(str(dxf_path)) == []

    def test_non_text_entities_skipped(self, tmp_path):
        import ezdxf

        doc = ezdxf.new(dxfversion="R2010")
        msp = doc.modelspace()
        msp.add_lwpolyline([(0, 0), (10, 0), (10, 10)])
        msp.add_circle((5, 5), radius=3.0)
        dxf_path = tmp_path / "no_text.dxf"
        doc.saveas(str(dxf_path))

        assert extract_text_entities(str(dxf_path)) == []

    def test_empty_mtext_entities_excluded(self, tmp_path):
        import ezdxf

        doc = ezdxf.new(dxfversion="R2010")
        doc.modelspace().add_mtext("", dxfattribs={"insert": (0, 0), "char_height": 2.5})
        dxf_path = tmp_path / "empty_mtext.dxf"
        doc.saveas(str(dxf_path))
        assert extract_text_entities(str(dxf_path)) == []


# ─────────────────────────────────────────────────────────────
# get_dxf_extents
# ─────────────────────────────────────────────────────────────


class TestGetDxfExtents:
    def test_returns_dict_with_extents_keys(self, minimal_dxf):
        result = get_dxf_extents(str(minimal_dxf))
        for key in ("x_min", "y_min", "x_max", "y_max", "width", "height"):
            assert key in result

    def test_width_and_height_positive(self, minimal_dxf):
        result = get_dxf_extents(str(minimal_dxf))
        assert result["width"] > 0
        assert result["height"] > 0

    def test_x_max_greater_than_x_min(self, minimal_dxf):
        result = get_dxf_extents(str(minimal_dxf))
        assert result["x_max"] > result["x_min"]

    def test_empty_dxf_raises_value_error(self, tmp_path):
        import ezdxf

        doc = ezdxf.new()
        dxf_path = tmp_path / "empty.dxf"
        doc.saveas(str(dxf_path))

        with pytest.raises(ValueError, match="No geometry"):
            get_dxf_extents(str(dxf_path))


# ─────────────────────────────────────────────────────────────
# CoordTransform
# ─────────────────────────────────────────────────────────────

_DXF_EXTENTS = {
    "x_min": 0.0,
    "y_min": 0.0,
    "x_max": 200.0,
    "y_max": 100.0,
    "width": 200.0,
    "height": 100.0,
}
# minimal_tile_meta: full_w=1024, full_h=512, tile_sz=256
#   → short_px=512, coord_w=512, coord_h=256
#   → scale_x = 512/200 = 2.56, scale_y = 256/100 = 2.56


class TestCoordTransform:
    @pytest.fixture
    def tf(self, minimal_tile_meta):
        return CoordTransform(_DXF_EXTENTS, minimal_tile_meta)

    def test_scale_x(self, tf):
        assert tf._scale_x == pytest.approx(512.0 / 200.0)

    def test_scale_y(self, tf):
        assert tf._scale_y == pytest.approx(256.0 / 100.0)

    def test_coord_h(self, tf):
        assert tf._coord_h == pytest.approx(256.0)

    def test_dxf_origin_maps_to_bottom_left_leaflet(self, tf):
        # DXF (0,0) = bottom-left → Leaflet lat=-256, lng=0
        ll = tf.to_leaflet(0.0, 0.0)
        assert ll["lat"] == pytest.approx(-256.0)
        assert ll["lng"] == pytest.approx(0.0)

    def test_dxf_max_corner_maps_to_top_right_leaflet(self, tf):
        # DXF (200,100) = top-right → Leaflet lat=0, lng=512
        ll = tf.to_leaflet(200.0, 100.0)
        assert ll["lat"] == pytest.approx(0.0)
        assert ll["lng"] == pytest.approx(512.0)

    def test_centre_maps_correctly(self, tf):
        # DXF (100,50) → px=256, py=128 → lat=-128, lng=256
        ll = tf.to_leaflet(100.0, 50.0)
        assert ll["lat"] == pytest.approx(-128.0, abs=0.01)
        assert ll["lng"] == pytest.approx(256.0, abs=0.01)

    def test_to_leaflet_returns_latlng_keys(self, tf):
        ll = tf.to_leaflet(0.0, 0.0)
        assert set(ll.keys()) == {"lat", "lng"}

    def test_corners_to_leaflet_returns_list_of_latlng(self, tf):
        corners = [(0.0, 0.0), (200.0, 0.0), (200.0, 100.0), (0.0, 100.0)]
        result = tf.corners_to_leaflet(corners)
        assert len(result) == 4
        assert all("lat" in ll and "lng" in ll for ll in result)

    def test_corners_to_leaflet_values(self, tf):
        corners = [(0.0, 0.0), (200.0, 100.0)]
        result = tf.corners_to_leaflet(corners)
        assert result[0]["lat"] == pytest.approx(-256.0)
        assert result[0]["lng"] == pytest.approx(0.0)
        assert result[1]["lat"] == pytest.approx(0.0)
        assert result[1]["lng"] == pytest.approx(512.0)


# ─────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────


def _make_entity(
    text: str = "FV101",
    x: float = 0.0,
    y: float = 0.0,
    height: float = 2.5,
    etype: str = "TEXT",
    layer: str = "TEXT",
    dxf_bbox: tuple[float, float, float, float] | None = None,
) -> DxfEntity:
    """Build a minimal DxfEntity. Synthesises dxf_bbox from position/height when omitted."""
    if dxf_bbox is None:
        dxf_bbox = (x, y, x + len(text) * height * 0.6, y + height)
    return {
        "text": text,
        "type": etype,
        "layer": layer,
        "dxf_bbox": dxf_bbox,
    }


def _make_tf(minimal_tile_meta) -> CoordTransform:
    return CoordTransform(_DXF_EXTENTS, minimal_tile_meta)


# ─────────────────────────────────────────────────────────────
# _entity_dxf_corners
# ─────────────────────────────────────────────────────────────


class TestEntityDxfCorners:
    def test_none_dxf_bbox_returns_none(self):
        e: DxfEntity = {"text": "X", "type": "TEXT", "layer": "0", "dxf_bbox": None}
        assert _entity_dxf_corners(e) is None

    def test_returns_four_corners(self):
        corners = _entity_dxf_corners(_make_entity())
        assert corners is not None
        assert len(corners) == 4

    def test_corners_include_padding(self):
        # bbox (1,2,11,7): bbox height = 5.0, pad = 5.0 * 0.12 = 0.6
        e = _make_entity(dxf_bbox=(1.0, 2.0, 11.0, 7.0))
        corners = _entity_dxf_corners(e)
        xs = [c[0] for c in corners]
        ys = [c[1] for c in corners]
        assert min(xs) == pytest.approx(1.0 - 5.0 * 0.12)
        assert max(xs) == pytest.approx(11.0 + 5.0 * 0.12)
        assert min(ys) == pytest.approx(2.0 - 5.0 * 0.12)
        assert max(ys) == pytest.approx(7.0 + 5.0 * 0.12)

    def test_zero_height_bbox_returns_corners(self):
        # A degenerate bbox (height=0) still returns 4 corners; padding is zero
        e = _make_entity(dxf_bbox=(5.0, 3.0, 15.0, 3.0))
        corners = _entity_dxf_corners(e)
        assert corners is not None
        assert len(corners) == 4


# ─────────────────────────────────────────────────────────────
# _entity_centre
# ─────────────────────────────────────────────────────────────


class TestEntityCentre:
    def test_returns_bbox_centre(self):
        e = _make_entity(dxf_bbox=(0.0, 0.0, 10.0, 4.0))
        cx, cy = _entity_centre(e)
        assert cx == pytest.approx(5.0)
        assert cy == pytest.approx(2.0)

    def test_none_dxf_bbox_returns_origin(self):
        e: DxfEntity = {"text": "X", "type": "TEXT", "layer": "0", "dxf_bbox": None}
        cx, cy = _entity_centre(e)
        assert cx == pytest.approx(0.0)
        assert cy == pytest.approx(0.0)


# ─────────────────────────────────────────────────────────────
# compute_bbox
# ─────────────────────────────────────────────────────────────


class TestComputeBbox:
    def test_none_dxf_bbox_returns_none(self, minimal_tile_meta):
        tf = _make_tf(minimal_tile_meta)
        e: DxfEntity = {"text": "X", "type": "TEXT", "layer": "0", "dxf_bbox": None}
        assert compute_bbox(e, tf) is None

    def test_returns_dict_with_corners_key(self, minimal_tile_meta):
        tf = _make_tf(minimal_tile_meta)
        result = compute_bbox(_make_entity(), tf)
        assert result is not None
        assert "corners" in result
        assert len(result["corners"]) == 4

    def test_each_corner_has_lat_lng(self, minimal_tile_meta):
        tf = _make_tf(minimal_tile_meta)
        result = compute_bbox(_make_entity(), tf)
        assert result is not None
        for corner in result["corners"]:
            assert "lat" in corner and "lng" in corner

    def test_corners_span_entity_width(self, minimal_tile_meta):
        tf = _make_tf(minimal_tile_meta)
        e = _make_entity(dxf_bbox=(50.0, 0.0, 60.0, 2.5))
        result = compute_bbox(e, tf)
        assert result is not None
        lngs = [c["lng"] for c in result["corners"]]
        assert max(lngs) > min(lngs)


# ─────────────────────────────────────────────────────────────
# build_index
# ─────────────────────────────────────────────────────────────


class TestBuildIndex:
    def test_empty_entities_returns_empty_dict(self):
        assert build_index([]) == {}

    def test_single_entity_indexed_by_text(self):
        e = _make_entity("FV101")
        result = build_index([e])
        assert "FV101" in result
        assert result["FV101"] is e

    def test_duplicate_text_first_occurrence_wins(self):
        e1 = _make_entity("FV101", x=0.0)
        e2 = _make_entity("FV101", x=99.0)
        result = build_index([e1, e2])
        assert result["FV101"] is e1

    def test_multiple_unique_entities_all_indexed(self):
        entities = [_make_entity("FV101"), _make_entity("HV201"), _make_entity("TCV301")]
        result = build_index(entities)
        assert set(result.keys()) == {"FV101", "HV201", "TCV301"}

    def test_whitespace_stripped_from_key(self):
        e = _make_entity("  FV101  ")
        result = build_index([e])
        assert "FV101" in result


# ─────────────────────────────────────────────────────────────
# build_clusters
# ─────────────────────────────────────────────────────────────


def _nearby_pair(text_top="FV", text_bot="501", h=2.5, x=10.0, gap=1.5):
    """Two entities separated by gap × h vertically (within default cluster threshold)."""
    top = _make_entity(text_top, x=x, y=50.0, height=h)
    bot = _make_entity(text_bot, x=x, y=50.0 - gap * h, height=h)
    return top, bot


class TestBuildClusters:
    def test_empty_returns_empty(self):
        assert build_clusters([]) == []

    def test_single_entity_no_cluster(self):
        assert build_clusters([_make_entity("FV101")]) == []

    def test_nearby_vertical_entities_cluster(self):
        top, bot = _nearby_pair()
        clusters = build_clusters([top, bot])
        assert len(clusters) == 1
        assert len(clusters[0]) == 2

    def test_distant_entities_no_cluster(self):
        # Separate by ~10 × height — far outside default gap_factor=3.5
        top = _make_entity("FV", x=10.0, y=50.0, height=2.5)
        bot = _make_entity("501", x=10.0, y=25.0, height=2.5)
        assert build_clusters([top, bot]) == []

    def test_cluster_reading_order_top_first(self):
        top, bot = _nearby_pair()
        clusters = build_clusters([bot, top])  # supply in reversed order
        assert clusters[0][0]["text"] == "FV"  # top entity first


# ─────────────────────────────────────────────────────────────
# build_cluster_index
# ─────────────────────────────────────────────────────────────


class TestBuildClusterIndex:
    def _pair(self, top_text, bot_text, h=2.5, gap=1.5):
        return [
            _make_entity(top_text, x=10.0, y=50.0, height=h),
            _make_entity(bot_text, x=10.0, y=50.0 - gap * h, height=h),
        ]

    def test_simple_pair_no_separator(self):
        entities = self._pair("FV", "501")
        idx = build_cluster_index(entities)
        assert "FV501" in idx

    def test_simple_pair_space_separator(self):
        entities = self._pair("FV", "501")
        idx = build_cluster_index(entities)
        assert "FV 501" in idx

    def test_inverted_t_variants(self):
        top = _make_entity("FV", x=10.0, y=50.0, height=2.5)
        b1 = _make_entity("12", x=7.0, y=46.25, height=2.5)
        b2 = _make_entity("54", x=13.0, y=46.25, height=2.5)
        idx = build_cluster_index([top, b1, b2])
        assert "FV12" in idx
        assert "FV 12" in idx
        assert "FV54" in idx
        assert "FV 54" in idx

    def test_case_insensitive_key(self):
        entities = self._pair("fv", "501")
        idx = build_cluster_index(entities)
        assert "FV501" in idx

    def test_isolated_entities_not_indexed(self):
        e1 = _make_entity("AA", x=0.0, y=0.0, height=2.5)
        e2 = _make_entity("BB", x=100.0, y=50.0, height=2.5)
        assert build_cluster_index([e1, e2]) == {}


class TestClusterRows:
    def test_two_distinct_rows_returns_tokens(self):
        top = _make_entity("FV", x=10.0, y=50.0, height=2.5)
        bot = _make_entity("501", x=10.0, y=46.25, height=2.5)
        rows = _cluster_rows([top, bot])
        assert rows is not None
        top_tokens, bot_tokens = rows
        assert top_tokens == ["FV"]
        assert bot_tokens == ["501"]

    def test_single_row_returns_none(self):
        # All entities at the same Y → no second row → None
        e1 = _make_entity("FV", x=5.0, y=50.0, height=2.5)
        e2 = _make_entity("501", x=15.0, y=50.0, height=2.5)
        assert _cluster_rows([e1, e2]) is None


class TestInvertedTVariants:
    def test_three_entity_inverted_t(self):
        top = _make_entity("FV", x=10.0, y=50.0, height=2.5)
        b1 = _make_entity("12", x=7.0, y=46.25, height=2.5)
        b2 = _make_entity("54", x=13.0, y=46.25, height=2.5)
        variants = _inverted_t_variants([top, b1, b2])
        assert "FV12" in variants
        assert "FV54" in variants

    def test_single_row_cluster_returns_empty(self):
        e1 = _make_entity("FV", x=5.0, y=50.0, height=2.5)
        e2 = _make_entity("501", x=15.0, y=50.0, height=2.5)
        e3 = _make_entity("XYZ", x=25.0, y=50.0, height=2.5)
        assert _inverted_t_variants([e1, e2, e3]) == set()

    def test_two_top_one_bottom_returns_empty(self):
        t1 = _make_entity("FV", x=5.0, y=50.0, height=2.5)
        t2 = _make_entity("HV", x=15.0, y=50.0, height=2.5)
        bot = _make_entity("501", x=10.0, y=46.25, height=2.5)
        assert _inverted_t_variants([t1, t2, bot]) == set()


# ─────────────────────────────────────────────────────────────
# build_hitboxes
# ─────────────────────────────────────────────────────────────


class TestBuildHitboxes:
    def test_empty_labels_returns_empty_list(self, minimal_tile_meta):
        tf = _make_tf(minimal_tile_meta)
        assert build_hitboxes([], {}, tf) == []

    def test_found_label_in_output(self, minimal_tile_meta):
        tf = _make_tf(minimal_tile_meta)
        idx = build_index([_make_entity("FV101", x=50.0, y=50.0, height=2.5)])
        result = build_hitboxes(["FV101"], idx, tf)
        assert len(result) == 1
        assert result[0]["label"] == "FV101"
        assert result[0]["found"] is True

    def test_not_found_label_excluded(self, minimal_tile_meta):
        tf = _make_tf(minimal_tile_meta)
        assert build_hitboxes(["MISSING"], {}, tf) == []

    def test_bbox_present_with_corners(self, minimal_tile_meta):
        tf = _make_tf(minimal_tile_meta)
        idx = build_index([_make_entity("FV101", x=50.0, y=50.0, height=2.5)])
        result = build_hitboxes(["FV101"], idx, tf)
        bbox = result[0]["bbox"]
        assert bbox is not None
        assert "corners" in bbox
        assert len(bbox["corners"]) == 4

    def test_hitbox_record_has_exactly_required_keys(self, minimal_tile_meta):
        tf = _make_tf(minimal_tile_meta)
        idx = build_index([_make_entity("FV101", x=50.0, y=50.0, height=2.5)])
        record = build_hitboxes(["FV101"], idx, tf)[0]
        assert set(record.keys()) == {"label", "found", "clustered", "bbox"}

    def test_mixed_found_and_not_found(self, minimal_tile_meta):
        tf = _make_tf(minimal_tile_meta)
        idx = build_index([_make_entity("FV101", x=50.0, y=50.0, height=2.5)])
        result = build_hitboxes(["FV101", "MISSING"], idx, tf)
        labels_out = [r["label"] for r in result]
        assert "FV101" in labels_out
        assert "MISSING" not in labels_out

    def test_label_whitespace_stripped_for_lookup(self, minimal_tile_meta):
        tf = _make_tf(minimal_tile_meta)
        idx = build_index([_make_entity("FV101", x=50.0, y=50.0, height=2.5)])
        result = build_hitboxes(["  FV101  "], idx, tf)
        assert len(result) == 1

    def test_exact_match_clustered_is_false(self, minimal_tile_meta):
        tf = _make_tf(minimal_tile_meta)
        idx = build_index([_make_entity("FV101", x=50.0, y=50.0, height=2.5)])
        result = build_hitboxes(["FV101"], idx, tf)
        assert result[0]["clustered"] is False

    def test_cluster_match_found(self, minimal_tile_meta):
        tf = _make_tf(minimal_tile_meta)
        top = _make_entity("FV", x=10.0, y=50.0, height=2.5)
        bot = _make_entity("501", x=10.0, y=46.25, height=2.5)
        ci = build_cluster_index([top, bot])
        result = build_hitboxes(["FV501"], {}, tf, cluster_index=ci)
        assert len(result) == 1
        assert result[0]["found"] is True

    def test_cluster_match_has_clustered_true(self, minimal_tile_meta):
        tf = _make_tf(minimal_tile_meta)
        top = _make_entity("FV", x=10.0, y=50.0, height=2.5)
        bot = _make_entity("501", x=10.0, y=46.25, height=2.5)
        ci = build_cluster_index([top, bot])
        result = build_hitboxes(["FV501"], {}, tf, cluster_index=ci)
        assert result[0]["clustered"] is True

    def test_cluster_match_bbox_spans_both_entities(self, minimal_tile_meta):
        tf = _make_tf(minimal_tile_meta)
        top = _make_entity("FV", x=10.0, y=50.0, height=2.5)
        bot = _make_entity("501", x=10.0, y=46.25, height=2.5)
        ci = build_cluster_index([top, bot])
        result = build_hitboxes(["FV501"], {}, tf, cluster_index=ci)
        bbox = result[0]["bbox"]
        assert bbox is not None
        lats = [c["lat"] for c in bbox["corners"]]
        top_bbox = compute_bbox(top, tf)
        bot_bbox = compute_bbox(bot, tf)
        assert top_bbox is not None and bot_bbox is not None
        top_lats = [c["lat"] for c in top_bbox["corners"]]
        bot_lats = [c["lat"] for c in bot_bbox["corners"]]
        assert min(lats) <= min(top_lats + bot_lats)
        assert max(lats) >= max(top_lats + bot_lats)


# ─────────────────────────────────────────────────────────────
# load_labels
# ─────────────────────────────────────────────────────────────


class TestLoadLabels:
    def test_loads_simple_labels(self, tmp_path):
        p = tmp_path / "labels.txt"
        p.write_text("FV101\nHV201\nTCV301\n", encoding="utf-8")
        assert load_labels(str(p)) == ["FV101", "HV201", "TCV301"]

    def test_strips_whitespace(self, tmp_path):
        p = tmp_path / "labels.txt"
        p.write_text("  FV101  \n  HV201  \n", encoding="utf-8")
        assert load_labels(str(p)) == ["FV101", "HV201"]

    def test_blank_lines_excluded(self, tmp_path):
        p = tmp_path / "labels.txt"
        p.write_text("FV101\n\n\nHV201\n", encoding="utf-8")
        assert load_labels(str(p)) == ["FV101", "HV201"]

    def test_comment_lines_excluded(self, tmp_path):
        p = tmp_path / "labels.txt"
        p.write_text("# comment\nFV101\n# another\nHV201\n", encoding="utf-8")
        assert load_labels(str(p)) == ["FV101", "HV201"]

    def test_empty_file_returns_empty_list(self, tmp_path):
        p = tmp_path / "labels.txt"
        p.write_text("", encoding="utf-8")
        assert load_labels(str(p)) == []


# ─────────────────────────────────────────────────────────────
# parse_args
# ─────────────────────────────────────────────────────────────


class TestParseArgs:
    def test_required_args_parsed(self):
        args = parse_args(["--dxf", "a.dxf", "--labels", "b.txt", "--tile-meta", "meta.json"])
        assert args.dxf == "a.dxf"
        assert args.labels == "b.txt"
        assert args.tile_meta == "meta.json"

    def test_defaults(self):
        args = parse_args(["--dxf", "a.dxf", "--labels", "b.txt", "--tile-meta", "meta.json"])
        assert args.out == "hitboxes.json"
        assert args.verbose is False
        assert args.cluster_gap == 3.5
        assert args.h_tolerance == 2.5

    def test_optional_args(self):
        args = parse_args(
            [
                "--dxf", "a.dxf",
                "--labels", "b.txt",
                "--tile-meta", "meta.json",
                "--out", "out/hb.json",
                "--verbose",
            ]
        )
        assert args.tile_meta == "meta.json"
        assert args.out == "out/hb.json"
        assert args.verbose is True

    def test_cluster_gap_arg(self):
        args = parse_args(
            ["--dxf", "a.dxf", "--labels", "b.txt", "--tile-meta", "meta.json",
             "--cluster-gap", "5.0"]
        )
        assert args.cluster_gap == pytest.approx(5.0)

    def test_h_tolerance_arg(self):
        args = parse_args(
            ["--dxf", "a.dxf", "--labels", "b.txt", "--tile-meta", "meta.json",
             "--h-tolerance", "1.0"]
        )
        assert args.h_tolerance == pytest.approx(1.0)

    def test_missing_dxf_exits(self):
        with pytest.raises(SystemExit):
            parse_args(["--labels", "b.txt"])

    def test_missing_labels_exits(self):
        with pytest.raises(SystemExit):
            parse_args(["--dxf", "a.dxf"])


# ─────────────────────────────────────────────────────────────
# main  (integration)
# ─────────────────────────────────────────────────────────────


class TestMain:
    def test_with_tile_meta_populates_coords(self, minimal_dxf, tmp_path, minimal_tile_meta):
        labels_path = tmp_path / "labels.txt"
        labels_path.write_text("FV101\nHV201\nMISSING\n", encoding="utf-8")
        meta_path = tmp_path / "tile_meta.json"
        meta_path.write_text(json.dumps(minimal_tile_meta), encoding="utf-8")
        out_path = tmp_path / "hitboxes.json"

        main(
            [
                "--dxf", str(minimal_dxf),
                "--labels", str(labels_path),
                "--tile-meta", str(meta_path),
                "--out", str(out_path),
            ]
        )

        data = json.loads(out_path.read_text())
        labels_out = [r["label"] for r in data]
        assert "FV101" in labels_out
        assert "HV201" in labels_out
        assert "MISSING" not in labels_out
        assert data[0]["bbox"] is not None

    def test_output_record_has_no_leaflet_key(self, minimal_dxf, tmp_path, minimal_tile_meta):
        labels_path = tmp_path / "labels.txt"
        labels_path.write_text("FV101\n", encoding="utf-8")
        meta_path = tmp_path / "tile_meta.json"
        meta_path.write_text(json.dumps(minimal_tile_meta), encoding="utf-8")
        out_path = tmp_path / "hitboxes.json"

        main(
            [
                "--dxf", str(minimal_dxf),
                "--labels", str(labels_path),
                "--tile-meta", str(meta_path),
                "--out", str(out_path),
            ]
        )

        data = json.loads(out_path.read_text())
        assert len(data) == 1
        assert "leaflet" not in data[0]
        assert "corners" in data[0]["bbox"]

    def test_creates_output_directory(self, minimal_dxf, tmp_path, minimal_tile_meta):
        labels_path = tmp_path / "labels.txt"
        labels_path.write_text("FV101\n", encoding="utf-8")
        meta_path = tmp_path / "tile_meta.json"
        meta_path.write_text(json.dumps(minimal_tile_meta), encoding="utf-8")
        out_path = tmp_path / "sub" / "dir" / "hitboxes.json"

        main(
            [
                "--dxf", str(minimal_dxf),
                "--labels", str(labels_path),
                "--tile-meta", str(meta_path),
                "--out", str(out_path),
            ]
        )

        assert out_path.exists()

    def test_verbose_flag_accepted(self, minimal_dxf, tmp_path, minimal_tile_meta):
        labels_path = tmp_path / "labels.txt"
        labels_path.write_text("FV101\n", encoding="utf-8")
        meta_path = tmp_path / "tile_meta.json"
        meta_path.write_text(json.dumps(minimal_tile_meta), encoding="utf-8")
        out_path = tmp_path / "hitboxes.json"

        main(
            [
                "--dxf", str(minimal_dxf),
                "--labels", str(labels_path),
                "--tile-meta", str(meta_path),
                "--out", str(out_path),
                "--verbose",
            ]
        )
        # No exception = pass

    def test_cluster_labels_resolved(self, tmp_path, minimal_tile_meta):
        import ezdxf

        doc = ezdxf.new(dxfversion="R2010")
        msp = doc.modelspace()
        msp.add_text("FV", dxfattribs={"insert": (10.0, 25.0), "height": 2.5})
        msp.add_text("101", dxfattribs={"insert": (10.0, 20.0), "height": 2.5})
        msp.add_lwpolyline([(0, 0), (50, 0), (50, 50), (0, 50)], dxfattribs={"closed": True})
        dxf_path = tmp_path / "cluster.dxf"
        doc.saveas(str(dxf_path))

        labels_path = tmp_path / "labels.txt"
        labels_path.write_text("FV101\n", encoding="utf-8")
        meta_path = tmp_path / "tile_meta.json"
        meta_path.write_text(json.dumps(minimal_tile_meta), encoding="utf-8")
        out_path = tmp_path / "hitboxes.json"

        main(
            [
                "--dxf", str(dxf_path),
                "--labels", str(labels_path),
                "--tile-meta", str(meta_path),
                "--out", str(out_path),
            ]
        )

        data = json.loads(out_path.read_text())
        assert len(data) == 1
        assert data[0]["label"] == "FV101"
        assert data[0]["found"] is True
        assert data[0]["clustered"] is True
