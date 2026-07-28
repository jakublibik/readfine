"""The verdict behind "this article has nothing to show, go to the source".

Getting it wrong is visible in both directions: too eager and a click on a perfectly
readable article throws the reader into the browser, too shy and the feature never
fires. The subtle part is that an article with no body yet is not the same as one that
will never have a body, which is why 'pending' and 'skipped' need their own branches."""
from app.models.article import Article
from app.services.article import body_permanently_empty


def _article(**kwargs):
    defaults = dict(
        readable_status="failed",
        readable_content=None,
        content=None,
    )
    defaults.update(kwargs)
    return Article(**defaults)


def test_extracted_content_is_not_empty():
    article = _article(readable_status="success", readable_content="<p>Body</p>")
    assert body_permanently_empty(article, True) is False


def test_success_without_content_falls_through():
    # apply_readable_result() only writes 'success' with a non-empty body, so this
    # combination means the row was touched elsewhere; treat it as having no body.
    article = _article(readable_status="success", readable_content="")
    assert body_permanently_empty(article, False) is True


def test_feed_content_survives_failed_extraction():
    article = _article(readable_status="failed", content="<p>From the feed</p>")
    assert body_permanently_empty(article, True) is False


def test_pending_extraction_is_not_permanent():
    # Either in flight or waiting on backoff; both may still produce a body.
    article = _article(readable_status="pending")
    assert body_permanently_empty(article, True) is False


def test_skipped_with_extraction_on_is_not_permanent():
    # Opening the detail flips these to pending and extracts.
    article = _article(readable_status="skipped")
    assert body_permanently_empty(article, True) is False


def test_skipped_with_extraction_off_is_permanent():
    article = _article(readable_status="skipped")
    assert body_permanently_empty(article, False) is True


def test_failed_with_nothing_is_permanent():
    article = _article(readable_status="failed")
    assert body_permanently_empty(article, True) is True


def test_unsubscribed_feed_counts_as_extraction_off():
    # UserFeed is outer-joined in the starred/labeled views, so extract_readable
    # arrives as None. The detail route gates its auto-extract on the same column,
    # so nothing would be extracted there either.
    article = _article(readable_status="skipped")
    assert body_permanently_empty(article, None) is True
