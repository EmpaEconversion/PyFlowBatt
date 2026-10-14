"""Terminal display of analysis progress, one live line per step with warnings grouped after."""

import logging
import re
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.padding import Padding
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.traceback import Traceback

from pyflowbatt.version import __title__, __version__

ICONS = {
    "ok": ("✔", "green"),
    "warn": ("⚠", "yellow"),
    "fail": ("✖", "red"),
    "skip": ("–", "dim"),  # noqa: RUF001
    "info": ("·", "dim"),
    "pending": ("○", "dim"),
}
# For output that can't encode the symbols above, e.g. redirected to a cp1252 file on Windows
ASCII_ICONS = {
    "ok": ("+", "green"),
    "warn": ("!", "yellow"),
    "fail": ("x", "red"),
    "skip": ("-", "dim"),
    "info": ("-", "dim"),
    "pending": (".", "dim"),
}

LOG_FILENAME = "pyflowbatt.log"

# Loggers whose warnings are informational, shown only with verbose
QUIET_LOGGERS = ("battinfoconverter_backend",)

# First line of a warning routed through logging.captureWarnings: "path:line: Category: message"
_PY_WARNING_RE = re.compile(r"^(?P<path>.+):(?P<line>\d+): (?P<category>\w+): (?P<message>.*)$")


class Reporter:
    """Progress hooks called during analysis, does nothing by default."""

    def start(self, folder: Path, n_samples: int) -> None:
        """Begin a run over n_samples sample folders."""

    @contextmanager
    def section(self, title: str) -> Generator[None, None, None]:  # noqa: ARG002
        """Group the steps run inside under a heading."""
        yield

    def plan(self, labels: list[str]) -> None:
        """Announce the steps a section will run, in order."""

    @contextmanager
    def task(self, label: str, detail: str = "") -> Generator[None, None, None]:  # noqa: ARG002
        """Run one step inside a section, detail is shown next to it (e.g. an output file)."""
        yield

    def skip(self, label: str, reason: str) -> None:
        """Show a step that had nothing to do."""

    def finish(self) -> None:
        """End the run."""


@dataclass
class _Line:
    """One step and the log records emitted while it ran."""

    label: str
    status: str = "running"
    detail: str = ""
    error: str = ""
    start: float = field(default_factory=time.perf_counter)
    end: float | None = None
    spinner: Spinner = field(default_factory=lambda: Spinner("dots", style="cyan"))
    records: list[logging.LogRecord] = field(default_factory=list)

    def close(self, level: int) -> None:
        """Finish the step, level is the worst level of its records."""
        self.end = time.perf_counter()
        if self.error or level >= logging.ERROR:
            self.status = "fail"
        elif level >= logging.WARNING:
            self.status = "warn"
        else:
            self.status = "ok"


class _CaptureHandler(logging.Handler):
    """Attach every record to the step that is currently running."""

    def __init__(self, reporter: "RichReporter") -> None:
        super().__init__()
        self.reporter = reporter

    def emit(self, record: logging.LogRecord) -> None:
        if self.reporter.current is not None:
            self.reporter.current.records.append(record)


class _ConsoleHandler(logging.Handler):
    """Print records from outside any section, sections print their own."""

    def __init__(self, reporter: "RichReporter") -> None:
        super().__init__()
        self.reporter = reporter

    def emit(self, record: logging.LogRecord) -> None:
        if self.reporter.capturing:
            return
        self.reporter.print_record(record, indent=False)


