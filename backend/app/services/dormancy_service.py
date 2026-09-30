"""Dormant accounts: feeds nobody reads any more stop being fetched.

An account is dormant when all of these hold:

- it is active (a deactivated account keeps nothing fetched, dormant or not) and not
  an admin,
- it has at least one feed (without one there is nothing to save, and a warning
  about feeds would be nonsense),
- it has no briefing switched on (a briefing is reading by email),
- its activity is older than ``dormant_after_days``,
- with the warning email on: the warning went out after that activity, and more
  than ``WARNING_LEAD`` ago.

Activity is the later of ``last_active_at`` (or ``created_at``, never active) and the
last use of any API token still valid, so a client that only reads over the API
keeps its account awake without a banner ever being seen.

The warning is never reset: it only counts while it is newer than the last
activity, so coming back cancels it by itself and the next absence earns a new one.

A feed is fetched while at least one subscriber is awake (active and not dormant).
That part holds with the setting off too: a deactivated account stopped keeping
feeds alive when this module was added.

Everything is a SQL expression over a ``User`` entity (or an alias of it), so the
scheduler, the warning job, the admin pages and the wake-up check read one rule.
"""
import asyncio
import logging
import smtplib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, false, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.auth import ApiToken
from app.models.feed import Feed, UserFeed
from app.models.settings import AppSettings
from app.models.user import User, UserCatchupConfig, UserSettings

logger = logging.getLogger(__name__)

# How long before an account falls dormant the warning goes out.
WARNING_LEAD = timedelta(days=7)
# Shortest setting the admin form accepts. Below a month a holiday would do it.
DORMANT_MIN_DAYS = 30
DORMANT_MAX_DAYS = 3650
# Warnings per run of the daily job, so the first run on a large instance does not
# run into the mail provider's sending limit. The rest follow on the next days.
WARNING_BATCH_LIMIT = 50
# Feed statuses the scheduler fetches (_select_due_feeds). Only such a feed can be
# stopped by its readers falling dormant.
FETCHED_STATUSES = ("active", "error")


@dataclass(frozen=True)
class DormancyPolicy:
    after_days: int | None = None
    warning_on: bool = False

    @property
    def enabled(self) -> bool:
        return bool(self.after_days)


def policy_from_settings(s: AppSettings | None) -> DormancyPolicy:
    """The policy an ``AppSettings`` row sets. The warning needs SMTP: without it
    accounts fall dormant unwarned even with the checkbox still on (an admin who
    removed SMTP later), the same as on an instance that never had mail."""
    if s is None:
        return DormancyPolicy()
    return DormancyPolicy(
        after_days=s.dormant_after_days or None,
        warning_on=bool(s.dormant_warning_enabled and s.smtp_host),
    )


async def load_policy(db: AsyncSession) -> DormancyPolicy:
    row = (await db.execute(
        select(
            AppSettings.dormant_after_days,
            AppSettings.dormant_warning_enabled,
            AppSettings.smtp_host,
        ).where(AppSettings.id == 1)
    )).one_or_none()
    if row is None:
        return DormancyPolicy()
    return DormancyPolicy(
        after_days=row.dormant_after_days or None,
        warning_on=bool(row.dormant_warning_enabled and row.smtp_host),
    )


# ── The rule, as SQL over a User entity ───────────────────────────────────────

def activity_sql(u=User):
    """Last activity of the account *u*: in the app, or through a valid API token.
    GREATEST skips NULLs in Postgres, so an account without tokens gets the first."""
    last_token_use = (
        select(func.max(ApiToken.last_used_at))
        .where(ApiToken.user_id == u.id, ApiToken.revoked_at.is_(None))
        .correlate(u)
        .scalar_subquery()
    )
    return func.greatest(func.coalesce(u.last_active_at, u.created_at), last_token_use)


def _has_feeds(u):
    # Aliased: this sits inside queries over user_feeds (the scheduler's gate), where
    # the bare table would correlate to the outer row and leave no FROM.
    uf = aliased(UserFeed)
    return select(uf.id).where(uf.user_id == u.id).correlate(u).exists()


def _has_briefing(u):
    return exists().where(
        UserCatchupConfig.user_id == u.id,
        UserCatchupConfig.briefing_enabled.is_(True),
    )


def _candidate_sql(u):
    """What an account needs before inactivity can put it to sleep at all."""
    return and_(
        u.is_active.is_(True),
        u.role != "admin",
        _has_feeds(u),
        not_(_has_briefing(u)),
    )


