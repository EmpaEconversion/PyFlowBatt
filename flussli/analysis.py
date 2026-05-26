"""Utility functions for analysing flow battery data from MPR files."""

import logging
import re
from pathlib import Path
from typing import Literal

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from flussli import cv, eis, gcpl, lsv, ocv
from flussli.config import FlussliConfig
from flussli.read import read_to_bdf

logger = logging.getLogger(__name__)

SAVE_FORMATS = Literal["parquet", "csv"] | None


def get_res_from_filename(s: str) -> float:
    """Get nominal resistance from filename."""
    match = re.search(r"_([\d.,]+)([kM]?Ohm)_", s)

    if match:
        value = float(match.group(1))
        unit = match.group(2)
        if unit == "kOhm":
            value *= 1e3
        elif unit == "MOhm":
            value *= 1e6
        return value
    return np.nan


def get_sampleid_from_folderpath(
    folderpath: str | Path,
    config: FlussliConfig | None = None,
) -> str:
    """Get the sample ID given a folder to a sample.

    Usually it is just the folder name, sometimes the parent.
    Pass a ``FlussliConfig`` to use a custom pattern or an explicit name.
    """
    folderpath = Path(folderpath)

    if config is not None and config.sample_id is not None:
        return config.sample_id

    pattern = config.sample_id_pattern if config is not None else r"^\d+_.+_.+$"
    # Sample ID has format [digits]_[somethingelse]_[somethingelse]
    # e.g. 250115_reda_1M-blahblahblah
    if re.match(pattern=pattern, string=folderpath.stem):
        return folderpath.stem
    try:
        if re.match(pattern=pattern, string=folderpath.parent.stem):
            return folderpath.parent.stem
    except AttributeError:
        pass
    logger.warning("Could not find sample ID in %s", folderpath)
    return "Unknown sample"


def df_save_bdf(df: pd.DataFrame, filepath: Path, save_format: SAVE_FORMATS = "parquet") -> None:
    """Save df to file."""
    if save_format is None:
        return
    if save_format == "parquet":
        df.to_parquet(filepath.with_suffix(".bdf.parquet"))
        return
    if save_format == "csv":
        df.to_csv(filepath.with_suffix(".csv.parquet"))
        return
    msg = "Format not understood"
    raise ValueError(msg)


def _glob_patterns(folder: Path, patterns: list[str], extensions: list[str]) -> list[Path]:
    """Return deduplicated files matching any stem pattern x extension combination."""
    seen: set[Path] = set()
    results: list[Path] = []
    for pattern in patterns:
        for ext in extensions:
            for p in folder.glob(f"{pattern}{ext}"):
                if p not in seen:
                    seen.add(p)
                    results.append(p)
    return results


