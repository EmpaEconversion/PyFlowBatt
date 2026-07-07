"""Optional pyflowbatt.toml configuration: technique patterns, extensions, sample ID."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

CONFIG_FILENAME = "pyflowbatt.toml"
EIS_TAGS = ["pre", "pre-50%SOC", "post-50%SOC", "post"]
DEFAULT_AREA_CM2 = 5.0  # fallback electrode area (cm^2) when no toml or BattINFO value exists
_LSV_NUMBER_RE = re.compile(r"_(\d+)_LSV_")

_TECHNIQUE_KEYS: dict[str, str] = {
    "gcpl": "gcpl_patterns",
    "ocv": "ocv_patterns",
    "lsv": "lsv_patterns",
    "cv_pre": "cv_pre_patterns",
    "cv_post": "cv_post_patterns",
    "eis": "eis_patterns",
}


@dataclass
class PyFlowBattConfig:
    r"""Configuration controlling how PyFlowBatt detects technique files and sample IDs.

    PyFlowBatt searches for files matching a glob 'pattern' plus an extension.

        Example `pyflowbatt.toml`:

        extra_extensions = [".mgr"]
        # or fully replace the extension list:
        # extensions = [".mpr", ".mgr"]

        [extra_patterns]
        eis  = ["*_EIS_*"]    # adds to the built-in *_PEIS_*
        gcpl = ["*_GCD_*"]

        [sample_id]
        pattern = "^[A-Z]+-\\d+$"     # custom regex
        # name = "daves-new-sample-01"  # or hard-code a fixed name

        area_cm2 = 3.14   # electrode area used to normalise LSV resistance

    Config files are loaded in ascending priority order: `~/pyflowbatt.toml`
    (lab-wide defaults), the parent folder, then the sample folder itself.

    If ``area_cm2`` is left unset, PyFlowBatt.analysis.get_area_cm2 falls back to the
    electrode area recorded in a BattINFO metadata file (if present) before finally
    falling back to :data:`DEFAULT_AREA_CM2`.
    """

    gcpl_patterns: list[str] = field(default_factory=lambda: ["*_GCPL_*"])
    ocv_patterns: list[str] = field(default_factory=lambda: ["*_OCV_*"])
    lsv_patterns: list[str] = field(default_factory=lambda: ["*_LSV_*"])
    cv_pre_patterns: list[str] = field(default_factory=lambda: ["*_CVApre*", "*_CVpre*"])
    cv_post_patterns: list[str] = field(default_factory=lambda: ["*_CVApost*", "*_CVpost*"])
    eis_patterns: list[str] = field(default_factory=lambda: ["*_PEIS_*"])
    extensions: list[str] = field(default_factory=lambda: [".mpr"])
    sample_id_pattern: str = r"^\d+_.+_.+$"
    sample_id: str | None = None  # explicit name; overrides pattern entirely
    lsv_threshold: int = 8  # numeric cutoff for pre/post when only one LSV file is found
    area_cm2: float | None = None  # explicit override; None lets BattINFO/default resolve it

    @classmethod
    def load(cls, folder: str | Path, *, home: Path | None = None) -> PyFlowBattConfig:
        """Load config from ~/pyflowbatt.toml, the parent folder, and the sample folder.

        Returns a config with only built-in defaults when no config file exists.
        """
        folder = Path(folder).resolve()
        home = home if home is not None else Path.home()
        config = cls()

        # Check three locations in ascending priority order:
        # home dir (lab-wide default), parent folder, sample folder itself.
        candidates = [
            home / CONFIG_FILENAME,
            folder.parent / CONFIG_FILENAME,
            folder / CONFIG_FILENAME,
        ]

        for path in candidates:
            if path.is_file():
                logger.debug("Loading PyFlowBatt config from %s", path)
                config._apply_toml(path)

        return config

    def all_patterns(self) -> list[str]:
        """All unique glob strings (stem pattern x extension) across every technique."""
        seen: set[str] = set()
        result: list[str] = []
        for patterns in (
            self.gcpl_patterns,
            self.ocv_patterns,
            self.lsv_patterns,
            self.cv_pre_patterns,
            self.cv_post_patterns,
            self.eis_patterns,
        ):
            for p in patterns:
                for ext in self.extensions:
                    full = f"{p}{ext}"
                    if full not in seen:
                        seen.add(full)
                        result.append(full)
        return result

    def _apply_toml(self, path: Path) -> None:
        """Merge a single TOML config file into this config (mutates in place)."""
        with path.open("rb") as f:
            data = tomllib.load(f)

        extra = data.get("extra_patterns", {})
        for toml_key, attr in _TECHNIQUE_KEYS.items():
            if toml_key in extra:
                values = extra[toml_key]
                if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                    logger.warning(
                        "Ignoring bad extra_patterns.%s in %s (expected list of strings)",
                        toml_key,
                        path,
                    )
                    continue
                current: list[str] = getattr(self, attr)
                for v in values:
                    if v not in current:
                        current.append(v)

        for key in ("extensions", "extra_extensions"):
            if key in data:
                values = data[key]
                if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                    logger.warning("Ignoring bad %s in %s (expected list of strings)", key, path)
                    continue
                if key == "extensions":
                    self.extensions = list(values)
                else:
                    for v in values:
                        if v not in self.extensions:
                            self.extensions.append(v)

        if "lsv_threshold" in data:
            if isinstance(data["lsv_threshold"], int):
                self.lsv_threshold = data["lsv_threshold"]
            else:
                logger.warning("Ignoring bad lsv_threshold in %s (expected int)", path)

        if "area_cm2" in data:
            if isinstance(data["area_cm2"], (int, float)) and not isinstance(
                data["area_cm2"], bool
            ):
                self.area_cm2 = float(data["area_cm2"])
            else:
                logger.warning("Ignoring bad area_cm2 in %s (expected number)", path)

        sid = data.get("sample_id", {})
        if "name" in sid:
            if isinstance(sid["name"], str):
                self.sample_id = sid["name"]
            else:
                logger.warning("Ignoring bad sample_id.name in %s (expected string)", path)
        if "pattern" in sid:
            if isinstance(sid["pattern"], str):
                self.sample_id_pattern = sid["pattern"]
            else:
                logger.warning("Ignoring bad sample_id.pattern in %s (expected string)", path)


def _glob_many(folder: Path, patterns: list[str], extensions: list[str]) -> list[Path]:
    """All files in folder matching any pattern x extension combo, de-duplicated."""
    seen: set[Path] = set()
    result: list[Path] = []
    for pattern in patterns:
        for ext in extensions:
            for p in folder.glob(f"{pattern}{ext}"):
                if p not in seen:
                    seen.add(p)
                    result.append(p)
    return result


def classify_technique_files(
    folder: str | Path,
    config: PyFlowBattConfig,
    *,
    warn: bool = False,
) -> dict[str, list[Path]]:
    """Classify technique files in `folder` according to `config`.

    Returns a dict with only the keys that had a match, drawn from: gcpl, ocv,
    lsv_pre, lsv_post, cv_pre, cv_post, eis_pre, eis_pre-50%SOC, eis_post-50%SOC,
    eis_post. Each value is the single-element list of the file selected for that
    label (largest-by-size for gcpl, first-match for ocv/cv, numeric pre/post split
    for lsv, positional tagging for eis).

    Pass warn=True to emit the ambiguity warnings analyse_sample historically
    logged inline. Call this with warn=True only once per sample per run (the
    RO-Crate writer re-classifies the same folder later and should pass warn=False
    to avoid duplicate log lines).
    """
    folder = Path(folder)
    result: dict[str, list[Path]] = {}

    gcpl_files = _glob_many(folder, config.gcpl_patterns, config.extensions)
    if gcpl_files:
        result["gcpl"] = [max(gcpl_files, key=lambda x: x.stat().st_size)]

    ocv_files = _glob_many(folder, config.ocv_patterns, config.extensions)
    if ocv_files:
        if warn and len(ocv_files) > 1:
            logger.warning("More than one OCV file, only reading %s", ocv_files[0].stem)
        result["ocv"] = [ocv_files[0]]

    lsv_files = _glob_many(folder, config.lsv_patterns, config.extensions)
    if lsv_files:
        numbers = [
            int(m.group(1)) if (m := _LSV_NUMBER_RE.search(f.stem)) else 0 for f in lsv_files
        ]
        lsv_sorted = [f for _, f in sorted(zip(numbers, lsv_files, strict=True))]
        if warn and len(lsv_sorted) > 2:
            logger.warning(
                "More than two LSV files, assuming %d is pre and %d is post",
                numbers[0],
                numbers[-1],
            )
        if len(lsv_sorted) == 1:
            p = "pre" if numbers[0] < config.lsv_threshold else "post"
            result[f"lsv_{p}"] = [lsv_sorted[0]]
        else:
            result["lsv_pre"] = [lsv_sorted[0]]
            result["lsv_post"] = [lsv_sorted[-1]]

    cv_pre_files = _glob_many(folder, config.cv_pre_patterns, config.extensions)
    if cv_pre_files:
        if warn and len(cv_pre_files) > 1:
            logger.warning("More than one CV file, only reading %s", cv_pre_files[0].stem)
        result["cv_pre"] = [cv_pre_files[0]]

    cv_post_files = _glob_many(folder, config.cv_post_patterns, config.extensions)
    if cv_post_files:
        if warn and len(cv_post_files) > 1:
            logger.warning("More than one CV file, only reading %s", cv_post_files[0].stem)
        result["cv_post"] = [cv_post_files[0]]

    eis_files = sorted(_glob_many(folder, config.eis_patterns, config.extensions))
    for tag, f in zip(EIS_TAGS, eis_files, strict=False):
        result[f"eis_{tag}"] = [f]

    return result
