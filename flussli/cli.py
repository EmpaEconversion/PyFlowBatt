"""Flussli CLI."""

import argparse
import logging
from pathlib import Path

from flussli.analysis import analyse_all_samples, dry_analyse_all_samples


def flussli(folder: str | None = None, *, dry: bool = False) -> None:
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
        dry_analyse_all_samples(folderpath)
    else:
        analyse_all_samples(folderpath)


def main() -> None:
    """Flussli CLI."""
    parser = argparse.ArgumentParser(prog="flussli")
    parser.add_argument(
        "--dry", action="store_true", help="Don't do any analysis, just check the folders"
    )
    parser.add_argument(
        "--folder", type=str, help="Folder to analyse, if not specified, analyse current folder"
    )
    args = parser.parse_args()
    flussli(folder=args.folder, dry=args.dry)


if __name__ == "__main__":
    main()
