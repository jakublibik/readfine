"""The Jinja filters that put stored values on screen."""
from app.templating import _hostname


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
