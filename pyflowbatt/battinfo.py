"""BattINFO ontology functions."""

import base64
import io
import json
import logging
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import pandas as pd

logger = logging.getLogger(__name__)


blank_flow_cell = {
    "@context": [
        "https://w3id.org/emmo/domain/battery/context",
        {
            "schema": "https://schema.org/",
            "emmo": "https://w3id.org/emmo#",
            "echem": "https://w3id.org/emmo/domain/electrochemistry#",
            "battery": "https://w3id.org/emmo/domain/battery#",
            "chemical": "https://w3id.org/emmo/domain/chemical-substance#",
            "unit": "https://qudt.org/vocab/unit/",
            "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
        },
    ],
    "@type": "RedoxFlowBattery",
}


def _deep_merge_dicts(target: dict, source: dict) -> dict:
    """Recursively merge source into target."""
    for k, v in source.items():
        if k in target and isinstance(target[k], dict) and isinstance(v, dict):
            _deep_merge_dicts(target[k], v)
        elif k in target and isinstance(target[k], list) and isinstance(v, list):
            target[k].extend(v)
        else:
            target[k] = v
    return target


def insert_dict_in_jsonld(
    obj: dict | list,
    keys: list[tuple],
    new_dict: dict,
    *,
    merge: bool = True,
) -> None:
    """Insert a dict into a nested jsonld.

    Args:
        obj: The JSON-LD object to put the thing in.
        keys: list of tuples with the key and optional exptected type, e.g.
            [
                ("hasPositiveElectrode", "Electrode"),
                ("hasCoating", "Coating"),
                ("hasActiveMaterial", None),
                ("hasMeasuredProperty", "MassLoading"),
            ]
        new_dict: The dict to insert
        merge (optional): Whether to merge or replace, default True (merge)

    """
    if not keys:
        msg = "Keys list cannot be empty"
        raise ValueError(msg)

    key, expected_type = keys[0]

    # At the end of the chain - could by empty, a dict, or a list
    if len(keys) == 1:
        if key not in obj or obj[key] is None:
            obj[key] = new_dict
        elif isinstance(obj[key], list):
            for i, o in enumerate(obj[key]):
                if isinstance(o, dict) and o.get("@type") == new_dict.get("@type"):
                    if merge:
                        _deep_merge_dicts(o, new_dict)
                    else:
                        obj[key][i] = new_dict
                    break
            else:
                obj[key].append(new_dict)
        elif isinstance(obj[key], dict):
            if obj[key].get("@type") == new_dict.get("@type"):
                if merge:
                    _deep_merge_dicts(obj[key], new_dict)
                else:
                    obj[key] = new_dict
            else:
                obj[key] = [obj[key], new_dict]
        else:
            msg = f"Unexpected type at end of path: {type(obj[key])}"
            raise TypeError(msg)
        return

    # Otherwise, recursively descend
    if key not in obj or obj[key] is None:
        obj[key] = {}

    val = obj[key]

    # If dict, descend directly
    if isinstance(val, dict):
        insert_dict_in_jsonld(val, keys[1:], new_dict, merge=merge)

    # If list, select the dict with the expected type
    elif isinstance(val, list):
        target = None
        if expected_type:
            for el in val:
                if isinstance(el, dict) and el.get("@type") == expected_type:
                    target = el
                    break
        if not target:
            # If no match found, create a new dict with that type
            target = {"@type": expected_type} if expected_type else {}
            val.append(target)
        insert_dict_in_jsonld(target, keys[1:], new_dict, merge=merge)

    else:
        msg = f"Unexpected type in path: {type(val)}"
        raise TypeError(msg)


def find_flow_cell(jsonld: dict) -> dict | None:
    """Search for the RedoxFlowBattery in a dict."""
    if "@type" in jsonld and jsonld["@type"] == "RedoxFlowBattery":
        return jsonld
    for value in jsonld.values():
        if isinstance(value, dict):
            result = find_flow_cell(value)
            if result is not None:
                return result
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    result = find_flow_cell(item)
                    if result is not None:
                        return result
    return None


