"""Analyse and plot functions for LSV measurements.

LSV = Linear Sweep Voltammetry.
"""

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.figure import Figure

from PyFlowBatt.read import read_to_bdf


def _read_lsv(filepath: Path | str) -> pd.DataFrame:
    """Read in LSV file with some sanity checks."""
    # Read file to df
    df = read_to_bdf(filepath)
    # Sanity checks
    if len(df) < 10:
        msg = f"File '{filepath}' has too few data points ({len(df)})."
        raise ValueError(msg)
    if not all(col in df.columns for col in ["Voltage / V", "Current / A"]):
        msg = f"File '{filepath}' does not contain the required columns."
        raise ValueError(msg)
    return df


def analyse(
    filepath: Path | str, area_cm2: float = 5
) -> tuple[pd.DataFrame, dict[str, float | str]]:
    """Extract data from LSV, fit a straight line to get resistance."""
    # Read file to df
    df = _read_lsv(filepath)

    # Fit straight line above cutoff current
    cutoff_current = (df["Current / A"].max() - df["Current / A"].min()) / 2
    mask = df["Current / A"] > cutoff_current
    x = df["Voltage / V"][mask]
    y = df["Current / A"][mask]
    slope, intercept = np.polyfit(x, y, 1)
    resistance_ohm = 1 / slope
    specific_resistance_ohm_cm2 = resistance_ohm * area_cm2
    results = {
        "Fit cutoff current / A": cutoff_current,
        "Intercept / A": intercept,
        "Slope / Ω⁻¹": slope,
        "Resistance / Ω": resistance_ohm,
        "Area / cm²": area_cm2,
        "Area specific resistance / Ω cm²": specific_resistance_ohm_cm2,
        "File name": Path(filepath).name,
    }
    return df, results


def plot(df: pd.DataFrame, results: dict[str, float | str]) -> tuple[Figure, Axes]:
    """Take LSV data df and results dict and plot."""
    # Plot the data
    fig, ax = plt.subplots()
    ax.plot(df["Voltage / V"], df["Current / A"], label="Data")
    ax.set_xlabel("Voltage / V")
    ax.set_ylabel("Current / A")

    # Plot the fit
    mask = df["Current / A"] > results["Fit cutoff current / A"]
    x = np.linspace(df["Voltage / V"][mask].min(), df["Voltage / V"][mask].max(), 10)
    y = results["Intercept / A"] + results["Slope / Ω⁻¹"] * x
    ax.plot(x, y, "k-", label="Fit")
    ax.legend()
    fig.tight_layout()

    return fig, ax
