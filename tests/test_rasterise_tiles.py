"""
Unit and integration tests for rasterise_tiles.py (Stage 2).

Unit tests have no cairosvg dependency and run in milliseconds.
Integration tests are marked @pytest.mark.integration and require cairosvg.
"""

import json
from unittest.mock import patch

import pytest
import rasterise_tiles

REQUIRED_META_KEYS = {
    "max_zoom",
    "tile_size",
    "full_width_px",
    "full_height_px",
    "leaflet_bounds",
}


# ─────────────────────────────────────────────────────────────
# _read_svg_viewbox
# ─────────────────────────────────────────────────────────────


class TestReadSvgViewbox:
    def test_viewbox_four_floats(self, simple_svg):
        w, h = rasterise_tiles._read_svg_viewbox(str(simple_svg))
        assert w == pytest.approx(200.0)
        assert h == pytest.approx(100.0)

    def test_fallback_to_width_height_attrs(self, tmp_path):
        # SVG with no viewBox but explicit width/height attributes
        svg = '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="150"><rect/></svg>'
        p = tmp_path / "no_vb.svg"
        p.write_text(svg, encoding="utf-8")
        w, h = rasterise_tiles._read_svg_viewbox(str(p))
        assert w == pytest.approx(300.0)
        assert h == pytest.approx(150.0)

    def test_no_viewbox_raises_value_error(self, tmp_path):
        # SVG with neither viewBox nor width/height
        svg = '<svg xmlns="http://www.w3.org/2000/svg"><rect/></svg>'
        p = tmp_path / "bad.svg"
        p.write_text(svg, encoding="utf-8")
        with pytest.raises(ValueError):
            rasterise_tiles._read_svg_viewbox(str(p))

    def test_reads_only_first_4096_bytes(self, tmp_path):
        # viewBox buried after the 4 KB read limit should NOT be found
        padding = " " * 5000
        svg = f'<svg xmlns="http://www.w3.org/2000/svg">{padding}viewBox="0 0 999 999"></svg>'
        p = tmp_path / "buried.svg"
        p.write_text(svg, encoding="utf-8")
        with pytest.raises(ValueError):
            rasterise_tiles._read_svg_viewbox(str(p))


# ─────────────────────────────────────────────────────────────
# _png_to_image
# ─────────────────────────────────────────────────────────────


class TestPngToImage:
    def test_returns_rgba_image(self, minimal_png):
        from PIL import Image

        data = minimal_png.read_bytes()
        img = rasterise_tiles._png_to_image(data)
        assert img.mode == "RGBA"
        assert isinstance(img, Image.Image)


# ─────────────────────────────────────────────────────────────
# _generate_tiles
# ─────────────────────────────────────────────────────────────

# Expected tile counts for the 512×256 minimal_png fixture.
# short = min(512,256) = 256
# z=0: cols=ceil(512*1/256)=2, rows=ceil(256*1/256)=1 → 2
# z=1: cols=4, rows=2 → 8
# z=2: cols=8, rows=4 → 32


class TestGenerateTiles:
    def test_tile_count_zoom_0(self, tmp_path, minimal_png):
        from PIL import Image

        out_dir = tmp_path / "tiles"
        img = Image.open(str(minimal_png)).convert("RGBA")
        rasterise_tiles._generate_tiles(img, out_dir, max_zoom=0, tile_sz=256)
        tiles = list(out_dir.rglob("*.webp"))
        assert len(tiles) == 2

    def test_tile_count_max_zoom_2(self, tmp_path, minimal_png):
        from PIL import Image

        out_dir = tmp_path / "tiles"
        img = Image.open(str(minimal_png)).convert("RGBA")
        rasterise_tiles._generate_tiles(img, out_dir, max_zoom=2, tile_sz=256)
        tiles = list(out_dir.rglob("*.webp"))
        assert len(tiles) == 42  # 2 + 8 + 32

    def test_directory_structure(self, tmp_path, minimal_png):
        from PIL import Image

        out_dir = tmp_path / "tiles"
        img = Image.open(str(minimal_png)).convert("RGBA")
        rasterise_tiles._generate_tiles(img, out_dir, max_zoom=1, tile_sz=256)
        # z/x/y.webp convention
        assert (out_dir / "0" / "0" / "0.webp").exists()
        assert (out_dir / "1" / "0" / "0.webp").exists()

    def test_all_tiles_are_256x256(self, tmp_path, minimal_png):
        from PIL import Image

        out_dir = tmp_path / "tiles"
        img = Image.open(str(minimal_png)).convert("RGBA")
        rasterise_tiles._generate_tiles(img, out_dir, max_zoom=1, tile_sz=256)
        for tile in out_dir.rglob("*.webp"):
            opened = Image.open(str(tile))
            assert opened.size == (256, 256), f"Bad size: {tile} → {opened.size}"


