"""Utility functions for analysing flow battery data from MPR files."""

import contextlib
import json
import logging
import re
from pathlib import Path
from typing import Literal

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from battinfoconverter_backend import convert_excel_to_jsonld

from pyflowbatt import battinfo, cv, eis, gcpl, lsv, ocv
from pyflowbatt.config import DEFAULT_AREA_CM2, EIS_TAGS, PyFlowBattConfig, classify_technique_files
from pyflowbatt.read import read_to_bdf

logger = logging.getLogger(__name__)

SAVE_FORMATS = Literal["parquet", "csv"] | None
DEFAULT_DEPTH = 6  # Default max search depth in folders
DEFAULT_SEARCH = 10000  # Default max number of folders searched

MPR_DESCRIPTIONS: dict[str, str] = {
    "gcpl": "Galvanostatic cycling measurement (EC-Lab MPR)",
    "ocv": "Open circuit voltage measurement (EC-Lab MPR)",
    "lsv_pre": "Pre-cycling linear sweep voltammetry (EC-Lab MPR)",
    "lsv_post": "Post-cycling linear sweep voltammetry (EC-Lab MPR)",
    "cv_pre": "Pre-cycling cyclic voltammetry (EC-Lab MPR)",
    "cv_post": "Post-cycling cyclic voltammetry (EC-Lab MPR)",
    "eis_pre": "EIS measurement, pre-cycling (EC-Lab MPR)",
    "eis_pre-50%SOC": "EIS measurement, pre-cycling 50% SOC (EC-Lab MPR)",
    "eis_post-50%SOC": "EIS measurement, post-cycling 50% SOC (EC-Lab MPR)",
    "eis_post": "EIS measurement, post-cycling (EC-Lab MPR)",
}


def get_res_from_filename(s: str) -> float:
    """Get nominal resistance from filename."""
    match = re.search(r"_([\d.,]+)([kM]?Ohm)_", s)

    if match:
        value_str = match.group(1)
        try:
            value = float(value_str)
        except ValueError:
            value = float(value_str.replace(",", "."))
        unit = match.group(2)
        if unit == "kOhm":
            value *= 1e3
        elif unit == "MOhm":
            value *= 1e6
        return value
    return np.nan


_RESISTANCE_UNIT_MULTIPLIERS = {
    "ohm": 1.0,
    "kiloohm": 1e3,
    "megaohm": 1e6,
}


def _resistance_unit_multiplier(unit: str) -> float | None:
    """Ohms-per-unit multiplier for a BattINFO resistance unit string.

    Accepts both the qudt-style ``unit:OHM``/``unit:KiloOHM``/``unit:MegaOHM`` and the
    bare ``Ohm``/``KiloOhm``/``MegaOhm`` forms seen in different BattINFO converter versions.
    """
    return _RESISTANCE_UNIT_MULTIPLIERS.get(unit.removeprefix("unit:").lower())


def _extract_assembled_resistance_ohm(raw_battinfo_json: dict) -> float | None:
    """Extract an assembled/external resistance (ohms) from a raw BattINFO jsonld dict."""
    properties = raw_battinfo_json.get("hasMeasuredProperty") or []
    if isinstance(properties, dict):
        properties = [properties]
    for prop in properties:
        if not isinstance(prop, dict) or prop.get("@type") != "ElectricResistance":
            continue
        unit = prop.get("hasMeasurementUnit")
        multiplier = _resistance_unit_multiplier(unit) if isinstance(unit, str) else None
        if multiplier is None:
            logger.warning(
                "Ignoring ElectricResistance with unrecognized unit %s in BattINFO file", unit
            )
            continue
        raw_value = prop.get("hasNumericalPart", {}).get("hasNumberValue")
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        return value * multiplier
    return None


def get_assembled_resistance_ohm(
    config: PyFlowBattConfig,
    raw_battinfo_json: dict | None = None,
    fallback_filename: str | None = None,
) -> float:
    """Resolve the assembled/external resistance (ohms) for the "Assembled resistance" summary.

    Resolved in priority order: an explicit ``assembled_resistance_ohm`` set in
    pyflowbatt.toml, then an ``ElectricResistance`` measurement in a BattINFO file, then a
    value parsed out of ``fallback_filename`` (e.g. ``..._25kOhm_...``) via
    :func:`get_res_from_filename`.

    Raises ``ValueError`` if pyflowbatt.toml sets ``assembled_resistance_ohm`` and a
    BattINFO-derived value is also available and the two disagree.
    """
    battinfo_value = (
        _extract_assembled_resistance_ohm(raw_battinfo_json)
        if raw_battinfo_json is not None
        else None
    )

    if config.assembled_resistance_ohm is not None:
        if battinfo_value is not None and battinfo_value != config.assembled_resistance_ohm:
            msg = (
                "Assembled resistance mismatch: pyflowbatt.toml sets assembled_resistance_ohm="
                f"{config.assembled_resistance_ohm} but the BattINFO file gives "
                f"{battinfo_value} ohm"
            )
            raise ValueError(msg)
        return config.assembled_resistance_ohm

    if battinfo_value is not None:
        return battinfo_value

    if fallback_filename is not None:
        return get_res_from_filename(fallback_filename)

    return np.nan