def analyse_sample(
    folder: str | Path,
    *,
    save_format: SAVE_FORMATS = "parquet",
    config: FlussliConfig | None = None,
    gcpl_files: list[Path] | None = None,
    ocv_files: list[Path] | None = None,
    lsv_files: list[Path] | None = None,
    cv_files_before: list[Path] | None = None,
    cv_files_after: list[Path] | None = None,
    eis_files: list[Path] | None = None,
) -> None:
    """Read all the files in a folder, analayse and plot everything.

    Without explicit files, it will attempt auto-detection for that technique.
    Place a ``flussli.toml`` in the folder/parent/home directory to customise
    glob patterns and sample ID detection. Or pass a FlussliConfig in Python.
    """
    folder = Path(folder)

    if config is None:
        config = FlussliConfig.load(folder)

    logger.info("\n🌊 Flussli-ing %s", folder.name)
    exts = config.extensions
    if gcpl_files is None:
        gcpl_files = _glob_patterns(folder, config.gcpl_patterns, exts)
    gcpl_file = max(gcpl_files, key=lambda x: x.stat().st_size) if gcpl_files else None
    if ocv_files is None:
        ocv_files = _glob_patterns(folder, config.ocv_patterns, exts)
    if lsv_files is None:
        lsv_files = _glob_patterns(folder, config.lsv_patterns, exts)
    if cv_files_before is None:
        cv_files_before = _glob_patterns(folder, config.cv_before_patterns, exts)
    if cv_files_after is None:
        cv_files_after = _glob_patterns(folder, config.cv_after_patterns, exts)
    if eis_files is None:
        eis_files = _glob_patterns(folder, config.eis_patterns, exts)
    logger.debug("Reading GCPL: %s", ", ".join([f.stem for f in gcpl_files]))
    logger.debug("Reading LSV: %s", ", ".join([f.stem for f in lsv_files]))
    logger.debug("Reading CV before: %s", ", ".join([f.stem for f in cv_files_before]))
    logger.debug("Reading CV after:  %s", ", ".join([f.stem for f in cv_files_after]))
    logger.debug("Reading EIS files: %s", ", ".join([f.stem for f in eis_files]))

    cycle_df = None
    cv_df = None
    lsv_df = None
    ratetest_df = None
    eis_df = None
    (folder / "results").mkdir(exist_ok=True)

    logger.info("⛓️‍💥 Analysing OCV")
    if len(ocv_files) == 0:
        logger.info("- ☹️ No OCV found, skipping")
        av_ocv = (np.nan, np.nan)
    else:
        if len(ocv_files) > 1:
            logger.warning("- More than one OCV file, only reading %s", ocv_files[0].stem)
        av_ocv = ocv.analyse(ocv_files[0])

    logger.info("🔋 Analysing GCPL")
    if gcpl_file is None:
        logger.warning("- ☹️ No GCPL files found, skipping")
    else:
        df, cycle_df = gcpl.analyse([gcpl_file])
        fig, _ax = gcpl.plot(df)
        fig.savefig(folder / "results" / "gcpl.png")
        plt.close()
        ratetest_df = gcpl.cycles_to_ratetest(cycle_df)
        df_save_bdf(df, folder / "results" / "gcpl.x", save_format=save_format)

    logger.info("↗️ Analysing LSV")
    lsv_res = {"pre": np.nan, "post": np.nan}
    if len(lsv_files) == 0:
        logger.info("- ☹️ No LSV files found, skipping")
    else:
        # Assume that small number is pre and big number is post
        numbers = [
            int(m.group(1)) if (m := re.match(r"_([\d]+)_LSV_", f.stem)) else 0 for f in lsv_files
        ]
        lsv_files = [f for _, f in sorted(zip(numbers, lsv_files, strict=True))]
        if len(lsv_files) > 2:
            logger.warning(
                "- More than two LSV files, assuming %d is pre and %d is post",
                numbers[0],
                numbers[-1],
            )

        if len(lsv_files) == 1:
            p = "pre" if numbers[0] < 8 else "post"
            logger.warning("- Only one LSV file found, assuming it is %s", p)
            df, results = lsv.analyse(lsv_files[0])
            fig, _ax = lsv.plot(df, results)
            fig.savefig(folder / "results" / f"lsv_{p}.png")
            plt.close(fig)
            df_save_bdf(df, folder / "results" / "lsv_{p}.x", save_format=save_format)
            lsv_res[p] = float(results["Area specific resistance / Ω cm²"])

            results = {"Pre or post cycle": p, **results}
            lsv_df = pd.DataFrame([results])
        else:
            df, results_pre = lsv.analyse(lsv_files[0])
            df_save_bdf(df, folder / "results" / "lsv_pre.x", save_format=save_format)
            fig, _ax = lsv.plot(df, results_pre)
            fig.savefig(folder / "results" / "lsv_pre.png")
            plt.close(fig)
            lsv_res["pre"] = float(results_pre["Area specific resistance / Ω cm²"])

            df, results_post = lsv.analyse(lsv_files[-1])
            df_save_bdf(df, folder / "results" / "lsv_post.x", save_format=save_format)
            fig, _ax = lsv.plot(df, results_post)
            fig.savefig(folder / "results" / "lsv_post.png")
            plt.close(fig)
            lsv_res["post"] = float(results_post["Area specific resistance / Ω cm²"])

            lsv_df = pd.DataFrame(
                [
                    {"Pre or post cycle": "pre", **results_pre},
                    {"Pre or post cycle": "post", **results_post},
                ]
            )

    logger.info("🚴 Analysing CV")
    cv_res = {"pre": np.nan, "post": np.nan}
    if len(cv_files_before) == 0 and len(cv_files_after) == 0:
        logger.warning("- ☹️ No  files found, skipping")
    else:
        for p, cv_files in [("pre", cv_files_before), ("post", cv_files_after)]:
            if not cv_files:
                continue
            if len(cv_files) > 1:
                logger.warning("More than one CV file, only reading %s", cv_files[0].stem)
            df, cv_df, capacitance_mF = cv.analyse(cv_files[0])
            df_save_bdf(df, folder / "results" / f"cv_{p}.x", save_format=save_format)
            fig, _ax = cv.plot(df, cv_df)
            fig.savefig(folder / "results" / f"cv_{p}.png")
            plt.close(fig)
            cv_res[p] = capacitance_mF

    logger.info("🌈 Analysing PEIS")
    eis_res = {}
    rows = []
    if len(eis_files) == 0:
        logger.warning("- ☹️ No EIS files were found, skipping")
    else:
        tags = ["pre", "pre-50%SOC", "post-50%SOC", "post"]
        for tag, eis_file in zip(tags, eis_files, strict=False):
            f = Path(eis_file)
            try:
                df = read_to_bdf(f)
                params, Z_fit = eis.analyse(df)
                fig, _ax = eis.plot(df, Z_fit)
                fig.savefig(folder / "results" / f"eis_{tag}.png")
                eis_res[tag] = params
                df["Real Impedance Fit / ohm"] = np.real(Z_fit)
                df["Real Impedance Fit / ohm"] = np.imag(Z_fit)
                df_save_bdf(df, folder / "results" / f"eis_{tag}.x", save_format=save_format)
                for name, values in params.items():
                    rows.append({"file": f.stem, "tag": tag, "name": name, **values})
            except Exception as e:
                logger.warning("- Failed to fit %s: %s", f.stem, str(e))
        if rows:
            eis_df = pd.DataFrame(rows)
            eis_df = eis_df.pivot(index=["file", "tag"], columns=["name"]).reset_index()
            eis_df.columns = [
                f"{name}_{field}" if name else field for field, name in eis_df.columns
            ]
            order = [
                f"{elem}_{x}"
                for elem in ["L0", "R0", "R1", "CPE1_0", "CPE1_1", "R2", "CPE2_0", "CPE2_1"]
                for x in ["value", "err", "unit"]
            ]
            eis_df = eis_df[["file", "tag", *order]]

    logger.info("💪 Making sample summary")

    # Create keys
    cols = [
        "Av. OCV / V",
        "ASR pre / Ω cm²",
        "ASR post / Ω cm²",
        "∆ASR / Ω cm²",
        "F pre / mF",
        "F post / mF",
        "∆F / mF",
        "Assembled resistance / Ω",
        "EIS R pre / Ω",
        "EIS R pre-50%SOC / Ω",
        "1st CE / %",
        "1st EE / %",
        "1st VE / %",
    ]
    # n cycles to include in the summary file
    n_cycles = [10, 20, 30, 40, 50]
    for n in n_cycles:
        cols.extend(
            [
                f"{n} cycles avg. CE / %",
                f"{n} cycles avg. EE / %",
                f"{n} cycles avg. VE / %",
                f"{n} cycles avg. charge capacity / mAh",
                f"{n} cycles avg. discharge capacity / mAh",
            ]
        )
    summary = {col: {"Value": np.nan, "Error": np.nan} for col in cols}

    # Now fill in values
    summary["Av. OCV / V"]["Value"] = av_ocv[0]
    summary["Av. OCV / V"]["Error"] = av_ocv[1]
    summary["ASR pre / Ω cm²"]["Value"] = lsv_res["pre"]
    summary["ASR post / Ω cm²"]["Value"] = lsv_res["post"]
    summary["∆ASR / Ω cm²"]["Value"] = lsv_res["post"] - lsv_res["pre"]
    summary["F pre / mF"]["Value"] = cv_res["pre"]
    summary["F post / mF"]["Value"] = cv_res["post"]
    summary["∆F / mF"]["Value"] = cv_res["post"] - cv_res["pre"]
    summary["Assembled resistance / Ω"]["Value"] = get_res_from_filename(gcpl_files[0].stem)
    summary["EIS R pre / Ω"]["Value"] = eis_res.get("pre", {}).get("R0", {}).get("value")
    summary["EIS R pre / Ω"]["Error"] = eis_res.get("pre", {}).get("R0", {}).get("err")
    summary["EIS R pre-50%SOC / Ω"]["Value"] = (
        eis_res.get("pre-50%SOC", {}).get("R0", {}).get("value")
    )
    summary["EIS R pre-50%SOC / Ω"]["Error"] = (
        eis_res.get("pre-50%SOC", {}).get("R0", {}).get("err")
    )

    if cycle_df is not None:
        mask = cycle_df["Total Cycle Count / 1"] == 1
        summary["1st CE / %"]["Value"] = float(
            cycle_df.loc[mask, "Coulombic Efficiency / %"].to_numpy()[0]
        )
        summary["1st EE / %"]["Value"] = float(
            cycle_df.loc[mask, "Energy Efficiency / %"].to_numpy()[0]
        )
        summary["1st VE / %"]["Value"] = float(
            cycle_df.loc[mask, "Voltage Efficiency / %"].to_numpy()[0]
        )

        max_cycles = cycle_df["Total Cycle Count / 1"].max()
        for n in n_cycles:
            if n <= max_cycles:
                mask = cycle_df["Total Cycle Count / 1"] <= n
                summary[f"{n} cycles avg. CE / %"]["Value"] = float(
                    cycle_df.loc[mask, "Coulombic Efficiency / %"].mean()
                )
                summary[f"{n} cycles avg. CE / %"]["Error"] = float(
                    cycle_df.loc[mask, "Coulombic Efficiency / %"].std()
                )
                summary[f"{n} cycles avg. EE / %"]["Value"] = float(
                    cycle_df.loc[mask, "Energy Efficiency / %"].mean()
                )
                summary[f"{n} cycles avg. EE / %"]["Error"] = float(
                    cycle_df.loc[mask, "Energy Efficiency / %"].std()
                )
                summary[f"{n} cycles avg. VE / %"]["Value"] = float(
                    cycle_df.loc[mask, "Voltage Efficiency / %"].mean()
                )
                summary[f"{n} cycles avg. VE / %"]["Error"] = float(
                    cycle_df.loc[mask, "Voltage Efficiency / %"].std()
                )
                summary[f"{n} cycles avg. charge capacity / mAh"]["Value"] = float(
                    cycle_df.loc[mask, "Charge Capacity / mAh"].mean()
                )
                summary[f"{n} cycles avg. charge capacity / mAh"]["Error"] = float(
                    cycle_df.loc[mask, "Charge Capacity / mAh"].std()
                )
                summary[f"{n} cycles avg. discharge capacity / mAh"]["Value"] = float(
                    cycle_df.loc[mask, "Discharge Capacity / mAh"].mean()
                )
                summary[f"{n} cycles avg. discharge capacity / mAh"]["Error"] = float(
                    cycle_df.loc[mask, "Discharge Capacity / mAh"].std()
                )

    writer = pd.ExcelWriter(folder / "results" / "summary.xlsx", engine="xlsxwriter")
    df = pd.DataFrame.from_dict(summary, orient="index").reset_index()
    df = df.rename(columns={"index": "Quantity"})
    df.to_excel(writer, sheet_name="Summary", index=False)
    workbook = writer.book
    worksheet = writer.sheets["Summary"]
    worksheet.autofit()

    if cycle_df is not None:
        cycle_df.to_excel(writer, sheet_name="Cycling", index=False)
        worksheet = writer.sheets["Cycling"]
        worksheet.autofit()
    if ratetest_df is not None:
        ratetest_df = ratetest_df.drop("Times seen", axis=1)
        ratetest_df.to_excel(writer, sheet_name="Ratetest", index=False)
        worksheet = writer.sheets["Ratetest"]
        worksheet.autofit()
    if cv_df is not None:
        cv_df = cv_df.loc[:, ["CV Cycle", *[c for c in cv_df.columns if c not in ["CV Cycle"]]]]
        cv_df.to_excel(writer, sheet_name="CV", index=False)
        worksheet = writer.sheets["CV"]
        worksheet.autofit()
    if lsv_df is not None:
        lsv_df.to_excel(writer, sheet_name="LSV", index=False)
        worksheet = writer.sheets["LSV"]
        worksheet.autofit()
    if eis_df is not None:
        eis_df.to_excel(writer, sheet_name="EIS", index=False)
        worksheet = writer.sheets["EIS"]
        worksheet.autofit()

    workbook.close()