def summarise_assembly(assembly: list[dict], sample_data: dict) -> str:
    """Summarise an assembly process."""
    mapping = {
        "Bottom": "CellCan",
        "Top": "CellLid",
        "Anode": "NegativeElectrode",
        "Separator": "Separator",
        "Cathode": "PositiveElectrode",
        "Spring": "Spring",
    }
    for step in assembly:
        if step["Step"] in mapping:
            step["Summary"] = mapping[step["Step"]]
        elif step["Step"] == "Spacer":
            if "bottom" in step["Description"].lower():
                thickness = sample_data.get("Bottom spacer thickness (mm)")
                step["Summary"] = f"{thickness} mm Spacer"
            elif "top" in step["Description"].lower():
                thickness = sample_data.get("Top spacer thickness (mm)")
                if not thickness:  # duct tape
                    thickness = sample_data.get("Bottom spacer thickness (mm)")
                step["Summary"] = f"{thickness} mm Spacer"
        elif step["Step"] == "Electrolyte":
            if "before" in step["Description"].lower():
                amount = sample_data.get("Electrolyte amount before separator (uL)")
                step["Summary"] = f"{amount} uL Electrolyte"
            elif "after" in step["Description"].lower():
                amount = sample_data.get("Electrolyte amount after separator (uL)")
                step["Summary"] = f"{amount} uL Electrolyte"
    summaries = [step.get("Summary") for step in assembly]
    return "Cell assembly sequence: " + ", ".join([s for s in summaries if s is not None])


def make_type_parent(data: dict, target_type: str) -> dict:
    """Promote object with target @type to the top level.

    Anything referencing this type is put in @reversed.
    """
    if isinstance(data, dict) and data.get("@type") == target_type:
        return data
    if isinstance(data, dict) and data.get("@reversed") is not None:
        msg = "Cannot rearrange object if @reversed in json-ld"
        raise ValueError(msg)

    data = deepcopy(data)
    ctx = data.pop("@context") if "@context" in data else None

    def find_target_and_parent(
        obj: dict | list | str | float | None = None,
        parent: dict | list | str | float | None = None,
        key: str | None = None,
        index: int | None = None,
    ) -> tuple:
        if isinstance(obj, dict):
            if obj.get("@type") == target_type:
                return obj, parent, key, index
            for k, v in obj.items():
                result = find_target_and_parent(v, obj, k, None)
                if result[0] is not None:
                    return result
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                result = find_target_and_parent(item, parent, key, i)
                if result[0] is not None:
                    return result
        return None, None, None, None

    target, parent, key, index = find_target_and_parent(data)
    if target is None:
        msg = f"No node with @type '{target_type}' found."
        raise ValueError(msg)

    # Remove the RedoxFlowBattery from its parent and replace with @id or remove
    placeholder = {"@id": "_:promoted"}
    assert isinstance(parent, (list, dict))  # noqa: S101  from intial check
    if isinstance(parent[key], list):
        # Replace only the matched item in the list
        parent[key][index] = placeholder
    else:
        parent[key] = placeholder

    # Build reversed object
    reversed_obj = deepcopy(parent)

    # Replace placeholder with original value in reversed object for display
    if isinstance(reversed_obj[key], list):
        # Filter out placeholder from list and keep other elements
        reversed_obj[key] = [item for item in reversed_obj[key] if item != placeholder]
        if len(reversed_obj[key]) == 1:
            reversed_obj[key] = reversed_obj[key][0]
    else:
        del reversed_obj[key]  # It was a direct object reference

    result = deepcopy(target)
    result["@reversed"] = {key: reversed_obj}
    if ctx:
        result["@context"] = ctx
    return result


def merge_contexts_strict(
    ctx1: str | list | dict,
    ctx2: str | list | dict,
    on_conflict: Literal["raise", "keep_left", "keep_right"] = "raise",
) -> list:
    """Merge the top level @context blocks of two JSON-LD."""
    ctx1_list = ctx1 if isinstance(ctx1, list) else [ctx1]
    ctx2_list = ctx2 if isinstance(ctx2, list) else [ctx2]

    def process_context_list(ctx_list: list) -> tuple[list, dict]:
        """Process a context list, split remotes and terms."""
        remotes = []
        terms: dict[str, str] = {}
        for ctx in ctx_list:
            if isinstance(ctx, str):
                remotes.append(ctx)
            elif isinstance(ctx, dict):
                terms.update(ctx.items())
        return remotes, terms

    remotes1, terms1 = process_context_list(ctx1_list)
    remotes2, terms2 = process_context_list(ctx2_list)

    # Merge remote contexts (keep unique)
    merged_remotes = list(dict.fromkeys(remotes1 + remotes2))  # preserves order, unique

    # Check for conflicts in namespace definitions
    for term, url in terms2.items():
        if term in terms1 and terms1[term] != url:
            if on_conflict == "raise":
                msg = f"Merging context: conflict for term '{term}': '{terms1[term]}' != '{url}'"
                raise ValueError(msg)
            logger.warning(
                "Merging context: conflict for term '%s': '%s' != '%s', keeping %s",
                term,
                terms1[term],
                url,
                "left" if on_conflict == "keep_left" else "right",
            )

    # Merge term definitions
    merged_terms = {**terms2, **terms1} if on_conflict == "keep_left" else {**terms1, **terms2}

    # Construct merged context list
    merged_context = [*merged_remotes]
    if merged_terms:
        merged_context.append(merged_terms)

    return merged_context