def get_sampleid_from_folderpath(
    folderpath: str | Path,
    config: PyFlowBattConfig | None = None,
    battinfo_name: str | None = None,
) -> str:
    """Get the sample ID for a sample folder.

    Resolved in priority order: an explicit ``sample_name`` set in pyflowbatt.toml, then
    the sample name from a BattINFO file (pass it as ``battinfo_name``), then a name
    derived from the folder path (usually the folder name, sometimes the parent).

    Raises ``ValueError`` if pyflowbatt.toml sets an explicit ``sample_name`` and a
    ``battinfo_name`` is also given and the two disagree.
    """
    folderpath = Path(folderpath)
    config = config or PyFlowBattConfig.load(folderpath)

    if config.sample_name is not None:
        if battinfo_name is not None and battinfo_name != config.sample_name:
            msg = (
                f"Sample name mismatch for {folderpath}: pyflowbatt.toml sets sample_name "
                f"'{config.sample_name}' but the BattINFO file gives '{battinfo_name}'"
            )
            raise ValueError(msg)
        return config.sample_name

    if battinfo_name is not None:
        return battinfo_name

    pattern = config.sample_name_pattern
    # Sample ID has format [digits]_[somethingelse]_[somethingelse]
    # e.g. 250115_reda_1M-blahblahblah
    if re.match(pattern=pattern, string=folderpath.stem):
        return folderpath.stem
    try:
        if re.match(pattern=pattern, string=folderpath.parent.stem):
            return folderpath.parent.stem
    except AttributeError:
        pass
    logger.warning("Could not find sample ID in %s", folderpath)
    return "Unknown sample"


def _extract_electrode_area_cm2(raw_battinfo_json: dict, electrode_key: str) -> float | None:
    """Extract a Substrate Area (cm^2) for one electrode from a raw BattINFO jsonld dict.

    ``electrode_key`` is ``"hasPositiveElectrode"`` or ``"hasNegativeElectrode"``.
    """
    electrode = raw_battinfo_json.get(electrode_key)
    if not isinstance(electrode, dict):
        return None
    substrate = electrode.get("Substrate")
    if not isinstance(substrate, dict):
        return None
    properties = substrate.get("hasMeasuredProperty") or []
    if isinstance(properties, dict):
        properties = [properties]
    for prop in properties:
        if not isinstance(prop, dict) or prop.get("@type") != "Area":
            continue
        unit = prop.get("hasMeasurementUnit")
        if unit != "unit:CentiM2":
            logger.warning(
                "Ignoring %s Area with unit %s in BattINFO file (expected unit:CentiM2)",
                electrode_key,
                unit,
            )
            continue
        value = prop.get("hasNumericalPart", {}).get("hasNumberValue")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def get_area_cm2(config: PyFlowBattConfig, raw_battinfo_json: dict | None = None) -> float:
    """Resolve the electrode area (cm^2) used to normalise LSV resistance.

    Resolved in priority order: an explicit ``area_cm2`` set in pyflowbatt.toml, then the
    BattINFO file's positive/negative electrode Substrate Area (if both are present and
    disagree, the smaller is used), then :data:`pyflowbatt.config.DEFAULT_AREA_CM2`.

    Raises ``ValueError`` if pyflowbatt.toml sets ``area_cm2`` and a BattINFO-derived area
    is also available and the two disagree.
    """
    battinfo_area: float | None = None
    if raw_battinfo_json is not None:
        pos = _extract_electrode_area_cm2(raw_battinfo_json, "hasPositiveElectrode")
        neg = _extract_electrode_area_cm2(raw_battinfo_json, "hasNegativeElectrode")
        if pos is not None and neg is not None:
            if pos != neg:
                logger.warning(
                    "Positive (%.4g cm2) and negative (%.4g cm2) electrode areas disagree "
                    "in BattINFO file; using the smaller",
                    pos,
                    neg,
                )
            battinfo_area = min(pos, neg)
        elif pos is not None:
            battinfo_area = pos
        elif neg is not None:
            battinfo_area = neg

    if config.area_cm2 is not None:
        if battinfo_area is not None and battinfo_area != config.area_cm2:
            msg = (
                f"Electrode area mismatch: pyflowbatt.toml sets area_cm2={config.area_cm2} "
                f"but the BattINFO file gives {battinfo_area} cm2"
            )
            raise ValueError(msg)
        return config.area_cm2

    if battinfo_area is not None:
        return battinfo_area

    return DEFAULT_AREA_CM2


