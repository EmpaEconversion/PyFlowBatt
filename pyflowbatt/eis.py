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
MODELS_URL = "https://empaeconversion.github.io/fasteis/models/"
MAX_FIT_ERROR = 0.1

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


def summary_units(summary_params: list[str]) -> dict[str, str]:
    """Units of each summary parameter.

    Looks up element in fasteis, so summary rows exist even if the full model is invalid or there
    are no EIS files. Names that aren't a fasteis element parameter are warned about and dropped.
    """
    import fasteis  # noqa: PLC0415

    units = {}
    for param in summary_params:
        try:
            circuit = fasteis.Circuit(param.split(".")[0])
            element_units = dict(zip(circuit.param_names(), circuit.param_units(), strict=True))
        except ValueError:
            element_units = {}
        if param in element_units:
            units[param] = element_units[param]
        else:
            logger.warning(
                "- EIS summary parameter %s is not a fasteis parameter name, so it is left out "
                "of the summary. Use names like R0.r or CPE1.alpha, see %s",
                param,
                MODELS_URL,
            )
    return units


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
                "be empty in the summary. See %s for how to write circuits",
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


def _impedance(df: pd.DataFrame) -> np.ndarray:
    """Measured complex impedance in ohm."""
    return (df["Real Impedance / ohm"] + 1j * df["Imaginary Impedance / ohm"]).to_numpy()


def fit_error(df: pd.DataFrame, Z_fit: np.ndarray) -> float:
    """RMS distance between fit and data relative to |Z|, e.g. 0.05 is 5% off on average."""
    Z = _impedance(df)
    return float(np.sqrt(np.mean(np.abs((Z_fit - Z) / np.abs(Z)) ** 2)))


def analyse(
    df: pd.DataFrame,
    label: str = "",
    model: str = DEFAULT_MODEL,
    *,
    guess_init: bool | None = None,
    max_fit_error: float = MAX_FIT_ERROR,
) -> tuple[dict[str, dict] | None, np.ndarray]:
    """Fit EIS to an equivalent circuit model, warning if the fit looks unreliable.

    Parameters are None if the fit error is above max_fit_error.
    """
    import fasteis  # noqa: PLC0415

    f = df["Frequency / Hz"]
    Z = _impedance(df)
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
    error = fit_error(df, Z_fit)
    if error > max_fit_error:
        logger.warning(
            "- EIS fit %s error is very high (%.3f>%s), not reporting parameters",
            label,
            error,
            max_fit_error,
        )
        return None, Z_fit
    return params, Z_fit


def plot(
    df: pd.DataFrame, Z_fit: np.ndarray, max_fit_error: float = MAX_FIT_ERROR
) -> tuple[Figure, Axes]:
    """Nyquist plot fit result."""
    Z = _impedance(df)
    error = fit_error(df, Z_fit)
    bad = error > max_fit_error
    fig, ax = plt.subplots()
    # Residual segments from each data point to its fitted point
    data_pts = np.column_stack([np.real(Z), -np.imag(Z)])
    fit_pts = np.column_stack([np.real(Z_fit), -np.imag(Z_fit)])
    segments = list(zip(data_pts, fit_pts, strict=True))
    ax.plot(np.real(Z), -np.imag(Z), "k.-", label="Data")
    ax.plot(
        np.real(Z_fit),
        -np.imag(Z_fit),
        "C0.--" if bad else "C0.-",
        label="Fit (bad)" if bad else "Fit",
    )
    ax.add_collection(
        LineCollection(segments, colors="grey", linewidths=0.8, label="Residual", zorder=1)
    )
    error_text = f"Fit error = {error:.3f}"
    if bad:
        error_text += f"\n(>{max_fit_error}, result ignored)"
    ax.legend(title=error_text)
    ax.set_xlabel("Real Impedance / ohm")
    ax.set_ylabel("Imaginary Impedance / ohm")
    fig.tight_layout()
    return fig, ax