def recursive_merge(left: dict, right: dict, *, default_right: bool = True) -> dict:
    """Recursively merge dicts."""
    for k, rv in right.items():
        if k not in left:
            left[k] = rv
        else:
            lv = left[k]
            if isinstance(lv, dict) and isinstance(rv, dict):
                if lv.get("@id") and rv.get("@id") and lv["@id"] != rv["@id"]:
                    left[k] = dedupe_jsonld_list([lv, rv])
                else:
                    left[k] = recursive_merge(lv, rv, default_right=default_right)
            elif isinstance(lv, list) and isinstance(rv, list):
                left[k] = dedupe_jsonld_list([*lv, *rv])
            elif isinstance(lv, list):
                left[k] = dedupe_jsonld_list([*lv, rv])
            elif isinstance(rv, list):
                left[k] = dedupe_jsonld_list([lv, *rv])
            elif lv != rv:
                if default_right:
                    left[k] = rv
                    logger.warning(
                        "JSON-LD merge conflict at %s: left - %s, right - %s, defaulting to right",
                        k,
                        lv,
                        rv,
                    )
                else:
                    logger.warning(
                        "JSON-LD merge conflict at %s: left - %s, right - %s, defaulting to left",
                        k,
                        lv,
                        rv,
                    )
            else:
                left[k] = lv  # unchanged
    return left


def dedupe_jsonld_list(lst: list) -> list:
    """Remove duplicates from a list of JSON-LD values (dict-aware)."""
    seen = set()
    deduped = []
    for item in lst:
        # Convert dicts to a frozen string for comparison
        key = json.dumps(item, sort_keys=True) if isinstance(item, dict) else str(item)
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


def make_test_object(battinfo_jsonld: dict) -> dict:
    """Put BattINFO redox flow battery description inside a BatteryTest object."""
    if battinfo_jsonld.get("@type") == "RedoxFlowBattery":
        return {
            "@context": battinfo_jsonld.pop("@context"),
            "@type": "BatteryTest",
            "hasTestObject": battinfo_jsonld,
        }
    if battinfo_jsonld.get("@type") == "BatteryTest":
        return battinfo_jsonld
    msg = "BattINFO JSON-LD must have RedoxFlowBattery or BatteryTest as top level @type"
    raise ValueError(msg)


def add_input_and_output() -> dict:
    """Create output and input datasets with CC by 4.0 licences."""
    return {
        "@type": "BatteryTest",
        "hasOutput": {
            "@type": ["BatteryTestResult", "dcat:Dataset"],
            "dcat:keyword": ["battery", "redox flow battery"],
            "dcterms:license": {"@id": "https://creativecommons.org/licenses/by/4.0/"},
            "dcterms:issued": datetime.now().date().isoformat(),  # noqa: DTZ005
            "schema:datePublished": datetime.now().date().isoformat(),  # noqa: DTZ005
        },
        "hasInput": {
            "@type": "dcat:Dataset",
            "dcterms:license": {"@id": "https://creativecommons.org/licenses/by/4.0/"},
            "dcterms:issued": datetime.now().date().isoformat(),  # noqa: DTZ005
            "schema:datePublished": datetime.now().date().isoformat(),  # noqa: DTZ005
        },
    }


def merge_jsonld(json1: dict, json2: dict) -> dict:
    """Merge two JSON-LD structures assuming they reference the SAME NODE."""
    if not isinstance(json1, dict) or not isinstance(json2, dict):
        raise TypeError
    if "@type" not in json1 or json1.get("@type") != json2.get("@type"):
        msg = "Two JSON-LDs do not have the same parent node"
        raise ValueError(msg)
    return recursive_merge(json1, json2)