def df_save_bdf(df: pd.DataFrame, filepath: Path, save_format: SAVE_FORMATS = "parquet") -> None:
    """Save df to file."""
    if save_format is None:
        return
    if save_format == "parquet":
        df.to_parquet(filepath.with_suffix(".bdf.parquet"))
        return
    if save_format == "csv":
        df.to_csv(filepath.with_suffix(".csv.parquet"))
        return
    msg = "Format not understood"
    raise ValueError(msg)


def analyse_sample(
    folder: str | Path,
    *,
    pub_info: dict | None = None,
    save_format: SAVE_FORMATS = "parquet",
    config: PyFlowBattConfig | None = None,
) -> tuple[dict[str, list[Path]], dict[str, list[Path]], str | None, str]:
    """Read all the files in a folder, analayse and plot everything.

    Without an explicit config, loads a ``pyflowbatt.toml`` cascade (home directory,
    parent folder, sample folder) to customise technique glob patterns and sample ID
    detection.
    """
    folder = Path(folder)
    pub_info = pub_info or {}
    fcid: str | None = None
    config = config or PyFlowBattConfig.load(folder)

    logger.info("\n🌊 PyFlowBatt-ing %s", folder.name)
    classified = classify_technique_files(folder, config, warn=True)
    gcpl_files = classified.get("gcpl", [])
    gcpl_file = gcpl_files[0] if gcpl_files else None
    ocv_files = classified.get("ocv", [])
    cv_files_before = classified.get("cv_pre", [])
    cv_files_after = classified.get("cv_post", [])
    eis_by_tag = {
        tag: classified[f"eis_{tag}"][0] for tag in EIS_TAGS if f"eis_{tag}" in classified
    }
    mps_files = list(folder.glob("*.mps"))
    battinfo_files = list(folder.glob("*.xlsx"))
    logger.debug("Reading GCPL: %s", ", ".join([f.stem for f in gcpl_files]))
    logger.debug(
        "Reading LSV: %s",
        ", ".join(f.stem for label in ("lsv_pre", "lsv_post") for f in classified.get(label, [])),
    )
    logger.debug("Reading CV before: %s", ", ".join([f.stem for f in cv_files_before]))
    logger.debug("Reading CV after:  %s", ", ".join([f.stem for f in cv_files_after]))
    logger.debug("Reading EIS files: %s", ", ".join([f.stem for f in eis_by_tag.values()]))
    logger.debug("Checking BattINFO files: %s", ", ".join([f.stem for f in battinfo_files]))

    cycle_df = None
    cv_df = None
    lsv_df = None
    ratetest_df = None
    eis_df = None
    results_dir = folder / "results"
    results_dir.mkdir(exist_ok=True)
    tracked_outputs: dict[str, list[Path]] = {}
    tracked_mpr_inputs: dict[str, list[Path]] = {}
    battinfo_xlsx_path: Path | None = None
    # suffix produced by df_save_bdf for the configured format
    bdf_suffix = (
        ".bdf.parquet"
        if save_format == "parquet"
        else (".csv.parquet" if save_format == "csv" else None)
    )

    logger.info("Battinfo-ifying")
    battinfo_json = None
    battinfo_sample_id = None
    raw_battinfo_json: dict | None = None
    if not battinfo_files:
        logger.info("- ☹️ No battinfo xlsx found, skipping")
    else:
        for file in battinfo_files:
            raw_json = None
            with contextlib.suppress(ValueError):
                raw_json = convert_excel_to_jsonld(file)
            if raw_json is None:
                continue
            battinfo_xlsx_path = file
            fcid = raw_json["schema:productID"]
            battinfo_sample_id = raw_json["schema:name"]
            raw_battinfo_json = raw_json
            battinfo_json = battinfo.make_test_object(raw_json)
            battinfo_json = battinfo.merge_jsonld_on_type(
                [battinfo_json, battinfo.add_input_and_output()],
            )
            if pub_info and pub_info.get("citation_string"):
                battinfo_json = battinfo.merge_jsonld_on_type(
                    [battinfo_json, battinfo.add_citation(pub_info["citation_string"])]
                )
            if pub_info and pub_info.get("authors") and pub_info.get("institutions"):
                battinfo_json = battinfo.merge_jsonld_on_type(
                    [
                        battinfo_json,
                        battinfo.add_authors(pub_info["authors"], pub_info["institutions"]),
                    ]
                )
            if pub_info and pub_info.get("sample_to_fig"):
                battinfo_json = battinfo.merge_jsonld_on_type(
                    [
                        battinfo_json,
                        battinfo.add_associated_media(
                            pub_info.get("publication_doi_url"),
                            pub_info["sample_to_fig"],
                            fcid,
                            battinfo_sample_id,
                        ),
                    ]
                )
            break
        else:
            logger.info("- ☹️ Couldn't convert battinfo xlsx")

    sample_id = get_sampleid_from_folderpath(folder, config, battinfo_sample_id)
    area_cm2 = get_area_cm2(config, raw_battinfo_json)
    assembled_resistance_ohm = get_assembled_resistance_ohm(
        config, raw_battinfo_json, gcpl_files[0].stem if gcpl_files else None
    )

    logger.info("⛓️‍💥 Analysing OCV")
    av_ocv = (np.nan, np.nan)
    if len(ocv_files) == 0:
        logger.info("- ☹️ No OCV found, skipping")
    else:
        try:
            av_ocv = ocv.analyse(ocv_files[0])
            tracked_mpr_inputs["ocv"] = [ocv_files[0]]
        except ValueError:
            logger.exception("Failed to analyse OCV")

    logger.info("🔋 Analysing GCPL")
    if gcpl_file is None:
        logger.warning("- ☹️ No GCPL files found, skipping")
    else:
        df, cycle_df = gcpl.analyse([gcpl_file])
        fig, _ax = gcpl.plot(df, cycle_df)
        fig.savefig(results_dir / "gcpl.png")
        plt.close(fig)
        ratetest_df = gcpl.cycles_to_ratetest(cycle_df)
        df_save_bdf(df, results_dir / "gcpl.x", save_format=save_format)
        tracked_outputs["gcpl"] = [results_dir / "gcpl.png"]
        if bdf_suffix:
            tracked_outputs["gcpl"].append((results_dir / "gcpl.x").with_suffix(bdf_suffix))
        tracked_mpr_inputs["gcpl"] = [gcpl_file]

    logger.info("↗️ Analysing LSV")
    lsv_res = {"pre": np.nan, "post": np.nan}
    lsv_pre_files = classified.get("lsv_pre", [])
    lsv_post_files = classified.get("lsv_post", [])
    if not lsv_pre_files and not lsv_post_files:
        logger.info("- ☹️ No LSV files found, skipping")
    else:
        if bool(lsv_pre_files) != bool(lsv_post_files):
            only = "pre" if lsv_pre_files else "post"
            logger.warning("- Only one LSV file found, assuming it is %s", only)
        lsv_rows = []
        for p, lsv_files_p in [("pre", lsv_pre_files), ("post", lsv_post_files)]:
            if not lsv_files_p:
                continue
            try:
                df, results = lsv.analyse(lsv_files_p[0], area_cm2=area_cm2)
                fig, _ax = lsv.plot(df, results)
                fig.savefig(results_dir / f"lsv_{p}.png")
                plt.close(fig)
                df_save_bdf(df, results_dir / f"lsv_{p}.x", save_format=save_format)
                lsv_res[p] = float(results["Area specific resistance / Ω cm²"])
                tracked_outputs[f"lsv_{p}"] = [results_dir / f"lsv_{p}.png"]
                if bdf_suffix:
                    tracked_outputs[f"lsv_{p}"].append(
                        (results_dir / f"lsv_{p}.x").with_suffix(bdf_suffix)
                    )
                tracked_mpr_inputs[f"lsv_{p}"] = [lsv_files_p[0]]
                lsv_rows.append({"Pre or post cycle": p, **results})
            except ValueError:
                logger.exception("Failed to analyse LSV file")
        if lsv_rows:
            lsv_df = pd.DataFrame(lsv_rows)

    logger.info("🚴 Analysing CV")
    cv_res = {"pre": np.nan, "post": np.nan}
    if len(cv_files_before) == 0 and len(cv_files_after) == 0:
        logger.warning("- ☹️ No  files found, skipping")
    else:
        for p, cv_files in [("pre", cv_files_before), ("post", cv_files_after)]:
            if not cv_files:
                continue
            df, cv_df, capacitance_mF = cv.analyse(
                cv_files[0],
                v_min=config.cv_v_min,
                v_max=config.cv_v_max,
                v_med=config.cv_v_med,
                v_range=config.cv_v_range,
                min_r2=config.cv_min_r2,
            )
            df_save_bdf(df, results_dir / f"cv_{p}.x", save_format=save_format)
            fig, _ax = cv.plot(df, cv_df, min_r2=config.cv_min_r2)
            fig.savefig(results_dir / f"cv_{p}.png")
            plt.close(fig)
            if capacitance_mF:
                cv_res[p] = capacitance_mF
            tracked_outputs[f"cv_{p}"] = [results_dir / f"cv_{p}.png"]
            if bdf_suffix:
                tracked_outputs[f"cv_{p}"].append(
                    (results_dir / f"cv_{p}.x").with_suffix(bdf_suffix)
                )
            tracked_mpr_inputs[f"cv_{p}"] = [cv_files[0]]

    logger.info("🌈 Analysing PEIS")
    eis_res = {}
    rows = []
    if not eis_by_tag:
        logger.warning("- ☹️ No EIS files were found, skipping")
    else:
        for tag, eis_file in eis_by_tag.items():
            f = Path(eis_file)
            try:
                df = read_to_bdf(f)
                params, Z_fit = eis.analyse(df)
                fig, _ax = eis.plot(df, Z_fit)
                fig.savefig(results_dir / f"eis_{tag}.png")
                plt.close(fig)
                eis_res[tag] = params
                df["Real Impedance Fit / ohm"] = np.real(Z_fit)
                df["Real Impedance Fit / ohm"] = np.imag(Z_fit)
                df_save_bdf(df, results_dir / f"eis_{tag}.x", save_format=save_format)
                tracked_outputs[f"eis_{tag}"] = [results_dir / f"eis_{tag}.png"]
                if bdf_suffix:
                    tracked_outputs[f"eis_{tag}"].append(
                        (results_dir / f"eis_{tag}.x").with_suffix(bdf_suffix)
                    )
                tracked_mpr_inputs[f"eis_{tag}"] = [Path(eis_file)]
                for name, values in params.items():
                    rows.append({"file": f.stem, "tag": tag, "name": name, **values})
            except Exception as e:
                logger.warning("- Failed to fit %s: %s", f.stem, str(e))
        if rows:
            eis_df = pd.DataFrame(rows)
            eis_df = eis_df.pivot(index=["file", "tag"], columns=["name"]).reset_index()
            eis_df.columns = [
                f"{name}_{field}" if name else field for field, name in eis_df.columns
            ]
            order = [
                f"{elem}_{x}"
                for elem in ["L0", "R0", "R1", "CPE1_0", "CPE1_1", "R2", "CPE2_0", "CPE2_1"]
                for x in ["value", "err", "unit"]
            ]
            eis_df = eis_df[["file", "tag", *order]]

    logger.info("💪 Making sample summary")

    # Create keys
    cols = [
        "Av. OCV / V",
        "ASR pre / Ω cm²",
        "ASR post / Ω cm²",
        "∆ASR / Ω cm²",
        "F pre / mF",
        "F post / mF",
        "∆F / mF",
        "Assembled resistance / Ω",
        "EIS R pre / Ω",
        "EIS R pre-50%SOC / Ω",
        "1st CE / %",
        "1st EE / %",
        "1st VE / %",
    ]
    # n cycles to include in the summary file
    n_cycles = config.summary_n_cycles
    for n in n_cycles:
        cols.extend(
            [
                f"{n} cycles avg. CE / %",
                f"{n} cycles avg. EE / %",
                f"{n} cycles avg. VE / %",
                f"{n} cycles avg. charge capacity / mAh",
                f"{n} cycles avg. discharge capacity / mAh",
            ]
        )
    summary = {col: {"Value": np.nan, "Error": np.nan} for col in cols}

    # Now fill in values
    summary["Av. OCV / V"]["Value"] = av_ocv[0]
    summary["Av. OCV / V"]["Error"] = av_ocv[1]
    summary["ASR pre / Ω cm²"]["Value"] = lsv_res["pre"]
    summary["ASR post / Ω cm²"]["Value"] = lsv_res["post"]
    summary["∆ASR / Ω cm²"]["Value"] = lsv_res["post"] - lsv_res["pre"]
    summary["F pre / mF"]["Value"] = cv_res["pre"]
    summary["F post / mF"]["Value"] = cv_res["post"]
    summary["∆F / mF"]["Value"] = (
        cv_res["post"] - cv_res["pre"] if (cv_res["post"] and cv_res["pre"]) else None
    )
    summary["Assembled resistance / Ω"]["Value"] = assembled_resistance_ohm
    summary["EIS R pre / Ω"]["Value"] = eis_res.get("pre", {}).get("R0", {}).get("value")
    summary["EIS R pre / Ω"]["Error"] = eis_res.get("pre", {}).get("R0", {}).get("err")
    summary["EIS R pre-50%SOC / Ω"]["Value"] = (
        eis_res.get("pre-50%SOC", {}).get("R0", {}).get("value")
    )
    summary["EIS R pre-50%SOC / Ω"]["Error"] = (
        eis_res.get("pre-50%SOC", {}).get("R0", {}).get("err")
    )

    if cycle_df is not None:
        mask = cycle_df["Cycle Count / 1"] == 1
        summary["1st CE / %"]["Value"] = float(
            cycle_df.loc[mask, "Coulombic Efficiency / %"].to_numpy()[0]
        )
        summary["1st EE / %"]["Value"] = float(
            cycle_df.loc[mask, "Energy Efficiency / %"].to_numpy()[0]
        )
        summary["1st VE / %"]["Value"] = float(
            cycle_df.loc[mask, "Voltage Efficiency / %"].to_numpy()[0]
        )

        max_cycles = cycle_df["Cycle Count / 1"].max()
        for n in n_cycles:
            if n <= max_cycles:
                mask = cycle_df["Cycle Count / 1"] <= n
                summary[f"{n} cycles avg. CE / %"]["Value"] = float(
                    cycle_df.loc[mask, "Coulombic Efficiency / %"].mean()
                )
                summary[f"{n} cycles avg. CE / %"]["Error"] = float(
                    cycle_df.loc[mask, "Coulombic Efficiency / %"].std()
                )
                summary[f"{n} cycles avg. EE / %"]["Value"] = float(
                    cycle_df.loc[mask, "Energy Efficiency / %"].mean()
                )
                summary[f"{n} cycles avg. EE / %"]["Error"] = float(
                    cycle_df.loc[mask, "Energy Efficiency / %"].std()
                )
                summary[f"{n} cycles avg. VE / %"]["Value"] = float(
                    cycle_df.loc[mask, "Voltage Efficiency / %"].mean()
                )
                summary[f"{n} cycles avg. VE / %"]["Error"] = float(
                    cycle_df.loc[mask, "Voltage Efficiency / %"].std()
                )
                summary[f"{n} cycles avg. charge capacity / mAh"]["Value"] = float(
                    cycle_df.loc[mask, "Charge Capacity / mAh"].mean()
                )
                summary[f"{n} cycles avg. charge capacity / mAh"]["Error"] = float(
                    cycle_df.loc[mask, "Charge Capacity / mAh"].std()
                )
                summary[f"{n} cycles avg. discharge capacity / mAh"]["Value"] = float(
                    cycle_df.loc[mask, "Discharge Capacity / mAh"].mean()
                )
                summary[f"{n} cycles avg. discharge capacity / mAh"]["Error"] = float(
                    cycle_df.loc[mask, "Discharge Capacity / mAh"].std()
                )

    writer = pd.ExcelWriter(folder / "results" / "summary.xlsx", engine="xlsxwriter")
    df = pd.DataFrame.from_dict(summary, orient="index").reset_index()
    df = df.rename(columns={"index": "Quantity"})
    df.to_excel(writer, sheet_name="Summary", index=False)
    workbook = writer.book
    worksheet = writer.sheets["Summary"]
    worksheet.autofit()

    if cycle_df is not None:
        cycle_df.to_excel(writer, sheet_name="Cycling", index=False)
        worksheet = writer.sheets["Cycling"]
        worksheet.autofit()
    if ratetest_df is not None:
        ratetest_df = ratetest_df.drop("Times seen", axis=1)
        ratetest_df.to_excel(writer, sheet_name="Ratetest", index=False)
        worksheet = writer.sheets["Ratetest"]
        worksheet.autofit()
    if cv_df is not None:
        cv_df = cv_df.loc[:, ["CV Cycle", *[c for c in cv_df.columns if c not in ["CV Cycle"]]]]
        cv_df.to_excel(writer, sheet_name="CV", index=False)
        worksheet = writer.sheets["CV"]
        worksheet.autofit()
    if lsv_df is not None:
        lsv_df.to_excel(writer, sheet_name="LSV", index=False)
        worksheet = writer.sheets["LSV"]
        worksheet.autofit()
    if eis_df is not None:
        eis_df.to_excel(writer, sheet_name="EIS", index=False)
        worksheet = writer.sheets["EIS"]
        worksheet.autofit()

    workbook.close()
    tracked_outputs["summary"] = [results_dir / "summary.xlsx"]

    tracked_extra_inputs: dict[str, list[Path]] = {}
    if mps_files:
        tracked_extra_inputs["protocol"] = mps_files
    if battinfo_xlsx_path:
        tracked_extra_inputs["battinfo_xlsx"] = [battinfo_xlsx_path]

    if battinfo_json is not None:
        zenodo_url = pub_info.get("zenodo_doi_url") if pub_info else None
        for label, paths in tracked_mpr_inputs.items():
            for path in paths:
                snippet = battinfo.add_data(
                    path.relative_to(folder).as_posix(),
                    zenodo_url,
                    extras={"rdfs:comment": MPR_DESCRIPTIONS.get(label)},
                )
                battinfo_json = battinfo.merge_jsonld_on_type([battinfo_json, snippet])
        for path in mps_files:
            snippet = battinfo.add_input_data(
                path.relative_to(folder).as_posix(),
                zenodo_url,
                "EC-Lab measurement protocol (.mps)",
            )
            battinfo_json = battinfo.merge_jsonld_on_type([battinfo_json, snippet])
        if battinfo_xlsx_path:
            snippet = battinfo.add_input_data(
                battinfo_xlsx_path.relative_to(folder).as_posix(),
                zenodo_url,
                "BattINFO converter Excel metadata input",
            )
            battinfo_json = battinfo.merge_jsonld_on_type([battinfo_json, snippet])
        for label_paths in tracked_outputs.values():
            for path in label_paths:
                rel = path.relative_to(folder).as_posix()
                with contextlib.suppress(ValueError):
                    snippet = battinfo.add_data(rel, zenodo_url)
                    battinfo_json = battinfo.merge_jsonld_on_type([battinfo_json, snippet])
        metadata_path = folder / f"metadata.{fcid or sample_id}.json"
        with metadata_path.open("w") as mf:
            json.dump(battinfo_json, mf, indent=4)
        tracked_outputs["metadata"] = [metadata_path]

    return tracked_outputs, tracked_extra_inputs, fcid, sample_id


