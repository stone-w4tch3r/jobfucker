"""The single typed client over the raw ``openai`` SDK.

The ``openai`` SDK is a weakly-typed dynamic boundary: it returns untyped JSON
payloads and accepts untyped request dicts. Instead of leaking ``Any`` into the
engine, everything the AI needs is exposed through this one typed module —
scoring, cover-letter generation, hh screening-test solving
(:mod:`jobfucker.hh_tests.ai`) and captcha vision (:mod:`jobfucker.captcha.ai`)
all share it:

- :class:`ScoreResult` — the validated structured-output result of scoring
  (a 1..5 ``fit_score`` integer plus the AI's freeform ``comment`` string).
- :class:`ScoreError` — a per-item scoring failure (invalid JSON, missing
  fields, or an out-of-range score) that survives the retry budget. Per the
  architecture §4, scoring uses **structured output (JSON Schema mode)** and
  **no regex extraction**; an out-of-range score is retried per call and only
  collapses into a per-item scoring error once the retry budget is exhausted —
  never a silent retry of the whole batch.
- :class:`AiClient` — the narrow Protocol stages depend on, so each stage is
  unit-testable against a stub without touching the network.
- :class:`OpenAIWrapper` — the concrete implementation of :class:`AiClient`.
- :class:`ChatRequest` / :class:`CompletionsFn` / :func:`default_completions` —
  the generic typed request + the single raw-SDK seam; consumers inject their
  own ``CompletionsFn`` stub so tests never hit the network.
- :func:`call_with_retry` — the one retry loop (attempts + exponential backoff)
  covering *both* transient network failures and structured-output validation
  failures: a model that returns invalid JSON is simply asked again.
- :func:`extract_json_text` — markdown-fence/prose healing for endpoints that
  ignore ``response_format``.

The AI prompting contract is **user-message-only**: the entire rendered prompt
is sent as a single ``user`` message — no ``system`` message, no wrapping.
Scoring requests a JSON Schema (OpenAI Structured Outputs, ``response_format``
type ``json_schema``); hh test solving uses JSON-object mode; cover-letter
generation uses plain text; captcha vision sends a multimodal message. Every AI
call is wrapped in up to :data:`_MAX_ATTEMPTS` attempts with exponential
backoff via :func:`call_with_retry`.

Not every endpoint honors ``response_format`` — free-tier providers in
particular may treat it as a hint and return the JSON wrapped in a markdown
code block (`` ```json `` or plain `` ``` ``) or in prose. The parse boundary
therefore extracts the payload before validating, and logs a warning when it
had to.

Expected failures (network, malformed JSON, out-of-range scores) are returned as
``Result``; exceptions are confined to a single boundary in the default
completion call and converted to ``Err`` here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Final, Literal, Protocol, TypeVar

from rusty_results.prelude import Err, Ok, Result

from jobfucker.config import OpenAIConfig

logger = logging.getLogger(__name__)

__all__ = [
    "AiBoundaryError",
    "AiClient",
    "ChatMessage",
    "ChatRequest",
    "CompletionsFn",
    "ImageUrl",
    "JsonSchemaFormat",
    "MessageContent",
    "OpenAIWrapper",
    "ScoreError",
    "ScoreResult",
    "call_with_retry",
    "default_completions",
    "extract_json_text",
]


class AiBoundaryError(Exception):
    """Raised inside the default completion call when the SDK returns no text.

    This is an *internal* control-flow exception confined to :mod:`ai` — it is
    caught and turned into a ``Result`` before it can escape to a caller. It must
    never propagate to the engine.
    """


# --- Request / result value types -------------------------------------------
@dataclass(frozen=True, slots=True)
class ImageUrl:
    """An image part of a multimodal message (a ``data:`` or ``https:`` URL)."""

    url: str


# Text-only or multimodal message content: a plain string, or an ordered mix of
# text chunks and image parts (the captcha vision path uses text + image).
type MessageContent = str | tuple[str | ImageUrl, ...]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One message in a completion request.

    The AI prompting contract is user-message-only: the entire rendered prompt
    is the sole ``user`` message — no ``system`` message, no wrapping. Text-only
    callers pass ``content`` as a ``str``; the captcha vision path passes a
    ``(text, ImageUrl)`` tuple.
    """

    role: Literal["user"]
    content: MessageContent


