"""Utility functions for analysing flow battery data from MPR files."""

import logging
import re
import warnings
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yadg

logger = logging.getLogger(__name__)


def analyse_gcpls(filepaths: str | Path | list[str | Path]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Take a list of GCPL files and return a time-series dataframe, and a per-cycle dataframe."""
    if not isinstance(filepaths, list):
        filepaths = [filepaths]
    dfs = []
    cycle_dfs = []
    total_cycles = 0

    # Read all the files
    for file in filepaths:
        with warnings.catch_warnings(record=True) as _w:
            data = yadg.extractors.extract("eclab.mpr", file)
            dfs.append(data.to_dataset().to_dataframe())

    # Reorder based on index
    start_times = [df.index[0] for df in dfs]
    order = np.argsort(start_times)
    dfs = [dfs[i] for i in order]

    # Go through each file and extract the data
    for df in dfs:
        if "half cycle" in df.columns:  # It is some cycling data
            # Have to do some duct taping
            # EC-labs 'cycles' and 'Q charge or discharge' are wrong
            df["dumb cycle"] = (df["ox or red"].diff() > 0).cumsum()
            df["Cycle"] = 0
            cycle = 1
            for _group, group_df in df.groupby("dumb cycle"):
                chg_mask = group_df["dq"] > 0
                dchg_mask = group_df["dq"] < 0
                if (
                    sum(group_df["dq"][chg_mask]) > 0
                    and sum(group_df["dq"][dchg_mask]) < 0
                    and sum(chg_mask) > 5
                    and sum(dchg_mask) > 5
                ):
                    df.loc[group_df.index, "Cycle"] = cycle
                    cycle += 1
            df["Total cycle"] = df["Cycle"] + total_cycles
            total_cycles = df["Total cycle"].max()

            # Create dataframe with just cycle information
            cycle_df = df.groupby("Cycle").first().index.to_frame()
            cycle_df["Total cycle"] = df.groupby("Cycle").first()["Total cycle"]

            # Fill that dataframe with per-cycle information
            for group, group_df in df.groupby("Cycle"):
                chg_mask = group_df["dq"] > 0
                dchg_mask = group_df["dq"] < 0
                if chg_mask.sum() > 0 and dchg_mask.sum() > 0:
                    charge_capacity = group_df["dq"][chg_mask].sum()
                    discharge_capacity = group_df["dq"][dchg_mask].sum()
                    charge_energy = (group_df["dq"] * group_df["Ewe"])[chg_mask].sum()
                    discharge_energy = (group_df["dq"] * group_df["Ewe"])[dchg_mask].sum()
                    avg_voltage = (group_df["Ewe"] * abs(group_df["dq"])).sum() / abs(
                        group_df["dq"]
                    ).sum()
                    cycle_df.loc[group, "Charge capacity (mAh)"] = charge_capacity
                    cycle_df.loc[group, "Discharge capacity (mAh)"] = -discharge_capacity
                    cycle_df.loc[group, "Charge energy (mWh)"] = charge_energy
                    cycle_df.loc[group, "Discharge energy (mWh)"] = -discharge_energy
                    cycle_df.loc[group, "Charge average voltage (V)"] = (
                        charge_energy / charge_capacity
                    )
                    cycle_df.loc[group, "Discharge average voltage (V)"] = (
                        discharge_energy / discharge_capacity
                    )
                    cycle_df.loc[group, "Cycle average voltage (V)"] = avg_voltage
                    cycle_df.loc[group, "Avg current (mA)"] = group_df["control_I"].abs().mean()
                    cycle_df.loc[group, "Coulombic efficiency (%)"] = (
                        -discharge_capacity / charge_capacity
                    ) * 100
                    cycle_df.loc[group, "Energy efficiency (%)"] = (
                        -discharge_energy / charge_energy
                    ) * 100
                    cycle_df.loc[group, "Voltage efficiency (%)"] = (
                        (discharge_energy / discharge_capacity)
                        / (charge_energy / charge_capacity)
                        * 100
                    )
                else:
                    cycle_df.loc[group, "Cycle"] = 0
            cycle_df = cycle_df[cycle_df["Cycle"] > 0]
            if cycle_df.empty:
                continue
            # Add to a list of dataframes
            cycle_dfs.append(cycle_df)
    dfs = [df.drop("dumb cycle", axis=1) if "dumb cycle" in df.columns else df for df in dfs]
    df = pd.concat(dfs)
    cycle_df = pd.concat(cycle_dfs)

    return df, cycle_df


def cycles_to_ratetest(cycle_df: pd.DataFrame) -> pd.DataFrame:
    """Take a ratetest per-cycle dataframe and aggregate by current."""
    current_groups = (cycle_df["Avg current (mA)"] != cycle_df["Avg current (mA)"].shift()).cumsum()
    ratetest_df = cycle_df.groupby(current_groups).agg(
        {
            "Avg current (mA)": ["mean"],
            "Total cycle": ["first", "last"],
            "Discharge capacity (mAh)": ["mean", "std"],
            "Charge capacity (mAh)": ["mean", "std"],
            "Discharge energy (mWh)": ["mean", "std"],
            "Charge energy (mWh)": ["mean", "std"],
            "Charge average voltage (V)": ["mean", "std"],
            "Discharge average voltage (V)": ["mean", "std"],
            "Cycle average voltage (V)": ["mean", "std"],
            "Coulombic efficiency (%)": ["mean", "std"],
            "Energy efficiency (%)": ["mean", "std"],
            "Voltage efficiency (%)": ["mean", "std"],
        },
    )
    # Rename the index to index
    ratetest_df = ratetest_df.reset_index(drop=True)

    # Create a new column that is 1 if it is the first time a current is seen,
    # 2 if it is the second time, etc.
    ratetest_df["Times seen"] = 0
    times_seen = {}
    for i, current in enumerate(ratetest_df["Avg current (mA)"]["mean"]):
        if current not in times_seen:
            times_seen[current] = 1
        else:
            times_seen[current] += 1
        ratetest_df.loc[i, "Times seen"] = times_seen[current]
    # # flatten the multi-index columns
    ratetest_df.columns = [" ".join(col).strip() for col in ratetest_df.columns.to_numpy()]
    # rename "Avg current (mA) mean" to "Avg current (mA)"
    return ratetest_df.rename(
        columns={
            "Avg current (mA) mean": "Avg current (mA)",
            "Total cycle first": "First cycle",
            "Total cycle last": "Last cycle",
        },
    )


def read_lsv(filepath: Path | str) -> pd.DataFrame:
    """Read in LSV file with some sanity checks."""
    # Read file to df
    filepath = Path(filepath)
    with warnings.catch_warnings(record=True) as _w:
        data = yadg.extractors.extract("eclab.mpr", filepath)
    df = data.to_dataset().to_dataframe()

    # Sanity checks
    if len(df) < 10:
        msg = f"File '{filepath}' has too few data points ({len(df)})."
        raise ValueError(msg)
    if not all(col in df.columns for col in ["Ewe", "<I>"]):
        msg = f"File '{filepath}' does not contain the required columns."
        raise ValueError(msg)
    return df


def analyse_lsv(filepath: Path | str) -> tuple[pd.DataFrame, dict[str, float | str]]:
    """Extract data from LSV, fit line and return resistance."""
    # Read file to df
    df = read_lsv(filepath)

    # Fit straight line above cutoff current
    cutoff_current = (df["<I>"].max() - df["<I>"].min()) / 2
    mask = df["<I>"] > cutoff_current
    x = df["Ewe"][mask]
    y = df["<I>"][mask]
    slope, intercept = np.polyfit(x, y, 1)
    resistance_ohm = 1e3 / slope
    area_cm2 = 5
    specific_resistance_ohm_cm2 = resistance_ohm * area_cm2
    results = {
        "Fit cutoff current (mA)": cutoff_current,
        "Intercept (mA)": intercept,
        "Slope (mΩ⁻¹)": slope,
        "Resistance (Ω)": resistance_ohm,
        "Area (cm²)": area_cm2,
        "Area specific resistance (Ω cm²)": specific_resistance_ohm_cm2,
        "File name": Path(filepath).name,
    }
    return df, results


def plot_lsv(df: pd.DataFrame, results: dict[str, float | str]) -> tuple:
    """Take LSV data df and results dict and plot."""
    # Plot the data
    fig, ax = plt.subplots()
    ax.plot(df["Ewe"], df["<I>"], label="Data")
    ax.set_xlabel("Voltage (V)")
    ax.set_ylabel("Current (mA)")

    # Plot the fit
    mask = df["<I>"] > results["Fit cutoff current (mA)"]
    x = np.linspace(df["Ewe"][mask].min(), df["Ewe"][mask].max(), 10)
    y = results["Intercept (mA)"] + results["Slope (mΩ⁻¹)"] * x
    ax.plot(x, y, "k-", label="Fit")
    ax.legend()
    fig.tight_layout()

    return fig, ax


def get_cva_capacitance(
    filepath: Path | str,
    v_min: float = 0.4004,
    v_max: float = 0.6,
    v_med: float = 0.5,
    v_range: float = 0.02,
) -> tuple[pd.DataFrame, pd.DataFrame, float]:
    """Analyse cyclic voltammetry data, find capacitance from scan-rate vs current difference."""
    # Read file to df
    filepath = Path(filepath)
    with warnings.catch_warnings(record=True) as _w:
        df = (
            yadg.extractors.extract(filetype="eclab.mpr", path=filepath)
            .to_dataset()
            .to_dataframe()
            .reset_index()
        )

    results = []
    cycle = 1
    df["Scan rate (V/s)"] = 0.0
    full_mask = np.zeros_like(df["Ewe"], dtype=bool)
    for group, _gdf in df.groupby("cycle number"):
        mask = (df["cycle number"] == group) & (df["Ewe"] > v_min) & (df["Ewe"] < v_max)
        full_mask = full_mask | mask
        if sum(mask) < 10:
            continue  # Not enough points

        # Add scan rate to the original df
        df.loc[mask, "Scan rate (V/s)"] = np.nan_to_num(
            df["control_V"][mask].diff() / df["uts"][mask].diff()
        )
        gdf = df[mask]

        # Extract some values
        upward_scan_current = np.mean(
            gdf["<I>"][
                (gdf["Scan rate (V/s)"] > 0)
                & (gdf["Ewe"] > v_med - v_range)
                & (gdf["Ewe"] < v_med + v_range)
            ]
        )
        downward_scan_current = np.mean(
            gdf["<I>"][
                (gdf["Scan rate (V/s)"] < 0)
                & (gdf["Ewe"] > v_med - v_range)
                & (gdf["Ewe"] < v_med + v_range)
            ]
        )
        current_diff = upward_scan_current - downward_scan_current
        scan_rate = np.median(abs(gdf["Scan rate (V/s)"]))

        results.append(
            {
                "I upsweep (mA)": upward_scan_current,
                "I downsweep (mA)": downward_scan_current,
                "∆I (mA)": current_diff,
                "Scan rate (V/s)": scan_rate,
                "CV Cycle": cycle,
            }
        )
        cycle = cycle + 1

    cva_df = pd.DataFrame(results)
    capacitance_mF, _intercept_mA = get_capacitance_from_cva(cva_df)

    # Only take data after the first Ns changes
    full_mask = (df["Ns changes"].cumsum() > 0) & full_mask

    return df[full_mask], cva_df, capacitance_mF


def get_capacitance_from_cva(cva_df: pd.DataFrame) -> tuple[float, float]:
    """Fit a straight line to get the capacitance from cyclic voltammetry."""
    # Round scan rate to 2 sig figs to group
    power = 10 ** np.floor(np.log10(np.abs(cva_df["Scan rate (V/s)"])))
    cva_df["Scan rate rounded (V/s)"] = np.round(cva_df["Scan rate (V/s)"] / power, 1) * power

    # Make mask of largest cycle in each scan rate rounded group
    mask = (
        cva_df.groupby("Scan rate rounded (V/s)")["CV Cycle"].transform("max") == cva_df["CV Cycle"]
    )

    # Do a linear fit of the Idiff/2 to get the capacitance
    # dy/dx is capacitance in mA s / V = mC / V = mF
    capacitance_mF, intercept_mA = np.polyfit(
        cva_df[mask]["Scan rate rounded (V/s)"], cva_df[mask]["∆I (mA)"] / 2, 1
    )

    return capacitance_mF, intercept_mA


def plot_time_series(df: pd.DataFrame) -> tuple:
    """Plot time series data."""
    fig, ax = plt.subplots()
    df = df.reset_index()
    ax.plot((df["uts"] - df["uts"].iloc[0]) / 3600, df["Ewe"])
    ax.set_xlabel("Time (h)")
    ax.set_ylabel("Voltage (V)")
    fig.tight_layout()
    return fig, ax


def plot_cva_capacitance(df: pd.DataFrame, cva_df: pd.DataFrame) -> tuple:
    """Plot cyclic voltammetry data."""
    fig, ax = plt.subplots(ncols=2)
    ax[0].plot(df["Ewe"], df["<I>"])
    ax[0].set_xlabel("Voltage (V)")
    ax[0].set_ylabel("Current (mA)")

    ax[1].plot(cva_df["Scan rate (V/s)"], cva_df["∆I (mA)"] / 2, "o", label="Data")
    ax[1].set_xlabel("Scan rate (V/s)")
    ax[1].set_ylabel("∆I/2 (mA)")

    m, c = get_capacitance_from_cva(cva_df)
    ax[1].plot(cva_df["Scan rate (V/s)"], m * cva_df["Scan rate (V/s)"] + c, "k-", label="Fit")
    ax[1].legend()
    fig.tight_layout()
    return fig, ax


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


def get_sampleid_from_folderpath(folderpath: str | Path) -> str:
    """Get the sample ID given a folder to a sample.

    Usually it is just the folder name, sometimes the parent.
    """
    folderpath = Path(folderpath)
    pattern = r"^\d+_.+_.+$"
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


def get_average_ocv(mpr_file: str | Path) -> tuple[float, float]:
    """Get the average OCV from an MPR OCV file. Returns mean and std."""
    df = (
        yadg.extractors.extract(filetype="eclab.mpr", path=mpr_file)
        .to_dataset()
        .to_dataframe()
        .reset_index()
    )
    voltage_col = next((c for c in ["Ewe", "<Ewe>"] if c in df), None)
    if voltage_col:
        return float(df[voltage_col].mean()), float(df[voltage_col].std())
    msg = f"Could not find voltage column in {mpr_file}."
    raise ValueError(msg)


def analyse_sample(folder: str | Path) -> None:
    """Read all the files in a folder, analayse and plot everything."""
    folder = Path(folder)

    logger.info("\n🌊 Flussli-ing %s", folder.name)
    gcpl_files = list(folder.glob("*_GCPL_*.mpr"))
    gcpl_file = max(gcpl_files, key=lambda x: x.stat().st_size) if gcpl_files else None
    ocv_files = list(folder.glob("*_OCV_*.mpr"))
    lsv_files = list(folder.glob("*_LSV_*.mpr"))
    cva_files_before = list(folder.glob("*_CVApre*.mpr"))
    cva_files_after = list(folder.glob("*_CVApost*.mpr"))
    logger.debug("Reading GCPL: %s", ", ".join([f.stem for f in gcpl_files]))
    logger.debug("Reading LSV: %s", ", ".join([f.stem for f in lsv_files]))
    logger.debug("Reading CVA before: %s", ", ".join([f.stem for f in cva_files_before]))
    logger.debug("Reading CVA after:  %s", ", ".join([f.stem for f in cva_files_after]))

    cycle_df = None
    cva_df = None
    lsv_df = None
    ratetest_df = None
    (folder / "results").mkdir(exist_ok=True)

    logger.info("⛓️‍💥 Analysing OCV")
    if len(ocv_files) == 0:
        logger.info("- ☹️ No OCV found, skipping")
        ocv = (np.nan, np.nan)
    else:
        if len(ocv_files) > 1:
            logger.warning("- More than one OCV file, only reading %s", ocv_files[0].stem)
        ocv = get_average_ocv(ocv_files[0])

    logger.info("🔋 Analysing GCPL")
    if gcpl_file is None:
        logger.warning("- ☹️ No GCPL files found, skipping")
    else:
        df, cycle_df = analyse_gcpls([gcpl_file])
        fig, _ax = plot_time_series(df)
        fig.savefig(folder / "results" / "gcpl.png")
        plt.close()
        ratetest_df = cycles_to_ratetest(cycle_df)
        df.to_parquet(folder / "results" / "gcpl_data.parquet")

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
            df, results = analyse_lsv(lsv_files[0])
            fig, _ax = plot_lsv(df, results)
            fig.savefig(folder / "results" / f"lsv_{p}.png")
            plt.close(fig)
            df.to_parquet(folder / "results" / f"lsv_{p}_data.parquet")
            lsv_res[p] = float(results["Area specific resistance (Ω cm²)"])

            results = {"Pre or post cycle": p, **results}
            lsv_df = pd.DataFrame([results])
        else:
            df, results_pre = analyse_lsv(lsv_files[0])
            df.to_parquet(folder / "results" / "lsv_pre_data.parquet")
            fig, _ax = plot_lsv(df, results_pre)
            fig.savefig(folder / "results" / "lsv_pre.png")
            plt.close(fig)
            lsv_res["pre"] = float(results_pre["Area specific resistance (Ω cm²)"])

            df, results_post = analyse_lsv(lsv_files[-1])
            df.to_parquet(folder / "results" / "lsv_post_data.parquet")
            fig, _ax = plot_lsv(df, results_post)
            fig.savefig(folder / "results" / "lsv_post.png")
            plt.close(fig)
            lsv_res["post"] = float(results_post["Area specific resistance (Ω cm²)"])

            lsv_df = pd.DataFrame(
                [
                    {"Pre or post cycle": "pre", **results_pre},
                    {"Pre or post cycle": "post", **results_post},
                ]
            )

    logger.info("🚴 Analysing CVA")
    cva_res = {"pre": np.nan, "post": np.nan}
    if len(cva_files_before) == 0 and len(cva_files_after) == 0:
        logger.warning("- ☹️ No CVA files found, skipping")
    else:
        for p, cva_files in [("pre", cva_files_before), ("post", cva_files_after)]:
            if not cva_files:
                continue
            if len(cva_files) > 1:
                logger.warning("More than one CVA file, only reading %s", cva_files[0].stem)
            df, cva_df, capacitance_mF = get_cva_capacitance(cva_files[0])
            df.to_parquet(folder / "results" / f"cva_{p}_data.parquet")
            fig, _ax = plot_cva_capacitance(df, cva_df)
            fig.savefig(folder / "results" / f"cva_{p}.png")
            plt.close(fig)
            cva_res[p] = capacitance_mF

    logger.info("💪 Making sample summary")

    # Create keys
    cols = [
        "Av. OCV (V)",
        "ASR pre (Ω cm²)",
        "ASR post (Ω cm²)",
        "∆ASR (Ω cm²)",
        "F pre (mF)",
        "F post (mF)",
        "∆F (mF)",
        "Assembled resistance (Ω)",
        "1st CE (%)",
        "1st EE (%)",
        "1st VE (%)",
    ]
    # n cycles to include in the summary file
    n_cycles = [10, 20, 30, 40, 50]
    for n in n_cycles:
        cols.extend(
            [
                f"{n} cycles avg. CE (%)",
                f"{n} cycles avg. EE (%)",
                f"{n} cycles avg. VE (%)",
                f"{n} cycles avg. charge capacity (mAh)",
                f"{n} cycles avg. discharge capacity (mAh)",
            ]
        )
    summary = {col: {"Value": np.nan, "Error": np.nan} for col in cols}

    # Now fill in values
    summary["Av. OCV (V)"]["Value"] = ocv[0]
    summary["Av. OCV (V)"]["Error"] = ocv[1]
    summary["ASR pre (Ω cm²)"]["Value"] = lsv_res["pre"]
    summary["ASR post (Ω cm²)"]["Value"] = lsv_res["post"]
    summary["∆ASR (Ω cm²)"]["Value"] = lsv_res["post"] - lsv_res["pre"]
    summary["F pre (mF)"]["Value"] = cva_res["pre"]
    summary["F post (mF)"]["Value"] = cva_res["post"]
    summary["∆F (mF)"]["Value"] = cva_res["post"] - cva_res["pre"]
    summary["Assembled resistance (Ω)"]["Value"] = get_res_from_filename(gcpl_files[0].stem)

    if cycle_df is not None:
        mask = cycle_df["Total cycle"] == 1
        summary["1st CE (%)"]["Value"] = float(
            cycle_df.loc[mask, "Coulombic efficiency (%)"].to_numpy()[0]
        )
        summary["1st EE (%)"]["Value"] = float(
            cycle_df.loc[mask, "Energy efficiency (%)"].to_numpy()[0]
        )
        summary["1st VE (%)"]["Value"] = float(
            cycle_df.loc[mask, "Voltage efficiency (%)"].to_numpy()[0]
        )

        max_cycles = cycle_df["Total cycle"].max()
        for n in n_cycles:
            if n <= max_cycles:
                mask = cycle_df["Total cycle"] <= n
                summary[f"{n} cycles avg. CE (%)"]["Value"] = float(
                    cycle_df.loc[mask, "Coulombic efficiency (%)"].mean()
                )
                summary[f"{n} cycles avg. CE (%)"]["Error"] = float(
                    cycle_df.loc[mask, "Coulombic efficiency (%)"].std()
                )
                summary[f"{n} cycles avg. EE (%)"]["Value"] = float(
                    cycle_df.loc[mask, "Energy efficiency (%)"].mean()
                )
                summary[f"{n} cycles avg. EE (%)"]["Error"] = float(
                    cycle_df.loc[mask, "Energy efficiency (%)"].std()
                )
                summary[f"{n} cycles avg. VE (%)"]["Value"] = float(
                    cycle_df.loc[mask, "Voltage efficiency (%)"].mean()
                )
                summary[f"{n} cycles avg. VE (%)"]["Error"] = float(
                    cycle_df.loc[mask, "Voltage efficiency (%)"].std()
                )
                summary[f"{n} cycles avg. charge capacity (mAh)"]["Value"] = float(
                    cycle_df.loc[mask, "Charge capacity (mAh)"].mean()
                )
                summary[f"{n} cycles avg. charge capacity (mAh)"]["Error"] = float(
                    cycle_df.loc[mask, "Charge capacity (mAh)"].std()
                )
                summary[f"{n} cycles avg. discharge capacity (mAh)"]["Value"] = float(
                    cycle_df.loc[mask, "Discharge capacity (mAh)"].mean()
                )
                summary[f"{n} cycles avg. discharge capacity (mAh)"]["Error"] = float(
                    cycle_df.loc[mask, "Discharge capacity (mAh)"].std()
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
    if cva_df is not None:
        cva_df = cva_df.loc[:, ["CV Cycle", *[c for c in cva_df.columns if c not in ["CV Cycle"]]]]
        cva_df.to_excel(writer, sheet_name="CVA", index=False)
        worksheet = writer.sheets["CVA"]
        worksheet.autofit()
    if lsv_df is not None:
        lsv_df.to_excel(writer, sheet_name="LSV", index=False)
        worksheet = writer.sheets["LSV"]
        worksheet.autofit()

    workbook.close()


def is_sample_folder(folderpath: str | Path) -> bool:
    """Determine whether a folder is a sample folder. A sample folder contains at least 1 mpr file."""
    folderpath = Path(folderpath)
    if not folderpath.is_dir():
        return False
    return bool(list(folderpath.glob("*.mpr")))


def find_all_sample_folders(folder: str | Path) -> list[Path]:
    """Find all sample folders in a folder. Search 2 folders deep."""
    folder = Path(folder)
    sample_folders = []
    if is_sample_folder(folder):  # It's just a sample folder
        return [folder]
    for subfolder in folder.iterdir():
        if not subfolder.is_dir():
            continue
        if is_sample_folder(subfolder):
            sample_folders.append(subfolder)
        else:
            for subsubfolder in subfolder.iterdir():
                if not subsubfolder.is_dir():
                    continue
                if is_sample_folder(subsubfolder):
                    sample_folders.append(subsubfolder)
    return sample_folders


def find_all_sample_summaries(folder: str | Path) -> list[Path]:
    """Collect all summary excels from all subfolders."""
    folder = Path(folder)
    sample_folders = find_all_sample_folders(folder)
    return [
        f / "results" / "summary.xlsx"
        for f in sample_folders
        if (f / "results" / "summary.xlsx").exists()
    ]


def merge_summaries(summary_xlsxs: list[str | Path]) -> pd.DataFrame:
    """Merge all summary sheets into one mega summary."""
    summary_xlsxs_paths = [Path(s).resolve() for s in summary_xlsxs]
    names = [get_sampleid_from_folderpath(s.parent.parent) for s in summary_xlsxs_paths]
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


def analyse_all_samples(folder: str | Path) -> None:
    """Take a folder and run all analysis."""
    folder = Path(folder).resolve()
    samples = find_all_sample_folders(folder)
    if len(samples) == 0:
        logger.error("No sample folders found in %s", folder)
        return
    if len(samples) == 1:
        analyse_sample(samples[0])
    else:
        logger.info("Found %d sample folders:", len(samples))
        for s in samples:
            analyse_sample(s)

        summaries = find_all_sample_summaries(folder)
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
    samples = find_all_sample_folders(folder)
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