# ─────────────────────────────────────────────────────────────
# parse_args
# ─────────────────────────────────────────────────────────────


class TestParseArgs:
    def test_defaults(self):
        args = rasterise_tiles.parse_args(["--svg", "x.svg"])
        assert args.svg == "x.svg"
        assert args.max_zoom == rasterise_tiles.DEFAULT_MAX_ZOOM
        assert args.tile_size == rasterise_tiles.TILE_SIZE
        assert args.tiles_dir == "tiles"
        assert args.tile_meta == "tile_meta.json"

    def test_custom_values(self):
        args = rasterise_tiles.parse_args(
            [
                "--svg",
                "my.svg",
                "--max-zoom",
                "3",
                "--tiles-dir",
                "out/tiles",
                "--tile-meta",
                "out/meta.json",
                "--tile-size",
                "512",
            ]
        )
        assert args.svg == "my.svg"
        assert args.max_zoom == 3
        assert args.tiles_dir == "out/tiles"
        assert args.tile_meta == "out/meta.json"
        assert args.tile_size == 512


# ─────────────────────────────────────────────────────────────
# tile_meta fields and coordinate bounds
# ─────────────────────────────────────────────────────────────


class TestTileMetaFields:
    def test_all_required_fields_present(self, minimal_tile_meta):
        for key in REQUIRED_META_KEYS:
            assert key in minimal_tile_meta, f"Missing key: {key}"

    def test_max_zoom_is_int(self, minimal_tile_meta):
        assert isinstance(minimal_tile_meta["max_zoom"], int)

    def test_leaflet_bounds_is_nested_list(self, minimal_tile_meta):
        bounds = minimal_tile_meta["leaflet_bounds"]
        assert isinstance(bounds, list)
        assert len(bounds) == 2
        assert len(bounds[0]) == 2
        assert len(bounds[1]) == 2

    def test_coordinate_bounds_formula(self):
        # Given: 1024×512 image, tile_sz=256
        # short_px = 512; leaflet_w = 1024*256/512 = 512; leaflet_h = 256
        full_w, full_h, tile_sz = 1024, 512, 256
        short_px = min(full_w, full_h)
        leaflet_w = round(full_w * tile_sz / short_px, 4)
        leaflet_h = round(full_h * tile_sz / short_px, 4)
        bounds = [[-leaflet_h, 0], [0, leaflet_w]]

        assert leaflet_w == pytest.approx(512.0)
        assert leaflet_h == pytest.approx(256.0)
        assert bounds == [[-256.0, 0], [0, 512.0]]


# ─────────────────────────────────────────────────────────────
# main() — direct calls with mocked cairosvg
# ─────────────────────────────────────────────────────────────


