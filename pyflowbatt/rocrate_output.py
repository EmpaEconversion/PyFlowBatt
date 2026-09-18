"""Write RO-Crate metadata for a batch PyFlowBatt analysis."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from rocrate.model import ContextEntity, SoftwareApplication
from rocrate.rocrate import ROCrate

from pyflowbatt.analysis import (
    EXTRA_INPUT_DESCRIPTIONS,
    OTHER_RAW_LABEL,
    OUTPUT_MISC_DESCRIPTIONS,
    _generic_eis_parts,
    _rel,
    data_description,
    get_sampleid_from_folderpath,
    plot_description,
    raw_description,
)
from pyflowbatt.config import PyFlowBattConfig, classify_technique_files
from pyflowbatt.version import __title__, __url__, __version__

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


def _measurement_technique(label: str) -> str:
    """Technique name for a raw input label, including numbered EIS labels."""
    if label in MEASUREMENT_LABELS:
        return MEASUREMENT_LABELS[label]
    if parts := _generic_eis_parts(label):
        when, number = parts
        return f"Electrochemical Impedance Spectroscopy ({when} cycling, measurement {number})"
    return label


LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"

ENCODING_FORMATS: dict[str, str] = {
    ".parquet": "application/x-parquet",
    ".csv": "text/csv",
    ".png": "image/png",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".mpr": "application/octet-stream",
}


def _output_description(label: str, path: Path) -> str | None:
    """Pick the description for a tracked output, by label and file type.

    Shared with the descriptions `analyse_sample` writes into BattINFO JSON-LD,
    so a plot and its companion data file get distinct, consistent wording.
    """
    if path.suffix == ".png":
        return plot_description(label)
    if path.suffix in (".parquet", ".csv"):
        return data_description(label)
    return OUTPUT_MISC_DESCRIPTIONS.get(label)


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


def _add_license(crate: ROCrate) -> None:
    """Describe the crate licence as a contextual entity, not just a bare URL."""
    license_entity = crate.add(
        ContextEntity(
            crate,
            identifier=LICENSE_URL,
            properties={
                "@type": "CreativeWork",
                "name": "CC BY 4.0",
                "description": "Creative Commons Attribution 4.0 International License",
            },
        )
    )
    crate.root_dataset["license"] = license_entity


def _add_software_provenance(crate: ROCrate) -> None:
    """Record which PyFlowBatt release built the crate."""
    software = crate.add(
        SoftwareApplication(
            crate,
            identifier=__url__,
            properties={
                "name": __title__,
                "version": __version__,
                "url": {"@id": __url__},
            },
        )
    )
    crate.add_action(
        software,
        identifier="#pyflowbatt-run",
        object=crate.root_dataset,
        properties={
            "name": "RO-Crate created",
            "endTime": datetime.now().astimezone().isoformat(timespec="seconds"),
        },
    )


def write_rocrate(
    root_folder: Path,
    sample_folders: list[Path],
    tracked_by_sample_folder: dict[Path, dict[str, list[Path]]],
    tracked_extras_by_sample_folder: dict[Path, dict[str, list[Path]]] | None = None,
    fcids_by_sample_folder: dict[Path, str | None] | None = None,
    configs_by_sample_folder: dict[Path, PyFlowBattConfig] | None = None,
    sample_ids_by_sample_folder: dict[Path, str] | None = None,
    raw_inputs_by_sample_folder: dict[Path, dict[str, list[Path]]] | None = None,
) -> None:
    """Write ro-crate-metadata.json at root_folder describing all inputs and outputs.

    `raw_inputs_by_sample_folder` is what `analyse_sample` found, so the crate and the
    BattINFO JSON-LD describe the same files. Without it the folders are re-classified,
    which finds the same technique files but no unmatched raw data.
    """
    crate = ROCrate()
    crate.root_dataset["name"] = root_folder.name
    crate.root_dataset["description"] = (
        f"Flow battery electrochemical analysis produced by PyFlowBatt for {root_folder.name}"
    )
    _add_license(crate)
    _add_software_provenance(crate)

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

        inputs = (raw_inputs_by_sample_folder or {}).get(sample_folder) or _classify_inputs(
            sample_folder, config
        )
        input_entities: dict[str, list] = {}
        for label, raw_paths in inputs.items():
            label_entities = []
            for raw_path in raw_paths:
                if not raw_path.exists():
                    continue
                raw_props: dict = {
                    "name": raw_path.stem,
                    "encodingFormat": ENCODING_FORMATS.get(
                        raw_path.suffix, ENCODING_FORMATS[".mpr"]
                    ),
                }
                if label == OTHER_RAW_LABEL:
                    raw_props["description"] = raw_description(label, raw_path)
                else:
                    raw_props["measurementTechnique"] = _measurement_technique(label)
                raw_entity = crate.add_file(
                    str(raw_path), dest_path=_rel(raw_path, root_folder), properties=raw_props
                )
                label_entities.append(raw_entity)
                sample_file_entities.append(raw_entity)
            if label_entities:
                input_entities[label] = label_entities

        # Unmatched raw files weren't analysed, so nothing derives from them.
        all_input_entities = [
            e for label, ents in input_entities.items() if label != OTHER_RAW_LABEL for e in ents
        ]

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
                # shared inputs living outside the sample folder (e.g. the root
                # publication info xlsx) stay out of the sample's hasPart
                if path.is_relative_to(sample_folder):
                    sample_file_entities.append(ent)
            if label_extra_ents:
                extra_entities[label] = label_extra_ents

        for label, paths in tracked_by_sample_folder.get(sample_folder, {}).items():
            if label == "metadata":
                derived_from = [
                    ent
                    for extra_label in ("battinfo_xlsx", "pub_info")
                    for ent in extra_entities.get(extra_label, [])
                ] or None
            else:
                derived_from = input_entities.get(label) or all_input_entities or None
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
                desc = _output_description(label, path)
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
