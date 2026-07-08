"""Write RO-Crate metadata for a batch PyFlowBatt analysis."""

from __future__ import annotations

from pathlib import Path

from rocrate.rocrate import ROCrate

from pyflowbatt.analysis import _rel, get_sampleid_from_folderpath
from pyflowbatt.config import PyFlowBattConfig, classify_technique_files

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


def _classify_inputs(
    sample_folder: Path, config: PyFlowBattConfig | None = None
) -> dict[str, list[Path]]:
    """Map measurement labels to their input files for one sample folder.

    Delegates to the same classifier `analyse_sample` uses, so RO-Crate output
    honors the same `pyflowbatt.toml` configuration. warn=False (default) avoids
    re-logging the ambiguity warnings `analyse_sample` already emitted earlier
    in the same run.
    """
    config = config or PyFlowBattConfig.load(sample_folder)
    return classify_technique_files(sample_folder, config)


def write_rocrate(
    root_folder: Path,
    sample_folders: list[Path],
    tracked_by_sample_folder: dict[Path, dict[str, list[Path]]],
    tracked_extras_by_sample_folder: dict[Path, dict[str, list[Path]]] | None = None,
    fcids_by_sample_folder: dict[Path, str | None] | None = None,
    configs_by_sample_folder: dict[Path, PyFlowBattConfig] | None = None,
    sample_ids_by_sample_folder: dict[Path, str] | None = None,
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
        config = (configs_by_sample_folder or {}).get(sample_folder)
        sample_id = (sample_ids_by_sample_folder or {}).get(
            sample_folder
        ) or get_sampleid_from_folderpath(sample_folder, config)
        rel_sample = _rel(sample_folder, root_folder)

        fcid = fcids_by_sample_folder.get(sample_folder) if fcids_by_sample_folder else None
        sample_props: dict = {
            "name": sample_id,
            "description": f"Electrochemical cell measurements: {sample_folder.name}",
        }
        if fcid:
            sample_props["identifier"] = fcid
        sample_dataset = crate.add_dataset(dest_path=rel_sample + "/", properties=sample_props)
        all_sample_datasets.append(sample_dataset)
        sample_file_entities = []

        inputs = _classify_inputs(sample_folder, config)
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

        extras = (
            tracked_extras_by_sample_folder.get(sample_folder, {})
            if tracked_extras_by_sample_folder
            else {}
        )
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

        for label, paths in tracked_by_sample_folder.get(sample_folder, {}).items():
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
