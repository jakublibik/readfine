from datetime import datetime
from pydantic import BaseModel, Field, field_validator


class ArticleStateUpdate(BaseModel):
    is_read: bool | None = None
    is_starred: bool | None = None
    is_archived: bool | None = None
    # Setting this False on an article with no feed takes away the only access to it,
    # so the article that comes back in the response is the last look a client gets:
    # the next GET answers 404. That is what the Saved view does too.
    is_saved: bool | None = None


class SaveUrlRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def url_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("URL cannot be empty")
        return v


class ArticleStateResponse(BaseModel):
    is_read: bool
    is_starred: bool
    is_archived: bool
    read_at: datetime | None

    model_config = {"from_attributes": True}


class _EffectiveScore:
    """`score` and `score_is_ai` for anything that carries both scorers' numbers.

    One definition for the list row and the story footer, so the two cannot show
    different numbers for the same article.
    """
    ai_score: float | None
    lexical_score: float | None

    @property
    def score(self) -> float | None:
        """The best score this article has, from either scorer."""
        from app.services.relevance_service import effective_score
        return effective_score(self.ai_score, self.lexical_score)[0]

    @property
    def score_is_ai(self) -> bool:
        """Whether `score` came from the model rather than from word matching."""
        from app.services.relevance_service import effective_score
        return effective_score(self.ai_score, self.lexical_score)[1]


class ArticleListItem(_EffectiveScore, BaseModel):
    id: int
    feed_id: int | None
    feed_title: str | None  # resolved from Feed or UserFeed.custom_title
    url: str | None
    title: str
    author: str | None
    summary: str | None
    snippet: str | None  # pre-computed: summary or stripped content prefix
    # "this article will never have a body", not "it has none right now" — an
    # extraction still in flight is not permanently empty. Computed when the list
    # renders, so a background extraction finishing afterwards leaves it stale.
    body_permanently_empty: bool = False
    # A first extraction attempt is still in flight. Only a saved-by-URL article's
    # row cares: it was inserted with a placeholder title and learns the real one
    # when extraction finishes, so the row polls until then.
    readable_active: bool = False
    # The article carries no text of its own: no extracted body, no feed content and
    # not even the page's own description. Deliberately about what there is to show
    # rather than about extraction having failed, because a saved-by-URL row only
    # needs marking while it is still nothing but the pasted address. One that failed
    # extraction but came back with a real title and a description reads like any
    # other row, and a warning on it would be noise; the reason and the retry live in
    # the article itself either way.
    nothing_to_show: bool = False
    published_at: datetime | None
    # Display-only string, formatted per the viewer's number/date format profile
    # (order/separators vary). Parse `published_at` (ISO) for machine use.
    formatted_date: str
    estimated_read_min: int | None
    image_url: str | None
    # state (None = no UserArticleState row yet = unread, not starred)
    is_read: bool
    is_starred: bool
    is_archived: bool
    is_saved: bool = False
    # Both scorers, kept apart. What the row shows is `score`, the better of the
    # two, and `score_is_ai` says which one that was: a template deciding it for
    # itself is how two views end up disagreeing about the same article.
    ai_score: float | None = None
    lexical_score: float | None = None
    labels: list[dict] = []  # [{"id": int, "name": str, "color": str}]
    # Story group this article belongs to, or None when nothing else covered it.
    story_id: int | None = None
    # How many other members of the group this reader may open, and how many of those
    # they have already read. Both are filled in by services.story_service while the
    # list renders, never by _to_list_item: they take a user-scoped query over the
    # whole group, and the group reaches past the page (a read member is missing from
    # an unread-only page, and the group can straddle the page boundary). Excluded
    # from API JSON, where nothing fills them in and a 0 would read as a fact.
    #
    # story_others counts what this view would give back on unfolding, which in a
    # filtered list is not the whole group: a label view unfolds the members carrying
    # that label and nothing else. story_total counts the group as it stands, which is
    # a fact about the news rather than about the filter, and is what tells a reader
    # that the one article their label caught is part of something bigger. The two are
    # equal in an unfiltered list.
    story_others: int = Field(default=0, exclude=True)
    story_total: int = Field(default=0, exclude=True)
    story_read: int = Field(default=0, exclude=True)
    # coalesce(published_at, fetched_at) used for keyset pagination cursor;
    # excluded from API JSON (internal pagination concern only)
    sort_ts: datetime | None = Field(default=None, exclude=True)

    model_config = {"from_attributes": False}

