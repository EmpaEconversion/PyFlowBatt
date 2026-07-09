"""Tests for the PyFlowBatt CLI, in particular `pyflowbatt init`."""

from pathlib import Path

import pytest

from pyflowbatt import cli


def test_init_config_writes_template(tmp_path: Path) -> None:
    """`pyflowbatt init --folder X` writes a template pyflowbatt.toml into X."""
    from pyflowbatt.config import TEMPLATE_TOML

    cli.init_config(folder=str(tmp_path))

    written = tmp_path / "pyflowbatt.toml"
    assert written.exists()
    assert written.read_text(encoding="utf-8") == TEMPLATE_TOML


def test_init_config_defaults_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no --folder given, init_config writes into the current working directory."""
    monkeypatch.chdir(tmp_path)
    cli.init_config()
    assert (tmp_path / "pyflowbatt.toml").exists()


def test_init_config_refuses_to_overwrite(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A second `init` call errors and leaves the existing pyflowbatt.toml untouched."""
    existing = tmp_path / "pyflowbatt.toml"
    existing.write_text("area_cm2 = 42\n")

    with caplog.at_level("ERROR", logger="pyflowbatt"):
        cli.init_config(folder=str(tmp_path))

    assert existing.read_text() == "area_cm2 = 42\n"
    assert "already exists" in caplog.text


def test_main_dispatches_init_subcommand(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`pyflowbatt init --folder X` on the CLI dispatches to init_config, not analyse."""
    called_with: dict = {}

    def fake_init_config(folder: str | None = None) -> None:
        called_with["folder"] = folder

    def fake_analyse(*_args: object, **_kwargs: object) -> None:
        msg = "analyse should not be called when the init subcommand is used"
        raise AssertionError(msg)

    monkeypatch.setattr(cli, "init_config", fake_init_config)
    monkeypatch.setattr(cli, "analyse", fake_analyse)
    monkeypatch.setattr("sys.argv", ["pyflowbatt", "init", "--folder", str(tmp_path)])

    cli.main()

    assert called_with == {"folder": str(tmp_path)}


def test_main_default_behavior_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a subcommand, `pyflowbatt --dry --folder X` still dispatches to analyse."""
    called_with: dict = {}

    def fake_analyse(**kwargs: object) -> None:
        called_with.update(kwargs)

    def fake_init_config(*_args: object, **_kwargs: object) -> None:
        msg = "init_config should not be called without the init subcommand"
        raise AssertionError(msg)

    monkeypatch.setattr(cli, "analyse", fake_analyse)
    monkeypatch.setattr(cli, "init_config", fake_init_config)
    monkeypatch.setattr("sys.argv", ["pyflowbatt", "--dry", "--folder", "somefolder"])

    cli.main()

    assert called_with == {"folder": "somefolder", "dry": True, "zip_output": False}


def test_main_zip_flag_dispatches_zip_output_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """`pyflowbatt --zip --folder X` dispatches to analyse with zip_output=True."""
    called_with: dict = {}

    def fake_analyse(**kwargs: object) -> None:
        called_with.update(kwargs)

    monkeypatch.setattr(cli, "analyse", fake_analyse)
    monkeypatch.setattr("sys.argv", ["pyflowbatt", "--zip", "--folder", "somefolder"])

    cli.main()

    assert called_with == {"folder": "somefolder", "dry": False, "zip_output": True}
