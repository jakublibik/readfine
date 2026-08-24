"""Export one user's scored articles for the offline embedding-vs-LLM test.

Runs inside the app container (it needs `app.config` for the database URL and
`app.services.ai_jobs.normalize_content` so the exported text is byte-identical
to what the LLM scorer was given). Writes JSONL to stdout: the first line is a
meta object, every following line is one article.

    docker exec readfine-app-1 python /tmp/export_sample.py --user-id 1 \
        --since 2026-07-10 > sample.jsonl

The output contains personal data (profile text, reading history, article
bodies). Keep it out of any synced directory and delete it when the test is
done.
"""
import argparse
import asyncio
import html as _html
import json
import re
import sys
from datetime import datetime, timezone

import nh3
from sqlalchemy import text

import app.database as db
from app.config import settings

# Same limit the scorer uses (`ai_scoring_service._CONTENT_MAX_CHARS`). Bodies are
# exported slightly longer so that re-joining title + body and truncating to 2000
# reproduces `normalize_content` exactly.
CONTENT_MAX_CHARS = 2000
BODY_EXPORT_CHARS = 2400

_WHITESPACE_RE = re.compile(r"\s+")


def plain_body(content: str | None, limit: int) -> str:
    """`normalize_content` without the title prepended, so callers can recombine."""
    plain = nh3.clean(content or "", tags=set())
    plain = _html.unescape(plain)
    return _WHITESPACE_RE.sub(" ", plain).strip()[:limit]


def iso(value) -> str | None:
    return value.isoformat() if value else None