def merge_jsonld_on_type(jsons: list[dict], target_type: str = "BatteryTest") -> dict:
    """Transform list of json-ld dicts, make target_type parent and merge."""
    jsons = deepcopy(jsons)  # Don't modify originals
    contexts = [j.pop("@context") for j in jsons if "@context" in j]
    jsons = [make_type_parent(j, target_type) for j in jsons]

    def repeated_context_merge(contexts: list[str | list | dict]) -> None | str | list | dict:
        if len(contexts) == 0:
            return None
        if len(contexts) == 1:
            return contexts[0]
        merged = merge_contexts_strict(contexts[0], contexts[1], on_conflict="keep_right")
        if len(contexts) == 2:
            return merged
        return repeated_context_merge([merged, *contexts[2:]])

    def repeated_merge(jsonld_list: list[dict]) -> dict:
        if len(jsonld_list) == 1:
            return jsonld_list[0]
        return recursive_merge(jsonld_list[0], repeated_merge(jsonld_list[1:]))

    merged_context = repeated_context_merge(contexts)
    merged_content = repeated_merge(jsons)

    return {
        **({"@context": merged_context} if merged_context else {}),
        **merged_content,
    }


def generate_battery_test(ontologized_protocols: dict | list[dict]) -> dict:
    """Generate test json-ld based on protocols."""
    if isinstance(ontologized_protocols, dict):
        ontologized_protocols = [ontologized_protocols]

    def recursive_protocol(ontologized_protocols: list[dict]) -> dict:
        test = {
            "@type": ["ConstantCurrentConstantVoltageCycling", "IterativeWorkflow"],
            "rdfs:label": "GeneratedBatteryTestProcedure",
            "hasTask": ontologized_protocols[0],
        }
        if len(ontologized_protocols) > 1:
            test["hasNext"] = recursive_protocol(ontologized_protocols[1:])
        return test

    return {
        "@context": [
            "https://w3id.org/emmo/domain/battery/context",
            {
                "schema": "https://schema.org/",
                "emmo": "https://w3id.org/emmo#",
                "echem": "https://w3id.org/emmo/domain/electrochemistry#",
                "battery": "https://w3id.org/emmo/domain/battery#",
                "chemical": "https://w3id.org/emmo/domain/chemical-substance#",
                "unit": "https://qudt.org/vocab/unit/",
                "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
            },
        ],
        "@type": "BatteryTest",
        "hasLaboratory": {
            "@type": "Laboratory",
            "@id": "https://www.wikidata.org/wiki/Q683116",
            "rdfs:label": "Empa",
        },
        "hasMeasurementParameter": recursive_protocol(ontologized_protocols),
    }


def add_fcid_output(
    fcid: str,
) -> dict:
    """Add FCID to output section of json-ld output."""
    return {
        "@type": "BatteryTest",
        "hasOutput": {
            "dcterms:title": f"Cycling data redox flow battery {fcid}",
            "dcterms:description": f"Cycling data redox flow battery {fcid}",
        },
    }


def zenodo_record_id(zenodo_doi_url: str) -> str:
    """Extract the numeric Zenodo record ID from a Zenodo DOI URL.

    e.g. "https://doi.org/10.5281/zenodo.20338409" -> "20338409"
    """
    return zenodo_doi_url.rsplit(".", maxsplit=1)[-1]


def zenodo_file_id(
    root_rel_path: str,
    zenodo_doi_url: str | None,
    *,
    package: Literal["zip", "files"] = "files",
    zip_filename: str | None = None,
) -> str:
    """Build an @id for a file once it is uploaded to Zenodo.

    Args:
        root_rel_path: path to the file relative to the root folder that gets
            uploaded to Zenodo, e.g. sample_01/results/eis_post.bdf.parquet
        zenodo_doi_url: Zenodo archive DOI URL, e.g. https://doi.org/10.5281/zenodo.12345678
        package: "zip" if the whole root folder is uploaded as a single zip (Zenodo
            has no URL that deep-links into a file inside that zip, so the fragment
            after "#" is only useful once the zip has been downloaded and extracted),
            or "files" if every file is uploaded individually, preserving the folder
            structure in the file's Zenodo "key" (in which case a direct download URL
            can be built for it).
        zip_filename: name of the zip file as uploaded to Zenodo (required when
            package="zip").

    Returns:
        A Zenodo URL identifying the file, or the bare root_rel_path if no
        zenodo_doi_url is given yet.

    """
    if not zenodo_doi_url:
        return root_rel_path

    record_id = zenodo_record_id(zenodo_doi_url)
    base = f"https://zenodo.org/records/{record_id}/files"
    if package == "files":
        return f"{base}/{quote(root_rel_path, safe='/')}"

    if not zip_filename:
        msg = "zip_filename is required when package='zip'"
        raise ValueError(msg)
    return f"{base}/{quote(zip_filename, safe='')}#{root_rel_path}"


