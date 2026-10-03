"""store email addresses lowercased and trimmed

Emails were matched exactly, and each path normalized them differently: sign-up
only trimmed, login did nothing, the email change lowercased. Someone who signed
up as "Alice@Example.com" could not log in as "alice@example.com", and that
address could register a second account. The code now normalizes every address
on the way in (normalize_email), and this brings the stored ones in line, so the
existing unique index on users.email covers case as well.

An address whose lowercased form another account already has is left as it is
and logged: lowercasing it would break the unique index and, since the app
upgrades before it starts, keep the instance down. Until an admin merges or
removes one of the two, the mixed-case account can't log in.

Not reversible: the original capitalisation is not kept.

Revision ID: 0118
Revises: 0117
Create Date: 2026-10-03
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = "0118"
down_revision = "0117"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    op.execute("""
        UPDATE users u SET email = lower(trim(u.email))
        WHERE u.email <> lower(trim(u.email))
          AND NOT EXISTS (
              SELECT 1 FROM users o
              WHERE o.id <> u.id AND lower(trim(o.email)) = lower(trim(u.email))
          )
    """)
    skipped = op.get_bind().execute(sa.text(
        "SELECT id, email FROM users WHERE email <> lower(trim(email)) ORDER BY id"
    )).all()
    for user_id, email in skipped:
        logger.warning(
            "User %s (%s) left as is: another account has the same address in other "
            "letter case. It can't log in until one of the two is merged or removed.",
            user_id, email,
        )

    op.execute("""
        UPDATE users SET pending_email = lower(trim(pending_email))
        WHERE pending_email <> lower(trim(pending_email))
    """)
    op.execute("""
        UPDATE invitations SET email = lower(trim(email))
        WHERE email <> lower(trim(email))
    """)


def downgrade() -> None:
    pass
