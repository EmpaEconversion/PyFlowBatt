"""Tests for pyflowbatt.toml configuration and technique file classification."""

import json
import logging
import re
import zipfile
from pathlib import Path

import pytest

from pyflowbatt import config as config_module
from pyflowbatt.config import PyFlowBattConfig, classify_technique_files
from pyflowbatt.rocrate_output import _classify_inputs
from pyflowbatt.version import __version__

# Synthetic filename scheme mirroring real EC-Lab exports: <prefix>_<num>_<TECHNIQUE>_<tail>.<ext>
# Sizes are included as the largest GCPL is used as cycling.
SYNTHETIC_FILES: dict[str, int] = {
    "sample_01_OCV_A": 10,
    "sample_02_PEIS_A": 10,
    "sample_03_LSV_A": 10,
    "sample_04_GCPL_A": 10,
    "sample_05_PEIS_B": 10,
    "sample_06_GCPL_B": 20,
    "sample_07_GCPL_C": 10,
    "sample_10_PEIS_C": 10,
    "sample_12_PEIS_D": 10,
    "sample_13_LSV_B": 10,
    "sample_CVApre_A": 10,
    "sample_CVApost_A": 10,
}


def _write_synthetic_sample(folder: Path, ext: str = ".mpr") -> Path:
    """Create a folder containing one synthetic file per technique, with the given extension."""
    folder.mkdir(parents=True, exist_ok=True)
    for name, size in SYNTHETIC_FILES.items():
        (folder / f"{name}{ext}").write_text("x" * size)
    return folder


def test_default_config_classifies_synthetic_files(tmp_path: Path) -> None:
    """With no pyflowbatt.toml anywhere, defaults match the built-in glob patterns."""
    sample = _write_synthetic_sample(tmp_path / "sample")
    config = PyFlowBattConfig.load(sample, home=tmp_path / "home")
    assert config.extensions == [".mpr"]

    classified = classify_technique_files(sample, config)

    assert classified["gcpl"][0].name == "sample_06_GCPL_B.mpr"  # largest by size
    assert classified["ocv"][0].name == "sample_01_OCV_A.mpr"

    # Numeric LSV pre/post split (default threshold is 8): 03 -> pre, 13 -> post.
    assert classified["lsv_pre"][0].name == "sample_03_LSV_A.mpr"
    assert classified["lsv_post"][0].name == "sample_13_LSV_B.mpr"

    assert classified["cv_pre"][0].name == "sample_CVApre_A.mpr"
    assert classified["cv_post"][0].name == "sample_CVApost_A.mpr"

    # 4 PEIS files, tagged in filename order: 02, 05, 10, 12.
    assert classified["eis_pre"][0].name == "sample_02_PEIS_A.mpr"
    assert classified["eis_pre-50%SOC"][0].name == "sample_05_PEIS_B.mpr"
    assert classified["eis_post-50%SOC"][0].name == "sample_10_PEIS_C.mpr"
    assert classified["eis_post"][0].name == "sample_12_PEIS_D.mpr"


def test_custom_extensions_override_changes_matches(tmp_path: Path) -> None:
    """A full `extensions` override in pyflowbatt.toml switches which files match."""
    sample = _write_synthetic_sample(tmp_path / "sample", ext=".mgr")
    (sample / "pyflowbatt.toml").write_text('extensions = [".mgr"]\n')

    # Without the toml, default extensions=[".mpr"] would find nothing.
    default_config = PyFlowBattConfig()
    assert classify_technique_files(sample, default_config) == {}

    config = PyFlowBattConfig.load(sample, home=tmp_path / "home")
    assert config.extensions == [".mgr"]

    classified = classify_technique_files(sample, config)
    assert classified["gcpl"][0].suffix == ".mgr"
    assert classified["ocv"][0].suffix == ".mgr"
    assert classified["lsv_pre"][0].suffix == ".mgr"


def test_extra_patterns_merge_not_replace(tmp_path: Path) -> None:
    """`extra_patterns` adds to the built-in patterns instead of replacing them."""
    sample = _write_synthetic_sample(tmp_path / "sample")
    (sample / "pyflowbatt.toml").write_text('[extra_patterns]\neis = ["*_GCPL_*"]\n')

    config = PyFlowBattConfig.load(sample, home=tmp_path / "home")
    assert config.eis_patterns == ["*_PEIS_*", "*_GCPL_*"]

    classified = classify_technique_files(sample, config)
    # Original PEIS match is still present as the first tag. Pulling GCPL files into the
    # EIS pool makes the layout match no known protocol, so tags are numbered.
    assert classified["eis_pre_1"][0].name == "sample_02_PEIS_A.mpr"
    # GCPL files now also show up under the later EIS tags (merged, not replaced).
    eis_names = [paths[0].name for label, paths in classified.items() if label.startswith("eis_")]
    assert any("_GCPL_" in name for name in eis_names)


def _write_two_eis_sample(folder: Path) -> Path:
    """Early-protocol layout: EIS before cycling, main GCPL, EIS after cycling."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("s_01_OCV_A", "s_02_PEIS_A", "s_03_LSV_A", "s_05_GCPL_A", "s_06_PEIS_A"):
        (folder / f"{name}.mpr").write_text("x")
    return folder


def test_one_eis_each_side_tagged_pre_and_post(tmp_path: Path) -> None:
    """Early protocol: one EIS before and one after cycling are 0% SOC pre and post."""
    sample = _write_two_eis_sample(tmp_path / "sample")
    config = PyFlowBattConfig.load(sample, home=tmp_path / "home")

    classified = classify_technique_files(sample, config)

    eis = {k: v[0].name for k, v in classified.items() if k.startswith("eis_")}
    assert eis == {"eis_pre": "s_02_PEIS_A.mpr", "eis_post": "s_06_PEIS_A.mpr"}


def test_two_eis_each_side_keeps_soc_tags(tmp_path: Path) -> None:
    """Standard protocol: two EIS each side keep the 0%/50% SOC tags."""
    sample = _write_synthetic_sample(tmp_path / "sample")
    config = PyFlowBattConfig.load(sample, home=tmp_path / "home")

    classified = classify_technique_files(sample, config)

    # Main cycling is the largest GCPL, step 06; EIS 02 and 05 are before it, 10 and 12 after.
    assert classified["eis_pre"][0].name == "sample_02_PEIS_A.mpr"
    assert classified["eis_pre-50%SOC"][0].name == "sample_05_PEIS_B.mpr"
    assert classified["eis_post-50%SOC"][0].name == "sample_10_PEIS_C.mpr"
    assert classified["eis_post"][0].name == "sample_12_PEIS_D.mpr"


def test_unknown_eis_layout_gets_numbered_tags(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An EIS layout matching no known protocol warns and gets numbered pre/post tags."""
    sample = tmp_path / "sample"
    sample.mkdir()
    for name in ("s_01_PEIS_A", "s_02_PEIS_A", "s_03_PEIS_A", "s_05_GCPL_A", "s_06_PEIS_A"):
        (sample / f"{name}.mpr").write_text("x")
    config = PyFlowBattConfig.load(sample, home=tmp_path / "home")

    with caplog.at_level("WARNING"):
        classified = classify_technique_files(sample, config, warn=True)

    eis = {k: v[0].name for k, v in classified.items() if k.startswith("eis_")}
    assert eis == {
        "eis_pre_1": "s_01_PEIS_A.mpr",
        "eis_pre_2": "s_02_PEIS_A.mpr",
        "eis_pre_3": "s_03_PEIS_A.mpr",
        "eis_post_1": "s_06_PEIS_A.mpr",
    }
    assert "3 before and 1 after" in caplog.text


def test_unnumbered_filenames_split_by_acquisition_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without step numbers, EIS files are ordered and split by their recorded start time."""
    sample = tmp_path / "sample"
    sample.mkdir()
    for name in ("cell_PEIS_alpha", "cell_PEIS_omega", "cell_GCPL_main"):
        (sample / f"{name}.mpr").write_text("x")
    # Filename order puts "alpha" first, but it was measured after cycling.
    times = {"cell_PEIS_omega": 100.0, "cell_GCPL_main": 200.0, "cell_PEIS_alpha": 900.0}
    monkeypatch.setattr(config_module, "_start_time", lambda p: times[p.stem])
    config = PyFlowBattConfig.load(sample, home=tmp_path / "home")

    classified = classify_technique_files(sample, config)

    eis = {k: v[0].name for k, v in classified.items() if k.startswith("eis_")}
    assert eis == {"eis_pre": "cell_PEIS_omega.mpr", "eis_post": "cell_PEIS_alpha.mpr"}


def test_step_numbers_win_over_timestamps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Step numbers are used when present, so no file has to be parsed for a timestamp."""
    sample = _write_two_eis_sample(tmp_path / "sample")

    def fail(path: Path) -> float | None:
        msg = f"should not read timestamps from {path}"
        raise AssertionError(msg)

    monkeypatch.setattr(config_module, "_start_time", fail)
    config = PyFlowBattConfig.load(sample, home=tmp_path / "home")

    classified = classify_technique_files(sample, config)

    eis = {k: v[0].name for k, v in classified.items() if k.startswith("eis_")}
    assert eis == {"eis_pre": "s_02_PEIS_A.mpr", "eis_post": "s_06_PEIS_A.mpr"}


