"""Regression: dwell tracking must persist reading time even when the article
has no UserArticleState row yet (read in detail panel before mark-read fires),
and must reject articles outside the user's reading context (#13)."""
from unittest.mock import MagicMock


def _accessible_result(ids):
    """A db.execute() result whose .all() yields (article_id,) rows."""
    r = MagicMock()
    r.all.return_value = [(i,) for i in ids]
    return r


def test_dwell_upserts_when_accessible(client, mock_db):
    # First execute = access check (passes); second = the dwell upsert.
    mock_db.execute.side_effect = [_accessible_result([123]), MagicMock()]
    resp = client.post("/htmx/articles/123/dwell", data={"seconds": "60"})
    assert resp.status_code == 204
    assert mock_db.execute.await_count == 2
    assert mock_db.commit.await_count == 1


def test_dwell_404_when_inaccessible(client, mock_db):
    # Access check returns nothing → article outside the user's context.
    mock_db.execute.side_effect = [_accessible_result([])]
    resp = client.post("/htmx/articles/123/dwell", data={"seconds": "60"})
    assert resp.status_code == 404
    assert mock_db.commit.await_count == 0


def test_dwell_ignores_short_sessions(client, mock_db):
    resp = client.post("/htmx/articles/123/dwell", data={"seconds": "3"})
    assert resp.status_code == 204
    # <= 3s is noise — nothing persisted, access not even checked.
    assert mock_db.execute.await_count == 0
    assert mock_db.commit.await_count == 0