@pytest.mark.integration
class TestMain:
    def test_main_single_strip(self, tmp_path, simple_svg, minimal_png):
        """main() single-strip path: n_strips == 1."""
        png_bytes = minimal_png.read_bytes()
        tiles_dir = tmp_path / "tiles"
        meta_path = tmp_path / "tile_meta.json"

        with patch("rasterise_tiles.cairosvg.svg2png", return_value=png_bytes):
            rasterise_tiles.main(
                [
                    "--svg",
                    str(simple_svg),
                    "--max-zoom",
                    "0",
                    "--tiles-dir",
                    str(tiles_dir),
                    "--tile-meta",
                    str(meta_path),
                ]
            )

        assert meta_path.exists()
        meta = json.loads(meta_path.read_text())
        for key in REQUIRED_META_KEYS:
            assert key in meta
        assert list(tiles_dir.rglob("*.webp"))

    def test_main_strip_rendering(self, tmp_path, simple_svg, minimal_png):
        """main() multi-strip path: forced by setting CAIRO_MAX_DIM to a small value."""
        png_bytes = minimal_png.read_bytes()
        tiles_dir = tmp_path / "tiles"
        meta_path = tmp_path / "tile_meta.json"

        with (
            patch.object(rasterise_tiles, "CAIRO_MAX_DIM", 256),
            patch("rasterise_tiles.cairosvg.svg2png", return_value=png_bytes),
        ):
            rasterise_tiles.main(
                [
                    "--svg",
                    str(simple_svg),
                    "--max-zoom",
                    "0",
                    "--tiles-dir",
                    str(tiles_dir),
                    "--tile-meta",
                    str(meta_path),
                ]
            )

        assert meta_path.exists()
        assert list(tiles_dir.rglob("*.webp"))

    def test_preserveaspectratio_none_injected(self, tmp_path, simple_svg, minimal_png):
        """main() injects preserveAspectRatio="none" into SVG bytes before rendering."""
        png_bytes = minimal_png.read_bytes()
        calls: list[bytes] = []

        def capture(**kwargs):
            calls.append(kwargs.get("bytestring", b""))
            return png_bytes

        with patch("rasterise_tiles.cairosvg.svg2png", side_effect=capture):
            rasterise_tiles.main(
                ["--svg", str(simple_svg), "--max-zoom", "0",
                 "--tiles-dir", str(tmp_path / "tiles"),
                 "--tile-meta", str(tmp_path / "tile_meta.json")]
            )

        assert calls
        for bs in calls:
            assert b'preserveAspectRatio="none"' in bs

    def test_existing_preserveaspectratio_replaced(self, tmp_path, minimal_png):
        """main() replaces an existing preserveAspectRatio value with "none"."""
        svg = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<svg xmlns="http://www.w3.org/2000/svg"'
            ' preserveAspectRatio="xMidYMid meet"'
            ' viewBox="0 0 200 100" width="200mm" height="100mm">\n'
            "  <rect/>\n</svg>\n"
        )
        svg_path = tmp_path / "par.svg"
        svg_path.write_text(svg, encoding="utf-8")
        png_bytes = minimal_png.read_bytes()
        calls: list[bytes] = []

        def capture(**kwargs):
            calls.append(kwargs.get("bytestring", b""))
            return png_bytes

        with patch("rasterise_tiles.cairosvg.svg2png", side_effect=capture):
            rasterise_tiles.main(
                ["--svg", str(svg_path), "--max-zoom", "0",
                 "--tiles-dir", str(tmp_path / "tiles"),
                 "--tile-meta", str(tmp_path / "tile_meta.json")]
            )

        assert calls
        for bs in calls:
            assert b'preserveAspectRatio="none"' in bs
            assert b'"xMidYMid meet"' not in bs

    def test_main_strip_no_viewbox_raises(self, tmp_path, minimal_png):
        """Strip path raises ValueError when SVG bytes have no viewBox attribute."""
        # SVG with width/height but no viewBox — passes _read_svg_viewbox but
        # the regex on bytes in the strip path finds nothing.
        svg_content = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="150">'
            '<rect x="0" y="0" width="300" height="150"/>'
            "</svg>"
        )
        svg_path = tmp_path / "no_vb.svg"
        svg_path.write_text(svg_content, encoding="utf-8")

        png_bytes = minimal_png.read_bytes()
        tiles_dir = tmp_path / "tiles"
        meta_path = tmp_path / "tile_meta.json"

        with (
            patch.object(rasterise_tiles, "CAIRO_MAX_DIM", 1),
            patch("rasterise_tiles.cairosvg.svg2png", return_value=png_bytes),
            pytest.raises(ValueError, match="viewBox attribute not found"),
        ):
            rasterise_tiles.main(
                [
                    "--svg",
                    str(svg_path),
                    "--max-zoom",
                    "0",
                    "--tiles-dir",
                    str(tiles_dir),
                    "--tile-meta",
                    str(meta_path),
                ]
            )
