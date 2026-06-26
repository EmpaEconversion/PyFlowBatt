"""Write RO-Crate metadata for a batch PyFlowBatt analysis."""

from __future__ import annotations

import re
from pathlib import Path

from rocrate.rocrate import ROCrate

from PyFlowBatt.analysis import get_sampleid_from_folderpath

MEASUREMENT_LABELS: dict[str, str] = {
    "gcpl": "Galvanostatic Cycling with Potential Limitation",
    "ocv": "Open Circuit Voltage",
    "lsv_pre": "Linear Sweep Voltammetry (pre-cycling)",
    "lsv_post": "Linear Sweep Voltammetry (post-cycling)",
    "cv_pre": "Cyclic Voltammetry (pre-cycling)",
    "cv_post": "Cyclic Voltammetry (post-cycling)",
    "eis_pre": "Electrochemical Impedance Spectroscopy (pre-cycling, 0% SOC)",
    "eis_pre-50%SOC": "Electrochemical Impedance Spectroscopy (pre-cycling, 50% SOC)",
    "eis_post-50%SOC": "Electrochemical Impedance Spectroscopy (post-cycling, 50% SOC)",
    "eis_post": "Electrochemical Impedance Spectroscopy (post-cycling, 0% SOC)",
}

ENCODING_FORMATS: dict[str, str] = {
    ".parquet": "application/x-parquet",
    ".csv": "text/csv",
    ".png": "image/png",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".mpr": "application/octet-stream",
}

EIS_TAGS = ["eis_pre", "eis_pre-50%SOC", "eis_post-50%SOC", "eis_post"]

OUTPUT_DESCRIPTIONS: dict[str, str] = {
    "gcpl": "Galvanostatic cycling analysis",
    "lsv_pre": "Pre-cycling linear sweep voltammetry analysis",
    "lsv_post": "Post-cycling linear sweep voltammetry analysis",
    "cv_pre": "Pre-cycling cyclic voltammetry analysis",
    "cv_post": "Post-cycling cyclic voltammetry analysis",
    "eis_pre": "EIS analysis, pre-cycling at 0% SOC",
    "eis_pre-50%SOC": "EIS analysis, pre-cycling at 50% SOC",
    "eis_post-50%SOC": "EIS analysis, post-cycling at 50% SOC",
    "eis_post": "EIS analysis, post-cycling at 0% SOC",
    "summary": "Per-sample analysis summary",
    "metadata": "BattINFO JSON-LD metadata",
}

EXTRA_INPUT_DESCRIPTIONS: dict[str, str] = {
    "protocol": "EC-Lab measurement protocol (.mps)",
    "battinfo_xlsx": "BattINFO metadata input",
}


def _classify_inputs(sample_folder: Path) -> dict[str, list[Path]]:
    """Map measurement labels to their input MPR files for one sample folder."""
    inputs: dict[str, list[Path]] = {}

    gcpl_files = list(sample_folder.glob("*_GCPL_*.mpr"))
    if gcpl_files:
        gcpl_file = max(gcpl_files, key=lambda x: x.stat().st_size)
        inputs["gcpl"] = [gcpl_file]

    ocv_files = list(sample_folder.glob("*_OCV_*.mpr"))
    if ocv_files:
        inputs["ocv"] = [ocv_files[0]]

    lsv_files = list(sample_folder.glob("*_LSV_*.mpr"))
    if lsv_files:
        numbers = [
            int(m.group(1)) if (m := re.match(r"_([\d]+)_LSV_", f.stem)) else 0 for f in lsv_files
        ]
        lsv_files = [f for _, f in sorted(zip(numbers, lsv_files, strict=True))]
        if len(lsv_files) == 1:
            p = "pre" if numbers[0] < 8 else "post"  # noqa: PLR2004
            inputs[f"lsv_{p}"] = [lsv_files[0]]
        else:
            inputs["lsv_pre"] = [lsv_files[0]]
            inputs["lsv_post"] = [lsv_files[-1]]

    cv_pre = list(sample_folder.glob("*_CVApre*.mpr")) + list(sample_folder.glob("*_CVpre*.mpr"))
    if cv_pre:
        inputs["cv_pre"] = [cv_pre[0]]

    cv_post = list(sample_folder.glob("*_CVApost*.mpr")) + list(sample_folder.glob("*_CVpost*.mpr"))
    if cv_post:
        inputs["cv_post"] = [cv_post[0]]

    eis_files = list(sample_folder.glob("*_PEIS_*.mpr"))
    for tag, eis_file in zip(EIS_TAGS, eis_files, strict=False):
        inputs[tag] = [eis_file]

    return inputs