def is_sample_folder(folderpath: str | Path, config: PyFlowBattConfig | None = None) -> bool:
    """Determine whether a folder is a sample folder.

    A sample folder contains at least one file matching any configured technique pattern.
    """
    folderpath = Path(folderpath)
    if not folderpath.is_dir():
        return False
    config = config or PyFlowBattConfig.load(folderpath)
    return any(any(folderpath.glob(p)) for p in config.all_patterns())


def find_all_sample_folders(
    folder: str | Path,
    max_search_depth: int = DEFAULT_DEPTH,
    max_folder_searches: int = DEFAULT_SEARCH,
    config: PyFlowBattConfig | None = None,
) -> list[Path]:
    """Find all sample folders in a folder."""
    folder = Path(folder)
    config = config or PyFlowBattConfig.load(folder)
    if is_sample_folder(folder, config):
        return [folder]
    sample_folders = []
    depth_exceeded_count = 0
    folders_searched = 0

    def recursive_check_folder(folder: Path, depth: int = 1) -> None:
        """Depth-first search for sample folders."""
        nonlocal folders_searched, depth_exceeded_count
        folders_searched += 1
        if folders_searched > max_folder_searches:
            return
        if depth > max_search_depth:
            depth_exceeded_count += 1
            return
        if is_sample_folder(folder, config):
            sample_folders.append(folder)
            return
        for subfolder in folder.iterdir():
            try:
                if subfolder.is_dir():
                    recursive_check_folder(subfolder, depth + 1)
            except (PermissionError, OSError):  # noqa: PERF203
                logger.error("Permission error on %s", subfolder)  # noqa: TRY400

    recursive_check_folder(folder)
    if depth_exceeded_count:
        logger.warning(
            "Search depth (%d) exceeded %d time(s) while looking for sample folders.",
            max_search_depth,
            depth_exceeded_count,
        )
    if folders_searched > max_folder_searches:
        logger.critical(
            "WARNING: Exceeded maximum number of folder searches (%d)!"
            "\nMake sure you are running PyFlowBatt on the correct folder!",
            max_folder_searches,
        )

    return sample_folders


