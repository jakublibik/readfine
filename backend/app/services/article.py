"""Article service: listing, detail, state toggles, unread count management."""
import logging
import re
from datetime import date, datetime, timedelta, timezone

import regex as _regex
from sqlalchemy import Text, and_, cast, delete, func, literal, literal_column, null, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import TSQUERY, insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.article import SUPPRESSED_BY_BULK, Article, UserArticleState
from app.models.feed import Feed, UserFeed
from app.models.label import ArticleLabel, Label
from app.models.user import User
from app.schemas.article import ArticleListItem, ArticleResponse, ArticleStateUpdate
from app.services.relevance_service import CJK_CHARS, effective_score_sql, score_cut
from app.services.scope_tokens import parse_label_tokens, parse_scope_tokens
from app.utils.datetime_format import current_viewer_tz, format_local
from app.utils.text import strip_html

logger = logging.getLogger(__name__)


# ── article access (tenant isolation — single source of truth) ────────────────

def add_article_access_joins(stmt, user_id: int):
    """Outer-join UserFeed and UserArticleState for the access predicate.

    Both joins are user-scoped in their ON clause, so ``article_access_predicate``
    can decide visibility purely from whether a row matched. Pair the two: every
    article read/write path uses this + ``article_access_predicate`` so the access
    rule lives in exactly one place.
    """
    return (
        stmt
        .outerjoin(
            UserFeed,
            (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user_id),
        )
        .outerjoin(
            UserArticleState,
            (UserArticleState.article_id == Article.id) & (UserArticleState.user_id == user_id),
        )
    )


def unread_clause():
    """Row-level clause for "this reader has not read the article".

    For queries that outer-join ``UserArticleState`` on this user: no state row
    (NULL) is unread too, which is why it is not a plain ``is_read IS false``.
    """
    return UserArticleState.is_read.is_(None) | UserArticleState.is_read.is_(False)


def visible_article_clause():
    """Row-level clause for "the article shows in the UI": not a retention stub.

    A trimmed article is a body-stripped stub kept only for the interest profile.
    The list hides it, so every badge and count over a list must too, or the number
    stands above fewer rows than it claims.
    """
    return Article.trimmed_at.is_(None)


def permanently_kept_predicate():
    """Row-level clause for "this UserArticleState keeps its article for good".

    Starred, archived or saved by URL: the three ways a reader says an article is
    not disposable. One definition, because the same question is asked from four
    places that must not drift apart — access (below), retention
    (``purge_service._fully_protected_exists``), and the two points in
    ``services.feed`` where an article survives its feed being deleted.

    Safe to negate: every operand is NOT NULL or an ``IS NOT NULL`` test, so
    ``~permanently_kept_predicate()`` has no three-valued surprises.
    """
    return (
        UserArticleState.is_starred.is_(True)
        | UserArticleState.is_archived.is_(True)
        | UserArticleState.saved_at.is_not(None)
    )


def permanently_kept_exists(exclude_user_id: int | None = None):
    """Correlated EXISTS: *some* user keeps the current Article for good.

    Any-user semantics, so one reader starring or saving an article pins the row
    for the whole instance. Correlates on ``Article.id``, so the enclosing query
    must select from (or update/delete) ``articles``.

    ``exclude_user_id`` leaves one user out: an account being deleted still has its
    state rows until the cascade, and must not keep anything alive by them.
    """
    conds = [UserArticleState.article_id == Article.id, permanently_kept_predicate()]
    if exclude_user_id is not None:
        conds.append(UserArticleState.user_id != exclude_user_id)
    return (
        select(UserArticleState.article_id)
        .where(*conds)
        .correlate(Article)
        .exists()
    )


def article_access_predicate():
    """WHERE clause for "this user may act on this article".

    True when the user is subscribed to the article's feed, OR keeps the article
    for good — starred/archived (access survives unsubscribe / feed deletion) or
    saved by URL (such an article usually has no feed at all). Requires the query
    to have added ``add_article_access_joins(user_id)`` first — the user scoping
    lives in those joins' ON clauses, so this predicate only checks whether a row
    matched.
    """
    return UserFeed.id.is_not(None) | permanently_kept_predicate()


async def drop_unreachable_labels(db: AsyncSession, user_id: int, article_ids) -> None:
    """Delete this user's labels on those of ``article_ids`` they can no longer reach.

    A label view lists by ``ArticleLabel`` alone, so a label left on an article the
    reader lost access to keeps it in that list while the detail, the read toggle and
    mark-all-read all refuse it, and the label's unread badge never clears. Called
    wherever access is given up: unsubscribing, and taking the star, archive or save
    off an article that has no subscription behind it. ``article_ids`` is a list or a
    SELECT of ids. Does not commit; flush first, since the check reads the database.
    """
    subscribed = (
        select(UserFeed.id)
        .join(Article, Article.feed_id == UserFeed.feed_id)
        .where(Article.id == ArticleLabel.article_id, UserFeed.user_id == ArticleLabel.user_id)
        .exists()
    )
    kept = (
        select(UserArticleState.article_id)
        .where(
            UserArticleState.article_id == ArticleLabel.article_id,
            UserArticleState.user_id == ArticleLabel.user_id,
            permanently_kept_predicate(),
        )
        .exists()
    )
    await db.execute(
        delete(ArticleLabel).where(
            ArticleLabel.user_id == user_id,
            ArticleLabel.article_id.in_(article_ids),
            ~subscribed,
            ~kept,
        )
    )


_SNIPPET_LEN = 200


def _make_snippet(summary: str | None, content: str | None) -> str | None:
    """Return a plain-text snippet: summary if usable, otherwise content prefix."""
    for source in (summary, content):
        if not source:
            continue
        text = strip_html(source)
        if len(text) > 20:
            return text[:_SNIPPET_LEN].rsplit(" ", 1)[0] if len(text) > _SNIPPET_LEN else text
    return None


