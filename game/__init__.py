"""slingshot.io game server package."""

from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"


def _read_version() -> str:
    try:
        value = _VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"VERSION file missing/unreadable: {_VERSION_FILE} ({exc})") from exc
    if not value:
        raise RuntimeError(f"VERSION file is empty: {_VERSION_FILE}")
    return value


__version__ = _read_version()