"""AI captcha handler (Phase 6, task 6.1).

Proves :class:`AiCaptchaHandler` uses only the ``openai_captcha`` config (never
the main ``openai`` section), pins a small ``max_tokens``, forwards the
section's optional ``reasoning_effort``, delegates to an injectable vision
call (no network in tests), and applies exact-majority consensus over
``consensus_requests`` (K) concurrent requests. The handler and its vision seam
are **async** per the async-migration spec (the captcha handler contract is
awaitable).
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from jobfucker.captcha.ai import (
    AiCaptchaHandler,
    CaptchaVisionFn,
    build_captcha_vision_request,
    consensus,
)
from jobfucker.config import OpenAIConfig


def _captcha_config() -> OpenAIConfig:
    return OpenAIConfig(
        model="captcha-model",
        base_url="https://captcha.example/v1",
        api_key="captcha-key",
    )


def _scripted_vision(script: Sequence[str | Exception]) -> tuple[CaptchaVisionFn, list[bytes]]:
    """A vision seam answering per call from ``script``; exceptions raise.

    Returns the seam plus the received images (call count = its length).
    """
    received: list[bytes] = []

    async def stub(config: OpenAIConfig, image: bytes) -> str:
        del config
        received.append(image)
        answer = script[len(received) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return stub, received


def test_build_request_defaults_no_reasoning_effort_and_pins_small_max_tokens() -> None:
    config = _captcha_config()
    request = build_captcha_vision_request(config, b"PNGDATA")
    assert request.reasoning_effort is None  # unset by default; no temperature anywhere
    assert request.max_tokens == 20  # small max_tokens (architecture §4/§5.2)
    assert request.model == "captcha-model"
    # The PNG is base64-encoded.
    assert request.image_b64.startswith("UE5HREFUQQ==")  # base64("PNGDATA")


def test_build_request_forwards_configured_reasoning_effort() -> None:
    config = _captcha_config().model_copy(update={"reasoning_effort": "true"})
    request = build_captcha_vision_request(config, b"PNGDATA")
    assert request.reasoning_effort == "true"  # verbatim; no enum validation


async def test_handler_delegates_to_openai_captcha_config_only() -> None:
    received: list[tuple[OpenAIConfig, bytes]] = []

    async def stub_vision(config: OpenAIConfig, image: bytes) -> str:
        received.append((config, image))
        return "  ABC123  "

    config = _captcha_config()
    handler = AiCaptchaHandler(config, vision=stub_vision)
    result = await handler(b"PNGDATA")
    assert result.is_ok
    assert result.unwrap() == "ABC123"
    # The vision call received exactly the openai_captcha config we built the
    # handler with — never the main openai section.
    assert received[0][0] is config
    assert received[0][1] == b"PNGDATA"


async def test_handler_uses_default_vision_when_not_injected() -> None:
    handler = AiCaptchaHandler(_captcha_config())
    # No assertion on network — just that the seam resolved to a callable.
    vision: CaptchaVisionFn = handler._vision  # type: ignore[private-usage]  # rationale: introspect the resolved seam in a unit test
    assert callable(vision)


async def test_handler_maps_vision_failure_to_err() -> None:
    async def failing_vision(config: OpenAIConfig, image: bytes) -> str:
        del config, image
        raise RuntimeError("vision service down")

    handler = AiCaptchaHandler(_captcha_config(), vision=failing_vision)
    result = await handler(b"PNGDATA")
    assert result.is_err
    assert "failed" in result.unwrap_err()


# --- Consensus (exact-majority over K vision requests) ----------------------


def test_consensus_picks_most_common_answer() -> None:
    assert consensus(["ab cd", "xy", "ab cd", "ab cd"]) == "ab cd"


def test_consensus_tie_returns_first_seen_answer() -> None:
    assert consensus(["ab", "xy", "xy", "ab"]) == "ab"


def test_consensus_all_distinct_returns_first_answer() -> None:
    assert consensus(["aa", "bb", "cc", "dd"]) == "aa"


def test_consensus_groups_whitespace_and_case_variants() -> None:
    # Whitespace runs and case are normalized for grouping; the winning group's
    # collapsed (single-spaced) form is returned — the vote cannot endorse a
    # specific internal spacing, and the board validates exact text.
    assert consensus(["ab  cd", "AB CD", " ab cd "]) == "ab cd"


def test_consensus_empty_answers_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="at least one"):
        consensus([])


async def test_handler_fires_default_four_vision_calls() -> None:
    stub, received = _scripted_vision(["ab cd"] * 4)
    handler = AiCaptchaHandler(_captcha_config(), vision=stub)
    result = await handler(b"PNGDATA")
    assert result.is_ok
    assert result.unwrap() == "ab cd"
    assert len(received) == 4  # default consensus_requests
    assert all(image == b"PNGDATA" for image in received)


async def test_handler_configured_consensus_requests_counts_calls() -> None:
    stub, received = _scripted_vision(["xy"] * 2)
    handler = AiCaptchaHandler(_captcha_config().model_copy(update={"consensus_requests": 2}), vision=stub)
    result = await handler(b"PNGDATA")
    assert result.is_ok
    assert result.unwrap() == "xy"
    assert len(received) == 2


async def test_handler_majority_beats_dissent() -> None:
    stub, _ = _scripted_vision(["кот", "кдт", "кот", "кот"])
    handler = AiCaptchaHandler(_captcha_config(), vision=stub)
    result = await handler(b"PNGDATA")
    assert result.is_ok
    assert result.unwrap() == "кот"


async def test_handler_drops_failed_requests_and_votes_among_successes() -> None:
    stub, received = _scripted_vision([RuntimeError("rate limited"), "ab cd", "ab cd", "zz zz"])
    handler = AiCaptchaHandler(_captcha_config(), vision=stub)
    result = await handler(b"PNGDATA")
    assert result.is_ok
    assert result.unwrap() == "ab cd"
    assert len(received) == 4  # all K were still attempted


async def test_handler_err_when_every_request_fails() -> None:
    stub, _ = _scripted_vision([RuntimeError("boom")] * 4)
    handler = AiCaptchaHandler(_captcha_config(), vision=stub)
    result = await handler(b"PNGDATA")
    assert result.is_err
    message = result.unwrap_err()
    assert "no usable answer" in message
    assert "boom" in message


async def test_handler_drops_empty_answers_from_the_vote() -> None:
    # A blank answer must not win (or tie-win) the vote: it would be submitted
    # verbatim and hard-fail the coordinator's answer validation.
    stub, _ = _scripted_vision(["", "  ", "ab cd", "ab cd"])
    handler = AiCaptchaHandler(_captcha_config(), vision=stub)
    result = await handler(b"PNGDATA")
    assert result.is_ok
    assert result.unwrap() == "ab cd"


async def test_handler_err_when_every_answer_is_empty() -> None:
    stub, _ = _scripted_vision(["", "  ", "", "\t"])
    handler = AiCaptchaHandler(_captcha_config(), vision=stub)
    result = await handler(b"PNGDATA")
    assert result.is_err
    assert "no usable answer" in result.unwrap_err()
    assert "empty answer" in result.unwrap_err()
