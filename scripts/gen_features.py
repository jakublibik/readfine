#!/usr/bin/env python3
"""Generate FEATURES.md from the single source of truth.

The feature list lives in ``backend/app/content/features.yml`` and is rendered
in-app at ``/features``. This script projects the same data into ``FEATURES.md``
at the repo root so the list stays readable on GitHub without being maintained
twice.

Run after editing the YAML:

    uv run python scripts/gen_features.py

CI regenerates it and fails if the committed ``FEATURES.md`` is stale.
"""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "backend" / "app" / "content" / "features.yml"
OUT = ROOT / "FEATURES.md"

HEADER = (
    "<!-- Generated from backend/app/content/features.yml by "
    "scripts/gen_features.py. Do not edit by hand. -->"
)


def render(data: dict) -> str:
    lines = [HEADER, "", "# Readfine features", ""]
    intro = (data.get("intro") or "").strip()
    if intro:
        lines += [intro, ""]
    for category in data["categories"]:
        lines += [f"## {category['name']}", ""]
        summary = (category.get("summary") or "").strip()
        if summary:
            lines += [f"_{summary}_", ""]
        for feature in category["features"]:
            desc = " ".join(feature["desc"].split())
            lines.append(f"- **{feature['title']}:** {desc}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    data = yaml.safe_load(SRC.read_text(encoding="utf-8"))
    OUT.write_text(render(data), encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
