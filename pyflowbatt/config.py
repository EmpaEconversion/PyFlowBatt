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
_STEP_NUMBER_RE = re.compile(r"_(\d+)_[A-Za-z]")

_TECHNIQUE_KEYS: dict[str, str] = {
    "gcpl": "gcpl_patterns",
    "ocv": "ocv_patterns",
    "lsv": "lsv_patterns",
    "cv_pre": "cv_pre_patterns",
    "cv_post": "cv_post_patterns",
    "eis": "eis_patterns",
}

_CV_KEYS: dict[str, str] = {
    "v_min": "cv_v_min",
    "v_max": "cv_v_max",
    "v_med": "cv_v_med",
    "v_range": "cv_v_range",
    "min_r2": "cv_min_r2",
}


def _is_number(value: object) -> bool:
    """Check if value is an int or float."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass
class PyFlowBattConfig:
    r"""Configuration controlling how PyFlowBatt detects technique files and sample IDs.

    PyFlowBatt searches for files matching a glob 'pattern' plus an extension.

        Example `pyflowbatt.toml`:

        sample_name = "daves-new-sample-01"    # hard-code an explicit sample name
        sample_name_pattern = "^[A-Z]+-\\d+$"  # or a custom regex, if not hard-coded

        extra_extensions = [".mgr"]
        # or fully replace the extension list:
        # extensions = [".mpr", ".mgr"]

        area_cm2 = 3.14   # electrode area used to normalise LSV resistance
        assembled_resistance_ohm = 25000   # external resistor value used in the cell assembly
        summary_n_cycles = [10, 20, 30, 40, 50]  # cycle counts reported in the summary sheet

        [extra_patterns]
        eis  = ["*_EIS_*"]    # adds to the built-in *_PEIS_*
        gcpl = ["*_GCD_*"]

        [eis_tags]            # pin EIS files to tags instead of filename order
        "pre"  = ["*_02_PEIS_*"]
        "post" = ["*_06_PEIS_*"]

        [cv]
        v_min = 0.4     # voltage window used to plot
        v_max = 0.6
        v_med = 0.5     # midpoint and range sampled to get currents from sweeps
        v_range = 0.02  # v_med +- v_range sampled
        min_r2 = 0.8    # fit-quality gate below which capacitance isn't reported

    Config files are loaded in ascending priority order: `~/pyflowbatt.toml`
    (lab-wide defaults), the parent folder, then the sample folder itself.

    If ``area_cm2`` is left unset, pyflowbatt.analysis.get_area_cm2 falls back to the
    electrode area recorded in a BattINFO metadata file (if present) before finally
    falling back to :data:`DEFAULT_AREA_CM2`.

    If ``assembled_resistance_ohm`` is left unset, pyflowbatt.analysis.get_assembled_resistance_ohm
    falls back to an ElectricResistance measurement recorded in a BattINFO metadata file (if
    present) before finally falling back to parsing it out of a filename (e.g. ``..._25kOhm_...``).
    """

    gcpl_patterns: list[str] = field(default_factory=lambda: ["*_GCPL_*"])
    ocv_patterns: list[str] = field(default_factory=lambda: ["*_OCV_*"])
    lsv_patterns: list[str] = field(default_factory=lambda: ["*_LSV_*"])
    cv_pre_patterns: list[str] = field(default_factory=lambda: ["*_CVApre*", "*_CVpre*"])
    cv_post_patterns: list[str] = field(default_factory=lambda: ["*_CVApost*", "*_CVpost*"])
    eis_patterns: list[str] = field(default_factory=lambda: ["*_PEIS_*"])
    extensions: list[str] = field(default_factory=lambda: [".mpr"])
    # EIS tag -> glob patterns; tags not listed are assigned in filename order
    eis_tag_patterns: dict[str, list[str]] = field(default_factory=dict)
    sample_name_pattern: str = r"^\d+_.+_.+$"
    sample_name: str | None = None  # overrides pattern entirely
    lsv_threshold: int = 8  # numeric cutoff for pre/post when only one LSV file is found
    area_cm2: float | None = None  # None lets BattINFO/default resolve it
    assembled_resistance_ohm: float | None = None  # None lets BattINFO/default resolve it
    # [cv] table settings; defaults match pyflowbatt.cv.analyse's own defaults
    cv_v_min: float = 0.4004
    cv_v_max: float = 0.6
    cv_v_med: float = 0.5
    cv_v_range: float = 0.02
    cv_min_r2: float = 0.8  # fit-quality gate below which CV capacitance isn't reported
    # cycle counts reported in the summary sheet (N cycles avg. CE/EE/VE/capacity)
    summary_n_cycles: list[int] = field(default_factory=lambda: [10, 20, 30, 40, 50])

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

        eis_tags = data.get("eis_tags", {})
        for tag, values in eis_tags.items():
            if tag not in EIS_TAGS:
                logger.warning(
                    "Ignoring unknown eis_tags.%s in %s (expected one of %s)", tag, path, EIS_TAGS
                )
                continue
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                logger.warning(
                    "Ignoring bad eis_tags.%s in %s (expected list of strings)", tag, path
                )
                continue
            self.eis_tag_patterns[tag] = list(values)

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
            if _is_number(data["area_cm2"]):
                self.area_cm2 = float(data["area_cm2"])
            else:
                logger.warning("Ignoring bad area_cm2 in %s (expected number)", path)

        if "assembled_resistance_ohm" in data:
            if _is_number(data["assembled_resistance_ohm"]):
                self.assembled_resistance_ohm = float(data["assembled_resistance_ohm"])
            else:
                logger.warning(
                    "Ignoring bad assembled_resistance_ohm in %s (expected number)", path
                )

        if "summary_n_cycles" in data:
            values = data["summary_n_cycles"]
            if isinstance(values, list) and all(_is_number(v) for v in values):
                self.summary_n_cycles = [int(v) for v in values]
            else:
                logger.warning(
                    "Ignoring bad summary_n_cycles in %s (expected list of numbers)", path
                )

        cv = data.get("cv", {})
        for toml_key, attr in _CV_KEYS.items():
            if toml_key in cv:
                if _is_number(cv[toml_key]):
                    setattr(self, attr, float(cv[toml_key]))
                else:
                    logger.warning("Ignoring bad cv.%s in %s (expected number)", toml_key, path)

        if "sample_name" in data:
            if isinstance(data["sample_name"], str):
                self.sample_name = data["sample_name"]
            else:
                logger.warning("Ignoring bad sample_name in %s (expected string)", path)

        if "sample_name_pattern" in data:
            if isinstance(data["sample_name_pattern"], str):
                self.sample_name_pattern = data["sample_name_pattern"]
            else:
                logger.warning("Ignoring bad sample_name_pattern in %s (expected string)", path)


TEMPLATE_TOML = """\
# pyflowbatt.toml — optional PyFlowBatt configuration.
#
# Every setting below is commented out and shows its built-in default.
# Uncomment and edit only the settings you want to override.
#
# You can put config files in:
# The home folder (~/pyflowbatt.toml): will apply EVERYWHERE
# The parent folder: applies to every sample folder inside
# The sample folder itself: only applies to that sample
# On conflicts, sample takes priority, then parent, then home.
#
# --- Sample name (default: reads from battinfo, otherwise uses folder name) ---
# sample_name = "daves-new-sample-01"      # hard-code an explicit sample name
# sample_name_pattern = "^[A-Z]+-\\\\d+$"  # or a custom regex, if not hard-coded

