"""Phase 5A (task 5.3): unit tests for the typed OpenAI wrapper.

These are pure unit tests (plain pytest, no live network): the low-level
``completions`` callable is injected as a stub, so :class:`OpenAIWrapper` never
touches the ``openai`` SDK / network. Covers structured-output parsing,
out-of-range handling, the request construction (JSON Schema / Structured
Outputs), retry with exponential backoff — for network failures *and* for
structured-output validation failures — and cover-letter generation.
"""

from __future__ import annotations

import logging

import pytest

from jobfucker.ai import (
    AiBoundaryError,
    ChatMessage,
    ChatRequest,
    CompletionsFn,
    JsonSchemaFormat,
    OpenAIWrapper,
    ScoreError,
    ScoreResult,
)
from jobfucker.config import OpenAIConfig


def _cfg() -> OpenAIConfig:
    return OpenAIConfig(
        model="gpt-test",
        base_url="https://api.example.com/v1",
        api_key="test-key",
    )


def _wrapper(completions: CompletionsFn) -> OpenAIWrapper:
    return OpenAIWrapper(_cfg(), completions=completions)


# --- test cases (async per the migrated AiClient contract) ----------------


def _completions_returning(text: str) -> CompletionsFn:
    async def _call(request: ChatRequest) -> str:
        del request
        return text

    return _call


def _completions_raising(exc: Exception) -> CompletionsFn:
    async def _call(request: ChatRequest) -> str:
        del request
        raise exc

    return _call


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``asyncio.sleep`` so retry backoff is instant in tests."""
    import jobfucker.ai

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr(jobfucker.ai.asyncio, "sleep", _instant)


# --- scoring: structured output -------------------------------------------------
async def test_score_ok_parses_structured_output() -> None:
    """A valid structured-output JSON becomes Ok(ScoreResult)."""
    wrapper = _wrapper(_completions_returning('{"fit_score": 4, "comment": "strong match"}'))
    result = await wrapper.score("score this vacancy")
    assert result.is_ok
    assert result.unwrap() == ScoreResult(fit_score=4, comment="strong match")


async def test_score_out_of_range_retries_then_is_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An out-of-range fit_score is retried, then becomes a per-item ScoreError."""
    _no_sleep(monkeypatch)
    calls: list[int] = []

    async def _call(request: ChatRequest) -> str:
        del request
        calls.append(1)
        return '{"fit_score": 9, "comment": "too high"}'

    wrapper = _wrapper(_call)
    result = await wrapper.score("prompt")
    assert result.is_err
    err = result.unwrap_err()
    assert isinstance(err, ScoreError)
    assert "out of range 1..5" in err.message
    assert len(calls) == 3  # validation failures are retried like network ones


async def test_score_low_out_of_range_retries_then_is_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fit_score below 1 is retried, then becomes a per-item ScoreError."""
    _no_sleep(monkeypatch)
    wrapper = _wrapper(_completions_returning('{"fit_score": 0, "comment": "too low"}'))
    result = await wrapper.score("prompt")
    assert result.is_err
    assert "out of range 1..5" in result.unwrap_err().message


async def test_score_invalid_json_retries_then_is_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed JSON is retried, then a per-item ScoreError with the raw text preserved."""
    _no_sleep(monkeypatch)
    calls: list[int] = []

    async def _call(request: ChatRequest) -> str:
        del request
        calls.append(1)
        return "not json at all"

    wrapper = _wrapper(_call)
    result = await wrapper.score("prompt")
    assert result.is_err
    err = result.unwrap_err()
    assert isinstance(err, ScoreError)
    assert "invalid JSON" in err.message
    assert err.raw == "not json at all"
    assert len(calls) == 3


async def test_score_missing_comment_retries_then_is_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A result missing the 'comment' string is retried, then an error."""
    _no_sleep(monkeypatch)
    wrapper = _wrapper(_completions_returning('{"fit_score": 3}'))
    result = await wrapper.score("prompt")
    assert result.is_err
    assert isinstance(result.unwrap_err(), ScoreError)


async def test_score_non_object_json_retries_then_is_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JSON array / scalar is retried, then a per-item ScoreError."""
    _no_sleep(monkeypatch)
    wrapper = _wrapper(_completions_returning("[1, 2, 3]"))
    result = await wrapper.score("prompt")
    assert result.is_err
    assert isinstance(result.unwrap_err(), ScoreError)


