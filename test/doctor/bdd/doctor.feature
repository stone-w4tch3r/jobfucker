Feature: Doctor checks
  The doctor verifies identity, captcha handling, and AI scoring before a real run.

  Scenario: The identity check returns the board identity
    Given a fake client with a working identity
    When the doctor checks the identity
    Then the identity check succeeds with the fake identity

  Scenario: The identity check surfaces a board failure
    Given a fake client whose identity fetch fails
    When the doctor checks the identity
    Then the identity check fails

  Scenario: The captcha check solves through the handler
    Given a captcha handler that solves
    When the doctor checks the captcha
    Then the captcha check succeeds with a solved answer

  Scenario: The captcha check fails when the handler cannot solve
    Given a captcha handler that cannot solve
    When the doctor checks the captcha
    Then the captcha check fails

  Scenario: The scoring check scores the canned resource vacancy
    Given a scripted AI that scores 4
    When the doctor checks the scoring
    Then the scoring check succeeds with score 4

  Scenario: The scoring check surfaces an AI failure
    Given a scripted AI whose score fails
    When the doctor checks the scoring
    Then the scoring check fails

  Scenario: The canned doctor vacancy resource loads
    When the doctor loads its vacancy resource
    Then the resource vacancy is a probe vacancy

  Scenario: An unreadable doctor vacancy resource fails to load
    Given a missing vacancy resource file
    When the doctor loads that vacancy resource
    Then the vacancy load fails with a read error

  Scenario: A wrong-shaped vacancy resource fails validation
    Given a vacancy resource with a wrong shape
    When the doctor loads that vacancy resource
    Then the vacancy load fails validation

  Scenario: The packaged captcha image resource loads
    When the doctor loads its captcha image resource
    Then the resource is a PNG image
