"""The Jinja filters that put stored values on screen."""
from app.templating import _error_headline, _hostname


class TestErrorHeadlineFilter:
    """Fetch error for a table that names the feed in a column of its own."""

    def test_drops_the_feed_url_off_an_http_failure(self):
        assert _error_headline(
            "HTTP 403 Forbidden: https://www.reddit.com/r/selfhosted/.rss"
        ) == "HTTP 403 Forbidden"

    def test_drops_a_url_carrying_a_query(self):
        assert _error_headline(
            "HTTP 429 Too Many Requests: http://www.techdirt.com/rss.xml?edition=te"
        ) == "HTTP 429 Too Many Requests"

    def test_keeps_a_message_that_has_no_url(self):
        assert _error_headline("The read operation timed out") == "The read operation timed out"

    def test_keeps_a_url_that_is_not_the_tail(self):
        """A URL the message goes on to say something about is part of what happened,
        unlike the address repeated after an HTTP status."""
        msg = "Redirect blocked: https://evil.example.com/x to somewhere else"
        assert _error_headline(msg) == msg

    def test_keeps_a_message_that_is_nothing_but_a_url(self):
        """Nothing to cut down to, so cutting would leave an empty cell."""
        assert _error_headline("https://only.example.com/feed") == "https://only.example.com/feed"

    def test_empty(self):
        assert _error_headline(None) == "" and _error_headline("") == ""


class TestHostnameFilter:
    """Source label for an article with no feed, read off its stored address."""

    def test_plain_host(self):
        assert _hostname("https://example.com/news/story") == "example.com"

    def test_strips_www(self):
        assert _hostname("https://www.example.com/a") == "example.com"

    def test_drops_credentials(self):
        """netloc is the whole authority, so this used to render 'user:pw@example.com'.
        Saved addresses are split before storage, so it is a second line rather than
        the only one, but this filter is where a stored address reaches the screen."""
        assert _hostname("https://user:pw@example.com/a") == "example.com"

    def test_drops_the_port(self):
        assert _hostname("https://example.com:8443/a") == "example.com"

    def test_lowercases(self):
        assert _hostname("https://EXAMPLE.com/a") == "example.com"

    def test_no_host(self):
        assert _hostname("not a url") == ""

    def test_empty(self):
        assert _hostname(None) == "" and _hostname("") == ""

    def test_unparseable_does_not_raise(self):
        """urlsplit rejects a malformed IPv6 literal; a label is not worth a 500."""
        assert _hostname("https://[oops/a") == ""