def dormant_sql(policy: DormancyPolicy, now: datetime, u=User):
    """True for a dormant account. See the module docstring for the rule."""
    if not policy.enabled:
        return false()
    activity = activity_sql(u)
    conds = [_candidate_sql(u), activity < now - timedelta(days=policy.after_days)]
    if policy.warning_on:
        conds.append(and_(
            u.pause_warning_sent_at > activity,
            u.pause_warning_sent_at < now - WARNING_LEAD,
        ))
    return and_(*conds)


def warned_sql(policy: DormancyPolicy, now: datetime, u=User):
    """Warned and counting down, not dormant yet (the admin's "dormant on <date>")."""
    if not (policy.enabled and policy.warning_on):
        return false()
    activity = activity_sql(u)
    return and_(
        _candidate_sql(u),
        u.pause_warning_sent_at > activity,
        u.pause_warning_sent_at >= now - WARNING_LEAD,
    )


def awake_sql(policy: DormancyPolicy, now: datetime, u=User):
    """An account whose subscriptions keep feeds fetched."""
    if not policy.enabled:
        return u.is_active.is_(True)
    return and_(u.is_active.is_(True), not_(dormant_sql(policy, now, u)))


def feed_has_awake_subscriber_sql(policy: DormancyPolicy, now: datetime, feed_id_col=Feed.id):
    """EXISTS an awake subscriber of the feed in *feed_id_col* (the scheduler's gate)."""
    sub = aliased(User)
    return (
        select(UserFeed.id)
        .join(sub, sub.id == UserFeed.user_id)
        .where(UserFeed.feed_id == feed_id_col, awake_sql(policy, now, sub))
        .exists()
    )


def has_unshared_feed_sql(policy: DormancyPolicy, now: datetime, u=User):
    """The account *u* follows at least one feed no other awake account follows,
    i.e. one its sleep actually stopped. Without it the welcome-back banner would tell
    a reader of shared feeds only that something was paused when nothing was. A feed
    that is not fetched anyway (disabled, paused by the admin) does not count."""
    own = aliased(UserFeed)
    own_feed = aliased(Feed)
    other = aliased(UserFeed)
    other_user = aliased(User)
    someone_else_awake = (
        select(other.id)
        .join(other_user, other_user.id == other.user_id)
        .where(
            other.feed_id == own.feed_id,
            other.user_id != u.id,
            awake_sql(policy, now, other_user),
        )
        .exists()
    )
    return (
        select(own.id)
        .join(own_feed, own_feed.id == own.feed_id)
        .where(
            own.user_id == u.id,
            own_feed.status.in_(FETCHED_STATUSES),
            not_(someone_else_awake),
        )
        .correlate(u)
        .exists()
    )


# ── Lookups ──────────────────────────────────────────────────────────────────

async def is_user_dormant(user_id: int, db: AsyncSession, now: datetime | None = None) -> bool:
    """Is this account dormant right now? Used to hold back AI work for readers who
    only share a feed with someone awake (the feed is fetched, they don't read it)."""
    policy = await load_policy(db)
    if not policy.enabled:
        return False
    now = now or datetime.now(timezone.utc)
    return bool(await db.scalar(select(dormant_sql(policy, now)).where(User.id == user_id)))


async def wakes_with_paused_feeds(user_id: int, db: AsyncSession, now: datetime | None = None) -> bool:
    """Is the account dormant, with at least one feed that is paused because of it?
    Asked just before its activity is recorded, to decide on the welcome-back banner."""
    policy = await load_policy(db)
    if not policy.enabled:
        return False
    now = now or datetime.now(timezone.utc)
    return bool(await db.scalar(
        select(and_(dormant_sql(policy, now), has_unshared_feed_sql(policy, now)))
        .where(User.id == user_id)
    ))


async def dormant_user_ids(db: AsyncSession, policy: DormancyPolicy, now: datetime) -> set[int]:
    if not policy.enabled:
        return set()
    return set((await db.scalars(select(User.id).where(dormant_sql(policy, now)))).all())


async def warned_users(db: AsyncSession, policy: DormancyPolicy, now: datetime) -> dict[int, datetime]:
    """Accounts counting down to dormancy, with the moment they fall asleep."""
    if not (policy.enabled and policy.warning_on):
        return {}
    rows = (await db.execute(
        select(User.id, User.pause_warning_sent_at).where(warned_sql(policy, now))
    )).all()
    return {uid: sent_at + WARNING_LEAD for uid, sent_at in rows}


async def unshared_feed_user_ids(db: AsyncSession, policy: DormancyPolicy, now: datetime,
                                 user_ids: set[int]) -> set[int]:
    """Of *user_ids*, those whose sleep stops at least one feed (for the admin tooltip)."""
    if not user_ids or not policy.enabled:
        return set()
    return set((await db.scalars(
        select(User.id).where(User.id.in_(user_ids), has_unshared_feed_sql(policy, now))
    )).all())