async def export_unscored(session, args, since, until) -> tuple[int, int]:
    """Articles in the user's feeds that never got a score (scenario C).

    Scoring is enqueued only when a non-AI filter applies a `label` action
    (`filter_service.py:581`), so the scored set is the one keyword and feed
    rules picked out. This exports the complement: what word matching missed,
    which is where a semantic score would have to earn its place.

    There is no engagement to measure against here and there never will be. The
    user is only shown labeled articles, so engagement on the rest is zero by
    construction regardless of how good they are. That is the exposure trap
    written down in the plan, and it is why scenario C is decided by reading a
    sample rather than by an AUC.

    Sampled at random rather than newest-first: the newest N would be whatever
    the last few fetch cycles brought in, which skews by feed and by topic.
    """
    result = await session.stream(text("""
        SELECT a.id AS article_id, a.title, a.url, a.feed_id,
               a.published_at, a.fetched_at, a.readable_content, a.content
        FROM articles a
        JOIN user_feeds uf ON uf.feed_id = a.feed_id AND uf.user_id = :uid
        LEFT JOIN user_article_states s
               ON s.article_id = a.id AND s.user_id = :uid
        WHERE s.ai_score IS NULL
          AND a.trimmed_at IS NULL
          AND a.fetched_at >= :since
          AND (CAST(:until AS timestamptz) IS NULL OR a.fetched_at < :until)
        ORDER BY random()
        LIMIT :limit
    """), {"uid": args.user_id, "since": since, "until": until,
           "limit": args.limit})

    written = empty_body = 0
    async for row in result:
        body = plain_body(row.readable_content or row.content, BODY_EXPORT_CHARS)
        empty_body += not body
        print(json.dumps({
            "article_id": row.article_id,
            "fetched_at": iso(row.fetched_at),
            "published_at": iso(row.published_at),
            "feed_id": row.feed_id,
            "title": row.title,
            "body": body,
            "has_readable": row.readable_content is not None,
            "url": row.url,
        }, ensure_ascii=False), flush=False)
        written += 1
    return written, empty_body


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--user-id", type=int, required=True)
    ap.add_argument("--since", required=True,
                    help="ISO date; start of the sample window (state created_at)")
    ap.add_argument("--until", default=None, help="ISO date; optional upper bound")
    ap.add_argument("--unscored", action="store_true",
                    help="export articles that never got a score instead (scenario "
                         "C: what the label-driven scope leaves out). Carries no "
                         "engagement to measure against, by construction")
    ap.add_argument("--limit", type=int, default=1500,
                    help="cap for --unscored, sampled at random across the window")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # article text is not ASCII

    since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    until = (datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc)
             if args.until else None)

    engine = db.create_engine(settings.database_url)
    factory = db.create_session_factory(engine)

    async with factory() as session:
        # Profile texts. `prev_text` is the profile that was live before the last
        # regeneration, i.e. the one the earlier segment was scored against.
        prof = (await session.execute(text("""
            SELECT ai_preference_text, ai_preference_prev_text,
                   ai_preference_updated_at, ai_preference_source,
                   ai_preference_auto_days
            FROM user_settings WHERE user_id = :uid
        """), {"uid": args.user_id})).one()

        # Profile-regime boundaries, straight from the usage log rather than memory.
        regen = (await session.execute(text("""
            SELECT created_at, provider, model
            FROM ai_usage_logs
            WHERE user_id = :uid AND operation = 'preference_generation'
            ORDER BY created_at DESC LIMIT 12
        """), {"uid": args.user_id})).all()

        # Sample-size check from the plan (step 1), over the same window.
        counts = (await session.execute(text("""
            SELECT count(*) AS scored,
                   count(*) FILTER (WHERE a.trimmed_at IS NULL) AS usable,
                   count(*) FILTER (WHERE a.trimmed_at IS NULL
                                      AND (s.user_starred
                                           OR s.dwell_seconds >= 60
                                           OR s.link_opened)) AS engaged,
                   min(s.created_at) AS first_state,
                   max(s.created_at) AS last_state
            FROM user_article_states s
            JOIN articles a ON a.id = s.article_id
            WHERE s.user_id = :uid AND s.ai_score IS NOT NULL
              AND s.created_at >= :since
              AND (CAST(:until AS timestamptz) IS NULL OR s.created_at < :until)
        """), {"uid": args.user_id, "since": since, "until": until})).one()

        # T1: past this age, purge keeps starred/archived articles whole, trims
        # merely-engaged ones (they drop out of this export, which filters on
        # trimmed_at) and deletes the rest. Anything older than T1 in the sample
        # is therefore a survivor, not a sample.
        purge_after_days = await session.scalar(text(
            "SELECT default_purge_after_days FROM app_settings WHERE id = 1"))

        meta = {
            "type": "meta",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "user_id": args.user_id,
            "kind": "unscored" if args.unscored else "scored",
            "purge_after_days": purge_after_days,
            "window": {"since": since.isoformat(), "until": iso(until)},
            "content_max_chars": CONTENT_MAX_CHARS,
            "profile": {
                "current": prof.ai_preference_text,
                "previous": prof.ai_preference_prev_text,
                "updated_at": iso(prof.ai_preference_updated_at),
                "source": prof.ai_preference_source,
                "auto_days": prof.ai_preference_auto_days,
            },
            "profile_regenerations": [
                {"at": iso(r.created_at), "provider": r.provider, "model": r.model}
                for r in regen
            ],
            "counts": {
                "scored": counts.scored,
                "usable": counts.usable,
                "engaged": counts.engaged,
                "first_state": iso(counts.first_state),
                "last_state": iso(counts.last_state),
            },
        }
        print(json.dumps(meta, ensure_ascii=False), flush=True)

        if args.unscored:
            written, empty_body = await export_unscored(session, args, since, until)
            await engine.dispose()
            print(f"exported {written} unscored articles "
                  f"(empty body={empty_body}); no engagement in this sample, "
                  f"scenario C is decided by reading the titles",
                  file=sys.stderr)
            return

        # The scoring job carries the time the score was actually written, which
        # is not the same as the state's created_at: applying a filter
        # retroactively enqueues scoring for older articles, and those would land
        # in the wrong profile regime if the sample were split by created_at.
        # `provider`/`model` exist only on jobs run after 2026-08-22 (migration
        # 0094), so they are NULL for the historical part of the sample.
        result = await session.stream(text("""
            SELECT s.article_id, s.created_at, s.ai_score,
                   s.user_starred, s.dwell_seconds, s.link_opened, s.is_read,
                   a.title, a.url, a.feed_id, a.published_at, a.fetched_at,
                   a.readable_content, a.content,
                   j.processed_at AS scored_at, j.status AS job_status,
                   j.provider AS job_provider, j.model AS job_model
            FROM user_article_states s
            JOIN articles a ON a.id = s.article_id
            LEFT JOIN article_ai_jobs j
                   ON j.article_id = s.article_id
                  AND j.user_id = s.user_id
                  AND j.operation = 'scoring'
            WHERE s.user_id = :uid AND s.ai_score IS NOT NULL
              AND a.trimmed_at IS NULL
              AND s.created_at >= :since
              AND (CAST(:until AS timestamptz) IS NULL OR s.created_at < :until)
            ORDER BY s.created_at
        """), {"uid": args.user_id, "since": since, "until": until})

        written = 0
        empty_body = 0
        no_job = 0
        async for row in result:
            body = plain_body(row.readable_content or row.content, BODY_EXPORT_CHARS)
            empty_body += not body
            no_job += row.scored_at is None
            print(json.dumps({
                "article_id": row.article_id,
                "state_created_at": iso(row.created_at),
                "fetched_at": iso(row.fetched_at),
                "scored_at": iso(row.scored_at),
                "job_status": row.job_status,
                "job_provider": row.job_provider,
                "job_model": row.job_model,
                "published_at": iso(row.published_at),
                "feed_id": row.feed_id,
                "ai_score": float(row.ai_score),
                "engaged": bool(row.user_starred or (row.dwell_seconds or 0) >= 60
                                or row.link_opened),
                "user_starred": bool(row.user_starred),
                "dwell_seconds": row.dwell_seconds,
                "link_opened": bool(row.link_opened),
                "is_read": bool(row.is_read),
                "title": row.title,
                "body": body,
                "has_readable": row.readable_content is not None,
                "url": row.url,
            }, ensure_ascii=False), flush=False)
            written += 1

    await engine.dispose()
    print(f"exported {written} articles "
          f"(scored={counts.scored} usable={counts.usable} engaged={counts.engaged}, "
          f"empty body={empty_body}, no scoring job={no_job})",
          file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
