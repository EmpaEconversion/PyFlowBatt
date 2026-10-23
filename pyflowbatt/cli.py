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
) -> None:
    """Run PyFlowBatt on a folder."""
    import matplotlib as mpl  # noqa: PLC0415

    # Plots are only saved, and a GUI backend breaks when the live display's thread collects them
    mpl.use("Agg")

    from pyflowbatt.analysis import analyse_all_samples, dry_analyse_all_samples  # noqa: PLC0415

    folderpath = Path.cwd() if not folder else Path(folder)
    reporter = RichReporter(verbose=verbose)
    if not folderpath.is_dir():
        with _console_logging(reporter):
            logger.error("%s is not a directory", folderpath)
        return
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
                analyse_all_samples(
                    folderpath,
                    max_folder_searches=max_folder_searches,
                    max_search_depth=max_search_depth,
                    zip_output=zip_output,
                    reporter=reporter,
                )
        except ConfigError as e:
            # The message names the file and line, a traceback would only hide it
            logger.error("%s", e)  # noqa: TRY400


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
        "--folder", type=str, help="Folder to analyse, if not specified, analyse current folder"
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
    analyse(folder=args.folder, dry=args.dry, zip_output=args.zip, verbose=args.verbose, **kwargs)


if __name__ == "__main__":
    main()