def test_unreadable_files_fall_back_to_filename_order(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With neither step numbers nor timestamps, EIS files keep filename-order tagging."""
    sample = tmp_path / "sample"
    sample.mkdir()
    for name in ("cell_PEIS_before", "cell_PEIS_after", "cell_GCPL_main"):
        (sample / f"{name}.mpr").write_text("x")
    monkeypatch.setattr(config_module, "_start_time", lambda _p: None)
    config = PyFlowBattConfig.load(sample, home=tmp_path / "home")

    with caplog.at_level("WARNING"):
        classified = classify_technique_files(sample, config, warn=True)

    eis = {k: v[0].name for k, v in classified.items() if k.startswith("eis_")}
    assert eis == {"eis_pre": "cell_PEIS_after.mpr", "eis_pre-50%SOC": "cell_PEIS_before.mpr"}
    assert "before/after cycling" in caplog.text


def test_eis_tagging_without_gcpl_falls_back_to_positional(tmp_path: Path) -> None:
    """With no cycling step to split around, EIS files keep filename-order tagging."""
    sample = tmp_path / "sample"
    sample.mkdir()
    for name in ("s_02_PEIS_A", "s_06_PEIS_A"):
        (sample / f"{name}.mpr").write_text("x")
    config = PyFlowBattConfig.load(sample, home=tmp_path / "home")

    classified = classify_technique_files(sample, config)

    eis = {k: v[0].name for k, v in classified.items() if k.startswith("eis_")}
    assert eis == {"eis_pre": "s_02_PEIS_A.mpr", "eis_pre-50%SOC": "s_06_PEIS_A.mpr"}


def test_eis_tags_pin_files_to_tags(tmp_path: Path) -> None:
    """eis_tags in pyflowbatt.toml assigns each EIS file to the given tag."""
    sample = _write_two_eis_sample(tmp_path / "sample")
    (sample / "pyflowbatt.toml").write_text(
        '[eis_tags]\n"pre" = ["*_02_PEIS_*"]\n"post" = ["*_06_PEIS_*"]\n'
    )
    config = PyFlowBattConfig.load(sample, home=tmp_path / "home")
    assert config.eis_tag_patterns == {"pre": ["*_02_PEIS_*"], "post": ["*_06_PEIS_*"]}

    classified = classify_technique_files(sample, config)

    eis = {k: v[0].name for k, v in classified.items() if k.startswith("eis_")}
    assert eis == {"eis_pre": "s_02_PEIS_A.mpr", "eis_post": "s_06_PEIS_A.mpr"}


def test_eis_tags_custom_names(tmp_path: Path) -> None:
    """Any filename-safe tag can be pinned, and unpinned files fill the standard tags."""
    sample = _write_two_eis_sample(tmp_path / "sample")
    (sample / "s_07_PEIS_A.mpr").write_text("x")
    (sample / "pyflowbatt.toml").write_text(
        '[eis_tags]\n"pre-20%SOC" = ["*_02_PEIS_*"]\n"post-80%SOC" = ["*_07_PEIS_*"]\n'
    )
    config = PyFlowBattConfig.load(sample, home=tmp_path / "home")

    classified = classify_technique_files(sample, config)

    eis = {k: v[0].name for k, v in classified.items() if k.startswith("eis_")}
    # Only one unpinned file is left, after cycling, which matches no standard layout
    assert eis == {
        "eis_pre-20%SOC": "s_02_PEIS_A.mpr",
        "eis_post-80%SOC": "s_07_PEIS_A.mpr",
        "eis_post_1": "s_06_PEIS_A.mpr",
    }


def test_eis_tags_custom_pin_keeps_standard_layout(tmp_path: Path) -> None:
    """Pinning an extra custom tag leaves the standard protocol tagging unchanged."""
    sample = _write_synthetic_sample(tmp_path / "sample")
    (sample / "sample_03_PEIS_X.mpr").write_text("x")
    (sample / "pyflowbatt.toml").write_text('[eis_tags]\n"pre-20%SOC" = ["*_03_PEIS_*"]\n')
    config = PyFlowBattConfig.load(sample, home=tmp_path / "home")

    classified = classify_technique_files(sample, config)

    eis = {k: v[0].name for k, v in classified.items() if k.startswith("eis_")}
    assert eis == {
        "eis_pre-20%SOC": "sample_03_PEIS_X.mpr",
        "eis_pre": "sample_02_PEIS_A.mpr",
        "eis_pre-50%SOC": "sample_05_PEIS_B.mpr",
        "eis_post-50%SOC": "sample_10_PEIS_C.mpr",
        "eis_post": "sample_12_PEIS_D.mpr",
    }


def test_eis_tags_custom_pin_splits_unpinned_around_cycling(tmp_path: Path) -> None:
    """Unpinned files are split before/after cycling, not filled in filename order."""
    sample = _write_synthetic_sample(tmp_path / "sample")
    (sample / "pyflowbatt.toml").write_text('[eis_tags]\n"pre-20%SOC" = ["*_05_PEIS_*"]\n')
    config = PyFlowBattConfig.load(sample, home=tmp_path / "home")

    classified = classify_technique_files(sample, config)

    # One before and two after cycling is not a standard layout
    eis = {k: v[0].name for k, v in classified.items() if k.startswith("eis_")}
    assert eis == {
        "eis_pre-20%SOC": "sample_05_PEIS_B.mpr",
        "eis_pre_1": "sample_02_PEIS_A.mpr",
        "eis_post_1": "sample_10_PEIS_C.mpr",
        "eis_post_2": "sample_12_PEIS_D.mpr",
    }


def test_eis_tags_pinned_standard_tag_fills_its_side(tmp_path: Path) -> None:
    """A pinned post tag leaves the other post tag for the unpinned file after cycling."""
    sample = _write_synthetic_sample(tmp_path / "sample")
    (sample / "pyflowbatt.toml").write_text('[eis_tags]\n"post" = ["*_12_PEIS_*"]\n')
    config = PyFlowBattConfig.load(sample, home=tmp_path / "home")

    classified = classify_technique_files(sample, config)

    eis = {k: v[0].name for k, v in classified.items() if k.startswith("eis_")}
    assert eis == {
        "eis_post": "sample_12_PEIS_D.mpr",
        "eis_pre": "sample_02_PEIS_A.mpr",
        "eis_pre-50%SOC": "sample_05_PEIS_B.mpr",
        "eis_post-50%SOC": "sample_10_PEIS_C.mpr",
    }


def test_eis_tags_pinned_tag_outside_layout_numbers_the_rest(tmp_path: Path) -> None:
    """A pinned 50% SOC tag doesn't fit the early protocol, so the rest are numbered."""
    sample = _write_two_eis_sample(tmp_path / "sample")
    (sample / "pyflowbatt.toml").write_text('[eis_tags]\n"pre-50%SOC" = ["*_02_PEIS_*"]\n')
    config = PyFlowBattConfig.load(sample, home=tmp_path / "home")

    classified = classify_technique_files(sample, config)

    eis = {k: v[0].name for k, v in classified.items() if k.startswith("eis_")}
    assert eis == {"eis_pre-50%SOC": "s_02_PEIS_A.mpr", "eis_post_1": "s_06_PEIS_A.mpr"}


def test_eis_tags_partial_fills_remaining_tags_in_order(tmp_path: Path) -> None:
    """A pinned standard tag counts towards the layout, unpinned files fill the rest."""
    sample = _write_two_eis_sample(tmp_path / "sample")
    (sample / "pyflowbatt.toml").write_text('[eis_tags]\n"post" = ["*_06_PEIS_*"]\n')
    config = PyFlowBattConfig.load(sample, home=tmp_path / "home")

    classified = classify_technique_files(sample, config)

    eis = {k: v[0].name for k, v in classified.items() if k.startswith("eis_")}
    assert eis == {"eis_pre": "s_02_PEIS_A.mpr", "eis_post": "s_06_PEIS_A.mpr"}


def test_eis_tags_sample_folder_overrides_parent(tmp_path: Path) -> None:
    """A sample-level eis_tags entry replaces the parent's entry for the same tag."""
    sample = _write_two_eis_sample(tmp_path / "parent" / "sample")
    (tmp_path / "parent" / "pyflowbatt.toml").write_text(
        '[eis_tags]\n"post-50%SOC" = ["*_06_PEIS_*"]\n"pre" = ["*_02_PEIS_*"]\n'
    )
    (sample / "pyflowbatt.toml").write_text('[eis_tags]\n"post-50%SOC" = ["*_99_PEIS_*"]\n')
    config = PyFlowBattConfig.load(sample, home=tmp_path / "home")

    assert config.eis_tag_patterns == {"post-50%SOC": ["*_99_PEIS_*"], "pre": ["*_02_PEIS_*"]}


def test_eis_tags_bad_entries_ignored(tmp_path: Path) -> None:
    """Tags with unsafe filename characters and non-list values in eis_tags are ignored."""
    sample = _write_two_eis_sample(tmp_path / "sample")
    (sample / "pyflowbatt.toml").write_text(
        '[eis_tags]\n"pre/20" = ["*_06_PEIS_*"]\n"mid 1" = ["*_06_PEIS_*"]\n'
        '"post" = "*_06_PEIS_*"\n'
    )
    config = PyFlowBattConfig.load(sample, home=tmp_path / "home")

    assert config.eis_tag_patterns == {}


def test_eis_tags_no_match_falls_back_to_positional(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A pinned pattern matching nothing warns and leaves the automatic tagging to decide."""
    sample = _write_two_eis_sample(tmp_path / "sample")
    (sample / "pyflowbatt.toml").write_text('[eis_tags]\n"pre" = ["*_42_PEIS_*"]\n')
    config = PyFlowBattConfig.load(sample, home=tmp_path / "home")

    with caplog.at_level("WARNING"):
        classified = classify_technique_files(sample, config, warn=True)

    eis = {k: v[0].name for k, v in classified.items() if k.startswith("eis_")}
    assert eis == {"eis_pre": "s_02_PEIS_A.mpr", "eis_post": "s_06_PEIS_A.mpr"}
    assert "No file matches eis_tags.pre" in caplog.text


def test_shared_classifier_used_by_rocrate(tmp_path: Path) -> None:
    """RO-Crate's _classify_inputs delegates to the same classify_technique_files."""
    sample = _write_synthetic_sample(tmp_path / "sample")
    (sample / "pyflowbatt.toml").write_text('[extra_patterns]\neis = ["*_GCPL_*"]\n')
    config = PyFlowBattConfig.load(sample, home=tmp_path / "home")

    direct = classify_technique_files(sample, config)
    via_rocrate = _classify_inputs(sample, config)

    assert direct == via_rocrate


def test_sample_id_explicit_override(tmp_path: Path) -> None:
    """sample_name in pyflowbatt.toml overrides detection, regardless of folder name."""
    from pyflowbatt.analysis import get_sampleid_from_folderpath

    # A folder name that would NOT match the default sample-ID regex.
    sample_dir = tmp_path / "not-a-normal-sample-name"
    sample_dir.mkdir()
    (sample_dir / "pyflowbatt.toml").write_text('sample_name = "hardcoded-name"\n')

    config = PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")
    assert get_sampleid_from_folderpath(sample_dir, config) == "hardcoded-name"


def test_sample_id_battinfo_name_used_when_no_toml_override(tmp_path: Path) -> None:
    """With no pyflowbatt.toml sample_name set, a BattINFO name wins over the folder name."""
    from pyflowbatt.analysis import get_sampleid_from_folderpath

    # A folder name that would NOT match the default sample-ID regex.
    sample_dir = tmp_path / "not-a-normal-sample-name"
    sample_dir.mkdir()

    config = PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")
    assert (
        get_sampleid_from_folderpath(sample_dir, config, battinfo_name="from-battinfo")
        == "from-battinfo"
    )


def test_sample_id_toml_wins_over_battinfo_when_they_agree(tmp_path: Path) -> None:
    """A pyflowbatt.toml sample_name matching the BattINFO name is accepted, no error."""
    from pyflowbatt.analysis import get_sampleid_from_folderpath

    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    (sample_dir / "pyflowbatt.toml").write_text('sample_name = "agreed-name"\n')

    config = PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")
    assert (
        get_sampleid_from_folderpath(sample_dir, config, battinfo_name="agreed-name")
        == "agreed-name"
    )


def test_sample_id_toml_battinfo_conflict_raises(tmp_path: Path) -> None:
    """A pyflowbatt.toml sample_name that disagrees with the BattINFO name is an error."""
    from pyflowbatt.analysis import get_sampleid_from_folderpath

    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    (sample_dir / "pyflowbatt.toml").write_text('sample_name = "toml-name"\n')

    config = PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")
    with pytest.raises(ValueError, match="Sample name mismatch"):
        get_sampleid_from_folderpath(sample_dir, config, battinfo_name="different-battinfo-name")


def _stub_battinfo(monkeypatch: pytest.MonkeyPatch, analysis_module: object) -> None:
    """Make BattINFO conversion return a minimal cell, so metadata gets written."""

    def fake_convert(_file: Path) -> dict:
        return {
            "@context": {},
            "@type": "RedoxFlowBattery",
            "schema:productID": "empa__fcid000001",
            "schema:name": "stub-cell",
        }

    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", fake_convert)


def test_raw_inputs_cover_unanalysed_and_unmatched_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raw files are all reported, whether their analysis failed or nothing matched them."""
    from pyflowbatt import analysis as analysis_module

    sample = _write_synthetic_sample(tmp_path / "sample")
    (sample / "metadata.xlsx").write_text("x")
    # Matches no technique pattern, so it is only picked up by the catch-all.
    (sample / "sample_random_extra.mpr").write_text("x")
    _stub_battinfo(monkeypatch, analysis_module)
    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")

    # The synthetic files aren't real EC-Lab data, so every technique fails to analyse.
    _outputs, _extras, raw_inputs, _fcid, _sample_id = analysis_module.analyse_sample(
        sample, save_format=None, config=config
    )

    # Only the largest GCPL is analysed, so the other cycling steps are unmatched too.
    assert {p.name for p in raw_inputs[analysis_module.OTHER_RAW_LABEL]} == {
        "sample_random_extra.mpr",
        "sample_04_GCPL_A.mpr",
        "sample_07_GCPL_C.mpr",
    }
    assert raw_inputs["gcpl"] == [sample / "sample_06_GCPL_B.mpr"]
    assert raw_inputs["eis_pre"] == [sample / "sample_02_PEIS_A.mpr"]

    metadata = json.loads((sample / "metadata.empa__fcid000001.json").read_text(encoding="utf-8"))
    described = {d["@id"] for d in metadata["hasOutput"]["dcat:distribution"]}
    assert "sample_random_extra.mpr" in described
    assert "sample_02_PEIS_A.mpr" in described


def test_unreadable_gcpl_and_cv_do_not_abort_the_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A GCPL or CV file that can't be analysed is warned about, not raised."""
    from pyflowbatt import analysis as analysis_module

    sample = _write_synthetic_sample(tmp_path / "sample")
    (sample / "metadata.xlsx").write_text("x")
    _stub_battinfo(monkeypatch, analysis_module)
    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")

    outputs, _extras, raw_inputs, _fcid, _sample_id = analysis_module.analyse_sample(
        sample, save_format=None, config=config
    )

    # Nothing could be analysed, but the raw files are still described.
    assert "gcpl" not in outputs
    assert "cv_pre" not in outputs
    assert raw_inputs["cv_pre"] == [sample / "sample_CVApre_A.mpr"]


def test_raw_description_names_the_format_only_for_mpr() -> None:
    """ "(EC-Lab MPR)" is only appended to descriptions of actual .mpr files."""
    from pyflowbatt import analysis as analysis_module

    mpr = analysis_module.raw_description("lsv_pre", Path("cell_03_LSV_A.mpr"))
    parquet = analysis_module.raw_description("lsv_pre", Path("cell_03_LSV_A.bdf.parquet"))
    numbered = analysis_module.raw_description("eis_post_1", Path("cell_10_PEIS_A.csv"))

    assert mpr == "Pre-cycling linear sweep voltammetry (EC-Lab MPR)"
    assert parquet == "Pre-cycling linear sweep voltammetry"
    assert numbered == "EIS measurement, after cycling, measurement 1"


def test_media_types_agree_between_crate_and_battinfo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same file gets the same media type in the crate and the BattINFO JSON-LD."""
    from pyflowbatt import analysis as analysis_module
    from pyflowbatt.rocrate_output import write_rocrate

    sample = _write_synthetic_sample(tmp_path / "sample")
    (sample / "metadata.xlsx").write_text("x")
    (sample / "sample_protocol.mps").write_text("EC-LAB SETTING FILE")
    _stub_battinfo(monkeypatch, analysis_module)
    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    outputs, extras, raw_inputs, fcid, sample_id = analysis_module.analyse_sample(
        sample, save_format=None, config=config, root_folder=tmp_path
    )
    write_rocrate(
        tmp_path,
        [sample],
        {sample: outputs},
        {sample: extras},
        {sample: fcid},
        {sample: config},
        {sample: sample_id},
        {sample: raw_inputs},
    )

    crate = json.loads((tmp_path / "ro-crate-metadata.json").read_text(encoding="utf-8"))
    crate_types = {
        e["@id"]: e["encodingFormat"] for e in crate["@graph"] if e.get("encodingFormat")
    }
    metadata_path = sample / "metadata.empa__fcid000001.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    iana = "http://www.iana.org/assignments/media-types/"
    battinfo_iris = [
        (d["@id"], d["dcat:mediaType"]["@id"])
        for section in ("hasOutput", "hasInput")
        for d in metadata[section]["dcat:distribution"]
        if d.get("dcat:mediaType")
    ]
    assert all(iri.startswith(iana) for _, iri in battinfo_iris)
    battinfo_types = {file_id: iri.removeprefix(iana) for file_id, iri in battinfo_iris}

    assert crate_types["sample/sample_02_PEIS_A.mpr"] == "application/octet-stream"
    assert crate_types["sample/sample_protocol.mps"] == "text/plain"
    assert crate_types[f"sample/{metadata_path.name}"] == "application/ld+json"
    # Raw .mpr files now carry a media type in the BattINFO JSON-LD too.
    assert battinfo_types["sample/sample_02_PEIS_A.mpr"] == "application/octet-stream"
    assert battinfo_types["sample/sample_protocol.mps"] == "text/plain"
    for file_id, crate_type in crate_types.items():
        if file_id in battinfo_types:
            assert battinfo_types[file_id] == crate_type, file_id


def test_unwrap_len_1_list() -> None:
    """Single-entity lists become plain values; empty and longer lists are untouched."""
    from pyflowbatt.rocrate_output import _unwrap_len_1_list

    assert _unwrap_len_1_list(["a"]) == "a"
    assert _unwrap_len_1_list(["a", "b"]) == ["a", "b"]
    assert _unwrap_len_1_list([]) is None


def test_rocrate_describes_unmatched_raw_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unmatched raw files reach the crate with a description and no measurementTechnique."""
    from pyflowbatt import analysis as analysis_module
    from pyflowbatt.rocrate_output import write_rocrate

    sample = _write_synthetic_sample(tmp_path / "sample")
    (sample / "metadata.xlsx").write_text("x")
    (sample / "sample_random_extra.mpr").write_text("x")
    _stub_battinfo(monkeypatch, analysis_module)
    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    outputs, extras, raw_inputs, fcid, sample_id = analysis_module.analyse_sample(
        sample, save_format=None, config=config, root_folder=tmp_path
    )

    write_rocrate(
        tmp_path,
        [sample],
        {sample: outputs},
        {sample: extras},
        {sample: fcid},
        {sample: config},
        {sample: sample_id},
        {sample: raw_inputs},
    )

    crate = json.loads((tmp_path / "ro-crate-metadata.json").read_text(encoding="utf-8"))
    entities = {e["@id"]: e for e in crate["@graph"]}
    unmatched = entities["sample/sample_random_extra.mpr"]
    assert "measurementTechnique" not in unmatched
    assert unmatched["description"] == f"{analysis_module.OTHER_RAW_DESCRIPTION} (EC-Lab MPR)"
    # A file whose analysis failed keeps its technique and is still described.
    assert (
        "Electrochemical Impedance"
        in entities["sample/sample_02_PEIS_A.mpr"]["measurementTechnique"]
    )


@pytest.mark.parametrize(
    "cell_type",
    [
        "RedoxFlowBattery",
        "VanadiumRedoxFlowBattery",
        "FlowCell",
        ["RedoxFlowBattery", "IronRedoxFlowBattery"],
    ],
)
def test_make_test_object_wraps_cell_types(cell_type: str | list[str]) -> None:
    """Cell classes, including subtypes from the v0.2.0 template, become the test object."""
    from pyflowbatt.battinfo import make_test_object

    test = make_test_object({"@context": {}, "@type": cell_type, "schema:productID": "FCID1"})
    assert test["@type"] == "BatteryTest"
    assert test["hasTestObject"]["@type"] == cell_type
    assert test["hasTestObject"]["schema:productID"] == "FCID1"


@pytest.mark.parametrize(
    ("cell_type", "expected"),
    [
        ("RedoxFlowBattery", True),
        ("VanadiumRedoxFlowBattery", True),
        ("LithiumIonBattery", True),
        ("FlowCell", True),
        ("CoinCell", True),
        (["Thing", "RedoxFlowBattery"], True),
        ("Cheese", False),
        ("Electrode", False),
        (None, False),
    ],
)
def test_is_cell_type(cell_type: str | list[str] | None, *, expected: bool) -> None:
    """Battery and cell classes are recognised, anything else is not."""
    from pyflowbatt.battinfo import is_cell_type

    assert is_cell_type(cell_type) == expected


def test_analyse_sample_accepts_cell_subtype(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subtype like VanadiumRedoxFlowBattery is analysed like any flow cell."""
    from pyflowbatt import analysis as analysis_module

    sample = tmp_path / "sample"
    sample.mkdir()
    (sample / "metadata.xlsx").write_text("x")  # content is irrelevant; conversion is stubbed

    def fake_convert(_file: Path) -> dict:
        return {
            "@context": {},
            "@type": "VanadiumRedoxFlowBattery",
            "schema:productID": "empa__fcid123456",
            "schema:name": "vanadium-cell",
        }

    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", fake_convert)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    _outputs, _extra_inputs, _raw_inputs, fcid, sample_id = analysis_module.analyse_sample(
        sample, save_format=None, config=config
    )
    assert fcid == "empa__fcid123456"
    assert sample_id == "vanadium-cell"
    metadata = json.loads((sample / "metadata.empa__fcid123456.json").read_text())
    assert metadata["hasTestObject"]["@type"] == "VanadiumRedoxFlowBattery"


def test_analyse_sample_warns_about_odd_cell_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A cell type that is not a battery or cell class is warned about, naming the sheet."""
    from pyflowbatt import analysis as analysis_module

    sample = tmp_path / "sample"
    sample.mkdir()
    (sample / "metadata.xlsx").write_text("x")  # content is irrelevant; conversion is stubbed

    def fake_convert(_file: Path) -> dict:
        return {
            "@context": {},
            "@type": "Cheese",
            "schema:productID": "FCID1",
            "schema:name": "cheese-cell",
        }

    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", fake_convert)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    with caplog.at_level("WARNING", logger="pyflowbatt"):
        analysis_module.analyse_sample(sample, save_format=None, config=config)

    assert "Cell type 'Cheese' in metadata.xlsx doesn't look like a battery" in caplog.text
    metadata = json.loads((sample / "metadata.FCID1.json").read_text())
    assert metadata["hasTestObject"]["@type"] == "Cheese"


def test_analyse_sample_uses_battinfo_fcid_without_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """analyse_sample resolves sample_name from BattINFO when pyflowbatt.toml doesn't set one."""
    from pyflowbatt import analysis as analysis_module

    # A folder name that would NOT match the default sample-ID regex.
    sample = tmp_path / "not-a-normal-sample-name"
    sample.mkdir()
    (sample / "metadata.xlsx").write_text("x")  # content is irrelevant; conversion is stubbed

    def fake_convert(_file: Path) -> dict:
        return {
            "@context": {},
            "@type": "RedoxFlowBattery",
            "schema:productID": "empa__fcid123456",
            "schema:name": "battinfo-derived-name",
        }

    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", fake_convert)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    _outputs, _extra_inputs, _raw_inputs, fcid, sample_id = analysis_module.analyse_sample(
        sample, save_format=None, config=config
    )
    assert sample_id == "battinfo-derived-name"
    assert fcid == "empa__fcid123456"
    assert (sample / "metadata.empa__fcid123456.json").exists()


def test_analyse_sample_uses_battinfo_name_without_toml_fcid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """analyse_sample resolves sample_name from BattINFO when pyflowbatt.toml doesn't set one."""
    from pyflowbatt import analysis as analysis_module

    # A folder name that would NOT match the default sample-ID regex.
    sample = tmp_path / "not-a-normal-sample-name"
    sample.mkdir()
    (sample / "metadata.xlsx").write_text("x")  # content is irrelevant; conversion is stubbed

    def fake_convert(_file: Path) -> dict:
        return {
            "@context": {},
            "@type": "RedoxFlowBattery",
            "schema:productID": None,
            "schema:name": "battinfo-derived-name",
        }

    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", fake_convert)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    _outputs, _extra_inputs, _raw_inputs, fcid, sample_id = analysis_module.analyse_sample(
        sample, save_format=None, config=config
    )
    assert sample_id == "battinfo-derived-name"
    assert fcid is None
    assert (sample / "metadata.battinfo-derived-name.json").exists()


def test_analyse_sample_raises_on_toml_battinfo_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """analyse_sample raises when pyflowbatt.toml sample_name disagrees with BattINFO's name."""
    from pyflowbatt import analysis as analysis_module

    sample = tmp_path / "sample"
    sample.mkdir()
    (sample / "metadata.xlsx").write_text("x")
    (sample / "pyflowbatt.toml").write_text('sample_name = "toml-name"\n')

    def fake_convert(_file: Path) -> dict:
        return {
            "@context": {},
            "@type": "RedoxFlowBattery",
            "schema:productID": "FCID1",
            "schema:name": "different-battinfo-name",
        }

    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", fake_convert)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    with pytest.raises(ValueError, match="Sample name mismatch"):
        analysis_module.analyse_sample(sample, save_format=None, config=config)


def _all_ids(obj: object) -> set[str]:
    """Recursively collect every "@id" string value in a JSON-LD-like structure."""
    found: set[str] = set()
    if isinstance(obj, dict):
        id_value = obj.get("@id")
        if isinstance(id_value, str):
            found.add(id_value)
        for v in obj.values():
            found |= _all_ids(v)
    elif isinstance(obj, list):
        for item in obj:
            found |= _all_ids(item)
    return found


def _fake_convert(_file: Path) -> dict:
    return {
        "@context": {},
        "@type": "RedoxFlowBattery",
        "schema:productID": "empa__fcid123456",
        "schema:name": "battinfo-derived-name",
    }


def _all_download_urls(obj: object) -> set[str]:
    """Recursively collect every "dcat:downloadURL" IRI."""
    found: set[str] = set()
    if isinstance(obj, dict):
        url_value = obj.get("dcat:downloadURL")
        if isinstance(url_value, dict):
            found.add(url_value["@id"])
        elif isinstance(url_value, str):
            msg = "dcat:downloadURL is plain string - it should be an object with @id"
            pytest.fail(msg)
        for v in obj.values():
            found |= _all_download_urls(v)
    elif isinstance(obj, list):
        for item in obj:
            found |= _all_download_urls(item)
    return found


def test_analyse_sample_without_zip_has_no_download_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a zip, @id stays root-relative and @base is set, but nothing has a downloadURL."""
    from pyflowbatt import analysis as analysis_module

    root = tmp_path / "project"
    sample = root / "sample_01"
    sample.mkdir(parents=True)
    (sample / "metadata.xlsx").write_text("x")

    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", _fake_convert)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    analysis_module.analyse_sample(
        sample,
        save_format=None,
        config=config,
        root_folder=root,
        pub_info={"zenodo_doi_url": "https://doi.org/10.5281/zenodo.20338409"},
    )

    metadata = json.loads((sample / "metadata.empa__fcid123456.json").read_text())
    ids = _all_ids(metadata)
    assert "sample_01/metadata.xlsx" in ids
    assert _all_download_urls(metadata) == set()

    context = metadata["@context"]
    local_terms = next(c for c in context if isinstance(c, dict))
    assert local_terms["@base"] == "https://zenodo.org/records/20338409/"


SHEET_ZENODO_URL = "https://doi.org/10.5281/zenodo.20338409"


def _fake_convert_with_references(_file: Path) -> dict:
    """Return converter output with @References included, the cell nested in a BatteryTest."""
    return {
        "@context": {},
        "@type": "BatteryTest",
        "hasTestObject": {
            "@type": "RedoxFlowBattery",
            "schema:productID": "empa__fcid123456",
            "schema:name": "battinfo-derived-name",
        },
        "hasOutput": {
            "@type": ["BatteryTestResult", "dcat:Dataset"],
            "dcterms:creator": {
                "@type": "schema:Person",
                "@id": "https://orcid.org/0000-0000-0000-0001",
                "schema:name": "Sheet Author",
            },
            "dcterms:issued": "2026-08-05",
            "dcat:accessURL": {"@id": SHEET_ZENODO_URL},
        },
    }


def test_analyse_sample_sheet_references_take_priority_over_pub_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The sheet beats pub_info, which only fills gaps, and pyflowbatt defaults come last."""
    from pyflowbatt import analysis as analysis_module

    root = tmp_path / "project"
    sample = root / "sample_01"
    sample.mkdir(parents=True)
    (sample / "metadata.xlsx").write_text("x")
    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", _fake_convert_with_references)

    pub_info = {
        "zenodo_doi_url": SHEET_ZENODO_URL,
        "citation_string": "Cite me",
        "authors": [
            {
                "name": "Pub Author",
                "orcid": "https://orcid.org/0000-0000-0000-0002",
                "affiliation": ["Empa"],
            }
        ],
        "institutions": {"Empa": {"wikidata_url": "http://www.wikidata.org/entity/Q683116"}},
    }
    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    with caplog.at_level("WARNING", logger="pyflowbatt"):
        analysis_module.analyse_sample(
            sample, save_format=None, config=config, root_folder=root, pub_info=pub_info
        )

    metadata = json.loads((sample / "metadata.empa__fcid123456.json").read_text())
    output = metadata["hasOutput"]
    assert output["dcterms:creator"]["schema:name"] == "Sheet Author"
    assert output["dcterms:issued"] == "2026-08-05"
    assert output["schema:citation"] == "Cite me"
    assert output["dcat:endpointURL"] == {"@id": "https://zenodo.org/api/records/20338409"}
    messages = [r.getMessage() for r in caplog.records if r.name == "pyflowbatt.battinfo"]
    assert len(messages) == 1
    assert "hasOutput/dcterms:creator" in messages[0]


def test_analyse_sample_zenodo_record_from_sheet_without_pub_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sheet's Dataset URL sets @base and download URLs when there is no pub_info."""
    from pyflowbatt import analysis as analysis_module

    root = tmp_path / "project"
    sample = root / "sample_01"
    sample.mkdir(parents=True)
    (sample / "metadata.xlsx").write_text("x")
    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", _fake_convert_with_references)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    analysis_module.analyse_sample(
        sample, save_format=None, config=config, root_folder=root, zip_filename="project.zip"
    )

    metadata = json.loads((sample / "metadata.empa__fcid123456.json").read_text())
    local_terms = next(c for c in metadata["@context"] if isinstance(c, dict))
    assert local_terms["@base"] == "https://zenodo.org/records/20338409/"
    expected = "https://zenodo.org/records/20338409/files/project.zip#sample_01/metadata.xlsx"
    assert expected in _all_download_urls(metadata)


