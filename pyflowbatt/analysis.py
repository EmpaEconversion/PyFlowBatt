"""Utility functions for analysing flow battery data from MPR files."""

import contextlib
import json
import logging
import re
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import unquote

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from battinfoconverter_backend import convert_excel_to_jsonld

from pyflowbatt import battinfo, cv, eis, gcpl, lsv, ocv
from pyflowbatt.config import (
    CONFIG_FILENAME,
    DEFAULT_AREA_CM2,
    EIS_TAGS,
    PyFlowBattConfig,
    classify_technique_files,
)
from pyflowbatt.read import read_to_bdf
from pyflowbatt.report import LOG_FILENAME, Reporter

logger = logging.getLogger(__name__)

SAVE_FORMATS = Literal["parquet", "csv"] | None
ROCRATE_FILENAME = "ro-crate-metadata.json"
DEFAULT_DEPTH = 6  # Default max search depth in folders
DEFAULT_SEARCH = 10000  # Default max number of folders searched

RAW_INPUT_DESCRIPTIONS: dict[str, str] = {
    "gcpl": "Galvanostatic cycling with potential limitation measurement",
    "ocv": "Open circuit voltage measurement",
    "lsv_pre": "Pre-cycling linear sweep voltammetry",
    "lsv_post": "Post-cycling linear sweep voltammetry",
    "cv_pre": "Pre-cycling cyclic voltammetry",
    "cv_post": "Post-cycling cyclic voltammetry",
    "eis_pre": "EIS measurement, pre-cycling",
    "eis_pre-50%SOC": "EIS measurement, pre-cycling 50% SOC",
    "eis_post-50%SOC": "EIS measurement, post-cycling 50% SOC",
    "eis_post": "EIS measurement, post-cycling",
}

# Descriptions for the plot (.png) tracked outputs, keyed by the same labels as the inputs.
OUTPUT_PLOT_DESCRIPTIONS: dict[str, str] = {
    "gcpl": (
        "Plots of voltage vs. time, "
        "and capacity, energy, coulombic efficiency, and voltage efficiency vs. cycle count "
        "for galvanostatic cycling with potential limitation (GCPL), "
        "i.e. constant-current-constant-voltage cycling"
    ),
    "lsv_pre": "Plot of current vs. voltage for pre-cycling linear sweep voltammetry",
    "lsv_post": "Plot of current vs. voltage for post-cycling linear sweep voltammetry",
    "cv_pre": "Plot of current vs. voltage for pre-cycling cyclic voltammetry",
    "cv_post": "Plot of current vs. voltage for post-cycling cyclic voltammetry",
    "eis_pre": (
        "Nyquist plot for electrochemical impedance spectroscopy (EIS) "
        r"from before cycling at 0% state of charge"
    ),
    "eis_pre-50%SOC": (
        "Nyquist plot for electrochemical impedance spectroscopy (EIS) "
        r"from before cycling at 50% state of charge"
    ),
    "eis_post-50%SOC": (
        "Nyquist plot for electrochemical impedance spectroscopy (EIS) "
        r"from after cycling at 50% state of charge"
    ),
    "eis_post": (
        "Nyquist plot for electrochemical impedance spectroscopy (EIS) "
        r"from after cycling at 0% state of charge"
    ),
}

# Descriptions for the time/frequency series (.parquet/.csv) tracked outputs, keyed by label.
OUTPUT_DATA_DESCRIPTIONS: dict[str, str] = {
    "gcpl": (
        "Time series data from galvanostatic cycling with potential limitation (GCPL), "
        "i.e. constant-current-constant-voltage cycling, "
        "using the battery data format (BDF)"
    ),
    "lsv_pre": (
        "Time series data from linear sweep voltammetry before battery cycling, "
        "using the battery data format (BDF)"
    ),
    "lsv_post": (
        "Time series data from linear sweep voltammetry after battery cycling, "
        "using the battery data format (BDF)"
    ),
    "cv_pre": (
        "Time series data from cyclic voltammetry before battery cycling, "
        "using the battery data format (BDF)"
    ),
    "cv_post": (
        "Time series data from cyclic voltammetry after battery cycling, "
        "using the battery data format (BDF)"
    ),
    "eis_pre": (
        "Frequency-domain electrochemical impedance spectroscopy (EIS) data "
        r"from before cycling at 0% state-of-charge, "
        "using the battery data format (BDF)"
    ),
    "eis_pre-50%SOC": (
        "Frequency-domain electrochemical impedance spectroscopy (EIS) data "
        r"from before cycling at 50% state-of-charge, "
        "using the battery data format (BDF)"
    ),
    "eis_post-50%SOC": (
        "Frequency-domain electrochemical impedance spectroscopy (EIS) data "
        r"from after cycling at 50% state-of-charge, "
        "using the battery data format (BDF)"
    ),
    "eis_post": (
        "Frequency-domain electrochemical impedance spectroscopy (EIS) data "
        r"from after cycling at 0% state-of-charge, "
        "using the battery data format (BDF)"
    ),
}

