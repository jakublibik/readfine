"""Readfine application package."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("readfine")
except PackageNotFoundError:  # package not installed (e.g. running from a raw checkout)
    __version__ = "0.0.0+unknown"
