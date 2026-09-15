Feature: HH vacancy applications
  Applying must preflight the current vacancy detail, submit exactly one form-encoded
  POST /negotiations, classify every documented outcome, and reconcile an unconfirmed
  submission through active negotiations without ever replaying it.

  Scenario: An applicable vacancy is applied to successfully
    Given a healthy HH session and an applicable vacancy
    When the client applies to the vacancy with a cover letter
    Then the application succeeds
    And HH receives exactly one form-encoded submission carrying the resume, vacancy, and message
    And the current detail is fetched before the submission

  Scenario: A 201 success carrying a body is a protocol failure
    Given a healthy HH session whose submission succeeds with a body
    When the client applies to the vacancy with a cover letter
    Then the submission is reported as a protocol failure

  Scenario: An already-applied envelope is an idempotent skip
    Given a healthy HH session and an already-applied vacancy
    When the client applies to the vacancy with a cover letter
    Then the outcome is a skip with reason already_applied
    And HH receives exactly one form-encoded submission carrying the resume, vacancy, and message

  Scenario: A test-required submission envelope is a skip
    Given a healthy HH session and a vacancy whose submission demands a test
    When the client applies to the vacancy with a cover letter
    Then the outcome is a skip with reason test_required

  Scenario: A submission redirect is an external-application skip
    Given a healthy HH session whose submission answers with a redirect
    When the client applies to the vacancy with a cover letter
    Then the outcome is a skip with reason external_application

  Scenario: An archived vacancy is skipped before any submission
    Given a healthy HH session and an archived vacancy
    When the client applies to the vacancy with a cover letter
    Then the outcome is a skip with reason vacancy_unavailable
    And no submission is sent

  Scenario: A vacancy with an attached test is declined by the board's test_required response
    Given a healthy HH session and a vacancy with an attached test
    When the client applies to the vacancy with a cover letter
    Then the outcome is a skip with reason test_required
    And a submission is sent

  Scenario: A vacancy with an external response form is skipped before any submission
    Given a healthy HH session and a vacancy with an external response form
    When the client applies to the vacancy with a cover letter
    Then the outcome is a skip with reason external_application
    And no submission is sent

  Scenario: A screening test on the apply page is fetched for a vacancy
    Given a healthy HH session and a vacancy whose apply page carries a screening test
    When the client fetches the vacancy's screening test
    Then the test carries the parsed free-text and choice tasks

  Scenario: A test vacancy is applied with answers through the website flow
    Given a healthy HH session and a vacancy whose apply page carries a screening test
    When the client applies to the vacancy with test answers
    Then the application succeeds
    And the website submission carried the answers and resume hash

  Scenario: A choice-open test task is applied with the own-variant encoding
    Given a healthy HH session and a vacancy whose apply page carries a choice-open test
    When the client applies to the vacancy with test answers
    Then the application succeeds
    And the website submission uses the choice-open encoding

  Scenario: A configured resume the page does not offer for the vacancy stops with a configuration error
    Given a healthy HH session whose configured resume is not applicable to the vacancy
    When the client applies to the vacancy with test answers
    Then the apply fails with a configuration error

  Scenario: A test removed from the apply page applies through the standard flow
    Given a healthy HH session and a vacancy whose apply page no longer carries the test
    When the client applies to the vacancy with test answers
    Then the application succeeds
    And the standard API submission was used

  Scenario: A response-impossible apply page is an idempotent skip
    Given a healthy HH session and a vacancy whose apply page reports response impossible
    When the client applies to the vacancy with test answers
    Then the outcome is a skip with reason already_applied
    And no website submission is sent

  Scenario: A duplicate website test apply is an idempotent skip
    Given a healthy HH session whose test submission is already applied
    When the client applies to the vacancy with test answers
    Then the outcome is a skip with reason already_applied

  Scenario: A test-required website envelope fails with the test_required code
    Given a healthy HH session whose test submission still demands a test
    When the client applies to the vacancy with test answers
    Then the test apply fails with code test_required

  Scenario: A letter-required website envelope fails with the letter_required code
    Given a healthy HH session whose test submission requires a cover letter
    When the client applies to the vacancy with test answers
    Then the test apply fails with code letter_required

  Scenario: An incomplete resume website envelope fails with the resume_incomplete code
    Given a healthy HH session whose test submission has an incomplete resume
    When the client applies to the vacancy with test answers
    Then the test apply fails with code resume_incomplete
    And the failure names the resume edit page

  Scenario: A test that changed after solving is refused
    Given a healthy HH session whose test gains a task after solving
    When the client applies to the vacancy with test answers
    Then the test apply fails with code test_changed

  Scenario: A rejected configured resume stops with a configuration error
    Given a healthy HH session whose configured resume the board rejects
    When the client applies to the vacancy with a cover letter
    Then the apply fails with a configuration error

  Scenario: A limit_exceeded envelope signals the stop
    Given a healthy HH session whose submission hits the daily cap
    When the client applies to the vacancy with a cover letter
    Then the apply fails with a limit stop

  Scenario: A bare 401 submission is an authorization failure
    Given a healthy HH session whose submission is rejected as unauthorized
    When the client applies to the vacancy with a cover letter
    Then the apply fails with an authorization error

  Scenario: A 5xx submission outcome is reconciled and stays unconfirmed
    Given a healthy HH session whose submission fails with a server error and no application on the board
    When the client applies to the vacancy with a cover letter
    Then the apply fails with an unconfirmed outcome
    And the active negotiations list is consulted

  Scenario: A submission challenge is solved and the submission replayed once
    Given a healthy HH session whose first submission meets a challenge
    When the client applies to the vacancy with a cover letter
    Then the application succeeds
    And the browser engine submitted one answer through the real page
    And HH receives two submissions after the solved challenge

  Scenario: A lost submission is reconciled through active negotiations
    Given a healthy HH session that loses its submission but holds the application
    When the client applies to the vacancy with a cover letter
    Then the application succeeds
    And the active negotiations list is consulted

  Scenario: A lost submission found on a later negotiations page is reconciled
    Given a healthy HH session that loses its submission and holds the application on page 2
    When the client applies to the vacancy with a cover letter
    Then the application succeeds
    And the active negotiations list is consulted

  Scenario: A lost submission absent from negotiations stays unconfirmed
    Given a healthy HH session that loses its submission with no application on the board
    When the client applies to the vacancy with a cover letter
    Then the apply fails with an unconfirmed outcome
    And the active negotiations list is consulted