async def test_score_comment_is_freeform_unvalidated_string() -> None:
    """The comment string may contain any structure; it is not validated."""
    wrapper = _wrapper(_completions_returning('{"fit_score": 5, "comment": "missing: none\\nexplanation: perfect"}'))
    result = await wrapper.score("prompt")
    assert result.is_ok
    assert "missing: none" in result.unwrap().comment


# --- scoring: tolerant extraction (endpoints that ignore response_format) ---------
async def test_score_accepts_json_in_json_fenced_block() -> None:
    """A ```json code block — the classic ignored-response_format symptom — parses."""
    wrapper = _wrapper(_completions_returning('```json\n{"fit_score": 4, "comment": "fenced with tag"}\n```'))
    result = await wrapper.score("prompt")
    assert result.is_ok
    assert result.unwrap() == ScoreResult(fit_score=4, comment="fenced with tag")


async def test_score_accepts_json_in_plain_fenced_block() -> None:
    """A plain ``` block without a language tag is accepted too."""
    wrapper = _wrapper(_completions_returning('```\n{"fit_score": 3, "comment": "fenced plain"}\n```'))
    result = await wrapper.score("prompt")
    assert result.is_ok
    assert result.unwrap() == ScoreResult(fit_score=3, comment="fenced plain")


async def test_score_accepts_json_wrapped_in_prose() -> None:
    """Prose around the JSON object is tolerated as well."""
    wrapper = _wrapper(
        _completions_returning('Here is the score:\n{"fit_score": 5, "comment": "wrapped in prose"}\nHope this helps!')
    )
    result = await wrapper.score("prompt")
    assert result.is_ok
    assert result.unwrap() == ScoreResult(fit_score=5, comment="wrapped in prose")


async def test_score_fence_extraction_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Extracting JSON from a fenced block logs a warning about the endpoint."""
    with caplog.at_level(logging.WARNING, logger="jobfucker.ai"):
        wrapper = _wrapper(_completions_returning('```json\n{"fit_score": 4, "comment": "ok"}\n```'))
        result = await wrapper.score("prompt")
    assert result.is_ok
    assert any("response_format" in message for message in _debug_messages(caplog))


# --- scoring: retry with exponential backoff -----------------------------------
async def test_score_network_error_retries_then_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistently failing seam is retried 3x then becomes Err(ScoreError)."""
    _no_sleep(monkeypatch)
    calls: list[int] = []

    async def _call(request: ChatRequest) -> str:
        del request
        calls.append(1)
        raise RuntimeError("connection reset")

    wrapper = _wrapper(_call)
    result = await wrapper.score("prompt")
    assert result.is_err
    err = result.unwrap_err()
    assert isinstance(err, ScoreError)
    assert "3 attempts" in err.message
    assert "connection reset" in err.message
    assert len(calls) == 3


async def test_score_retries_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient failure on the first attempt is retried and succeeds."""
    _no_sleep(monkeypatch)
    attempts: list[int] = []

    async def _call(request: ChatRequest) -> str:
        del request
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("transient")
        return '{"fit_score": 5, "comment": "ok"}'

    wrapper = _wrapper(_call)
    result = await wrapper.score("prompt")
    assert result.is_ok
    assert result.unwrap().fit_score == 5
    assert len(attempts) == 3


async def test_score_retries_invalid_json_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model that first returns invalid JSON is asked again and succeeds."""
    _no_sleep(monkeypatch)
    attempts: list[int] = []

    async def _call(request: ChatRequest) -> str:
        del request
        attempts.append(1)
        if len(attempts) < 3:
            return "not json yet"
        return '{"fit_score": 5, "comment": "finally"}'

    wrapper = _wrapper(_call)
    result = await wrapper.score("prompt")
    assert result.is_ok
    assert result.unwrap() == ScoreResult(fit_score=5, comment="finally")
    assert len(attempts) == 3


