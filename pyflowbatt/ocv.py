"""Functions for OCV.

OCV = open circuit voltage.
"""

import pandas as pd


def analyse(df: pd.DataFrame) -> tuple[float, float]:
    """Get the average OCV from an MPR OCV file. Returns mean and standard deviation."""
    if "Voltage / V" in df.columns:
        return float(df["Voltage / V"].mean()), float(df["Voltage / V"].std())
    msg = "Could not find voltage column for OCV."
    raise ValueError(msg)
