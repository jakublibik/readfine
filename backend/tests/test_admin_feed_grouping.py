"""Unit tests for the admin Feeds "group by host" ordering contract.

Ordering: named-with-error (host A–Z) → Other·errors → named-clean (host A–Z)
→ Other. Within every group: error feeds first, then title A–Z. Named groups
require ≥2 feeds on a host; single-feed hosts collapse into the Other buckets.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services.admin_service import group_feeds_by_host


def _feed(title: str, url: str, status: str = "active") -> dict:
    return {"feed": SimpleNamespace(title=title, feed_url=url, status=status), "article_count": 0}


def test_multi_feed_host_becomes_named_group_www_stripped():
    items = [
        _feed("B", "https://www.reddit.com/r/b/.rss"),
        _feed("A", "https://reddit.com/r/a/.rss"),
    ]
    groups = group_feeds_by_host(items)
    assert len(groups) == 1
    g = groups[0]
    assert g["kind"] == "named"
    assert g["host"] == "reddit.com"   # www. stripped, both feeds same host
    assert g["count"] == 2
    assert [i["feed"].title for i in g["feeds"]] == ["A", "B"]  # A–Z within group


def test_single_feed_hosts_collapse_into_other():
    items = [
        _feed("Zeta", "https://z.example/rss"),
        _feed("Alpha", "https://a.example/rss"),
    ]
    groups = group_feeds_by_host(items)
    assert len(groups) == 1
    assert groups[0]["kind"] == "other"
    assert groups[0]["label"] == "Other"
    assert [i["feed"].title for i in groups[0]["feeds"]] == ["Alpha", "Zeta"]  # A–Z


def test_error_singletons_split_into_other_errors_bucket():
    items = [
        _feed("clean1", "https://c1.example/rss"),
        _feed("bad1", "https://b1.example/rss", "error"),
        _feed("clean2", "https://c2.example/rss"),
    ]
    kinds = [g["kind"] for g in group_feeds_by_host(items)]
    # error singletons float up in their own bucket, clean ones stay at the bottom
    assert kinds == ["other_errors", "other"]
    err, clean = group_feeds_by_host(items)
    assert [i["feed"].title for i in err["feeds"]] == ["bad1"]
    assert [i["feed"].title for i in clean["feeds"]] == ["clean1", "clean2"]


def test_full_ordering_error_first_named_before_other():
    items = [
        # named group with an error (reddit) — must sort to the very top
        _feed("R-clean", "https://reddit.com/r/x/.rss"),
        _feed("R-err", "https://reddit.com/r/y/.rss", "error"),
        # named clean group (youtube)
        _feed("Y1", "https://youtube.com/feeds/videos.xml?x=1"),
        _feed("Y2", "https://youtube.com/feeds/videos.xml?x=2"),
        # singletons: one error, one clean
        _feed("solo-err", "https://s1.example/rss", "error"),
        _feed("solo-ok", "https://s2.example/rss"),
    ]
    groups = group_feeds_by_host(items)
    assert [(g["kind"], g.get("host") or g["label"]) for g in groups] == [
        ("named", "reddit.com"),        # named-with-error
        ("other_errors", "Other · errors"),
        ("named", "youtube.com"),        # named-clean
        ("other", "Other"),
    ]
    # error feed first within the reddit group
    assert [i["feed"].title for i in groups[0]["feeds"]] == ["R-err", "R-clean"]


def test_within_group_status_priority_error_disabled_paused_active():
    # Same host (≥2 feeds → named group); mixed statuses must order
    # error → disabled → paused → active, then title A–Z within a status.
    items = [
        _feed("z-active", "https://h.example/1", "active"),
        _feed("paused-one", "https://h.example/2", "paused"),
        _feed("disabled-one", "https://h.example/3", "disabled"),
        _feed("err-one", "https://h.example/4", "error"),
        _feed("a-active", "https://h.example/5", "active"),
    ]
    (group,) = group_feeds_by_host(items)
    assert group["kind"] == "named"
    assert [i["feed"].title for i in group["feeds"]] == [
        "err-one", "disabled-one", "paused-one", "a-active", "z-active",
    ]


def test_named_error_groups_sorted_az_among_themselves():
    items = [
        _feed("z1", "https://zeta.example/rss"),
        _feed("z2", "https://zeta.example/rss", "error"),
        _feed("a1", "https://alpha.example/rss"),
        _feed("a2", "https://alpha.example/rss", "error"),
    ]
    groups = group_feeds_by_host(items)
    assert [g["host"] for g in groups] == ["alpha.example", "zeta.example"]
