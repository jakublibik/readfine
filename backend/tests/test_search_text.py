"""Full-text matching and ranking.

Search folds accents on both sides (so "zpravy" finds "zprávy" and back), and ranks
a match in the title above one in the body.

Runs against the real (dev) database inside a transaction that is always rolled
back. Skips automatically if the DB is unreachable.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings as app_settings
from app.models.article import Article
from app.models.feed import Feed, UserFeed
from app.models.user import User
from app.services.article import list_articles

NOW = datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def pg():
    engine = create_async_engine(app_settings.database_url)
    try:
        conn = await engine.connect()
    except Exception as exc:
        await engine.dispose()
        from tests.conftest import db_unreachable
        db_unreachable(exc)
    trans = await conn.begin()
    session = AsyncSession(bind=conn, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await conn.close()
        await engine.dispose()


async def _setup(session, articles):
    """A fresh user subscribed to one feed holding ``articles``, given as
    (title, body) pairs, the first one newest."""
    u = uuid.uuid4().hex[:12]
    user = User(email=f"fts_{u}@test.invalid", password_hash="x", display_name="t")
    session.add(user)
    await session.flush()
    feed = Feed(feed_url=f"https://ex.invalid/{u}.xml", title="t", subscriber_count=1)
    session.add(feed)
    await session.flush()
    session.add(UserFeed(user_id=user.id, feed_id=feed.id))
    ids = []
    for i, (title, body) in enumerate(articles):
        g = uuid.uuid4().hex
        when = NOW - timedelta(hours=i)
        a = Article(feed_id=feed.id, guid=g, guid_hash=g, title=title, content=body,
                    readable_status="success", published_at=when, fetched_at=when)
        session.add(a)
        await session.flush()
        ids.append(a.id)
    return user, ids


async def test_accents_fold_both_ways(pg):
    tok = "zq" + uuid.uuid4().hex[:8]
    user, (accented, plain) = await _setup(pg, [
        (f"Zprávy {tok}", "<p>x</p>"), (f"Zpravy {tok}", "<p>x</p>"),
    ])
    for q in (f"zpravy {tok}", f"zprávy {tok}", f"ZPRÁVY {tok}"):
        found = {a.id for a in await list_articles(user=user, db=pg, q=q)}
        assert found == {accented, plain}, q


async def test_title_match_ranks_above_body_mentions(pg):
    tok = "zq" + uuid.uuid4().hex[:8]
    # The body article is newer and says the word three times, and still ranks below.
    filler = " ".join(["lorem ipsum dolor sit amet"] * 40)
    user, (in_body, in_title) = await _setup(pg, [
        ("Something else", f"<p>{tok} {filler} {tok} {filler} {tok}</p>"),
        (f"All about {tok}", f"<p>{filler}</p>"),
    ])
    found = [a.id for a in await list_articles(user=user, db=pg, q=tok, sort_order="relevance")]
    assert found == [in_title, in_body]


async def test_star_matches_word_beginnings(pg):
    tok = "zq" + uuid.uuid4().hex[:8]
    user, (plural, instr, other) = await _setup(pg, [
        (f"Zprávy {tok}", "<p>x</p>"), (f"Se zprávami {tok}", "<p>x</p>"),
        (f"Zpracování {tok}", "<p>x</p>"),
    ])

    async def found(q):
        return {a.id for a in await list_articles(user=user, db=pg, q=q)}

    assert await found(f"zpráv* {tok}") == {plural, instr}
    # Accents fold inside a prefix too, and a shorter prefix reaches further.
    assert await found(f"zprav* {tok}") == {plural, instr}
    assert await found(f"zpra* {tok}") == {plural, instr, other}
    # No star, whole words only, as before.
    assert await found(f"zpráv {tok}") == set()
    # A starred word can be excluded like any other.
    assert await found(f"{tok} -zpráv*") == {other}


async def test_one_letter_star_is_a_plain_word(pg):
    tok = "zq" + uuid.uuid4().hex[:8]
    user, (art,) = await _setup(pg, [(f"Zprávy {tok}", "<p>x</p>")])
    assert await list_articles(user=user, db=pg, q=f"z* {tok}") == []


async def test_english_word_forms_match(pg):
    tok = "zq" + uuid.uuid4().hex[:8]
    user, (voting, other) = await _setup(pg, [
        (f"Voting opens {tok}", "<p>x</p>"), (f"Nothing here {tok}", "<p>x</p>"),
    ])
    for q in (f"votes {tok}", f"vote {tok}", f"voted {tok}"):
        found = {a.id for a in await list_articles(user=user, db=pg, q=q)}
        assert found == {voting}, q


async def test_english_stop_words_still_match_as_written(pg):
    """The stemmed half drops "the" and "who"; the written half keeps them."""
    tok = "zq" + uuid.uuid4().hex[:8]
    user, (band, other) = await _setup(pg, [
        (f"The Who on tour {tok}", "<p>x</p>"), (f"On tour {tok}", "<p>x</p>"),
    ])
    found = {a.id for a in await list_articles(user=user, db=pg, q=f"the who {tok}")}
    assert found == {band}


async def test_star_reaches_stemmed_forms(pg):
    """"running*" is 'running':* as written and 'run':* stemmed, so it finds "runs"."""
    tok = "zq" + uuid.uuid4().hex[:8]
    user, (runs, other) = await _setup(pg, [
        (f"She runs daily {tok}", "<p>x</p>"), (f"She walks {tok}", "<p>x</p>"),
    ])
    found = {a.id for a in await list_articles(user=user, db=pg, q=f"running* {tok}")}
    assert found == {runs}


async def test_phrase_keeps_its_order_with_stemmed_words(pg):
    tok = "zq" + uuid.uuid4().hex[:8]
    user, (in_order, reversed_) = await _setup(pg, [
        (f"Voting opens {tok}", "<p>x</p>"), (f"Opens voting {tok}", "<p>x</p>"),
    ])
    found = {a.id for a in await list_articles(user=user, db=pg, q=f'"votes open" {tok}')}
    assert found == {in_order}