def test_analyse_sample_raises_on_zenodo_record_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sheet and pub_info naming different Zenodo records is an error."""
    from pyflowbatt import analysis as analysis_module

    root = tmp_path / "project"
    sample = root / "sample_01"
    sample.mkdir(parents=True)
    (sample / "metadata.xlsx").write_text("x")
    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", _fake_convert_with_references)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    with pytest.raises(ValueError, match="Zenodo record mismatch"):
        analysis_module.analyse_sample(
            sample,
            save_format=None,
            config=config,
            root_folder=root,
            pub_info={"zenodo_doi_url": "https://doi.org/10.5281/zenodo.99999999"},
        )


def test_analyse_sample_owns_file_list_and_keeps_sheet_descriptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Sheet file rows only describe files pyflowbatt finds, the rest are dropped with a warning."""
    from pyflowbatt import analysis as analysis_module

    root = tmp_path / "project"
    sample = root / "sample_01"
    sample.mkdir(parents=True)
    (sample / "metadata.xlsx").write_text("x")

    def fake_convert(file: Path) -> dict:
        jsonld = _fake_convert_with_references(file)
        zip_url = "https://zenodo.org/records/20338409/files/project.zip"
        jsonld["hasOutput"]["dcat:distribution"] = [
            {"@id": f"{zip_url}#sample_01/metadata.xlsx", "rdfs:comment": "Sheet description"},
            {"@id": f"{zip_url}#empa__fcid000001/data.parquet", "rdfs:comment": "Other cell"},
        ]
        jsonld["hasInput"] = {
            "@type": ["RawData", "dcat:Dataset"],
            "dcat:distribution": {"@id": f"{zip_url}#empa__fcid000001/raw.mpr"},
        }
        return jsonld

    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", fake_convert)
    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    with caplog.at_level("WARNING", logger="pyflowbatt"):
        analysis_module.analyse_sample(sample, save_format=None, config=config, root_folder=root)

    metadata = json.loads((sample / "metadata.empa__fcid123456.json").read_text())
    file_ids = [
        d["@id"]
        for section in ("hasOutput", "hasInput")
        for d in metadata[section]["dcat:distribution"]
        if isinstance(metadata[section]["dcat:distribution"], list)
    ] + [
        metadata[section]["dcat:distribution"]["@id"]
        for section in ("hasOutput", "hasInput")
        if isinstance(metadata[section]["dcat:distribution"], dict)
    ]
    assert file_ids
    assert not any("://" in i for i in file_ids)
    inputs = metadata["hasInput"]["dcat:distribution"]
    xlsx = next(d for d in (inputs if isinstance(inputs, list) else [inputs]))
    assert xlsx["@id"] == "sample_01/metadata.xlsx"
    assert xlsx["rdfs:comment"] == "Sheet description"
    assert metadata["hasInput"]["@type"] == "dcat:Dataset"
    assert "lists empa__fcid000001/data.parquet, which is missing" in caplog.text


def test_analyse_all_samples_collects_requested_files_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Default, include_files and BattINFO-requested files are each described and zipped once."""
    from pyflowbatt import analysis as analysis_module

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    root = tmp_path / "project"
    sample = _write_synthetic_sample(root / "sample_01")
    other = _write_synthetic_sample(root / "sample_02")
    (sample / "metadata.xlsx").write_text("x")
    (other / "metadata.xlsx").write_text("x")
    for name in ("notes.pdf", "setup.txt", "photo.png"):
        (sample / name).write_text("x")
    (other / "other_notes.pdf").write_text("x")
    # Extensions that also match PyFlowBatt's own files, and an overlapping include pattern
    (sample / "pyflowbatt.toml").write_text(
        'extra_extensions = [".png", ".xlsx", ".json", ".toml"]\ninclude_files = ["*.pdf"]\n'
    )

    def fake_convert(file: Path) -> dict:
        jsonld = _fake_convert_with_references(file)
        if file.parent.name == "sample_01":
            jsonld["hasOutput"]["dcat:distribution"] = [
                {"dcterms:identifier": "sample_01/notes.pdf", "rdfs:comment": "Lab notes"},
                {"dcterms:identifier": "sample_02/other_notes.pdf"},
                {"dcterms:identifier": "sample_01/missing.csv"},
            ]
            jsonld["hasInput"] = {
                "@type": "dcat:Dataset",
                "dcat:distribution": {
                    "dcterms:identifier": "sample_01/setup.txt",
                    "rdfs:comment": "Rig setup",
                },
            }
        else:
            jsonld["hasTestObject"]["schema:productID"] = "empa__fcid000002"
        return jsonld

    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", fake_convert)
    for _ in range(2):  # the second run also sees the first run's metadata files
        caplog.clear()
        with caplog.at_level("WARNING", logger="pyflowbatt"):
            analysis_module.analyse_all_samples(root, save_format=None, zip_output=True)

    metadata = json.loads((sample / "metadata.empa__fcid123456.json").read_text())
    nodes: dict[str, list[tuple[str, dict]]] = {}
    for section in ("hasInput", "hasOutput"):
        dist = metadata[section]["dcat:distribution"]
        for node in dist if isinstance(dist, list) else [dist]:
            nodes.setdefault(node["@id"], []).append((section, node))
    assert all(len(found) == 1 for found in nodes.values())
    assert nodes["sample_01/notes.pdf"][0][0] == "hasOutput"
    assert nodes["sample_01/notes.pdf"][0][1]["rdfs:comment"] == "Lab notes"
    assert nodes["sample_01/setup.txt"][0][0] == "hasInput"
    assert nodes["sample_01/setup.txt"][0][1]["rdfs:comment"] == "Rig setup"
    assert nodes["sample_01/metadata.xlsx"][0][0] == "hasInput"
    assert nodes["sample_01/pyflowbatt.toml"][0][0] == "hasInput"
    assert not any(node_id.endswith(".json") for node_id in nodes)
    assert "sample_02/other_notes.pdf" not in nodes

    assert "lists sample_02/other_notes.pdf, which is outside this sample folder" in caplog.text
    assert "lists sample_01/missing.csv, which is missing" in caplog.text
    # Only sample_02's own sheet or config could ask for its file
    assert "Not zipping 1 file(s)" in caplog.text
    assert "package them: sample_02/other_notes.pdf" in caplog.text
    with zipfile.ZipFile(tmp_path / "project.zip") as zf:
        names = zf.namelist()
    assert {"sample_01/notes.pdf", "sample_01/setup.txt", "sample_01/photo.png"} <= set(names)
    assert len(names) == len(set(names))


def test_analyse_sample_lists_protocol_once_as_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A protocol is only an input, even if .mps is a raw data extension and the sheet lists it."""
    from pyflowbatt import analysis as analysis_module

    root = tmp_path / "project"
    sample = root / "sample_01"
    sample.mkdir(parents=True)
    (sample / "metadata.xlsx").write_text("x")
    (sample / "cell.mps").write_text("EC-LAB SETTING FILE")
    (sample / "pyflowbatt.toml").write_text('extra_extensions = [".mps"]\n')

    def fake_convert(file: Path) -> dict:
        jsonld = _fake_convert_with_references(file)
        jsonld["hasInput"] = {
            "@type": "dcat:Dataset",
            "dcat:distribution": {
                "dcterms:identifier": "sample_01/cell.mps",
                "rdfs:comment": "Sheet protocol description",
            },
        }
        return jsonld

    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", fake_convert)
    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    analysis_module.analyse_sample(sample, save_format=None, config=config, root_folder=root)

    metadata = json.loads((sample / "metadata.empa__fcid123456.json").read_text())
    nodes = {}
    for section in ("hasInput", "hasOutput"):
        dist = metadata[section]["dcat:distribution"]
        dist = dist if isinstance(dist, list) else [dist]
        nodes[section] = [d for d in dist if d["@id"] == "sample_01/cell.mps"]
    assert nodes["hasOutput"] == []
    assert len(nodes["hasInput"]) == 1
    assert nodes["hasInput"][0]["rdfs:comment"] == "Sheet protocol description"