def is_sample_folder(folderpath: str | Path, config: FlussliConfig | None = None) -> bool:
    """Determine whether a folder is a sample folder.

    A sample folder contains at least one file matching any configured technique pattern.
    Short-circuits on the first match found.
    """
    folderpath = Path(folderpath)
    if not folderpath.is_dir():
        return False
    patterns = (config if config is not None else FlussliConfig()).all_patterns()
    return any(next(folderpath.glob(p), None) is not None for p in patterns)


def find_all_sample_folders(folder: str | Path, config: FlussliConfig | None = None) -> list[Path]:
    """Find all sample folders in a folder. Search 2 folders deep."""
    folder = Path(folder)
    if config is None:
        config = FlussliConfig.load(folder)
    sample_folders = []
    if is_sample_folder(folder, config):
        return [folder]
    for subfolder in folder.iterdir():
        if not subfolder.is_dir():
            continue
        if is_sample_folder(subfolder, config):
            sample_folders.append(subfolder)
        else:
            for subsubfolder in subfolder.iterdir():
                if not subsubfolder.is_dir():
                    continue
                if is_sample_folder(subsubfolder, config):
                    sample_folders.append(subsubfolder)
    return sample_folders


def find_all_sample_summaries(
    folder: str | Path, config: FlussliConfig | None = None
) -> list[Path]:
    """Collect all summary excels from all subfolders."""
    folder = Path(folder)
    if config is None:
        config = FlussliConfig.load(folder)
    sample_folders = find_all_sample_folders(folder, config)
    return [
        f / "results" / "summary.xlsx"
        for f in sample_folders
        if (f / "results" / "summary.xlsx").exists()
    ]


