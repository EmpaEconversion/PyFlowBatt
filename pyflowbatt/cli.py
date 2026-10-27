"""PyFlowBatt CLI."""

import argparse
import io
import logging
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from pyflowbatt.config import CONFIG_FILENAME, ConfigError, write_template_config
from pyflowbatt.report import LOG_FILENAME, RichReporter

logger = logging.getLogger("pyflowbatt")


@contextmanager
def _console_logging(
    reporter: RichReporter, *, verbose: bool = False, log_path: Path | None = None
) -> Generator[None, None, None]:
    """Send log records, including Python warnings, to the reporter's console and a log file."""
    root = logging.getLogger()
    reporter.log_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    handlers: list[logging.Handler] = [reporter.log_handler]
    if log_path is not None:
        try:
            file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
        except OSError as e:
            reporter.console.print(f"Not writing a log file, cannot open {log_path}: {e}")
        else:
            file_handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
            )
            handlers.append(file_handler)
            reporter.log_path = log_path
    for handler in handlers:
        root.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.captureWarnings(True)  # noqa: FBT003
    try:
        yield
    finally:
        logging.captureWarnings(False)  # noqa: FBT003
        logger.setLevel(previous_level)
        for handler in handlers:
            root.removeHandler(handler)
            if handler is not reporter.log_handler:
                handler.close()


def init_config(folder: str | None = None) -> None:
    """Write a template pyflowbatt.toml into folder (default: current directory)."""
    folderpath = Path.cwd() if not folder else Path(folder)
    with _console_logging(RichReporter()):
        try:
            path = write_template_config(folderpath)
        except FileExistsError:
            # Expected/handled condition, not a bug - a traceback here would be noisy.
            logger.error("%s already exists, not overwriting", folderpath / CONFIG_FILENAME)  # noqa: TRY400
            return
        logger.info("Wrote template config to %s", path)


def analyse(  # noqa: PLR0913
    folder: str | None = None,
    *,
    dry: bool = False,
    zip_output: bool = False,
    max_folder_searches: int = 10000,
    max_search_depth: int = 6,
    verbose: bool = False,
) -> dict[Path, str]:
    """Run PyFlowBatt on a folder, returns the sample ID of each analysed sample folder."""
    import matplotlib as mpl  # noqa: PLC0415

    # Plots are only saved, and a GUI backend breaks when the live display's thread collects them
    mpl.use("Agg")

    from pyflowbatt.analysis import analyse_all_samples, dry_analyse_all_samples  # noqa: PLC0415

    folderpath = Path.cwd() if not folder else Path(folder)
    reporter = RichReporter(verbose=verbose)
    if not folderpath.is_dir():
        with _console_logging(reporter):
            logger.error("%s is not a directory", folderpath)
        return {}
    log_path = None if dry else folderpath / LOG_FILENAME
    with _console_logging(reporter, verbose=verbose, log_path=log_path):
        try:
            if dry:
                logger.info("Running PyFlowBatt on: %s", folderpath)
                dry_analyse_all_samples(
                    folderpath,
                    max_folder_searches=max_folder_searches,
                    max_search_depth=max_search_depth,
                    zip_output=zip_output,
                )
            else:
                return analyse_all_samples(
                    folderpath,
                    max_folder_searches=max_folder_searches,
                    max_search_depth=max_search_depth,
                    zip_output=zip_output,
                    reporter=reporter,
                )
        except ConfigError as e:
            # The message names the file and line, a traceback would only hide it
            logger.error("%s", e)  # noqa: TRY400
    return {}


def _combined_summary_path(path: str, *, dry: bool) -> Path:
    """Resolve --combined-summary to an .xlsx file, raising ValueError if it can't be written."""
    from pyflowbatt.analysis import check_path_writable  # noqa: PLC0415

    summary_path = Path(path).resolve()
    if summary_path.is_dir():
        summary_path /= "combined_summary.xlsx"
    elif summary_path.suffix.lower() != ".xlsx":
        msg = f"--combined-summary must be a folder or an .xlsx file, not {path}"
        raise ValueError(msg)
    if not summary_path.parent.is_dir():
        msg = f"Cannot write {summary_path.name}: {summary_path.parent} is not a directory"
        raise ValueError(msg)
    if not dry:
        try:
            check_path_writable(summary_path)
        except PermissionError as e:
            raise ValueError(str(e)) from e
    return summary_path