@pytest.mark.parametrize(
    ("node", "expected"),
    [
        ({"dcterms:identifier": "a/b.csv"}, "a/b.csv"),
        ({"@id": "https://zenodo.org/records/1/files/data.zip#a/b%20c.csv"}, "a/b c.csv"),
        ({"@id": "https://zenodo.org/records/1/files/a/b.csv"}, "a/b.csv"),
    ],
)
def test_pop_sheet_files_recovers_paths(node: dict, expected: str) -> None:
    """Converter file nodes give back their root-relative path and are removed."""
    from pyflowbatt.battinfo import SheetFile, pop_sheet_files

    jsonld = {"hasInput": {"@type": "dcat:Dataset", "dcat:distribution": node}}
    assert pop_sheet_files(jsonld) == {expected: SheetFile("hasInput", None)}
    assert jsonld == {}


def test_analyse_sample_records_pub_info_xlsx_as_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The root publication info xlsx is tracked as an input of the sample's metadata."""
    from pyflowbatt import analysis as analysis_module

    root = tmp_path / "project"
    sample = root / "sample_01"
    sample.mkdir(parents=True)
    (sample / "metadata.xlsx").write_text("x")
    pub_info_xlsx = root / "publication_info.xlsx"
    pub_info_xlsx.write_text("x")

    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", _fake_convert)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    _outputs, extra_inputs, _raw_inputs, _fcid, _sample_id = analysis_module.analyse_sample(
        sample,
        save_format=None,
        config=config,
        root_folder=root,
        pub_info={"zenodo_doi_url": "https://doi.org/10.5281/zenodo.20338409"},
        pub_info_path=pub_info_xlsx,
        zip_filename="project.zip",
    )

    assert extra_inputs["pub_info"] == [pub_info_xlsx]

    metadata = json.loads((sample / "metadata.empa__fcid123456.json").read_text())
    assert "publication_info.xlsx" in _all_ids(metadata["hasInput"])
    expected = "https://zenodo.org/records/20338409/files/project.zip#publication_info.xlsx"
    assert expected in _all_download_urls(metadata["hasInput"])


def test_analyse_sample_zip_download_url_points_at_zip_with_path_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a zip, @id stays root-relative, downloadURL is the zip with a fragment."""
    from pyflowbatt import analysis as analysis_module

    root = tmp_path / "project"
    sample = root / "sample_01"
    sample.mkdir(parents=True)
    (sample / "metadata.xlsx").write_text("x")

    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", _fake_convert)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    analysis_module.analyse_sample(
        sample,
        save_format=None,
        config=config,
        root_folder=root,
        zip_filename="project.zip",
        pub_info={"zenodo_doi_url": "https://doi.org/10.5281/zenodo.20338409"},
    )

    metadata = json.loads((sample / "metadata.empa__fcid123456.json").read_text())
    ids = _all_ids(metadata)
    assert "sample_01/metadata.xlsx" in ids

    urls = _all_download_urls(metadata)
    expected = "https://zenodo.org/records/20338409/files/project.zip#sample_01/metadata.xlsx"
    assert expected in urls

    context = metadata["@context"]
    local_terms = next(c for c in context if isinstance(c, dict))
    assert local_terms["@base"] == "https://zenodo.org/records/20338409/"


def test_analyse_sample_base_shared_across_zips_in_one_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two zips in the same record share "@base": it's scoped to the record, not the zip.

    E.g. publishing the same sample as a small binary-format zip and a larger
    csv zip under one Zenodo record: files shared between the two bundles
    (raw inputs, plots, summaries) are the same resource, and should resolve
    to the same "@id" once expanded - only "dcat:downloadURL" differs per zip.
    """
    from pyflowbatt import analysis as analysis_module

    root = tmp_path / "project"
    sample = root / "sample_01"
    sample.mkdir(parents=True)
    (sample / "metadata.xlsx").write_text("x")

    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", _fake_convert)
    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")

    bases = []
    download_urls = []
    for zip_filename in ("sample_01_parquet.zip", "sample_01_csv.zip"):
        analysis_module.analyse_sample(
            sample,
            save_format=None,
            config=config,
            root_folder=root,
            zip_filename=zip_filename,
            pub_info={"zenodo_doi_url": "https://doi.org/10.5281/zenodo.20338409"},
        )
        metadata = json.loads((sample / "metadata.empa__fcid123456.json").read_text())
        assert "sample_01/metadata.xlsx" in _all_ids(metadata)
        local_terms = next(c for c in metadata["@context"] if isinstance(c, dict))
        bases.append(local_terms["@base"])
        download_urls.append(_all_download_urls(metadata))

    assert bases[0] == bases[1] == "https://zenodo.org/records/20338409/"
    assert download_urls[0] != download_urls[1]


@pytest.mark.parametrize("zip_output", [False, True])
def test_analyse_all_samples_rocrate_only_inside_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, zip_output: bool
) -> None:
    """The RO-Crate is only written with a zip, and then only inside it."""
    from pyflowbatt import analysis as analysis_module

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    root = tmp_path / "project"
    _write_synthetic_sample(root / "sample_01")

    analysis_module.analyse_all_samples(root, save_format=None, zip_output=zip_output)

    assert not (root / "ro-crate-metadata.json").exists()
    zip_path = tmp_path / "project.zip"
    assert zip_path.exists() == zip_output
    if zip_output:
        with zipfile.ZipFile(zip_path) as zf:
            assert "ro-crate-metadata.json" in zf.namelist()


def test_agreed_dataset_details_keeps_agreement_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Terms all samples agree on are mapped, conflicting ones are left out with a warning."""
    from pyflowbatt.rocrate_output import agreed_dataset_details

    docs = [
        {
            "hasOutput": {
                "dcterms:title": "Title",
                "dcterms:license": {"@id": "https://creativecommons.org/licenses/by/4.0/"},
                "schema:datePublished": "2026-08-05",
                "dcat:accessURL": {"@id": SHEET_ZENODO_URL},
            }
        },
        {
            "hasOutput": {
                "dcterms:title": "Title",
                "dcterms:license": {"@id": "https://creativecommons.org/publicdomain/zero/1.0/"},
                "schema:datePublished": "2026-08-05",
            }
        },
    ]
    with caplog.at_level("WARNING", logger="pyflowbatt"):
        details = agreed_dataset_details(docs)

    assert details == {
        "name": "Title",
        "datePublished": "2026-08-05",
        "identifier": SHEET_ZENODO_URL,
    }
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1
    assert "disagree on dcterms:license" in messages[0]


def test_write_rocrate_root_from_dataset_details(tmp_path: Path) -> None:
    """Dataset details replace the crate's own root name, description, license and dates."""
    from pyflowbatt.rocrate_output import write_rocrate

    cc0 = "https://creativecommons.org/publicdomain/zero/1.0/"
    write_rocrate(
        tmp_path,
        [],
        {},
        dataset_details={
            "name": "Title",
            "description": "Description",
            "license": cc0,
            "datePublished": "2026-08-05",
            "identifier": SHEET_ZENODO_URL,
        },
    )

    crate = json.loads((tmp_path / "ro-crate-metadata.json").read_text(encoding="utf-8"))
    entities = {e["@id"]: e for e in crate["@graph"]}
    root = entities["./"]
    assert root["name"] == "Title"
    assert root["description"] == "Description"
    assert root["license"] == {"@id": cc0}
    assert entities[cc0]["@type"] == "CreativeWork"
    assert root["datePublished"] == "2026-08-05"
    assert root["identifier"] == SHEET_ZENODO_URL


def test_write_rocrate_root_defaults_without_dataset_details(tmp_path: Path) -> None:
    """Without dataset details the root keeps the folder name and CC BY 4.0."""
    from pyflowbatt.rocrate_output import LICENSE_URL, write_rocrate

    write_rocrate(tmp_path, [], {})

    crate = json.loads((tmp_path / "ro-crate-metadata.json").read_text(encoding="utf-8"))
    entities = {e["@id"]: e for e in crate["@graph"]}
    root = entities["./"]
    assert root["name"] == tmp_path.name
    assert root["license"] == {"@id": LICENSE_URL}
    assert entities[LICENSE_URL]["name"] == "CC BY 4.0"
    assert "identifier" not in root


def test_analyse_all_samples_rocrate_root_from_battinfo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The zipped RO-Crate root takes its name and identifier from the samples' BattINFO."""
    from pyflowbatt import analysis as analysis_module

    def fake_convert(file: Path) -> dict:
        jsonld = _fake_convert_with_references(file)
        jsonld["hasOutput"]["dcterms:title"] = "Dataset title"
        return jsonld

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", fake_convert)
    root = tmp_path / "project"
    for name in ("sample_01", "sample_02"):
        sample = _write_synthetic_sample(root / name)
        (sample / "metadata.xlsx").write_text("x")

    analysis_module.analyse_all_samples(root, save_format=None, zip_output=True)

    with zipfile.ZipFile(tmp_path / "project.zip") as zf:
        crate = json.loads(zf.read("ro-crate-metadata.json"))
    crate_root = next(e for e in crate["@graph"] if e["@id"] == "./")
    assert crate_root["name"] == "Dataset title"
    assert crate_root["identifier"] == SHEET_ZENODO_URL


def test_analyse_sample_no_zenodo_url_keeps_bare_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no zenodo_doi_url yet, @id stays a plain root-relative path, no downloadURL."""
    from pyflowbatt import analysis as analysis_module

    root = tmp_path / "project"
    sample = root / "sample_01"
    sample.mkdir(parents=True)
    (sample / "metadata.xlsx").write_text("x")

    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", _fake_convert)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    analysis_module.analyse_sample(sample, save_format=None, config=config, root_folder=root)

    metadata = json.loads((sample / "metadata.empa__fcid123456.json").read_text())
    ids = _all_ids(metadata)
    assert "sample_01/metadata.xlsx" in ids
    assert not _all_download_urls(metadata)


def test_zip_folder_uses_root_relative_arcnames_no_wrapping_directory(tmp_path: Path) -> None:
    """zip_folder archives files under root-relative paths, matching BattINFO @id paths."""
    from pyflowbatt.analysis import zip_folder

    root = tmp_path / "project"
    (root / "sample_01" / "results").mkdir(parents=True)
    (root / "sample_01" / "results" / "gcpl.png").write_bytes(b"fake png")
    (root / "combined_summary.xlsx").write_bytes(b"fake xlsx")

    zip_path = tmp_path / "project.zip"
    zip_folder(root, zip_path, ["sample_01/results/gcpl.png", "combined_summary.xlsx"])

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert names == {"sample_01/results/gcpl.png", "combined_summary.xlsx"}


def test_zip_folder_skips_undescribed_files_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Files not in the member list stay out of the zip and are named in a warning."""
    from pyflowbatt.analysis import zip_folder

    root = tmp_path / "project"
    (root / "sample_01").mkdir(parents=True)
    (root / "sample_01" / "data.csv").write_text("x")
    (root / "sample_01" / "old.bak").write_text("x")

    zip_path = tmp_path / "project.zip"
    with caplog.at_level("WARNING", logger="pyflowbatt"):
        zip_folder(root, zip_path, ["sample_01/data.csv"])

    with zipfile.ZipFile(zip_path) as zf:
        assert zf.namelist() == ["sample_01/data.csv"]
    assert "Not zipping 1 file(s)" in caplog.text
    assert "sample_01/old.bak" in caplog.text


def test_inherit_false_skips_home_and_parent_and_warns_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A config with inherit = false is used alone, with one warning about the home config."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "pyflowbatt.toml").write_text("area_cm2 = 9\nextra_extensions = ['.mgr']\n")
    project = tmp_path / "project"
    (project / "pyflowbatt.toml").parent.mkdir()
    (project / "pyflowbatt.toml").write_text("assembled_resistance_ohm = 1000\n")
    samples = [project / "sample_01", project / "sample_02"]
    for sample in samples:
        sample.mkdir()
        (sample / "pyflowbatt.toml").write_text("inherit = false\nlsv_threshold = 3\n")

    with caplog.at_level("WARNING", logger="pyflowbatt"):
        configs = [PyFlowBattConfig.load(sample, home=home) for sample in samples]

    for config in configs:
        assert config == PyFlowBattConfig(lsv_threshold=3)
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1
    assert str(home / "pyflowbatt.toml") in messages[0]


def test_resolved_toml_reproduces_settings_despite_other_home_config(tmp_path: Path) -> None:
    """A resolved pyflowbatt.toml loads back to the same settings, whatever the home config."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "pyflowbatt.toml").write_text(
        'extra_extensions = [".mgr"]\narea_cm2 = 3\n\n[extra_patterns]\ngcpl = ["*_GCD_*"]\n'
    )
    sample = tmp_path / "project" / "sample_01"
    sample.mkdir(parents=True)
    (sample.parent / "pyflowbatt.toml").write_text(
        'include_files = ["*.txt"]\nsummary_n_cycles = [5, 10]\n\n[eis]\nmodel = "R0-(R1,CPE1)"\n'
    )
    (sample / "pyflowbatt.toml").write_text(
        'sample_name = "cell \\"A\\""\nlsv_threshold = 4\n\n'
        '[eis_tags]\n"pre-20%SOC" = ["*_03_PEIS_*"]\n\n[cv]\nv_max = 0.65\n'
    )
    original = PyFlowBattConfig.load(sample, home=home)
    resolved = original.to_resolved_toml()

    unpacked = tmp_path / "unpacked" / "sample_01"
    unpacked.mkdir(parents=True)
    (unpacked / "pyflowbatt.toml").write_text(resolved, encoding="utf-8")
    other_home = tmp_path / "other_home"
    other_home.mkdir()
    (other_home / "pyflowbatt.toml").write_text(
        'assembled_resistance_ohm = 5\nextra_extensions = [".mpt"]\n\n[eis_tags]\n"post" = ["*"]\n'
    )

    assert PyFlowBattConfig.load(unpacked, home=other_home) == original
    assert resolved.startswith(f"## Settings PyFlowBatt {__version__} used here")
    assert "inherit = false" in resolved
    assert "area_cm2 = 3.0" in resolved


def test_find_included_files_stays_inside_folder(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Patterns reaching outside the folder, or absolute ones, are ignored with a warning."""
    from pyflowbatt.analysis import find_included_files

    root = tmp_path / "project"
    sample = root / "sample_01"
    (sample / "sub").mkdir(parents=True)
    (sample / "note.txt").write_text("x")
    (root / "shared.txt").write_text("x")
    (tmp_path / "outside.txt").write_text("x")
    patterns = [
        "sub/../note.txt",
        "../shared.txt",
        "../../outside.txt",
        str(tmp_path / "outside.txt"),
    ]

    with caplog.at_level("WARNING", logger="pyflowbatt"):
        found = find_included_files(sample, patterns)

    assert found == [sample / "note.txt"]
    messages = [r.getMessage() for r in caplog.records]
    assert sum("is outside" in m for m in messages) == 2
    assert sum("must be relative" in m for m in messages) == 1