@dataclass(frozen=True, slots=True)
class JsonSchemaFormat:
    """A JSON Schema for OpenAI Structured Outputs (``response_format`` json_schema).

    All modern OpenAI-compatible endpoints support Structured Outputs: the model
    is forced to produce JSON conforming to ``schema``. ``strict=True`` makes the
    model adhere exactly (no extra keys, no missing required keys).
    """

    name: str
    schema: Mapping[str, object]  # lint-ignore[restricted-object]: JSON Schema is the dynamic API boundary
    strict: bool = True


# Plain text (``"text"``), a JSON object (``"json_object"``) or a JSON Schema
# (``JsonSchemaFormat``) for structured output. This is what the SDK boundary
# receives as ``response_format``. ``json_object`` is the mode for dynamic-key
# payloads (the hh test answer map) that ``json_schema`` cannot express.
type ResponseFormat = Literal["text", "json_object"] | JsonSchemaFormat


@dataclass(frozen=True, slots=True)
class ChatRequest:
    """A fully-specified chat-completion request (typed at the SDK boundary).

    ``reasoning_effort`` is a free-form passthrough (values are
    provider-dependent); ``None`` omits the parameter. The SDK types it as a
    closed Literal, but other providers accept arbitrary values.
    ``response_format`` is ``None`` (the SDK default is used, nothing sent) for
    consumers that pass no format at all — the captcha vision path.
    ``max_tokens`` caps the completion length (the captcha vision path pins a
    small value); ``None`` omits the parameter entirely.
    """

    model: str
    messages: tuple[ChatMessage, ...]
    reasoning_effort: str | None
    response_format: ResponseFormat | None = None
    max_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ScoreResult:
    """Validated structured-output scoring result (``fit_score`` in 1..5).

    ``comment`` is a freeform AI-generated string. Its internal structure
    (missing skills, explanation, etc.) is intentionally NOT part of the JSON
    Schema and is NOT validated — the user may restructure ``comment`` freely
    in the prompt; the model puts whatever it produces into the string.
    """

    fit_score: int
    comment: str


@dataclass(frozen=True, slots=True)
class ScoreError:
    """A per-item scoring failure (parsing or out-of-range score).

    Returned only after the retry budget is exhausted; ``raw`` keeps the last
    attempt's completion text for diagnostics.
    """

    message: str
    raw: str | None = None  # the raw completion text, for diagnostics


# --- Low-level seam ----------------------------------------------------------
# The default completion call goes through the raw ``openai`` SDK (the dynamic
# boundary); tests and any other client inject their own ``CompletionsFn`` so
# no network is ever hit in the suite. The seam is async.
CompletionsFn = Callable[[ChatRequest], Awaitable[str]]


# --- Protocol the stages depend on ------------------------------------------
class AiClient(Protocol):
    """The narrow AI surface :mod:`stages` need.

    Both methods take an already-rendered prompt string (the full Jinja2 body
    rendered by :mod:`jobfucker.stages.prompts`) and return ``Result``. The
    prompt is sent as a single ``user`` message — no system message, no wrapping.
    """

    async def score(self, prompt: str) -> Result[ScoreResult, ScoreError]:
        """Score a vacancy from its rendered prompt; fit_score 1..5 or a per-item error."""
        ...

    async def generate_cover_letter(self, prompt: str) -> Result[str, str]:
        """Generate a cover-letter body from its rendered prompt."""
        ...


