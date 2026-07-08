"""PyFlowBatt CLI."""

import argparse
import logging
from pathlib import Path

from pyflowbatt.config import CONFIG_FILENAME, write_template_config


def init_config(folder: str | None = None) -> None:
    """Write a template pyflowbatt.toml into folder (default: current directory)."""
    folderpath = Path.cwd() if not folder else Path(folder)
    logger = logging.getLogger("pyflowbatt")
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)

    try:
        path = write_template_config(folderpath)
    except FileExistsError:
        # Expected/handled condition, not a bug - a traceback here would be noisy.
        logger.error("%s already exists, not overwriting", folderpath / CONFIG_FILENAME)  # noqa: TRY400
        return
    logger.info("Wrote template config to %s", path)


def analyse(
    folder: str | None = None,
    *,
    dry: bool = False,
    max_folder_searches: int = 10000,
    max_search_depth: int = 6,
) -> None:
    """Run PyFlowBatt on a folder."""
    from pyflowbatt.analysis import analyse_all_samples, dry_analyse_all_samples  # noqa: PLC0415

    folderpath = Path.cwd() if not folder else Path(folder)
    logger = logging.getLogger("pyflowbatt")
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)

    logger.info("Running PyFlowBatt on: %s", folderpath)
    if not folderpath.is_dir():
        logger.error("%s is not a directory", folderpath)
        return
    if dry:
        dry_analyse_all_samples(
            folderpath,
            max_folder_searches=max_folder_searches,
            max_search_depth=max_search_depth,
        )
    else:
        analyse_all_samples(
            folderpath,
            max_folder_searches=max_folder_searches,
            max_search_depth=max_search_depth,
        )


def main() -> None:
    """PyFlowBatt CLI."""
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
    analyse(folder=args.folder, dry=args.dry, **kwargs)


if __name__ == "__main__":
    main()
