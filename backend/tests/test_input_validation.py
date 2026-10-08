"""Hand-built input that used to end in a 500 (review M4-06, M5-06, M6-05).

Scope and label tokens arrive as JSON in forms and query strings, so anything
can be in them. The parsers must drop what they cannot use, and the validators
must refuse it with a ValueError the routes turn into a message.
"""
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from starlette.datastructures import FormData

from app.routers.web.settings.filters import _FilterFormError, _parse_filter_form
from app.schemas.filter import FilterCreate
from app.schemas.label import LabelCreate
from app.services.catchup_service import validate_scope
from app.services.filter_service import _validate_scope_list
from app.services.scope_tokens import parse_label_tokens, parse_scope_tokens, token_id


class TestTokenParsers:
    @pytest.mark.parametrize("raw", ["5", "{}", '"feed:1"', "null", "true"])
    def test_json_that_is_not_a_list_means_no_scope(self, raw):
        assert parse_scope_tokens(raw) == ([], [])
        assert parse_label_tokens(raw) == (False, [])

    def test_ids_past_the_integer_column_are_dropped(self):
        assert parse_scope_tokens('["feed:99999999999", "feed:3"]') == ([3], [])
        assert parse_label_tokens('["label:99999999999", "label:4"]') == (False, [4])

    def test_non_string_items_are_skipped(self):
        assert parse_scope_tokens('[5, null, "folder:2"]') == ([], [2])

    @pytest.mark.parametrize("raw", ["-1", "2147483648", "²", "abc", ""])
    def test_token_id_refuses_what_no_row_can_have(self, raw):
        with pytest.raises(ValueError):
            token_id(raw)

    def test_token_id_takes_the_whole_range(self):
        assert token_id("0") == 0
        assert token_id("2147483647") == 2147483647


class TestScopeValidation:
    async def test_non_list_scope_is_a_value_error(self):
        with pytest.raises(ValueError):
            await validate_scope(1, "5", AsyncMock())

    @pytest.mark.parametrize("item", [5, None, "feed:99999999999", "folder:x"])
    async def test_bad_items_are_a_value_error(self, item):
        with pytest.raises(ValueError):
            await _validate_scope_list(1, [item], AsyncMock())


class TestNameLimits:
    def test_label_name_is_stripped_and_required(self):
        assert LabelCreate(name="  Tech ").name == "Tech"
        with pytest.raises(ValidationError):
            LabelCreate(name="   ")

    def test_label_name_and_position_fit_their_columns(self):
        with pytest.raises(ValidationError):
            LabelCreate(name="x" * 101)
        with pytest.raises(ValidationError):
            LabelCreate(name="Tech", position=40000)

    def test_filter_name_fits_its_column(self):
        with pytest.raises(ValidationError):
            FilterCreate(name="x" * 101)


def _filter_form(**overrides):
    fields = {
        "name": "News",
        "match_operator": "AND",
        "position": "0",
        "cond_field": "title",
        "cond_operator": "contains",
        "cond_value": "python",
        "cond_position": "0",
    }
    fields.update(overrides)
    return FormData(list(fields.items()))


class TestFilterForm:
    def test_valid_form_parses(self):
        payload = _parse_filter_form(_filter_form())
        assert payload.name == "News"
        assert payload.conditions[0].operator == "contains"

    def test_unknown_operator_keeps_what_was_typed(self):
        with pytest.raises(_FilterFormError) as exc:
            _parse_filter_form(_filter_form(cond_operator="matches"))
        # The editor shows the submitted values again rather than an empty form.
        assert exc.value.form_values.name == "News"
        assert exc.value.form_values.conditions[0].value == "python"
        assert "operator" in str(exc.value)

    def test_blank_name_is_refused(self):
        with pytest.raises(_FilterFormError):
            _parse_filter_form(_filter_form(name="   "))

    def test_junk_condition_position_falls_back(self):
        payload = _parse_filter_form(_filter_form(cond_position="²"))
        assert payload.conditions[0].position == 0