def body_permanently_empty(article: Article, extract_readable: bool | None) -> bool:
    """True when the article will never have a body to show, so the reader can be
    sent straight to the source.

    Deliberately narrower than the "nothing to render right now" branch in
    article_content.html, which also covers articles still being extracted. The two
    look alike but answer different questions and must not be merged.
    """
    if article.readable_status == "success" and article.readable_content:
        return False
    if article.content:
        return False
    if article.readable_status == "pending":
        # Extraction in flight, or waiting on retry backoff.
        return False
    if article.readable_status == "skipped" and extract_readable:
        # Opening the detail kicks off extraction (see htmx_article_detail).
        return False
    return True


# The scorers a search can filter and sort by, as the filter editor names them.
SCORE_SOURCES = ("ai", "basic", "relevance")

# The time windows the search offers, in days back from now. A request takes any
# number of days, so a view kept for later isn't bound to this menu.
SINCE_DAYS_OPTIONS = {
    1: "Last 24 hours", 3: "Last 3 days", 7: "Last 7 days", 30: "Last 30 days", 365: "Last 12 months",
}


def score_expr(source: str | None):
    """The score column a search reads: AI, basic, or AI else basic (the default,
    the same number the article list shows)."""
    if source == "ai":
        return UserArticleState.ai_score
    if source == "basic":
        return UserArticleState.lexical_score
    return effective_score_sql(UserArticleState)


def _format_date(dt: datetime | None) -> str:
    # Uses the per-request viewer timezone (set in the auth dependency).
    return format_local(dt, current_viewer_tz.get(), "short")


# Accent-folded and weighted: title A, summary B, body D. Each part goes in twice, as
# written ('simple') and stemmed ('english'), so "votes" finds "voting" while a query
# the English parser drops as stop words ("The Who") still finds its exact words.
# The 'simple' half also cuts Chinese, Japanese and Korean into overlapping character
# pairs (cjk_bigrams, migration 0117), and Thai, Lao, Khmer and Burmese with them
# (0125): the parser splits only on spaces and punctuation, so a whole sentence in
# these scripts was one word and nothing inside it could be found. The 'english'
# half is left alone, or every pair would be in twice.
# Must match the expression of idx_articles_search_fts (migration 0117) exactly, or
# searches stop using the index.
_FTS_TITLE = "immutable_unaccent(coalesce(articles.title, ''))"
_FTS_SUMMARY = "immutable_unaccent(coalesce(articles.summary, ''))"
_FTS_BODY = (
    "immutable_unaccent(coalesce(articles.content, '') || ' ' || "
    "coalesce(articles.readable_content, ''))"
)
_FTS_PARSE = {"simple": "cjk_bigrams({})", "english": "{}"}
_FTS_VECTOR = "(" + " || ".join(
    f"setweight(to_tsvector('{config}', {wrap.format(part)}), '{weight}')"
    for part, weight in ((_FTS_TITLE, "A"), (_FTS_SUMMARY, "B"), (_FTS_BODY, "D"))
    for config, wrap in _FTS_PARSE.items()
) + ")"

# A query term longer than this is not a search anyone typed. It also bounds the
# tsquery rewrite below, which selects one column per word: about 1,700 distinct words
# went past Postgres's 1,664-column limit and ended in a server error.
MAX_QUERY_LENGTH = 500

# A word ending in "*" matches every word it begins ("zpráv*" finds "zprávami"). Two
# letters at least: a one-letter prefix matches nearly everything and is slow.
# `regex`, whose `\w` includes combining marks: under `re` a word ending in a vowel
# sign ("சென்னை*") had no letter before the star and was searched as written.
_PREFIX_WORD = _regex.compile(r"(\w{2,})\*")
_STAR_AFTER_WORD = _regex.compile(r"(\w)\*")
_SINGLE_LEXEME = re.compile(r"^'([^']+)'$")

# What cjk_bigrams() cuts into pairs: CJK, and the Thai and Lao, Myanmar and Khmer
# blocks (migration 0125). Only search pairs the last four; the relevance score
# reads them as words it cannot split, see `relevance_service.tokenize`.
_PAIRED_CHARS = CJK_CHARS + "\u0e00-\u0eff\u1000-\u109f\u1780-\u17ff"

# A run of them in the query, with a "-" that negates it (at the start or after a
# space, not a hyphen inside a word) and a trailing star, which means nothing for
# pairs.
_CJK_QUERY_RUN = re.compile(rf"((?<!\S)-)?([{_PAIRED_CHARS}]+)\*?")


def split_cjk_query(q: str) -> tuple[str, list[tuple[str, bool]]]:
    """Rewrite the CJK in a search box input to match the bigrammed index.

    A CJK run becomes the phrase of its pairs ("人工智能" is "人工 工智 智能" in
    quotes), so it matches only where the characters stand together, as written.
    Inside quotes the pairs just join the phrase. Everything else is left as it was,
    so Latin words, "-word" and "or" keep working, also next to CJK ("OpenAI发布").

    A single character has no pair, and indexing single characters would grow the
    index again, so outside quotes it comes back separately as (char, negated), to be
    looked for in the title. Returns the rewritten query and those characters.
    """
    singles: list[tuple[str, bool]] = []

    def outside(m: re.Match) -> str:
        neg, run = m.group(1) or "", m.group(2)
        if len(run) == 1:
            singles.append((run, bool(neg)))
            return " "
        return f' {neg}"{_bigrams(run)}" '

    def inside(m: re.Match) -> str:
        return f" {m.group(1) or ''}{_bigrams(m.group(2))} "

    # websearch syntax: every other piece between double quotes is a phrase.
    parts = q.split('"')
    for i, part in enumerate(parts):
        parts[i] = _CJK_QUERY_RUN.sub(inside if i % 2 else outside, part)
    return '"'.join(parts), singles


def _bigrams(run: str) -> str:
    if len(run) == 1:
        return run
    return " ".join(run[i:i + 2] for i in range(len(run) - 1))


async def _search_tsquery(db: AsyncSession, q: str):
    """:func:`_build_search_tsquery`, once per session and query.

    One search page asks for the same tsquery up to three times (the list, its count,
    the story members in scope), and each build is two round trips.
    """
    cache = db.info.setdefault("search_tsquery", {})
    if q not in cache:
        cache[q] = await _build_search_tsquery(db, q)
    return cache[q]