# --- YAML/JSON boundary helpers (mirrors config.py narrowing) ---------------
def _as_str_keyed_dict(
    value: object,  # lint-ignore[restricted-object]: JSON boundary; keys narrowed
) -> dict[str, object] | None:  # lint-ignore[restricted-object]: AI JSON payload  # lint-ignore[raw-dict]: AI JSON dict
    """Return ``value`` as a str-keyed mapping, or ``None`` if it is not one."""
    if not isinstance(value, dict):
        return None
    narrowed: dict[str, object] = {}  # lint-ignore[restricted-object]: AI payload  # lint-ignore[raw-dict]: AI JSON
    for key in value:  # type: ignore[reportUnknownVariableType]  # rationale: dict from dynamic JSON; keys narrowed to str below
        if isinstance(key, str):
            narrowed[key] = value[key]
    return narrowed


def _to_int(value: object) -> int | None:  # lint-ignore[restricted-object]: AI JSON field
    """Narrow a JSON field to ``int`` (rejecting booleans, which are ints)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _decode_json(text: str) -> object:  # lint-ignore[restricted-object]: AI JSON boundary
    """Decode AI JSON text into an untyped ``object`` (the dynamic boundary)."""
    return json.loads(text)  # type: ignore[reportAny]  # rationale: json.loads returns Any; object is the dynamic boundary


# A markdown fenced block needs at least an opening fence line, one content
# line and a closing fence line.
_MIN_FENCE_LINES: Final = 3


def extract_json_text(text: str) -> str:
    """Return the JSON payload embedded in ``text``, tolerating markdown fences.

    Endpoints that ignore ``response_format`` (some free-tier providers) return
    the JSON wrapped in a code block — with a language tag (`` ```json ``) or
    without (plain `` ``` ``) — or surrounded by prose. Three layers of
    tolerance, in order:

    1. bare JSON object (the fast path — real structured-output endpoints);
    2. a markdown fenced block, whatever the language tag;
    3. the substring between the first ``{`` and the last ``}``.

    Text with no JSON object is returned unchanged, so the caller's parse
    error stays truthful about the raw output.
    """
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return text

    lines = stripped.splitlines()
    if len(lines) >= _MIN_FENCE_LINES and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
        body = "\n".join(lines[1:-1])
        if body.strip():
            return body

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


# The closed score scale; an AI fit_score outside this range is a per-item error.
_MIN_SCORE: Final = 1
_MAX_SCORE: Final = 5

# OpenAI Structured Outputs JSON Schema for scoring. The model is forced to
# produce ``{"fit_score": <int 1..5>, "comment": <string>}``. The ``comment``
# field is a freeform string — its internal structure (missing, explanation,
# requirements, ...) is intentionally NOT in the schema; the user may restructure
# the prompt's ``<output_format>`` freely and the model puts whatever it
# produces into the string.
_SCORING_SCHEMA: Final = JsonSchemaFormat(
    name="scoring_result",
    schema={
        "type": "object",
        "properties": {
            "fit_score": {"type": "integer", "minimum": _MIN_SCORE, "maximum": _MAX_SCORE},
            "comment": {"type": "string"},
        },
        "required": ["fit_score", "comment"],
        "additionalProperties": False,
    },
    strict=True,
)


# --- Parsing -----------------------------------------------------------------
def _parse_score_json(text: str) -> Result[ScoreResult, ScoreError]:
    """Validate the structured-output JSON into a :class:`ScoreResult`.

    An out-of-range fit_score (not in 1..5), missing/invalid fields, or
    malformed JSON each become a per-item :class:`ScoreError` — never a batch
    abort. JSON wrapped in markdown fences or prose (endpoints that ignore
    ``response_format``) is extracted first and parses normally; the
    ``comment`` string is accepted verbatim, its internal structure is not
    validated.
    """
    extracted = extract_json_text(text)
    if extracted != text:
        logger.warning(
            "AI endpoint ignored response_format; extracted JSON from markdown-fenced or prose-wrapped output"
        )
    try:
        decoded = _decode_json(extracted)
    except json.JSONDecodeError as exc:
        return Err(ScoreError(message=f"AI returned invalid JSON: {exc}", raw=text))

    data = _as_str_keyed_dict(decoded)

    if data is None:
        return Err(ScoreError(message="AI score result is not a JSON object", raw=text))

    raw_score = data.get("fit_score")
    comment = data.get("comment")

    score = _to_int(raw_score)
    if score is None or not isinstance(comment, str):
        return Err(
            ScoreError(
                message="AI score result must contain an integer 'fit_score' and a string 'comment'",
                raw=text,
            )
        )
    if not _MIN_SCORE <= score <= _MAX_SCORE:
        return Err(
            ScoreError(
                message=f"AI fit_score {score} is out of range 1..5",
                raw=text,
            )
        )
    return Ok(ScoreResult(fit_score=score, comment=comment))


# --- DEBUG logging helpers ---------------------------------------------------
def _message_content_to_sdk(
    content: MessageContent,
) -> str | list[dict[str, object]]:  # lint-ignore[restricted-object]: SDK JSON  # lint-ignore[raw-dict]: SDK dict
    """Convert the typed :class:`MessageContent` into the SDK's message content.

    A plain string passes through; a ``(str | ImageUrl, ...)`` tuple becomes
    the multimodal content-part list (``{"type": "text"}`` /
    ``{"type": "image_url"}`` entries).
    """
    if isinstance(content, str):
        return content
    parts: list[dict[str, object]] = []  # lint-ignore[restricted-object]: AI payload  # lint-ignore[raw-dict]: AI JSON
    for part in content:
        if isinstance(part, ImageUrl):
            parts.append({"type": "image_url", "image_url": {"url": part.url}})
        else:
            parts.append({"type": "text", "text": part})
    return parts


def _messages_to_sdk(
    messages: tuple[ChatMessage, ...],
) -> list[dict[str, object]]:  # lint-ignore[restricted-object]: SDK JSON  # lint-ignore[raw-dict]: SDK dict
    """Convert typed messages into the SDK's untyped message dicts."""
    return [{"role": message.role, "content": _message_content_to_sdk(message.content)} for message in messages]


