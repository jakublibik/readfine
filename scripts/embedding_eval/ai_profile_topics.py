"""The AI profile split into topics, as the offline evals read it.

Moved here from `app.services.relevance_service` once the app stopped reading the
AI profile for basic relevance (the one-off seed was dropped). Kept verbatim so
`run_lexical_fidelity.py` and `run_terms_eval.py` still reproduce their numbers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# The generator writes `label: topics` lines and asks for "High relevance /
# Moderate relevance / Avoid", but the model may translate or reword the labels,
# so both are matched loosely and anything unrecognised counts as positive.
_NEGATIVE_LABEL_RE = re.compile(
    r"avoid|exclude|not interested|no interest|dislike|skip|irrelevant|"
    r"nezajím|vyhýb|vynech|nechci", re.IGNORECASE)
# Splitting on the Czech conjunction "a" is deliberately left out: it collides
# with the English article, and the profile is written in English by default.
_TOPIC_SEPARATOR_RE = re.compile(r"[,;]|\band\b|\bnebo\b")
_MIN_TOPIC_CHARS = 4


def split_topics(line: str) -> list[str]:
    """Split a topic list on separators that sit outside brackets.

    The generator writes topics like "health science with mechanistic findings
    (nutrition, exercise, longevity)". Splitting on every comma turns that into
    bare "exercise" and "longevity)" — fragments that have lost the context that
    made them a topic, and that then match unrelated articles.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    tokens = _TOPIC_SEPARATOR_RE.split(line)
    separators = _TOPIC_SEPARATOR_RE.findall(line)
    for i, token in enumerate(tokens):
        current.append(token)
        depth += token.count("(") - token.count(")")
        if i < len(separators):
            if depth > 0:  # separator inside brackets: keep the topic together
                current.append(separators[i])
            else:
                parts.append("".join(current))
                current = []
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


@dataclass(frozen=True)
class Profile:
    """The interest profile split into the units a lexical score is built from."""

    positive: list[str]
    negative: list[str]

    def __bool__(self) -> bool:
        return bool(self.positive)


def parse_profile(text: str | None) -> Profile:
    """Split `ai_preference_text` into positive and negative topic units.

    High and Moderate land in the same positive list. The negatives are returned
    so that nothing mistakes them for positives, and are never offered as terms.
    """
    if not text or not text.strip():
        return Profile([], [])

    positive_lines: list[str] = []
    negative_lines: list[str] = []
    for raw_line in text.strip().splitlines():
        line = raw_line.strip(" -•\t")
        if not line:
            continue
        label, _, topics = line.partition(":")
        if not topics.strip():
            # A line without a label is a plain sentence: keep it whole and positive.
            positive_lines.append(line)
        elif _NEGATIVE_LABEL_RE.search(label):
            negative_lines.append(topics.strip())
        else:
            positive_lines.append(topics.strip())

    def units(lines: list[str]) -> list[str]:
        out = [t for line in lines
               for t in split_topics(line) if len(t) >= _MIN_TOPIC_CHARS]
        return out or ([" ".join(lines)] if lines else [])

    positive = units(positive_lines)
    if not positive and negative_lines:
        # Nothing but an avoid list: there is nothing to rank by, and scoring the
        # avoid list as if it were positive is the failure mode this whole module
        # is written around.
        return Profile([], units(negative_lines))
    return Profile(positive, units(negative_lines))
