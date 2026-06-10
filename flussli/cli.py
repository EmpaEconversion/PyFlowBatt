"""Flussli CLI."""

import argparse
import logging
from pathlib import Path

from flussli.analysis import analyse_all_samples, dry_analyse_all_samples


def flussli(
    folder: str | None = None,
    *,
    dry: bool = False,
    max_folder_searches: int = 10000,
    max_search_depth: int = 6,
) -> None:
    """Run flussli on a folder."""
    folderpath = Path.cwd() if not folder else Path(folder)
    logger = logging.getLogger("flussli")
    logger.addHandler(logging.StreamHandler())
    logger.setLevel(logging.INFO)

    logger.info("Running flussli on: %s", folderpath)
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
    """Flussli CLI."""
    parser = argparse.ArgumentParser(prog="flussli")
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
    kwargs: dict = {}
    if args.max_folder_searches is not None:
        kwargs["max_folder_searches"] = args.max_folder_searches
    if args.max_search_depth is not None:
        kwargs["max_search_depth"] = args.max_search_depth
    flussli(folder=args.folder, dry=args.dry, **kwargs)


if __name__ == "__main__":
    main()