def _format_request(request: ChatRequest) -> str:
    """Pretty-print the outgoing request as indented JSON for DEBUG logs.

    The typed :class:`ChatRequest` carries no credentials by design — the API
    key lives only inside the SDK client constructor and never enters the
    logged payload — so no redaction is needed.
    """
    payload: dict[str, object] = {  # lint-ignore[restricted-object]: AI payload  # lint-ignore[raw-dict]: AI JSON
        "model": request.model,
        "messages": _messages_to_sdk(request.messages),
    }
    if request.reasoning_effort is not None:
        payload["reasoning_effort"] = request.reasoning_effort
    if request.max_tokens is not None:
        payload["max_tokens"] = request.max_tokens
    if isinstance(request.response_format, JsonSchemaFormat):
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": request.response_format.name,
                "schema": dict(request.response_format.schema),
                "strict": request.response_format.strict,
            },
        }
    elif request.response_format is not None:
        payload["response_format"] = {"type": request.response_format}
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _format_response(text: str) -> str:
    """Pretty-print the AI text when it is JSON; otherwise keep it verbatim.

    The raw text matters for diagnostics (e.g. invalid-JSON responses), so
    unparseable output is logged exactly as received.
    """
    try:
        decoded = _decode_json(text)
    except json.JSONDecodeError:
        return text
    return json.dumps(decoded, indent=2, ensure_ascii=False)


# --- Concrete implementation ---------------------------------------------------
# Retry: up to this many total attempts per AI call, with exponential backoff
# (base * 2^(attempt-1) seconds) between attempts. Retries cover transient
# network/API failures AND structured-output validation failures: a model that
# returns invalid JSON (or an out-of-range score) is asked again, so a single
# malformed response never fails the item. Only when every attempt fails does
# the loop return the last failure (an exception message or the parse error).
_MAX_ATTEMPTS: Final = 3
_BACKOFF_BASE_SECONDS: Final = 1.0

