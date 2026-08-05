"""Build and difficulty-calibrate ProgramBench tasks locally."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("programsmith")
except PackageNotFoundError:  # pragma: no cover - only when imported outside an install
    __version__ = "0+unknown"
