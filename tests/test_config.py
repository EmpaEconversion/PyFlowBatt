"""Tests for pyflowbatt.toml configuration and technique file classification."""

from pathlib import Path

import pytest

from PyFlowBatt.config import PyFlowBattConfig, classify_technique_files
from PyFlowBatt.rocrate_output import _classify_inputs

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
    # Original PEIS match is still present as the first tag.
    assert classified["eis_pre"][0].name == "sample_02_PEIS_A.mpr"
    # GCPL files now also show up under the later EIS tags (merged, not replaced).
    eis_names = [classified[f"eis_{tag}"][0].name for tag in ("pre-50%SOC", "post-50%SOC", "post")]
    assert any("_GCPL_" in name for name in eis_names)


def test_shared_classifier_used_by_rocrate(tmp_path: Path) -> None:
    """RO-Crate's _classify_inputs delegates to the same classify_technique_files."""
    sample = _write_synthetic_sample(tmp_path / "sample")
    (sample / "pyflowbatt.toml").write_text('[extra_patterns]\neis = ["*_GCPL_*"]\n')
    config = PyFlowBattConfig.load(sample, home=tmp_path / "home")

    direct = classify_technique_files(sample, config)
    via_rocrate = _classify_inputs(sample, config)

    assert direct == via_rocrate


def test_sample_id_explicit_override(tmp_path: Path) -> None:
    """[sample_id].name in pyflowbatt.toml overrides detection, regardless of folder name."""
    from PyFlowBatt.analysis import get_sampleid_from_folderpath

    # A folder name that would NOT match the default sample-ID regex.
    sample_dir = tmp_path / "not-a-normal-sample-name"
    sample_dir.mkdir()
    (sample_dir / "pyflowbatt.toml").write_text('[sample_id]\nname = "hardcoded-name"\n')

    config = PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")
    assert get_sampleid_from_folderpath(sample_dir, config) == "hardcoded-name"


def test_sample_id_battinfo_name_used_when_no_toml_override(tmp_path: Path) -> None:
    """With no pyflowbatt.toml sample_id set, a BattINFO-derived name wins over the folder name."""
    from PyFlowBatt.analysis import get_sampleid_from_folderpath

    # A folder name that would NOT match the default sample-ID regex.
    sample_dir = tmp_path / "not-a-normal-sample-name"
    sample_dir.mkdir()

    config = PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")
    assert (
        get_sampleid_from_folderpath(sample_dir, config, battinfo_name="from-battinfo")
        == "from-battinfo"
    )


def test_sample_id_toml_wins_over_battinfo_when_they_agree(tmp_path: Path) -> None:
    """A pyflowbatt.toml sample_id matching the BattINFO name is accepted, no error."""
    from PyFlowBatt.analysis import get_sampleid_from_folderpath

    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    (sample_dir / "pyflowbatt.toml").write_text('[sample_id]\nname = "agreed-name"\n')

    config = PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")
    assert (
        get_sampleid_from_folderpath(sample_dir, config, battinfo_name="agreed-name")
        == "agreed-name"
    )


def test_sample_id_toml_battinfo_conflict_raises(tmp_path: Path) -> None:
    """A pyflowbatt.toml sample_id that disagrees with the BattINFO name is an error."""
    from PyFlowBatt.analysis import get_sampleid_from_folderpath

    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    (sample_dir / "pyflowbatt.toml").write_text('[sample_id]\nname = "toml-name"\n')

    config = PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")
    with pytest.raises(ValueError, match="Sample name mismatch"):
        get_sampleid_from_folderpath(sample_dir, config, battinfo_name="different-battinfo-name")