# Descriptions for tracked outputs that aren't plots or data series, keyed by label.
OUTPUT_MISC_DESCRIPTIONS: dict[str, str] = {
    "summary": "Per-sample analysis summary",
    "metadata": "BattINFO JSON-LD metadata",
    "included": "Additional file included with the dataset",
}

# Label for files matched by include_files in pyflowbatt.toml
INCLUDED_LABEL = "included"

# Descriptions for extra tracked inputs (protocol file, BattINFO xlsx), keyed by label.
EXTRA_INPUT_DESCRIPTIONS: dict[str, str] = {
    "protocol": "EC-Lab measurement protocol (.mps)",
    "battinfo_xlsx": "BattINFO converter Excel metadata input",
    "pub_info": "Publication and Zenodo deposition Excel metadata input",
    "settings": "PyFlowBatt settings used here, combining all pyflowbatt.toml files",
}

# Label for raw data files in a sample folder that no technique pattern matched.
OTHER_RAW_LABEL = "other_raw"
OTHER_RAW_DESCRIPTION = "Raw electrochemical data file, not used in the analysis"

# Numbered EIS labels, used when a folder's EIS layout doesn't match a known protocol.
# They carry no state of charge, so their descriptions only say which side of cycling
# the measurement came from.
_GENERIC_EIS_RE = re.compile(r"^eis_(pre|post)_(\d+)$")


def _generic_eis_parts(label: str) -> tuple[str, str] | None:
    """Split a numbered EIS label into ("before"|"after", number)."""
    match = _GENERIC_EIS_RE.match(label)
    if not match:
        return None
    return ("before" if match.group(1) == "pre" else "after", match.group(2))


def _custom_eis_tag(label: str) -> str | None:
    """Tag of an EIS label set in pyflowbatt.toml, e.g. "pre-20%SOC" from "eis_pre-20%SOC"."""
    if not label.startswith("eis_") or _GENERIC_EIS_RE.match(label):
        return None
    return label.removeprefix("eis_")


def raw_description(label: str, path: Path) -> str | None:
    """Describe a raw input file, naming the format only where we know what it is."""
    if label in RAW_INPUT_DESCRIPTIONS:
        description = RAW_INPUT_DESCRIPTIONS[label]
    elif label == OTHER_RAW_LABEL:
        description = OTHER_RAW_DESCRIPTION
    elif parts := _generic_eis_parts(label):
        when, number = parts
        description = f"EIS measurement, {when} cycling, measurement {number}"
    elif tag := _custom_eis_tag(label):
        description = f"EIS measurement, tagged {tag}"
    else:
        return None
    if path.suffix.lower() == ".mpr":
        return f"{description} (EC-Lab MPR)"
    return description


def plot_description(label: str) -> str | None:
    """Describe a plot (.png) output for the given label."""
    if label in OUTPUT_PLOT_DESCRIPTIONS:
        return OUTPUT_PLOT_DESCRIPTIONS[label]
    if parts := _generic_eis_parts(label):
        when, number = parts
        return (
            "Nyquist plot for electrochemical impedance spectroscopy (EIS) "
            f"from {when} cycling, measurement {number}"
        )
    if tag := _custom_eis_tag(label):
        return f"Nyquist plot for electrochemical impedance spectroscopy (EIS), tagged {tag}"
    return None


