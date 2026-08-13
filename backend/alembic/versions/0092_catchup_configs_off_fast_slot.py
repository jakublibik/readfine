"""move catch-up configs off the fast slot and turn off the briefings that used it

The fast slot is the scoring model now: it answers with one number per article, so it
is picked to be as small as possible, and digests it writes read like it. Catch me up
and briefings always run on the main model from here on.

Briefings configured on the fast slot are turned off rather than quietly moved over,
because the main model is normally the more expensive one and nobody signed up for a
higher bill arriving on schedule. The reason goes into briefing_last_error, which the
config list and the briefing dialog both show, and switching the briefing back on
clears it.

Revision ID: 0092
Revises: 0091
Create Date: 2026-08-13
"""
from alembic import op
import sqlalchemy as sa

revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None

_DISABLED_MSG = (
    "Turned off automatically: briefings no longer run on the scoring model. "
    "Set up your main model in Settings → AI and switch the briefing back on."
)


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("""
            UPDATE user_catchup_configs
               SET briefing_enabled = false,
                   briefing_next_send_at = NULL,
                   briefing_retry_count = 0,
                   briefing_last_error = :msg
             WHERE briefing_enabled AND model_slot = 'fast'
        """),
        {"msg": _DISABLED_MSG},
    )
    conn.execute(sa.text(
        "UPDATE user_catchup_configs SET model_slot = 'quality' WHERE model_slot = 'fast'"
    ))


def downgrade() -> None:
    # Nothing to undo: which briefings this turned off is indistinguishable
    # afterwards from the ones their owners had turned off themselves.
    pass