def test_zip_folder_never_writes_entries_outside_the_zip_root(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Absolute or ".." entry names are refused, whether from files or generated text."""
    from pyflowbatt.analysis import zip_folder

    root = tmp_path / "project"
    root.mkdir()
    (root / "ok.txt").write_text("x")
    (tmp_path / "evil.txt").write_text("x")

    zip_path = tmp_path / "out.zip"
    with caplog.at_level("WARNING", logger="pyflowbatt"):
        zip_folder(
            root,
            zip_path,
            ["ok.txt", "../evil.txt", "C:/evil.txt", "/evil.txt"],
            generated={"a/../../evil.toml": "x", "settings.toml": "x"},
        )

    with zipfile.ZipFile(zip_path) as zf:
        assert sorted(zf.namelist()) == ["ok.txt", "settings.toml"]
    assert "zip entries must stay inside the zipped folder" in caplog.text


def test_include_files_adds_up_across_configs(tmp_path: Path) -> None:
    """include_files patterns from parent and sample configs are combined."""
    sample = tmp_path / "project" / "sample_01"
    sample.mkdir(parents=True)
    (sample.parent / "pyflowbatt.toml").write_text('include_files = ["*.txt"]\n')
    (sample / "pyflowbatt.toml").write_text('include_files = ["photos/*.jpg", "*.txt"]\n')

    config = PyFlowBattConfig.load(sample, home=tmp_path / "home")
    assert config.include_files == ["*.txt", "photos/*.jpg"]


def test_analyse_all_samples_zips_included_and_described_files_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """include_files are described in BattINFO and the crate and zipped, strays are skipped."""
    from pyflowbatt import analysis as analysis_module

    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", _fake_convert)
    root = tmp_path / "project"
    sample = _write_synthetic_sample(root / "sample_01")
    (sample / "metadata.xlsx").write_text("x")
    (sample / "pyflowbatt.toml").write_text('include_files = ["photos/*.jpg"]\n')
    (sample / "photos").mkdir()
    (sample / "photos" / "cell.jpg").write_bytes(b"jpg")
    (sample / "old.bak").write_text("x")
    (root / "pyflowbatt.toml").write_text('include_files = ["README.md"]\n')
    (root / "README.md").write_text("readme")

    with caplog.at_level("WARNING", logger="pyflowbatt"):
        analysis_module.analyse_all_samples(root, save_format=None, zip_output=True)

    with zipfile.ZipFile(tmp_path / "project.zip") as zf:
        names = set(zf.namelist())
        crate = json.loads(zf.read("ro-crate-metadata.json"))
    assert {"sample_01/photos/cell.jpg", "README.md"} <= names
    assert "sample_01/old.bak" not in names
    assert "sample_01/old.bak" in caplog.text

    entities = {e["@id"]: e for e in crate["@graph"]}
    photo = entities["sample_01/photos/cell.jpg"]
    assert photo["description"] == "Additional file included with the dataset"
    assert "wasDerivedFrom" not in photo
    assert "README.md" in entities

    metadata = json.loads((sample / "metadata.empa__fcid123456.json").read_text())
    outputs = metadata["hasOutput"]["dcat:distribution"]
    photo_node = next(d for d in outputs if d["@id"] == "sample_01/photos/cell.jpg")
    assert photo_node["@type"] == "dcat:Distribution"
    assert "csvw:tableSchema" not in photo_node


def test_analyse_all_samples_zips_resolved_settings_without_touching_originals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The zip holds resolved pyflowbatt.toml files that reproduce each folder's settings."""
    from pyflowbatt import analysis as analysis_module

    home = tmp_path / "home"
    home.mkdir()
    (home / "pyflowbatt.toml").write_text("area_cm2 = 3\n")
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", _fake_convert)
    root = tmp_path / "project"
    sample = _write_synthetic_sample(root / "group" / "sample_01")
    (sample / "metadata.xlsx").write_text("x")
    sample_toml = "lsv_threshold = 4\n"
    (sample / "pyflowbatt.toml").write_text(sample_toml)
    (root / "group" / "pyflowbatt.toml").write_text("assembled_resistance_ohm = 1000\n")
    original = analysis_module.PyFlowBattConfig.load(sample)

    with caplog.at_level("WARNING", logger="pyflowbatt"):
        analysis_module.analyse_all_samples(root, save_format=None, zip_output=True)

    assert (sample / "pyflowbatt.toml").read_text() == sample_toml
    assert "pyflowbatt.toml" not in caplog.text
    with zipfile.ZipFile(tmp_path / "project.zip") as zf:
        names = set(zf.namelist())
        crate = json.loads(zf.read("ro-crate-metadata.json"))
        zf.extractall(tmp_path / "unpacked")
    assert {"group/sample_01/pyflowbatt.toml", "pyflowbatt.toml"} <= names
    assert "group/pyflowbatt.toml" not in names
    entities = {e["@id"]: e for e in crate["@graph"]}
    assert "pyflowbatt.toml" in entities["group/sample_01/pyflowbatt.toml"]["description"]
    metadata = json.loads((sample / "metadata.empa__fcid123456.json").read_text())
    assert "group/sample_01/pyflowbatt.toml" in _all_ids(metadata["hasInput"])

    other_home = tmp_path / "other_home"
    other_home.mkdir()
    (other_home / "pyflowbatt.toml").write_text("lsv_threshold = 12\n")
    unpacked_sample = tmp_path / "unpacked" / "group" / "sample_01"
    assert analysis_module.PyFlowBattConfig.load(unpacked_sample, home=other_home) == original


def test_sample_id_custom_pattern(tmp_path: Path) -> None:
    """sample_name_pattern in pyflowbatt.toml replaces the default sample-ID regex."""
    from pyflowbatt.analysis import get_sampleid_from_folderpath

    sample_dir = tmp_path / "ABC-123"
    sample_dir.mkdir()
    (sample_dir / "pyflowbatt.toml").write_text('sample_name_pattern = "^[A-Z]+-\\\\d+$"\n')

    config = PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")
    assert config.sample_name_pattern == r"^[A-Z]+-\d+$"
    assert get_sampleid_from_folderpath(sample_dir, config) == "ABC-123"


def test_area_cm2_default(tmp_path: Path) -> None:
    """Without a pyflowbatt.toml, area_cm2 stays unset (None) so BattINFO/default can resolve it."""
    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    config = PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")
    assert config.area_cm2 is None


def test_area_cm2_toml_override(tmp_path: Path) -> None:
    """area_cm2 in pyflowbatt.toml overrides the default electrode area."""
    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    (sample_dir / "pyflowbatt.toml").write_text("area_cm2 = 3.14\n")

    config = PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")
    assert config.area_cm2 == 3.14


def test_area_cm2_bad_value_ignored(tmp_path: Path) -> None:
    """A non-numeric area_cm2 in pyflowbatt.toml is ignored, keeping the None sentinel."""
    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    (sample_dir / "pyflowbatt.toml").write_text('area_cm2 = "not-a-number"\n')

    config = PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")
    assert config.area_cm2 is None


def _make_raw_battinfo_json(
    *,
    pos_area: float | None = None,
    neg_area: float | None = None,
    unit: str = "unit:CentiM2",
    resistance_value: float | str | None = None,
    resistance_unit: str = "unit:OHM",
) -> dict:
    """Build a minimal raw BattINFO jsonld dict with optional electrode Area / resistance entries."""

    def electrode(area: float | None) -> dict:
        properties = []
        if area is not None:
            properties.append(
                {
                    "@type": "Area",
                    "hasNumericalPart": {"@type": "emmo:RealData", "hasNumberValue": area},
                    "hasMeasurementUnit": unit,
                }
            )
        return {"hasSubstrate": {"hasMeasuredProperty": properties}}

    raw_json = {
        "@context": {},
        "@type": "RedoxFlowBattery",
        "schema:productID": "FCID1",
        "schema:name": "battinfo-name",
        "hasPositiveElectrode": electrode(pos_area),
        "hasNegativeElectrode": electrode(neg_area),
    }
    if resistance_value is not None:
        raw_json["hasMeasuredProperty"] = {
            "@type": "ElectricResistance",
            "hasNumericalPart": {"@type": "emmo:RealData", "hasNumberValue": resistance_value},
            "hasMeasurementUnit": resistance_unit,
        }
    return raw_json


def _wrap_in_battery_test(raw_json: dict) -> dict:
    """Nest a cell in a BatteryTest."""
    cell = dict(raw_json)
    return {
        "@context": cell.pop("@context"),
        "@type": "BatteryTest",
        "hasTestObject": cell,
        "hasOutput": {"@type": ["BatteryTestResult", "dcat:Dataset"], "dcterms:title": "x"},
    }


def test_get_area_cm2_default_no_toml_no_battinfo() -> None:
    """With neither pyflowbatt.toml nor BattINFO, area_cm2 falls back to DEFAULT_AREA_CM2."""
    from pyflowbatt.analysis import get_area_cm2
    from pyflowbatt.config import DEFAULT_AREA_CM2

    assert get_area_cm2(PyFlowBattConfig()) == DEFAULT_AREA_CM2


def test_get_area_cm2_from_battinfo_when_electrodes_agree() -> None:
    """When positive and negative electrode areas agree, that value is used."""
    from pyflowbatt.analysis import get_area_cm2

    raw_json = _make_raw_battinfo_json(pos_area=5.0, neg_area=5.0)
    assert get_area_cm2(PyFlowBattConfig(), raw_json) == 5.0


def test_get_area_cm2_from_battinfo_picks_smaller_when_electrodes_disagree() -> None:
    """When positive and negative electrode areas disagree, the smaller one is used."""
    from pyflowbatt.analysis import get_area_cm2

    raw_json = _make_raw_battinfo_json(pos_area=5.0, neg_area=4.0)
    assert get_area_cm2(PyFlowBattConfig(), raw_json) == 4.0


def test_get_area_cm2_from_legacy_substrate_key() -> None:
    """Old schemas had "Substrate" not "hasSubstrate" keys."""
    from pyflowbatt.analysis import get_area_cm2

    raw_json = _make_raw_battinfo_json(pos_area=4.0, neg_area=4.0)
    for electrode in ("hasPositiveElectrode", "hasNegativeElectrode"):
        raw_json[electrode]["Substrate"] = raw_json[electrode].pop("hasSubstrate")
    assert get_area_cm2(PyFlowBattConfig(), raw_json) == 4.0


def test_get_area_cm2_ignores_wrong_unit() -> None:
    """A BattINFO Area entry in a unit other than CentiM2 is ignored."""
    from pyflowbatt.analysis import get_area_cm2
    from pyflowbatt.config import DEFAULT_AREA_CM2

    raw_json = _make_raw_battinfo_json(pos_area=5.0, neg_area=5.0, unit="unit:MicroM2")
    assert get_area_cm2(PyFlowBattConfig(), raw_json) == DEFAULT_AREA_CM2


def test_get_area_cm2_toml_wins_when_battinfo_agrees() -> None:
    """An explicit pyflowbatt.toml area_cm2 that agrees with BattINFO is accepted."""
    from pyflowbatt.analysis import get_area_cm2

    raw_json = _make_raw_battinfo_json(pos_area=3.14, neg_area=3.14)
    config = PyFlowBattConfig(area_cm2=3.14)
    assert get_area_cm2(config, raw_json) == 3.14


def test_get_area_cm2_toml_battinfo_conflict_raises() -> None:
    """An explicit pyflowbatt.toml area_cm2 that disagrees with BattINFO is an error."""
    from pyflowbatt.analysis import get_area_cm2

    raw_json = _make_raw_battinfo_json(pos_area=5.0, neg_area=5.0)
    config = PyFlowBattConfig(area_cm2=3.14)
    with pytest.raises(ValueError, match="Electrode area mismatch"):
        get_area_cm2(config, raw_json)


def test_analyse_sample_uses_battinfo_area_for_lsv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """analyse_sample resolves area_cm2 from BattINFO electrodes when no toml override exists."""
    import pandas as pd

    from pyflowbatt import analysis as analysis_module

    sample = tmp_path / "sample"
    sample.mkdir()
    (sample / "sample_03_LSV_A.mpr").write_text("x")
    (sample / "sample_13_LSV_B.mpr").write_text("x")
    (sample / "metadata.xlsx").write_text("x")  # content irrelevant; conversion is stubbed

    raw_json = _make_raw_battinfo_json(pos_area=4.0, neg_area=4.0)
    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", lambda _file: raw_json)

    captured_areas: list[float] = []

    def fake_analyse(filepath: Path, area_cm2: float = 5) -> dict:
        captured_areas.append(area_cm2)
        return {
            "Fit cutoff current / A": 0.0,
            "Intercept / A": 0.0,
            "Slope / Ω⁻¹": 1.0,
            "Resistance / Ω": 1.0,
            "Area / cm²": area_cm2,
            "Area specific resistance / Ω cm²": area_cm2,
            "File name": Path(filepath).name,
        }

    def fake_plot(df: pd.DataFrame, results: dict) -> tuple:
        import matplotlib.pyplot as plt

        return plt.subplots()

    monkeypatch.setattr(analysis_module, "read_to_bdf", lambda file: file)
    monkeypatch.setattr(analysis_module.lsv, "analyse", fake_analyse)
    monkeypatch.setattr(analysis_module.lsv, "plot", fake_plot)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    analysis_module.analyse_sample(sample, save_format=None, config=config)

    assert captured_areas == [4.0, 4.0]


def test_analyse_sample_raises_on_toml_battinfo_area_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """analyse_sample raises when pyflowbatt.toml area_cm2 disagrees with BattINFO electrodes."""
    from pyflowbatt import analysis as analysis_module

    sample = tmp_path / "sample"
    sample.mkdir()
    (sample / "metadata.xlsx").write_text("x")
    (sample / "pyflowbatt.toml").write_text("area_cm2 = 3.14\n")

    raw_json = _make_raw_battinfo_json(pos_area=5.0, neg_area=5.0)
    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", lambda _file: raw_json)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    with pytest.raises(ValueError, match="Electrode area mismatch"):
        analysis_module.analyse_sample(sample, save_format=None, config=config)


def test_get_assembled_resistance_ohm_default_no_toml_no_battinfo_no_filename() -> None:
    """With no toml, no BattINFO, and no fallback filename, the result is NaN."""
    import math

    from pyflowbatt.analysis import get_assembled_resistance_ohm

    assert math.isnan(get_assembled_resistance_ohm(PyFlowBattConfig()))


def test_get_assembled_resistance_ohm_from_filename_fallback() -> None:
    """With no toml or BattINFO, the value is parsed out of the fallback filename."""
    from pyflowbatt.analysis import get_assembled_resistance_ohm

    value = get_assembled_resistance_ohm(
        PyFlowBattConfig(), fallback_filename="sample_25kOhm_04_GCPL_CE4"
    )
    assert value == 25000.0


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("unit:OHM", 25000.0),
        ("unit:KiloOHM", 25000000.0),
        ("unit:MegaOHM", 25000000000.0),
        ("Ohm", 25000.0),
        ("KiloOhm", 25000000.0),
        ("MegaOhm", 25000000000.0),
    ],
)
def test_get_assembled_resistance_ohm_from_battinfo_unit_variants(
    unit: str, expected: float
) -> None:
    """All six unit spellings/prefixes convert to ohms correctly."""
    from pyflowbatt.analysis import get_assembled_resistance_ohm

    raw_json = _make_raw_battinfo_json(resistance_value="25000", resistance_unit=unit)
    assert get_assembled_resistance_ohm(PyFlowBattConfig(), raw_json) == expected


