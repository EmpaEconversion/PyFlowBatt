"""Analyse and plot functions for EIS measurements.

EIS = electrochemical impedance spectroscopy.
"""

import logging

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.axes import Axes
from matplotlib.collections import LineCollection
from matplotlib.figure import Figure

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "L0-R0-(R1,CPE1)-(R2,CPE2)"
DEFAULT_SUMMARY_PARAMS = ["R0.r"]
MODELS_URL = "https://empaeconversion.github.io/fasteis/models/"

# fasteis unit strings -> units for summary row labels
PRETTY_UNITS: dict[str, str] = {
    "ohm": "Ω",
    "F": "F",
    "H": "H",
    "H*s": "H s",
    "s": "s",
    "-": "1",
    "ohm^-1*s^alpha": "Ω⁻¹ s^α",  # noqa: RUF001
    "ohm*s^-0.5": "Ω s^-0.5",
    "F*s^(gamma-1)": "F s^(γ-1)",  # noqa: RUF001
    "ohm*m^2": "Ω m²",
}


def summary_label(param: str, tag: str, unit: str) -> str:
    """Summary sheet row label for one fit parameter of one tagged EIS measurement."""
    return f"EIS {param} {tag} / {PRETTY_UNITS.get(unit, unit)}"


def check_model(model: str, summary_params: list[str]) -> tuple[dict[str, str], bool]:
    """Validate an EIS circuit string, warning about missing summary params or ML model.

    Returns the parameter units keyed by name, and whether fasteis can guess initial
    parameters. Raises ValueError if the string is not a valid fasteis circuit.
    """
    import fasteis  # noqa: PLC0415

    circuit = fasteis.Circuit(model)
    units = dict(zip(circuit.param_names(), circuit.param_units(), strict=True))
    for param in summary_params:
        if param not in units:
            logger.warning(
                "- EIS summary parameter %s is not in model %s (parameters: %s), so it will "
                "be missing from the summary. See %s for how to write circuits",
                param,
                model,
                ", ".join(units),
                MODELS_URL,
            )
    # A synthetic spectrum is enough to find out if a trained model exists
    f = np.logspace(-1, 5, 10)
    try:
        circuit.guess(f, circuit.impedance(f))
    except ValueError:
        logger.warning(
            "- EIS model %s is not in the fasteis ML model library, so initial fit "
            "parameters cannot be guessed and the fit may be poor. See %s for available models",
            model,
            MODELS_URL,
        )
        return units, False
    return units, True


def analyse(
    df: pd.DataFrame,
    label: str = "",
    model: str = DEFAULT_MODEL,
    *,
    guess_init: bool | None = None,
) -> tuple[dict[str, dict], np.ndarray]:
    """Fit EIS to an equivalent circuit model, warning if the fit looks unreliable."""
    import fasteis  # noqa: PLC0415

    f = df["Frequency / Hz"]
    Z = df["Real Impedance / ohm"] + 1j * df["Imaginary Impedance / ohm"]
    circuit = fasteis.Circuit(model)
    res = circuit.fit(f, Z, guess_init=guess_init)
    Z_fit = res.circuit.impedance(f)
    errs = res.stderr or {}
    params = {
        name: {"value": res.params[name], "err": errs.get(name), "unit": unit}
        for name, unit in zip(circuit.param_names(), circuit.param_units(), strict=True)
    }
    if not res.success:
        logger.warning("- EIS fit %s did not converge", label)
    if all(p["err"] is None for p in params.values()):
        logger.warning("- EIS fit %s has no standard errors, parameters are degenerate", label)
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