class RichReporter(Reporter):
    """Live spinner per step in the terminal, warnings and errors listed after each section."""

    def __init__(
        self,
        console: Console | None = None,
        *,
        verbose: bool = False,
        quiet_loggers: tuple[str, ...] = QUIET_LOGGERS,
    ) -> None:
        """Verbose also lists info and debug records, and prints full tracebacks."""
        self.console = console or Console()
        self.verbose = verbose
        self.quiet_loggers = quiet_loggers
        self.log_path: Path | None = None
        self.icons = ICONS if self.console.encoding.lower().startswith("utf") else ASCII_ICONS
        self.capturing = False
        self.current: _Line | None = None
        self.log_handler = _ConsoleHandler(self)
        self._lines: list[_Line] = []
        self._title = ""
        self._live: Live | None = None
        self._n_samples = 0
        self._warnings = 0
        self._errors = 0

    def start(self, folder: Path, n_samples: int) -> None:
        """Print the run header."""
        self._n_samples = n_samples
        self.console.print(
            Text.assemble(
                (f"{__title__} {__version__}", "bold"),
                f" {self.icons['info'][0]} {folder} {self.icons['info'][0]} ",
                _plural(n_samples, "sample"),
            )
        )

    @contextmanager
    def section(self, title: str) -> Generator[None, None, None]:
        """Show a live block of steps, then the warnings and errors they logged."""
        loose = _Line("")
        self._title = title
        self._lines = []
        self.current = loose
        capture = _CaptureHandler(self)
        root = logging.getLogger()
        root.addHandler(capture)
        self.capturing = True
        try:
            yield
        finally:
            for line in self._lines:
                if line.status == "pending":
                    line.status, line.detail = "skip", "not run"
            if self._live is None:
                self.console.print(self._render())
            else:
                self._live.stop()
                self._live = None
                if not self.console.is_terminal:
                    self.console.line()  # Live only ends its block with a newline on terminals
            root.removeHandler(capture)
            self.capturing = False
            self.current = None
            for line in [loose, *self._lines]:
                self._print_line_records(line)
            self._lines = []

    def plan(self, labels: list[str]) -> None:
        """Show the section's steps as waiting, so the block doesn't grow as they run."""
        self._lines.extend(_Line(label, status="pending") for label in labels)
        self._start_live()

    @contextmanager
    def task(self, label: str, detail: str = "") -> Generator[None, None, None]:
        """Show a spinner while the step runs, then an icon for the worst level it logged."""
        line = self._claim(label)
        line.status, line.detail, line.start = "running", detail, time.perf_counter()
        previous, self.current = self.current, line
        try:
            yield
        except BaseException as e:
            line.error = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            raise
        finally:
            self.current = previous
            line.close(max(map(self._shown_level, line.records), default=logging.NOTSET))

    def skip(self, label: str, reason: str) -> None:
        """Show a dimmed line for a step with nothing to do."""
        line = self._claim(label)
        line.status, line.detail = "skip", reason

    def _claim(self, label: str) -> _Line:
        """Planned line waiting under this label, or a new line after the others."""
        for line in self._lines:
            if line.status == "pending" and line.label == label:
                return line
        line = _Line(label)
        self._lines.append(line)
        self._start_live()
        return line

    def _start_live(self) -> None:
        """Start drawing the section once it has lines, so its title and steps appear together."""
        if self._live is None:
            self._live = Live(
                get_renderable=self._render, console=self.console, refresh_per_second=10
            )
            self._live.start(refresh=True)

    def finish(self) -> None:
        """Print totals for the run."""
        parts = [_plural(self._n_samples, "sample")]
        if self._warnings:
            parts.append(_plural(self._warnings, "warning"))
        if self._errors:
            parts.append(_plural(self._errors, "error"))
        style = "red" if self._errors else "yellow" if self._warnings else "green"
        self.console.print()
        self.console.print(Text(f"Done: {', '.join(parts)}", style=style))
        if self.log_path:
            self.console.print(Text(f"Full log: {self.log_path}", style="dim"))

    def print_record(self, record: logging.LogRecord, label: str = "", *, indent: bool) -> None:
        """Print one log record with an icon for its level."""
        level = self._shown_level(record)
        if level < record.levelno and not self.verbose:
            return
        if level >= logging.ERROR:
            self._errors += 1
            icon, style = self.icons["fail"]
        elif level >= logging.WARNING:
            self._warnings += 1
            icon, style = self.icons["warn"]
        else:
            icon, style = self.icons["info"]
        message = record.getMessage()
        if not indent and level < logging.WARNING:
            self.console.print(Text(message))
            return
        message = message.strip().removeprefix("- ")
        if record.name == "py.warnings" and not self.verbose:
            message = _short_warning(message)
        exc = record.exc_info[1] if record.exc_info else None
        if exc is not None and not self.verbose:
            message += f": {exc}"
        self._print_icon_line(icon, style, label, message, indent=indent)
        if exc is not None and self.verbose and record.exc_info:
            self.console.print(Traceback.from_exception(*record.exc_info))

    def _print_line_records(self, line: _Line) -> None:
        for record in line.records:
            if self._shown_level(record) >= logging.WARNING or self.verbose:
                self.print_record(record, line.label, indent=True)
        if line.error:
            self._errors += 1
            self._print_icon_line(*self.icons["fail"], line.label, line.error, indent=True)

    def _shown_level(self, record: logging.LogRecord) -> int:
        """Level a record is shown at, quiet loggers' warnings count as info."""
        quiet = any(
            record.name == name or record.name.startswith(f"{name}.") for name in self.quiet_loggers
        )
        if quiet and record.levelno < logging.ERROR:
            return min(record.levelno, logging.INFO)
        return record.levelno

    def _print_icon_line(
        self, icon: str, style: str, label: str, message: str, *, indent: bool
    ) -> None:
        """Print a message after an icon, wrapped lines aligned with the message."""
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=1)
        grid.add_column(overflow="fold")
        text = Text(f"{label}: ", style="bold") if label else Text()
        text.append(message)
        grid.add_row(Text(icon, style=style), text)
        self.console.print(Padding(grid, (0, 0, 0, 2 if indent else 0), expand=False))

    def _render(self) -> Group:
        table = Table.grid(padding=(0, 1))
        table.add_column(width=1)
        table.add_column(min_width=16)
        table.add_column(style="dim")
        now = time.perf_counter()
        for line in self._lines:
            if line.status == "running":
                icon = line.spinner
            else:
                symbol, style = self.icons[line.status]
                icon = Text(symbol, style=style)
            if line.status == "pending":
                table.add_row(icon, Text(line.label, style="dim"), "")
            elif line.status == "skip":
                table.add_row(icon, line.label, line.detail)
            else:
                elapsed = (line.end or now) - line.start
                table.add_row(icon, line.label, f"{elapsed:5.1f}s  {line.detail}")
        return Group(
            Text(), Text(self._title, style="bold"), Padding(table, (0, 0, 0, 2), expand=False)
        )


def _short_warning(message: str) -> str:
    """Shorten a captured Python warning to "Category: message (file:line)"."""
    match = _PY_WARNING_RE.match(message.splitlines()[0])
    if not match:
        return message
    return f"{match['category']}: {match['message']} ({Path(match['path']).name}:{match['line']})"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"