def dormant_feeds_sql(policy: DormancyPolicy, now: datetime):
    """Feeds with subscribers, none of them awake: not fetched until one comes back.
    Only feeds the scheduler would otherwise fetch; a disabled or paused one has its
    own status."""
    return and_(
        Feed.subscriber_count > 0,
        Feed.status.in_(FETCHED_STATUSES),
        not_(feed_has_awake_subscriber_sql(policy, now)),
    )


# ── Warning email ────────────────────────────────────────────────────────────

def _warning_due_sql(policy: DormancyPolicy, now: datetime):
    activity = activity_sql(User)
    return and_(
        _candidate_sql(User),
        User.email_verified.is_(True),
        or_(User.pause_warning_sent_at.is_(None), User.pause_warning_sent_at <= activity),
        activity < now - (timedelta(days=policy.after_days) - WARNING_LEAD),
    )


def _format_date(moment: datetime, tz_name: str | None) -> str:
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:
        tz = timezone.utc
    local = moment.astimezone(tz)
    return f"{local:%B} {local.day}, {local.year}"


def warning_email(display_name: str, after_days: int, pause_on: str,
                  public_url: str | None) -> tuple[str, str]:
    """Subject and plain-text body of the warning. Worded conditionally ("those of
    your feeds that nobody else here follows"): the mail goes out whether or not the
    account shares every feed with someone awake. Skipping those would leave them
    awake for good, and a shared feed would then never pause when its last other
    reader leaves."""
    subject = f"Readfine will stop updating some of your feeds on {pause_on}"
    if public_url:
        sign_in = f"Sign in here: {public_url.rstrip('/')}/login"
    else:
        sign_in = "Sign in to Readfine any time to keep them updated."
    body = (
        f"Hi {display_name},\n\n"
        "You haven't opened Readfine in a while. To save resources, Readfine stops "
        f"fetching feeds whose readers have all been inactive for more than {after_days} "
        f"days. From {pause_on}, that includes those of your feeds that nobody else "
        "here follows.\n\n"
        "Your feeds, folders, labels and filters stay as they are. Sign in any time, "
        "before or after that date, and fetching starts again.\n\n"
        f"{sign_in}\n\n"
        "If you don't plan to come back, you can delete your account in Settings.\n"
    )
    return subject, body


async def send_dormancy_warnings(db: AsyncSession, now: datetime | None = None,
                                 public_url: str | None = None) -> int:
    """Email the accounts that fall dormant in a week. Returns how many were stamped.

    A recipient the server refuses for good (5xx) is stamped anyway, or an abandoned
    account with a dead address would never fall asleep and would be retried daily.
    A temporary refusal (4xx) is skipped and retried on the next run. A connection or login failure stamps nothing and ends the run; tomorrow tries again.
    """
    from app.utils.smtp import send_email

    now = now or datetime.now(timezone.utc)
    s = (await db.execute(select(AppSettings).where(AppSettings.id == 1))).scalar_one_or_none()
    policy = policy_from_settings(s)
    if not (policy.enabled and policy.warning_on):
        return 0

    rows = (await db.execute(
        select(User, UserSettings.timezone)
        .outerjoin(UserSettings, UserSettings.user_id == User.id)
        .where(_warning_due_sql(policy, now))
        .order_by(activity_sql(User))
        .limit(WARNING_BATCH_LIMIT)
    )).all()

    stamped = 0
    pause_at = now + WARNING_LEAD
    for user, tz_name in rows:
        subject, body = warning_email(
            user.display_name, policy.after_days, _format_date(pause_at, tz_name), public_url,
        )
        try:
            await asyncio.to_thread(send_email, s, user.email, subject, body)
        except smtplib.SMTPRecipientsRefused as exc:
            # Raised for a 4xx too (greylisting, a full mailbox). That one may get
            # through tomorrow, so it is left unstamped and the account stays awake.
            if any(code < 500 for code, _ in exc.recipients.values()):
                logger.warning("Dormancy warning deferred for user %d: %s", user.id, exc)
                continue
            logger.warning("Dormancy warning refused for user %d: %s", user.id, exc)
        except (smtplib.SMTPException, OSError, ValueError) as exc:
            logger.error("Dormancy warnings stopped after %d: %s", stamped, exc)
            break
        user.pause_warning_sent_at = now
        await db.commit()
        stamped += 1
    if stamped:
        logger.info("Dormancy warnings: %d sent", stamped)
    return stamped
