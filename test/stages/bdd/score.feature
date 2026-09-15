Feature: AI scoring of fetched vacancies
  In order to decide which vacancies are worth applying to
  As the jobfucker engine
  I want to score vacancies 1-5 with AI and keep sub-threshold ones pending

  Scenario: sub-threshold vacancies keep their score and stay pending
    Given a pipeline with min_required_score 3
    And two vacancies seeded at position 0 and 1
    And an AI that scores them 5 then 2
    When the score stage runs over the pipeline
    Then the position 0 vacancy has score 5 and is not skipped
    And the position 1 vacancy has score 2 and no apply status
    And the score report shows 2 scored and 1 sub-threshold

  Scenario: an out-of-range AI score becomes a per-vacancy error without aborting the batch
    Given a pipeline with min_required_score 3
    And two vacancies seeded at position 0 and 1
    And an AI that returns an out-of-range score then a valid score 4
    When the score stage runs over the pipeline
    Then the position 0 vacancy has a score_error and no score
    And the position 1 vacancy is scored 4
    And the score report shows 1 scored and 1 failed
