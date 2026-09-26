"""lexical relevance: the score column, the corpus statistics, the per-user switch

lexical_score sits beside ai_score rather than replacing it. The two scorers have
to stay comparable over the same reader and the same profile, and every consumer
then picks which number it means instead of inheriting whichever one happened to be
written last.

lexical_terms and lexical_corpus are the BM25 corpus statistics: how many of the last
month's articles each term appears in, plus the document count and average length that
the IDF and the length normalization need. Instance-wide, rebuilt whole every night
(app.services.relevance_corpus_service) and empty until that job first runs, which is
the state the scorer reads as "no score yet" rather than as a score of zero. Expect
tens of thousands of rows: 5404 terms over 3106 articles offline, growing sublinearly,
so around 40k at production size.

basic_scoring_enabled starts on for everyone. It is harmless on an account with no
interest profile: with nothing to match against, the scorer produces no score at all.

Nothing is backfilled. lexical_score fills in as articles arrive, and only a saved
profile pulls the last 7 days up with it.

Revision ID: 0103
Revises: 0102
Create Date: 2026-09-22
"""
from alembic import op
import sqlalchemy as sa

revision = "0103"
down_revision = "0102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_article_states",
        sa.Column("lexical_score", sa.Float(), nullable=True),
    )
    op.add_column(
        "user_settings",
        sa.Column("basic_scoring_enabled", sa.Boolean(), nullable=False,
                  server_default=sa.text("true")),
    )
    op.create_table(
        "lexical_terms",
        sa.Column("term", sa.String(64), primary_key=True),
        sa.Column("doc_freq", sa.Integer(), nullable=False),
    )
    op.create_table(
        "lexical_corpus",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("n_docs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("avg_doc_len", sa.Float(), nullable=False, server_default="0"),
        sa.Column("min_df", sa.SmallInteger(), nullable=False, server_default="3"),
        sa.Column("ngram_max", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("window_days", sa.SmallInteger(), nullable=False, server_default="30"),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("build_seconds", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("lexical_corpus")
    op.drop_table("lexical_terms")
    op.drop_column("user_settings", "basic_scoring_enabled")
    op.drop_column("user_article_states", "lexical_score")
