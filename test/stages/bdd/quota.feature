Feature: Per-window apply quotas
  In order to respect each board's declared application quota window
  As the engine
  I want a shared per-auth counter and a per-pipeline counter keyed by that window

  Scenario: The account cap stops applying once the window's quota is reached
    Given a monthly client with a 5-per-month account cap
    And a pipeline with apply limit 10
    And 4 applications already recorded for the account this month
    And 2 eligible vacancies
    When the apply stage runs
    Then 1 vacancy is applied
    And the stop names the monthly account cap

  Scenario: A full account counter stops before any attempt
    Given a monthly client with a 5-per-month account cap
    And a pipeline with apply limit 10
    And 5 applications already recorded for the account this month
    And 2 eligible vacancies
    When the apply stage runs
    Then 0 vacancies are applied
    And the stop names the monthly account cap

  Scenario: A new month starts with a fresh counter
    Given a monthly client with a 5-per-month account cap
    And a pipeline with apply limit 10
    And 5 applications already recorded for the account last month
    And 2 eligible vacancies
    When the apply stage runs
    Then 2 vacancies are applied
    And the account counter for this month is 2

  Scenario: Two pipelines share the account counter but keep their own
    Given a monthly client with a 50-per-month account cap
    And two pipelines with apply limit 1 sharing one login
    When each pipeline runs once over 2 eligible vacancies
    Then each pipeline applied 1 vacancy
    And the account counter is 2
    And each pipeline counter is 1