async def _build_search_tsquery(db: AsyncSession, q: str):
    """The tsquery for a search box input: websearch syntax, accent-folded, each word
    matching as written or stemmed, and ``word*`` a prefix match.

    The query is parsed as written ('simple'), then every lexeme in it becomes
    ``('as written' | 'stemmed')``. Doing it per word keeps the query's own logic
    (every word, phrases, ``-word``): a stop word has no stemmed form and stays
    required as written, where ORing a whole stemmed query would have dropped it and
    turned "the who tour" into "tour".

    websearch_to_tsquery has no prefix syntax, so the stars are taken out before it
    parses the input, and the lexemes of the starred words are marked ``:*``, on both
    forms ("running*" is 'running':* | 'run':*). Lexemes and stems come from Postgres
    (lowered and unaccented as the vector is), so the rewrite finds them as the
    parser wrote them.
    """
    prefixes = _PREFIX_WORD.findall(q)
    folded = func.immutable_unaccent(_STAR_AFTER_WORD.sub(r"\1", q))
    written = func.websearch_to_tsquery("simple", folded)
    try:
        # Round-trip to PostgreSQL to catch malformed inputs before the full query
        row = (await db.execute(select(
            written.cast(Text),
            *(
                func.websearch_to_tsquery("simple", func.immutable_unaccent(word)).cast(Text)
                for word in prefixes
            ),
        ))).one()
    except Exception:
        logger.warning("websearch_to_tsquery failed for %r, falling back to plainto_tsquery", q)
        return func.plainto_tsquery("simple", folded)

    text_form = row[0]
    lexemes = list(dict.fromkeys(re.findall(r"'([^']+)'", text_form)))
    if not lexemes:
        return written
    starred = {m.group(1) for m in map(_SINGLE_LEXEME.match, row[1:]) if m}
    stems = (await db.execute(select(
        *(func.websearch_to_tsquery("english", lexeme).cast(Text) for lexeme in lexemes),
    ))).one()

    def expand(match: re.Match) -> str:
        lexeme = match.group(1)
        star = ":*" if lexeme in starred else ""
        stem = _SINGLE_LEXEME.match(stems[lexemes.index(lexeme)] or "")
        if stem is None or stem.group(1) == lexeme:
            return f"'{lexeme}'{star}"
        return f"( '{lexeme}'{star} | '{stem.group(1)}'{star} )"

    return cast(re.sub(r"'([^']+)'", expand, text_form), TSQUERY)


