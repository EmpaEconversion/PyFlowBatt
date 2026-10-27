"""Tests for the PyFlowBatt CLI, in particular `pyflowbatt init`."""

import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from pyflowbatt import cli
from pyflowbatt.analysis import folder_labels, zip_folder


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

    assert called_with == {
        "folder": "somefolder",
        "dry": True,
        "zip_output": False,
        "verbose": False,
    }


def test_main_zip_flag_dispatches_zip_output_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """`pyflowbatt --zip --folder X` dispatches to analyse with zip_output=True."""
    called_with: dict = {}

    def fake_analyse(**kwargs: object) -> None:
        called_with.update(kwargs)

    monkeypatch.setattr(cli, "analyse", fake_analyse)
    monkeypatch.setattr("sys.argv", ["pyflowbatt", "--zip", "--folder", "somefolder"])

    cli.main()

    assert called_with == {
        "folder": "somefolder",
        "dry": False,
        "zip_output": True,
        "verbose": False,
    }


def test_analyse_bad_toml_logs_error(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A broken pyflowbatt.toml is reported as one error message, not a traceback."""
    (tmp_path / "pyflowbatt.toml").write_text("--- Electrode area ---\n")

    with caplog.at_level("ERROR", logger="pyflowbatt"):
        cli.analyse(folder=str(tmp_path))

    assert "line 1: --- Electrode area ---" in caplog.text


def test_analyse_writes_log_file(tmp_path: Path) -> None:
    """A real run writes all logs to pyflowbatt.log."""
    cli.analyse(folder=str(tmp_path))
    log = (tmp_path / "pyflowbatt.log").read_text(encoding="utf-8")
    assert "No sample folders found" in log


def test_dry_run_writes_no_log_file(tmp_path: Path) -> None:
    """Dry runs do not change files."""
    cli.analyse(folder=str(tmp_path), dry=True)
    assert not (tmp_path / "pyflowbatt.log").exists()


def test_zip_skips_log_file(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The log is not included with the zip, and is left out without a warning."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "data.csv").write_text("x")
    (root / "pyflowbatt.log").write_text("log")
    with caplog.at_level("WARNING", logger="pyflowbatt"):
        zip_path = zip_folder(root, tmp_path / "out.zip", ["data.csv"])
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.namelist() == ["data.csv"]
    assert caplog.records == []


@pytest.mark.parametrize("verbose", [False, True])
def test_log_file_has_debug_only_when_verbose(tmp_path: Path, *, verbose: bool) -> None:
    """Debug records reach pyflowbatt.log only with --verbose."""
    (tmp_path / "pyflowbatt.toml").write_text("")
    cli.analyse(folder=str(tmp_path), verbose=verbose)
    log = (tmp_path / "pyflowbatt.log").read_text(encoding="utf-8")
    assert ("Loading PyFlowBatt config from" in log) is verbose


@pytest.mark.parametrize(
    ("argv", "folders", "combined_summary"),
    [
        (["--folder", "x", "y"], ["x", "y"], None),
        (["--folder", "x", "--combined-summary", "out.xlsx"], ["x"], "out.xlsx"),
    ],
)
def test_main_dispatches_analyse_folders(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    folders: list[str],
    combined_summary: str | None,
) -> None:
    """Several folders, or --combined-summary, dispatch to analyse_folders."""
    called_with: dict = {}

    def fake_analyse_folders(folders: list[str], **kwargs: object) -> None:
        called_with.update(kwargs, folders=folders)

    monkeypatch.setattr(cli, "analyse_folders", fake_analyse_folders)
    monkeypatch.setattr("sys.argv", ["pyflowbatt", *argv])

    cli.main()

    assert called_with == {
        "folders": folders,
        "combined_summary": combined_summary,
        "dry": False,
        "zip_output": False,
        "verbose": False,
    }


def _write_summary(sample_folder: Path, values: dict[str, float]) -> None:
    import pandas as pd  # noqa: PLC0415

    path = sample_folder / "results" / "summary.xlsx"
    path.parent.mkdir(parents=True)
    df = pd.DataFrame({"Value": list(values.values()), "Error": None}, index=list(values))
    df.to_excel(path, sheet_name="Summary")


def _fake_analyse(
    summaries: dict[str, dict[str, dict[str, float]]],
) -> Callable[..., dict[Path, str]]:
    """Analyse that writes the given summaries per sample instead of analysing data."""

    def fake(folder: str, **_kwargs: object) -> dict[Path, str]:
        ids = {}
        for sample, values in summaries[Path(folder).name].items():
            _write_summary(Path(folder) / sample, values)
            ids[Path(folder) / sample] = sample
        return ids

    return fake


def _make_folders(tmp_path: Path, names: list[str]) -> list[str]:
    for name in names:
        (tmp_path / name).mkdir(parents=True)
    return [str(tmp_path / name) for name in names]


def test_analyse_folders_combines_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every sample, and each folder's average, go into the --combined-summary file."""
    import pandas as pd  # noqa: PLC0415

    summaries = {
        "x": {"x1": {"OCV": 1.0, "CE": 90.0}, "x2": {"OCV": 3.0, "CE": 92.0}},
        "y": {"y1": {"OCV": 5.0, "Extra": 7.0}},
    }
    folders = _make_folders(tmp_path / "data", list(summaries))
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.setattr(cli, "analyse", _fake_analyse(summaries))

    cli.analyse_folders(folders, combined_summary=str(tmp_path / "elsewhere" / "all.xlsx"))

    sheets = pd.read_excel(tmp_path / "elsewhere" / "all.xlsx", sheet_name=None, index_col=0)
    assert list(sheets) == ["Summary", "By folder"]
    assert list(sheets["Summary"].columns) == ["Average", "Std", "x1", "x2", "y1"]
    assert sheets["Summary"].loc["OCV", "Average"] == 3.0
    by_folder = sheets["By folder"]
    assert list(by_folder.columns) == ["x Average", "x Std", "y Average", "y Std"]
    assert list(by_folder.index) == ["OCV", "Extra", "CE"]
    assert by_folder.loc["OCV", "x Average"] == 2.0
    assert by_folder.loc["OCV", "y Average"] == 5.0
    assert pd.isna(by_folder.loc["CE", "y Average"])


def test_analyse_folders_combined_summary_into_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder given as --combined-summary gets combined_summary.xlsx inside it."""
    folders = _make_folders(tmp_path, ["x", "y"])
    monkeypatch.setattr(cli, "analyse", _fake_analyse({"x": {"x1": {"OCV": 1.0}}, "y": {}}))

    cli.analyse_folders(folders, combined_summary=str(tmp_path))

    assert (tmp_path / "combined_summary.xlsx").exists()


def test_analyse_folders_without_combined_summary_writes_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Several folders are each analysed, with no combined file unless asked for."""
    folders = _make_folders(tmp_path, ["x", "y"])
    analysed = []
    monkeypatch.setattr(cli, "analyse", lambda folder, **_kwargs: analysed.append(folder) or {})

    cli.analyse_folders(folders)

    assert analysed == [str(Path(f).resolve()) for f in folders]
    assert list(tmp_path.glob("*.xlsx")) == []


def test_analyse_folders_dry_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry run writes no combined summary."""
    folders = _make_folders(tmp_path, ["x", "y"])
    monkeypatch.setattr(cli, "analyse", lambda *_args, **_kwargs: {})

    cli.analyse_folders(folders, combined_summary=str(tmp_path), dry=True)

    assert not (tmp_path / "combined_summary.xlsx").exists()


def _fail(*_args: object, **_kwargs: object) -> None:
    msg = "analyse should not run"
    raise AssertionError(msg)


@pytest.mark.parametrize("target", ["missing/all.xlsx", "all.csv"])
def test_analyse_folders_bad_combined_summary_fails_before_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    target: str,
) -> None:
    """An unusable --combined-summary path is reported before any slow analysis runs."""
    folders = _make_folders(tmp_path, ["x", "y"])
    monkeypatch.setattr(cli, "analyse", _fail)

    with caplog.at_level("ERROR", logger="pyflowbatt"):
        cli.analyse_folders(folders, combined_summary=str(tmp_path / target))

    assert caplog.records


def test_analyse_folders_rejects_nested_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A folder inside another would have its samples analysed and averaged twice."""
    (tmp_path / "x" / "inner").mkdir(parents=True)
    monkeypatch.setattr(cli, "analyse", _fail)

    with caplog.at_level("ERROR", logger="pyflowbatt"):
        cli.analyse_folders([str(tmp_path / "x"), str(tmp_path / "x" / "inner")])

    assert "is inside" in caplog.text


def test_folder_labels_add_parents_only_where_names_clash() -> None:
    """Folders are labelled by name, with parent folders added to tell same names apart."""
    folders = [Path("/a/run1"), Path("/b/run1"), Path("/c/x/run2")]
    assert list(folder_labels(folders).values()) == ["a/run1", "b/run1", "run2"]
