"""Share target: pulling an address out of what another app handed us, and the one
route that writes.

The rest of the share flow is a form and a template, which CLAUDE.md puts under "don't
test". These two are not: the extraction is real logic with a guess in it, and the POST
saves an article from input that arrives from outside the app entirely.
"""
import json

import pytest

from app.routers.web.pwa import extract_shared_url


class TestExtractSharedUrl:
    def test_url_field_wins_when_the_app_fills_it_in(self):
        assert extract_shared_url(
            "https://example.com/story", "Some headline https://other.example/x"
        ) == "https://example.com/story"

    def test_url_is_dug_out_of_text_when_the_field_is_empty(self):
        """The common Android share: everything in one string, url left blank."""
        assert extract_shared_url(
            None, "Some headline https://example.com/story"
        ) == "https://example.com/story"

    def test_bare_url_in_text(self):
        assert extract_shared_url("", "https://example.com/story") == "https://example.com/story"

    def test_first_url_wins_when_the_text_holds_several(self):
        assert extract_shared_url(
            None, "via https://aggregator.example/i?id=1 https://example.com/real"
        ) == "https://aggregator.example/i?id=1"

    def test_nothing_usable_returns_none(self):
        assert extract_shared_url(None, "just a note, no link") is None
        assert extract_shared_url(None, None) is None
        assert extract_shared_url("", "") is None

    @pytest.mark.parametrize("scheme_url", [
        "javascript:alert(1)",
        "content://media/external/images/1",
        "mailto:someone@example.com",
        "file:///etc/passwd",
    ])
    def test_a_scheme_we_cannot_fetch_is_not_an_answer(self, scheme_url):
        """Only http(s) matches, so anything else falls through as if nothing came."""
        assert extract_shared_url(scheme_url, None) is None

    def test_a_bad_scheme_in_url_still_lets_the_text_have_its_turn(self):
        assert extract_shared_url(
            "content://provider/1", "Shared: https://example.com/story"
        ) == "https://example.com/story"

    @pytest.mark.parametrize("shared,expected", [
        ("Read https://example.com/story.", "https://example.com/story"),
        ("Read https://example.com/story, then this", "https://example.com/story"),
        ('He said "https://example.com/story"', "https://example.com/story"),
        ("(see https://example.com/story)", "https://example.com/story"),
    ])
    def test_sentence_punctuation_is_not_part_of_the_address(self, shared, expected):
        assert extract_shared_url(None, shared) == expected

    def test_a_closing_bracket_the_address_opened_is_kept(self):
        """Wikipedia disambiguations end in one; trimming it fetches the wrong page."""
        url = "https://en.wikipedia.org/wiki/Mercury_(planet)"
        assert extract_shared_url(None, f"Look at {url}") == url


class TestShareTargetForm:
    def test_the_form_offers_what_it_found(self, client):
        resp = client.get("/share-target", params={
            "title": "Some headline", "text": "Some headline https://example.com/story",
        })
        assert resp.status_code == 200
        assert 'value="https://example.com/story"' in resp.text

    def test_a_shared_title_is_not_rendered_as_markup(self, client):
        """The title is written by whichever app did the sharing."""
        resp = client.get("/share-target", params={"title": "<img src=x onerror=alert(1)>"})
        assert resp.status_code == 200
        assert "<img src=x" not in resp.text

    def test_the_shared_address_is_never_made_clickable(self, client):
        """href="javascript:…" survives escaping and runs, so it must not get there."""
        resp = client.get("/share-target", params={"text": "https://example.com/story"})
        assert 'href="https://example.com/story"' not in resp.text

    def test_nothing_is_saved_by_asking_for_the_page(self, client, mock_db):
        client.get("/share-target", params={"text": "https://example.com/story"})
        mock_db.commit.assert_not_called()


class TestShareTargetSave:
    def test_saving_needs_a_session(self, unauth_client, mock_db):
        """Redirected to the login page, and nothing written on the way."""
        resp = unauth_client.post(
            "/share-target", data={"url": "https://example.com/story"}, follow_redirects=False,
        )
        assert resp.status_code in (302, 303, 307)
        assert "/login" in resp.headers["location"]
        mock_db.commit.assert_not_called()

    def test_the_form_is_not_a_way_round_url_validation(self, client, monkeypatch):
        """The field is editable, so the address it posts gets the same checks as any
        other saved URL rather than being trusted for having come from the share sheet."""
        async def refuse(url, user, db):
            raise ValueError("Only http and https addresses can be saved")

        monkeypatch.setattr(
            "app.services.saved_article_service.save_article_by_url", refuse
        )
        resp = client.post("/share-target", data={"url": "javascript:alert(1)"})
        assert resp.status_code == 200
        assert "Only http and https addresses can be saved" in resp.text
        assert "Saved." not in resp.text


def test_the_manifest_advertises_the_share_target(unauth_client):
    """Without this Readfine never appears in the share sheet, and nothing says why."""
    manifest = json.loads(unauth_client.get("/manifest.webmanifest").content)
    target = manifest["share_target"]
    # GET on purpose: a POST share target arrives with no CSRF header and across a
    # SameSite=lax boundary. Changing this silently breaks sharing.
    assert target["method"] == "GET"
    assert target["action"] == "/share-target"
    assert set(target["params"]) == {"title", "text", "url"}