def add_input_data(
    rel_file_path: str,
    zenodo_doi_url: str | None,
    comment: str | None = None,
    *,
    package: Literal["zip", "files"] = "files",
    zip_filename: str | None = None,
) -> dict:
    """Add links to raw input files to the hasInput section of json-ld output.

    Args:
        rel_file_path: path to file relative to the root folder uploaded to Zenodo,
            e.g. sample_01/cell01_GCPL_01.mpr
        zenodo_doi_url: optional Zenodo archive URL
        comment: human-readable description of what this file is
        package: see :func:`zenodo_file_id`
        zip_filename: see :func:`zenodo_file_id`

    Returns:
        dict with "BatteryTest" as top level type.

    """
    ext = Path(rel_file_path).suffix.lower()
    if ext in {".mpr", ".mps"}:
        media_type = "application/octet-stream"
    elif ext in {".xlsx", ".xls"}:
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    elif ext == ".json":
        media_type = "application/json"
    else:
        media_type = "application/octet-stream"

    dist: dict = {
        "@id": zenodo_file_id(
            rel_file_path, zenodo_doi_url, package=package, zip_filename=zip_filename
        ),
        "@type": "dcat:Distribution",
        "dcat:mediaType": media_type,
    }
    if comment:
        dist["rdfs:comment"] = comment

    return {
        "@type": "BatteryTest",
        "hasInput": {
            "@type": ["RawData", "dcat:Dataset"],
            "dcat:distribution": dist,
        },
    }


def add_data(
    rel_file_path: str,
    zenodo_doi_url: str | None,
    extras: dict | None = None,
    *,
    package: Literal["zip", "files"] = "files",
    zip_filename: str | None = None,
) -> dict:
    """Add links to data files to output section of json-ld output.

    Args:
        rel_file_path: path to file relative to the root folder uploaded to Zenodo,
            e.g. empa__fcid01345/empa__fcid01345.bdf.parquet
        zenodo_doi_url: path to zenodo archive e.g. https://doi.org/10.1234/zenodo.12345678
        extras: dict with any extra terms to include in metadata
        package: see :func:`zenodo_file_id`
        zip_filename: see :func:`zenodo_file_id`

    Returns:
        dict with "BatteryTest" as top level type.

    """
    extras = extras or {}
    if rel_file_path.endswith(".parquet"):
        additions: dict[str, str | list | dict] = {
            "dcat:mediaType": "application/vnd.apache.parquet",
            "csvw:tableSchema": "https://w3id.org/battery-data-alliance/ontology/battery-data-format/schema",
            "rdfs:comment": "Time series electrochemical data using Battery Data Format (bdf) columns",
        }
        if "eis." in rel_file_path:
            additions["rdfs:comment"] = (
                "Frequency-domain electrochemical data using Battery Data Format (bdf) columns"
            )
    elif rel_file_path.endswith(".csv"):
        additions = {
            "dcat:mediaType": "text/csv",
            "csvw:tableSchema": "https://w3id.org/battery-data-alliance/ontology/battery-data-format/schema",
            "csvw:dialect": {"@type": "csvw:Dialect", "csvw:delimiter": ",", "csvw:skipRows": 0},
            "rdfs:comment": "Time series electrochemical data using Battery Data Format (bdf) columns",
        }
        if "eis" in rel_file_path:
            additions["rdfs:comment"] = (
                "Frequency-domain electrochemical data using Battery Data Format (bdf) columns"
            )
    elif rel_file_path.endswith(".json"):
        additions = {
            "dcat:mediaType": "application/json",
        }
    elif rel_file_path.endswith(".png"):
        additions = {
            "dcat:mediaType": "image/png",
        }
    elif rel_file_path.endswith(".xlsx"):
        additions = {
            "dcat:mediaType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "rdfs:comment": "Excel sheets with summary information from sample analysis",
        }
    elif rel_file_path.endswith(".mpr"):
        additions = {
            "@type": ["dcat:Distribution", "RawData"],
            "rdfs:comment": "Raw electrochemical data in proprietary Biologic .mpr binary format",
        }
    elif rel_file_path.endswith(".mps"):
        additions = {"rdfs:comment": "Cycling protocol in text-based Biologic .mps format"}
    else:
        msg = f"Unknown file type: {rel_file_path}"
        raise ValueError(msg)

    return {
        "@type": "BatteryTest",
        "hasOutput": {
            "dcat:distribution": {
                "@id": zenodo_file_id(
                    rel_file_path, zenodo_doi_url, package=package, zip_filename=zip_filename
                ),
                "@type": "dcat:Distribution",
                **additions,
                **extras,
            }
        },
    }


