"""``pipeline.yaml`` parsing + validation (Phase 4, task 4.1).

The pipeline config is the single YAML file that describes one automated
job-application "pipeline": which board it targets, the search query, auth and
resume sources, the OpenAI settings used for scoring / cover-letter generation
(and, independently, for AI captcha solving), scoring/apply prompt templates,
and the per-pipeline daily apply limit.

Design notes / decisions:

- **pydantic at the boundary.** The YAML is decoded into pydantic models. A
  nested ``service.<board>`` section is validated into the concrete per-board
  pydantic model (e.g. ``HHServiceConfig`` or ``MockServiceConfig``) that implements the contract's
  ``ServiceConfigSection`` protocol. The registry ``SERVICE_SECTION_MODELS``
  maps a board name to its section model, so a new board registers its section
  here without touching the rest of the config.
- **``service`` is the per-board mapping.** The YAML's ``service`` value is a
  mapping with exactly one board key (a pipeline targets exactly one board);
  that single key IS the client-selector string stored on
  ``PipelineConfig.service``.
- **Referenced files are loaded and injected.** Credential files, the resume,
  the API key and the prompt templates are resolved relative to the pipeline
  file's directory (absolute references are used as-is), read, and their
  contents embedded in the returned config. File contents are
  ``strip()``ed at load: a referenced file conventionally ends with a trailing
  newline (an editor/``echo`` artifact, not part of the value), and a raw
  newline inside a credential or API key would corrupt it downstream. Inline
  values are carried exactly as authored. A missing referenced file is an
  ``Err`` with a clear message, as is malformed YAML.
- **Inline values are allowed.** Every "content" slot (login/password, resume,
  api_key, scoring/apply prompt) accepts EITHER a referenced file
  (``*_file``/``path``) OR an inline value (``login``/``password``,
  ``contents``, ``api_key``, ``scoring_prompt``/``apply_prompt``). The loader
  enforces "exactly one source per slot" at YAML-load time; in-memory and
  reconstructed configs may legitimately hold both the path and its content.
- **Prompt templates are captured, not rendered.** ``scoring_prompt`` /
  ``apply_prompt`` hold the raw template text; rendering (resume + vacancy
  injection) is Phase 5.
- **No ORM dependency.** ``config.py`` never imports the storage models.

Expected failures (malformed YAML, missing files, bad values, unknown board)
return ``Result``; a programming/invariant error would raise.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError
from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import SearchWindow, ServiceConfigSection
from jobfucker.clients.factory import Factory
from jobfucker.clients.hh.config import HHServiceConfig
from jobfucker.clients.mock.params import MockServiceConfig

# Board name -> concrete ``service.<board>`` section model. The registry
# includes the available first-party clients. Each model implements
# ``ServiceConfigSection``.
SectionModel = type[BaseModel]
SERVICE_SECTION_MODELS: dict[str, SectionModel] = {  # lint-ignore[module-mutable-state]: m  # lint-ignore[raw-dict]: m
    "hh": HHServiceConfig,
    "mock": MockServiceConfig,
}


# --- Section models ---------------------------------------------------------
class AuthConfig(BaseModel):
    """Credentials block: optional file path AND/OR the inline secret contents.

    The engine/CLI never read the credential files directly after load; the
    secret text is carried in ``login`` / ``password`` on the validated config.

    Each ``*_file`` field is optional so a config may provide the credentials
    either from a referenced file (``login_file``/``password_file``) or inline
    (``login``/``password``). The loader enforces "exactly one source per slot"
    at YAML-load time (see :func:`load_pipeline_config`); in-memory/reconstructed
    configs may legitimately hold both the path and its resolved content.

    Schema twin: ``PipelineAuthYaml`` (``app/pipeline_schema_source.py``)
    """

    login_file: Path | None = None
    password_file: Path | None = None
    login: str = ""  # loaded contents of ``login_file`` or the inline value
    password: str = ""  # loaded contents of ``password_file`` or the inline value


class ResumeConfig(BaseModel):
    """Resume block: optional path plus the loaded markdown contents.

    ``path`` is optional so the resume may be given inline via ``contents``
    instead of a referenced file; the loader enforces "exactly one source" at
    YAML-load time, while in-memory/reconstructed configs may hold both.

    Schema twin: ``PipelineResumeYaml`` (``app/pipeline_schema_source.py``)
    """

    path: Path | None = None
    contents: str = ""


class OpenAIConfig(BaseModel):
    """OpenAI settings for one purpose (scoring/cover-letter OR captcha).

    ``openai_captcha`` is an independent section (never a fallback to the main
    ``openai`` model/key) but has the same shape, so it reuses this model.

    ``reasoning_effort`` is a free-form passthrough of the provider's
    ``reasoning_effort`` parameter; allowed values are provider-dependent (e.g.
    OpenAI: low/medium/high; Gemini: true/false), so any string is accepted and
    forwarded verbatim. ``None`` omits the parameter.

    ``consensus_requests`` is meaningful only for the ``openai_captcha``
    section: the number of vision requests (K) fired per captcha attempt, with
    the exact-majority answer applied. Ignored by the main ``openai`` section.

    Schema twin: ``PipelineOpenAiYaml`` (``app/pipeline_schema_source.py``)
    """

    model: str
    base_url: str
    api_key_file: Path | None = None
    api_key: str | None = None  # loaded contents of ``api_key_file``
    reasoning_effort: str | None = None
    consensus_requests: int = Field(
        default=4,
        ge=1,
        le=10,
        title="Vision requests per captcha attempt",
        description=(
            "K vision requests fired at one captcha image per attempt; the "
            "exact-majority answer is submitted. 1 = single request (no consensus)."
        ),
    )


class ScoringConfig(BaseModel):
    """Scoring settings: pass threshold + the scoring prompt template.

    Schema twin: ``PipelineScoringYaml`` (``app/pipeline_schema_source.py``)
    """

    min_required_score: int
    scoring_prompt_file: Path | None = None
    scoring_prompt: str = ""  # raw template text


class ApplyConfig(BaseModel):
    """Apply settings: the cover-letter prompt template.

    Schema twin: ``PipelineApplyYaml`` (``app/pipeline_schema_source.py``)
    """

    apply_prompt_file: Path | None = None
    apply_prompt: str = ""


class HhTestSolvingConfig(BaseModel):
    """HH screening-test solving settings: enable toggle + the prompt template.

    Optional top-level section (absent = AI test solving unavailable; the
    ``--test-answers`` file solver still works). The AI solver reuses the main
    ``openai`` section (test solving is applicant-profile work, not
    captcha-class throwaway vision — the ``openai_captcha`` independence
    precedent does not apply). ``enabled`` duplicates the ``--no-test-ai`` CLI
    flag at the config level (flag wins).

    ``allow_ai_to_skip_test_when_not_enough_context`` opts into the AI decline
    fallback: the solver is offered — and only then recognizes — the
    ``{"not_enough_context_for_test": "<comment>"}`` envelope, which the apply
    stage stores as a per-vacancy skip (never a failure). Off by default: the
    model must answer or fail.

    Schema twin: ``PipelineHhTestSolvingYaml`` (``app/pipeline_schema_source.py``)
    """

    enabled: bool = True
    allow_ai_to_skip_test_when_not_enough_context: bool = False
    test_prompt_file: Path | None = None
    test_prompt: str = ""  # raw template text


class LimitsConfig(BaseModel):
    """Per-pipeline limit, validated at config time against the client cap.

    Schema twin: ``PipelineLimitsYaml`` (``app/pipeline_schema_source.py``)
    """

    daily_apply_limit: int = 50


class PipelineConfig(BaseModel):
    """The validated top-level ``pipeline.yaml`` model.

    ``service`` is the client-selector string (the single board key of the
    YAML's ``service`` mapping); ``service_section`` is the validated per-board
    section (a :class:`ServiceConfigSection` implementation, e.g.
    :class:`MockServiceConfig` for ``service`` == ``"mock"``).

    Schema twin: ``PipelineYamlDocument`` (``app/pipeline_schema_source.py``)
    describes the AUTHORED yaml shape
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    name: str
    description: str
    service: str
    auth: AuthConfig
    resume: ResumeConfig
    openai: OpenAIConfig
    openai_captcha: OpenAIConfig | None = None
    scoring: ScoringConfig
    apply: ApplyConfig
    limits: LimitsConfig
    hh_test_solving: HhTestSolvingConfig | None = None

    # The concrete per-board section. Held as a (pydantic-unvalidated) private
    # attribute because ``ServiceConfigSection`` is a Protocol; pydantic cannot
    # generate a schema for it. The value is a fully-validated concrete model
    # attached by the loader (its own pydantic model validated the shape).
    _service_section: ServiceConfigSection | None = PrivateAttr(default=None)

    def set_service_section(self, section: ServiceConfigSection) -> None:
        """Attach the validated per-board section (loader-internal)."""
        object.__setattr__(self, "_service_section", section)

    @property
    def service_section(self) -> ServiceConfigSection:
        """The validated per-board section (``resume_id`` + board-specific fields)."""
        if self._service_section is None:
            raise RuntimeError("service_section was not populated by the loader")
        return self._service_section


# --- Path resolution --------------------------------------------------------
def _read_text(path: Path) -> Result[str, str]:
    """Read a text file as ``Ok`` or a clear ``Err`` on any failure."""
    try:
        return Ok(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return Err(f"Could not read referenced file {path}: {exc}")


# --- Typed YAML narrowing ---------------------------------------------------
def _parse_yaml(text: str) -> object:  # lint-ignore[restricted-object]: PyYAML boundary; narrowed below
    """Parse YAML text into an ``object`` (dynamic boundary)."""
    return yaml.safe_load(text)  # type: ignore[reportAny]  # rationale: PyYAML returns Any; object is the dynamic boundary


def _as_str_keyed_dict(
    value: object,  # lint-ignore[restricted-object]: YAML boundary value; keys narrowed
) -> dict[str, object] | None:  # lint-ignore[restricted-object]: YAML payload  # lint-ignore[raw-dict]: YAML dict
    """Return ``value`` as a str-keyed mapping, or ``None`` if not a mapping."""
    if not isinstance(value, dict):
        return None
    narrowed: dict[str, object] = {}  # lint-ignore[restricted-object]: YAML payload to pydantic
    for key in value:  # type: ignore[reportUnknownVariableType]  # rationale: dict from dynamic YAML; keys narrowed to str below
        if isinstance(key, str):
            narrowed[key] = value[key]
    return narrowed


# --- Loader -----------------------------------------------------------------
def _parse_pipeline_config_yaml(
    path: Path, text: str
) -> Result[dict[str, object], str]:  # lint-ignore[restricted-object]: YAML payload  # lint-ignore[raw-dict]: YAML dict
    """Parse YAML text into a str-keyed dict, or a clear ``Err``."""
    try:
        parsed = _parse_yaml(text)
    except yaml.YAMLError as exc:
        return Err(f"Malformed YAML in {path}: {exc}")
    data = _as_str_keyed_dict(parsed)
    if data is None:
        return Err(f"pipeline.yaml at {path} must be a mapping, got {type(parsed).__name__}")
    return Ok(data)


@dataclass(frozen=True, slots=True)
class _ContentSlot:
    """One exclusive-or source pair within a ``pipeline.yaml`` section.

    Describes the ``file_key``/``inline_key`` pair plus whether the slot is
    REQUIRED (a missing source is an error) or OPTIONAL (both absent is fine).
    """

    section: str
    file_key: str
    inline_key: str
    required: bool


# Each nested section's content slots. ``openai`` / ``openai_captcha``
# ``api_key`` is optional: both absent is fine, both present is an error. All
# other slots are required: exactly one of file/inline must be provided.
_SLOT_GROUPS: tuple[tuple[str, tuple[_ContentSlot, ...]], ...] = (
    (
        "auth",
        (
            _ContentSlot("auth", "login_file", "login", required=True),
            _ContentSlot("auth", "password_file", "password", required=True),
        ),
    ),
    ("resume", (_ContentSlot("resume", "path", "contents", required=True),)),
    ("openai", (_ContentSlot("openai", "api_key_file", "api_key", required=False),)),
    ("openai_captcha", (_ContentSlot("openai_captcha", "api_key_file", "api_key", required=False),)),
    ("scoring", (_ContentSlot("scoring", "scoring_prompt_file", "scoring_prompt", required=True),)),
    ("apply", (_ContentSlot("apply", "apply_prompt_file", "apply_prompt", required=True),)),
    (
        "hh_test_solving",
        (_ContentSlot("hh_test_solving", "test_prompt_file", "test_prompt", required=False),),
    ),
)


def _check_one_slot(
    slot: _ContentSlot,
    section_dict: dict[str, object],  # lint-ignore[restricted-object]: YAML payload  # lint-ignore[raw-dict]: dict
) -> Result[None, str]:
    """Validate one exclusive-or slot against a raw section mapping."""
    has_file = slot.file_key in section_dict
    has_inline = slot.inline_key in section_dict
    if has_file and has_inline:
        return Err(f"{slot.section}: provide either '{slot.file_key}' or '{slot.inline_key}', not both")
    if not has_file and not has_inline and slot.required:
        return Err(f"{slot.section}: provide one of '{slot.file_key}' or '{slot.inline_key}'")
    return Ok(None)


def _check_content_slots(
    data: dict[str, object],  # lint-ignore[restricted-object]: YAML payload  # lint-ignore[raw-dict]: YAML dict
) -> Result[None, str]:
    """Enforce "exactly one source per content slot" on the raw YAML dict.

    Runs per nested section only when that section is present as a mapping (a
    fully absent section is left to pydantic to report as "field required").
    """
    for section_name, slots in _SLOT_GROUPS:
        section_dict = _as_str_keyed_dict(data.get(section_name))
        if section_dict is None:
            continue
        for slot in slots:
            check = _check_one_slot(slot, section_dict)
            if check.is_err:
                return Err(check.unwrap_err())
    return Ok(None)


def load_pipeline_config(path: Path) -> Result[PipelineConfig, str]:
    """Parse and validate ``pipeline.yaml`` at ``path`` (see module docstring).

    Args:
        path: the ``pipeline.yaml`` path to load.

    Returns:
        ``Ok(PipelineConfig)`` with referenced-file contents injected, or an
        ``Err`` with a clear message for malformed YAML, a missing referenced
        file, an unknown board, or invalid values.

    Relative file references resolve against the pipeline file's own directory
    (``../`` climbs out; absolute references are used as-is). ``~`` in the
    pipeline path and in file references expands to the user's home.
    """
    path = path.expanduser()
    base_dir = path.resolve().parent
    read = _read_text(path)
    if read.is_err:
        return Err(read.unwrap_err())
    parsed_data = _parse_pipeline_config_yaml(path, read.unwrap())
    if parsed_data.is_err:
        return Err(parsed_data.unwrap_err())
    data = parsed_data.unwrap()

    section_result = _validate_service_section(data)
    if section_result.is_err:
        return Err(section_result.unwrap_err())
    board_name, service_section = section_result.unwrap()

    # Exclusive-or pre-check: each content slot must use exactly one source
    # (file reference OR inline value). Runs on the raw YAML before pydantic
    # so an ambiguous/empty slot is a clear, targeted error rather than a
    # file-read or a field-required message buried in model validation.
    slot_check = _check_content_slots(data)
    if slot_check.is_err:
        return Err(slot_check.unwrap_err())

    # Build the top-level config with ``service`` set to the derived selector.
    rest: dict[str, object] = {  # lint-ignore[restricted-object]: YAML payload to pydantic
        key: value for key, value in data.items() if key != "service"
    }
    rest["service"] = board_name
    try:
        parsed_config = PipelineConfig.model_validate(rest)
    except ValidationError as exc:
        return Err(f"Invalid pipeline.yaml at {path}: {exc}")

    injected = _inject_contents(parsed_config, base_dir)
    if injected.is_err:
        return Err(injected.unwrap_err())
    config = injected.unwrap()
    config.set_service_section(service_section)
    return Ok(config)


def _validate_service_section(
    data: dict[str, object],  # lint-ignore[restricted-object]: YAML payload  # lint-ignore[raw-dict]: YAML section dict
) -> Result[tuple[str, ServiceConfigSection], str]:
    """Pull the ``service.<board>`` mapping and validate it into its model.

    ``service`` must be a mapping with exactly one board key (a pipeline
    targets exactly one board). The section value is validated against the
    registered per-board model.
    """
    service_raw = data.get("service")
    service_map = _as_str_keyed_dict(service_raw)
    if service_map is None or not service_map:
        return Err("'service' must be a mapping with exactly one board, e.g. `service: {mock: {...}}`")
    if len(service_map) != 1:
        return Err(f"'service' must target exactly one board, got {sorted(service_map)}")
    board_name, section_value = next(iter(service_map.items()))
    section_model = SERVICE_SECTION_MODELS.get(board_name)
    if section_model is None:
        known = ", ".join(sorted(SERVICE_SECTION_MODELS)) or "<none>"
        return Err(f"Unknown board {board_name!r}; known boards: {known}")
    try:
        section = section_model.model_validate(section_value)
    except ValidationError as exc:
        return Err(f"Invalid service.{board_name} section: {exc}")
    # The concrete pydantic model implements ServiceConfigSection by
    # construction (resume_id + board fields); widen statically at this one
    # registry boundary.
    typed_section: ServiceConfigSection = section  # type: ignore[assignment, reportAssignmentType]  # rationale: registered section models implement ServiceConfigSection; static widening at the registry boundary
    return Ok((board_name, typed_section))


def _resolve_content(
    ref: Path | None,
    inline: str | None,
    base_dir: Path,
) -> Result[str | None, str]:
    """Return a content slot's value: the inline value, or the file's contents.

    When ``ref`` is ``None`` the slot was configured inline (or left empty) and
    the inline value is returned unchanged — authored YAML text is never altered.
    When ``ref`` is set, a relative reference resolves against ``base_dir`` (the
    pipeline file's directory) and the file is read; ``~`` expands to the user's
    home; a missing/unreadable file is a clear ``Err``.

    File contents are ``strip()``ed before being returned: a referenced file
    conventionally ends with a trailing newline (editors, ``echo``), and a raw
    newline inside a credential or API key would corrupt the value downstream
    (the OpenAI SDK rejects a key containing one at the transport layer with a
    misleading ``APIConnectionError``).
    """
    if ref is None:
        return Ok(inline)
    expanded = ref.expanduser()
    resolved = expanded if expanded.is_absolute() else base_dir / expanded
    result = _read_text(resolved)
    if result.is_err:
        return Err(result.unwrap_err())
    return Ok(result.unwrap().strip())


def _inject_contents(config: PipelineConfig, base_dir: Path) -> Result[PipelineConfig, str]:
    """Resolve referenced files and inject their contents into the config.

    A content slot is loaded ONLY from its file when the file field is non-``None``;
    otherwise the already-present inline value is kept unchanged.
    """
    login = _resolve_content(config.auth.login_file, config.auth.login, base_dir)
    if login.is_err:
        return Err(login.unwrap_err())
    password = _resolve_content(config.auth.password_file, config.auth.password, base_dir)
    if password.is_err:
        return Err(password.unwrap_err())
    resume = _resolve_content(config.resume.path, config.resume.contents, base_dir)
    if resume.is_err:
        return Err(resume.unwrap_err())
    api_key = _resolve_content(config.openai.api_key_file, config.openai.api_key, base_dir)
    if api_key.is_err:
        return Err(api_key.unwrap_err())

    captcha_api_key: str | None = None
    if config.openai_captcha is not None:
        captcha = _resolve_content(
            config.openai_captcha.api_key_file,
            config.openai_captcha.api_key,
            base_dir,
        )
        if captcha.is_err:
            return Err(captcha.unwrap_err())
        captcha_api_key = captcha.unwrap()

    scoring_prompt = _resolve_content(config.scoring.scoring_prompt_file, config.scoring.scoring_prompt, base_dir)
    if scoring_prompt.is_err:
        return Err(scoring_prompt.unwrap_err())
    apply_prompt = _resolve_content(config.apply.apply_prompt_file, config.apply.apply_prompt, base_dir)
    if apply_prompt.is_err:
        return Err(apply_prompt.unwrap_err())

    test_prompt: str = ""
    if config.hh_test_solving is not None:
        hh_test_prompt = _resolve_content(
            config.hh_test_solving.test_prompt_file,
            config.hh_test_solving.test_prompt,
            base_dir,
        )
        if hh_test_prompt.is_err:
            return Err(hh_test_prompt.unwrap_err())
        test_prompt = hh_test_prompt.unwrap() or ""

    openai = config.openai.model_copy(update={"api_key": api_key.unwrap()})
    openai_captcha = (
        config.openai_captcha.model_copy(update={"api_key": captcha_api_key})
        if config.openai_captcha is not None
        else None
    )
    hh_test_solving = (
        config.hh_test_solving.model_copy(update={"test_prompt": test_prompt})
        if config.hh_test_solving is not None
        else None
    )
    updated = config.model_copy(
        update={
            "auth": config.auth.model_copy(update={"login": login.unwrap() or "", "password": password.unwrap() or ""}),
            "resume": config.resume.model_copy(update={"contents": resume.unwrap() or ""}),
            "openai": openai,
            "openai_captcha": openai_captcha,
            "scoring": config.scoring.model_copy(update={"scoring_prompt": scoring_prompt.unwrap() or ""}),
            "apply": config.apply.model_copy(update={"apply_prompt": apply_prompt.unwrap() or ""}),
            "hh_test_solving": hh_test_solving,
        }
    )
    return Ok(updated)


# --- Service-section persistence (rebuild a stage by --pipeline-id) ---------
def dump_service_section(section: ServiceConfigSection) -> str:
    """Serialize a validated ``service.<board>`` section to opaque JSON.

    Every registered section is a pydantic ``BaseModel``; ``model_dump`` yields
    the board-specific fields (including the concrete params object) as JSON-safe
    primitives. ``by_alias`` emits a section field's input alias(ses) instead of
    its Python name, so a field like the mock salary's lower bound round-trips
    as ``from`` (its alias) rather than ``from_`` (its Python name) — exactly
    what :func:`load_service_section` accepts back. The stored JSON is
    board-neutral from Core's perspective — Core never reads its internals,
    only re-validates it through the registry.

    Raises:
        TypeError: when the section is not a pydantic ``BaseModel`` (programming
            error — every registered section model is one).
    """
    if not isinstance(section, BaseModel):
        raise TypeError(f"service section must be a pydantic BaseModel, got {type(section).__name__}")
    return json.dumps(section.model_dump(mode="json", by_alias=True))


def load_service_section(service: str, raw: str | None) -> Result[ServiceConfigSection | None, str]:
    """Re-validate a stored opaque ``service.<board>`` JSON into its section model.

    ``raw`` is ``None`` for rows that predate the column (back-compat): the
    caller then has no section to rebuild an engine with and must treat the
    pipeline as non-runnable. Invalid JSON or an unknown board is an ``Err``
    (a corrupted/downgraded DB row is an expected failure, not a crash).

    Args:
        service: the client-selector string (selects the section model).
        raw: the stored opaque JSON, or ``None`` when absent.

    Returns:
        ``Ok`` with the re-validated section, ``Ok(None)`` when ``raw`` is
        ``None``, or ``Err`` on invalid JSON / unknown board.
    """
    if raw is None:
        return Ok(None)
    section_model = SERVICE_SECTION_MODELS.get(service)
    if section_model is None:
        return Err(f"Unknown board {service!r}; cannot rebuild its config section")
    try:
        # json.loads returns Any; handing it straight to pydantic's validating
        # constructor is the intended dynamic boundary (shape checked there).
        section = section_model.model_validate(json.loads(raw))
    except json.JSONDecodeError as exc:
        return Err(f"Stored service section for {service!r} is not valid JSON: {exc}")
    except ValidationError as exc:
        return Err(f"Stored service section for {service!r} failed validation: {exc}")
    # The concrete pydantic model implements ServiceConfigSection by
    # construction; widen statically at this registry boundary (same as
    # ``_validate_service_section``).
    typed_section: ServiceConfigSection = section  # type: ignore[assignment, reportAssignmentType]  # rationale: registered section models implement ServiceConfigSection; static widening at the registry boundary
    return Ok(typed_section)


def dump_openai_captcha(captcha: OpenAIConfig | None) -> str | None:
    """Serialize the optional AI-captcha section to opaque JSON, or ``None``.

    Unlike the pre-snapshot path-based serialization, this trusts the DB and
    embeds the ``api_key`` CONTENT too (the main ``openai`` column already holds
    the key content, and the captcha key is recovered the same way). ``None``
    when no captcha section is configured.
    """
    if captcha is None:
        return None
    return json.dumps(captcha.model_dump(mode="json"))


def load_openai_captcha(raw: str | None) -> Result[OpenAIConfig | None, str]:
    """Re-validate a stored opaque ``openai_captcha`` JSON back into a config.

    ``raw`` is ``None`` when no captcha section was stored (``Ok(None)``).
    Invalid JSON or values that fail validation is a clear ``Err`` (a corrupted
    row is an expected failure, not a crash).
    """
    if raw is None:
        return Ok(None)
    try:
        # json.loads returns Any; handing it to pydantic's validating
        # constructor is the intended dynamic boundary (shape checked there).
        captcha = OpenAIConfig.model_validate(json.loads(raw))
    except json.JSONDecodeError as exc:
        return Err(f"Stored openai_captcha is not valid JSON: {exc}")
    except ValidationError as exc:
        return Err(f"Stored openai_captcha failed validation: {exc}")
    return Ok(captcha)


def dump_hh_test_solving(section: HhTestSolvingConfig | None) -> str | None:
    """Serialize the optional hh test-solving section to opaque JSON, or ``None``.

    Mirrors :func:`dump_openai_captcha`: trusts the DB and embeds the prompt
    template CONTENT too, so a ``--pipeline-id`` run reconstructs the section
    with zero file I/O. ``None`` when the section is not configured.
    """
    if section is None:
        return None
    return json.dumps(section.model_dump(mode="json"))


def load_hh_test_solving(raw: str | None) -> Result[HhTestSolvingConfig | None, str]:
    """Re-validate a stored opaque ``hh_test_solving`` JSON back into a config.

    ``raw`` is ``None`` when no section was stored (``Ok(None)``); invalid JSON
    or failed validation is a clear ``Err`` (a corrupted row is an expected
    failure, not a crash).
    """
    if raw is None:
        return Ok(None)
    try:
        section = HhTestSolvingConfig.model_validate(json.loads(raw))
    except json.JSONDecodeError as exc:
        return Err(f"Stored hh_test_solving is not valid JSON: {exc}")
    except ValidationError as exc:
        return Err(f"Stored hh_test_solving failed validation: {exc}")
    return Ok(section)


def search_index_range_error(pool_size: int) -> str:
    """Canonical out-of-range message for a search-pool index selection."""
    if pool_size <= 0:
        return "Pipeline has no searches; the service section must declare at least one entry under searches"
    return f"Pipeline has {pool_size} search(es); --use-search-config must be a search index from 0 to {pool_size - 1}"


def with_overrides(
    config: PipelineConfig,
    *,
    search_index: int,
    query: str | None = None,
    filter_data: Mapping[str, object] | None = None,  # lint-ignore[restricted-object]: YAML payload boundary
) -> Result[PipelineConfig, str]:
    """Return a copy of ``config`` with one search entry overridden (the ``search`` preview path).

    ``search_index`` selects the pool entry; ``query`` replaces the entry's
    query; ``filter_data`` replaces its ``filter`` block — the **whole
    section** is revalidated through its own pydantic model with the entry
    swapped in — the same proven ``model_dump(by_alias=True)`` →
    ``model_validate`` roundtrip as :func:`dump_service_section`/
    :func:`load_service_section`, so alias fields (``from`` ↔ ``from_``) and
    untouched siblings (mock ``behavior``, other entries) survive intact.
    Validation is local and strict: unknown keys (``extra="forbid"``), bad
    enum values, and cross-field violations surface as ``Err`` with the
    pydantic text — never a silently ignored or broadened upstream request.

    An out-of-range ``search_index`` is an ``Err`` naming the pool size; an
    entry model without a ``filter`` field cannot be filter-overridden (fail
    fast with a message naming the board); a non-pydantic section is a
    programming error and raises (every registered section is a
    :class:`~pydantic.BaseModel`, same invariant as :func:`dump_service_section`).
    """
    if query is None and filter_data is None:
        return Ok(config)
    entries = list(config.service_section.searches)
    if not 0 <= search_index < len(entries):
        return Err(search_index_range_error(len(entries)))
    entry = entries[search_index]
    if not isinstance(entry, BaseModel):
        raise TypeError(f"search entry must be a pydantic BaseModel, got {type(entry).__name__}")
    if filter_data is not None and "filter" not in type(entry).model_fields:
        return Err(f"Board {config.service!r} has no filter to override")

    # Always copy first: ``set_service_section`` mutates in place, and the
    # override path must never leak into the caller's config.
    updated = config.model_copy()
    updated.set_service_section(config.service_section)

    entry_payload = entry.model_dump(mode="json", by_alias=True)
    if query is not None:
        entry_payload["query"] = query
    if filter_data is not None:
        entry_payload["filter"] = filter_data
    try:
        overridden_entry = type(entry).model_validate(entry_payload)
    except ValidationError as exc:
        return Err(f"Invalid search override: {exc}")
    entries[search_index] = overridden_entry

    section = updated.service_section
    if not isinstance(section, BaseModel):
        raise TypeError(f"service section must be a pydantic BaseModel, got {type(section).__name__}")
    section_payload = section.model_dump(mode="json", by_alias=True)
    section_payload["searches"] = [e.model_dump(mode="json", by_alias=True) for e in entries]
    try:
        overridden_section = type(section).model_validate(section_payload)
    except ValidationError as exc:
        return Err(f"Invalid search override: {exc}")
    # The concrete pydantic model implements ServiceConfigSection by
    # construction; widen statically at this registry boundary (same as
    # ``load_service_section``).
    typed_section: ServiceConfigSection = overridden_section  # type: ignore[assignment, reportAssignmentType]  # rationale: registered section models implement ServiceConfigSection; static widening at the registry boundary
    updated.set_service_section(typed_section)
    return Ok(updated)


@dataclass(frozen=True, slots=True)
class PersistedPipelineRefs:
    """Decoupled view of a stored ``pipelines`` row's scalar + CONTENT fields.

    Carries the loaded file CONTENTS (auth login/password, resume markdown,
    OpenAI api key, scoring/apply prompt text), the config scalars, and the
    opaque ``service_section`` / ``openai_captcha`` JSON columns. It deliberately
    never imports the storage DTO, so :mod:`config` stays storage-free; the
    composition root maps a storage row into this view and hands it to
    :func:`reconstruct_pipeline_config`, which assembles a
    :class:`PipelineConfig` with **zero file I/O** ("we trust the db").

    Content invariants: file-loaded contents are trimmed at ``init``/``update``
    time, and reconstruction re-strips the columns as a safety net, so a
    reconstructed config never carries newline artifacts.

    ``openai_model`` / ``openai_base_url`` are the config scalars;
    ``openai_reasoning_effort`` is the optional reasoning_effort scalar (str
    because it comes from the TEXT column; ``None`` when not configured);
    ``service_section`` / ``openai_captcha`` are the stored opaque JSON strings
    (or ``None``).
    """

    name: str
    description: str | None
    service: str
    login: str  # auth login content
    password: str  # auth password content
    resume: str  # resume markdown content
    openai_model: str
    openai_base_url: str | None
    openai_api_key: str  # openai api_key content
    openai_reasoning_effort: str | None  # openai reasoning_effort scalar (TEXT column value)
    min_required_score: int
    scoring_prompt: str  # scoring prompt template content
    apply_prompt: str  # apply prompt template content
    daily_apply_limit: int
    service_section: str | None  # opaque service.<board> JSON
    openai_captcha: str | None  # opaque openai_captcha JSON
    # Optional hh screening-test solving block (enabled + prompt template
    # CONTENT) as opaque JSON; ``None`` when not configured.
    hh_test_solving: str | None = None


def reconstruct_pipeline_config(refs: PersistedPipelineRefs) -> Result[PipelineConfig, str]:
    """Assemble a validated :class:`PipelineConfig` from a stored row's columns.

    This is the content-in-DB reconstruction: it reads the loaded file CONTENTS
    and config scalars from ``refs`` (the row columns), re-validates the
    persisted ``service.<board>`` section and the optional ``openai_captcha``
    JSON, and attaches them. It performs **zero file I/O** — the original
    referenced files are never re-read, so a ``--pipeline-id`` stage run works
    even if they were moved or deleted.

    The row stores contents, not paths, so the config's ``*_path``/``*_file``
    fields are rebuilt as inert placeholders; no runtime code re-reads them
    after reconstruction.

    Stored content columns are ``strip()``ed on reconstruction, mirroring the
    load-time trim: rows written before that trim (or with hand-edited values)
    must not leak newline artifacts into ``--pipeline-id`` stage runs.

    Args:
        refs: the decoupled row view (contents + scalars + opaque section JSONs).

    Returns:
        ``Ok(PipelineConfig)`` with contents embedded and the section attached,
        or ``Err`` when the stored service section is missing/invalid, the
        stored ``openai_captcha`` is invalid, or the board is unknown (re-run
        ``init`` to fix).
    """
    section_result = load_service_section(refs.service, refs.service_section)
    if section_result.is_err:
        return Err(section_result.unwrap_err())
    section = section_result.unwrap()
    if section is None:
        return Err(f"Pipeline for service {refs.service!r} has no stored service section; re-run init")

    captcha_result = load_openai_captcha(refs.openai_captcha)
    if captcha_result.is_err:
        return Err(captcha_result.unwrap_err())
    captcha = captcha_result.unwrap()
    if captcha is not None and captcha.api_key is not None:
        captcha = captcha.model_copy(update={"api_key": captcha.api_key.strip()})

    hh_test_result = load_hh_test_solving(refs.hh_test_solving)
    if hh_test_result.is_err:
        return Err(hh_test_result.unwrap_err())
    hh_test_solving = hh_test_result.unwrap()
    if hh_test_solving is not None:
        hh_test_solving = hh_test_solving.model_copy(update={"test_prompt": hh_test_solving.test_prompt.strip()})

    # Inert path placeholders: the row stores CONTENTS, so there is no real
    # referenced-file path to restore. Nothing at runtime re-reads these paths.
    _unused_path = Path()
    config = PipelineConfig(
        name=refs.name,
        description=refs.description or "",
        service=refs.service,
        auth=AuthConfig(
            login_file=_unused_path,
            password_file=_unused_path,
            login=refs.login.strip(),
            password=refs.password.strip(),
        ),
        resume=ResumeConfig(path=_unused_path, contents=refs.resume.strip()),
        openai=OpenAIConfig(
            model=refs.openai_model,
            base_url=refs.openai_base_url or "",
            api_key=refs.openai_api_key.strip(),
            # Free-form passthrough, forwarded verbatim (values are
            # provider-dependent); ``None`` omits the parameter.
            reasoning_effort=refs.openai_reasoning_effort,
        ),
        openai_captcha=captcha,
        scoring=ScoringConfig(min_required_score=refs.min_required_score, scoring_prompt=refs.scoring_prompt.strip()),
        apply=ApplyConfig(apply_prompt=refs.apply_prompt.strip()),
        limits=LimitsConfig(daily_apply_limit=refs.daily_apply_limit),
        hh_test_solving=hh_test_solving,
    )
    config.set_service_section(section)
    return Ok(config)


# --- Config-time cap validation (task 4.3) ----------------------------------
def validate_cap(pipeline: PipelineConfig, factory: Factory) -> Result[None, str]:
    """Validate ``limits.daily_apply_limit`` against the client's per-auth cap.

    The cap is read via ``Factory.cap``, which looks up the client's
    class-level ``service_info.per_auth_daily_cap`` without constructing a
    client — a cap is static client metadata, never something that needs a live
    client (and never a constant in core). A violation is a config error surfaced
    immediately (``jobfucker init`` turns this into a non-zero exit + message).

    Args:
        pipeline: the validated pipeline config.
        factory: the client registry used to resolve the service's cap.

    Returns:
        ``Ok(None)`` when the limit is within the cap, else ``Err`` with a
        clear message.
    """
    try:
        cap = factory.cap(pipeline.service)
    except KeyError as exc:
        return Err(str(exc))
    limit = pipeline.limits.daily_apply_limit
    if limit > cap:
        return Err(f"daily_apply_limit ({limit}) exceeds the {pipeline.service!r} per-auth daily cap ({cap})")
    return Ok(None)


def validate_search_windows(pipeline: PipelineConfig, factory: Factory) -> Result[None, str]:
    """Resolve every search entry's window through the one validation matrix.

    Runs each pool entry's ``window`` (or the :class:`SearchWindow` defaults
    when the entry configures none) through :func:`plan_fetch` against the
    client's class-level ``max_search_items`` — the same matrix a CLI flag run
    passes through, so a bad configured window fails at ``init``/``update``
    with the same message a bad flag would produce (prefixed ``search N: ``).

    Args:
        pipeline: the validated pipeline config.
        factory: the client registry used to resolve the service's cap.

    Returns:
        ``Ok(None)`` when every entry's window resolves, else the first ``Err``.
    """
    from jobfucker.stages.fetch_plan import (
        plan_fetch,
    )  # local import: ``stages/__init__`` pulls the whole domain (ai → config) and would close a cycle at module load

    max_items: int | None
    try:
        max_items = factory.max_search_items(pipeline.service)
    except KeyError as exc:
        return Err(str(exc))
    for index, entry in enumerate(pipeline.service_section.searches):
        window = entry.window if entry.window is not None else SearchWindow()
        resolved = plan_fetch(window, max_search_items=max_items, origin=f"search {index}: ")
        if resolved.is_err:
            return Err(resolved.unwrap_err())
    return Ok(None)


__all__ = [
    "SERVICE_SECTION_MODELS",
    "ApplyConfig",
    "AuthConfig",
    "HhTestSolvingConfig",
    "LimitsConfig",
    "OpenAIConfig",
    "PersistedPipelineRefs",
    "PipelineConfig",
    "ResumeConfig",
    "ScoringConfig",
    "dump_hh_test_solving",
    "dump_openai_captcha",
    "dump_service_section",
    "load_hh_test_solving",
    "load_openai_captcha",
    "load_pipeline_config",
    "load_service_section",
    "reconstruct_pipeline_config",
    "search_index_range_error",
    "validate_cap",
    "validate_search_windows",
    "with_overrides",
]