async def list_articles(
    user: User,
    db: AsyncSession,
    feed_id: int | None = None,
    folder_id: int | None = None,
    scope_include: str | None = None,
    label_id: int | None = None,
    label_filter: str | None = None,
    unread_only: bool = False,
    read_status: str | None = None,
    starred_only: bool = False,
    archived_only: bool = False,
    saved_only: bool = False,
    labeled_only: bool = False,
    story_id: int | None = None,
    story_ids: list[int] | None = None,
    q: str | None = None,
    sort_order: str = "newest",
    score_source: str | None = None,
    score_op: str | None = None,
    score_val: float | None = None,
    since_days: int | None = None,
    search: bool = False,
    limit: int = 50,
    offset: int = 0,
    cursor_ts: datetime | None = None,
    cursor_id: int | None = None,
    cursor_key: float | None = None,
    _count=None,
    _ids: bool = False,
) -> list[ArticleListItem]:
    """Return articles visible to the user with their read/star state.

    ``search`` marks a search view, with or without a query term. It searches what
    a text search does (subscriptions plus what the reader keeps for good), so
    adding a word to a pure filter view never widens what it can find.

    ``_count`` is for ``count_articles`` only: a count expression to take over the
    same filters instead of the rows, so the two can't disagree on what matches.
    ``_ids`` likewise returns the unexecuted SELECT of matching article ids, for a
    bulk write over exactly what the list shows (mark a saved search read).
    """
    # State/label views are anchored on user-owned state (star, archive, save, label)
    # that outlives the feed subscription: the feed may be unsubscribed or deleted
    # (Article.feed_id NULL), and a saved-by-URL article never had one, so the
    # UserFeed/Feed joins must be optional. The user-scoping — and thus tenant
    # isolation — comes from those anchors (UserArticleState.user_id /
    # ArticleLabel.user_id below), never from the feed join, so dropping the
    # subscription requirement here cannot leak other users' articles. Feed-browsing
    # views instead require an active subscription.
    #
    # Full-text search joins that same club, but for a different reason and with a
    # different safety net. It has no state anchor of its own — nothing like
    # `is_starred == True` to scope it — so it must carry ``article_access_predicate``
    # explicitly below. Without the optional join a saved-by-URL article could never
    # be found by search at all (it has no feed, so the inner join drops it), which
    # defeats the point of saving it; the same was quietly true of starred/archived
    # articles left orphaned by an unsubscribe.
    #
    # Asking for one story's members is the third kind. Like search it has no anchor
    # of its own — a story is global, built across every feed in the instance — so it
    # carries ``article_access_predicate`` below and must keep the optional joins, or
    # a member the reader keeps only through a star would drop out of its own group.
    searching = bool(q and q.strip()) or search
    feed_optional = (
        starred_only or archived_only or saved_only
        or label_id is not None or labeled_only or searching
        or story_id is not None or bool(story_ids)
    )
    stmt = select(
        Article,
        UserArticleState,
        Feed.title.label("feed_title"),
        UserFeed.custom_title.label("custom_title"),
        UserFeed.extract_readable.label("extract_readable"),
    )
    uas_join = (UserArticleState.article_id == Article.id) & (UserArticleState.user_id == user.id)
    uf_join = (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user.id)
    if feed_optional:
        # UserArticleState stays an outer join: a labeled article may have no state
        # row yet, and for starred/archived the `is_*` filters below make an outer
        # join equivalent to an inner one.
        stmt = (
            stmt
            .outerjoin(UserArticleState, uas_join)
            .outerjoin(Feed, Feed.id == Article.feed_id)
            .outerjoin(UserFeed, uf_join)
        )
    else:
        # Normal view: user must be subscribed to the feed
        stmt = (
            stmt
            .join(UserFeed, uf_join)
            .join(Feed, Feed.id == Article.feed_id)
            .outerjoin(UserArticleState, uas_join)
        )

    if searching or story_id is not None or story_ids:
        # The branches above with no anchor of their own. This is what keeps them
        # user-scoped, so it must not be dropped or narrowed: the joins are outer
        # here, and without it search would match every article in the table and a
        # story would hand over the feeds this reader never subscribed to.
        stmt = stmt.where(article_access_predicate())

    if story_id is not None:
        stmt = stmt.where(Article.story_id == story_id)

    # The same question for a page's worth of stories at once, which is what lets the
    # list find out how much of each group its own filters would give back without
    # asking once per row.
    if story_ids:
        stmt = stmt.where(Article.story_id.in_(story_ids))

    # Retention-trimmed articles are body-stripped stubs kept only for the interest
    # profile — never shown in the UI.
    stmt = stmt.where(visible_article_clause())

    if feed_id is not None:
        stmt = stmt.where(Article.feed_id == feed_id)

    if folder_id is not None:
        if folder_id == 0:
            # "No folder" means a subscription without one. Under the outer joins an
            # article with no subscription at all has a NULL folder too, and must
            # not pass for uncategorized.
            stmt = stmt.where(UserFeed.id.is_not(None), UserFeed.folder_id == None)
        else:
            stmt = stmt.where(UserFeed.folder_id == folder_id)

    # Multi-select scope (same JSON format as filters/catchup: ["feed:1","folder:2"]).
    # Empty lists mean "all feeds" — no restriction. A scope names subscriptions, so
    # it requires one: under the outer joins that is what keeps a kept article from
    # a feed the reader left, or one saved by URL, out of it (and "folder:0" from
    # matching it on its NULL folder). Unknown ids simply match nothing.
    if scope_include:
        scope_feed_ids, scope_folder_ids = parse_scope_tokens(scope_include)
        if scope_feed_ids or scope_folder_ids:
            clauses = []
            if scope_feed_ids:
                clauses.append(Article.feed_id.in_(scope_feed_ids))
            for fid in scope_folder_ids:
                if fid == 0:
                    clauses.append(UserFeed.folder_id.is_(None))
                else:
                    clauses.append(UserFeed.folder_id == fid)
            stmt = stmt.where(UserFeed.id.is_not(None), or_(*clauses))

    if label_id is not None:
        stmt = stmt.join(
            ArticleLabel,
            (ArticleLabel.article_id == Article.id)
            & (ArticleLabel.user_id == user.id)
            & (ArticleLabel.label_id == label_id),
        )
    elif labeled_only:
        stmt = stmt.where(
            select(ArticleLabel.article_id)
            .where(
                (ArticleLabel.article_id == Article.id)
                & (ArticleLabel.user_id == user.id)
            )
            .exists()
        )

    # Search label filter (multi-select): "any" = has at least one label,
    # otherwise articles carrying at least one of the selected labels.
    if label_filter:
        any_label, lf_ids = parse_label_tokens(label_filter)
        cond = (ArticleLabel.article_id == Article.id) & (ArticleLabel.user_id == user.id)
        if any_label:
            stmt = stmt.where(select(ArticleLabel.article_id).where(cond).exists())
        elif lf_ids:
            stmt = stmt.where(
                select(ArticleLabel.article_id)
                .where(cond & ArticleLabel.label_id.in_(lf_ids))
                .exists()
            )

    if unread_only:
        stmt = stmt.where(unread_clause())

    # Search status filter: "unread" / "read" go by the read flag, which scrolling
    # past and mark-all-read set too. "engaged" / "not_engaged" go by what the reader
    # actually did, the Stats definition of read: long enough in front of it, or the
    # original opened. Anything else = all.
    if read_status == "unread":
        stmt = stmt.where(unread_clause())
    elif read_status == "read":
        stmt = stmt.where(UserArticleState.is_read == True)
    elif read_status in ("engaged", "not_engaged"):
        from app.services.story_service import ENGAGED_DWELL_SECONDS
        engaged = (
            (UserArticleState.dwell_seconds >= ENGAGED_DWELL_SECONDS)
            | UserArticleState.link_opened.is_(True)
        )
        # No state row (outer join) means never opened: NULL counts as not engaged.
        engaged = func.coalesce(engaged, False)
        stmt = stmt.where(engaged if read_status == "engaged" else ~engaged)

    if starred_only:
        stmt = stmt.where(UserArticleState.is_starred == True)

    if archived_only:
        stmt = stmt.where(UserArticleState.is_archived == True)

    if saved_only:
        stmt = stmt.where(UserArticleState.saved_at.is_not(None))

    # Search score condition and the "score" sort, over the scorer the reader picked.
    # An article with no score from it (NULL) fails either comparison, so it never
    # matches a condition and sorts last. score_val is on the 0–100 scale the list
    # shows, and is held against the number shown there, which is rounded: a row
    # reading 70 (stored 0.696) is "at least 70", not "below 70".
    #
    # The user_id condition changes no result, since a score lives on the state row and
    # an article without one never matches, but the query needs it to be fast: the
    # score is a coalesce, which Postgres does not treat as rejecting NULLs, so without
    # it the outer join stays outer and every article on the instance is scanned
    # (144 ms against 4 ms on production data).
    score = score_expr(score_source)
    if score_op in ("gte", "lt") and score_val is not None:
        cut = score_cut(score_val) / 100
        stmt = stmt.where(
            UserArticleState.user_id == user.id,
            score >= cut if score_op == "gte" else score < cut,
        )

    # Search time window: the last N days, counted back from now, so a window kept
    # for later keeps moving with the calendar. On the date the list sorts by.
    if since_days:
        since = datetime.now(timezone.utc) - timedelta(days=since_days)
        stmt = stmt.where(func.coalesce(Article.published_at, Article.fetched_at) >= since)

    tsquery = None
    if q:
        q = q[:MAX_QUERY_LENGTH]  # the web list and the API take it unbounded
        fts_vec = literal_column(_FTS_VECTOR)
        fts_q, cjk_singles = split_cjk_query(q)
        # A lone CJK character ("猫") is not in the index, so it's looked for in the
        # titles of the articles the rest of the query leaves. Bodies would be too
        # slow (289 ms against 7 ms on dev), and a search of nothing else has no
        # rank, so it sorts by date.
        for char, negated in cjk_singles:
            stmt = stmt.where(~Article.title.contains(char) if negated else Article.title.contains(char))
        if fts_q.strip() or not cjk_singles:
            # The query is accent-folded like the vector, so "zpravy" finds "zprávy".
            tsquery = await _search_tsquery(db, fts_q)
            stmt = stmt.where(fts_vec.op('@@')(tsquery))

    if _count is not None:
        return (await db.execute(stmt.with_only_columns(_count))).scalar() or 0
    if _ids:
        return stmt.with_only_columns(Article.id)

    coalesced = func.coalesce(Article.published_at, Article.fetched_at)
    # Two sorts lead with something other than the date: the score, and for a text
    # search the rank (search's default; without a query term "relevance" is newest).
    # Normalization 1 divides the rank by 1 + log(length), so a long body doesn't
    # outrank a title match on the number of mentions alone.
    sort_key = None
    if sort_order == "score":
        sort_key = score
    elif tsquery is not None and sort_order not in ("newest", "oldest"):
        sort_key = func.ts_rank(fts_vec, tsquery, 1)

    # Every sort pages by keyset: the next page starts after the last row's sort
    # values, not after a number of rows. Rows marked read on scroll drop out of an
    # unread list between pages, and an offset would then skip as many rows as left.
    # The id breaks ties, matching ix_articles_sort_ts.
    has_cursor = cursor_ts is not None and cursor_id is not None
    if sort_key is not None:
        # The key goes out with the row, so the next page can start after it. A
        # score is NULL for articles the scorer hasn't seen; those sort last, and a
        # NULL cursor_key means the previous page already ended among them. A score
        # can change between pages when the terms do; the worst that does is show
        # or skip that one article, where an offset would shift the whole page.
        stmt = stmt.add_columns(sort_key.label("sort_key")).order_by(
            sort_key.desc().nulls_last(), coalesced.desc(), Article.id.desc(),
        )
        if has_cursor:
            after = tuple_(coalesced, Article.id) < tuple_(cursor_ts, cursor_id)
            if cursor_key is None:
                stmt = stmt.where(sort_key.is_(None), after)
            else:
                stmt = stmt.where(or_(
                    sort_key < cursor_key,
                    and_(sort_key == cursor_key, after),
                    sort_key.is_(None),
                ))
    elif sort_order == "oldest":
        stmt = stmt.order_by(coalesced.asc(), Article.id.asc())
        if has_cursor:
            stmt = stmt.where(tuple_(coalesced, Article.id) > tuple_(cursor_ts, cursor_id))
    else:
        stmt = stmt.order_by(coalesced.desc(), Article.id.desc())
        if has_cursor:
            stmt = stmt.where(tuple_(coalesced, Article.id) < tuple_(cursor_ts, cursor_id))
    stmt = stmt.limit(limit)
    # A cursor supersedes offset; offset stays for the REST API, which keeps
    # offset/limit semantics.
    if not has_cursor:
        stmt = stmt.offset(offset)

    rows = (await db.execute(stmt)).all()

    # Batch-fetch labels for all articles
    labels_by_article: dict[int, list[dict]] = {}
    if rows:
        article_ids = [row[0].id for row in rows]
        labels_rows = (await db.execute(
            select(ArticleLabel.article_id, Label.id, Label.name, Label.color)
            .join(Label, Label.id == ArticleLabel.label_id)
            .where(ArticleLabel.article_id.in_(article_ids), ArticleLabel.user_id == user.id)
            .order_by(ArticleLabel.article_id, Label.position, func.lower(Label.name))
        )).all()
        for aid, lid, lname, lcolor in labels_rows:
            labels_by_article.setdefault(aid, []).append({"id": lid, "name": lname, "color": lcolor})

    items = []
    for row in rows:
        article, state, feed_title, custom_title, extract_readable = row[:5]
        item = _to_list_item(
            article, state, feed_title, custom_title, extract_readable,
            labels_by_article.get(article.id, []),
        )
        if sort_key is not None:
            item.sort_key = row.sort_key
        items.append(item)
    return items