# --- File extensions searched for technique files ---

# extra_extensions = [".mgr"]      # add extra extensions to the default [".mpr"]
# extensions = [".mpr", ".mgr"]    # or fully replace the extension list instead of adding to it

# --- LSV pre/post split: file numbers below this are "pre", at/above are "post" ---
# lsv_threshold = 8

# --- Electrode area (cm^2), used to normalise LSV resistance ---
# area_cm2 = 5

# --- Assembled/external resistance (ohms) ---
# assembled_resistance_ohm = 25000

# --- Cycle counts reported in the summary sheet ---
# summary_n_cycles = [10, 20, 30, 40, 50]

# --- Extra filename glob patterns per technique (added to the built-in patterns below) ---
# [extra_patterns]
# gcpl    = ["*_cycling_*"]                  # built-in: *_GCPL_*
# ocv     = ["*_rest_*"]                     # built-in: *_OCV_*
# lsv     = ["*_linear-sweep_*"]             # built-in: *_LSV_*
# cv_pre  = ["*_cyclic-voltammetry-pre_*"]   # built-in: *_CVApre*, *_CVpre*
# cv_post = ["*_cyclic-voltammetry-post_*"]  # built-in: *_CVApost*, *_CVpost*
# eis     = ["*_impedance_*"]                # built-in: *_PEIS_*

# --- Pin EIS files to specific tags (default: tagged in filename order) ---
# Tags: "pre", "pre-50%SOC", "post-50%SOC", "post". Unlisted tags are filled in
# filename order from the remaining EIS files.
# [eis_tags]
# "pre"  = ["*_02_PEIS_*"]
# "post" = ["*_06_PEIS_*"]

