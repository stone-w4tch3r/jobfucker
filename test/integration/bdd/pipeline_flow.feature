Feature: full pipeline flow (fetch → score → generate_cv → apply) against the mock
  In order to prove the whole engine core works end to end
  As the jobfucker engine
  I want a full pipeline run to fetch, score, generate and apply without a live board

  @integration
  Scenario: a happy path run applies to every fetched vacancy
    Given a fresh engine against the mock
    When the pipeline runs end to end
    Then every fetched vacancy is applied and the daily counters reflect it

  @integration
  Scenario: a re-run does not double-apply
    Given an engine whose pipeline has already been run once
    When the pipeline runs a second time
    Then no vacancy is applied again

  @integration
  Scenario: reaching the per-auth daily limit stops applying
    Given an engine with a one-per-day apply limit and several vacancies
    When the pipeline runs end to end
    Then only one apply happens and the report flags limit_reached

  @integration
  Scenario: a no-op update appends no snapshot and keeps the id
    Given a stored pipeline with its config snapshot
    When its identical config is re-applied
    Then no snapshot is appended and the id is unchanged

  @integration
  Scenario: two pipelines sharing one login share one daily counter
    Given two pipelines sharing one account login
    When both pipelines run end to end
    Then the shared daily counter records the combined total and both pipelines are applied

  @integration
  Scenario: re-fetching under a newer snapshot keeps one row with both provenance stamps
    Given a pipeline scored under its first snapshot
    When a newer snapshot is appended and the pipeline is re-fetched under it
    Then one row survives with fetched and scored snapshot ids pointing at their snapshots