def test_analyse_sample_uses_battinfo_name_when_no_toml_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """analyse_sample resolves sample_id from BattINFO when pyflowbatt.toml doesn't set one."""
    from PyFlowBatt import analysis as analysis_module

    # A folder name that would NOT match the default sample-ID regex.
    sample = tmp_path / "not-a-normal-sample-name"
    sample.mkdir()
    (sample / "metadata.xlsx").write_text("x")  # content is irrelevant; conversion is stubbed

    def fake_convert(_file: Path) -> dict:
        return {
            "@context": {},
            "@type": "RedoxFlowBattery",
            "schema:productID": "FCID1",
            "schema:name": "battinfo-derived-name",
        }

    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", fake_convert)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    _tracked_outputs, _tracked_extra_inputs, fcid, sample_id = analysis_module.analyse_sample(
        sample, save_format=None, config=config
    )
    assert sample_id == "battinfo-derived-name"
    assert fcid == "FCID1"
    assert (sample / "metadata.battinfo-derived-name.json").exists()


def test_analyse_sample_raises_on_toml_battinfo_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """analyse_sample raises when pyflowbatt.toml sample_id disagrees with BattINFO's name."""
    from PyFlowBatt import analysis as analysis_module

    sample = tmp_path / "sample"
    sample.mkdir()
    (sample / "metadata.xlsx").write_text("x")
    (sample / "pyflowbatt.toml").write_text('[sample_id]\nname = "toml-name"\n')

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


def test_sample_id_custom_pattern(tmp_path: Path) -> None:
    """[sample_id].pattern in pyflowbatt.toml replaces the default sample-ID regex."""
    from PyFlowBatt.analysis import get_sampleid_from_folderpath

    sample_dir = tmp_path / "ABC-123"
    sample_dir.mkdir()
    (sample_dir / "pyflowbatt.toml").write_text('[sample_id]\npattern = "^[A-Z]+-\\\\d+$"\n')

    config = PyFlowBattConfig.load(sample_dir, home=tmp_path / "home")
    assert config.sample_id_pattern == r"^[A-Z]+-\d+$"
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
    from PyFlowBatt.analysis import get_area_cm2
    from PyFlowBatt.config import DEFAULT_AREA_CM2

    assert get_area_cm2(PyFlowBattConfig()) == DEFAULT_AREA_CM2


def test_get_area_cm2_from_battinfo_when_electrodes_agree() -> None:
    """When positive and negative electrode areas agree, that value is used."""
    from PyFlowBatt.analysis import get_area_cm2

    raw_json = _make_raw_battinfo_json(pos_area=5.0, neg_area=5.0)
    assert get_area_cm2(PyFlowBattConfig(), raw_json) == 5.0


def test_get_area_cm2_from_battinfo_picks_smaller_when_electrodes_disagree() -> None:
    """When positive and negative electrode areas disagree, the smaller one is used."""
    from PyFlowBatt.analysis import get_area_cm2

    raw_json = _make_raw_battinfo_json(pos_area=5.0, neg_area=4.0)
    assert get_area_cm2(PyFlowBattConfig(), raw_json) == 4.0


def test_get_area_cm2_ignores_wrong_unit() -> None:
    """A BattINFO Area entry in a unit other than CentiM2 is ignored."""
    from PyFlowBatt.analysis import get_area_cm2
    from PyFlowBatt.config import DEFAULT_AREA_CM2

    raw_json = _make_raw_battinfo_json(pos_area=5.0, neg_area=5.0, unit="unit:MicroM2")
    assert get_area_cm2(PyFlowBattConfig(), raw_json) == DEFAULT_AREA_CM2


def test_get_area_cm2_toml_wins_when_battinfo_agrees() -> None:
    """An explicit pyflowbatt.toml area_cm2 that agrees with BattINFO is accepted."""
    from PyFlowBatt.analysis import get_area_cm2

    raw_json = _make_raw_battinfo_json(pos_area=3.14, neg_area=3.14)
    config = PyFlowBattConfig(area_cm2=3.14)
    assert get_area_cm2(config, raw_json) == 3.14


def test_get_area_cm2_toml_battinfo_conflict_raises() -> None:
    """An explicit pyflowbatt.toml area_cm2 that disagrees with BattINFO is an error."""
    from PyFlowBatt.analysis import get_area_cm2

    raw_json = _make_raw_battinfo_json(pos_area=5.0, neg_area=5.0)
    config = PyFlowBattConfig(area_cm2=3.14)
    with pytest.raises(ValueError, match="Electrode area mismatch"):
        get_area_cm2(config, raw_json)