def merge_summaries(summary_xlsxs: list[str | Path]) -> pd.DataFrame:
    """Merge all summary sheets into one mega summary."""
    summary_xlsxs_paths = [Path(s).resolve() for s in summary_xlsxs]
    names = [
        get_sampleid_from_folderpath(s.parent.parent, FlussliConfig.load(s.parent.parent))
        for s in summary_xlsxs_paths
    ]
    # append a number to duplicate names
    for i, name in enumerate(names):
        if names.count(name) > 1:
            names[i] = f"{name} ({names.count(name)})"
    dfs = [pd.read_excel(s, sheet_name="Summary", index_col=0) for s in summary_xlsxs]
    dfs = [df["Value"] for df in dfs]
    for name, df in zip(names, dfs, strict=True):
        df.name = name
    df = pd.concat(dfs, axis=1)
    vals = df.copy()

    df["Average"] = vals.mean(axis=1)
    df["Std"] = vals.std(axis=1)

    # Make the index a new column
    df["Quantity"] = df.index
    df = df.reset_index(drop=True)

    # put these columns at the start
    cols = ["Quantity", "Average", "Std", *names]
    return df[cols]


def analyse_all_samples(folder: str | Path, *, save_format: SAVE_FORMATS = "parquet") -> None:
    """Take a folder and run all analysis."""
    folder = Path(folder).resolve()
    config = FlussliConfig.load(folder)
    samples = find_all_sample_folders(folder, config)
    if len(samples) == 0:
        logger.error("No sample folders found in %s", folder)
        return
    if len(samples) == 1:
        analyse_sample(samples[0], save_format=save_format, config=config)
    else:
        logger.info("Found %d sample folders:", len(samples))
        for s in samples:
            analyse_sample(s, save_format=save_format, config=config)

        summaries = find_all_sample_summaries(folder, config)
        df = merge_summaries(summaries)
        (folder / "combined_results").mkdir(exist_ok=True)
        writer = pd.ExcelWriter(
            folder / "combined_results" / "combined_summary.xlsx", engine="xlsxwriter"
        )
        df.to_excel(writer, index=False, sheet_name="Summary")
        workbook = writer.book
        worksheet = writer.sheets["Summary"]
        worksheet.autofit()
        workbook.close()
        logger.info("\n🎉 Combined all the results into one big summary")


def dry_analyse_all_samples(folder: str | Path) -> None:
    """Take a folder and tell the user what flussli would do."""
    folder = Path(folder).resolve()
    config = FlussliConfig.load(folder)
    samples = find_all_sample_folders(folder, config)
    if len(samples) == 0:
        logger.error("No sample folders found in %s", folder)
        return
    if len(samples) == 1:
        logger.info("Found one sample inside")
    else:
        logger.info("Found several samples inside.")
    logger.info("I would analyse the following samples and make a 'results' subfolder inside:")
    for s in samples:
        logger.info("  - %s", s)
    if len(samples) > 1:
        logger.info("Then I would combine all the summaries into one 'combined_results' subfolder.")