# Retry-loop generics: the loop validates each attempt's output through an
# injectable parse step, so validation lives inside the retry budget.
_T = TypeVar("_T")
_E = TypeVar("_E")


def _accept_text(text: str) -> Result[str, str]:
    """The default parse step for plain-text outputs (cover letters).

    Cover-letter generation has no structured output, so any text the seam
    returns is accepted verbatim. (Empty content never reaches here: the
    default seam raises :class:`AiBoundaryError`, which the retry loop treats
    as a transient failure.)
    """
    return Ok(text)


async def call_with_retry[T, E](
    completions: CompletionsFn,
    request: ChatRequest,
    *,
    parse: Callable[[str], Result[T, E]],
) -> Result[T, E | str]:
    """Call the completions seam with up to :data:`_MAX_ATTEMPTS` attempts.

    The one retry loop shared by every AI consumer (scoring, cover letters, hh
    test solving). Both failure classes are retried with exponential backoff
    (``_BACKOFF_BASE_SECONDS * 2^(attempt-1)``):

    - exceptions from the seam (transient network/API failures), and
    - output that fails ``parse`` validation — a model returning invalid
      JSON or an out-of-range score is simply asked again.

    When the budget is exhausted the *last* failure is returned: the
    exception message (``str``) or the parse step's own error value.

    Every exchange is logged at DEBUG (request once, each attempt's raw
    response and failures) — the diagnostics trail for debugging AI output
    problems (e.g. invalid JSON).
    """
    logger.debug("AI request:\n%s", _format_request(request))
    last_failure: E | str | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            content = await completions(request)
        except Exception as exc:
            last_failure = f"AI call failed after {_MAX_ATTEMPTS} attempts: {exc}"
            logger.debug("AI attempt %d failed: %s", attempt, exc)
        else:
            logger.debug("AI response (attempt %d):\n%s", attempt, _format_response(content))
            parsed = parse(content)
            if parsed.is_ok:
                return Ok(parsed.unwrap())
            last_failure = parsed.unwrap_err()
            logger.debug("AI attempt %d output failed validation: %s", attempt, last_failure)
        if attempt < _MAX_ATTEMPTS:
            await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2.0 ** (attempt - 1)))
    assert last_failure is not None  # guaranteed: loop ran and every attempt failed
    return Err(last_failure)


def _build_response_format(
    fmt: ResponseFormat,
) -> dict[str, object]:  # lint-ignore[restricted-object]: SDK JSON  # lint-ignore[raw-dict]: SDK dict
    """Convert the typed :class:`ResponseFormat` into the SDK's ``response_format`` dict.

    Plain text → ``{"type": "text"}``; JSON-object mode → ``{"type":
    "json_object"}``; a JSON Schema → the Structured Outputs envelope
    ``{"type": "json_schema", "json_schema": {...}}``.
    """
    if isinstance(fmt, JsonSchemaFormat):
        return {
            "type": "json_schema",
            "json_schema": {
                "name": fmt.name,
                "schema": dict(fmt.schema),
                "strict": fmt.strict,
            },
        }
    return {"type": fmt}