def test_analyse_sample_uses_battinfo_area_for_lsv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """analyse_sample resolves area_cm2 from BattINFO electrodes when no toml override exists."""
    import pandas as pd

    from PyFlowBatt import analysis as analysis_module

    sample = tmp_path / "sample"
    sample.mkdir()
    (sample / "sample_03_LSV_A.mpr").write_text("x")
    (sample / "sample_13_LSV_B.mpr").write_text("x")
    (sample / "metadata.xlsx").write_text("x")  # content irrelevant; conversion is stubbed

    raw_json = _make_raw_battinfo_json(pos_area=4.0, neg_area=4.0)
    monkeypatch.setattr(analysis_module, "convert_excel_to_jsonld", lambda _file: raw_json)

    captured_areas: list[float] = []

    def fake_analyse(filepath: Path, area_cm2: float = 5) -> tuple[pd.DataFrame, dict]:
        captured_areas.append(area_cm2)
        df = pd.DataFrame({"Voltage / V": [0.0, 1.0], "Current / A": [0.0, 1.0]})
        results = {
            "Fit cutoff current / A": 0.0,
            "Intercept / A": 0.0,
            "Slope / Ω⁻¹": 1.0,
            "Resistance / Ω": 1.0,
            "Area / cm²": area_cm2,
            "Area specific resistance / Ω cm²": area_cm2,
            "File name": Path(filepath).name,
        }
        return df, results

    def fake_plot(df: pd.DataFrame, results: dict) -> tuple:
        import matplotlib.pyplot as plt

        return plt.subplots()

    monkeypatch.setattr(analysis_module.lsv, "analyse", fake_analyse)
    monkeypatch.setattr(analysis_module.lsv, "plot", fake_plot)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    analysis_module.analyse_sample(sample, save_format=None, config=config)

    assert captured_areas == [4.0, 4.0]


def test_analyse_sample_raises_on_toml_battinfo_area_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """analyse_sample raises when pyflowbatt.toml area_cm2 disagrees with BattINFO electrodes."""
    from PyFlowBatt import analysis as analysis_module

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

    from PyFlowBatt.analysis import get_assembled_resistance_ohm

    assert math.isnan(get_assembled_resistance_ohm(PyFlowBattConfig()))


def test_get_assembled_resistance_ohm_from_filename_fallback() -> None:
    """With no toml or BattINFO, the value is parsed out of the fallback filename."""
    from PyFlowBatt.analysis import get_assembled_resistance_ohm

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
    from PyFlowBatt.analysis import get_assembled_resistance_ohm

    raw_json = _make_raw_battinfo_json(resistance_value="25000", resistance_unit=unit)
    assert get_assembled_resistance_ohm(PyFlowBattConfig(), raw_json) == expected


def test_get_assembled_resistance_ohm_ignores_unrecognized_unit() -> None:
    """A BattINFO ElectricResistance with an unrecognized unit falls through to the filename."""
    from PyFlowBatt.analysis import get_assembled_resistance_ohm

    raw_json = _make_raw_battinfo_json(resistance_value="25000", resistance_unit="unit:VOLT")
    value = get_assembled_resistance_ohm(
        PyFlowBattConfig(), raw_json, fallback_filename="sample_9kOhm_04_GCPL_CE4"
    )
    assert value == 9000.0


def test_get_assembled_resistance_ohm_toml_wins_when_battinfo_agrees() -> None:
    """An explicit pyflowbatt.toml assembled_resistance_ohm that agrees with BattINFO is fine."""
    from PyFlowBatt.analysis import get_assembled_resistance_ohm

    raw_json = _make_raw_battinfo_json(resistance_value="25000", resistance_unit="unit:OHM")
    config = PyFlowBattConfig(assembled_resistance_ohm=25000.0)
    assert get_assembled_resistance_ohm(config, raw_json) == 25000.0