# --- CV analysis parameters ---
# [cv]
# v_min = 0.4004   # voltage window used to compute scan rate
# v_max = 0.6
# v_med = 0.5      # midpoint voltage sampled for up/downsweep current
# v_range = 0.02   # v_med +- v_range sampled
# min_r2 = 0.8     # fit-quality gate below which capacitance isn't reported
"""


def write_template_config(folder: str | Path) -> Path:
    """Write a fully-commented pyflowbatt.toml template into `folder`.

    Raises FileExistsError if a pyflowbatt.toml already exists there — never overwrites.
    """
    folder = Path(folder)
    path = folder / CONFIG_FILENAME
    if path.exists():
        msg = f"{path} already exists, not overwriting"
        raise FileExistsError(msg)
    path.write_text(TEMPLATE_TOML, encoding="utf-8")
    return path


def _step_number(path: Path) -> int | None:
    """Technique step number from an EC-Lab filename, e.g. 5 for `cell_05_PEIS_C01`.

    Takes the last match so a run counter earlier in the name (`..._50Cycles_2_02_PEIS_CE5`)
    doesn't win over the step number.
    """
    matches = _STEP_NUMBER_RE.findall(path.stem)
    return int(matches[-1]) if matches else None


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
    for lsv, `eis_tag_patterns` then positional tagging for eis).

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

    gcpl_file = result["gcpl"][0] if "gcpl" in result else None
    result.update(_classify_eis(folder, config, gcpl_file, warn=warn))

    return result


def _classify_eis(
    folder: Path, config: PyFlowBattConfig, gcpl_file: Path | None, *, warn: bool
) -> dict[str, list[Path]]:
    """Tag EIS files, splitting them around the main cycling step.

    Files pinned by `eis_tag_patterns` win; any remaining files are tagged in
    filename order if pins exist, otherwise by :func:`_eis_order_and_tags`.
    """
    result: dict[str, list[Path]] = {}
    pinned: set[Path] = set()
    for tag in EIS_TAGS:
        patterns = config.eis_tag_patterns.get(tag)
        if not patterns:
            continue
        matches = sorted(_glob_many(folder, patterns, config.extensions))
        if not matches:
            if warn:
                logger.warning("No file matches eis_tags.%s %s", tag, patterns)
            continue
        if warn and len(matches) > 1:
            logger.warning(
                "More than one file matches eis_tags.%s, only reading %s", tag, matches[0].stem
            )
        result[f"eis_{tag}"] = [matches[0]]
        pinned.add(matches[0])

    eis_files = sorted(_glob_many(folder, config.eis_patterns, config.extensions))
    free_files = [f for f in eis_files if f not in pinned]
    if not free_files:
        return result

    # With pins in play, the remaining files just fill whatever tags are left over.
    if pinned:
        ordered = free_files
        tags = [tag for tag in EIS_TAGS if f"eis_{tag}" not in result]
    else:
        ordered, tags = _eis_order_and_tags(free_files, gcpl_file, warn=warn)
    for tag, f in zip(tags, ordered, strict=False):
        result[f"eis_{tag}"] = [f]
    return result


def _start_time(path: Path) -> float | None:
    """Read the acquisition start time (unix seconds) recorded inside an EC-Lab .mpr."""
    if path.suffix.lower() != ".mpr":
        return None
    try:
        import yadg  # noqa: PLC0415

        dataset = yadg.extractors.extract("eclab.mpr", path).to_dataset()
        return float(dataset["uts"].values[0])
    except Exception:  # noqa: BLE001  an unreadable file just means no timestamp
        logger.debug("Could not read a start time from %s", path.name)
        return None


def _eis_sort_keys(
    eis_files: list[Path], gcpl_file: Path | None
) -> tuple[list[float] | None, float | None]:
    """Put the EIS files and the cycling step on one common scale for ordering.

    Filename step numbers are used when every file has one, as reading them is free.
    Otherwise the acquisition timestamps inside the files are used, which is
    authoritative but has to parse the (potentially large) cycling file.
    """
    gcpl_step = _step_number(gcpl_file) if gcpl_file else None
    steps = [_step_number(f) for f in eis_files]
    if gcpl_step is not None and all(step is not None for step in steps):
        return [float(step) for step in steps if step is not None], float(gcpl_step)

    gcpl_time = _start_time(gcpl_file) if gcpl_file else None
    times = [_start_time(f) for f in eis_files]
    if gcpl_time is not None and all(time is not None for time in times):
        logger.debug("No step numbers in EIS filenames, ordering by acquisition time")
        return [time for time in times if time is not None], gcpl_time

    return None, None


def _eis_order_and_tags(
    eis_files: list[Path], gcpl_file: Path | None, *, warn: bool
) -> tuple[list[Path], list[str]]:
    """Order EIS files by when they ran and tag them by the protocol's layout.

    Two before and two after cycling is the standard protocol (0% and 50% SOC, each
    side); one before and one after is the early protocol (0% SOC either side). Any
    other layout can't be mapped onto the SOC-specific tags, so files get numbered
    `pre_N`/`post_N` tags instead. Falls back to filename order when neither step
    numbers nor timestamps are readable.
    """
    keys, gcpl_key = _eis_sort_keys(eis_files, gcpl_file)
    if keys is None or gcpl_key is None:
        if warn:
            logger.warning(
                "Cannot tell which EIS files are before/after cycling, tagging in filename order"
            )
        return eis_files, EIS_TAGS

    ordered = [f for _, f in sorted(zip(keys, eis_files, strict=True))]
    before = [key for key in keys if key < gcpl_key]
    after = [key for key in keys if key >= gcpl_key]
    if len(before) == 2 and len(after) == 2:
        return ordered, EIS_TAGS
    if len(before) == 1 and len(after) == 1:
        return ordered, ["pre", "post"]

    if warn:
        logger.warning(
            "Unexpected EIS layout (%d before and %d after cycling), "
            "tagging them pre_N/post_N instead of by state of charge",
            len(before),
            len(after),
        )
    tags = [f"pre_{i}" for i in range(1, len(before) + 1)]
    tags += [f"post_{i}" for i in range(1, len(after) + 1)]
    return ordered, tags