def test_get_assembled_resistance_ohm_ignores_unrecognized_unit() -> None:
    """A BattINFO ElectricResistance with an unrecognized unit falls through to the filename."""
    from pyflowbatt.analysis import get_assembled_resistance_ohm

    raw_json = _make_raw_battinfo_json(resistance_value="25000", resistance_unit="unit:VOLT")
    value = get_assembled_resistance_ohm(
        PyFlowBattConfig(), raw_json, fallback_filename="sample_9kOhm_04_GCPL_CE4"
    )
    assert value == 9000.0


def test_get_assembled_resistance_ohm_toml_wins_when_battinfo_agrees() -> None:
    """An explicit pyflowbatt.toml assembled_resistance_ohm that agrees with BattINFO is fine."""
    from pyflowbatt.analysis import get_assembled_resistance_ohm

    raw_json = _make_raw_battinfo_json(resistance_value="25000", resistance_unit="unit:OHM")
    config = PyFlowBattConfig(assembled_resistance_ohm=25000.0)
    assert get_assembled_resistance_ohm(config, raw_json) == 25000.0


def test_get_assembled_resistance_ohm_toml_battinfo_conflict_raises() -> None:
    """An explicit pyflowbatt.toml assembled_resistance_ohm disagreeing with BattINFO errors."""
    from pyflowbatt.analysis import get_assembled_resistance_ohm

    raw_json = _make_raw_battinfo_json(resistance_value="25000", resistance_unit="unit:OHM")
    config = PyFlowBattConfig(assembled_resistance_ohm=9000.0)
    with pytest.raises(ValueError, match="Assembled resistance mismatch"):
        get_assembled_resistance_ohm(config, raw_json)


def test_analyse_sample_uses_battinfo_resistance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """analyse_sample resolves the summary's Assembled resistance from BattINFO."""
    import pandas as pd

    from pyflowbatt import analysis as analysis_module

    sample = tmp_path / "sample"
    sample.mkdir()
    (sample / "metadata.xlsx").write_text("x")  # content irrelevant; conversion is stubbed

    raw_json = _make_raw_battinfo_json(resistance_value="25000", resistance_unit="unit:KiloOHM")
    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", lambda _file: raw_json)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    analysis_module.analyse_sample(sample, save_format=None, config=config)

    df = pd.read_excel(sample / "results" / "summary.xlsx", sheet_name="Summary", index_col=0)
    assert df.loc["Assembled resistance / Ω", "Value"] == 25000000.0


def test_analyse_sample_raises_on_toml_battinfo_resistance_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """analyse_sample raises when toml assembled_resistance_ohm disagrees with BattINFO."""
    from pyflowbatt import analysis as analysis_module

    sample = tmp_path / "sample"
    sample.mkdir()
    (sample / "metadata.xlsx").write_text("x")
    (sample / "pyflowbatt.toml").write_text("assembled_resistance_ohm = 9000\n")

    raw_json = _make_raw_battinfo_json(resistance_value="25000", resistance_unit="unit:OHM")
    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", lambda _file: raw_json)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    with pytest.raises(ValueError, match="Assembled resistance mismatch"):
        analysis_module.analyse_sample(sample, save_format=None, config=config)


def test_analyse_sample_reads_cell_nested_in_battery_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ID, name, area and resistance are read from a cell nested in a BatteryTest."""
    import pandas as pd

    from pyflowbatt import analysis as analysis_module

    sample = tmp_path / "not-a-normal-sample-name"
    sample.mkdir()
    (sample / "sample_03_LSV_A.mpr").write_text("x")
    (sample / "metadata.xlsx").write_text("x")  # content irrelevant; conversion is stubbed

    raw_json = _make_raw_battinfo_json(
        pos_area=4.0,
        neg_area=4.0,
        unit="SquareCentiMetre",
        resistance_value=25000,
        resistance_unit="Ohm",
    )
    nested = _wrap_in_battery_test(raw_json)
    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", lambda _file: nested)

    captured_areas: list[float] = []

    def fake_analyse(filepath: Path, area_cm2: float = 5) -> dict:
        captured_areas.append(area_cm2)
        return {
            "Fit cutoff current / A": 0.0,
            "Intercept / A": 0.0,
            "Slope / Ω⁻¹": 1.0,
            "Resistance / Ω": 1.0,
            "Area / cm²": area_cm2,
            "Area specific resistance / Ω cm²": area_cm2,
            "File name": Path(filepath).name,
        }

    def fake_plot(df: pd.DataFrame, results: dict) -> tuple:
        import matplotlib.pyplot as plt

        return plt.subplots()

    monkeypatch.setattr(analysis_module, "read_to_bdf", lambda file: file)
    monkeypatch.setattr(analysis_module.lsv, "analyse", fake_analyse)
    monkeypatch.setattr(analysis_module.lsv, "plot", fake_plot)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    _outputs, _extra_inputs, _raw_inputs, fcid, sample_id = analysis_module.analyse_sample(
        sample, save_format=None, config=config
    )

    assert fcid == "FCID1"
    assert sample_id == "battinfo-name"
    assert captured_areas == [4.0]
    df = pd.read_excel(sample / "results" / "summary.xlsx", sheet_name="Summary", index_col=0)
    assert df.loc["Assembled resistance / Ω", "Value"] == 25000.0
    metadata = json.loads((sample / "metadata.FCID1.json").read_text(encoding="utf-8"))
    assert metadata["@type"] == "BatteryTest"
    assert metadata["hasTestObject"]["schema:productID"] == "FCID1"
    assert metadata["hasOutput"]["dcterms:title"] == "x"


def test_area_cm2_passed_to_lsv_analyse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """analyse_sample threads config.area_cm2 through to the real lsv.analyse call.

    Only LSV files are created (no GCPL/OCV/CV/EIS/battinfo) so analyse_sample's other
    sections take their "nothing found, skipping" branches without needing to be stubbed.
    lsv.analyse/lsv.plot are stubbed to avoid parsing real EC-Lab binary data.
    """
    import pandas as pd

    from pyflowbatt import analysis as analysis_module

    sample = tmp_path / "sample"
    sample.mkdir()
    (sample / "sample_03_LSV_A.mpr").write_text("x")
    (sample / "sample_13_LSV_B.mpr").write_text("x")
    (sample / "pyflowbatt.toml").write_text("area_cm2 = 2.5\n")

    captured_areas: list[float] = []

    def fake_analyse(filepath: Path, area_cm2: float = 5) -> dict:
        captured_areas.append(area_cm2)
        return {
            "Fit cutoff current / A": 0.0,
            "Intercept / A": 0.0,
            "Slope / Ω⁻¹": 1.0,
            "Resistance / Ω": 1.0,
            "Area / cm²": area_cm2,
            "Area specific resistance / Ω cm²": area_cm2,
            "File name": Path(filepath).name,
        }

    def fake_plot(df: pd.DataFrame, results: dict) -> tuple:
        import matplotlib.pyplot as plt

        return plt.subplots()

    monkeypatch.setattr(analysis_module, "read_to_bdf", lambda file: file)
    monkeypatch.setattr(analysis_module.lsv, "analyse", fake_analyse)
    monkeypatch.setattr(analysis_module.lsv, "plot", fake_plot)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    assert config.area_cm2 == 2.5

    analysis_module.analyse_sample(sample, save_format=None, config=config)

    assert captured_areas == [2.5, 2.5]


def _cv_analyse_defaults() -> dict:
    """Read the default kwarg values off cv.analyse's signature."""
    import inspect

    from pyflowbatt import cv as cv_module

    sig = inspect.signature(cv_module.analyse)
    return {
        name: param.default
        for name, param in sig.parameters.items()
        if param.default is not inspect.Parameter.empty
    }


def test_cv_config_defaults_match_cv_module() -> None:
    """Without a pyflowbatt.toml, the [cv] settings match cv.analyse's own defaults."""
    from pyflowbatt import cv as cv_module

    config = PyFlowBattConfig()
    defaults = _cv_analyse_defaults()
    assert config.cv_v_min == defaults["v_min"]
    assert config.cv_v_max == defaults["v_max"]
    assert config.cv_v_med == defaults["v_med"]
    assert config.cv_v_range == defaults["v_range"]
    assert config.cv_min_r2 == defaults["min_r2"] == cv_module.MIN_R2


def test_cv_config_toml_override(tmp_path: Path) -> None:
    """[cv] settings in pyflowbatt.toml override the defaults."""
    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    (sample_dir / "pyflowbatt.toml").write_text(
        "[cv]\nv_min = 0.1\nv_max = 0.5\nv_med = 0.3\nv_range = 0.01\nmin_r2 = 0.9\n"
    )

    config = PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")
    assert config.cv_v_min == 0.1
    assert config.cv_v_max == 0.5
    assert config.cv_v_med == 0.3
    assert config.cv_v_range == 0.01
    assert config.cv_min_r2 == 0.9


def test_cv_config_bad_value_ignored(tmp_path: Path) -> None:
    """A non-numeric [cv] value is ignored, keeping the default, without affecting other keys."""
    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    (sample_dir / "pyflowbatt.toml").write_text('[cv]\nv_min = "not-a-number"\nv_max = 0.5\n')

    config = PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")
    assert config.cv_v_min == 0.4004  # unchanged default
    assert config.cv_v_max == 0.5  # valid override still applied


def test_analyse_sample_passes_cv_config_to_cv_analyse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """analyse_sample threads the [cv] config values through to the real cv.analyse call."""
    import pandas as pd

    from pyflowbatt import analysis as analysis_module

    sample = tmp_path / "sample"
    sample.mkdir()
    (sample / "sample_CVApre_A.mpr").write_text("x")
    (sample / "sample_CVApost_A.mpr").write_text("x")
    (sample / "pyflowbatt.toml").write_text(
        "[cv]\nv_min = 0.1\nv_max = 0.5\nv_med = 0.3\nv_range = 0.01\n"
    )

    captured_kwargs: list[dict] = []

    def fake_analyse(filepath: Path, **kwargs: float) -> tuple[pd.DataFrame, pd.DataFrame, float]:
        captured_kwargs.append(kwargs)
        cv_df = pd.DataFrame({"CV Cycle": [1]})
        df = pd.DataFrame({"CV Cycle / 1": [1]})
        return df, cv_df, 1.0

    def fake_plot(df: pd.DataFrame, cv_df: pd.DataFrame, min_r2: float = 0.8) -> tuple:
        import matplotlib.pyplot as plt

        return plt.subplots()

    monkeypatch.setattr(analysis_module, "read_to_bdf", lambda file: file)
    monkeypatch.setattr(analysis_module.cv, "analyse", fake_analyse)
    monkeypatch.setattr(analysis_module.cv, "plot", fake_plot)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    analysis_module.analyse_sample(sample, save_format=None, config=config)

    assert captured_kwargs == [
        {"v_min": 0.1, "v_max": 0.5, "v_med": 0.3, "v_range": 0.01, "min_r2": 0.8},
        {"v_min": 0.1, "v_max": 0.5, "v_med": 0.3, "v_range": 0.01, "min_r2": 0.8},
    ]


def test_summary_n_cycles_default() -> None:
    """Without a pyflowbatt.toml, summary_n_cycles keeps the historical default list."""
    config = PyFlowBattConfig()
    assert config.summary_n_cycles == [10, 20, 30, 40, 50]


def test_summary_n_cycles_toml_override(tmp_path: Path) -> None:
    """summary_n_cycles in pyflowbatt.toml replaces the default list."""
    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    (sample_dir / "pyflowbatt.toml").write_text("summary_n_cycles = [5, 10]\n")

    config = PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")
    assert config.summary_n_cycles == [5, 10]


def test_summary_n_cycles_bad_value_ignored(tmp_path: Path) -> None:
    """A non-list-of-numbers summary_n_cycles in pyflowbatt.toml is ignored."""
    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    (sample_dir / "pyflowbatt.toml").write_text('summary_n_cycles = ["a", "b"]\n')

    config = PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")
    assert config.summary_n_cycles == [10, 20, 30, 40, 50]


