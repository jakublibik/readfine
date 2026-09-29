"""What each reading/display preference accepts.

Shared by Settings → Preferences and the OPML import, so a value restored from a
file is held to the same rules as one picked in the form.
"""

DENSITY_VALUES = {"compact", "comfortable", "summary"}
SORT_VALUES = {"newest", "oldest"}
UNREAD_FILTER_VALUES = {"show_all", "unread_only", "adaptive"}
LABEL_DISPLAY_VALUES = {"none", "indicator", "dots"}
FONT_SIZE_VALUES = {"sm", "md", "lg"}
FONT_FAMILY_VALUES = {"sans", "serif"}

ARTICLES_PER_PAGE_MIN, ARTICLES_PER_PAGE_MAX = 10, 200


def clamp_articles_per_page(value: int) -> int:
    return max(ARTICLES_PER_PAGE_MIN, min(ARTICLES_PER_PAGE_MAX, value))


def clamp_buckets(small_max: int, medium_max: int) -> tuple[int, int]:
    """The two layout breakpoints, kept in range and at least 100px apart."""
    small_max = max(320, min(1000, small_max))
    medium_max = max(small_max + 100, min(2000, medium_max))
    return small_max, medium_max
