"""The release notes shown in-app at ``/changelog``, read from ``CHANGELOG.md``.

The page shows the changelog of the code that is running, so a self-hoster on an
older version sees their own notes rather than upstream's. ``CHANGELOG.md`` lives in
the repository root; the Docker image copies it next to the app (``/app``).
"""
import functools
import re
from dataclasses import dataclass, field
from pathlib import Path

from app.utils.markdown import md_render

_BACKEND_DIR = Path(__file__).resolve().parents[2]
# In the image the backend is /app and the file is copied into it; in a checkout
# it sits one level up, in the repository root.
_CANDIDATES = (_BACKEND_DIR / "CHANGELOG.md", _BACKEND_DIR.parent / "CHANGELOG.md")

# Sections meant for whoever runs the instance (migrations, config), not readers.
ADMIN_SECTIONS = {"upgrade notes"}

_RELEASE_RE = re.compile(r"^## \[(?P<version>[^\]]+)\](?:[ \t]*-[ \t]*(?P<date>\S+))?", re.M)
_SECTION_RE = re.compile(r"^### +(.+?)\s*$", re.M)


@dataclass
class Section:
    title: str | None  # None for text before the first ### heading
    html: str
    admin_only: bool = False


@dataclass
class Release:
    version: str  # "Unreleased" or "0.19.0"
    date: str | None
    sections: list[Section] = field(default_factory=list)

    @property
    def unreleased(self) -> bool:
        return self.version.lower() == "unreleased"

    @property
    def anchor(self) -> str:
        return "unreleased" if self.unreleased else "v" + self.version


def _parse_sections(body: str) -> list[Section]:
    sections: list[Section] = []
    parts = _SECTION_RE.split(body)
    # split() with one group gives [intro, title1, body1, title2, body2, ...]
    intro = parts[0].strip()
    if intro:
        sections.append(Section(None, md_render(intro)))
    for title, text in zip(parts[1::2], parts[2::2]):
        text = text.strip()
        if not text:
            continue
        sections.append(Section(title, md_render(text), title.lower() in ADMIN_SECTIONS))
    return sections


def parse_changelog(text: str) -> list[Release]:
    """Split a Keep a Changelog file into releases, newest first.

    Everything before the first ``## [...]`` heading (the file's own preamble) is
    dropped. A release with nothing under it, such as an empty ``[Unreleased]``
    right after a release, is left out.
    """
    matches = list(_RELEASE_RE.finditer(text))
    releases = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections = _parse_sections(text[m.end():end])
        if sections:
            releases.append(Release(m["version"], m["date"], sections))
    return releases


@functools.lru_cache(maxsize=1)
def load_changelog() -> list[Release] | None:
    """Parsed ``CHANGELOG.md``, or None when the file isn't there. Cached."""
    for path in _CANDIDATES:
        if path.is_file():
            return parse_changelog(path.read_text(encoding="utf-8"))
    return None