async def count_articles(user: User, db: AsyncSession, *, collapsing: bool, **filters) -> int:
    """How many rows ``list_articles`` would draw for these filters, all pages together.

    One row per story where the list folds them (``collapsing``), one per article
    otherwise; see ``story_service.row_count``. Takes the list's own filter arguments.
    """
    from app.services.story_service import row_count
    return await list_articles(user, db, _count=row_count(collapsing), **filters)


async def has_articles(user: User, db: AsyncSession, **filters) -> bool:
    """Whether ``list_articles`` would return any row for these filters.

    Asked as EXISTS over the unordered match, not as a one-row page of the list. Under
    the list's date ordering, LIMIT 1 lets the planner walk ix_articles_sort_ts, which
    covers every article on the instance, in the hope of an early match. In a view with
    no match (a label or feed with nothing unread) that walk reads the whole table:
    110 ms against 3 ms on 168k articles, growing with the instance rather than with
    the reader. Without the ordering the planner starts from the view's own rows.
    """
    stmt = await list_articles(user, db, _ids=True, **filters)
    return bool(await db.scalar(select(stmt.exists())))


def _to_list_item(
    article, state, feed_title, custom_title, extract_readable, labels: list[dict]
) -> ArticleListItem:
    """Build one list row from the columns every list query selects.

    Shared by the list and by ``get_article_list_item``, which re-renders a single
    row in place: a field added to ArticleListItem has to be filled in here, and
    only here, or the two would answer differently for the same article.
    """
    return ArticleListItem(
        id=article.id,
        feed_id=article.feed_id,
        feed_title=custom_title or feed_title,
        url=article.url,
        title=article.title,
        author=article.author,
        summary=article.summary,
        snippet=_make_snippet(article.summary, article.content),
        body_permanently_empty=body_permanently_empty(article, extract_readable),
        readable_active=article.readable_active,
        nothing_to_show=not (
            article.readable_content or article.content or article.summary
        ),
        published_at=article.published_at,
        formatted_date=_format_date(article.published_at or article.created_at),
        estimated_read_min=article.estimated_read_min,
        image_url=article.image_url,
        is_read=state.is_read if state else False,
        is_starred=state.is_starred if state else False,
        is_archived=state.is_archived if state else False,
        is_saved=bool(state and state.saved_at),
        ai_score=state.ai_score if state else None,
        lexical_score=state.lexical_score if state else None,
        labels=labels,
        # Only the group's identity. What the row says about it (how many other
        # sources, whether one was read) is user-scoped and gets annotated later.
        story_id=article.story_id,
        sort_ts=article.published_at or article.fetched_at,
    )


