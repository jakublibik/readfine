"""A feed title of Atom type="html" is stored as the plain text it reads as.

feedparser hands such a title over as markup, and it used to be stored that way, so
The Verge's `Can an &#8216;eSUV&#8217; e-bike` showed the entities spelled out.
Runs on feedparser's real output, since the detail type strings are what decide.
"""
import feedparser

from app.utils.text import feed_title_text


def _entry_title(title_xml: str):
    doc = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<feed xmlns="http://www.w3.org/2005/Atom"><title>T</title>'
           f'<entry><id>1</id><link href="https://ex.invalid/1"/>{title_xml}</entry></feed>')
    e = feedparser.parse(doc).entries[0]
    return feed_title_text(e.get("title"), e.get("title_detail"))


def test_html_title_entities_are_decoded():
    assert _entry_title(
        '<title type="html"><![CDATA[Can an &#8216;eSUV&#8217; e-bike &amp; more]]></title>'
    ) == "Can an \u2018eSUV\u2019 e-bike & more"


def test_html_title_tags_go_without_a_space():
    assert _entry_title(
        '<title type="html"><![CDATA[<em>Dune</em>\'s sequel]]></title>'
    ) == "Dune's sequel"


def test_escaped_markup_stays_text():
    assert _entry_title(
        '<title type="html"><![CDATA[Why &lt;div&gt; soup]]></title>'
    ) == "Why <div> soup"


def test_text_title_is_left_alone():
    # A type="text" title comes back decoded already; decoding it again would turn
    # a literal "&amp;" in the text into "&".
    assert _entry_title('<title type="text">R&amp;D &amp;amp; more</title>') == "R&D &amp; more"


def test_rss_title_is_left_alone():
    doc = ('<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
           '<item><title>Plain &amp; simple</title><link>https://ex.invalid/1</link></item>'
           '</channel></rss>')
    e = feedparser.parse(doc).entries[0]
    assert feed_title_text(e.get("title"), e.get("title_detail")) == "Plain & simple"


def test_missing_title():
    assert feed_title_text(None, None) is None


def test_rss_title_with_markup_is_decoded():
    # feedparser guesses the type of an RSS title, and a WordPress feed that encodes
    # its titles twice is what it calls html.
    for raw, shown in [
        ("<![CDATA[A &#8216;quote&#8217; <b>here</b>]]>", "A \u2018quote\u2019 here"),
        ("Tom &amp;amp; Jerry", "Tom & Jerry"),
        ("Why &lt;div&gt; soup", "Why <div> soup"),  # plain text, left as it is
    ]:
        doc = ('<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
               f'<item><title>{raw}</title><link>https://ex.invalid/1</link></item>'
               '</channel></rss>')
        e = feedparser.parse(doc).entries[0]
        assert feed_title_text(e.get("title"), e.get("title_detail")) == shown
