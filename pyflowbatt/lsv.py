"""Analyse and plot functions for LSV measurements.

LSV = Linear Sweep Voltammetry.
"""

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.figure import Figure


def _check_lsv(df: pd.DataFrame) -> pd.DataFrame:
    """Sanity check LSV df."""
    # Sanity checks
    if len(df) < 10:
        msg = f"LSV dataframe has too few data points ({len(df)})."
        raise ValueError(msg)
    if not all(col in df.columns for col in ["Voltage / V", "Current / A"]):
        msg = "LSV dataframe does not contain the required columns."
        raise ValueError(msg)
    return df


def analyse(df: pd.DataFrame, area_cm2: float = 5) -> dict[str, float | str]:
    """Extract data from LSV, fit a straight line to get resistance."""
    # Read file to df
    df = _check_lsv(df)

    # Fit straight line above cutoff current
    cutoff_current = (df["Current / A"].max() - df["Current / A"].min()) / 2
    mask = df["Current / A"] > cutoff_current
    x = df["Voltage / V"][mask]
    y = df["Current / A"][mask]
    slope, intercept = np.polyfit(x, y, 1)
    resistance_ohm = 1 / slope
    specific_resistance_ohm_cm2 = resistance_ohm * area_cm2
    return {
        "Fit cutoff current / A": cutoff_current,
        "Intercept / A": intercept,
        "Slope / Ω⁻¹": slope,
        "Resistance / Ω": resistance_ohm,
        "Area / cm²": area_cm2,
        "Area specific resistance / Ω cm²": specific_resistance_ohm_cm2,
    }


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