def add_zenodo_url(
    zenodo_doi_url: str,
) -> dict:
    """Add Zenodo URL to output section of json-ld output."""
    return {
        "@type": "BatteryTest",
        "hasOutput": {
            "dcat:accessURL": zenodo_doi_url,
            "dcat:endpointURL": f"https://zenodo.org/api/records/{zenodo_record_id(zenodo_doi_url)}",
        },
    }


def add_associated_media(
    paper_doi_url: str | None,
    sample_to_fig: dict,
    fcid: str | None,
    sample_id: str | None,
) -> dict:
    """Add associated media to output section of json-ld output."""
    figs = [
        v for k, v in sample_to_fig.items() if k in (sample_id, fcid)
    ]  # e.g. ["Fig. 3a", "Fig. 3b"]
    if not figs:
        return {"@type": "BatteryTest"}
    associated_media: dict[str, str | list | dict] = {"@id": paper_doi_url} if paper_doi_url else {}
    associated_media["rdfs:label"] = figs[0] if len(figs) == 1 else figs
    associated_media["rdfs:comment"] = (
        "Subfigure of associated scientific publication containing this data"
    )

    return {
        "@type": "BatteryTest",
        "hasOutput": {
            "schema:associatedMedia": associated_media,
        },
    }


def add_citation(
    citation_string: str,
) -> dict:
    """Add publishing details to output section of json-ld output."""
    return {
        "@type": "BatteryTest",
        "hasOutput": {
            "schema:citation": citation_string,
        },
    }


def add_authors(
    authors: list[dict],
    institutions: dict[str, dict],
) -> dict:
    """Add authors to output section of json-ld.

    Args:
        authors: a list of dictionaries, each describing one author
            the dict must include the keys:
                "orcid": str e.g. https://orcid.org/1234-1234-1234-1234
                "name": str e.g. "John Battery"
                "affiliation": list[str] e.g. ["Empa", "ETH Zurich", "EPFL"]
        institutions: a dict of dictionaries, each describing one institution
            e.g.
                {
                    "Empa": {"wikidata_url": "https://www.wikidata.org/wiki/Q683116"}
                    "ETH Zurich": {"wikidata_url": "https://www.wikidata.org/wiki/Q11942"}
                    "EPFL": {"wikidata_url": "https://www.wikidata.org/wiki/Q262760"}
                }

    Returns:
        dict with "BatteryTest" as top level type.

    """

    def make_author(author: dict) -> dict:
        auth_dict: dict[str, str | list | dict] = {"@type": "schema:Person"}
        if author.get("orcid"):
            auth_dict["@id"] = author["orcid"]
        auth_dict["schema:name"] = author["name"]
        if len(author["affiliation"]) == 1:
            auth_dict["schema:affiliation"] = {
                "@type": "schema:ResearchOrganization",
                "@id": institutions[author["affiliation"][0]]["wikidata_url"],
                "schema:name": author["affiliation"][0],
            }
        elif len(author["affiliation"]) > 1:
            auth_dict["schema:affiliation"] = [
                {
                    "@type": "schema:ResearchOrganization",
                    "@id": institutions[affil]["wikidata_url"],
                    "schema:name": affil,
                }
                for affil in author["affiliation"]
            ]
        return auth_dict

    authors_jsonld = [make_author(author) for author in authors]
    return {
        "@type": "BatteryTest",
        "hasOutput": {
            "dcterms:creator": authors_jsonld[0] if len(authors_jsonld) == 1 else authors_jsonld,
        },
    }


