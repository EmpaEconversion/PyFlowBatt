"""Analyse and plot functions for CV/CVA measurements.

CV = Cyclic Voltammetry
CVA = Cyclic Voltammetry Advanced (EC-lab technique)
"""

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from flussli.read import read_to_bdf


def _get_capacitance_from_cv(cv_df: pd.DataFrame) -> tuple[float, float]:
    """Fit a straight line to get the capacitance from cyclic voltammetry."""
    # Round scan rate to 2 sig figs to group
    power = 10 ** np.floor(np.log10(np.abs(cv_df["Scan rate / V s⁻¹"])))
    cv_df["Scan rate rounded / V s⁻¹"] = np.round(cv_df["Scan rate / V s⁻¹"] / power, 1) * power

    # Make mask of largest cycle in each scan rate rounded group
    mask = (
        cv_df.groupby("Scan rate rounded / V s⁻¹")["CV Cycle"].transform("max") == cv_df["CV Cycle"]
    )

    # Do a linear fit of the Idiff/2 to get the capacitance
    # dy/dx is capacitance in mA s / V = mC / V = mF
    capacitance_mF, intercept_mA = np.polyfit(
        cv_df[mask]["Scan rate rounded / V s⁻¹"], cv_df[mask]["∆I / A"] / 2, 1
    )

    return capacitance_mF, intercept_mA


def analyse(
    filepath: Path | str,
    v_min: float = 0.4004,
    v_max: float = 0.6,
    v_med: float = 0.5,
    v_range: float = 0.02,
) -> tuple[pd.DataFrame, pd.DataFrame, float]:
    """Analyse cyclic voltammetry data, find capacitance from scan-rate vs current difference."""
    df = read_to_bdf(filepath)

    results = []
    cycle = 1
    df["Scan rate / V s⁻¹"] = 0.0
    full_mask = np.zeros_like(df["Voltage / V"], dtype=bool)
    for group, _gdf in df.groupby("Cycle Count / 1"):
        mask = (
            (df["Cycle Count / 1"] == group)
            & (df["Voltage / V"] > v_min)
            & (df["Voltage / V"] < v_max)
        )
        full_mask = full_mask | mask
        if sum(mask) < 10:
            continue  # Not enough points

        # Add scan rate to the original df
        df.loc[mask, "Scan rate / V s⁻¹"] = np.nan_to_num(
            df["Voltage / V"][mask].diff() / df["Unix Time / s"][mask].diff()
        )
        gdf = df[mask]

        # Extract some values
        upward_scan_current = np.mean(
            gdf["Current / A"][
                (gdf["Scan rate / V s⁻¹"] > 0)
                & (gdf["Voltage / V"] > v_med - v_range)
                & (gdf["Voltage / V"] < v_med + v_range)
            ]
        )
        downward_scan_current = np.mean(
            gdf["Current / A"][
                (gdf["Scan rate / V s⁻¹"] < 0)
                & (gdf["Voltage / V"] > v_med - v_range)
                & (gdf["Voltage / V"] < v_med + v_range)
            ]
        )
        current_diff = upward_scan_current - downward_scan_current
        scan_rate = np.median(abs(gdf["Scan rate / V s⁻¹"]))

        results.append(
            {
                "I upsweep / A": upward_scan_current,
                "I downsweep / A": downward_scan_current,
                "∆I / A": current_diff,
                "Scan rate / V s⁻¹": scan_rate,
                "CV Cycle": cycle,
            }
        )
        cycle = cycle + 1

    cv_df = pd.DataFrame(results)
    capacitance_F, _intercept_mA = _get_capacitance_from_cv(cv_df)
    capacitance_mF = capacitance_F * 1000

    # Only take data after the first step
    full_mask = (df["Step Count / 1"] >= 2) & full_mask

    return df[full_mask], cv_df, capacitance_mF


def plot(df: pd.DataFrame, cv_df: pd.DataFrame) -> tuple:
    """Plot cyclic voltammetry data."""
    fig, ax = plt.subplots(ncols=2)
    ax[0].plot(df["Voltage / V"], df["Current / A"])
    ax[0].set_xlabel("Voltage / V")
    ax[0].set_ylabel("Current / A")

    ax[1].plot(cv_df["Scan rate / V s⁻¹"], cv_df["∆I / A"] / 2, "o", label="Data")
    ax[1].set_xlabel("Scan rate / V s⁻¹")
    ax[1].set_ylabel("∆I/2 / A")

    m, c = _get_capacitance_from_cv(cv_df)
    ax[1].plot(cv_df["Scan rate / V s⁻¹"], m * cv_df["Scan rate / V s⁻¹"] + c, "k-", label="Fit")
    ax[1].legend()
    fig.tight_layout()
    return fig, ax