def data_description(label: str) -> str | None:
    """Describe a data series (.parquet/.csv) output for the given label."""
    if label in OUTPUT_DATA_DESCRIPTIONS:
        return OUTPUT_DATA_DESCRIPTIONS[label]
    if parts := _generic_eis_parts(label):
        when, number = parts
        return (
            "Frequency-domain electrochemical impedance spectroscopy (EIS) data "
            f"from {when} cycling, measurement {number}, using the battery data format (BDF)"
        )
    if tag := _custom_eis_tag(label):
        return (
            "Frequency-domain electrochemical impedance spectroscopy (EIS) data, "
            f"tagged {tag}, using the battery data format (BDF)"
        )
    return None


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
    substrate = electrode.get("hasSubstrate") or electrode.get("Substrate")  # old schema name
    if not isinstance(substrate, dict):
        return None
    properties = substrate.get("hasMeasuredProperty") or []
    if isinstance(properties, dict):
        properties = [properties]
    for prop in properties:
        if not isinstance(prop, dict) or prop.get("@type") != "Area":
            continue
        unit = prop.get("hasMeasurementUnit")
        if unit not in {"unit:CentiM2", "SquareCentiMetre", "emmo:SquareCentiMetre"}:
            logger.warning(
                "Ignoring %s Area with unit %s in BattINFO file "
                "(expected unit:CentiM2 or SquareCentiMetre)",
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


def _rel(path: Path, root: Path) -> str:
    """Return a forward-slash relative path string from root."""
    return path.relative_to(root).as_posix()


def check_path_writable(path: Path) -> None:
    r"""Raise a clear ``PermissionError`` early if ``path`` can't be created.

    Writing the zip only happens after the full analysis runs, so without this
    check a permissions problem (e.g. ``path`` sitting directly in a drive root
    like ``C:\``) only surfaces at the very end, wasting all that work.
    """
    probe = path.parent / f".pyflowbatt_write_test_{uuid.uuid4().hex}"
    try:
        probe.touch()
    except OSError as e:
        msg = (
            f"Cannot write '{path.name}' to {path.parent}: {e}. "
            r"Pick an output folder you have write access to (like documents, avoid C:\.), "
            r"or run as administrator."
        )
        raise PermissionError(msg) from e
    else:
        probe.unlink()


def is_pyflowbatt_file(path: Path) -> bool:
    """Check for files PyFlowBatt writes or reads as settings, which it always adds itself."""
    name = path.name
    return name in {CONFIG_FILENAME, LOG_FILENAME, "combined_summary.xlsx"} or (
        name.startswith("metadata.") and name.endswith(".json")
    )


def find_included_files(folder: Path, patterns: list[str]) -> list[Path]:
    """Files in folder matching any include_files glob pattern, in a stable order.

    Matches outside folder, e.g. from "../" in a pattern, are left out with a warning.
    """
    base = folder.resolve()
    found = set()
    for pattern in patterns:
        try:
            matches = list(folder.glob(pattern))
        except (NotImplementedError, ValueError):
            logger.warning("Ignoring include_files pattern %r: it must be relative", pattern)
            continue
        for path in matches:
            resolved = path.resolve()
            if not resolved.is_relative_to(base):
                logger.warning(
                    "Ignoring %s from include_files pattern %r: it is outside %s",
                    resolved,
                    pattern,
                    folder,
                )
            elif path.is_file() and not is_pyflowbatt_file(path):
                found.add(folder / resolved.relative_to(base))
    return sorted(found)


def _silently_skipped(file: Path) -> bool:
    """Check for hidden EC-lab files, Excel lock files, the PyFlowBatt log and config files.

    The zip holds resolved pyflowbatt.toml files in place of the originals.
    """
    return (
        file.suffix in {".mgr", ".sta", ".mpp", ".mgp"}
        or file.name.startswith("~$")
        or file.name in {LOG_FILENAME, CONFIG_FILENAME}
    )


def crate_file_paths(crate_path: Path) -> list[str]:
    """Root-relative paths of the files an RO-Crate manifest describes."""
    crate = json.loads(crate_path.read_text(encoding="utf-8"))
    paths = []
    for entity in crate["@graph"]:
        types = entity["@type"] if isinstance(entity["@type"], list) else [entity["@type"]]
        if "File" in types:
            paths.append(unquote(entity["@id"]))
    return paths


def resolved_settings(
    root_folder: Path,
    configs_by_sample_folder: dict[Path, PyFlowBattConfig],
    root_config: PyFlowBattConfig,
) -> dict[str, str]:
    """Write resolved pyflowbatt.toml text for the root and each sample folder, by path."""
    settings = {
        _rel(sample_folder / CONFIG_FILENAME, root_folder): config.to_resolved_toml()
        for sample_folder, config in configs_by_sample_folder.items()
    }
    settings.setdefault(CONFIG_FILENAME, root_config.to_resolved_toml())
    return settings


def _escapes_root(rel: str) -> bool:
    """Check if a zip entry name is absolute or climbs out with "..", so could extract anywhere."""
    posix = rel.replace("\\", "/")
    return (
        posix.startswith("/")
        or re.match(r"^[A-Za-z]:", posix) is not None
        or ".." in PurePosixPath(posix).parts
    )


def zip_folder(
    root_folder: Path,
    zip_path: Path,
    members: list[str],
    generated: dict[str, str] | None = None,
) -> Path:
    """Zip the listed root-relative files into zip_path, with no wrapping top-level directory.

    Archive members are root_folder-relative paths, matching the "@id"s used for
    files in both the RO-Crate manifest and BattINFO metadata, so a file's "@id"
    can be found directly inside the zip once it's extracted. Other files in
    root_folder are left out with a warning. `generated` maps archive paths to text
    written straight into the zip, never to root_folder. Entries that could extract
    outside the target folder, like "../x" or "C:/x", are never written.
    """
    generated = generated or {}
    unsafe = sorted(rel for rel in {*members, *generated} if _escapes_root(rel))
    if unsafe:
        logger.warning("Not zipping %s: zip entries must stay inside the zipped folder", unsafe)
    generated = {rel: text for rel, text in generated.items() if rel not in unsafe}
    wanted = set(members) - set(unsafe)
    skipped = [
        rel
        for file in sorted(root_folder.rglob("*"))
        if file.is_file()
        and not _silently_skipped(file)
        and (rel := _rel(file, root_folder)) not in wanted
    ]
    if skipped:
        shown = ", ".join(skipped[:10]) + (
            f" and {len(skipped) - 10} more" if len(skipped) > 10 else ""
        )
        logger.warning(
            "Not zipping %d file(s) the metadata doesn't describe, "
            "add them to include_files in pyflowbatt.toml to package them: %s",
            len(skipped),
            shown,
        )
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in sorted(wanted - set(generated)):
            zf.write(root_folder / rel, arcname=rel)
        for rel, text in sorted(generated.items()):
            zf.writestr(rel, text)
    return zip_path


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
    pub_info_path: str | Path | None = None,
    save_format: SAVE_FORMATS = "parquet",
    config: PyFlowBattConfig | None = None,
    root_folder: str | Path | None = None,
    zip_filename: str | None = None,
    reporter: Reporter | None = None,
) -> tuple[dict[str, list[Path]], dict[str, list[Path]], dict[str, list[Path]], str | None, str]:
    """Read all the files in a folder, analayse and plot everything.

    Returns the tracked outputs, the extra inputs (protocol and metadata files), every
    raw data file found (keyed by technique label, with unmatched files under
    ``OTHER_RAW_LABEL``), the FCID and the sample ID.

    Without an explicit config, loads a ``pyflowbatt.toml`` cascade (home directory,
    parent folder, sample folder) to customise technique glob patterns and sample ID
    detection.

    ``root_folder`` is the top-level folder that will be uploaded to Zenodo (defaults
    to ``folder`` itself). BattINFO metadata file paths are recorded relative to it, so
    that when there are multiple sample folders under one root, "@id"s stay unambiguous
    and match where the file actually sits once the root folder is packaged for Zenodo.
    Every file's "@id" is this bare root-relative path, matching its "@id" in the
    RO-Crate manifest (ro-crate-metadata.json); this deliberately never changes with
    zipping or Zenodo upload, so the two documents can be cross-referenced by "@id" alone.

    ``pub_info_path`` is the xlsx ``pub_info`` was parsed from; when given it is recorded
    as an input alongside the protocol and BattINFO xlsx files. Dataset details from the
    BattINFO sheet take priority over ``pub_info``, which takes priority over defaults.

    ``zip_filename`` is the zip the root folder is packaged into for Zenodo. With it, and
    a Zenodo record from the BattINFO sheet's Dataset URL or ``pub_info["zenodo_doi_url"]``,
    each file gets a "dcat:downloadURL" (separate from its "@id") into that zip, with the
    in-archive path as a fragment (see :func:`pyflowbatt.battinfo.zenodo_download_url`).

    ``reporter`` is told when each step starts and finishes, to show progress.
    """
    folder = Path(folder)
    root_folder = Path(root_folder) if root_folder is not None else folder
    pub_info = pub_info or {}
    pub_info_path = Path(pub_info_path) if pub_info_path is not None else None
    fcid: str | None = None
    config = config or PyFlowBattConfig.load(folder)
    reporter = reporter or Reporter()

    logger.info("Analysing %s", folder.name)
    classified = classify_technique_files(folder, config, warn=True)
    gcpl_files = classified.get("gcpl", [])
    gcpl_file = gcpl_files[0] if gcpl_files else None
    ocv_files = classified.get("ocv", [])
    cv_files_before = classified.get("cv_pre", [])
    cv_files_after = classified.get("cv_post", [])
    eis_by_tag = {
        label.removeprefix("eis_"): paths[0]
        for label, paths in classified.items()
        if label.startswith("eis_")
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
    reporter.plan(
        [
            "BattINFO",
            "OCV",
            "GCPL",
            *([f"LSV {p}" for p in ("pre", "post") if classified.get(f"lsv_{p}")] or ["LSV"]),
            *(
                [
                    f"CV {p}"
                    for p, files in (("pre", cv_files_before), ("post", cv_files_after))
                    if files
                ]
                or ["CV"]
            ),
            *([f"EIS {tag}" for tag in eis_by_tag] or ["EIS"]),
            "Summary",
            "Metadata",
        ]
    )

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

    battinfo_json = None
    battinfo_sample_id = None
    battinfo_cell: dict | None = None
    zenodo_url: str | None = None
    sheet_files: dict[str, str | None] = {}
    if not battinfo_files:
        logger.debug("No BattINFO xlsx found, skipping")
        reporter.skip("BattINFO", "no xlsx")
    else:
        with reporter.task("BattINFO"):
            for file in battinfo_files:
                raw_json = None
                with contextlib.suppress(ValueError):
                    raw_json = convert_excel_to_jsonld(file)
                if raw_json is None:
                    continue
                battinfo_xlsx_path = file
                # The cell might already be in a BatteryTest
                battinfo_json = battinfo.make_test_object(raw_json)
                battinfo_cell = battinfo_json["hasTestObject"]
                if not battinfo.is_cell_type(battinfo_cell.get("@type")):
                    logger.warning(
                        "Cell type %r in %s doesn't look like a battery or cell class, "
                        "the metadata will describe the tested object as that",
                        battinfo_cell.get("@type"),
                        _rel(file, root_folder),
                    )
                fcid = battinfo_cell["schema:productID"]
                battinfo_sample_id = battinfo_cell["schema:name"]
                # pyflowbatt lists the files it finds, the sheet only adds descriptions
                sheet_files = battinfo.pop_sheet_files(battinfo_json)
                zenodo_url = battinfo.resolve_zenodo_url(
                    battinfo_json, pub_info.get("zenodo_doi_url")
                )

                # Publication info only fills what the BattINFO sheet leaves out
                pub_list: list[dict] = [{"@type": "BatteryTest"}]
                if zenodo_url:
                    pub_list.append(battinfo.add_zenodo_url(zenodo_url))
                if pub_info.get("citation_string"):
                    pub_list.append(battinfo.add_citation(pub_info["citation_string"]))
                if pub_info.get("institution"):
                    pub_list.append(
                        battinfo.add_institution(
                            pub_info["institution"],
                            pub_info.get("institutions", {})
                            .get(pub_info["institution"], {})
                            .get("wikidata_url"),
                        )
                    )
                if pub_info.get("authors") and pub_info.get("institutions"):
                    pub_list.append(
                        battinfo.add_authors(pub_info["authors"], pub_info["institutions"]),
                    )
                if pub_info.get("sample_to_fig"):
                    pub_list.append(
                        battinfo.add_associated_media(
                            pub_info.get("publication_doi_url"),
                            pub_info["sample_to_fig"],
                            fcid,
                            battinfo_sample_id,
                        ),
                    )
                defaults = battinfo.merge_jsonld_on_type(
                    [battinfo.add_input_and_output(), battinfo.add_software()]
                )

                battinfo_json = battinfo.priority_merge(
                    battinfo_json,
                    battinfo.merge_jsonld_on_type(pub_list),
                    names=("BattINFO sheet", "publication info"),
                )
                battinfo_json = battinfo.priority_merge(battinfo_json, defaults)
                break
            else:
                logger.warning("Couldn't convert any xlsx to BattINFO, no metadata written")

    sample_id = get_sampleid_from_folderpath(folder, config, battinfo_sample_id)
    area_cm2 = get_area_cm2(config, battinfo_cell)
    assembled_resistance_ohm = get_assembled_resistance_ohm(
        config, battinfo_cell, gcpl_files[0].stem if gcpl_files else None
    )

    av_ocv = (np.nan, np.nan)
    if len(ocv_files) == 0:
        logger.debug("No OCV found, skipping")
        reporter.skip("OCV", "no files")
    else:
        with reporter.task("OCV"):
            try:
                df = read_to_bdf(ocv_files[0])
                av_ocv = ocv.analyse(df)
                tracked_mpr_inputs["ocv"] = [ocv_files[0]]
            except Exception:
                logger.exception("Failed to analyse OCV")

    if gcpl_file is None:
        logger.debug("No GCPL files found, skipping")
        reporter.skip("GCPL", "no files")
    else:
        with reporter.task("GCPL"):
            try:
                df, cycle_df = gcpl.analyse(read_to_bdf(gcpl_file))
                fig, _ax = gcpl.plot(df, cycle_df)
                fig.savefig(results_dir / "gcpl.png")
                plt.close(fig)
                ratetest_df = gcpl.cycles_to_ratetest(cycle_df)
                df_save_bdf(df, results_dir / "gcpl.x", save_format=save_format)
                tracked_outputs["gcpl"] = [results_dir / "gcpl.png"]
                if bdf_suffix:
                    tracked_outputs["gcpl"].append((results_dir / "gcpl.x").with_suffix(bdf_suffix))
                tracked_mpr_inputs["gcpl"] = [gcpl_file]
            except Exception as e:
                cycle_df = None
                logger.error("Failed to analyse %s: %s", gcpl_file.stem, str(e))  # noqa: TRY400

    lsv_res = {"pre": np.nan, "post": np.nan}
    lsv_pre_files = classified.get("lsv_pre", [])
    lsv_post_files = classified.get("lsv_post", [])
    if not lsv_pre_files and not lsv_post_files:
        logger.debug("No LSV files found, skipping")
        reporter.skip("LSV", "no files")
    else:
        lsv_rows = []
        for p, lsv_files_p in [("pre", lsv_pre_files), ("post", lsv_post_files)]:
            if not lsv_files_p:
                continue
            with reporter.task(f"LSV {p}"):
                if bool(lsv_pre_files) != bool(lsv_post_files):
                    logger.warning("Only one LSV file found, assuming it is %s", p)
                try:
                    df = read_to_bdf(lsv_files_p[0])
                    results = lsv.analyse(df, area_cm2=area_cm2)
                    results["File name"] = lsv_files_p[0].name
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
                except Exception:
                    logger.exception("Failed to analyse LSV file")
        if lsv_rows:
            lsv_df = pd.DataFrame(lsv_rows)

    cv_res = {"pre": np.nan, "post": np.nan}
    if len(cv_files_before) == 0 and len(cv_files_after) == 0:
        logger.debug("No CV files found, skipping")
        reporter.skip("CV", "no files")
    else:
        for p, cv_files in [("pre", cv_files_before), ("post", cv_files_after)]:
            if not cv_files:
                continue
            with reporter.task(f"CV {p}"):
                try:
                    df, cv_df, capacitance_mF = cv.analyse(
                        read_to_bdf(cv_files[0]),
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
                except Exception as e:
                    logger.error("Failed to analyse %s: %s", cv_files[0].stem, str(e))  # noqa: TRY400

    eis_res = {}
    rows = []
    eis_units: dict[str, str] = {}
    has_ml_guess = False
    eis_to_fit = eis_by_tag
    if not eis_by_tag:
        logger.debug("No EIS files found, skipping")
        reporter.skip("EIS", "no files")
    else:
        try:
            eis_units, has_ml_guess = eis.check_model(config.eis_model, config.eis_summary_params)
        except ValueError as e:
            logger.warning("Invalid EIS model %s, skipping EIS: %s", config.eis_model, e)
            for tag in eis_by_tag:
                reporter.skip(f"EIS {tag}", "invalid model")
            eis_to_fit = {}
    for tag, eis_file in eis_to_fit.items():
        f = Path(eis_file)
        with reporter.task(f"EIS {tag}"):
            try:
                df = read_to_bdf(f)
                params, Z_fit = eis.analyse(
                    df, label=f.stem, model=config.eis_model, guess_init=has_ml_guess
                )
                fig, _ax = eis.plot(df, Z_fit)
                fig.savefig(results_dir / f"eis_{tag}.png")
                plt.close(fig)
                eis_res[tag] = params
                df["Real Impedance Fit / ohm"] = np.real(Z_fit)
                df["Imaginary Impedance Fit / ohm"] = np.imag(Z_fit)
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
                logger.error("Failed to fit %s: %s", f.stem, str(e))  # noqa: TRY400
    if rows:
        eis_df = pd.DataFrame(rows)
        eis_df = eis_df.pivot(index=["file", "tag"], columns=["name"]).reset_index()
        eis_df.columns = [f"{name}_{field}" if name else field for field, name in eis_df.columns]
        order = [f"{name}_{x}" for name in eis_units for x in ["value", "err", "unit"]]
        eis_df = eis_df[["file", "tag", *order]]

    with reporter.task("Summary", detail="results/summary.xlsx"):
        # Standard tags always get rows so combined summaries line up, other tags follow
        eis_tags = [*EIS_TAGS, *(tag for tag in eis_res if tag not in EIS_TAGS)]
        eis_rows = {}
        for param, unit in eis.summary_units(config.eis_summary_params).items():
            for tag in eis_tags:
                fit = eis_res.get(tag, {}).get(param, {})
                label = eis.summary_label(param, tag, unit)
                eis_rows[label] = {"Value": fit.get("value"), "Error": fit.get("err")}

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
            *eis_rows,
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
        summary.update(eis_rows)

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

    # Every raw file in the folder, whether or not its analysis produced anything, so the
    # metadata describes the data that ships rather than only what could be analysed.
    raw_inputs: dict[str, list[Path]] = {label: list(paths) for label, paths in classified.items()}
    classified_files = {path for paths in classified.values() for path in paths}
    # Protocols, the BattINFO sheet and PyFlowBatt's own files are added in their own role
    own_inputs = {*mps_files, *([battinfo_xlsx_path] if battinfo_xlsx_path else [])}
    other_raw = [
        path
        for ext in config.extensions
        for path in sorted(folder.glob(f"*{ext}"))
        if path not in classified_files and path not in own_inputs and not is_pyflowbatt_file(path)
    ]
    if other_raw:
        logger.info("- Found %d raw data file(s) not matching any technique", len(other_raw))
        raw_inputs[OTHER_RAW_LABEL] = other_raw

    tracked_extra_inputs: dict[str, list[Path]] = {}
    if mps_files:
        tracked_extra_inputs["protocol"] = mps_files
    if battinfo_xlsx_path:
        tracked_extra_inputs["battinfo_xlsx"] = [battinfo_xlsx_path]
    if pub_info_path and pub_info_path != battinfo_xlsx_path:
        tracked_extra_inputs["pub_info"] = [pub_info_path]

    already_tracked = {
        path
        for group in (raw_inputs, tracked_extra_inputs, tracked_outputs)
        for paths in group.values()
        for path in paths
    }
    # Files the BattINFO sheet asks for are included like include_files, if in this sample
    sheet_requested = {
        root_folder / rel
        for rel in sheet_files
        if (root_folder / rel).is_file()
        and (root_folder / rel).resolve().is_relative_to(folder.resolve())
        and not is_pyflowbatt_file(root_folder / rel)
    }
    included = sorted(
        {*find_included_files(folder, config.include_files), *sheet_requested} - already_tracked
    )
    if included:
        tracked_outputs[INCLUDED_LABEL] = included

    if battinfo_json is not None:
        with reporter.task("Metadata"):

            def describe(rel: str, default: str | None) -> str | None:
                """Prefer the BattINFO sheet's description of a file over pyflowbatt's."""
                sheet_file = sheet_files.pop(rel, None)
                return (sheet_file.comment if sheet_file else None) or default

            for label, paths in raw_inputs.items():
                for path in paths:
                    rel = _rel(path, root_folder)
                    snippet = battinfo.add_data(
                        rel,
                        zenodo_url,
                        extras={"rdfs:comment": describe(rel, raw_description(label, path))},
                        zip_filename=zip_filename,
                    )
                    battinfo_json = battinfo.merge_jsonld_on_type([battinfo_json, snippet])
            input_files = [
                *((path, "protocol") for path in mps_files),
                *((path, "battinfo_xlsx") for path in [battinfo_xlsx_path] if path),
                *((path, "pub_info") for path in tracked_extra_inputs.get("pub_info", [])),
                # Only exists in the zip, written from the resolved settings
                *([(folder / CONFIG_FILENAME, "settings")] if zip_filename else []),
            ]
            for path, label in input_files:
                rel = _rel(path, root_folder)
                snippet = battinfo.add_input_data(
                    rel,
                    zenodo_url,
                    describe(rel, EXTRA_INPUT_DESCRIPTIONS[label]),
                    zip_filename=zip_filename,
                )
                battinfo_json = battinfo.merge_jsonld_on_type([battinfo_json, snippet])
            for label, label_paths in tracked_outputs.items():
                for path in label_paths:
                    rel = _rel(path, root_folder)
                    if label == INCLUDED_LABEL:
                        # Input file rows in the BattINFO sheet stay inputs
                        sheet_file = sheet_files.get(rel)
                        is_input = sheet_file is not None and sheet_file.section == "hasInput"
                        add = battinfo.add_input_data if is_input else battinfo.add_included_data
                        snippet = add(
                            rel,
                            zenodo_url,
                            describe(rel, OUTPUT_MISC_DESCRIPTIONS[label]),
                            zip_filename=zip_filename,
                        )
                        battinfo_json = battinfo.merge_jsonld_on_type([battinfo_json, snippet])
                        continue
                    if path.suffix == ".png":
                        comment = plot_description(label)
                    elif rel.endswith((".parquet", ".csv")):
                        comment = data_description(label)
                    else:
                        comment = OUTPUT_MISC_DESCRIPTIONS.get(label)
                    comment = describe(rel, comment)
                    extras = {"rdfs:comment": comment} if comment else None
                    snippet = battinfo.add_data(
                        rel,
                        zenodo_url,
                        extras=extras,
                        zip_filename=zip_filename,
                    )
                    battinfo_json = battinfo.merge_jsonld_on_type([battinfo_json, snippet])
            for rel in sheet_files:
                where = "outside this sample folder" if (root_folder / rel).is_file() else "missing"
                logger.warning("The BattINFO sheet lists %s, which is %s, ignoring it", rel, where)
            metadata_path = folder / f"metadata.{fcid or sample_id}.json"
            with metadata_path.open("w", encoding="utf-8") as mf:
                json.dump(battinfo_json, mf, indent=4, ensure_ascii=False)
            tracked_outputs["metadata"] = [metadata_path]
    else:
        reporter.skip("Metadata", "no BattINFO")

    return tracked_outputs, tracked_extra_inputs, raw_inputs, fcid, sample_id


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


def _merged_row_order(indexes: list[pd.Index]) -> list[str]:
    """All row labels, each label missing from earlier summaries placed after its predecessor."""
    order: list[str] = []
    for index in indexes:
        prev = None
        for label in index:
            if label not in order:
                order.insert(order.index(prev) + 1 if prev is not None else 0, label)
            prev = label
    return order


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
    df = pd.concat(dfs, axis=1).reindex(_merged_row_order([df.index for df in dfs]))
    vals = df.copy()

    df["Average"] = vals.mean(axis=1)
    df["Std"] = vals.std(axis=1)

    # Make the index a new column
    df["Quantity"] = df.index
    df = df.reset_index(drop=True)

    # put these columns at the start
    cols = ["Quantity", "Average", "Std", *names]
    return df[cols]


def analyse_all_samples(  # noqa: PLR0913
    folder: str | Path,
    *,
    save_format: SAVE_FORMATS = "parquet",
    max_search_depth: int = DEFAULT_DEPTH,
    max_folder_searches: int = DEFAULT_SEARCH,
    zip_output: bool = False,
    reporter: Reporter | None = None,
) -> None:
    """Take a folder and run all analysis.

    If ``zip_output`` is True, everything is zipped into a single zip file next to
    ``folder`` once analysis finishes, for uploading to Zenodo. The zip holds an RO-Crate
    manifest, and BattINFO files get download URLs into the zip. Otherwise (the default)
    only the analysis results and BattINFO metadata are written.

    ``reporter`` is told when each sample and step starts and finishes, to show progress.
    """
    folder = Path(folder).resolve()
    reporter = reporter or Reporter()
    pub_info, pub_info_path = pub_info_from_root(folder)
    zip_filename = pub_info.get("zenodo_zip_filename") or f"{folder.name}.zip"
    if zip_output:
        check_path_writable(folder.parent / zip_filename)
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
    raw_inputs_by_sample_folder: dict[Path, dict[str, list[Path]]] = {}
    fcids_by_sample_folder: dict[Path, str | None] = {}
    sample_ids_by_sample_folder: dict[Path, str] = {}
    reporter.start(folder, len(sample_folders))
    for i, sample_folder in enumerate(sample_folders, start=1):
        name = _rel(sample_folder, folder) if sample_folder != folder else folder.name
        with reporter.section(f"[{i}/{len(sample_folders)}] {name}"):
            (
                tracked_by_sample_folder[sample_folder],
                tracked_extras_by_sample_folder[sample_folder],
                raw_inputs_by_sample_folder[sample_folder],
                fcids_by_sample_folder[sample_folder],
                sample_ids_by_sample_folder[sample_folder],
            ) = analyse_sample(
                sample_folder,
                save_format=save_format,
                pub_info=pub_info,
                pub_info_path=pub_info_path,
                config=configs_by_sample_folder[sample_folder],
                root_folder=folder,
                zip_filename=zip_filename if zip_output else None,
                reporter=reporter,
            )

    from pyflowbatt.rocrate_output import write_rocrate  # noqa: PLC0415

    with reporter.section("Finishing"):
        reporter.plan(
            [
                *(["Combined summary"] if len(sample_folders) > 1 else []),
                *(["RO-Crate", "Zip"] if zip_output else []),
            ]
        )
        if len(sample_folders) > 1:
            with reporter.task("Combined summary", detail="combined_summary.xlsx"):
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
                logger.debug("Combined all the results into one big summary")

        if zip_output:
            crate_path = folder / ROCRATE_FILENAME
            zip_path = folder.parent / zip_filename
            settings = resolved_settings(folder, configs_by_sample_folder, search_config)
            try:
                with reporter.task("RO-Crate", detail=ROCRATE_FILENAME):
                    root_included = [
                        path
                        for path in find_included_files(folder, search_config.include_files)
                        if not any(
                            path in paths
                            for by_sample in (
                                tracked_by_sample_folder,
                                tracked_extras_by_sample_folder,
                                raw_inputs_by_sample_folder,
                            )
                            for tracked in by_sample.values()
                            for paths in tracked.values()
                        )
                    ]
                    write_rocrate(
                        folder,
                        sample_folders,
                        tracked_by_sample_folder,
                        tracked_extras_by_sample_folder,
                        fcids_by_sample_folder,
                        configs_by_sample_folder,
                        sample_ids_by_sample_folder,
                        raw_inputs_by_sample_folder,
                        root_included=root_included,
                        settings_files=list(settings),
                    )
                with reporter.task("Zip", detail=str(zip_path)):
                    zip_folder(
                        folder,
                        zip_path,
                        [*crate_file_paths(crate_path), ROCRATE_FILENAME],
                        generated=settings,
                    )
                    logger.debug("Zipped everything into %s", zip_path)
            finally:
                # The ro-crate metadata only exists in the zip
                crate_path.unlink(missing_ok=True)

    reporter.finish()


def dry_analyse_all_samples(
    folder: str | Path,
    max_search_depth: int = DEFAULT_DEPTH,
    max_folder_searches: int = DEFAULT_SEARCH,
    *,
    save_format: SAVE_FORMATS = "parquet",
    zip_output: bool = False,
) -> None:
    """Take a folder and tell the user what PyFlowBatt would do."""
    logger.info("Beginning dry-run search.")
    folder = Path(folder).resolve()
    config = PyFlowBattConfig.load(folder)
    pub_info, pub_info_path = pub_info_from_root(folder)
    sample_folders = find_all_sample_folders(folder, max_search_depth, max_folder_searches, config)
    if len(sample_folders) == 0:
        logger.error("No sample folders found in %s", folder)
        return
    if len(sample_folders) == 1:
        logger.info("Found 1 sample inside")
    else:
        logger.info("Found %d samples inside.", len(sample_folders))
    for sample_folder in sample_folders:
        logger.info("  - %s", sample_folder.relative_to(folder))
        sample_config = PyFlowBattConfig.load(sample_folder)
        classified = classify_technique_files(sample_folder, sample_config)
        tagged_files: list[tuple[str, Path]] = [
            (tag, f) for tag, files in classified.items() for f in files
        ]
        tagged_files += [("protocol", f) for f in sample_folder.glob("*.mps")]
        tagged_files += [("battinfo_xlsx", f) for f in sample_folder.glob("*.xlsx")]
        tagged_files.sort(key=lambda item: item[1].name)
        if tagged_files:
            tag_width = max(len(tag) for tag, _ in tagged_files)
            for tag, file in tagged_files:
                logger.info("      - %-*s %s", tag_width + 1, f"{tag}:", file.name)
        else:
            logger.info("      (no recognised technique files found)")
    if pub_info_path:
        logger.info(
            "I would read publication info from '%s' and record it as a metadata input.",
            pub_info_path.name,
        )
    logger.info(
        "In each sample folder, I would analyse this data and make a 'results' subfolder inside."
    )
    logger.info("Results would be saved as battery data format in '%s' files.", save_format)
    if len(sample_folders) > 1:
        logger.info(
            "Then I would combine all the summaries into one 'combined_results.xlsx' "
            "at the root '%s'.",
            folder.name,
        )
    if zip_output:
        zip_filename = pub_info.get("zenodo_zip_filename") or f"{folder.name}.zip"
        logger.info(
            "Then I would write an RO-Crate manifest, zip everything and output to %s.",
            folder.parent / zip_filename,
        )
        logger.info("BattINFO metadata would reference files via that zip's download URL.")
    else:
        logger.info(
            "Files would be left unzipped (pass --zip to package them with an RO-Crate "
            "manifest for Zenodo)."
        )


def pub_info_from_root(folder: Path) -> tuple[dict, Path | None]:
    """Get publication info, and the xlsx it came from, from the root folder.

    Returns an empty dict and None when no xlsx in the folder parses as publication info.
    """
    candidate_files = folder.glob("*.xlsx")
    for file in candidate_files:
        with contextlib.suppress(Exception):
            return battinfo.parse_zenodo_info_xlsx(file), file
    return {}, None
