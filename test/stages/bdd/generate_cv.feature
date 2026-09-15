Feature: Cover letter generation for eligible vacancies
  In order to prepare apply messages for the best matching vacancies
  As the jobfucker engine
  I want to generate cover letters for eligible scored vacancies and skip the rest

  Scenario: a cover letter is generated for eligible vacancies and skipped ones are untouched
    Given a pipeline with min_required_score 3
    And an eligible scored vacancy "good" with score 5 and a sub-threshold "weak" with score 2
    And an AI that generates the letter "Hello, ACME!"
    When the cover-letter stage runs over the pipeline
    Then "good" has the generated cover letter
    And "weak" is skipped and has no cover letter
    And the generate report shows 1 generated and 1 skipped

  Scenario: a manual-skip vacancy is excluded from cover-letter generation
    Given a pipeline with min_required_score 3
    And a manually-skipped vacancy "blocked" with score 5 and an eligible vacancy "ok" with score 4
    And an AI that generates the letter "letter for ok"
    When the cover-letter stage runs over the pipeline
    Then "blocked" is skipped and has no cover letter
    And "ok" has the generated cover letter
    And the generate report shows 1 generated and 1 skipped

  Scenario: a failed generation is stored as a per-vacancy error without aborting
    Given a pipeline with min_required_score 3
    And an eligible scored vacancy "good" with score 5 and another eligible "also" with score 5
    And an AI that fails the first letter then generates "done"
    When the cover-letter stage runs over the pipeline
    Then "good" has a cover_letter_error
    And "also" has the generated cover letter "done"
    And the generate report shows 1 generated and 1 failed