def _rel(path: Path, root: Path) -> str:
    """Return a forward-slash relative path string from root."""
    return path.relative_to(root).as_posix()


def write_rocrate(
    root_folder: Path,
    sample_folders: list[Path],
    tracked_by_sample: dict[Path, dict[str, list[Path]]],
    tracked_extras_by_sample: dict[Path, dict[str, list[Path]]] | None = None,
    fcids_by_sample: dict[Path, str | None] | None = None,
) -> None:
    """Write ro-crate-metadata.json at root_folder describing all inputs and outputs."""
    crate = ROCrate()
    crate.root_dataset["name"] = root_folder.name
    crate.root_dataset["description"] = (
        f"Flow battery electrochemical analysis produced by PyFlowBatt for {root_folder.name}"
    )
    crate.root_dataset["license"] = {"@id": "https://creativecommons.org/licenses/by/4.0/"}

    all_sample_datasets = []

    for sample_folder in sample_folders:
        sample_id = get_sampleid_from_folderpath(sample_folder)
        rel_sample = _rel(sample_folder, root_folder)

        fcid = fcids_by_sample.get(sample_folder) if fcids_by_sample else None
        sample_props: dict = {
            "name": sample_id,
            "description": f"Electrochemical cell measurements: {sample_folder.name}",
        }
        if fcid:
            sample_props["identifier"] = fcid
        sample_dataset = crate.add_dataset(dest_path=rel_sample + "/", properties=sample_props)
        all_sample_datasets.append(sample_dataset)
        sample_file_entities = []

        inputs = _classify_inputs(sample_folder)
        input_entities: dict[str, list] = {}
        for label, mpr_paths in inputs.items():
            label_entities = []
            for mpr_path in mpr_paths:
                if not mpr_path.exists():
                    continue
                mpr_entity = crate.add_file(
                    str(mpr_path),
                    dest_path=_rel(mpr_path, root_folder),
                    properties={
                        "name": mpr_path.stem,
                        "encodingFormat": ENCODING_FORMATS[".mpr"],
                        "measurementTechnique": MEASUREMENT_LABELS.get(label, label),
                    },
                )
                label_entities.append(mpr_entity)
                sample_file_entities.append(mpr_entity)
            if label_entities:
                input_entities[label] = label_entities

        all_input_entities = [e for ents in input_entities.values() for e in ents]

        extras = tracked_extras_by_sample.get(sample_folder, {}) if tracked_extras_by_sample else {}
        extra_entities: dict[str, list] = {}
        for label, paths in extras.items():
            desc = EXTRA_INPUT_DESCRIPTIONS.get(label, label)
            label_extra_ents = []
            for path in paths:
                if not path.exists():
                    continue
                ent = crate.add_file(
                    str(path),
                    dest_path=_rel(path, root_folder),
                    properties={
                        "name": path.name,
                        "encodingFormat": ENCODING_FORMATS.get(
                            path.suffix, "application/octet-stream"
                        ),
                        "description": desc,
                    },
                )
                label_extra_ents.append(ent)
                sample_file_entities.append(ent)
            if label_extra_ents:
                extra_entities[label] = label_extra_ents

        for label, paths in tracked_by_sample.get(sample_folder, {}).items():
            if label == "metadata":
                derived_from = extra_entities.get("battinfo_xlsx") or None
            else:
                derived_from = input_entities.get(label) or all_input_entities or None
            desc = OUTPUT_DESCRIPTIONS.get(label)
            for path in paths:
                if not path.exists():
                    continue
                ext = "".join(path.suffixes)
                fmt = ENCODING_FORMATS.get(
                    path.suffix, ENCODING_FORMATS.get(ext, "application/octet-stream")
                )
                props: dict = {
                    "name": path.name,
                    "encodingFormat": fmt,
                    "wasDerivedFrom": derived_from,
                }
                if desc:
                    props["description"] = desc
                output_ent = crate.add_file(
                    str(path), dest_path=_rel(path, root_folder), properties=props
                )
                sample_file_entities.append(output_ent)

        sample_dataset["hasPart"] = sample_file_entities  # type: ignore[index]

    combined_summary = root_folder / "combined_summary.xlsx"
    if combined_summary.exists():
        crate.add_file(
            str(combined_summary),
            dest_path="combined_summary.xlsx",
            properties={
                "name": "combined_summary.xlsx",
                "encodingFormat": ENCODING_FORMATS[".xlsx"],
                "wasDerivedFrom": all_sample_datasets or None,
                "description": "Combined per-sample summary across all cells",
            },
        )

    crate.write(str(root_folder))
