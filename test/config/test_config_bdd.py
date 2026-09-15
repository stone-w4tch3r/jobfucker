"""Phase 4 (tasks 4.1/4.3): Gherkin acceptance scenarios for pipeline config.

Behavioral scenarios (a valid mock pipeline loads; an over-cap limit is
rejected) are expressed as a ``.feature`` with typed ``pytest-bdd`` steps, per
the test/AGENTS.md harness convention. The pure failure-mode unit tests (each
of the error paths) live in ``test_config.py``.
"""

from __future__ import annotations

from pathlib import Path

from pytest_bdd import given, parsers, scenarios, then, when
from rusty_results.prelude import Ok, Result

from jobfucker.clients.base import ClientCredentials, ClientDeps
from jobfucker.clients.factory import Factory
from jobfucker.clients.mock.client import MockClient
from jobfucker.clients.mock.params import MockSearchParams, MockServiceConfig
from jobfucker.config import PipelineConfig, load_pipeline_config, validate_cap
from test.config.helpers import build_inline_pipeline_yaml, write_pipeline_files

scenarios("bdd/config.feature")


def _build_mock_factory(data_dir: Path) -> Factory:
    """A factory with the ``mock`` client registered.

    Deliberately section-less: cap validation (the only consumer here) reads the
    cap via ``Factory.cap``, which needs no config section.
    """
    deps = ClientDeps(
        service="mock",
        profile_id=None,
        data_dir=data_dir,
        credentials=ClientCredentials(login="test-login", password="test-password"),
        auth_interaction=_StubAuthInteraction(),
        captcha_handler=_stub_captcha,
    )
    factory = Factory(deps)
    factory.register("mock", MockClient)
    return factory


@given("a valid mock pipeline file is written into the runtime config dir", target_fixture="config_path")
def write_valid_pipeline(runtime_dir: Path) -> Path:
    """Write a valid mock pipeline.yaml plus all referenced files."""
    return write_pipeline_files(runtime_dir, daily_apply_limit=5)


@given("an inline pipeline file is written into the runtime config dir", target_fixture="config_path")
def write_inline_pipeline(runtime_dir: Path) -> Path:
    """Write a pipeline.yaml that specifies every content slot inline (no files)."""
    return build_inline_pipeline_yaml(runtime_dir / "config")


@given(
    parsers.parse("a mock pipeline file with daily_apply_limit {limit:d} is written"),
    target_fixture="config_path",
)
def write_over_cap_pipeline(runtime_dir: Path, limit: int) -> Path:
    """Write a mock pipeline whose limit exceeds the mock cap (200)."""
    return write_pipeline_files(runtime_dir, daily_apply_limit=limit)


@when("the pipeline config is loaded", target_fixture="load_result")
def load_config(config_path: Path) -> Result[PipelineConfig, str]:
    """Load the pipeline config and return the Result for later assertions."""
    return load_pipeline_config(config_path)


@when("the config-time cap validation runs", target_fixture="cap_result")
def run_cap_validation(config_path: Path, runtime_dir: Path) -> Result[None, str]:
    """Load the config and run cap validation against a mock factory."""
    loaded = load_pipeline_config(config_path)
    assert loaded.is_ok, loaded
    factory = _build_mock_factory(runtime_dir / "data")
    return validate_cap(loaded.unwrap(), factory)


@then(parsers.parse('the config is Ok with the name "{name}"'))
def assert_config_ok(load_result: Result[PipelineConfig, str], name: str) -> None:
    """Assert the load succeeded and the pipeline name matches."""
    assert load_result.is_ok, f"expected Ok, got {load_result}"
    assert load_result.unwrap().name == name


@then('the mock section carries resume_id "mock-resume-1" and mock filter params')
def assert_mock_section(load_result: Result[PipelineConfig, str]) -> None:
    """Assert the ``service.<board>`` section is a MockServiceConfig with resume_id + filter."""
    section = load_result.unwrap().service_section
    assert isinstance(section, MockServiceConfig)
    assert section.resume_id == "mock-resume-1"
    assert isinstance(section.searches[0].filter, MockSearchParams)
    assert section.searches[0].filter.only_with_salary is True


@then("the credential and resume contents are injected")
def assert_contents_injected(load_result: Result[PipelineConfig, str]) -> None:
    """Assert referenced-file contents were loaded into the config."""
    config = load_result.unwrap()
    # Every referenced-file content slot is stripped at load: a file ends with
    # a trailing newline (editor/echo artifact), so the loaded values carry no
    # newline artifacts (a raw newline in a key would break the OpenAI SDK).
    assert config.auth.login == "mock-login"
    assert config.auth.password == "mock-password"
    assert config.resume.contents == "# Mock CV"
    assert config.openai.api_key == "mock-api-key"
    assert config.scoring.scoring_prompt == "score template"
    assert config.apply.apply_prompt == "apply template"


@then("the inline contents are carried exactly as written")
def assert_inline_contents(load_result: Result[PipelineConfig, str]) -> None:
    """Assert inline (file-less) contents load and are carried unchanged."""
    config = load_result.unwrap()
    assert config.auth.login == "inline-login"
    assert config.auth.password == "inline-password"
    assert config.resume.contents == "# Inline CV\n"
    assert config.openai.api_key == "inline-api-key"
    assert config.scoring.scoring_prompt == "inline score template"
    assert config.apply.apply_prompt == "inline apply template"


@then("validation is an Err mentioning the per-auth cap")
def assert_cap_err(cap_result: Result[None, str]) -> None:
    """Assert an over-cap limit surfaces an Err naming the per-auth cap."""
    assert cap_result.is_err, cap_result.unwrap()
    assert "per-auth daily cap" in cap_result.unwrap_err()


# --- minimal interaction/captcha stubs for building a mock Factory ----------
class _StubAuthInteraction:
    """Minimal :class:`AuthInteractionProvider` returning fixed values."""

    async def request_code(self, *, prompt: str) -> Result[str, str]:
        del prompt
        return Ok("123456")

    async def confirm(self, *, prompt: str) -> Result[bool, str]:
        del prompt
        confirmed: bool = True
        return Ok(confirmed)


async def _stub_captcha(image: bytes) -> Result[str, str]:
    del image
    return Ok("stub")
