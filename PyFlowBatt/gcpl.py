"""Analyse and plot functions for GCPL measurements.

GCPL = Galvanostatic Cycling with Potential Limitation.
i.e. constant-current-constant-voltage cycling.
"""

import logging
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.figure import Figure

from PyFlowBatt.read import read_to_bdf

logger = logging.getLogger(__name__)


def plot(df: pd.DataFrame) -> tuple[Figure, Axes]:
    """Plot time series data."""
    fig, ax = plt.subplots()
    df = df.reset_index()
    ax.plot((df["Unix Time / s"] - df["Unix Time / s"].iloc[0]) / 3600, df["Voltage / V"])
    ax.set_xlabel("Time / h")
    ax.set_ylabel("Voltage / V")
    fig.tight_layout()
    return fig, ax


def analyse(filepaths: str | Path | list[str | Path]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Take a list of GCPL files and return a time-series dataframe, and a per-cycle dataframe."""
    if not isinstance(filepaths, list):
        filepaths = [filepaths]
    dfs = []
    cycle_dfs = []
    total_cycles = 0

    # Read all the files
    dfs = [read_to_bdf(file) for file in filepaths]

    # Reorder based on index
    start_times = [df["Unix Time / s"].iloc[0] for df in dfs]
    order = np.argsort(start_times)
    dfs = [dfs[i] for i in order]

    # Go through each file and extract the data
    for df in dfs:
        if "Cycle Count / 1" not in df.columns:
            logger.warning("Cycle count required in data")
            continue
        df["Total Cycle Count / 1"] = df["Cycle Count / 1"] + total_cycles
        total_cycles = df["Total Cycle Count / 1"].max()

        # Create dataframe with just cycle information
        cycle_df = df.groupby("Cycle Count / 1").first().index.to_frame()
        cycle_df["Total Cycle Count / 1"] = df.groupby("Cycle Count / 1").first()[
            "Total Cycle Count / 1"
        ]

        # Fill that dataframe with per-cycle information
        dt = df["Unix Time / s"].diff().fillna(float("0"))
        df["dq"] = df["Current / A"] * dt / 3.6  # mAh
        for group, group_df in df.groupby("Cycle Count / 1"):
            chg_mask = group_df["dq"] > 0
            dchg_mask = group_df["dq"] < 0
            if chg_mask.sum() > 0 and dchg_mask.sum() > 0:
                charge_capacity = group_df["dq"][chg_mask].sum()
                discharge_capacity = group_df["dq"][dchg_mask].sum()
                charge_energy = (group_df["dq"] * group_df["Voltage / V"])[chg_mask].sum()
                discharge_energy = (group_df["dq"] * group_df["Voltage / V"])[dchg_mask].sum()
                avg_voltage = (group_df["Voltage / V"] * abs(group_df["dq"])).sum() / abs(
                    group_df["dq"]
                ).sum()
                cycle_df.loc[group, "Charge Capacity / mAh"] = charge_capacity
                cycle_df.loc[group, "Discharge Capacity / mAh"] = -discharge_capacity
                cycle_df.loc[group, "Charge Energy / mWh"] = charge_energy
                cycle_df.loc[group, "Discharge Energy / mWh"] = -discharge_energy
                cycle_df.loc[group, "Charge Average Voltage / V"] = charge_energy / charge_capacity
                cycle_df.loc[group, "Discharge Average Voltage / V"] = (
                    discharge_energy / discharge_capacity
                )
                cycle_df.loc[group, "Cycle Average Voltage / V"] = avg_voltage
                cycle_df.loc[group, "Average Current / A"] = group_df["Current / A"].abs().mean()
                cycle_df.loc[group, "Coulombic Efficiency / %"] = (
                    -discharge_capacity / charge_capacity
                ) * 100
                cycle_df.loc[group, "Energy Efficiency / %"] = (
                    -discharge_energy / charge_energy
                ) * 100
                cycle_df.loc[group, "Voltage Efficiency / %"] = (
                    (discharge_energy / discharge_capacity)
                    / (charge_energy / charge_capacity)
                    * 100
                )
            else:
                cycle_df.loc[group, "Cycle Count / 1"] = 0
        cycle_df = cycle_df[cycle_df["Cycle Count / 1"] > 0]
        if cycle_df.empty:
            continue
        # Add to a list of dataframes
        cycle_dfs.append(cycle_df)
    dfs = [df.drop("dq", axis=1) if "dq" in df.columns else df for df in dfs]
    df = pd.concat(dfs)
    cycle_df = pd.concat(cycle_dfs)

    return df, cycle_df


def round_sig(s: pd.Series, sig: int = 2) -> pd.Series:
    """Round to significant figures."""
    return s.apply(lambda x: round(x, sig - int(np.floor(np.log10(abs(x)))) - 1) if x != 0 else 0)


def cycles_to_ratetest(cycle_df: pd.DataFrame) -> pd.DataFrame:
    """Take a ratetest per-cycle dataframe and aggregate by current."""
    rounded = round_sig(cycle_df["Average Current / A"])
    current_groups = (rounded != rounded.shift()).cumsum()
    ratetest_df = cycle_df.groupby(current_groups).agg(
        {
            "Average Current / A": ["mean"],
            "Total Cycle Count / 1": ["first", "last"],
            "Discharge Capacity / mAh": ["mean", "std"],
            "Charge Capacity / mAh": ["mean", "std"],
            "Discharge Energy / mWh": ["mean", "std"],
            "Charge Energy / mWh": ["mean", "std"],
            "Charge Average Voltage / V": ["mean", "std"],
            "Discharge Average Voltage / V": ["mean", "std"],
            "Cycle Average Voltage / V": ["mean", "std"],
            "Coulombic Efficiency / %": ["mean", "std"],
            "Energy Efficiency / %": ["mean", "std"],
            "Voltage Efficiency / %": ["mean", "std"],
        },
    )
    # Rename the index to index
    ratetest_df = ratetest_df.reset_index(drop=True)

    # Create a new column that is 1 if it is the first time a current is seen,
    # 2 if it is the second time, etc.
    ratetest_df["Times seen"] = 0
    times_seen = {}
    for i, current in enumerate(ratetest_df["Average Current / A"]["mean"]):
        if current not in times_seen:
            times_seen[current] = 1
        else:
            times_seen[current] += 1
        ratetest_df.loc[i, "Times seen"] = times_seen[current]
    # Flatten, rename the multi-index columns
    ratetest_df.columns = [" ".join(col).strip() for col in ratetest_df.columns.to_numpy()]
    return ratetest_df.rename(
        columns={
            "Average Current / A mean": "Average Current / A",
            "Total Cycle Count / 1 first": "First cycle",
            "Total Cycle Count / 1 last": "Last cycle",
        },
    )
