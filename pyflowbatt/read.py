"""Functions to read in data files. bdf read with extra functionality for mpr files."""

from pathlib import Path

import pandas as pd


def read_to_bdf(file: str | Path) -> pd.DataFrame:
    """Read file into pandas dataframe with BDF columns."""
    if Path(file).suffix == ".mpr":
        import yadg  # noqa: PLC0415

        df = yadg.extractors.extract("eclab.mpr", file).to_dataset().to_dataframe().reset_index()
        cols = set(df.columns)
        if not ({"I", "<I>"} & cols):
            if ({"dq", "dQ"} & cols) and "uts" in cols:
                # dq is mA h, multiply by 3600 to get mA s
                # Then multiply by diff(time) / s to get current in mA
                # mA -> A happens later, in _mpr_df_to_bdf
                dq_col = next(col for col in ("dq", "dQ") if col in cols)
                dt = df["uts"].diff().fillna(float("inf"))
                df["I"] = 3600 * df[dq_col] / dt
            else:
                df["I"] = 0  # e.g. OCV

        if "half cycle" in df.columns:  # It is cycling data
            # Have to do some duct taping
            # EC-labs 'cycles' and 'Q charge or discharge' are sometimes wrong
            df["dumb cycle"] = (df["ox or red"].astype(int).diff() > 0).cumsum()
            df["cycle number"] = 0
            cycle = 1
            for _group, group_df in df.groupby("dumb cycle"):
                chg_mask = group_df["dq"] > 0
                dchg_mask = group_df["dq"] < 0
                if (
                    sum(group_df["dq"][chg_mask]) > 0
                    and sum(group_df["dq"][dchg_mask]) < 0
                    and sum(chg_mask) > 5
                    and sum(dchg_mask) > 5
                ):
                    df.loc[group_df.index, "cycle number"] = cycle
                    cycle += 1

        if "Ns changes" in df.columns:
            df["step number"] = 1 + df["Ns changes"].astype(int).cumsum()

        return _mpr_df_to_bdf(df)
    import bdf  # noqa: PLC0415

    return bdf.read(file)


def _mpr_df_to_bdf(df: pd.DataFrame) -> pd.DataFrame:
    """Convert mpr columns to BDF."""
    col_map = {
        "uts": "Unix Time / s",
        "time": "Test Time / s",
        "Ewe": "Voltage / V",
        "Ece": "Voltage / V",
        "<Ewe>": "Voltage / V",
        "<Ece>": "Voltage / V",
        "Ecell": "Voltage / V",
        "I": "Current / A",
        "<I>": "Current / A",
        "freq": "Frequency / Hz",
        "Re(Z)": "Real Impedance / ohm",
        "-Im(Z)": "Imaginary Impedance / ohm",
        "Re(Z)_fit_Ohm": "Real Impedance Fit / ohm",
        "Im(Z)_fit_Ohm": "Imaginary Impedance Fit / ohm",
        "cycle number": "Cycle Count / 1",
        "step number": "Step Count / 1",
        "Temperature": "Ambient Temperature / degC",
    }
    multiplier_map = {
        "Imaginary Impedance / ohm": -1,
        "Current / A": 1e-3,
    }

    # Rename cols to bdf
    rename_cols = [c for c in col_map if c in df.columns]
    df = df[rename_cols].rename(columns={c: col_map[c] for c in rename_cols})

    # Modify cols if needed
    multiply_cols = [c for c in multiplier_map if c in df.columns]
    for c in multiply_cols:
        df[c] = df[c] * multiplier_map[c]

    return df
