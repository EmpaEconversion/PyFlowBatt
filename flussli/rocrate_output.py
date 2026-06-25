"""Write RO-Crate metadata for a batch flussli analysis."""

from __future__ import annotations

import re
from pathlib import Path

from rocrate.rocrate import ROCrate

from flussli.analysis import get_sampleid_from_folderpath

MEASUREMENT_LABELS: dict[str, str] = {
    "gcpl": "Galvanostatic Cycling with Potential Limitation",
    "ocv": "Open Circuit Voltage",
    "lsv_pre": "Linear Sweep Voltammetry (pre-cycling)",
    "lsv_post": "Linear Sweep Voltammetry (post-cycling)",
    "cv_pre": "Cyclic Voltammetry (pre-cycling)",
    "cv_post": "Cyclic Voltammetry (post-cycling)",
    "eis_pre": "Electrochemical Impedance Spectroscopy (pre-cycling)",
    "eis_pre-50%SOC": "Electrochemical Impedance Spectroscopy (pre-cycling, 50% SOC)",
    "eis_post-50%SOC": "Electrochemical Impedance Spectroscopy (post-cycling, 50% SOC)",
    "eis_post": "Electrochemical Impedance Spectroscopy (post-cycling)",
}

ENCODING_FORMATS: dict[str, str] = {
    ".parquet": "application/x-parquet",
    ".csv": "text/csv",
    ".png": "image/png",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".mpr": "application/octet-stream",
}

EIS_TAGS = ["eis_pre", "eis_pre-50%SOC", "eis_post-50%SOC", "eis_post"]


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
    save_format: str,
) -> None:
    """Write ro-crate-metadata.json at root_folder describing all inputs and outputs."""
    crate = ROCrate()
    crate.root_dataset["name"] = root_folder.name
    crate.root_dataset["description"] = (
        f"Flow battery electrochemical analysis produced by flussli for {root_folder.name}"
    )

    all_sample_datasets = []

    for sample_folder in sample_folders:
        sample_id = get_sampleid_from_folderpath(sample_folder)
        rel_sample = _rel(sample_folder, root_folder)

        sample_dataset = crate.add_dataset(
            rel_sample + "/",
            properties={
                "name": sample_id,
                "description": f"Electrochemical cell measurements: {sample_folder.name}",
            },
        )
        all_sample_datasets.append(sample_dataset)

        inputs = _classify_inputs(sample_folder)
        input_entities: dict[str, list] = {}

        for label, mpr_paths in inputs.items():
            label_entities = []
            for mpr_path in mpr_paths:
                if not mpr_path.exists():
                    continue
                rel_mpr = _rel(mpr_path, root_folder)
                mpr_entity = crate.add_file(
                    rel_mpr,
                    properties={
                        "name": mpr_path.stem,
                        "encodingFormat": ENCODING_FORMATS[".mpr"],
                        "measurementType": MEASUREMENT_LABELS.get(label, label),
                    },
                )
                label_entities.append(mpr_entity)
            if label_entities:
                input_entities[label] = label_entities

        all_input_entities = [e for entities in input_entities.values() for e in entities]

        results_dir = sample_folder / "results"
        if not results_dir.exists():
            continue

        for label, mpr_entities in input_entities.items():
            data_ext = ".parquet" if save_format == "parquet" else ".csv"
            data_path = results_dir / f"{label}.x.bdf{data_ext}"
            if data_path.exists():
                crate.add_file(
                    _rel(data_path, root_folder),
                    properties={
                        "name": data_path.name,
                        "encodingFormat": ENCODING_FORMATS[data_ext],
                        "derivedFrom": mpr_entities,
                    },
                )

            plot_path = results_dir / f"{label}.png"
            if plot_path.exists():
                crate.add_file(
                    _rel(plot_path, root_folder),
                    properties={
                        "name": plot_path.name,
                        "encodingFormat": ENCODING_FORMATS[".png"],
                        "derivedFrom": mpr_entities,
                    },
                )

        summary_path = results_dir / "summary.xlsx"
        if summary_path.exists():
            crate.add_file(
                _rel(summary_path, root_folder),
                properties={
                    "name": "summary.xlsx",
                    "encodingFormat": ENCODING_FORMATS[".xlsx"],
                    "derivedFrom": all_input_entities if all_input_entities else None,
                },
            )

    combined_summary = root_folder / "combined_results" / "combined_summary.xlsx"
    if combined_summary.exists():
        crate.add_file(
            _rel(combined_summary, root_folder),
            properties={
                "name": "combined_summary.xlsx",
                "encodingFormat": ENCODING_FORMATS[".xlsx"],
                "derivedFrom": all_sample_datasets if all_sample_datasets else None,
            },
        )

    crate.write(str(root_folder))
