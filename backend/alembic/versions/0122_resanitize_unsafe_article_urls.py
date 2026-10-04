"""Re-sanitize articles whose address carries a quote, angle bracket or whitespace

Relative links in an article body used to be made absolute after sanitizing, with
the article's address pasted into the attribute unescaped. An address with a quote
in it closed the attribute and the rest of it landed in the stored body as markup.
The rewrite escapes now and new addresses are percent-encoded on the way in, but a
body stored before that may still carry whatever the address put there.

Only rows whose address has one of those characters can be affected, so only those
are touched: the body goes through the sanitizer again (the feed's own allowlist for
``content``, the extraction's for ``readable_content``) and the address is encoded
the way the fetchers now store it.

Revision ID: 0122
Revises: 0121
Create Date: 2026-10-04
"""
import logging

import nh3
import sqlalchemy as sa
from alembic import op

from app.services.readable_service import _sanitize
from app.utils.parsing import encode_unsafe_url_chars, normalize_url

revision = "0122"
down_revision = "0121"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, url, content, readable_content FROM articles"
        " WHERE url ~ '[\"<>[:space:][:cntrl:]`]'"
    )).all()
    for aid, url, content, readable in rows:
        new_url = encode_unsafe_url_chars(url)[:2048]
        conn.execute(
            sa.text(
                "UPDATE articles SET url = :url, url_normalized = :norm,"
                " content = :content, readable_content = :readable WHERE id = :id"
            ),
            {
                "id": aid,
                "url": new_url,
                "norm": normalize_url(new_url),
                "content": nh3.clean(content) if content else content,
                "readable": _sanitize(readable) if readable else readable,
            },
        )
    if rows:
        logger.info("re-sanitized %d article(s) with an unsafe address", len(rows))


def downgrade() -> None:
    # The old bodies are gone and were the problem; nothing to restore.
    pass
