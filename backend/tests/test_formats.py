"""Unit tests for the number/date format profiles (variant B)."""
from datetime import date

import pytest

from app.utils.formats import (
    format_number,
    format_date_parts,
    format_choices,
    resolve_profile,
    is_valid_format,
    FORMAT_PROFILES,
)

# NBSP (U+00A0) is the eu thousands separator.
NBSP = " "


class TestFormatNumber:
    def test_decimals_per_profile(self):
        assert format_number(1234.56, 2, "us") == "1,234.56"
        assert format_number(1234.56, 2, "uk") == "1,234.56"
        assert format_number(1234.56, 2, "eu") == f"1{NBSP}234,56"
        assert format_number(1234.56, 2, "de") == "1.234,56"
        assert format_number(1234.56, 2, "iso") == "1234.56"

    def test_integer_grouping_per_profile(self):
        assert format_number(12345, None, "us") == "12,345"
        assert format_number(12345, None, "eu") == f"12{NBSP}345"
        assert format_number(12345, None, "de") == "12.345"
        assert format_number(12345, None, "iso") == "12345"

    def test_none_and_non_numeric_render_empty(self):
        assert format_number(None, 2, "eu") == ""
        assert format_number("not a number", 2, "eu") == ""

    def test_unknown_profile_falls_back_to_default(self):
        # Unknown key resolves to the neutral iso profile.
        assert format_number(1234.5, 1, "zz") == "1234.5"

    def test_half_even_rounding(self):
        # Python format uses round-half-even; 2.5 → "2".
        assert format_number(2.5, 0, "iso") == "2"
        assert format_number(3.5, 0, "iso") == "4"


class TestFormatDateParts:
    # Day 25 (> 12) so an order bug can't pass unnoticed.
    D = date(2026, 6, 25)

    def test_order_and_separator(self):
        assert format_date_parts(self.D, resolve_profile("us")) == "06/25/2026"
        assert format_date_parts(self.D, resolve_profile("uk")) == "25/06/2026"
        assert format_date_parts(self.D, resolve_profile("eu")) == "25.06.2026"
        assert format_date_parts(self.D, resolve_profile("de")) == "25.06.2026"
        assert format_date_parts(self.D, resolve_profile("iso")) == "2026-06-25"

    def test_without_year_drops_year(self):
        assert format_date_parts(self.D, resolve_profile("us"), with_year=False) == "06/25"
        assert format_date_parts(self.D, resolve_profile("eu"), with_year=False) == "25.06"
        assert format_date_parts(self.D, resolve_profile("iso"), with_year=False) == "06-25"


class TestHelpers:
    def test_is_valid_format(self):
        assert is_valid_format("eu")
        assert not is_valid_format("xx")
        assert not is_valid_format("")
        assert not is_valid_format(None)

    def test_format_choices_cover_all_profiles(self):
        choices = dict(format_choices())
        assert set(choices) == set(FORMAT_PROFILES)
        # Example uses day 25 and shows the profile's separators.
        assert choices["eu"] == f"1{NBSP}234,56 · 25.06.2026 (Europe)"
        assert choices["us"] == "1,234.56 · 06/25/2026 (US)"
