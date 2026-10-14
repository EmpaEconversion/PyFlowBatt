"""Tests for the terminal progress reporter."""

import io
import logging
from pathlib import Path

import pytest
from rich.console import Console

from pyflowbatt.report import Reporter, RichReporter

logger = logging.getLogger("pyflowbatt.test")


def make_reporter(*, verbose: bool = False) -> tuple[RichReporter, io.StringIO]:
    """Make a reporter writing plain text to a buffer."""
    out = io.StringIO()
    console = Console(file=out, width=120, color_system=None)
    return RichReporter(console, verbose=verbose), out


@pytest.fixture(autouse=True)
def _debug_level() -> None:
    logger.setLevel(logging.DEBUG)


def test_task_icons_follow_worst_level() -> None:
    """Clean steps get a tick, warnings a warning sign, errors a cross."""
    reporter, out = make_reporter()
    with reporter.section("[1/1] sample"):
        with reporter.task("OCV"):
            logger.info("fine")
        with reporter.task("LSV post"):
            logger.warning("- Only one LSV file found")
        with reporter.task("EIS pre"):
            logger.error("Failed to fit eis_pre")
        reporter.skip("CV", "no files")

    text = out.getvalue()
    assert "✔ OCV" in text
    assert "⚠ LSV post" in text
    assert "✖ EIS pre" in text
    assert "– CV" in text  # noqa: RUF001
    assert "no files" in text
    assert "⚠ LSV post: Only one LSV file found" in text
    assert "✖ EIS pre: Failed to fit eis_pre" in text
    assert "fine" not in text


def test_verbose_lists_info_records() -> None:
    """With verbose, info records from a step are listed after the section."""
    reporter, out = make_reporter(verbose=True)
    with reporter.section("s"), reporter.task("OCV"):
        logger.info("read 10 rows")
    assert "OCV: read 10 rows" in out.getvalue()


def test_records_outside_tasks_grouped_without_label() -> None:
    """A warning inside a section but outside any step is listed unlabelled."""
    reporter, out = make_reporter()
    with reporter.section("s"):
        logger.warning("Could not find sample ID")
    assert "⚠ Could not find sample ID" in out.getvalue()


def test_exception_marks_task_failed_and_propagates() -> None:
    """An exception escaping a step shows a cross and is re-raised."""
    reporter, out = make_reporter()

    def fail() -> None:
        with reporter.section("s"), reporter.task("BattINFO"):
            msg = "mismatch"
            raise ValueError(msg)

    with pytest.raises(ValueError, match="mismatch"):
        fail()
    assert "✖ BattINFO: ValueError: mismatch" in out.getvalue()


def test_console_handler_quiet_inside_sections() -> None:
    """The console handler only prints records from outside sections."""
    reporter, out = make_reporter()
    root = logging.getLogger()
    root.addHandler(reporter.log_handler)
    try:
        logger.warning("before")
        with reporter.section("s"), reporter.task("OCV"):
            logger.warning("inside")
    finally:
        root.removeHandler(reporter.log_handler)
    text = out.getvalue()
    assert text.count("before") == 1
    assert text.count("inside") == 1


def test_records_still_propagate(caplog: pytest.LogCaptureFixture) -> None:
    """Capturing for display does not hide records from other handlers."""
    reporter, _ = make_reporter()
    with caplog.at_level("WARNING"), reporter.section("s"), reporter.task("OCV"):
        logger.warning("visible")
    assert "visible" in caplog.text


def test_finish_counts_warnings_and_errors() -> None:
    """The final line totals warnings and errors across sections."""
    reporter, out = make_reporter()
    reporter.start(Path("root"), 2)
    for name in ("a", "b"):
        with reporter.section(name), reporter.task("OCV"):
            logger.warning("w")
    with reporter.section("c"), reporter.task("EIS"):
        logger.error("e")
    reporter.finish()
    assert "Done: 2 samples, 2 warnings, 1 error" in out.getvalue()


def test_base_reporter_is_silent() -> None:
    """The default reporter runs steps without output or capture."""
    reporter = Reporter()
    with reporter.section("s"), reporter.task("OCV", detail="x"):
        reporter.skip("CV", "no files")
    reporter.finish()


def test_ascii_icons_when_output_cannot_encode_symbols() -> None:
    """Output in a non-UTF encoding gets ASCII icons instead of failing to encode."""
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="cp1252")
    reporter = RichReporter(Console(file=out, width=120, color_system=None))
    with reporter.section("s"), reporter.task("OCV"):
        logger.warning("w")
    out.flush()
    text = raw.getvalue().decode("cp1252")
    assert "! OCV" in text
    assert "! OCV: w" in text


def test_quiet_logger_warnings_hidden_and_do_not_mark_task() -> None:
    """BattINFO converter warnings are informational, its critical messages still show."""
    reporter, out = make_reporter()
    quiet = logging.getLogger("battinfoconverter_backend.validate")
    with reporter.section("s"):
        with reporter.task("BattINFO"):
            quiet.warning("Term already in the default context")
        with reporter.task("Metadata"):
            quiet.critical("Missing required value")
    text = out.getvalue()
    assert "✔ BattINFO" in text
    assert "default context" not in text
    assert "✖ Metadata: Missing required value" in text


def test_quiet_logger_warnings_shown_when_verbose() -> None:
    """With verbose, quiet loggers' warnings are listed as info."""
    reporter, out = make_reporter(verbose=True)
    with reporter.section("s"), reporter.task("BattINFO"):
        logging.getLogger("battinfoconverter_backend").warning("Term already in context")
    text = out.getvalue()
    assert "✔ BattINFO" in text
    assert "· BattINFO: Term already in context" in text


def test_planned_steps_keep_their_order() -> None:
    """Steps fill the planned lines in place, whatever order they run in."""
    reporter, out = make_reporter()
    with reporter.section("s"):
        reporter.plan(["BattINFO", "OCV", "GCPL"])
        with reporter.task("GCPL"):
            pass
        reporter.skip("BattINFO", "no xlsx")
        with reporter.task("OCV"):
            pass
    text = out.getvalue()
    assert text.index("BattINFO") < text.index("OCV") < text.index("GCPL")
    assert text.count("GCPL") == 1


def test_planned_steps_never_run_marked_not_run() -> None:
    """A planned step left waiting when the section ends, e.g. after an error, says so."""
    reporter, out = make_reporter()

    def fail() -> None:
        with reporter.section("s"):
            reporter.plan(["BattINFO", "Summary"])
            with reporter.task("BattINFO"):
                msg = "mismatch"
                raise ValueError(msg)

    with pytest.raises(ValueError, match="mismatch"):
        fail()
    assert "– Summary" in out.getvalue()  # noqa: RUF001
    assert "not run" in out.getvalue()


def test_pending_lines_rendered_before_steps_run() -> None:
    """The live block lists every planned step as waiting before any of them starts."""
    reporter, _ = make_reporter()
    reporter.plan(["OCV", "GCPL"])
    out = io.StringIO()
    Console(file=out, width=80, color_system=None).print(reporter._render())  # noqa: SLF001
    assert "○ OCV" in out.getvalue()
    assert "○ GCPL" in out.getvalue()
