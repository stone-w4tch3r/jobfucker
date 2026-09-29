Feature: Habr vacancy applications
  Applying must preflight the current vacancy detail, submit exactly one multipart
  response (with the cover letter as the body field, or no fields when letterless),
  classify every documented outcome, pace submissions by the board's interval, and
  reconcile an unconfirmed submission by refetching the detail — never replaying it.

  Scenario: An applicable vacancy is applied to successfully
    Given a Habr client whose application succeeds
    When the client applies to the vacancy with a cover letter
    Then the application succeeds
    And the current detail is fetched before exactly one multipart submission carrying the letter

  Scenario: A letterless application sends no body field
    Given a Habr client whose application succeeds
    When the client applies to the vacancy without a cover letter
    Then the application succeeds
    And the submission carried no body field

  Scenario: A success without a response id is a protocol failure
    Given a Habr client whose application succeeds with an empty response
    When the client applies to the vacancy with a cover letter
    Then the submission is reported as a protocol failure

  Scenario: The CSRF token is prefetched before the pacing wait
    Given a Habr client whose application succeeds
    When the client applies to the vacancy with a cover letter
    Then the application succeeds
    And a CSRF request was recorded before the pacing delay ran
    And the CSRF request precedes the response POST in the recorded order

  Scenario: A CSRF failure aborts before pacing and submission
    Given a Habr client whose CSRF token cannot be scraped
    When the client applies to the vacancy with a cover letter
    Then the apply fails with a protocol error
    And no submission is sent
    And the pacing delay was never invoked

  Scenario: An already-applied vacancy is skipped before any submission
    Given a Habr client and an already-applied vacancy
    When the client applies to the vacancy with a cover letter
    Then the outcome is a skip with reason already_applied
    And no submission is sent

  Scenario: An archived vacancy is skipped before any submission
    Given a Habr client and an archived vacancy
    When the client applies to the vacancy with a cover letter
    Then the outcome is a skip with reason vacancy_unavailable
    And no submission is sent

  Scenario: A hidden vacancy is skipped before any submission
    Given a Habr client and a hidden vacancy
    When the client applies to the vacancy with a cover letter
    Then the outcome is a skip with reason vacancy_unavailable
    And no submission is sent

  Scenario: A duplicate submission is an idempotent skip
    Given a Habr client whose duplicate submission is rejected
    When the client applies to the vacancy with a cover letter
    Then the outcome is a skip with reason already_applied

  Scenario: A bare unauthorized submission is an authorization failure
    Given a Habr client whose submission is rejected as unauthorized
    When the client applies to the vacancy with a cover letter
    Then the apply fails with an authorization error

  Scenario: A throttled submission waits and retries exactly once before failing
    Given a Habr client whose submission is throttled
    When the client applies to the vacancy with a cover letter
    Then the outcome is a per-vacancy failure
    And the submission was sent twice
    And the pacing delay was invoked twice

  Scenario: A submission that hits the monthly cap stops with a limit error
    Given a Habr client whose submission hits the monthly cap
    When the client applies to the vacancy with a cover letter
    Then the apply fails with a limit stop

  Scenario: A missing vacancy submission is a not-found error
    Given a Habr client whose submission is not found
    When the client applies to the vacancy with a cover letter
    Then the apply fails with a not-found error

  Scenario: A persistent CSRF rejection is a configuration error
    Given a Habr client whose submission needs a CSRF refresh
    When the client applies to the vacancy with a cover letter
    Then the apply fails with a configuration error
    And the submission was sent twice after the CSRF refresh

  Scenario: Another 4xx submission is a per-vacancy failure
    Given a Habr client whose submission is rejected as forbidden
    When the client applies to the vacancy with a cover letter
    Then the outcome is a per-vacancy failure with the board status code

  Scenario: A server-error submission is a transport failure
    Given a Habr client whose submission fails with a server error
    When the client applies to the vacancy with a cover letter
    Then the apply fails with a transport error

  Scenario: A pre-send connection loss is a transport failure
    Given a Habr client whose submission cannot connect
    When the client applies to the vacancy with a cover letter
    Then the apply fails with a transport error

  Scenario: A lost submission found on the board is reconciled as success
    Given a Habr client that loses its submission but holds the application
    When the client applies to the vacancy with a cover letter
    Then the application succeeds
    And the detail was refetched to reconcile

  Scenario: A lost submission absent from the board stays unconfirmed
    Given a Habr client that loses its submission with no application on the board
    When the client applies to the vacancy with a cover letter
    Then the apply fails with an unconfirmed outcome
    And the detail was refetched to reconcile