def find_all_sample_summaries(
    folder: str | Path,
    max_search_depth: int = DEFAULT_DEPTH,
    max_folder_searches: int = DEFAULT_SEARCH,
    config: PyFlowBattConfig | None = None,
) -> list[Path]:
    """Collect all summary excels from all subfolders."""
    folder = Path(folder)
    sample_folders = find_all_sample_folders(folder, max_search_depth, max_folder_searches, config)
    return [
        f / "results" / "summary.xlsx"
        for f in sample_folders
        if (f / "results" / "summary.xlsx").exists()
    ]


def merge_summaries(
    summary_xlsxs: list[str | Path],
    sample_ids_by_sample_folder: dict[Path, str] | None = None,
) -> pd.DataFrame:
    """Merge all summary sheets into one mega summary."""
    summary_xlsxs_paths = [Path(s).resolve() for s in summary_xlsxs]
    names = [
        (sample_ids_by_sample_folder or {}).get(s.parent.parent)
        or get_sampleid_from_folderpath(s.parent.parent)
        for s in summary_xlsxs_paths
    ]
    # append a number to duplicate names
    for i, name in enumerate(names):
        if names.count(name) > 1:
            names[i] = f"{name} ({names.count(name)})"
    dfs = [pd.read_excel(s, sheet_name="Summary", index_col=0) for s in summary_xlsxs]
    dfs = [df["Value"] for df in dfs]
    for name, df in zip(names, dfs, strict=True):
        df.name = name
    df = pd.concat(dfs, axis=1)
    vals = df.copy()

    df["Average"] = vals.mean(axis=1)
    df["Std"] = vals.std(axis=1)

    # Make the index a new column
    df["Quantity"] = df.index
    df = df.reset_index(drop=True)

    # put these columns at the start
    cols = ["Quantity", "Average", "Std", *names]
    return df[cols]