def analyse_folders(  # noqa: PLR0913
    folders: list[str],
    *,
    combined_summary: str | None = None,
    dry: bool = False,
    zip_output: bool = False,
    max_folder_searches: int = 10000,
    max_search_depth: int = 6,
    verbose: bool = False,
) -> None:
    """Run PyFlowBatt on each folder, then combine all their summaries if asked to."""
    from pyflowbatt.analysis import write_multi_folder_summary  # noqa: PLC0415

    paths = list(dict.fromkeys(Path(f).resolve() for f in folders))
    with _console_logging(RichReporter()):
        if bad := [p for p in paths if not p.is_dir()]:
            logger.error("Not a directory: %s", ", ".join(map(str, bad)))
            return
        if nested := [(a, b) for a in paths for b in paths if a != b and b.is_relative_to(a)]:
            # Samples in both would be analysed twice and counted twice in the averages
            logger.error("%s is inside %s, pass only one of them", *nested[0][::-1])
            return
        try:
            summary_path = (
                _combined_summary_path(combined_summary, dry=dry) if combined_summary else None
            )
        except ValueError as e:
            logger.error("%s", e)  # noqa: TRY400
            return

    sample_ids_by_folder = {
        path: analyse(
            str(path),
            dry=dry,
            zip_output=zip_output,
            max_folder_searches=max_folder_searches,
            max_search_depth=max_search_depth,
            verbose=verbose,
        )
        for path in paths
    }
    if summary_path is None:
        return

    reporter = RichReporter(verbose=verbose)
    with _console_logging(reporter, verbose=verbose):
        if dry:
            logger.info(
                "Then I would combine the summaries from all %d folders into '%s'.",
                len(paths),
                summary_path,
            )
            return
        with (
            reporter.section(f"Across {len(paths)} folders"),
            reporter.task("Combined summary", detail=summary_path.name),
        ):
            written = write_multi_folder_summary(sample_ids_by_folder, summary_path)
            if not written:
                logger.error("No sample summaries to combine")
        if written:
            logger.info("\nCombined summary of all folders: %s", summary_path)


def main() -> None:
    """PyFlowBatt CLI."""
    if isinstance(sys.stdout, io.TextIOWrapper):
        # File names and messages can hold characters a redirected cp1252 stdout can't encode
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(prog="pyflowbatt")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser(
        "init",
        help=f"Write a template {CONFIG_FILENAME} into a folder (fully commented out)",
    )
    init_parser.add_argument(
        "--folder",
        type=str,
        help="Folder to write the template into, if not specified, use the current folder",
    )

    parser.add_argument(
        "--dry", action="store_true", help="Don't do any analysis, just check the folders"
    )
    parser.add_argument(
        "--folder",
        type=str,
        nargs="+",
        help=(
            "Folder(s) to analyse, if not specified, analyse current folder. "
            "Several folders are analysed one after another."
        ),
    )
    parser.add_argument(
        "--combined-summary",
        type=str,
        metavar="PATH",
        help=(
            "Also combine the summaries of every folder analysed into one Excel file at PATH, "
            "an .xlsx file or a folder to write combined_summary.xlsx into"
        ),
    )
    parser.add_argument(
        "--zip",
        action="store_true",
        help=(
            "Package the analysed folder for Zenodo: zip it with an RO-Crate manifest inside, "
            "and reference files in BattINFO metadata via the zip's Zenodo download URL. "
            "Default: only analyse, without a zip or RO-Crate."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show info and debug messages for each step, and full tracebacks",
    )
    parser.add_argument(
        "--max-folder-searches",
        type=int,
        help="Maximum number of folders that can be checked when looking for sample folders",
    )
    parser.add_argument(
        "--max-search-depth",
        type=int,
        help="Maximum depth of searching when looking for sample folders",
    )
    args = parser.parse_args()

    if args.command == "init":
        init_config(folder=args.folder)
        return

    kwargs: dict = {}
    if args.max_folder_searches is not None:
        kwargs["max_folder_searches"] = args.max_folder_searches
    if args.max_search_depth is not None:
        kwargs["max_search_depth"] = args.max_search_depth
    kwargs.update(dry=args.dry, zip_output=args.zip, verbose=args.verbose)
    if args.combined_summary or (args.folder and len(args.folder) > 1):
        folders = args.folder or [str(Path.cwd())]
        analyse_folders(folders, combined_summary=args.combined_summary, **kwargs)
    else:
        analyse(folder=args.folder[0] if args.folder else None, **kwargs)


if __name__ == "__main__":
    main()
