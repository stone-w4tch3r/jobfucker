"""Typed source for the generated ``pipeline.yaml`` JSON Schema.

The repo-root ``pipeline.schema.json`` is a generated artifact: ``uv run poe
schemas`` renders it from the typed models below, the same way the
vacancies-dump schema is rendered from
:class:`~jobfucker.app.vacancy_document_schema.VacancyDumpDocument`.

The models describe the AUTHORED yaml, not the loader's in-memory shape:
``config.py`` validates the post-load config (``service`` reduced to the
client-selector string, referenced files injected into content fields), while
the editor-facing schema must describe the yaml as written — ``service`` as a
one-board mapping and every content slot as EITHER a ``*_file``/``path``
reference OR an inline value. That exclusivity is enforced at runtime by the
loader's ``_SLOT_GROUPS`` pre-check; it is mirrored here as schema-level
``oneOf``/``not`` constraints so editors flag mistakes too.

Board sections are NOT duplicated: ``mock``/``hh`` reuse the registered
``SERVICE_SECTION_MODELS`` models directly, so a filter/vacancy/behavior change
lands in the schema on the next ``poe schemas`` run. Parity tests guard the
authored slot models against field drift from the loader models.

The schema is deliberately STRICTER than the loader: these models are closed
(``extra="forbid"``) and type-exact (no pydantic lax coercion — an unquoted
``daily_apply_limit: "50"`` string is schema-rejected but loader-accepted),
and explicit yaml nulls (``login:`` with nothing after it) are rejected even
though the in-memory ``| None`` annotations would allow them.

These models are schema-generation sources only: they are never instantiated by
the app, and they carry no validators — validation semantics live in
``config.py`` (single source of truth).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from jobfucker.clients.hh.config import HHServiceConfig
from jobfucker.clients.mock.params import MockServiceConfig
from jobfucker.schema_source import SchemaSource

PIPELINE_SCHEMA_ID: Final = (
    "https://raw.githubusercontent.com/stone-w4tch3r/jobfucker/main/docs/schemas/pipeline.schema.json"
)


def _xor(file_key: str, inline_key: str) -> dict[str, JsonValue]:  # lint-ignore[raw-dict]: JSON Schema fragment
    """Schema constraint: exactly one of the two slot keys must be present."""
    return {"oneOf": [{"required": [file_key]}, {"required": [inline_key]}]}


def _not_both(file_key: str, inline_key: str) -> dict[str, JsonValue]:  # lint-ignore[raw-dict]: JSON Schema fragment
    """Schema constraint: at most one of the two (both-optional) slot keys."""
    return {"not": {"allOf": [{"required": [file_key]}, {"required": [inline_key]}]}}


def _not_null() -> dict[str, JsonValue]:  # lint-ignore[raw-dict]: JSON Schema fragment
    """Schema constraint rejecting an explicit yaml null slot value.

    The annotations stay ``| None`` only so the keys are optional; a written
    ``key:`` with nothing after it (yaml null) is an author error the loader
    rejects. Extras MERGE with the generated ``anyOf [$ref/type, null]`` schema
    (they cannot remove it), but jsonschema evaluates all keywords, so the
    added ``not: {type: null}`` wins.
    """
    return {"not": {"type": "null"}}


# Authored slot value: absent OR a string; explicit null is rejected (see
# :func:`_not_null`). Loader twins carry plain ``str``/``str | None`` post-load.
SlotStr = Annotated[str | None, Field(default=None, json_schema_extra=_not_null())]


# --- Authored content-slot sections -----------------------------------------
class PipelineAuthYaml(BaseModel):
    """Authored ``auth`` section: one source per credential slot.

    Schema twin of ``AuthConfig`` (``config.py``) — edit both together; the
    ``test_authored_models_match_loader_fields`` parity test guards field names.
    """

    model_config = ConfigDict(
        title="Credentials",
        extra="forbid",
        json_schema_extra={  # lint-ignore[raw-dict]: JSON Schema constraint fragment
            "allOf": [_xor("login_file", "login"), _xor("password_file", "password")]
        },
    )

    login_file: SlotStr
    login: SlotStr
    password_file: SlotStr
    password: SlotStr


class PipelineResumeYaml(BaseModel):
    """Authored ``resume`` section: a path reference OR inline contents.

    Schema twin of ``ResumeConfig`` (``config.py``) — edit both together.
    """

    model_config = ConfigDict(
        title="Resume",
        extra="forbid",
        json_schema_extra=_xor("path", "contents"),  # lint-ignore[raw-dict]: JSON Schema constraint fragment
    )

    path: SlotStr
    contents: SlotStr


class PipelineOpenAiYaml(BaseModel):
    """Authored ``openai``/``openai_captcha`` section (shared shape).

    The api key is optional and may come from either source, never both.
    ``consensus_requests`` is meaningful only for ``openai_captcha``.

    Schema twin of ``OpenAIConfig`` (``config.py``) — edit both together.
    """

    model_config = ConfigDict(
        title="OpenAI settings",
        extra="forbid",
        json_schema_extra=_not_both("api_key_file", "api_key"),  # lint-ignore[raw-dict]: JSON Schema fragment
    )

    model: str
    base_url: str
    api_key_file: SlotStr
    api_key: SlotStr
    reasoning_effort: SlotStr
    consensus_requests: int = Field(default=4, ge=1, le=10)


class PipelineScoringYaml(BaseModel):
    """Authored ``scoring`` section: threshold + exactly one prompt source.

    Schema twin of ``ScoringConfig`` (``config.py``) — edit both together.
    """

    model_config = ConfigDict(
        title="Scoring",
        extra="forbid",
        json_schema_extra=_xor("scoring_prompt_file", "scoring_prompt"),  # lint-ignore[raw-dict]: JSON Schema fragment
    )

    min_required_score: int
    scoring_prompt_file: SlotStr
    scoring_prompt: SlotStr


class PipelineApplyYaml(BaseModel):
    """Authored ``apply`` section: exactly one cover-letter prompt source.

    Schema twin of ``ApplyConfig`` (``config.py``) — edit both together.
    """

    model_config = ConfigDict(
        title="Apply",
        extra="forbid",
        json_schema_extra=_xor("apply_prompt_file", "apply_prompt"),  # lint-ignore[raw-dict]: JSON Schema fragment
    )

    apply_prompt_file: SlotStr
    apply_prompt: SlotStr


class PipelineHhTestSolvingYaml(BaseModel):
    """Authored ``hh_test_solving`` section (optional, HH-specific).

    Schema twin of ``HhTestSolvingConfig`` (``config.py``) — edit both
    together. Both prompt sources are optional (absence = the AI solver is
    unavailable; the ``--test-answers`` file solver still works).
    ``allow_ai_to_skip_test_when_not_enough_context`` opts into the AI decline
    fallback (per-vacancy skip with the AI's comment, never a failure).
    """

    model_config = ConfigDict(
        title="HH test solving",
        extra="forbid",
        json_schema_extra={  # lint-ignore[raw-dict]: JSON Schema fragment
            "description": (
                "HH-only screening-test solving: AI-solver prompt + enable toggle. "
                "The AI solver reuses the main `openai` section."
            ),
            **_not_both("test_prompt_file", "test_prompt"),
        },
    )

    enabled: bool = True
    allow_ai_to_skip_test_when_not_enough_context: bool = False
    test_prompt_file: SlotStr
    test_prompt: SlotStr


class PipelineLimitsYaml(BaseModel):
    """Authored ``limits`` section.

    Schema twin of ``LimitsConfig`` (``config.py``) — edit both together.
    """

    model_config = ConfigDict(title="Limits", extra="forbid")

    daily_apply_limit: int = 50


# --- Service mapping --------------------------------------------------------
class PipelineServiceYaml(BaseModel):
    """The authored one-board ``service`` mapping.

    Section models are reused from ``SERVICE_SECTION_MODELS`` (the same objects
    ``config.py`` validates against), so the schema can never drift from what
    the loader actually accepts. No schema twin — a new board needs a field
    here AND a registry entry; the service-registry parity test guards the pair.
    """

    model_config = ConfigDict(
        title="Board service",
        extra="forbid",
        json_schema_extra={  # lint-ignore[raw-dict]: JSON Schema constraint fragment
            "description": "Exactly one board key; the single key selects the client.",
            "oneOf": [{"required": ["mock"]}, {"required": ["hh"]}],
        },
    )

    mock: MockServiceConfig | None = Field(default=None, json_schema_extra=_not_null())
    hh: HHServiceConfig | None = Field(default=None, json_schema_extra=_not_null())


# --- Root -------------------------------------------------------------------
class PipelineYamlDocument(BaseModel):
    """The authored ``pipeline.yaml`` top level (schema-generation source).

    Schema twin of ``PipelineConfig`` (``config.py``) — edit both together;
    the ``test_authored_models_match_loader_fields`` parity test guards fields.
    """

    model_config = ConfigDict(
        title="jobfucker pipeline.yaml",
        extra="forbid",
        json_schema_extra={  # lint-ignore[raw-dict]: JSON Schema fragment
            "description": (
                "Authored pipeline.yaml (one board per file). Generated from the typed "
                "contract — change the models, then run `uv run poe schemas`."
            )
        },
    )

    name: str
    description: str
    service: PipelineServiceYaml
    auth: PipelineAuthYaml
    resume: PipelineResumeYaml
    openai: PipelineOpenAiYaml
    openai_captcha: PipelineOpenAiYaml | None = None
    scoring: PipelineScoringYaml
    apply: PipelineApplyYaml
    limits: PipelineLimitsYaml
    hh_test_solving: PipelineHhTestSolvingYaml | None = None


PIPELINE_SCHEMA_SOURCE: Final = SchemaSource(
    name="pipeline",
    schema_id=PIPELINE_SCHEMA_ID,
    model=PipelineYamlDocument,
    artifact_paths=(Path("docs/schemas/pipeline.schema.json"),),
)
