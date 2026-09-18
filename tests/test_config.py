"""Tests for pyflowbatt.toml configuration and technique file classification."""

import json
import zipfile
from pathlib import Path

import pytest

from pyflowbatt import config as config_module
from pyflowbatt.config import PyFlowBattConfig, classify_technique_files
from pyflowbatt.rocrate_output import _classify_inputs

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


def test_eis_tags_partial_fills_remaining_tags_in_order(tmp_path: Path) -> None:
    """Tags not listed in eis_tags are filled in filename order from the unpinned files."""
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
    """Unknown tags and non-list values in eis_tags are ignored."""
    sample = _write_two_eis_sample(tmp_path / "sample")
    (sample / "pyflowbatt.toml").write_text(
        '[eis_tags]\n"during" = ["*_06_PEIS_*"]\n"post" = "*_06_PEIS_*"\n'
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
    """Recursively collect every "dcat:downloadURL" string value."""
    found: set[str] = set()
    if isinstance(obj, dict):
        url_value = obj.get("dcat:downloadURL")
        if isinstance(url_value, str):
            found.add(url_value)
        for v in obj.values():
            found |= _all_download_urls(v)
    elif isinstance(obj, list):
        for item in obj:
            found |= _all_download_urls(item)
    return found


def test_analyse_sample_default_package_is_files_and_root_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default zenodo_package="files": @id stays root-relative, downloadURL is per-file."""
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

    urls = _all_download_urls(metadata)
    expected = "https://zenodo.org/records/20338409/files/sample_01/metadata.xlsx"
    assert expected in urls

    context = metadata["@context"]
    local_terms = next(c for c in context if isinstance(c, dict))
    assert local_terms["@base"] == "https://zenodo.org/records/20338409/"


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
    )

    assert extra_inputs["pub_info"] == [pub_info_xlsx]

    metadata = json.loads((sample / "metadata.empa__fcid123456.json").read_text())
    assert "publication_info.xlsx" in _all_ids(metadata["hasInput"])
    expected = "https://zenodo.org/records/20338409/files/publication_info.xlsx"
    assert expected in _all_download_urls(metadata["hasInput"])


def test_analyse_sample_zip_package_id_points_at_zip_with_path_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """zenodo_package="zip": @id stays root-relative, downloadURL is the zip with a fragment."""
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
        zenodo_package="zip",
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


def test_analyse_sample_zip_package_base_shared_across_zips_in_one_record(
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
            zenodo_package="zip",
            pub_info={
                "zenodo_doi_url": "https://doi.org/10.5281/zenodo.20338409",
                "zenodo_zip_filename": zip_filename,
            },
        )
        metadata = json.loads((sample / "metadata.empa__fcid123456.json").read_text())
        assert "sample_01/metadata.xlsx" in _all_ids(metadata)
        local_terms = next(c for c in metadata["@context"] if isinstance(c, dict))
        bases.append(local_terms["@base"])
        download_urls.append(_all_download_urls(metadata))

    assert bases[0] == bases[1] == "https://zenodo.org/records/20338409/"
    assert download_urls[0] != download_urls[1]


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
    zip_folder(root, zip_path)

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    assert names == {"sample_01/results/gcpl.png", "combined_summary.xlsx"}


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
        return {"Substrate": {"hasMeasuredProperty": properties}}

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