async def get_article_list_item(
    user: User, article_id: int, db: AsyncSession
) -> ArticleListItem | None:
    """One article in list-row form, for re-rendering a single row in place.

    ``get_article`` returns an ArticleResponse, which lacks the fields a row needs
    (snippet, formatted_date, body_permanently_empty), hence this sibling. Access
    goes through the shared predicate, so a saved article with no feed resolves the
    same way it does everywhere else.
    """
    stmt = (
        select(
            Article,
            UserArticleState,
            Feed.title.label("feed_title"),
            UserFeed.custom_title.label("custom_title"),
            UserFeed.extract_readable.label("extract_readable"),
        )
        .outerjoin(Feed, Feed.id == Article.feed_id)
        .where(Article.id == article_id, Article.trimmed_at.is_(None))
    )
    stmt = add_article_access_joins(stmt, user.id).where(article_access_predicate())
    row = (await db.execute(stmt)).first()
    if row is None:
        return None

    article, state, feed_title, custom_title, extract_readable = row
    return _to_list_item(
        article, state, feed_title, custom_title, extract_readable,
        await _fetch_labels(article.id, user.id, db),
    )


async def _fetch_labels(article_id: int, user_id: int, db: AsyncSession) -> list[dict]:
    rows = (await db.execute(
        select(ArticleLabel.label_id, Label.name, Label.color)
        .join(Label, Label.id == ArticleLabel.label_id)
        .where(ArticleLabel.article_id == article_id, ArticleLabel.user_id == user_id)
        .order_by(Label.position, func.lower(Label.name))
    )).all()
    return [{"id": r[0], "name": r[1], "color": r[2]} for r in rows]


async def get_article(user: User, article_id: int, db: AsyncSession) -> ArticleResponse | None:
    """Return article detail with user state. Returns None if not accessible.

    Access is granted if the user subscribes to the feed, OR has a starred/archived
    state for the article (remains accessible after unsubscribing). A retention stub
    is not an article any more (see visible_article_clause): a stale list or a link
    by id gets None, as it would for a deleted one.
    """
    stmt = add_article_access_joins(
        select(
            Article,
            UserArticleState,
            Feed.title.label("feed_title"),
            UserFeed.custom_title.label("custom_title"),
        ).outerjoin(Feed, Feed.id == Article.feed_id),
        user.id,
    ).where(
        Article.id == article_id,
        article_access_predicate(),
        visible_article_clause(),
    )
    row = (await db.execute(stmt)).first()
    if not row:
        return None

    article, state, feed_title, custom_title = row
    return _article_response(
        article, state, feed_title, custom_title,
        await _fetch_labels(article_id, user.id, db),
    )


async def mark_scope_read(
    user: User,
    db: AsyncSession,
    before: datetime,
    feed_id: int | None = None,
    folder_id: int | None = None,
    label_id: int | None = None,
    starred_only: bool = False,
    archived_only: bool = False,
    saved_only: bool = False,
    labeled_only: bool = False,
) -> None:
    """Bulk mark as read all articles in scope with fetched_at <= before.

    Starred/archived/saved scopes only UPDATE (state row is guaranteed to exist).
    All other scopes upsert to handle articles with and without existing state rows.

    Everything written here is stamped ``suppressed_by='bulk'``. Clearing a backlog is
    the reader saying they are not going to read these, which is the opposite of what
    the story suppression needs to hear: it hides an article for repeating one the
    reader read themselves, and without the stamp one press of "mark all read" over a
    few hundred articles would arm it against everything those articles were about.
    Reading one properly afterwards takes the stamp off again (the dwell and
    link-opened handlers do that), so this only ever withholds a signal that was never
    given.
    """
    now = datetime.now(timezone.utc)

    if starred_only or archived_only or saved_only:
        # Articles in these views already have a state row by definition – plain UPDATE suffices.
        # Drive the UPDATE from a subquery so we never materialize IDs into Python
        # (which previously blew past asyncpg's 32767-parameter limit on large feeds).
        if starred_only:
            filter_cond = UserArticleState.is_starred == True
        elif archived_only:
            filter_cond = UserArticleState.is_archived == True
        else:
            filter_cond = UserArticleState.saved_at.is_not(None)
        scope_articles = (
            select(Article.id)
            .join(
                UserArticleState,
                (UserArticleState.article_id == Article.id)
                & (UserArticleState.user_id == user.id),
            )
            .where(Article.fetched_at <= before, filter_cond)
        )
        await db.execute(
            update(UserArticleState)
            .where(
                UserArticleState.user_id == user.id,
                UserArticleState.article_id.in_(scope_articles),
                UserArticleState.is_read == False,
            )
            .values(is_read=True, read_at=now, suppressed_at=now, suppressed_by=SUPPRESSED_BY_BULK)
        )
        await db.commit()
        return

    # All other scopes: user must be subscribed; upsert to handle missing state rows.
    # A single INSERT … SELECT … ON CONFLICT keeps everything server-side — no IDs
    # round-trip through Python, so feed size is irrelevant.
    def scoped_select(*cols):
        q = (
            select(*cols)
            .join(UserFeed, (UserFeed.feed_id == Article.feed_id) & (UserFeed.user_id == user.id))
            .where(Article.fetched_at <= before)
        )
        if feed_id is not None:
            q = q.where(Article.feed_id == feed_id)
        elif folder_id is not None:
            q = q.where(UserFeed.folder_id.is_(None) if folder_id == 0 else UserFeed.folder_id == folder_id)
        elif label_id is not None:
            q = q.join(
                ArticleLabel,
                (ArticleLabel.article_id == Article.id)
                & (ArticleLabel.user_id == user.id)
                & (ArticleLabel.label_id == label_id),
            )
        elif labeled_only:
            q = q.where(
                select(ArticleLabel.article_id)
                .where((ArticleLabel.article_id == Article.id) & (ArticleLabel.user_id == user.id))
                .exists()
            )
        # else: no extra filter → all subscribed articles
        return q

    insert_select = scoped_select(
        literal(user.id), Article.id,
        literal(True), literal(False), literal(False), literal(now),
        literal(now), literal(SUPPRESSED_BY_BULK),
    )
    stmt = pg_insert(UserArticleState).from_select(
        ["user_id", "article_id", "is_read", "is_starred", "is_archived", "read_at",
         "suppressed_at", "suppressed_by"],
        insert_select,
    ).on_conflict_do_update(
        index_elements=["user_id", "article_id"],
        set_={"is_read": True, "read_at": now,
              "suppressed_at": now, "suppressed_by": SUPPRESSED_BY_BULK},
        where=(UserArticleState.__table__.c.is_read == False),
    )
    await db.execute(stmt)
    await db.commit()


