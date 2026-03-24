"""Initial schema

Revision ID: 0001
Revises:
Create Date: 2026-03-24
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, ARRAY

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Extensions
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    # IMMUTABLE wrapper needed for using unaccent() in index expressions
    op.execute("""
        CREATE OR REPLACE FUNCTION immutable_unaccent(text)
        RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS
        $$ SELECT public.unaccent('public.unaccent', $1) $$
    """)

    # --- users ---
    op.create_table(
        "users",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="user"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_index("ix_users_role", "users", ["role"])

    # --- user_settings ---
    op.create_table(
        "user_settings",
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("list_density_web", sa.String(20), server_default="medium"),
        sa.Column("list_density_mobile", sa.String(20), server_default="compact"),
        sa.Column("mark_read_on_scroll", sa.Boolean, server_default="true"),
        sa.Column("show_unread_only", sa.Boolean, server_default="true"),
        sa.Column("default_sort_order", sa.String(10), server_default="newest"),
        sa.Column("left_panel_pinned", sa.Boolean, server_default="true"),
        sa.Column("articles_per_page", sa.SmallInteger, server_default="50"),
        sa.Column("timezone", sa.String(50), server_default="UTC"),
        sa.Column("language", sa.String(10), server_default="cs"),
        sa.Column("keyboard_shortcuts_enabled", sa.Boolean, server_default="true"),
    )

    # --- api_tokens ---
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("token_prefix", sa.String(10), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_api_tokens_user_id", "api_tokens", ["user_id"])
    op.create_index("ix_api_tokens_token_hash", "api_tokens", ["token_hash"], unique=True)

    # --- password_reset_tokens ---
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_password_reset_tokens_token_hash", "password_reset_tokens", ["token_hash"], unique=True)

    # --- invitations ---
    op.create_table(
        "invitations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("created_by", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token", sa.String(64), nullable=False),
        sa.Column("email", sa.String(255)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("used_by", sa.Integer, sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_invitations_token", "invitations", ["token"], unique=True)

    # --- app_settings ---
    op.create_table(
        "app_settings",
        sa.Column("id", sa.SmallInteger, primary_key=True),
        sa.Column("registration_enabled", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("default_fetch_interval_min", sa.SmallInteger, nullable=False, server_default="60"),
        sa.Column("max_feeds_per_user", sa.SmallInteger, nullable=False, server_default="200"),
        sa.Column("default_purge_after_days", sa.SmallInteger, server_default="90"),
        sa.Column("default_purge_keep_count", sa.SmallInteger, server_default="500"),
        sa.Column("smtp_host", sa.String(255)),
        sa.Column("smtp_port", sa.SmallInteger, server_default="587"),
        sa.Column("smtp_user", sa.String(255)),
        sa.Column("smtp_password_encrypted", sa.Text),
        sa.Column("smtp_from_email", sa.String(255)),
        sa.Column("smtp_use_tls", sa.Boolean, server_default="true"),
        sa.Column("ai_enabled", sa.Boolean, server_default="false"),
        sa.Column("ai_require_user_keys", sa.Boolean, server_default="false"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    # Seed default app settings row
    op.execute("INSERT INTO app_settings (id) VALUES (1)")

    # --- audit_log ---
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("admin_id", sa.Integer, sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("target_type", sa.String(30)),
        sa.Column("target_id", sa.Integer),
        sa.Column("detail", JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_audit_log_admin_created", "audit_log", ["admin_id", sa.text("created_at DESC")])
    op.create_index("ix_audit_log_target", "audit_log", ["target_type", "target_id"])
    op.create_index("ix_audit_log_created", "audit_log", [sa.text("created_at DESC")])

    # --- folders ---
    op.create_table(
        "folders",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("position", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_folders_user_name", "folders", ["user_id", "name"], unique=True)
    op.create_index("ix_folders_user_id", "folders", ["user_id"])

    # --- feeds ---
    op.create_table(
        "feeds",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("feed_url", sa.String(2048), nullable=False),
        sa.Column("is_private", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("fetch_auth_user", sa.String(255)),
        sa.Column("fetch_auth_pass_encrypted", sa.Text),
        sa.Column("site_url", sa.String(2048)),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("favicon_url", sa.String(2048)),
        sa.Column("favicon_data", sa.Text),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("last_error", sa.Text),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True)),
        sa.Column("last_fetch_duration_ms", sa.Integer),
        sa.Column("last_published_at", sa.DateTime(timezone=True)),
        sa.Column("fetch_interval_min", sa.SmallInteger),
        sa.Column("subscriber_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("feed_type", sa.String(20), nullable=False, server_default="rss"),
        sa.Column("type_config", JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('active', 'error', 'paused')", name="ck_feeds_status"),
        sa.CheckConstraint("feed_type IN ('rss', 'youtube', 'scrape', 'twitter', 'podcast')", name="ck_feeds_feed_type"),
    )
    # Unique only for public feeds
    op.execute(
        "CREATE UNIQUE INDEX ix_feeds_url_public ON feeds (feed_url) WHERE is_private = FALSE"
    )
    op.create_index("ix_feeds_status", "feeds", ["status"])
    op.create_index("ix_feeds_last_fetched", "feeds", ["last_fetched_at"])

    # --- user_feeds ---
    op.create_table(
        "user_feeds",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("feed_id", sa.Integer, sa.ForeignKey("feeds.id", ondelete="CASCADE"), nullable=False),
        sa.Column("folder_id", sa.Integer, sa.ForeignKey("folders.id", ondelete="SET NULL")),
        sa.Column("custom_title", sa.String(255)),
        sa.Column("description", sa.Text),
        sa.Column("extract_readable", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("unread_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("purge_after_days", sa.SmallInteger),
        sa.Column("purge_keep_count", sa.SmallInteger),
        sa.Column("position", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_user_feeds_user_feed", "user_feeds", ["user_id", "feed_id"], unique=True)
    op.create_index("ix_user_feeds_user_id", "user_feeds", ["user_id"])
    op.create_index("ix_user_feeds_feed_id", "user_feeds", ["feed_id"])
    op.create_index("ix_user_feeds_user_folder", "user_feeds", ["user_id", "folder_id"])

    # --- articles ---
    op.create_table(
        "articles",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("feed_id", sa.Integer, sa.ForeignKey("feeds.id", ondelete="SET NULL")),
        sa.Column("guid", sa.String(2048), nullable=False),
        sa.Column("guid_hash", sa.String(64), nullable=False),
        sa.Column("url", sa.String(2048)),
        sa.Column("title", sa.String(1000), nullable=False),
        sa.Column("author", sa.String(255)),
        sa.Column("content", sa.Text),
        sa.Column("content_source", sa.String(20)),
        sa.Column("readable_content", sa.Text),
        sa.Column("readable_status", sa.String(10), nullable=False, server_default="skipped"),
        sa.Column("readable_retries", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("readable_next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("summary", sa.Text),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("estimated_read_min", sa.SmallInteger),
        sa.Column("word_count", sa.Integer),
        sa.Column("image_url", sa.String(2048)),
        sa.Column("ai_summary", sa.Text),
        sa.Column("ai_score", sa.Float),
        sa.Column("ai_tags_suggested", ARRAY(sa.String)),
        sa.Column("ai_processed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "readable_status IN ('pending', 'success', 'failed', 'skipped')",
            name="ck_articles_readable_status",
        ),
    )
    op.execute(
        "CREATE UNIQUE INDEX ix_articles_feed_guid ON articles (feed_id, guid_hash) WHERE feed_id IS NOT NULL"
    )
    op.create_index("ix_articles_feed_published", "articles", ["feed_id", sa.text("published_at DESC")])
    op.execute(
        "CREATE INDEX ix_articles_fts ON articles USING GIN "
        "(to_tsvector('simple', immutable_unaccent(title) || ' ' || immutable_unaccent(COALESCE(content, ''))))"
    )

    # --- user_article_states ---
    op.create_table(
        "user_article_states",
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("article_id", sa.BigInteger, sa.ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("is_read", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("is_starred", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("is_archived", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("is_hidden", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.Column("share_token", sa.String(32)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_uas_user_read", "user_article_states", ["user_id", "is_read", "article_id"])
    op.create_index("ix_uas_user_starred", "user_article_states", ["user_id", "is_starred"])
    op.create_index("ix_uas_user_archived", "user_article_states", ["user_id", "is_archived"])
    op.execute(
        "CREATE UNIQUE INDEX ix_uas_share_token ON user_article_states (share_token) WHERE share_token IS NOT NULL"
    )

    # --- labels ---
    op.create_table(
        "labels",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("color", sa.String(7), nullable=False, server_default="#6366f1"),
        sa.Column("position", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_labels_user_name", "labels", ["user_id", "name"], unique=True)

    # --- article_labels ---
    op.create_table(
        "article_labels",
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("article_id", sa.BigInteger, sa.ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("label_id", sa.Integer, sa.ForeignKey("labels.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("assigned_by_filter", sa.Boolean, nullable=False, server_default="false"),
    )
    op.create_index("ix_article_labels_user_label", "article_labels", ["user_id", "label_id"])
    op.create_index("ix_article_labels_article", "article_labels", ["article_id"])

    # --- filters ---
    op.create_table(
        "filters",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("match_operator", sa.String(5), nullable=False, server_default="AND"),
        sa.Column("position", sa.SmallInteger, nullable=False, server_default="0"),
        sa.Column("stop_on_match", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_filters_user_id", "filters", ["user_id"])
    op.create_index("ix_filters_user_active_pos", "filters", ["user_id", "is_active", "position"])

    # --- filter_conditions ---
    op.create_table(
        "filter_conditions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("filter_id", sa.Integer, sa.ForeignKey("filters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("field", sa.String(30), nullable=False),
        sa.Column("operator", sa.String(20), nullable=False),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("position", sa.SmallInteger, nullable=False, server_default="0"),
    )
    op.create_index("ix_filter_conditions_filter_id", "filter_conditions", ["filter_id"])

    # --- filter_actions ---
    op.create_table(
        "filter_actions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("filter_id", sa.Integer, sa.ForeignKey("filters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("action_type", sa.String(30), nullable=False),
        sa.Column("action_value", sa.Text),
    )
    op.create_index("ix_filter_actions_filter_id", "filter_actions", ["filter_id"])

    # --- fetch_logs ---
    op.create_table(
        "fetch_logs",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("feed_id", sa.Integer, sa.ForeignKey("feeds.id", ondelete="CASCADE"), nullable=False),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("http_status", sa.SmallInteger),
        sa.Column("error_message", sa.Text, nullable=False),
    )
    op.create_index("ix_fetch_logs_feed_failed", "fetch_logs", ["feed_id", sa.text("failed_at DESC")])

    # --- ai_profiles (Phase 2, created now) ---
    op.create_table(
        "ai_profiles",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("purpose", sa.String(30), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("api_key_encrypted", sa.Text),
        sa.Column("max_tokens", sa.Integer, server_default="1000"),
        sa.Column("summary_language", sa.String(10), server_default="cs"),
        sa.Column("is_active", sa.Boolean, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # --- user_ai_keys (Phase 2, created now) ---
    op.create_table(
        "user_ai_keys",
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("provider", sa.String(30), primary_key=True),
        sa.Column("api_key_encrypted", sa.Text, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # --- updated_at triggers ---
    op.execute("""
        CREATE OR REPLACE FUNCTION update_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)

    for table in ("users", "feeds", "user_feeds", "filters", "app_settings"):
        op.execute(f"""
            CREATE TRIGGER trg_{table}_updated_at
            BEFORE UPDATE ON {table}
            FOR EACH ROW EXECUTE FUNCTION update_updated_at()
        """)


def downgrade() -> None:
    for table in ("users", "feeds", "user_feeds", "filters", "app_settings"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_updated_at ON {table}")

    op.execute("DROP FUNCTION IF EXISTS update_updated_at")

    op.drop_table("user_ai_keys")
    op.drop_table("ai_profiles")
    op.drop_table("fetch_logs")
    op.drop_table("filter_actions")
    op.drop_table("filter_conditions")
    op.drop_table("filters")
    op.drop_table("article_labels")
    op.drop_table("labels")
    op.drop_table("user_article_states")
    op.drop_table("articles")
    op.drop_table("user_feeds")
    op.drop_table("feeds")
    op.drop_table("folders")
    op.drop_table("audit_log")
    op.drop_table("app_settings")
    op.drop_table("invitations")
    op.drop_table("password_reset_tokens")
    op.drop_table("api_tokens")
    op.drop_table("user_settings")
    op.drop_table("users")

    op.execute("DROP FUNCTION IF EXISTS immutable_unaccent(text)")
    op.execute("DROP EXTENSION IF EXISTS unaccent")
