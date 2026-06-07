"""Regression: dwell tracking must persist reading time even when the article
has no UserArticleState row yet (read in detail panel before mark-read fires)."""


def test_dwell_upserts_when_no_state_row(client, mock_db):
    # No pre-existing state row — the old code silently dropped the dwell here.
    resp = client.post("/htmx/articles/123/dwell", data={"seconds": "60"})
    assert resp.status_code == 204
    # Must always issue the upsert + commit, regardless of prior state.
    assert mock_db.execute.await_count == 1
    assert mock_db.commit.await_count == 1


def test_dwell_ignores_short_sessions(client, mock_db):
    resp = client.post("/htmx/articles/123/dwell", data={"seconds": "3"})
    assert resp.status_code == 204
    # <= 3s is noise — nothing persisted.
    assert mock_db.execute.await_count == 0
    assert mock_db.commit.await_count == 0