def test_get_assembled_resistance_ohm_toml_battinfo_conflict_raises() -> None:
    """An explicit pyflowbatt.toml assembled_resistance_ohm disagreeing with BattINFO errors."""
    from PyFlowBatt.analysis import get_assembled_resistance_ohm

    raw_json = _make_raw_battinfo_json(resistance_value="25000", resistance_unit="unit:OHM")
    config = PyFlowBattConfig(assembled_resistance_ohm=9000.0)
    with pytest.raises(ValueError, match="Assembled resistance mismatch"):
        get_assembled_resistance_ohm(config, raw_json)


def test_analyse_sample_uses_battinfo_resistance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """analyse_sample resolves the summary's Assembled resistance from BattINFO."""
    import pandas as pd

    from PyFlowBatt import analysis as analysis_module

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
    from PyFlowBatt import analysis as analysis_module

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

    from PyFlowBatt import analysis as analysis_module

    sample = tmp_path / "sample"
    sample.mkdir()
    (sample / "sample_03_LSV_A.mpr").write_text("x")
    (sample / "sample_13_LSV_B.mpr").write_text("x")
    (sample / "pyflowbatt.toml").write_text("area_cm2 = 2.5\n")

    captured_areas: list[float] = []

    def fake_analyse(filepath: Path, area_cm2: float = 5) -> tuple[pd.DataFrame, dict]:
        captured_areas.append(area_cm2)
        df = pd.DataFrame({"Voltage / V": [0.0, 1.0], "Current / A": [0.0, 1.0]})
        results = {
            "Fit cutoff current / A": 0.0,
            "Intercept / A": 0.0,
            "Slope / Ω⁻¹": 1.0,
            "Resistance / Ω": 1.0,
            "Area / cm²": area_cm2,
            "Area specific resistance / Ω cm²": area_cm2,
            "File name": Path(filepath).name,
        }
        return df, results

    def fake_plot(df: pd.DataFrame, results: dict) -> tuple:
        import matplotlib.pyplot as plt

        return plt.subplots()

    monkeypatch.setattr(analysis_module.lsv, "analyse", fake_analyse)
    monkeypatch.setattr(analysis_module.lsv, "plot", fake_plot)

    config = analysis_module.PyFlowBattConfig.load(sample, home=tmp_path / "home")
    assert config.area_cm2 == 2.5

    analysis_module.analyse_sample(sample, save_format=None, config=config)

    assert captured_areas == [2.5, 2.5]


def _cv_analyse_defaults() -> dict:
    """Read the default kwarg values off cv.analyse's signature."""
    import inspect

    from PyFlowBatt import cv as cv_module

    sig = inspect.signature(cv_module.analyse)
    return {
        name: param.default
        for name, param in sig.parameters.items()
        if param.default is not inspect.Parameter.empty
    }


def test_cv_config_defaults_match_cv_module() -> None:
    """Without a pyflowbatt.toml, the [cv] settings match cv.analyse's own defaults."""
    from PyFlowBatt import cv as cv_module

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

    from PyFlowBatt import analysis as analysis_module

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

    from PyFlowBatt import analysis as analysis_module

    sample = tmp_path / "sample"
    sample.mkdir()
    (sample / "sample_04_GCPL_A.mpr").write_text("x")
    (sample / "pyflowbatt.toml").write_text("summary_n_cycles = [5, 10]\n")

    cycle_counts = list(range(1, 15))
    cycle_df = pd.DataFrame(
        {
            "Total Cycle Count / 1": cycle_counts,
            "Coulombic Efficiency / %": [95.0] * len(cycle_counts),
            "Energy Efficiency / %": [90.0] * len(cycle_counts),
            "Voltage Efficiency / %": [94.0] * len(cycle_counts),
            "Charge Capacity / mAh": [10.0] * len(cycle_counts),
            "Discharge Capacity / mAh": [9.5] * len(cycle_counts),
        }
    )

    def fake_gcpl_analyse(_filepaths: list) -> tuple[pd.DataFrame, pd.DataFrame]:
        return pd.DataFrame({"Voltage / V": [0.0]}), cycle_df

    def fake_gcpl_plot(_df: pd.DataFrame) -> tuple:
        import matplotlib.pyplot as plt

        return plt.subplots()

    def fake_cycles_to_ratetest(_cycle_df: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame({"Times seen": [1]})

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