async def filter_accessible_article_ids(
    user_id: int, article_ids: list[int], db: AsyncSession
) -> list[int]:
    """Return the subset of article_ids the user may act on.

    Access mirrors get_article: the article belongs to a subscribed feed, or the
    user has starred/archived it. Guards client-driven state writes (batch
    mark-read, dwell) against stale/crafted ids that fall outside the user's
    reading context and would otherwise skew their stats / AI preference.
    """
    if not article_ids:
        return []
    rows = await db.execute(
        add_article_access_joins(select(Article.id), user_id).where(
            Article.id.in_(article_ids),
            article_access_predicate(),
        )
    )
    return [r[0] for r in rows.all()]


async def _close_stories(user_id: int, article_ids: list[int], db: AsyncSession) -> None:
    """Read one article of a story, be done with the story.

    A folded row stands for the whole story, so finishing with it finishes with the
    rest. Unfolding it takes that back: the members are then rows of their own on
    screen, and closing the ones the reader just asked to see, while they are looking
    at them, is the opposite of what the gesture meant. Which of the two it is, only
    the browser knows, so it says so on the request and the callers pass it on.

    Imported here rather than at module level because story_service imports the access
    helpers from this module. Kept as one call so every human way of marking an article
    read — the scroll batch, the button, the API — closes a group the same way; the
    machine ways (URL dedup, the filter action) deliberately do not, or a filter could
    close stories nobody had looked at.
    """
    from app.services.story_service import mark_group_read
    await mark_group_read(user_id, article_ids, db)


async def _reopen_story(user_id: int, article_id: int, db: AsyncSession) -> None:
    """The other half of ``_close_stories``: un-read the row, un-read the story.

    Only the members closed on the reader's behalf come back — see
    ``story_service.reopen_group``. Gated by the same ``close_story`` flag as closing
    is, so a group the reader has unfolded is left alone in both directions: its
    members are rows of their own then, and each answers for itself.
    """
    from app.services.story_service import reopen_group
    await reopen_group(user_id, article_id, db)


async def mark_articles_read_batch(
    user: User, article_ids: list[int], db: AsyncSession,
    unfolded_ids: list[int] | None = None,
) -> None:
    """Mark specific articles as read in one upsert. Used by scroll-based batch mark-read.

    ``unfolded_ids`` are the ones whose story is open on screen, and they keep their
    story to themselves — see ``_close_stories``.
    """
    if not article_ids:
        return
    article_ids = await filter_accessible_article_ids(user.id, article_ids, db)
    if not article_ids:
        return
    unfolded = set(unfolded_ids or ())
    folded = [aid for aid in article_ids if aid not in unfolded]
    now = datetime.now(timezone.utc)
    stmt = pg_insert(UserArticleState).values([
        {"user_id": user.id, "article_id": aid, "is_read": True,
         "is_starred": False, "is_archived": False, "read_at": now}
        for aid in article_ids
    ]).on_conflict_do_update(
        index_elements=["user_id", "article_id"],
        # suppressed_at goes with the read it belongs to: a machine mark that the
        # reader has since undone and then read for real is a human read now, and
        # leaving the stamp behind would let it pass as a machine one forever.
        set_={"is_read": True, "read_at": now,
              "suppressed_at": null(), "suppressed_by": null()},
        where=(UserArticleState.__table__.c.is_read.is_not(True)),
    )
    await db.execute(stmt)
    await _close_stories(user.id, folded, db)
    await db.commit()


def _apply_star_side_effects(state, article, *, starred: bool, extract_readable: bool) -> None:
    """Star/unstar side effects shared by toggle_article_state and update_article_state
    so web and API star behave identically.

    Starring marks user intent (user_starred — a positive AI-preference signal),
    retention protection (ever_starred), starred_at, and triggers readable
    extraction. Unstarring snapshots dwell and treats an unstar within 60s as an
    accidental star (clears ever_starred)."""
    if starred:
        state.user_starred = True
        state.ever_starred = True
        state.starred_at = datetime.now(timezone.utc)
        # Starring something the machine had closed says the guess was wrong about
        # this one, so it stops being a machine read and counts as seen for real.
        state.suppressed_at = None
        state.suppressed_by = None
        if extract_readable and article.readable_status == "skipped":
            article.readable_status = "pending"
    else:
        state.unstar_dwell_seconds = state.dwell_seconds
        if state.starred_at and (datetime.now(timezone.utc) - state.starred_at).total_seconds() < 60:
            state.ever_starred = False