def analyse_all_samples(
    folder: str | Path,
    *,
    save_format: SAVE_FORMATS = "parquet",
    max_search_depth: int = DEFAULT_DEPTH,
    max_folder_searches: int = DEFAULT_SEARCH,
) -> None:
    """Take a folder and run all analysis."""
    folder = Path(folder).resolve()
    pub_info = pub_info_from_root(folder)
    search_config = PyFlowBattConfig.load(folder)
    sample_folders = find_all_sample_folders(
        folder, max_search_depth, max_folder_searches, search_config
    )
    if len(sample_folders) == 0:
        logger.error("No sample folders found in %s", folder)
        return
    configs_by_sample_folder: dict[Path, PyFlowBattConfig] = {
        sample_folder: PyFlowBattConfig.load(sample_folder) for sample_folder in sample_folders
    }
    tracked_by_sample_folder: dict[Path, dict[str, list[Path]]] = {}
    tracked_extras_by_sample_folder: dict[Path, dict[str, list[Path]]] = {}
    fcids_by_sample_folder: dict[Path, str | None] = {}
    sample_ids_by_sample_folder: dict[Path, str] = {}
    if len(sample_folders) > 1:
        logger.info("Found %d sample folders:", len(sample_folders))
    for sample_folder in sample_folders:
        (
            tracked_by_sample_folder[sample_folder],
            tracked_extras_by_sample_folder[sample_folder],
            fcids_by_sample_folder[sample_folder],
            sample_ids_by_sample_folder[sample_folder],
        ) = analyse_sample(
            sample_folder,
            save_format=save_format,
            pub_info=pub_info,
            config=configs_by_sample_folder[sample_folder],
        )

    if len(sample_folders) > 1:
        summaries = find_all_sample_summaries(
            folder, max_search_depth, max_folder_searches, search_config
        )
        df = merge_summaries(summaries, sample_ids_by_sample_folder)
        writer = pd.ExcelWriter(folder / "combined_summary.xlsx", engine="xlsxwriter")
        df.to_excel(writer, index=False, sheet_name="Summary")
        workbook = writer.book
        worksheet = writer.sheets["Summary"]
        worksheet.autofit()
        workbook.close()
        logger.info("\n🎉 Combined all the results into one big summary")

    from pyflowbatt.rocrate_output import write_rocrate  # noqa: PLC0415

    write_rocrate(
        folder,
        sample_folders,
        tracked_by_sample_folder,
        tracked_extras_by_sample_folder,
        fcids_by_sample_folder,
        configs_by_sample_folder,
        sample_ids_by_sample_folder,
    )
    logger.info("📦 Written RO-Crate metadata to %s", folder / "ro-crate-metadata.json")


