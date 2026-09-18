"""slingshot.io game server package."""

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
_FALLBACK_VERSION = "1.5.0"


def _read_version() -> str:
    try:
        value = _VERSION_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError as exc:
        log.warning("could not read VERSION file (%s); using %s", exc, _FALLBACK_VERSION)
    return _FALLBACK_VERSION


__version__ = _read_version()