"""Full-text matching and ranking.

Search folds accents on both sides (so "zpravy" finds "zprávy" and back), and ranks
a match in the title above one in the body. Chinese, Japanese and Korean are indexed
as character pairs, so a word inside an unspaced sentence is found.

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
from app.services.article import list_articles, split_cjk_query

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


async def test_star_after_a_vowel_sign(pg):
    """Tamil attaches case endings: "in Chennai" is one word starting "Chennai"."""
    tok = "zq" + uuid.uuid4().hex[:8]
    user, (locative, other) = await _setup(pg, [
        (f"சென்னையில் மழை {tok}", "<p>x</p>"), (f"மதுரை {tok}", "<p>x</p>"),
    ])
    found = {a.id for a in await list_articles(user=user, db=pg, q=f"சென்னை* {tok}")}
    assert found == {locative}


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


# ── CJK ────────────────────────────────────────────────────────────────────────

def test_split_cjk_query():
    # A run becomes the phrase of its pairs; Latin, "-" and "or" stay as they were.
    assert split_cjk_query("人工智能")[0].split() == ['"人工', "工智", '智能"']
    fts, singles = split_cjk_query("OpenAI发布 -芯片 or apple")
    assert fts.split() == ["OpenAI", '"发布"', '-"芯片"', "or", "apple"]
    assert singles == []
    # Inside quotes the pairs join the phrase that is already there.
    assert split_cjk_query('"OpenAI 发布会"')[0].split() == ['"OpenAI', "发布", "布会", '"']
    # A star after CJK means nothing for pairs and goes.
    assert split_cjk_query("芯片*")[0].split() == ['"芯片"']
    # One character comes back on its own, negated by a leading "-" only.
    fts, singles = split_cjk_query("猫 -狗 apple")
    assert fts.split() == ["apple"]
    assert singles == [("猫", False), ("狗", True)]
    assert split_cjk_query("no-猫")[1] == [("猫", False)]


async def test_cjk_word_inside_a_sentence(pg):
    tok = "zq" + uuid.uuid4().hex[:8]
    user, (zh, apart, ja, ko) = await _setup(pg, [
        (f"新闻 {tok}", "<p>我们讨论了人工智能的发展。</p>"),
        (f"新闻 {tok}", "<p>人工制品和智能手机</p>"),
        (f"ニュース {tok}", "<p>東京都の天気は晴れ</p>"),
        (f"뉴스 {tok}", "<p>서울 날씨가 좋다</p>"),
    ])

    async def found(q):
        return {a.id for a in await list_articles(user=user, db=pg, q=q)}

    # The characters have to stand together: "人工" and "智能" apart is no match.
    assert await found(f"人工智能 {tok}") == {zh}
    assert await found(f"智能 {tok}") == {zh, apart}
    assert await found(f"東京 {tok}") == {ja}
    assert await found(f"天気 {tok}") == {ja}
    # A Korean word with a particle attached.
    assert await found(f"날씨 {tok}") == {ko}
    assert await found(f"{tok} -智能") == {ja, ko}
    assert await found(f'"人工智能的" {tok}') == {zh}


async def test_thai_lao_khmer_burmese_word_inside_a_sentence(pg):
    """No spaces between words either; vowel signs end up inside the pairs."""
    tok = "zq" + uuid.uuid4().hex[:8]
    user, (th, lo, km, my) = await _setup(pg, [
        (f"ข่าว {tok}", "<p>วันนี้เราพูดถึงปัญญาประดิษฐ์และแมว</p>"),
        (f"news {tok}", "<p>ມື້ນີ້ມີຂ່າວດີ</p>"),
        (f"news {tok}", "<p>ថ្ងៃនេះមានព័ត៌មានល្អ</p>"),
        (f"news {tok}", "<p>ဒီနေ့သတင်းကောင်းရှိတယ်</p>"),
    ])

    async def found(q):
        return {a.id for a in await list_articles(user=user, db=pg, q=q)}

    assert await found(f"ปัญญาประดิษฐ์ {tok}") == {th}
    assert await found(f"ประดิษฐ์ {tok}") == {th}
    assert await found(f"ຂ່າວ {tok}") == {lo}
    assert await found(f"ព័ត៌មាន {tok}") == {km}
    assert await found(f"သတင်း {tok}") == {my}
    assert await found(f"{tok} -แมว") == {lo, km, my}
    assert await found(f"หมา {tok}") == set()


async def test_cjk_next_to_latin(pg):
    tok = "zq" + uuid.uuid4().hex[:8]
    user, (glued, other) = await _setup(pg, [
        (f"OpenAI发布新模型 {tok}", "<p>x</p>"), (f"OpenAI 公司 {tok}", "<p>x</p>"),
    ])
    found = {a.id for a in await list_articles(user=user, db=pg, q=f"OpenAI发布 {tok}")}
    assert found == {glued}


async def test_single_cjk_character_searches_titles(pg):
    tok = "zq" + uuid.uuid4().hex[:8]
    user, (cat, in_body, dog, older_cat) = await _setup(pg, [
        (f"我的猫 {tok}", "<p>x</p>"), (f"新闻 {tok}", "<p>猫</p>"),
        (f"狗 {tok}", "<p>x</p>"), (f"猫和狗 {tok}", "<p>x</p>"),
    ])

    async def found(q, **kw):
        return [a.id for a in await list_articles(user=user, db=pg, q=q, **kw)]

    assert set(await found(f"猫 {tok}")) == {cat, older_cat}
    assert set(await found(f"{tok} -猫")) == {in_body, dog}
    # With nothing else in the query there is no rank to sort by: newest first.
    assert await found("猫", sort_order="relevance") == [cat, older_cat]


async def test_very_long_query_is_cut_not_a_server_error(pg):
    """About 1,700 distinct words used to go past Postgres's 1,664-column limit in
    the tsquery rewrite. The query is cut to MAX_QUERY_LENGTH before it gets there."""
    tok = "zq" + uuid.uuid4().hex[:8]
    user, (aid,) = await _setup(pg, [(f"Title {tok}", "<p>x</p>")])
    q = tok + " " + " ".join(f"w{i:04d}" for i in range(1800))
    await list_articles(user=user, db=pg, q=q)  # no TooManyColumnsError
    assert {a.id for a in await list_articles(user=user, db=pg, q=tok)} == {aid}


async def test_tsquery_is_built_once_per_session_and_query(pg):
    from unittest.mock import patch
    from app.services import article as article_service
    tok = "zq" + uuid.uuid4().hex[:8]
    user, _ = await _setup(pg, [(f"Title {tok}", "<p>x</p>")])
    real = article_service._build_search_tsquery
    with patch.object(article_service, "_build_search_tsquery", side_effect=real) as build:
        await list_articles(user=user, db=pg, q=tok)
        await list_articles(user=user, db=pg, q=tok)
        await list_articles(user=user, db=pg, q=tok + " other")
    assert build.await_count == 2