def dry_analyse_all_samples(
    folder: str | Path,
    max_search_depth: int = DEFAULT_DEPTH,
    max_folder_searches: int = DEFAULT_SEARCH,
) -> None:
    """Take a folder and tell the user what PyFlowBatt would do."""
    logger.info("Beginning dry-run search.")
    folder = Path(folder).resolve()
    config = PyFlowBattConfig.load(folder)
    sample_folders = find_all_sample_folders(folder, max_search_depth, max_folder_searches, config)
    if len(sample_folders) == 0:
        logger.error("No sample folders found in %s", folder)
        return
    if len(sample_folders) == 1:
        logger.info("Found 1 sample inside")
    else:
        logger.info("Found %d samples inside.", len(sample_folders))
    logger.info("I would analyse the following samples and make a 'results' subfolder inside:")
    for sample_folder in sample_folders:
        logger.info("  - %s", sample_folder)
    if len(sample_folders) > 1:
        logger.info(
            "Then I would combine all the summaries into one 'combined_results' subfolder inside %s.",
            folder,
        )


def pub_info_from_root(folder: Path) -> dict:
    """Get publication info from xlsx in root folder."""
    candidate_files = folder.glob("*.xlsx")
    for file in candidate_files:
        with contextlib.suppress(Exception):
            return battinfo.parse_zenodo_info_xlsx(file)
    return {}
