"""Phase 1 (task 1.3): Gherkin BDD smoke via ``pytest-bdd``.

Proves the Gherkin acceptance path compiles and runs green, following the
step-typing convention (test/AGENTS.md §3): every step function has **explicit
parameter and return types**, and ``{placeholder}`` params are annotated to match
what the parser emits (``int`` via the ``:d`` format specifier).

The tiny behaviour exercised here — "a vacancy at or above the minimum required
score is eligible for apply" — mirrors the scoring->apply eligibility rule that
Phases 5.3/5.5 implement; it lives here purely to smoke-test the harness.
"""

from __future__ import annotations

from dataclasses import dataclass

from pytest_bdd import given, parsers, scenarios, then, when


@dataclass(frozen=True, slots=True)
class ScoredVacancy:
    """A vacancy carrying a raw AI score and its minimum apply threshold."""

    score: int
    min_required_score: int = 0


def is_eligible_for_apply(vacancy: ScoredVacancy) -> bool:
    """True when a vacancy scores at or above the configured minimum."""
    return vacancy.score >= vacancy.min_required_score


# Dynamic Gherkin scenario collection; the generated test_* functions are
# invisible to basedpyright (documented hazard, test/AGENTS.md §2). The
# feature file lives beside this collector under test/families/bdd/.
scenarios("bdd/acceptance_smoke.feature")


@given(parsers.parse("a vacancy scored {score:d}"), target_fixture="vacancy")
def vacancy_step(score: int) -> ScoredVacancy:
    """Seed a scored vacancy from the ``{score:d}`` placeholder."""
    return ScoredVacancy(score=score)


@when(
    parsers.parse("the minimum required score is {threshold:d}"),
    target_fixture="vacancy",
)
def threshold_step(vacancy: ScoredVacancy, threshold: int) -> ScoredVacancy:
    """Return an updated vacancy carrying the new minimum score threshold.

    ``target_fixture`` on a ``When`` re-seeds the ``vacancy`` fixture so the
    following ``Then`` steps receive the thresholded vacancy — the v8 fixture
    chain (test/AGENTS.md §2).
    """
    return ScoredVacancy(score=vacancy.score, min_required_score=threshold)


@then("the vacancy is eligible for apply")
def eligible_step(vacancy: ScoredVacancy) -> None:
    """Assert the vacancy is at or above its minimum required score."""
    assert is_eligible_for_apply(vacancy)


@then("the vacancy is not eligible for apply")
def not_eligible_step(vacancy: ScoredVacancy) -> None:
    """Assert the vacancy is below its minimum required score."""
    assert not is_eligible_for_apply(vacancy)
