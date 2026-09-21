"""Media types used by RO-crate and BattINFO metadata."""

from pathlib import Path

DEFAULT_MEDIA_TYPE = "application/octet-stream"

MEDIA_TYPES: dict[str, str] = {
    ".mpr": DEFAULT_MEDIA_TYPE,
    ".mps": "text/plain",
    ".parquet": "application/vnd.apache.parquet",
    ".csv": "text/csv",
    ".json": "application/ld+json",  # every json PyFlowBatt writes is JSON-LD
    ".png": "image/png",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def media_type(path: str | Path) -> str:
    """Media type for a file, by extension, defaulting to binary."""
    return MEDIA_TYPES.get(Path(path).suffix.lower(), DEFAULT_MEDIA_TYPE)