async def test_score_retries_out_of_range_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient out-of-range score is retried and a later attempt succeeds."""
    _no_sleep(monkeypatch)
    attempts: list[int] = []

    async def _call(request: ChatRequest) -> str:
        del request
        attempts.append(1)
        if len(attempts) < 3:
            return '{"fit_score": 7, "comment": "oops"}'
        return '{"fit_score": 4, "comment": "ok"}'

    wrapper = _wrapper(_call)
    result = await wrapper.score("prompt")
    assert result.is_ok
    assert result.unwrap().fit_score == 4
    assert len(attempts) == 3


async def test_score_validation_and_network_failures_share_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Network and validation failures count against the same attempt budget."""
    _no_sleep(monkeypatch)
    attempts: list[int] = []

    async def _call(request: ChatRequest) -> str:
        del request
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("transient")
        if len(attempts) == 2:
            return '{"fit_score": 9, "comment": "too high"}'
        return '{"fit_score": 3, "comment": "ok"}'

    wrapper = _wrapper(_call)
    result = await wrapper.score("prompt")
    assert result.is_ok
    assert result.unwrap().fit_score == 3
    assert len(attempts) == 3


async def test_score_empty_content_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """No content from the SDK maps to a per-item error, not a crash (after retries)."""
    _no_sleep(monkeypatch)
    wrapper = _wrapper(_completions_raising(AiBoundaryError("OpenAI returned an empty completion")))
    result = await wrapper.score("prompt")
    assert result.is_err
    assert isinstance(result.unwrap_err(), ScoreError)


# --- scoring: request construction ----------------------------------------------
async def test_score_request_uses_json_schema_and_default_no_reasoning_effort() -> None:
    """Scoring must request JSON Schema structured output; reasoning_effort defaults to None."""
    captured: list[ChatRequest] = []

    async def _capture(request: ChatRequest) -> str:
        captured.append(request)
        return '{"fit_score": 5, "comment": "yes"}'

    wrapper = _wrapper(_capture)
    result = await wrapper.score("the prompt text")
    assert result.is_ok
    assert len(captured) == 1
    request = captured[0]
    assert request.model == "gpt-test"
    assert isinstance(request.response_format, JsonSchemaFormat)
    assert request.response_format.name == "scoring_result"
    assert request.response_format.strict is True
    assert request.reasoning_effort is None  # unset by default; no temperature anywhere
    assert request.messages == (ChatMessage(role="user", content="the prompt text"),)


async def test_reasoning_effort_forwarded_to_score_and_generate() -> None:
    """A configured ``reasoning_effort`` lands on both request types, verbatim."""
    config = OpenAIConfig(
        model="gpt-test",
        base_url="https://api.example.com/v1",
        api_key="test-key",
        # A provider-specific value outside OpenAI's classic set (e.g. Gemini's
        # true/false) — proves no enum validation exists anywhere in the path.
        reasoning_effort="true",
    )
    captured: list[ChatRequest] = []

    async def _capture(request: ChatRequest) -> str:
        captured.append(request)
        if isinstance(request.response_format, JsonSchemaFormat):
            return '{"fit_score": 5, "comment": "yes"}'
        return "hire me"

    wrapper = OpenAIWrapper(config, completions=_capture)
    score_result = await wrapper.score("score prompt")
    letter_result = await wrapper.generate_cover_letter("letter prompt")
    assert score_result.is_ok
    assert letter_result.is_ok
    assert len(captured) == 2
    assert all(request.reasoning_effort == "true" for request in captured)


# --- cover letter ---------------------------------------------------------------
async def test_generate_cover_letter_ok() -> None:
    """A successful generation returns the cover-letter text."""
    wrapper = _wrapper(_completions_returning("Dear hiring team, ..."))
    result = await wrapper.generate_cover_letter("prompt")
    assert result.is_ok
    assert result.unwrap() == "Dear hiring team, ..."


