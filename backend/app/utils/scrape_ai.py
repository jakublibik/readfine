"""AI prompt generation for CSS selector discovery."""
from bs4 import BeautifulSoup

_ARTICLE_TAGS = {"article", "li", "div", "section"}
_SKIP_PARENT_TAGS = {"nav", "header", "footer", "aside", "form", "script", "style"}
_SKIP_OWN_CLASS_KEYWORDS = {
    "modal", "popup", "overlay", "payment", "subscribe", "cookie",
    "newsletter", "promo", "advertisement", "banner",
}
_HEADING_TAGS = {"h1", "h2", "h3", "h4"}
_MAX_BLOCK_LEN = 10_000  # large wrappers (full page, header, etc.) are not article cards


def _has_skip_class(tag) -> bool:
    combined = " ".join(tag.get("class") or []).lower() + " " + (tag.get("id") or "").lower()
    return any(kw in combined for kw in _SKIP_OWN_CLASS_KEYWORDS)


def _is_in_skip_container(tag) -> bool:
    for parent in tag.parents:
        if getattr(parent, "name", None) in _SKIP_PARENT_TAGS:
            return True
    return False


def _looks_like_article_block(tag) -> bool:
    if tag.name not in _ARTICLE_TAGS:
        return False
    if tag.get("aria-hidden") == "true":
        return False
    if _has_skip_class(tag):
        return False
    if _is_in_skip_container(tag):
        return False
    if len(str(tag)) > _MAX_BLOCK_LEN:
        return False
    has_heading = bool(tag.find(_HEADING_TAGS))
    has_link = bool(tag.find("a", href=True))
    return has_link and (has_heading or len(tag.get_text(strip=True)) > 50)


def extract_article_sample(html: str) -> str:
    """Extract representative article HTML blocks for AI analysis (~3000 chars)."""
    soup = BeautifulSoup(html, "lxml")
    blocks: list[str] = []
    seen: set[int] = set()
    for tag in soup.find_all(True):
        if not _looks_like_article_block(tag):
            continue
        if any(id(p) in seen for p in tag.parents):
            continue
        seen.add(id(tag))
        blocks.append(str(tag)[:500])
        if len(blocks) >= 5:
            break
    return "\n\n".join(blocks)[:3000] if blocks else html[:3000]


def build_selector_prompt(url: str, sample: str, history: list[dict] | None = None) -> str:
    """Build AI prompt from pre-extracted sample, optionally with refinement history."""
    base = (
        f"I need to scrape article links from this webpage: {url}\n\n"
        "Below are sample HTML blocks from the page that look like article listings. "
        "Find a CSS selector that matches the repeating group of article links "
        "(not navigation, footer, sidebar, or ads).\n\n"
        "Return ONLY the CSS selector as plain text — no JSON, no explanation, no quotes.\n\n"
        f"HTML sample:\n{sample}"
    )
    if history:
        attempts = "\n".join(
            f"Attempt {i+1}: selector `{h.get('selector', '')}` — feedback: {h.get('feedback', '')}"
            for i, h in enumerate(history[-5:])
        )
        base += f"\n\nPrevious attempts:\n{attempts}\n\nProvide a corrected CSS selector."
    return base


def generate_selector_prompt(url: str, html: str) -> str:
    """Generate a prompt for an external AI to find the CSS selector for article links."""
    sample = extract_article_sample(html)
    return build_selector_prompt(url, sample)