class ArticleResponse(BaseModel):
    id: int
    feed_id: int | None
    feed_title: str | None
    url: str | None
    title: str
    author: str | None
    content: str | None
    content_source: str | None
    # The page's own og:description, captured for feedless (saved) articles. Shown as
    # a clearly-marked fallback when extraction produced nothing to read.
    summary: str | None = None
    readable_content: str | None
    readable_status: str
    readable_error: str | None = None
    # True only while a first extraction attempt is in flight (status 'pending',
    # no retries yet). Drives the "Extracting…" spinner + poll; a pending article
    # waiting on retry-backoff is not "active" and must not poll/flash.
    readable_active: bool = False
    published_at: datetime | None
    estimated_read_min: int | None
    word_count: int | None
    image_url: str | None
    is_read: bool
    is_starred: bool
    is_archived: bool
    # Saved by URL by this user. Gates the "Remove from Saved" actions — deliberately
    # keyed on the article's own state, not on which view the reader came from.
    is_saved: bool = False
    read_at: datetime | None
    share_token: str | None = None
    ai_summary: str | None = None
    ai_summary_truncated: bool = False
    ai_context: str | None = None
    labels: list[dict] = []
    # Story group this article belongs to, or None when nothing else covered it. Only
    # says a group exists — how much of it this reader may see is a separate question
    # (services.story_service), since the grouping is global and feeds are not.
    story_id: int | None = None

    model_config = {"from_attributes": False}


class StoryMember(_EffectiveScore, BaseModel):
    """One other article covering the same story, as the reader footer shows it.

    Deliberately narrow: the footer lists coverage, it does not re-render article rows,
    and a member comes from a group built across all feeds, so anything selected here
    is one access mistake away from leaking another reader's subscriptions.
    """
    id: int
    title: str
    url: str | None
    feed_title: str | None
    published_at: datetime | None
    is_read: bool = False
    # Read, and read by this reader rather than closed on their behalf. Finishing a
    # story marks the rest of it read (story_service.mark_group_read), so is_read alone
    # says almost nothing in the footer: it is true of every member the moment the
    # reader is done with the article the footer hangs from. This is the one the footer
    # says "read" for, so the word answers "did I actually meet this one".
    read_by_reader: bool = False
    is_starred: bool = False
    ai_score: float | None = None
    lexical_score: float | None = None
    # Admin diagnostic only (list_members with title_norm): trigram similarity to the
    # open article and whether one headline reads as a follow-up of the other.
    similarity: float | None = None
    follow_up: bool = False

    model_config = {"from_attributes": False}


class SuppressedArticle(BaseModel):
    """One article the suppression rule kept out of the list, for the settings list.

    ``instead_of`` and ``match`` are reconstructed at render time rather than stored:
    what is kept is that the article was hidden, not which article decided it, and the
    counterpart is found again through the story the two share. So both are the best
    available account of what happened, not a record of it — see
    ``story_service.list_suppressed``.
    """
    id: int
    title: str
    feed_title: str | None
    hidden_at: datetime
    # False once the reader has read it after all, which is what takes the hiding off.
    # The row stays either way: it is a record of what was done, not of what still is.
    still_hidden: bool = True
    # The headline this was hidden for repeating, and how alike the two are. None when
    # that article is gone (unsubscribed, or taken by retention) — the row still stands,
    # because the counter above the list counts it either way.
    instead_of: str | None = None
    match: float | None = None

    model_config = {"from_attributes": False}
