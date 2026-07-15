"""Loader for the feature list (single source of truth).

The list lives in ``app/content/features.yml`` and is rendered in-app at
``/features`` and generated into ``FEATURES.md`` by ``scripts/gen_features.py``.
"""
import functools
from pathlib import Path

import yaml

_FEATURES_PATH = Path(__file__).resolve().parent.parent / "content" / "features.yml"


@functools.lru_cache(maxsize=1)
def load_features() -> dict:
    """Return the parsed feature list. Cached; the file ships with the app."""
    with _FEATURES_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)