def default_completions(config: OpenAIConfig) -> CompletionsFn:
    """The real completion call targeting the raw ``openai`` SDK.

    This is the only place the whole AI surface touches the SDK (scoring, cover
    letters, hh test solving, captcha vision). Network errors are allowed to
    raise here and are converted to ``Err`` by :func:`call_with_retry` (or the
    captcha handler). The request is forwarded as explicit typed kwargs (a
    ``**kwargs`` splat would make the SDK call's return type unknown to the
    type checker). Async per the migration spec: the SDK's ``AsyncOpenAI``
    client is used, and the request is ``await``ed (its
    ``client.chat.completions.create`` is a coroutine).
    """

    async def _call(request: ChatRequest) -> str:
        import openai

        client = openai.AsyncOpenAI(api_key=config.api_key, base_url=config.base_url)
        try:
            sdk_messages = _messages_to_sdk(request.messages)
            response_format = (
                _build_response_format(request.response_format)
                if request.response_format is not None
                else openai.NOT_GIVEN
            )
            completion = await client.chat.completions.create(
                model=request.model,
                messages=sdk_messages,  # type: ignore[reportArgumentType]  # rationale: OpenAI SDK accepts our message dicts; its param is an untyped union
                reasoning_effort=request.reasoning_effort,  # type: ignore[reportArgumentType]  # rationale: SDK types the param as a closed Literal (Optional[ReasoningEffort] | Omit) but the API accepts arbitrary provider-dependent values; our str passthrough is intentional, None omits
                response_format=response_format,  # type: ignore[reportArgumentType]  # rationale: SDK accepts the dict; strict-typed model differs from upstream union
                max_tokens=request.max_tokens if request.max_tokens is not None else openai.NOT_GIVEN,  # type: ignore[reportArgumentType]  # rationale: None must be omitted (not sent as null), so NOT_GIVEN is substituted; SDK union differs from our Optional[int]
            )
            content = completion.choices[0].message.content
            if content is None:
                raise AiBoundaryError("OpenAI returned an empty completion")
            return content
        finally:
            # Release the per-call client's httpx connection pool immediately,
            # even when the completion raises; AsyncOpenAI.close is a coroutine.
            # The client is created per call (never cached) so the default seam
            # stays stateless and connections never leak until GC.
            await client.close()

    return _call


class OpenAIWrapper:
    """A typed, mockable :class:`AiClient` over the raw ``openai`` SDK.

    Args:
        config: the ``openai`` section of the pipeline config (model, base_url,
            api_key).
        completions: an optional low-level completion callable. When omitted, a
            default implementation against the real ``openai`` SDK is used; tests
            inject a stub so no network is ever hit.
    """

    def __init__(self, config: OpenAIConfig, *, completions: CompletionsFn | None = None) -> None:
        self._config = config
        self._completions = completions if completions is not None else default_completions(config)

    async def score(self, prompt: str) -> Result[ScoreResult, ScoreError]:
        """Score a vacancy, returning a validated 1..5 result or a per-item error.

        The prompt is sent as a single ``user`` message with a JSON Schema
        (Structured Outputs) forcing ``{"fit_score": int, "comment": str}``.
        Structured-output validation lives inside the retry loop: invalid JSON
        or an out-of-range score is retried (up to :data:`_MAX_ATTEMPTS`), and
        only a persistent failure becomes a per-item :class:`ScoreError`.
        """
        request = ChatRequest(
            model=self._config.model,
            messages=(ChatMessage(role="user", content=prompt),),
            reasoning_effort=self._config.reasoning_effort,
            response_format=_SCORING_SCHEMA,
        )
        call_result = await call_with_retry(self._completions, request, parse=_parse_score_json)
        if call_result.is_err:
            error = call_result.unwrap_err()
            if isinstance(error, str):
                return Err(ScoreError(message=error))
            return Err(error)
        return Ok(call_result.unwrap())

    async def generate_cover_letter(self, prompt: str) -> Result[str, str]:
        """Generate a cover-letter body, returning the text or an error message.

        The prompt is sent as a single ``user`` message with plain-text output.
        Transient network failures are retried; there is no structured output
        to validate.
        """
        request = ChatRequest(
            model=self._config.model,
            messages=(ChatMessage(role="user", content=prompt),),
            reasoning_effort=self._config.reasoning_effort,
            response_format="text",
        )
        call_result = await call_with_retry(self._completions, request, parse=_accept_text)
        if call_result.is_err:
            return Err(call_result.unwrap_err())
        return Ok(call_result.unwrap())
