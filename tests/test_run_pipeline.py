"""
Unit tests for run_pipeline.py (the three-stage pipeline runner).

All stage main() functions are mocked — this suite verifies argument
forwarding and control flow, not the behaviour of individual stages.
"""

import io
import json
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _run(args: list[str]) -> SimpleNamespace:
    import run_pipeline

    buf = io.StringIO()
    try:
        with redirect_stderr(buf):
            run_pipeline.main(args)
        return SimpleNamespace(returncode=0, stderr=buf.getvalue())
    except SystemExit as exc:
        return SimpleNamespace(
            returncode=exc.code if exc.code is not None else 1,
            stderr=buf.getvalue(),
        )


def _patch_stages():
    """Return a context-manager tuple that mocks all three stage mains."""
    return (
        patch("run_pipeline.render_svg.main"),
        patch("run_pipeline.rasterise_tiles.main"),
        patch("run_pipeline.extract_hitboxes.main"),
    )


# ─────────────────────────────────────────────────────────────
# Basic invocation
# ─────────────────────────────────────────────────────────────


class TestBasicInvocation:
    def test_all_stages_called(self, tmp_path, minimal_dxf):
        with (
            patch("run_pipeline.render_svg.main") as mock_render,
            patch("run_pipeline.rasterise_tiles.main") as mock_raster,
            patch("run_pipeline.extract_hitboxes.main") as mock_extract,
        ):
            import run_pipeline

            run_pipeline.main(["--dxf", str(minimal_dxf), "--labels", "labels.txt"])

        assert mock_render.called
        assert mock_raster.called
        assert mock_extract.called

    def test_stages_called_in_order(self, tmp_path, minimal_dxf):
        call_order: list[str] = []
        with (
            patch(
                "run_pipeline.render_svg.main", side_effect=lambda _: call_order.append("render")
            ),
            patch(
                "run_pipeline.rasterise_tiles.main",
                side_effect=lambda _: call_order.append("raster"),
            ),
            patch(
                "run_pipeline.extract_hitboxes.main",
                side_effect=lambda _: call_order.append("extract"),
            ),
        ):
            import run_pipeline

            run_pipeline.main(["--dxf", str(minimal_dxf), "--labels", "labels.txt"])

        assert call_order == ["render", "raster", "extract"]

    def test_missing_labels_exits_nonzero(self, tmp_path, minimal_dxf):
        result = _run(["--dxf", str(minimal_dxf)])
        assert result.returncode != 0

    def test_missing_dxf_exits_nonzero(self, tmp_path):
        result = _run(["--labels", "labels.txt"])
        assert result.returncode != 0


# ─────────────────────────────────────────────────────────────
# Output directory
# ─────────────────────────────────────────────────────────────


class TestOutputDirectory:
    def test_default_out_dir_is_dxf_parent(self, tmp_path, minimal_dxf):
        with (
            patch("run_pipeline.render_svg.main") as mock_render,
            patch("run_pipeline.rasterise_tiles.main"),
            patch("run_pipeline.extract_hitboxes.main"),
        ):
            import run_pipeline

            run_pipeline.main(["--dxf", str(minimal_dxf), "--labels", "labels.txt"])

        render_argv = mock_render.call_args[0][0]
        svg_out = render_argv[1]
        assert svg_out.startswith(str(minimal_dxf.parent))

    def test_explicit_out_dir_used_for_all_outputs(self, tmp_path, minimal_dxf):
        out = tmp_path / "output"
        with (
            patch("run_pipeline.render_svg.main") as mock_render,
            patch("run_pipeline.rasterise_tiles.main") as mock_raster,
            patch("run_pipeline.extract_hitboxes.main") as mock_extract,
        ):
            import run_pipeline

            run_pipeline.main(
                [
                    "--dxf",
                    str(minimal_dxf),
                    "--labels",
                    "labels.txt",
                    "--out-dir",
                    str(out),
                ]
            )

        render_argv = mock_render.call_args[0][0]
        assert render_argv[1].startswith(str(out))

        raster_argv = mock_raster.call_args[0][0]
        tiles_dir = raster_argv[raster_argv.index("--tiles-dir") + 1]
        assert tiles_dir.startswith(str(out))

        extract_argv = mock_extract.call_args[0][0]
        hitboxes_out = extract_argv[extract_argv.index("--out") + 1]
        assert hitboxes_out.startswith(str(out))

    def test_out_dir_created_if_missing(self, tmp_path, minimal_dxf):
        new_dir = tmp_path / "deep" / "nested"
        with (
            patch("run_pipeline.render_svg.main"),
            patch("run_pipeline.rasterise_tiles.main"),
            patch("run_pipeline.extract_hitboxes.main"),
        ):
            import run_pipeline

            run_pipeline.main(
                [
                    "--dxf",
                    str(minimal_dxf),
                    "--labels",
                    "labels.txt",
                    "--out-dir",
                    str(new_dir),
                ]
            )

        assert new_dir.exists()


# ─────────────────────────────────────────────────────────────
# Argument forwarding
# ─────────────────────────────────────────────────────────────


