"""Analyse and plot functions for CV/CVA measurements.

CV = Cyclic Voltammetry
CVA = Cyclic Voltammetry Advanced (EC-lab technique)
"""

import logging
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.figure import Figure

from flussli.read import read_to_bdf

logger = logging.getLogger(__name__)

MIN_R2 = 0.8


def _get_capacitance_from_cv(cv_df: pd.DataFrame) -> tuple[float, float, float]:
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
    x = cv_df[mask]["Scan rate rounded / V s⁻¹"]
    y = cv_df[mask]["∆I / A"] / 2
    capacitance_mF, intercept_mA = np.polyfit(x, y, 1)

    # R-squared sanity check
    y_pred = capacitance_mF * x + intercept_mA
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - y.mean()) ** 2)
    r2 = 1 - ss_res / ss_tot

    return capacitance_mF, intercept_mA, r2


def analyse(
    filepath: Path | str,
    v_min: float = 0.4004,
    v_max: float = 0.6,
    v_med: float = 0.5,
    v_range: float = 0.02,
) -> tuple[pd.DataFrame, pd.DataFrame, float | None]:
    """Analyse cyclic voltammetry data, find capacitance from scan-rate vs current difference."""
    df = read_to_bdf(filepath)

    results = []
    cycle = 1
    df["Scan rate / V s⁻¹"] = 0.0
    df["CV Cycle / 1"] = 0
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
        upscan_mask = (
            (gdf["Scan rate / V s⁻¹"] > 0)
            & (gdf["Voltage / V"] > v_med - v_range)
            & (gdf["Voltage / V"] < v_med + v_range)
        )
        upward_scan_current = np.mean(gdf["Current / A"][upscan_mask])
        downscan_mask = (
            (gdf["Scan rate / V s⁻¹"] < 0)
            & (gdf["Voltage / V"] > v_med - v_range)
            & (gdf["Voltage / V"] < v_med + v_range)
        )
        downward_scan_current = np.mean(gdf["Current / A"][downscan_mask])
        current_diff = upward_scan_current - downward_scan_current
        scan_rate = np.median(abs(gdf["Scan rate / V s⁻¹"][upscan_mask | downscan_mask]))
        if not np.isnan(upward_scan_current) and not np.isnan(downward_scan_current):
            results.append(
                {
                    "I upsweep / A": upward_scan_current,
                    "I downsweep / A": downward_scan_current,
                    "∆I / A": current_diff,
                    "Scan rate / V s⁻¹": scan_rate,
                    "CV Cycle": cycle,
                }
            )
            df.loc[mask, "CV Cycle / 1"] = cycle
            cycle = cycle + 1

    cv_df = pd.DataFrame(results)
    capacitance_F, _intercept_mA, r2 = _get_capacitance_from_cv(cv_df)
    capacitance_mF = capacitance_F * 1000

    # Only take data after the first step
    full_mask = (df["Step Count / 1"] >= 2) & full_mask

    if r2 < MIN_R2:
        logger.warning(
            "CV fit R² value is very low (%.3f<%s), not reporting capacitance", r2, MIN_R2
        )
        return df[full_mask], cv_df, None
    return df[full_mask], cv_df, capacitance_mF


def plot(df: pd.DataFrame, cv_df: pd.DataFrame) -> tuple[Figure, Axes]:
    """Plot cyclic voltammetry data."""
    fig, ax = plt.subplots(ncols=2)
    min_sweep = min(cv_df["Scan rate / V s⁻¹"])
    max_sweep = max(cv_df["Scan rate / V s⁻¹"])
    for group, group_df in df.groupby("CV Cycle / 1"):
        if group > 0:
            rate = cv_df["Scan rate rounded / V s⁻¹"][cv_df["CV Cycle"] == group].iloc[0]
            color_val = (rate - min_sweep) / (max_sweep - min_sweep)
            ax[0].plot(
                group_df["Voltage / V"],
                group_df["Current / A"],
                color=plt.cm.turbo(color_val),
            )
    ax[0].set_xlabel("Voltage / V")
    ax[0].set_ylabel("Current / A")
    lo, hi = np.percentile(df["Current / A"], [0.02, 99.98])
    pad = (hi - lo) * 0.05
    ax[0].set_ylim(lo - pad, hi + pad)

    for i, row in cv_df.iterrows():
        color_val = (row["Scan rate / V s⁻¹"] - min_sweep) / (max_sweep - min_sweep)
        ax[1].plot(
            row["Scan rate / V s⁻¹"],
            row["∆I / A"] / 2,
            "o",
            color=plt.cm.turbo(color_val),
            label="Data" if i == 1 else None,
        )
    ax[1].set_xlabel("Scan rate / V s⁻¹")
    ax[1].set_ylabel("∆I/2 / A")

    m, c, r2 = _get_capacitance_from_cv(cv_df)
    if r2 > MIN_R2:
        label = "Fit"
        style = "k-"
        r2_text = f"R² = {r2:.3f}"
    else:
        label = "Fit (bad)"
        style = "k--"
        r2_text = f"R² = {r2:.3f}\n(<0.8, result ignored)"
    ax[1].plot(cv_df["Scan rate / V s⁻¹"], m * cv_df["Scan rate / V s⁻¹"] + c, style, label=label)
    ax[1].legend()
    ax[1].text(0.95, 0.05, r2_text, transform=ax[1].transAxes, ha="right", va="bottom")
    fig.tight_layout()
    return fig, ax
