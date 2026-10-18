"""Unit tests for result validation without calling OpenAI."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from pg_mcp.config.settings import OpenAIConfig, ValidationConfig
from pg_mcp.services.result_validator import ResultValidator


def _validator(min_confidence: int = 80, threshold: int = 70) -> ResultValidator:
    validator = ResultValidator(
        openai_config=OpenAIConfig(api_key="sk-test"),
        validation_config=ValidationConfig(
            enabled=True,
            min_confidence_score=min_confidence,
            confidence_threshold=threshold,
        ),
    )
    validator.client = MagicMock()
    validator.client.chat = MagicMock()
    validator.client.chat.completions = MagicMock()
    return validator


def _response(payload: dict[str, object]) -> MagicMock:
    message = MagicMock()
    message.content = json.dumps(payload)
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    return response


@pytest.mark.asyncio
async def test_min_confidence_score_rejects_borderline_result() -> None:
    """The stricter of the two confidence floors decides acceptability."""
    validator = _validator(min_confidence=80, threshold=70)
    validator.client.chat.completions.create = AsyncMock(
        return_value=_response(
            {"confidence": 75, "explanation": "partial match", "suggestion": "add a filter"}
        )
    )

    result = await validator.validate(
        question="How many active users?",
        sql="SELECT COUNT(*) FROM users",
        results=[{"count": 3}],
        row_count=1,
    )

    assert result.confidence == 75
    assert result.is_acceptable is False


@pytest.mark.asyncio
async def test_disabled_validation_skips_llm() -> None:
    """Disabled validation does not call the model."""
    validator = ResultValidator(
        openai_config=OpenAIConfig(api_key="sk-test"),
        validation_config=ValidationConfig(enabled=False),
    )
    validator.client.chat.completions.create = AsyncMock()

    result = await validator.validate(
        question="count",
        sql="SELECT 1",
        results=[{"?column?": 1}],
        row_count=1,
    )

    assert result.confidence == 100
    assert result.is_acceptable is True
    validator.client.chat.completions.create.assert_not_awaited()