class TestArgumentForwarding:
    def test_max_zoom_forwarded_to_rasterise(self, tmp_path, minimal_dxf):
        with (
            patch("run_pipeline.render_svg.main"),
            patch("run_pipeline.rasterise_tiles.main") as mock_raster,
            patch("run_pipeline.extract_hitboxes.main"),
        ):
            import run_pipeline

            run_pipeline.main(
                ["--dxf", str(minimal_dxf), "--labels", "labels.txt", "--max-zoom", "7"]
            )

        raster_argv = mock_raster.call_args[0][0]
        assert "--max-zoom" in raster_argv
        assert raster_argv[raster_argv.index("--max-zoom") + 1] == "7"

    def test_verbose_forwarded_to_extract(self, tmp_path, minimal_dxf):
        with (
            patch("run_pipeline.render_svg.main"),
            patch("run_pipeline.rasterise_tiles.main"),
            patch("run_pipeline.extract_hitboxes.main") as mock_extract,
        ):
            import run_pipeline

            run_pipeline.main(["--dxf", str(minimal_dxf), "--labels", "labels.txt", "--verbose"])

        extract_argv = mock_extract.call_args[0][0]
        assert "--verbose" in extract_argv

    def test_verbose_absent_by_default(self, tmp_path, minimal_dxf):
        with (
            patch("run_pipeline.render_svg.main"),
            patch("run_pipeline.rasterise_tiles.main"),
            patch("run_pipeline.extract_hitboxes.main") as mock_extract,
        ):
            import run_pipeline

            run_pipeline.main(["--dxf", str(minimal_dxf), "--labels", "labels.txt"])

        extract_argv = mock_extract.call_args[0][0]
        assert "--verbose" not in extract_argv

    def test_cluster_gap_forwarded(self, tmp_path, minimal_dxf):
        with (
            patch("run_pipeline.render_svg.main"),
            patch("run_pipeline.rasterise_tiles.main"),
            patch("run_pipeline.extract_hitboxes.main") as mock_extract,
        ):
            import run_pipeline

            run_pipeline.main(
                ["--dxf", str(minimal_dxf), "--labels", "labels.txt", "--cluster-gap", "6.0"]
            )

        extract_argv = mock_extract.call_args[0][0]
        assert "--cluster-gap" in extract_argv
        assert extract_argv[extract_argv.index("--cluster-gap") + 1] == "6.0"

    def test_h_tolerance_forwarded(self, tmp_path, minimal_dxf):
        with (
            patch("run_pipeline.render_svg.main"),
            patch("run_pipeline.rasterise_tiles.main"),
            patch("run_pipeline.extract_hitboxes.main") as mock_extract,
        ):
            import run_pipeline

            run_pipeline.main(
                ["--dxf", str(minimal_dxf), "--labels", "labels.txt", "--h-tolerance", "4.0"]
            )

        extract_argv = mock_extract.call_args[0][0]
        assert "--h-tolerance" in extract_argv
        assert extract_argv[extract_argv.index("--h-tolerance") + 1] == "4.0"

    def test_labels_forwarded_to_extract(self, tmp_path, minimal_dxf):
        with (
            patch("run_pipeline.render_svg.main"),
            patch("run_pipeline.rasterise_tiles.main"),
            patch("run_pipeline.extract_hitboxes.main") as mock_extract,
        ):
            import run_pipeline

            run_pipeline.main(["--dxf", str(minimal_dxf), "--labels", "my_labels.txt"])

        extract_argv = mock_extract.call_args[0][0]
        assert "--labels" in extract_argv
        assert extract_argv[extract_argv.index("--labels") + 1] == "my_labels.txt"


# ─────────────────────────────────────────────────────────────
# Themes config
# ─────────────────────────────────────────────────────────────


class TestThemesConfig:
    def _make_themes_file(self, tmp_path: Path, themes: dict) -> Path:
        p = tmp_path / "themes.json"
        p.write_text(json.dumps(themes), encoding="utf-8")
        return p

    def test_themes_config_forwarded_to_render(self, tmp_path, minimal_dxf):
        cfg = self._make_themes_file(tmp_path, {"light": {"background": "#fff"}})
        with (
            patch("run_pipeline.render_svg.main") as mock_render,
            patch("run_pipeline.rasterise_tiles.main"),
            patch("run_pipeline.extract_hitboxes.main"),
        ):
            import run_pipeline

            run_pipeline.main(
                [
                    "--dxf",
                    str(minimal_dxf),
                    "--labels",
                    "labels.txt",
                    "--themes-config",
                    str(cfg),
                ]
            )

        render_argv = mock_render.call_args[0][0]
        assert "--themes-config" in render_argv
        assert render_argv[render_argv.index("--themes-config") + 1] == str(cfg)

    def test_first_theme_svg_used_for_rasterise(self, tmp_path, minimal_dxf):
        cfg = self._make_themes_file(
            tmp_path,
            {"_comment": "skip", "light": {"background": "#fff"}, "dark": {"background": "#000"}},
        )
        with (
            patch("run_pipeline.render_svg.main"),
            patch("run_pipeline.rasterise_tiles.main") as mock_raster,
            patch("run_pipeline.extract_hitboxes.main"),
        ):
            import run_pipeline

            run_pipeline.main(
                [
                    "--dxf",
                    str(minimal_dxf),
                    "--labels",
                    "labels.txt",
                    "--themes-config",
                    str(cfg),
                ]
            )

        raster_argv = mock_raster.call_args[0][0]
        svg_for_tiles = raster_argv[raster_argv.index("--svg") + 1]
        # "light" is the first non-_ key
        assert "_light." in svg_for_tiles

    def test_no_themes_config_uses_plain_svg(self, tmp_path, minimal_dxf):
        with (
            patch("run_pipeline.render_svg.main"),
            patch("run_pipeline.rasterise_tiles.main") as mock_raster,
            patch("run_pipeline.extract_hitboxes.main"),
        ):
            import run_pipeline

            run_pipeline.main(["--dxf", str(minimal_dxf), "--labels", "labels.txt"])

        raster_argv = mock_raster.call_args[0][0]
        svg_for_tiles = raster_argv[raster_argv.index("--svg") + 1]
        # Should be the plain stem.svg, not a theme variant
        assert "_" not in Path(svg_for_tiles).stem
