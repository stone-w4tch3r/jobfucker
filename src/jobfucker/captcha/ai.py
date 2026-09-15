"""AI captcha handler: solves a captcha image with an OpenAI-compatible vision model.

Implements the shared contract :data:`CaptchaHandler`
(``Callable[[bytes], Awaitable[Result[str, str]]]``). It base64-encodes the
captcha PNG, sends ``consensus_requests`` (K, default 4) multimodal requests to
the vision model configured in the **`openai_captcha`** pipeline section only
(never the main ``openai`` section — captcha AI must not fall back to the
scoring model/key), each with a small ``max_tokens`` and the section's optional
``reasoning_effort`` passthrough, and applies the most-common-answer (plurality)
consensus of the K answers. No temperature is sent (modern models don't need it).

Consensus: each answer is normalized (strip + collapse whitespace) and grouped
case-insensitively; the most common group wins (plurality; first-seen order
breaks ties, including the all-distinct case, which returns the first answer).
A wrong consensus is not fatal — the board client's bounded attempt loop (e.g.
``service.hh.captcha_max_attempts``) requests a fresh image and retries. Failed
or empty requests are dropped from the vote; only when no request yields a
usable answer does the handler return ``Err``.

The low-level SDK call goes through the shared typed client in
:mod:`jobfucker.ai` (:func:`~jobfucker.ai.default_completions`), via the typed,
injectable seam (``CaptchaVisionFn``) so the handler is unit-testable against a
stub with no network. Async per the async-migration spec (the handler contract
is awaitable; the vision call goes through the shared ``AsyncOpenAI`` path).
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final

from rusty_results.prelude import Err, Ok, Result

from jobfucker.ai import ChatMessage, ChatRequest, ImageUrl, default_completions
from jobfucker.config import OpenAIConfig

logger = logging.getLogger(__name__)

__all__ = [
    "AiCaptchaHandler",
    "CaptchaVisionFn",
    "CaptchaVisionRequest",
    "build_captcha_vision_request",
    "consensus",
]

# The instruction sent alongside the image: return only the recognised text.
# The Cyrillic hint is deliberately a *tip*, not an instruction: observed hh.ru
# text captchas are lowercase word-like Cyrillic sequences with single spaces,
# but if that ever changes the model must trust the image over this hint.
_CAPTCHA_INSTRUCT = (
    "Return only the characters shown in the image, with no extra text. "
    "Tip: it usually shows lowercase Cyrillic, word-like character sequences "
    "separated by a single space — but trust the image over this tip."
)

# Captcha solving is short (small max_tokens is all the recognized text needs).
# No deterministic temperature anymore; the optional reasoning_effort comes from
# the ``openai_captcha`` section (see build_captcha_vision_request).
_CAPTCHA_MAX_TOKENS: Final = 20


@dataclass(frozen=True, slots=True)
class CaptchaVisionRequest:
    """A fully-typed captcha vision completion request.

    Carries exactly what the SDK needs; exposing it as a value object lets tests
    assert ``reasoning_effort`` / ``max_tokens`` and that the request is built
    from the passed ``openai_captcha`` config (never the main ``openai`` one).

    ``reasoning_effort`` is a free-form passthrough (values are
    provider-dependent); ``None`` omits the parameter.
    """

    model: str
    reasoning_effort: str | None
    max_tokens: int
    image_b64: str


def build_captcha_vision_request(config: OpenAIConfig, image: bytes) -> CaptchaVisionRequest:
    """Build a captcha vision request from an ``openai_captcha`` config + PNG bytes.

    Args:
        config: the ``openai_captcha`` pipeline section (model/base_url/api_key).
        image: the raw PNG bytes of the captcha.

    Returns:
        The typed request with the PNG base64-encoded, ``max_tokens`` pinned to
        the captcha default, and the section's optional ``reasoning_effort``
        forwarded (``None`` = omittable).
    """
    return CaptchaVisionRequest(
        model=config.model,
        reasoning_effort=config.reasoning_effort,
        max_tokens=_CAPTCHA_MAX_TOKENS,
        image_b64=base64.b64encode(image).decode("ascii"),
    )


# The low-level seam: returns the recognised text or raises on failure.
# Async per the async-migration spec (the captcha handler is awaitable; the
# vision call goes through the shared ``AsyncOpenAI`` path in ``jobfucker.ai``).
CaptchaVisionFn = Callable[[OpenAIConfig, bytes], Awaitable[str]]


def _normalize(text: str) -> str:
    """Normalize one recognised answer for grouping: strip + collapse whitespace."""
    return " ".join(text.split())


def consensus(answers: Sequence[str]) -> str:
    """Pick the most-common (plurality) answer from K recognised texts.

    Answers are normalized (strip + collapse whitespace) and grouped
    case-insensitively, so ``"ab cd"``/``"Ab  cd"`` merge into one vote. The
    most common group wins; ``Counter.most_common`` breaks ties by first-seen
    order — deterministic, including the all-distinct case, which returns the
    first answer. The returned text is the winning group's normalized (single-
    spaced) form: the vote cannot endorse a specific casing or internal
    spacing, and a collapsed form is what the board's exact-match validation
    is most likely to accept.

    Args:
        answers: the successful recognition texts (must be non-empty).

    Returns:
        The consensus answer.
    """
    if not answers:
        raise ValueError("consensus() requires at least one answer")
    normalized = [_normalize(answer) for answer in answers]
    winner = Counter(answer.lower() for answer in normalized).most_common(1)[0][0]
    return next(key for key in normalized if key.lower() == winner)


def _default_vision(_config: OpenAIConfig) -> CaptchaVisionFn:
    """The real captcha vision call through the shared typed AI client.

    Builds the multimodal :class:`~jobfucker.ai.ChatRequest` (instruction text
    + base64 PNG image part, small ``max_tokens``, optional ``reasoning_effort``)
    and delegates to :func:`~jobfucker.ai.default_completions` — the one raw-SDK
    path in the codebase. Errors are allowed to raise here and are converted to
    ``Result`` by :class:`AiCaptchaHandler`.
    """

    async def _call(captcha_config: OpenAIConfig, image: bytes) -> str:
        request = build_captcha_vision_request(captcha_config, image)
        completions = default_completions(captcha_config)
        return await completions(
            ChatRequest(
                model=request.model,
                messages=(
                    ChatMessage(
                        role="user",
                        content=(
                            _CAPTCHA_INSTRUCT,
                            ImageUrl(url=f"data:image/png;base64,{request.image_b64}"),
                        ),
                    ),
                ),
                reasoning_effort=request.reasoning_effort,
                max_tokens=request.max_tokens,
            )
        )

    return _call


class AiCaptchaHandler:
    """A :data:`CaptchaHandler` that solves a captcha via the ``openai_captcha`` model.

    Each call fires ``config.consensus_requests`` (K) concurrent vision requests
    and returns the most-common (plurality) answer (see :func:`consensus`).

    Args:
        config: the ``openai_captcha`` pipeline section (never the main ``openai``
            section — the selector only ever passes ``openai_captcha`` here);
            its ``consensus_requests`` sets K.
        vision: an optional low-level vision callable. Defaults to a real SDK
            call; tests inject a stub so no network is hit.
    """

    def __init__(self, config: OpenAIConfig, *, vision: CaptchaVisionFn | None = None) -> None:
        self._config: OpenAIConfig = config
        self._vision: CaptchaVisionFn = vision if vision is not None else _default_vision(config)

    async def __call__(self, image: bytes) -> Result[str, str]:
        """Solve the captcha image, returning the consensus answer.

        Fires ``consensus_requests`` (K) concurrent vision requests at the same
        image and returns the most-common (plurality) answer (:func:`consensus`).
        Failed or empty requests are dropped from the vote; only when no request
        yields a usable answer does the handler return ``Err``.

        Args:
            image: the PNG bytes of the captcha.

        Returns:
            ``Ok`` with the consensus answer, or ``Err`` when every request
            fails or comes back empty.
        """
        outcomes = await asyncio.gather(*(self._one(image) for _ in range(self._config.consensus_requests)))
        answers: list[str] = []
        failures: list[str] = []
        for outcome in outcomes:
            if outcome.is_err:
                failures.append(outcome.unwrap_err())
            elif answer := outcome.unwrap():
                answers.append(answer)
        if not answers:
            detail = "; ".join(failures) if failures else "empty answer"
            return Err(f"AI captcha produced no usable answer ({self._config.consensus_requests} requests): {detail}")
        return Ok(consensus(answers))

    async def _one(self, image: bytes) -> Result[str, str]:
        """One vision request, mapped to ``Result``; failures are logged, not raised."""
        try:
            return Ok((await self._vision(self._config, image)).strip())
        except Exception as exc:
            logger.warning("AI captcha vision request failed: %s", exc)
            return Err(f"AI captcha solving failed: {exc}")
