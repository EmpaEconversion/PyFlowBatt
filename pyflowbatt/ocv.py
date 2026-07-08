"""Functions for OCV.

OCV = open circuit voltage.
"""

from pathlib import Path

from pyflowbatt.read import read_to_bdf


def analyse(file: str | Path) -> tuple[float, float]:
    """Get the average OCV from an MPR OCV file. Returns mean and standard deviation."""
    df = read_to_bdf(file)
    if "Voltage / V" in df.columns:
        return float(df["Voltage / V"].mean()), float(df["Voltage / V"].std())
    msg = f"Could not find voltage column in {file}."
    raise ValueError(msg)
