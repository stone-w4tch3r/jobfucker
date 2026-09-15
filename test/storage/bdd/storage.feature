Feature: Storage identity, snapshot, soft-delete and re-run semantics
  In order to keep history consistent across pipeline runs without re-identifying
  As the jobfucker engine
  I want stable identities, append-only snapshots, and auth-keyed daily limits honoured

  Scenario: Vacancies that already reached an apply decision are reported for skipping
    Given a pipeline exists
    And it has an applied vacancy "done" and a pending vacancy "pending"
    When the decided external ids are queried
    Then the processed set contains "done" but not "pending"

  Scenario: The daily apply counter increments atomically per auth
    Given a pipeline exists
    When the counter for service "mock", login "a@ex.com" on "2026-08-05" is incremented three times
    Then its count is 3 and only one row exists for that service, login and date

  Scenario: Appending a snapshot keeps the pipeline id stable
    Given a pipeline exists
    When a second snapshot is appended and made current
    Then the pipeline id is unchanged and there are two snapshots

  Scenario: A no-op update appends zero snapshot rows
    Given a pipeline is created with a config
    When it is updated with the identical config
    Then the snapshot history still has exactly one row
