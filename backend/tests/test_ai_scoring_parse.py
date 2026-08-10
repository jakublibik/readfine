"""Unit tests for score_article response parsing.

A parse failure must raise (so the caller's retry/failure path runs) rather than
silently returning a fake 0.5 score that would pollute ranking and AI filters.
"""
import pytest
from unittest.mock import AsyncMock, patch

from app.services.ai_service import Completion, score_article


async def _run(raw: str):
    with patch(
        "app.services.ai_service._complete",
        new=AsyncMock(return_value=Completion(raw, 7, 1, False)),
    ):
        return await score_article("content", "profile", AsyncMock(), "anthropic", "model")


class TestScoreArticleParsing:
    @pytest.mark.asyncio
    async def test_plain_number(self):
        score, in_tok, out_tok = await _run("0.8")
        assert score == 0.8
        assert (in_tok, out_tok) == (7, 1)

    @pytest.mark.asyncio
    async def test_number_wrapped_in_prose(self):
        score, _, _ = await _run("0.8 - relevant")
        assert score == 0.8

    @pytest.mark.asyncio
    async def test_score_prefix(self):
        score, _, _ = await _run("Score: 0.7")
        assert score == 0.7

    @pytest.mark.asyncio
    async def test_clamped_above_one(self):
        score, _, _ = await _run("1.5")
        assert score == 1.0

    @pytest.mark.asyncio
    async def test_unparseable_raises(self):
        with pytest.raises(ValueError):
            await _run("banana")

    @pytest.mark.asyncio
    async def test_empty_raises(self):
        with pytest.raises(ValueError):
            await _run("")