def test_analyse_sample_summary_uses_custom_n_cycles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """analyse_sample's summary sheet reports exactly the configured summary_n_cycles."""
    import pandas as pd

    from pyflowbatt import analysis as analysis_module

    sample = tmp_path / "sample"
    sample.mkdir()
    (sample / "sample_04_GCPL_A.mpr").write_text("x")
    (sample / "pyflowbatt.toml").write_text("summary_n_cycles = [5, 10]\n")

    cycle_counts = list(range(1, 15))
    cycle_df = pd.DataFrame(
        {
            "Cycle Count / 1": cycle_counts,
            "Coulombic Efficiency / %": [95.0] * len(cycle_counts),
            "Energy Efficiency / %": [90.0] * len(cycle_counts),
            "Voltage Efficiency / %": [94.0] * len(cycle_counts),
            "Charge Capacity / mAh": [10.0] * len(cycle_counts),
            "Discharge Capacity / mAh": [9.5] * len(cycle_counts),
        }
    )

    def fake_gcpl_analyse(_filepaths: list) -> tuple[pd.DataFrame, pd.DataFrame]:
        return pd.DataFrame({"Voltage / V": [0.0]}), cycle_df

    def fake_gcpl_plot(_df: pd.DataFrame, _cycle_df: pd.DataFrame) -> tuple:
        import matplotlib.pyplot as plt

        return plt.subplots()

    def fake_cycles_to_ratetest(_cycle_df: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame({"Times seen": [1]})

    monkeypatch.setattr(analysis_module, "read_to_bdf", lambda file: file)
    monkeypatch.setattr(analysis_module.gcpl, "analyse", fake_gcpl_analyse)
    monkeypatch.setattr(analysis_module.gcpl, "plot", fake_gcpl_plot)
    monkeypatch.setattr(analysis_module.gcpl, "cycles_to_ratetest", fake_cycles_to_ratetest)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    analysis_module.analyse_sample(sample, save_format=None, config=config)

    df = pd.read_excel(sample / "results" / "summary.xlsx", sheet_name="Summary")
    quantities = set(df["Quantity"])
    assert "5 cycles avg. CE / %" in quantities
    assert "10 cycles avg. CE / %" in quantities
    assert "20 cycles avg. CE / %" not in quantities
    assert "30 cycles avg. CE / %" not in quantities


def test_cascade_priority(tmp_path: Path) -> None:
    """Test priority of settings.

    home -> parent -> sample-folder configs merge in ascending priority.
    """
    home = tmp_path / "home"
    parent = tmp_path / "parent"
    sample = parent / "sample"
    home.mkdir()
    sample.mkdir(parents=True)

    (home / "pyflowbatt.toml").write_text('[extra_patterns]\ngcpl = ["*_FROM_HOME_*"]\n')
    (parent / "pyflowbatt.toml").write_text('[extra_patterns]\ngcpl = ["*_FROM_PARENT_*"]\n')
    (sample / "pyflowbatt.toml").write_text('[extra_patterns]\ngcpl = ["*_FROM_SAMPLE_*"]\n')

    config = PyFlowBattConfig.load(sample, home=home)
    assert config.gcpl_patterns == [
        "*_GCPL_*",
        "*_FROM_HOME_*",
        "*_FROM_PARENT_*",
        "*_FROM_SAMPLE_*",
    ]


def test_eis_model_default_matches_eis_module() -> None:
    """Without a pyflowbatt.toml, the EIS model is eis.DEFAULT_MODEL."""
    from pyflowbatt import eis

    assert PyFlowBattConfig().eis_model == eis.DEFAULT_MODEL


def test_eis_model_toml_override(tmp_path: Path) -> None:
    """[eis] model in pyflowbatt.toml accepts any string."""
    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    (sample_dir / "pyflowbatt.toml").write_text('[eis]\nmodel = "R0-(R1,CPE1)-Wo1"\n')

    config = PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")
    assert config.eis_model == "R0-(R1,CPE1)-Wo1"


def test_eis_model_bad_value_ignored(tmp_path: Path) -> None:
    """A non-string [eis] model is ignored, keeping the default."""
    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    (sample_dir / "pyflowbatt.toml").write_text("[eis]\nmodel = 5\n")

    config = PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")
    assert config.eis_model == "L0-R0-(R1,CPE1)-(R2,CPE2)"


@pytest.mark.parametrize(
    ("model", "summary_params", "has_ml", "warnings"),
    [
        ("L0-R0-(R1,CPE1)-(R2,CPE2)", ["R0.r"], True, []),
        ("two_rq_l", ["R0.r", "CPE2.alpha"], True, []),
        ("(R1,CPE1)-R0", ["R0.r"], True, []),
        ("R1-(R2,CPE2)", ["R0.r"], True, ["R0.r is not in model"]),
        ("R0-(R1,CPE1)-W2-Wo3", ["R0.r"], False, ["not in the fasteis ML model library"]),
        (
            "R1-(R2,CPE2)-W3-Wo4",
            ["R0.r", "Wo4.tau"],
            False,
            ["R0.r is not in model", "not in the fasteis ML model library"],
        ),
    ],
)
def test_eis_check_model_warnings(
    model: str,
    summary_params: list[str],
    has_ml: bool,
    warnings: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """check_model warns about missing summary params and a missing ML model."""
    from pyflowbatt import eis

    with caplog.at_level("WARNING", logger="pyflowbatt.eis"):
        units, ml = eis.check_model(model, summary_params)
    assert ml is has_ml
    assert len(units) > 0
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == len(warnings)
    for message, expected in zip(messages, warnings, strict=True):
        assert expected in message
        assert eis.MODELS_URL in message


def test_eis_check_model_invalid_raises() -> None:
    """An invalid circuit string raises ValueError."""
    from pyflowbatt import eis

    with pytest.raises(ValueError):
        eis.check_model("R0-(R1", ["R0.r"])


def test_eis_analyse_custom_model_without_ml_guess() -> None:
    """A model outside the ML library still fits, with columns named by the circuit."""
    import fasteis
    import numpy as np
    import pandas as pd

    from pyflowbatt import eis

    model = "R0-(R1,CPE1)-Wo1"
    f = np.logspace(5, -1, 40)
    Z = fasteis.Circuit(model).with_values([1.0, 2.0, 1e-3, 0.9, 3.0, 10.0]).impedance(f)
    df = pd.DataFrame(
        {"Frequency / Hz": f, "Real Impedance / ohm": Z.real, "Imaginary Impedance / ohm": Z.imag}
    )
    params, Z_fit = eis.analyse(df, model=model, guess_init=False)
    assert list(params) == ["R0.r", "R1.r", "CPE1.q", "CPE1.alpha", "Wo1.z0", "Wo1.tau"]
    assert len(Z_fit) == len(f)


def test_eis_summary_params_toml_override(tmp_path: Path) -> None:
    """[eis] summary_params replaces the default list, and bad values are ignored."""
    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    home = tmp_path / "home"
    toml = sample_dir / "pyflowbatt.toml"
    default = ["R0.r", "R1.r", "R2.r"]
    assert PyFlowBattConfig.load(sample_dir, home=home).eis_summary_params == default

    toml.write_text('[eis]\nsummary_params = ["R0.r", "R1.r"]\n')
    assert PyFlowBattConfig.load(sample_dir, home=home).eis_summary_params == ["R0.r", "R1.r"]

    toml.write_text('[eis]\nsummary_params = "R1.r"\n')
    assert PyFlowBattConfig.load(sample_dir, home=home).eis_summary_params == default


def test_eis_summary_label_pretty_units() -> None:
    """Summary labels use display units, passing unknown unit strings through."""
    from pyflowbatt import eis

    assert eis.summary_label("R0.r", "pre", "ohm") == "EIS R0.r pre / Ω"
    assert eis.summary_label("CPE1.alpha", "post", "-") == "EIS CPE1.alpha post / 1"
    assert eis.summary_label("X1.y", "pre", "odd") == "EIS X1.y pre / odd"


def test_analyse_sample_summary_reports_every_eis_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every summary param gets a row per standard tag plus each extra fitted tag."""
    import numpy as np
    import pandas as pd

    from pyflowbatt import analysis as analysis_module

    sample = _write_two_eis_sample(tmp_path / "sample")
    (sample / "s_07_PEIS_A.mpr").write_text("x")
    (sample / "pyflowbatt.toml").write_text(
        '[eis_tags]\n"pre-20%SOC" = ["*_07_PEIS_*"]\n'
        '[eis]\nsummary_params = ["R0.r", "CPE1.alpha", "Missing.x"]\n'
    )
    fitted = {"s_02_PEIS_A": 1.0, "s_06_PEIS_A": 2.0, "s_07_PEIS_A": 3.0}

    def fake_analyse(_df: object, label: str, **_kwargs: object) -> tuple[dict, np.ndarray]:
        r0 = fitted[label]
        params = {
            "R0.r": {"value": r0, "err": r0 / 10, "unit": "ohm"},
            "CPE1.alpha": {"value": 0.9, "err": None, "unit": "-"},
        }
        return params, np.array([1 + 1j])

    def fake_plot(_df: object, _z_fit: np.ndarray, **_kwargs: object) -> tuple:
        import matplotlib.pyplot as plt

        return plt.subplots()

    def fake_check_model(_model: str, _params: list[str]) -> tuple[dict[str, str], bool]:
        return {"R0.r": "ohm", "CPE1.alpha": "-"}, True

    monkeypatch.setattr(analysis_module, "read_to_bdf", lambda _f: pd.DataFrame({"x": [0]}))
    monkeypatch.setattr(analysis_module, "df_save_bdf", lambda *_a, **_k: None)
    monkeypatch.setattr(analysis_module.eis, "analyse", fake_analyse)
    monkeypatch.setattr(analysis_module.eis, "plot", fake_plot)
    monkeypatch.setattr(analysis_module.eis, "check_model", fake_check_model)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    analysis_module.analyse_sample(sample, save_format=None, config=config)

    df = pd.read_excel(sample / "results" / "summary.xlsx", sheet_name="Summary", index_col=0)
    eis_rows = [q for q in df.index if q.startswith("EIS")]
    assert eis_rows == [
        "EIS R0.r pre / Ω",
        "EIS R0.r pre-50%SOC / Ω",
        "EIS R0.r post-50%SOC / Ω",
        "EIS R0.r post / Ω",
        "EIS R0.r pre-20%SOC / Ω",
        "EIS CPE1.alpha pre / 1",
        "EIS CPE1.alpha pre-50%SOC / 1",
        "EIS CPE1.alpha post-50%SOC / 1",
        "EIS CPE1.alpha post / 1",
        "EIS CPE1.alpha pre-20%SOC / 1",
    ]
    assert df.loc["EIS R0.r pre / Ω", "Value"] == 1.0
    assert df.loc["EIS R0.r pre / Ω", "Error"] == 0.1
    assert df.loc["EIS R0.r pre-20%SOC / Ω", "Value"] == 3.0
    assert df.loc["EIS R0.r post / Ω", "Value"] == 2.0
    assert np.isnan(df.loc["EIS R0.r pre-50%SOC / Ω", "Value"])


def test_analyse_sample_leaves_out_rejected_eis_fit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fit rejected for its fit error is left out of the summary and EIS sheet but plotted."""
    import numpy as np
    import pandas as pd

    from pyflowbatt import analysis as analysis_module

    sample = _write_two_eis_sample(tmp_path / "sample")
    (sample / "pyflowbatt.toml").write_text("[eis]\nmax_fit_error = 0.05\n")
    max_errors = []

    def fake_analyse(
        _df: object, label: str, max_fit_error: float, **_kwargs: object
    ) -> tuple[dict | None, np.ndarray]:
        max_errors.append(max_fit_error)
        if label == "s_06_PEIS_A":
            return None, np.array([1 + 1j])
        return {"R0.r": {"value": 1.0, "err": 0.1, "unit": "ohm"}}, np.array([1 + 1j])

    def fake_plot(_df: object, _z_fit: np.ndarray, max_fit_error: float) -> tuple:
        import matplotlib.pyplot as plt

        max_errors.append(max_fit_error)
        return plt.subplots()

    monkeypatch.setattr(analysis_module, "read_to_bdf", lambda _f: pd.DataFrame({"x": [0]}))
    monkeypatch.setattr(analysis_module, "df_save_bdf", lambda *_a, **_k: None)
    monkeypatch.setattr(analysis_module.eis, "analyse", fake_analyse)
    monkeypatch.setattr(analysis_module.eis, "plot", fake_plot)
    monkeypatch.setattr(analysis_module.eis, "check_model", lambda *_a: ({"R0.r": "ohm"}, True))

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    analysis_module.analyse_sample(sample, save_format=None, config=config)

    assert max_errors == [0.05] * 4
    assert (sample / "results" / "eis_post.png").exists()
    xlsx = sample / "results" / "summary.xlsx"
    summary = pd.read_excel(xlsx, sheet_name="Summary", index_col=0)
    assert summary.loc["EIS R0.r pre / Ω", "Value"] == 1.0
    assert np.isnan(summary.loc["EIS R0.r post / Ω", "Value"])
    assert list(pd.read_excel(xlsx, sheet_name="EIS")["tag"]) == ["pre"]


def test_eis_fit_error_relative_to_modulus() -> None:
    """Fit error is the RMS of |Z_fit - Z| / |Z|, so it doesn't scale with point count."""
    import numpy as np
    import pandas as pd

    from pyflowbatt import eis

    for n in (10, 100):
        Z = np.full(n, 3 + 4j)
        df = pd.DataFrame({"Real Impedance / ohm": Z.real, "Imaginary Impedance / ohm": Z.imag})
        assert eis.fit_error(df, Z) == 0
        assert eis.fit_error(df, Z + 0.5j) == pytest.approx(0.1)


def test_eis_analyse_rejects_bad_fit(caplog: pytest.LogCaptureFixture) -> None:
    """A fit with error above max_fit_error warns and returns no parameters."""
    import fasteis
    import numpy as np
    import pandas as pd

    from pyflowbatt import eis

    f = np.logspace(5, -1, 40)
    Z = fasteis.Circuit("R0-(R1,C1)").with_values([1.0, 10.0, 1e-2]).impedance(f)
    df = pd.DataFrame(
        {"Frequency / Hz": f, "Real Impedance / ohm": Z.real, "Imaginary Impedance / ohm": Z.imag}
    )
    params, Z_fit = eis.analyse(df, model="R0-(R1,C1)", guess_init=False)
    assert params is not None
    assert eis.fit_error(df, Z_fit) < 1e-3

    with caplog.at_level(logging.WARNING):
        params, Z_fit = eis.analyse(df, label="flat", model="R0", guess_init=False)
    assert params is None
    assert len(Z_fit) == len(f)
    assert "EIS fit flat error is very high" in caplog.text


def test_eis_max_fit_error_toml_override(tmp_path: Path) -> None:
    """[eis] max_fit_error overrides the default, and a non-number is ignored."""
    from pyflowbatt import eis

    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    home = tmp_path / "home"
    toml = sample_dir / "pyflowbatt.toml"
    assert PyFlowBattConfig.load(sample_dir, home=home).eis_max_fit_error == eis.MAX_FIT_ERROR

    toml.write_text("[eis]\nmax_fit_error = 0.3\n")
    config = PyFlowBattConfig.load(sample_dir, home=home)
    assert config.eis_max_fit_error == 0.3
    assert "max_fit_error = 0.3" in config.to_resolved_toml()

    toml.write_text('[eis]\nmax_fit_error = "low"\n')
    assert PyFlowBattConfig.load(sample_dir, home=home).eis_max_fit_error == eis.MAX_FIT_ERROR


def test_custom_eis_tag_descriptions() -> None:
    """Custom EIS tags get descriptions naming the tag, standard ones keep theirs."""
    from pyflowbatt import analysis as analysis_module
    from pyflowbatt.rocrate_output import _measurement_technique

    label = "eis_pre-20%SOC"
    assert "tagged pre-20%SOC" in analysis_module.raw_description(label, Path("a.mpr"))
    assert "tagged pre-20%SOC" in analysis_module.plot_description(label)
    assert "tagged pre-20%SOC" in analysis_module.data_description(label)
    assert _measurement_technique(label) == "Electrochemical Impedance Spectroscopy (pre-20%SOC)"
    standard = analysis_module.OUTPUT_PLOT_DESCRIPTIONS["eis_pre"]
    assert analysis_module.plot_description("eis_pre") == standard
    assert "measurement 2" in analysis_module.plot_description("eis_post_2")


def test_eis_summary_units_from_element(caplog: pytest.LogCaptureFixture) -> None:
    """Summary units come from each parameter's own element, dropping unknown names."""
    from pyflowbatt import eis

    with caplog.at_level("WARNING", logger="pyflowbatt.eis"):
        units = eis.summary_units(["R0.r", "CPE1.alpha", "Wo3.tau", "Missing.x", "R0.x"])
    assert units == {"R0.r": "ohm", "CPE1.alpha": "-", "Wo3.tau": "s"}
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 2
    assert "Missing.x" in messages[0]
    assert "R0.x" in messages[1]


def test_analyse_sample_summary_has_eis_rows_without_eis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without EIS files or a valid model, the standard EIS rows are still in the summary."""
    import pandas as pd

    from pyflowbatt import analysis as analysis_module

    sample = _write_two_eis_sample(tmp_path / "sample")
    for f in sample.glob("*_PEIS_*"):
        f.unlink()
    (sample / "pyflowbatt.toml").write_text('[eis]\nmodel = "R0-(R1"\n')
    monkeypatch.setattr(analysis_module, "read_to_bdf", lambda _f: pd.DataFrame({"x": [0]}))
    monkeypatch.setattr(analysis_module, "df_save_bdf", lambda *_a, **_k: None)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    analysis_module.analyse_sample(sample, save_format=None, config=config)

    df = pd.read_excel(sample / "results" / "summary.xlsx", sheet_name="Summary", index_col=0)
    eis_rows = [q for q in df.index if q.startswith("EIS")]
    assert eis_rows == [
        f"EIS {param} {tag} / Ω"
        for param in ["R0.r", "R1.r", "R2.r"]
        for tag in ["pre", "pre-50%SOC", "post-50%SOC", "post"]
    ]
    assert df.loc[eis_rows, "Value"].isna().all()


def test_merge_summaries_keeps_missing_rows_in_place(tmp_path: Path) -> None:
    """Rows only some samples have go after their neighbour, not at the bottom."""
    import pandas as pd

    from pyflowbatt.analysis import merge_summaries

    rows_by_sample = {
        "a": ["OCV", "EIS R0.r pre", "EIS R0.r post", "1st CE"],
        "b": ["OCV", "EIS R0.r pre", "EIS R0.r post", "EIS R0.r pre-20%SOC", "1st CE", "Extra"],
        "c": ["New first", "OCV", "1st CE"],
    }
    paths = []
    for name, rows in rows_by_sample.items():
        path = tmp_path / name / "results" / "summary.xlsx"
        path.parent.mkdir(parents=True)
        df = pd.DataFrame({"Value": range(len(rows)), "Error": None}, index=rows)
        df.to_excel(path, sheet_name="Summary")
        paths.append(path)

    df = merge_summaries(paths, {tmp_path / name: name for name in rows_by_sample})

    assert list(df["Quantity"]) == [
        "New first",
        "OCV",
        "EIS R0.r pre",
        "EIS R0.r post",
        "EIS R0.r pre-20%SOC",
        "1st CE",
        "Extra",
    ]
    assert df.set_index("Quantity").loc["EIS R0.r pre-20%SOC", "b"] == 3
    assert pd.isna(df.set_index("Quantity").loc["EIS R0.r pre-20%SOC", "a"])


@pytest.mark.parametrize(
    ("rel_path", "frequency_domain"),
    [
        ("s/results/eis_pre.bdf.parquet", True),
        ("s/results/eis_pre-20%SOC.bdf.csv", True),
        ("s/results/s.bdf.parquet", False),
        ("weis_sample/results/weis_sample.bdf.csv", False),
    ],
)
def test_battinfo_eis_data_comment(rel_path: str, frequency_domain: bool) -> None:
    """BDF outputs from EIS are described as frequency-domain, others as time series."""
    from pyflowbatt.battinfo import add_data

    comment = add_data(rel_path, None)["hasOutput"]["dcat:distribution"]["rdfs:comment"]
    assert comment.startswith("Frequency-domain" if frequency_domain else "Time series")


DCAT_NS = "http://www.w3.org/ns/dcat#"
BDF_SCHEMA = "https://w3id.org/battery-data-alliance/ontology/battery-data-format/schema"


@pytest.mark.parametrize(
    ("rel_path", "media_type", "table_schema"),
    [
        ("s/results/gcpl.bdf.csv", "text/csv", BDF_SCHEMA),
        ("s/results/gcpl.bdf.parquet", "application/vnd.apache.parquet", BDF_SCHEMA),
        ("s/s_02_GCPL_A.mpr", "application/octet-stream", None),
    ],
)
def test_battinfo_distribution_values_are_iris(
    rel_path: str, media_type: str, table_schema: str | None
) -> None:
    """mediaType, tableSchema, and downloadURL are IRIs."""
    from rdflib import Graph, Namespace, URIRef

    from pyflowbatt.battinfo import add_data

    dcat = Namespace(DCAT_NS)
    csvw = Namespace("http://www.w3.org/ns/csvw#")
    dist = add_data(rel_path, "https://doi.org/10.5281/zenodo.12345678", zip_filename="data.zip")
    doc = {
        "@context": {"@base": "https://example.org/", "dcat": str(dcat), "csvw": str(csvw)},
        **dist["hasOutput"]["dcat:distribution"],
    }
    graph = Graph().parse(data=json.dumps(doc), format="json-ld")
    file = URIRef(f"https://example.org/{rel_path}")

    assert graph.value(file, dcat.mediaType) == URIRef(
        f"http://www.iana.org/assignments/media-types/{media_type}"
    )
    expected_schema = URIRef(table_schema) if table_schema else None
    assert graph.value(file, csvw.tableSchema) == expected_schema
    download_url = graph.value(file, dcat.downloadURL)
    assert isinstance(download_url, URIRef)
    assert str(download_url).startswith("https://zenodo.org/")


def test_battinfo_zenodo_access_urls_are_iris() -> None:
    """Zenodo accessURL and endpointURL are IRIs."""
    from rdflib import Graph, Namespace, URIRef

    from pyflowbatt.battinfo import add_zenodo_url

    dcat = Namespace(DCAT_NS)
    output = add_zenodo_url("https://doi.org/10.5281/zenodo.12345678")["hasOutput"]
    doc = {"@context": {"dcat": str(dcat)}, **output}
    graph = Graph().parse(data=json.dumps(doc), format="json-ld")

    assert list(graph.objects(None, dcat.accessURL)) == [
        URIRef("https://doi.org/10.5281/zenodo.12345678")
    ]
    assert list(graph.objects(None, dcat.endpointURL)) == [
        URIRef("https://zenodo.org/api/records/12345678")
    ]


def test_priority_merge_keeps_high_fills_gaps_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    """On a disagreement the high value wins with a warning, missing keys come from low."""
    from pyflowbatt.battinfo import priority_merge

    high = {"@type": "BatteryTest", "hasOutput": {"dcterms:issued": "2026-08-05"}}
    low = {
        "@type": "BatteryTest",
        "hasOutput": {"dcterms:issued": "2026-10-21", "schema:citation": "Cite me"},
    }
    with caplog.at_level("WARNING", logger="pyflowbatt"):
        merged = priority_merge(high, low, names=("sheet", "pub"))

    assert merged["hasOutput"] == {"dcterms:issued": "2026-08-05", "schema:citation": "Cite me"}
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1
    assert "sheet and pub disagree at hasOutput/dcterms:issued" in messages[0]


def test_priority_merge_without_names_is_silent(caplog: pytest.LogCaptureFixture) -> None:
    """Without source names, disagreements are resolved without a warning."""
    from pyflowbatt.battinfo import priority_merge

    with caplog.at_level("WARNING", logger="pyflowbatt"):
        merged = priority_merge({"a": 1}, {"a": 2})
    assert merged == {"a": 1}
    assert caplog.records == []


def test_priority_merge_keeps_high_list_whole() -> None:
    """A list from high replaces the one from low, e.g. an ordered author list."""
    from pyflowbatt.battinfo import priority_merge

    author_a = {"@id": "https://orcid.org/a", "schema:name": "A"}
    author_b = {"@id": "https://orcid.org/b", "schema:name": "B"}
    merged = priority_merge(
        {"dcterms:creator": [author_a]}, {"dcterms:creator": [author_b, author_a]}
    )
    assert merged["dcterms:creator"] == [author_a]


def test_priority_merge_nodes_by_id() -> None:
    """Nodes with the same @id are merged, a different @id keeps the high node."""
    from pyflowbatt.battinfo import priority_merge

    high = {
        "dcterms:publisher": {"@id": "http://www.wikidata.org/entity/Q683116"},
        "dcat:accessURL": {"@id": "https://doi.org/10.5281/zenodo.1"},
    }
    low = {
        "dcterms:publisher": {
            "@id": "http://www.wikidata.org/entity/Q683116",
            "schema:name": "Empa",
        },
        "dcat:accessURL": {"@id": "https://doi.org/10.5281/zenodo.2"},
    }
    merged = priority_merge(high, low)
    assert merged["dcterms:publisher"] == {
        "@id": "http://www.wikidata.org/entity/Q683116",
        "schema:name": "Empa",
    }
    assert merged["dcat:accessURL"] == {"@id": "https://doi.org/10.5281/zenodo.1"}


def test_priority_merge_combines_types_and_distributions() -> None:
    """@type and dcat:distribution are combined, files with the same @id merge into one node."""
    from pyflowbatt.battinfo import priority_merge

    high = {
        "@type": ["BatteryTestResult"],
        "dcat:distribution": [{"@id": "a.csv", "rdfs:comment": "From the sheet"}],
    }
    low = {
        "@type": "dcat:Dataset",
        "dcat:distribution": [
            {"@id": "a.csv", "rdfs:comment": "From pyflowbatt", "dcat:downloadURL": {"@id": "u"}},
            {"@id": "b.csv"},
        ],
    }
    merged = priority_merge(high, low)
    assert merged["@type"] == ["BatteryTestResult", "dcat:Dataset"]
    assert merged["dcat:distribution"] == [
        {"@id": "a.csv", "rdfs:comment": "From the sheet", "dcat:downloadURL": {"@id": "u"}},
        {"@id": "b.csv"},
    ]


@pytest.mark.parametrize(
    ("sheet_url", "pub_info_url", "expected"),
    [
        ("https://doi.org/10.5281/zenodo.1", None, "https://doi.org/10.5281/zenodo.1"),
        (None, "https://doi.org/10.5281/zenodo.2", "https://doi.org/10.5281/zenodo.2"),
        (
            "https://doi.org/10.5281/zenodo.1",
            "https://doi.org/10.5281/zenodo.1",
            "https://doi.org/10.5281/zenodo.1",
        ),
        (None, None, None),
    ],
)
def test_resolve_zenodo_url(
    sheet_url: str | None, pub_info_url: str | None, expected: str | None
) -> None:
    """The sheet's Dataset URL is used first, then publication info."""
    from pyflowbatt.battinfo import resolve_zenodo_url

    sheet = {"hasOutput": {"dcat:accessURL": {"@id": sheet_url}}} if sheet_url else {}
    assert resolve_zenodo_url(sheet, pub_info_url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://doi.org/10.5281/zenodo.21805277",
        "https://doi.org/10.5281/zenodo.21805277/",
        "https://zenodo.org/records/21805277",
        "https://zenodo.org/records/21805277?preview=1",
        "https://zenodo.org/record/21805277",
        "https://zenodo.org/doi/10.5281/zenodo.21805277",
    ],
)
def test_zenodo_record_id_from_doi_and_record_urls(url: str) -> None:
    """DOIs and record page URLs give the same record number and @base."""
    from pyflowbatt.battinfo import add_zenodo_url, zenodo_record_id

    assert zenodo_record_id(url) == "21805277"
    base = add_zenodo_url(url)["@context"][0]["@base"]
    assert base == "https://zenodo.org/records/21805277/"


def test_zenodo_record_id_rejects_other_urls() -> None:
    """A URL without a Zenodo record number is an error naming the URL."""
    from pyflowbatt.battinfo import zenodo_record_id

    with pytest.raises(ValueError, match=re.escape("example.org/dataset/42")):
        zenodo_record_id("https://example.org/dataset/42")


def test_resolve_zenodo_url_same_record_in_different_forms() -> None:
    """A record page URL in the sheet and a DOI in pub_info for one record do not conflict."""
    from pyflowbatt.battinfo import resolve_zenodo_url

    sheet = {"hasOutput": {"dcat:accessURL": {"@id": "https://zenodo.org/records/21805277"}}}
    pub_info_url = "https://doi.org/10.5281/zenodo.21805277"
    assert resolve_zenodo_url(sheet, pub_info_url) == "https://zenodo.org/records/21805277"


def test_template_every_single_hash_line_uncomments_to_valid_toml(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Explanations use ##, and deleting the # from every other line gives a valid config."""
    from pyflowbatt.config import TEMPLATE_TOML

    lines = TEMPLATE_TOML.splitlines()
    assert all(not line or line.startswith(("##", "# ")) for line in lines)
    uncommented = [line.removeprefix("# ") for line in lines]
    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    (sample_dir / "pyflowbatt.toml").write_text("\n".join(uncommented), encoding="utf-8")

    with caplog.at_level("WARNING", logger="pyflowbatt"):
        config = PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")
    assert caplog.records == []
    assert config.eis_model == "L0-R0-(R1,CPE1)-(R2,CPE2)"
    assert config.cv_v_max == 0.6


def test_bad_toml_error_names_file_and_line(tmp_path: Path) -> None:
    """An uncommented explanation line raises ConfigError with the file, line and a hint."""
    from pyflowbatt.config import ConfigError

    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    toml = sample_dir / "pyflowbatt.toml"
    toml.write_text(
        "## --- Electrode area ---\narea_cm2 = 3\n\n"
        "--- LSV pre/post split: file numbers below this are pre ---\nlsv_threshold = 8\n"
    )

    with pytest.raises(ConfigError) as excinfo:
        PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")
    message = str(excinfo.value)
    assert str(toml) in message
    assert "line 4: --- LSV pre/post split" in message
    assert "put the # back" in message


@pytest.mark.parametrize(
    ("toml_text", "hint"),
    [
        ('## Standard tags:\nStandard tags: "pre", "post".\n', "explanation line"),
        ('model = "R0"\n# [cv]\neis = ["*_x_*"]\n[eis]\n', "deleted the # in front"),
        ("area_cm2 = 5\narea_cm2 = 6\n", "set twice"),
        ('sample_name_pattern = "^\\d+$"\n', "Use single quotes"),
        ('model = "R0\n', "missing its closing quote"),
        ('summary_params = ["R0.r", "R1.r"\n', "missing its closing ]"),
        ("model = L0-R0\n", "Text values need quotes"),
        ("lsv_threshold = True\n", "true/false are lowercase"),
        ("area_cm2 = 5 cm2\n", "no units"),
        ('[eis\nmodel = "R0"\n', "Section lines look like [eis]"),
        ("area cm2 = 5\n", "can't contain spaces"),
        ("x = {a = 1\n", "Only delete the single #"),
    ],
)
def test_bad_toml_hint_matches_mistake(tmp_path: Path, toml_text: str, hint: str) -> None:
    """Each kind of TOML mistake gets a hint about its likely cause."""
    from pyflowbatt.config import ConfigError

    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    (sample_dir / "pyflowbatt.toml").write_text(toml_text)

    with pytest.raises(ConfigError, match=re.escape(hint)):
        PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")


def test_toml_with_bom_loads(tmp_path: Path) -> None:
    """A UTF-8 file with a byte order mark, as some Windows editors save, still loads."""
    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    (sample_dir / "pyflowbatt.toml").write_bytes(b"\xef\xbb\xbfarea_cm2 = 3\n")

    assert PyFlowBattConfig.load(sample_dir, home=tmp_path / "home").area_cm2 == 3


@pytest.mark.parametrize(
    ("toml_text", "expected"),
    [
        (
            '# [eis]\nmodel = "R0"\n',
            "Ignoring model at the top level ({path}, line 2): it belongs under [eis]",
        ),
        (
            '[eis]\nmodel = "R0"\n\n# [cv]\nv_min = 0.1\n',
            "Ignoring v_min in [eis] ({path}, line 5): it belongs under [cv]",
        ),
        (
            "[cv]\nmin_r2 = 0.5\narea_cm2 = 3\n",
            "Ignoring area_cm2 in [cv] ({path}, line 3): it is a top-level setting",
        ),
        (
            '[eis_tags]\n"pre" = ["*_02_*"]\n# [eis]\nsummary_params = ["R1.r"]\n',
            "Ignoring summary_params in [eis_tags] ({path}, line 4): it belongs under [eis]",
        ),
        ("area_cm = 3\n", "({path}, line 1): unknown setting, did you mean area_cm2?"),
        ('[eiss]\nmodel = "R0"\n', "({path}, line 1): unknown section, did you mean [eis]?"),
        ("cv = 5\n", "({path}, line 1): cv is a section, so write it as a [cv] line"),
    ],
)
def test_misplaced_setting_warns_with_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, toml_text: str, expected: str
) -> None:
    """A setting outside its section is ignored with a warning naming its line and section."""
    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    toml = sample_dir / "pyflowbatt.toml"
    toml.write_text(toml_text)

    with caplog.at_level("WARNING", logger="pyflowbatt"):
        config = PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")

    assert [r.getMessage() for r in caplog.records if expected.format(path=toml) in r.getMessage()]
    assert config.area_cm2 is None
    assert config.cv_v_min == 0.4004
    assert config.eis_summary_params == ["R0.r", "R1.r", "R2.r"]
    assert config.eis_tag_patterns in ({}, {"pre": ["*_02_*"]})


def test_eis_tags_any_name_not_warned_as_misplaced(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Custom tag names under [eis_tags] are not reported as unknown settings."""
    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    (sample_dir / "pyflowbatt.toml").write_text('[eis_tags]\n"pre-20%SOC" = ["*_03_*"]\n')

    with caplog.at_level("WARNING", logger="pyflowbatt"):
        config = PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")

    assert caplog.records == []
    assert config.eis_tag_patterns == {"pre-20%SOC": ["*_03_*"]}