async def test_generate_cover_letter_error_is_string_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generation failure returns Err(str), not an exception (after retries)."""
    _no_sleep(monkeypatch)
    wrapper = _wrapper(_completions_raising(AiBoundaryError("empty completion")))
    result = await wrapper.generate_cover_letter("prompt")
    assert result.is_err
    assert "3 attempts" in result.unwrap_err()


async def test_generate_cover_letter_request_is_text_output() -> None:
    """Cover-letter generation uses text output (not JSON mode)."""
    captured: list[ChatRequest] = []

    async def _capture(request: ChatRequest) -> str:
        captured.append(request)
        return "hire me"

    wrapper = _wrapper(_capture)
    result = await wrapper.generate_cover_letter("prompt")
    assert result.is_ok
    assert captured[0].response_format == "text"


# --- no system message ----------------------------------------------------------
async def test_score_sends_single_user_message_no_system() -> None:
    """The prompt is a single user message — no system message, no wrapping."""
    captured: list[ChatRequest] = []

    async def _capture(request: ChatRequest) -> str:
        captured.append(request)
        return '{"fit_score": 3, "comment": "r"}'

    wrapper = _wrapper(_capture)
    await wrapper.score("the entire prompt as user content")
    assert len(captured) == 1
    messages = captured[0].messages
    assert len(messages) == 1
    assert messages[0].role == "user"
    assert messages[0].content == "the entire prompt as user content"


async def test_wrapper_with_injected_completions_never_constructs_client() -> None:
    """The stub seam means the wrapper does no network work when given a callable.

    Constructing the default would import openai lazily at call time; with an
    injected ``completions`` the default path is never triggered, so no client
    or network is involved. Verified by a completions that asserts it is used.
    """
    called = False

    async def _stub(request: ChatRequest) -> str:
        nonlocal called
        called = True
        del request
        return '{"fit_score": 3, "comment": "r"}'

    wrapper = _wrapper(_stub)
    result = await wrapper.score("p")
    assert result.is_ok
    assert called


# --- DEBUG request/response logging -------------------------------------------
def _debug_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The formatted messages of the ``jobfucker.ai`` DEBUG records."""
    return [record.getMessage() for record in caplog.records if record.name == "jobfucker.ai"]


async def test_score_logs_formatted_request_and_response_json(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every exchange is logged at DEBUG: request JSON (model/prompt/schema) + response JSON."""
    with caplog.at_level(logging.DEBUG, logger="jobfucker.ai"):
        wrapper = _wrapper(_completions_returning('{"fit_score": 4, "comment": "ok"}'))
        result = await wrapper.score("score this vacancy")

    assert result.is_ok
    messages = _debug_messages(caplog)
    request_log = next(m for m in messages if "AI request" in m)
    response_log = next(m for m in messages if "AI response" in m)

    # The request is pretty-printed JSON: model, user message, structured-output schema.
    assert '"model": "gpt-test"' in request_log
    assert '"role": "user"' in request_log
    assert '"content": "score this vacancy"' in request_log
    assert '"type": "json_schema"' in request_log
    assert '"name": "scoring_result"' in request_log

    # The response is re-formatted (indented) JSON.
    assert '"fit_score": 4' in response_log
    assert '"comment": "ok"' in response_log


async def test_score_logs_raw_response_when_not_json(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unparseable response (the invalid-JSON failure mode) is logged verbatim."""
    _no_sleep(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="jobfucker.ai"):
        wrapper = _wrapper(_completions_returning("fit_score: 2\ncomment: nope"))
        result = await wrapper.score("prompt")

    assert result.is_err
    assert "invalid JSON" in result.unwrap_err().message
    response_log = next(m for m in _debug_messages(caplog) if "AI response" in m)
    assert "fit_score: 2" in response_log
    assert "comment: nope" in response_log


async def test_retries_log_each_failed_validation(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid structured output is logged per attempt with the failure reason."""
    _no_sleep(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="jobfucker.ai"):
        wrapper = _wrapper(_completions_returning("not json at all"))
        result = await wrapper.score("prompt")

    assert result.is_err
    failures = [m for m in _debug_messages(caplog) if "failed validation" in m]
    assert len(failures) == 3
    assert all("invalid JSON" in m for m in failures)


async def test_generate_logs_request_and_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cover-letter generation logs its text-output request and plain-text response."""
    with caplog.at_level(logging.DEBUG, logger="jobfucker.ai"):
        wrapper = _wrapper(_completions_returning("Dear hiring team, ..."))
        result = await wrapper.generate_cover_letter("cover prompt")

    assert result.is_ok
    messages = _debug_messages(caplog)
    request_log = next(m for m in messages if "AI request" in m)
    response_log = next(m for m in messages if "AI response" in m)
    assert '"content": "cover prompt"' in request_log
    assert '"type": "text"' in request_log
    assert "Dear hiring team, ..." in response_log


async def test_retries_log_each_failed_attempt(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient failures are logged per attempt (attempt number + error)."""
    _no_sleep(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="jobfucker.ai"):
        wrapper = _wrapper(_completions_raising(RuntimeError("connection reset")))
        result = await wrapper.score("prompt")

    assert result.is_err
    failures = [m for m in _debug_messages(caplog) if "attempt" in m and "failed" in m]
    assert len(failures) == 3
    assert all("connection reset" in m for m in failures)
