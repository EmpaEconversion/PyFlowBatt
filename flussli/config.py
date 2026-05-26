"""Configuration for flussli — file patterns and sample ID detection."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

_TECHNIQUE_KEYS = {
    "gcpl": "gcpl_patterns",
    "ocv": "ocv_patterns",
    "lsv": "lsv_patterns",
    "cv_before": "cv_before_patterns",
    "cv_after": "cv_after_patterns",
    "eis": "eis_patterns",
}


@dataclass
class FlussliConfig:
    r"""Configuration controlling how flussli detects files and sample IDs.

    Flussli searches for files with `glob`, and looks for files matching a
    'pattern' plus an extension.

        Example `flussli.toml`:

        # add to list of extensions to search for
        extra_extensions = [".mpr"]
        # or override list
        # extensions = [".mpr", ".mpt"]

        [extra_patterns]
        eis  = ["*_EIS_*"]    # adds to the built-in *_PEIS_*
        gcpl = ["*_GCD_*"]

        [sample_id]
        pattern = "^[A-Z]+-\\\\d+$"     # custom regex
        # name = "daves-new-sample-01"  # or hard-code a fixed name

    Config files are loaded in ascending priority order: `~/flussli.toml`
    (lab-wide defaults), the parent folder, then the sample folder itself.
    """

    gcpl_patterns: list[str] = field(default_factory=lambda: ["*_GCPL_*"])
    ocv_patterns: list[str] = field(default_factory=lambda: ["*_OCV_*"])
    lsv_patterns: list[str] = field(default_factory=lambda: ["*_LSV_*"])
    cv_before_patterns: list[str] = field(default_factory=lambda: ["*_CVApre*", "*_CVpre*"])
    cv_after_patterns: list[str] = field(default_factory=lambda: ["*_CVApost*", "*_CVpost*"])
    eis_patterns: list[str] = field(default_factory=lambda: ["*_PEIS_*"])
    extensions: list[str] = field(default_factory=lambda: [".mpr", ".mpt", ".csv"])
    sample_id_pattern: str = r"^\d+_.+_.+$"
    sample_id: str | None = None  # explicit name; overrides pattern entirely

    @classmethod
    def load(cls, folder: str | Path) -> Self:
        """Load config from ~/flussli.toml, the parent folder, and the sample folder.

        Returns a config with only built-in defaults when no config file exists.
        """
        folder = Path(folder).resolve()
        config = cls()

        # Check three locations in ascending priority order:
        # home dir (lab-wide default), parent folder, sample folder itself.
        candidates = [
            Path.home() / "flussli.toml",
            folder.parent / "flussli.toml",
            folder / "flussli.toml",
        ]

        for path in candidates:
            if path.is_file():
                logger.debug("Loading flussli config from %s", path)
                config._apply_toml(path)

        return config

    def all_patterns(self) -> list[str]:
        """All unique glob strings (stem pattern × extension) across every technique."""
        seen: set[str] = set()
        result: list[str] = []
        for patterns in (
            self.gcpl_patterns,
            self.ocv_patterns,
            self.lsv_patterns,
            self.cv_before_patterns,
            self.cv_after_patterns,
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
