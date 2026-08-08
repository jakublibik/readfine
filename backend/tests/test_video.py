"""Unit tests for app.utils.video: recognising a video URL and building its markup.

Pure functions, no DB and no HTTP. The extraction path that *uses* them lives in
test_service_readable.py (TestExtractReadableVideoPage).
"""
import re

import pytest

from app.utils.video import (
    description_paragraphs,
    video_body_from_feed,
    video_target,
    youtube_full_description,
)


class TestVideoTarget:
    @pytest.mark.parametrize("url,expected", [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", ("youtube", "dQw4w9WgXcQ")),
        ("https://youtube.com/watch?v=dQw4w9WgXcQ&t=30s", ("youtube", "dQw4w9WgXcQ")),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ", ("youtube", "dQw4w9WgXcQ")),
        ("https://youtu.be/dQw4w9WgXcQ", ("youtube", "dQw4w9WgXcQ")),
        ("https://youtu.be/dQw4w9WgXcQ?t=30", ("youtube", "dQw4w9WgXcQ")),
        ("https://www.youtube.com/shorts/abc123XYZ_-", ("youtube", "abc123XYZ_-")),
        ("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ", ("youtube", "dQw4w9WgXcQ")),
        ("https://vimeo.com/76979871", ("vimeo", "76979871")),
        ("https://player.vimeo.com/video/76979871", ("vimeo", "76979871")),
    ])
    def test_recognises_video_pages(self, url, expected):
        assert video_target(url) == expected

    @pytest.mark.parametrize("url", [
        # Pages on the same hosts that are not a single video: replacing one of these
        # with a thumbnail would throw away the page the user actually asked for.
        "https://www.youtube.com/@LinusTechTips",
        "https://www.youtube.com/playlist?list=PL1234567890",
        "https://www.youtube.com/feeds/videos.xml?channel_id=UC123",
        "https://vimeo.com/channels/staffpicks",
        # Right shape, wrong host.
        "https://example.com/watch?v=dQw4w9WgXcQ",
        # An id that could not be one, so nothing is built from it.
        "https://www.youtube.com/watch?v=../../evil",
        "https://youtu.be/",
        None,
    ])
    def test_ignores_everything_else(self, url):
        assert video_target(url) is None


class TestYoutubeFullDescription:
    def test_reads_the_untruncated_description(self):
        html = '{"videoDetails":{"shortDescription":"First line.\\nSecond line.","lengthSeconds":"212"}}'
        assert youtube_full_description(html) == "First line.\nSecond line."

    def test_unescapes_json_escapes(self):
        html = r'{"shortDescription":"Quote \" and backslash \\ and é"}'
        assert youtube_full_description(html) == 'Quote " and backslash \\ and é'

    def test_absent_payload_returns_none(self):
        assert youtube_full_description("<html><body>no payload</body></html>") is None

    def test_broken_payload_returns_none(self):
        # Malformed escape: the fallback to og:description must be silent, never a raise.
        assert youtube_full_description(r'{"shortDescription":"bad \q escape"}') is None

    def test_empty_description_returns_none(self):
        assert youtube_full_description('{"shortDescription":"   "}') is None


class TestDescriptionParagraphs:
    def test_blank_lines_split_paragraphs(self):
        assert description_paragraphs("One.\n\nTwo.") == "<p>One.</p><p>Two.</p>"

    def test_single_newlines_become_breaks(self):
        assert description_paragraphs("One.\nTwo.") == "<p>One.<br>Two.</p>"

    def test_markup_in_the_description_is_text(self):
        out = description_paragraphs('<script>alert(1)</script>')
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_urls_are_left_as_text(self):
        assert description_paragraphs("See https://example.com/x") == "<p>See https://example.com/x</p>"

    def test_no_description_is_no_markup(self):
        assert description_paragraphs(None) == ""
        assert description_paragraphs("   ") == ""

    def test_timestamps_are_not_linked_without_a_video(self):
        assert description_paragraphs("0:00 Intro") == "<p>0:00 Intro</p>"


class TestTimestampLinks:
    _V = ("youtube", "dQw4w9WgXcQ")

    def _links(self, line):
        return re.findall(r'data-seek="(\d+)">([^<]+)</a>', description_paragraphs(line, self._V))

    def test_chapter_mark_becomes_a_seek(self):
        assert self._links("1:23 The setup") == [("83", "1:23")]

    def test_hours_are_counted(self):
        assert self._links("1:02:03 Long bit") == [("3723", "1:02:03")]

    def test_zero_mark_is_linked_too(self):
        # 0:00 opens the chapter list of nearly every description; a falsy second count
        # must still produce a link.
        assert self._links("0:00 Intro") == [("0", "0:00")]

    def test_several_marks_on_one_line(self):
        assert self._links("see 0:30 and 2:00") == [("30", "0:30"), ("120", "2:00")]

    @pytest.mark.parametrize("text", [
        "shot in 16:9 not 4:3",     # aspect ratios: no two-digit seconds field
        "a 1:1 call",
        "part 1:2 of the series",
        "at 1:2:3 nothing",         # not zero-padded, so not a timestamp
    ])
    def test_leaves_other_colon_numbers_alone(self, text):
        assert self._links(text) == []

    def test_href_points_at_the_same_moment_on_the_site(self):
        # The href is what a reader without the script gets, so it has to carry the
        # timestamp as well.
        out = description_paragraphs("1:23 The setup", self._V)
        assert 'href="https://www.youtube.com/watch?v=dQw4w9WgXcQ&amp;t=83s"' in out

    def test_vimeo_marks_point_at_vimeo(self):
        out = description_paragraphs("1:23 The setup", ("vimeo", "76979871"))
        assert 'href="https://vimeo.com/76979871#t=83s"' in out

    def test_text_around_a_mark_is_still_escaped(self):
        out = description_paragraphs("1:23 <b>bold</b>", self._V)
        assert "<b>" not in out
        assert "&lt;b&gt;" in out


class TestVideoBodyFromFeed:
    _DESC = "0:00 Intro\n\nSecond paragraph & a <tag>."

    def test_youtube_item_becomes_video_plus_description(self):
        body = video_body_from_feed(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ", description_text=self._DESC)
        assert 'data-video-id="dQw4w9WgXcQ"' in body
        assert "Second paragraph &amp; a &lt;tag&gt;." in body
        assert 'data-seek="0"' in body

    def test_feed_html_is_ignored_when_text_is_given(self):
        body = video_body_from_feed(
            "https://youtu.be/dQw4w9WgXcQ", description_text="Just this",
            feed_html="<p>not this</p>")
        assert "Just this" in body
        assert "not this" not in body

    def test_other_feeds_keep_their_own_markup(self):
        # A feed that merely links a video writes real HTML in its items; escaping it
        # would put its tags on the screen, so the video goes above it untouched.
        body = video_body_from_feed(
            "https://youtu.be/dQw4w9WgXcQ", feed_html='<p>Post <a href="/x">link</a></p>')
        assert body.startswith("<figure")
        assert '<a href="/x">link</a>' in body

    def test_ordinary_article_is_not_a_video(self):
        assert video_body_from_feed("https://example.com/post", feed_html="<p>x</p>") is None

    def test_no_url_is_not_a_video(self):
        assert video_body_from_feed(None) is None

    def test_video_with_no_description_is_still_a_video(self):
        body = video_body_from_feed("https://youtu.be/dQw4w9WgXcQ", description_text="")
        assert 'data-video-id="dQw4w9WgXcQ"' in body


