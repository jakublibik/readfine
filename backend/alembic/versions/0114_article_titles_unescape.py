"""article titles: decode the HTML entities an html-typed feed title left in them

feedparser returns a title of Atom type="html" as markup, and until this revision it
was stored that way, so a WordPress title arrived as `Can an &#8216;eSUV&#8217;
e-bike` and was shown with the entities spelled out. The fetcher now stores plain
text (utils.text.feed_title_text); this decodes the titles already stored.

Only titles with an entity in them are touched, and only the entities are decoded:
whether a stored `<em>` came from markup or from a plain title can no longer be told
apart. Two passes, for a feed that encoded its titles twice (`&amp;#8217;`).
Not reversible: there is no telling afterwards which titles had entities.

Revision ID: 0114
Revises: 0113
Create Date: 2026-09-28
"""
import html

import sqlalchemy as sa
from alembic import op

revision = "0114"
down_revision = "0113"
branch_labels = None
depends_on = None

_ENTITY = r"&(#[0-9]+|#x[0-9a-fA-F]+|[a-zA-Z]+);"


def _decode(title: str) -> str:
    return html.unescape(html.unescape(title))


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, title FROM articles WHERE title ~ :pattern"),
        {"pattern": _ENTITY},
    ).all()
    changed = [{"id": r.id, "title": _decode(r.title)[:1000]}
               for r in rows if _decode(r.title) != r.title]
    if changed:
        conn.execute(sa.text("UPDATE articles SET title = :title WHERE id = :id"), changed)


def downgrade() -> None:
    pass
