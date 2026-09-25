"""Analyse and plot functions for EIS measurements.

EIS = electrochemical impedance spectroscopy.
"""

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.collections import LineCollection
from matplotlib.figure import Figure


def analyse(
    df: pd.DataFrame,
) -> tuple[dict[str, dict], np.ndarray]:
    """Fit EIS to R-(R,CPE)-(R,CPE) model."""
    import fasteis  # noqa: PLC0415

    f = df["Frequency / Hz"]
    Z = df["Real Impedance / ohm"] + 1j * df["Imaginary Impedance / ohm"]
    circuit = fasteis.Circuit("L0-R0-(R1,CPE1)-(R2,CPE2)")
    res = circuit.fit(f, Z)
    if not res.success:
        msg = "Failed to fit EIS"
        raise ValueError(msg)
    Z_fit = res.circuit.impedance(f)
    assert res.stderr is not None  # noqa: S101
    params = {
        name: {"value": res.params[name], "err": res.stderr[name], "unit": unit}
        for name, unit in zip(circuit.param_names(), circuit.param_units(), strict=True)
    }
    return params, Z_fit


def plot(df: pd.DataFrame, Z_fit: np.ndarray) -> tuple[Figure, Axes]:
    """Nyquist plot fit result."""
    Z = df["Real Impedance / ohm"] + 1j * df["Imaginary Impedance / ohm"]
    fig, ax = plt.subplots()
    # Residual segments from each data point to its fitted point
    data_pts = np.column_stack([np.real(Z), -np.imag(Z)])
    fit_pts = np.column_stack([np.real(Z_fit), -np.imag(Z_fit)])
    segments = list(zip(data_pts, fit_pts, strict=True))
    ax.plot(np.real(Z), -np.imag(Z), "k.-", label="Data")
    ax.plot(np.real(Z_fit), -np.imag(Z_fit), "C0.-", label="Fit")
    ax.add_collection(
        LineCollection(segments, colors="grey", linewidths=0.8, label="Residual", zorder=1)
    )
    ax.legend()
    ax.set_xlabel("Real Impedance / ohm")
    ax.set_ylabel("Imaginary Impedance / ohm")
    fig.tight_layout()
    return fig, ax
