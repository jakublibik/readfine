"""AI prompt generation for CSS selector discovery."""
from bs4 import BeautifulSoup

_ARTICLE_TAGS = {"article", "li", "div", "section"}
_SKIP_CONTAINERS = {"nav", "header", "footer", "aside"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4"}


def _is_in_skip_container(tag) -> bool:
    for parent in tag.parents:
        if getattr(parent, "name", None) in _SKIP_CONTAINERS:
            return True
    return False


def _looks_like_article_block(tag) -> bool:
    if tag.name not in _ARTICLE_TAGS:
        return False
    if _is_in_skip_container(tag):
        return False
    has_heading = bool(tag.find(_HEADING_TAGS))
    has_link = bool(tag.find("a", href=True))
    return has_link and (has_heading or len(tag.get_text(strip=True)) > 50)


def generate_selector_prompt(url: str, html: str) -> str:
    """Generate a prompt for an external AI to find the CSS selector for article links."""
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

    sample = "\n\n".join(blocks)[:3000] if blocks else html[:3000]

    return (
        f"I need to scrape article links from this webpage: {url}\n\n"
        "Below are sample HTML blocks from the page that look like article listings. "
        "Find a CSS selector that matches the repeating group of article links "
        "(not navigation, footer, sidebar, or ads).\n\n"
        "Return ONLY the CSS selector as plain text — no JSON, no explanation, no quotes.\n\n"
        f"HTML sample:\n{sample}"
    )