async def _load_article_for_write(user: User, article_id: int, db: AsyncSession):
    """Load an article the user may act on, plus their state (created if missing) and
    the display fields. Returns ``(article, state, feed_title, custom_title,
    extract_readable)`` or ``None`` when inaccessible. Shared by the toggle and
    update paths so both use one query and one access check."""
    stmt = add_article_access_joins(
        select(
            Article,
            UserArticleState,
            Feed.title.label("feed_title"),
            UserFeed.custom_title.label("custom_title"),
            UserFeed.extract_readable,
        ).outerjoin(Feed, Feed.id == Article.feed_id),
        user.id,
    ).where(
        Article.id == article_id,
        article_access_predicate(),
        visible_article_clause(),
    )
    row = (await db.execute(stmt)).first()
    if not row:
        return None
    article, state, feed_title, custom_title, extract_readable = row
    if state is None:
        # Two writes on an article with no state yet (a double click) would both add a
        # row, and the second would fail on the primary key. ON CONFLICT lets it wait
        # for the first and then pick up its row.
        await db.execute(
            pg_insert(UserArticleState)
            .values(user_id=user.id, article_id=article_id)
            .on_conflict_do_nothing()
        )
        state = await db.scalar(select(UserArticleState).where(
            UserArticleState.user_id == user.id,
            UserArticleState.article_id == article_id,
        ))
    return article, state, feed_title, custom_title, extract_readable


def _article_response(article, state, feed_title, custom_title, labels) -> ArticleResponse:
    """The ArticleResponse for one article and the reader's state (None when they have
    none yet). Shared by the detail read and the state writes: the writes used to build
    their own and left share_token, the AI fields, story_id and readable_active at
    their defaults, so a PATCH answered with less than a GET of the same article."""
    return ArticleResponse(
        id=article.id,
        feed_id=article.feed_id,
        feed_title=custom_title or feed_title,
        url=article.url,
        title=article.title,
        author=article.author,
        content=article.content,
        content_source=article.content_source,
        summary=article.summary,
        readable_content=article.readable_content,
        readable_status=article.readable_status,
        readable_error=article.readable_error,
        readable_active=article.readable_active,
        published_at=article.published_at,
        estimated_read_min=article.estimated_read_min,
        word_count=article.word_count,
        image_url=article.image_url,
        is_read=state.is_read if state else False,
        is_starred=state.is_starred if state else False,
        is_archived=state.is_archived if state else False,
        is_saved=bool(state and state.saved_at),
        read_at=state.read_at if state else None,
        share_token=state.share_token if state else None,
        ai_summary=state.ai_summary if state else None,
        ai_summary_truncated=state.ai_summary_truncated if state else False,
        ai_context=state.ai_context if state else None,
        ai_score=state.ai_score if state else None,
        lexical_score=state.lexical_score if state else None,
        story_id=article.story_id,
        labels=labels,
    )


async def toggle_article_state(
    user: User,
    article_id: int,
    field: str,
    db: AsyncSession,
    close_story: bool = True,
) -> ArticleResponse | None:
    """Toggle a single boolean field (is_read/is_starred/is_archived) in one DB round-trip.

    ``close_story=False`` when the article's story is unfolded on screen — see
    ``_close_stories``. It governs both directions: reading a folded row finishes the
    story, un-reading it brings the story back."""
    assert field in {"is_read", "is_starred", "is_archived"}
    loaded = await _load_article_for_write(user, article_id, db)
    if loaded is None:
        return None
    article, state, feed_title, custom_title, extract_readable = loaded

    new_value = not getattr(state, field, False)
    # Read before the stamp is cleared below: only a read the reader made themselves
    # can have closed a story, so only taking that one back reopens one.
    was_machine_read = state.suppressed_at is not None
    setattr(state, field, new_value)

    if field == "is_read":
        state.read_at = datetime.now(timezone.utc) if new_value else None
        state.suppressed_at = None
        state.suppressed_by = None
        if close_story and article.story_id is not None:
            await db.flush()
            if new_value:
                await _close_stories(user.id, [article_id], db)
            elif not was_machine_read:
                await _reopen_story(user.id, article_id, db)

    if field == "is_starred":
        _apply_star_side_effects(state, article, starred=new_value, extract_readable=bool(extract_readable))

    if field in ("is_starred", "is_archived") and not new_value:
        await db.flush()
        await drop_unreachable_labels(db, user.id, [article_id])

    await db.commit()
    await db.refresh(state)
    labels = await _fetch_labels(article_id, user.id, db)
    return _article_response(article, state, feed_title, custom_title, labels)


async def update_article_state(
    user: User,
    article_id: int,
    payload: ArticleStateUpdate,
    db: AsyncSession,
    close_story: bool = True,
) -> ArticleResponse | None:
    """Set is_read / is_starred / is_archived / is_saved from a payload. Creates
    UserArticleState if needed. One round-trip: load, apply, commit, respond from
    loaded data."""
    loaded = await _load_article_for_write(user, article_id, db)
    if loaded is None:
        return None
    article, state, feed_title, custom_title, extract_readable = loaded

    if payload.is_read is not None:
        # See toggle_article_state: the stamp says whether this read was the reader's.
        was_machine_read = state.suppressed_at is not None
        state.is_read = payload.is_read
        state.read_at = datetime.now(timezone.utc) if payload.is_read else None
        state.suppressed_at = None
        state.suppressed_by = None
        if close_story and article.story_id is not None:
            await db.flush()
            if payload.is_read:
                await _close_stories(user.id, [article_id], db)
            elif not was_machine_read:
                await _reopen_story(user.id, article_id, db)

    if payload.is_starred is not None:
        was_starred = bool(state.is_starred)
        state.is_starred = payload.is_starred
        if payload.is_starred != was_starred:
            _apply_star_side_effects(
                state, article, starred=payload.is_starred, extract_readable=bool(extract_readable)
            )

    if payload.is_archived is not None:
        state.is_archived = payload.is_archived

    if payload.is_saved is not None:
        # saved_at is a timestamp rather than a flag (retention reads it, and it is
        # what exempts the article from a purge), so the payload's boolean is turned
        # into one here. Nothing is fetched either way: this pins an article the user
        # can already reach, whereas save_article_by_url is what imports an address
        # and extracts it. Re-saving a feedless article dropped from Saved therefore
        # goes through that path and not this one, since unsaving it took away the
        # access this write needs.
        state.saved_at = datetime.now(timezone.utc) if payload.is_saved else None

    if False in (payload.is_starred, payload.is_archived, payload.is_saved):
        await db.flush()
        await drop_unreachable_labels(db, user.id, [article_id])

    await db.commit()
    await db.refresh(state)
    labels = await _fetch_labels(article_id, user.id, db)
    return _article_response(article, state, feed_title, custom_title, labels)
