"""basic relevance gets its own profile: a term list, plus prefix counts

relevance_terms is the basic scorer's profile, one term per line, separate from
ai_preference_text. The AI profile is a description written for a model, and
BM25 read as a word list it misfired on its brackets and generic words. Starts
empty on every account: nothing is migrated, since the feature is unreleased and
the one-off seed from the AI profile is an offer the reader confirms, not a copy.
relevance_terms_updated_at replaces ai_preference_updated_at as what makes the
7-day backfill due.

lexical_prefixes holds document frequencies of token prefixes, the IDF of a
truncated match (a query word minus its last two characters, never below four).
lexical_corpus.tokenizer records which tokenization counted the table. Existing
rows get 1, the tokenizer before script-aware splitting, which the scorer treats
as no table at all, so the first pass after the upgrade rebuilds it. avg_doc_len
and ngram_max go: BM25 runs without length normalization and without bigram
features, so neither was ever read.

relevance_suggestion_dismissals keeps suggested terms the reader turned down, so
the same suggestion does not come back.

Revision ID: 0105
Revises: 0104
Create Date: 2026-09-23
"""
from alembic import op
import sqlalchemy as sa

revision = "0105"
down_revision = "0104"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_settings", sa.Column("relevance_terms", sa.Text(), nullable=True))
    op.add_column("user_settings", sa.Column(
        "relevance_terms_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_settings", sa.Column(
        "relevance_terms_source", sa.String(12), nullable=True))

    op.create_table(
        "lexical_prefixes",
        sa.Column("prefix", sa.String(64), primary_key=True),
        sa.Column("doc_freq", sa.Integer(), nullable=False),
    )
    op.add_column("lexical_corpus", sa.Column(
        "tokenizer", sa.SmallInteger(), nullable=False, server_default="1"))
    op.drop_column("lexical_corpus", "avg_doc_len")
    op.drop_column("lexical_corpus", "ngram_max")

    op.create_table(
        "relevance_suggestion_dismissals",
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("term", sa.String(200), primary_key=True),
        sa.Column("kind", sa.String(10), primary_key=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("relevance_suggestion_dismissals")
    op.add_column("lexical_corpus", sa.Column(
        "ngram_max", sa.SmallInteger(), nullable=False, server_default="1"))
    op.add_column("lexical_corpus", sa.Column(
        "avg_doc_len", sa.Float(), nullable=False, server_default="0"))
    op.drop_column("lexical_corpus", "tokenizer")
    op.drop_table("lexical_prefixes")
    op.drop_column("user_settings", "relevance_terms_source")
    op.drop_column("user_settings", "relevance_terms_updated_at")
    op.drop_column("user_settings", "relevance_terms")
