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


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--user-id", type=int, required=True)
    ap.add_argument("--since", required=True,
                    help="ISO date; start of the sample window (state created_at)")
    ap.add_argument("--until", default=None, help="ISO date; optional upper bound")
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

        meta = {
            "type": "meta",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "user_id": args.user_id,
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

        result = await session.stream(text("""
            SELECT s.article_id, s.created_at, s.ai_score,
                   s.user_starred, s.dwell_seconds, s.link_opened, s.is_read,
                   a.title, a.url, a.feed_id, a.published_at,
                   a.readable_content, a.content
            FROM user_article_states s
            JOIN articles a ON a.id = s.article_id
            WHERE s.user_id = :uid AND s.ai_score IS NOT NULL
              AND a.trimmed_at IS NULL
              AND s.created_at >= :since
              AND (CAST(:until AS timestamptz) IS NULL OR s.created_at < :until)
            ORDER BY s.created_at
        """), {"uid": args.user_id, "since": since, "until": until})

        written = 0
        async for row in result:
            body = plain_body(row.readable_content or row.content, BODY_EXPORT_CHARS)
            print(json.dumps({
                "article_id": row.article_id,
                "state_created_at": iso(row.created_at),
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
          f"(scored={counts.scored} usable={counts.usable} engaged={counts.engaged})",
          file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