def add_institution(
    name: str = "Empa",
    wikidata_url: str | None = None,
) -> dict:
    """Add publishing details to output section of json-ld."""
    inst_dict = {
        "@type": "BatteryTest",
        "hasOutput": {
            "dc:publisher": {
                "@type": "schema:ResearchOrganization",
            },
        },
    }
    if wikidata_url:
        inst_dict["hasOutput"]["dc:publisher"]["@id"] = wikidata_url
    inst_dict["hasOutput"]["dc:publisher"]["schema:name"] = name
    return inst_dict


def parse_zenodo_info_xlsx(
    xlsx_file: str | Path,
) -> dict:
    """Parse zenodo info from uploaded xlsx content string.

    Args:
        xlsx_file: Path to a file, a content string from upload button

    Returns:
        dict with zenodo info. The "General" sheet's rows are lowercased and
        underscored into keys, e.g. a "Zenodo Doi Url" row becomes "zenodo_doi_url".
        An optional "Zenodo Zip Filename" row overrides the default zip filename
        (``<root folder name>.zip``) used by :func:`zenodo_file_id` when the CLI's
        ``--zip`` flag is used; whether "zip" or "files" packaging applies is decided
        by that flag, not by anything in this sheet.

    """
    if isinstance(xlsx_file, Path):
        excel_file = pd.ExcelFile(xlsx_file)
    else:
        _content_type, content_string = xlsx_file.split(",")
        decoded = base64.b64decode(content_string)
        excel_file = pd.ExcelFile(io.BytesIO(decoded))
    sheet_names = [str(s) for s in excel_file.sheet_names]
    expected_sheets = ["General", "Figures", "Authors", "Institutions"]
    if not all(sheet in expected_sheets for sheet in sheet_names):
        msg = "Uploaded xlsx does not have expected sheets for Zenodo info."
        raise ValueError(msg)

    def nan_to_none(x):
        """Convert pd nan to None."""
        if isinstance(x, (list, tuple, dict)):
            return x
        return None if pd.isna(x) else x

    def strip_if_str(x: object) -> object:
        if isinstance(x, str):
            return x.strip()
        return x

    # Read General sheet
    general_df = pd.read_excel(excel_file, sheet_name="General", engine="openpyxl", header=None)
    keys = general_df[0].to_numpy()
    keys = [k.strip().lower().replace(" ", "_") for k in keys]
    values = [strip_if_str(nan_to_none(v)) for v in general_df[1].to_numpy()]
    general_dict = dict(zip(keys, values, strict=True))

    # Read Figures sheet
    figures_df = pd.read_excel(excel_file, sheet_name="Figures", engine="openpyxl")
    figures_dict = {}
    for _, row in figures_df.iterrows():
        key = strip_if_str(row["Barcode"] if pd.notna(row["Barcode"]) else row["Sample ID"])
        figures = row.drop(labels=["Sample ID", "Barcode"]).dropna().astype(str).tolist()
        figures = [f.strip() for f in figures]
        figures_dict[key] = figures

    # Read Institutions sheet
    institutions_df = pd.read_excel(excel_file, sheet_name="Institutions", engine="openpyxl")
    institutions_df.columns = [
        col.strip().lower().replace(" ", "_") for col in institutions_df.columns
    ]
    institutions_dict = institutions_df.set_index("name").to_dict(orient="index")
    institutions_dict = {
        strip_if_str(name): {k: strip_if_str(nan_to_none(v)) for k, v in attrs.items()}
        for name, attrs in institutions_dict.items()
    }

    # Read Authors sheet
    authors_df = pd.read_excel(excel_file, sheet_name="Authors", engine="openpyxl")
    authors_list = []
    for _, row in authors_df.iterrows():
        affil_list = row.drop(labels=["ORCID", "Name"]).dropna().astype(str).tolist()
        affil_list = [f.strip() for f in affil_list]
        authors_list.append(
            {
                "name": strip_if_str(nan_to_none(row["Name"])),
                "orcid": strip_if_str(nan_to_none(row["ORCID"])),
                "affiliation": affil_list,
            }
        )

    return {
        **general_dict,
        "sample_to_fig": figures_dict,
        "institutions": institutions_dict,
        "authors": authors_list,
    }
