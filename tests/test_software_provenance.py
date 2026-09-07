"""Tests that the PyFlowBatt version is recorded in both metadata documents."""

import json
from pathlib import Path

from pyflowbatt import battinfo
from pyflowbatt.rocrate_output import LICENSE_URL, write_rocrate
from pyflowbatt.version import __url__, __version__


def _graph_by_id(root_folder: Path) -> dict[str, dict]:
    metadata = json.loads((root_folder / "ro-crate-metadata.json").read_text(encoding="utf-8"))
    return {entity["@id"]: entity for entity in metadata["@graph"]}


def test_rocrate_records_software_version(tmp_path: Path) -> None:
    """The crate carries a SoftwareApplication entity holding the current version."""
    write_rocrate(tmp_path, [], {})
    entities = _graph_by_id(tmp_path)

    software = entities[__url__]
    assert software["@type"] == "SoftwareApplication"
    assert software["version"] == __version__


def test_rocrate_create_action_links_software_to_root(tmp_path: Path) -> None:
    """A CreateAction ties the software to the root dataset as its instrument."""
    write_rocrate(tmp_path, [], {})
    entities = _graph_by_id(tmp_path)

    action = entities["#pyflowbatt-run"]
    assert action["@type"] == "CreateAction"
    assert action["instrument"] == {"@id": __url__}
    assert action["object"] == {"@id": "./"}


def test_rocrate_describes_its_license(tmp_path: Path) -> None:
    """The licence is a CreativeWork contextual entity, not just a bare URL reference."""
    write_rocrate(tmp_path, [], {})
    entities = _graph_by_id(tmp_path)

    assert entities["./"]["license"] == {"@id": LICENSE_URL}
    license_entity = entities[LICENSE_URL]
    assert license_entity["@type"] == "CreativeWork"
    assert license_entity["name"]
    assert license_entity["description"]


def test_add_software_merges_into_battery_test_output() -> None:
    """add_software lands under hasOutput without displacing the other snippets."""
    merged = battinfo.merge_jsonld_on_type(
        [
            battinfo.generate_battery_test({"@type": "DummyProtocol"}),
            battinfo.add_input_and_output(),
            battinfo.add_software(),
        ]
    )
    output = merged["hasOutput"]
    instrument = output["prov:wasGeneratedBy"]["schema:instrument"]

    assert instrument["@id"] == __url__
    assert instrument["schema:softwareVersion"] == __version__
    # the software must not displace what the other snippets put on the same node
    assert output["dcterms:license"] == {"@id": "https://creativecommons.org/licenses/by/4.0/"}
    assert "BatteryTestResult" in output["@type"]
