Feature: HH authentication preflight
  Every HH client action must prove that its Bearer authorization is healthy before proceeding.

  Scenario: A valid persisted token is healthchecked and reused
    Given a valid persisted HH token
    When the HH client authorizes
    Then authorization succeeds
    And HH received only an applicant healthcheck

  Scenario: Missing state performs browserless login and OAuth
    Given no persisted HH authentication state
    When the HH client authorizes
    Then authorization succeeds
    And HH received the complete credential OAuth flow
    And verified authentication state was persisted securely

  Scenario: The login cookie bootstrap redirect is replayed once
    Given no persisted state and a login cookie bootstrap redirect
    When the HH client authorizes
    Then authorization succeeds
    And HH received one login bootstrap replay before the credential OAuth flow

  Scenario: A transient credential-login connection failure is retried
    Given no persisted state and one credential-login connection failure
    When the HH client authorizes
    Then authorization succeeds
    And HH received two credential-login connection attempts

  Scenario: Persistent credential-login connection failures are bounded and identified
    Given no persisted state and persistent credential-login connection failures
    When the HH client authorizes
    Then authorization fails with a credential-login transport error
    And HH received three credential-login connection attempts

  Scenario: An expired token refreshes once
    Given an expired persisted HH token
    When the HH client authorizes
    Then authorization succeeds
    And HH received one refresh followed by an applicant healthcheck
    And the rotated token state was persisted

  Scenario: A locally valid rejected token performs full login without refresh
    Given a rejected persisted HH token
    When the HH client authorizes
    Then authorization succeeds
    And HH received no refresh before the replacement credential OAuth flow

  Scenario: OAuth state mismatch fails without persisting the candidate token
    Given no persisted state and an OAuth state mismatch
    When the HH client authorizes
    Then authorization fails with a protocol error
    And no authentication state was persisted

  Scenario: An OAuth response that omits state remains compatible
    Given no persisted state and an OAuth response without state
    When the HH client authorizes
    Then authorization succeeds
    And HH received the complete credential OAuth flow

  Scenario: OAuth redirect to an unexpected origin is rejected
    Given no persisted state and an invalid OAuth redirect
    When the HH client authorizes
    Then authorization fails with a protocol error
    And no authentication state was persisted

  Scenario: A malformed OAuth token response is rejected
    Given no persisted state and a malformed OAuth token response
    When the HH client authorizes
    Then authorization fails with a protocol error
    And no authentication state was persisted

  Scenario: An OAuth token challenge is classified before token decoding
    Given no persisted state and reCAPTCHA at the OAuth token boundary
    When the HH client authorizes
    Then authorization fails with a CAPTCHA error
    And no authentication state was persisted

  Scenario: Embedded login CAPTCHA is solved on the credential form
    Given no persisted state and an embedded login CAPTCHA
    When the HH client authorizes
    Then authorization succeeds
    And HH received the researched embedded CAPTCHA login flow
    And HH received no standalone CAPTCHA submission

  Scenario: A wrong embedded login CAPTCHA answer rotates the image key
    Given an embedded login CAPTCHA rejecting its first answer
    When the HH client authorizes
    Then authorization succeeds
    And HH received two fresh embedded CAPTCHA image keys

  Scenario: Embedded login CAPTCHA attempts are bounded
    Given an embedded login CAPTCHA rejecting every answer
    When the HH client authorizes
    Then authorization fails with a CAPTCHA error
    And HH received exactly four embedded CAPTCHA answers

  Scenario: Credential failure after an accepted login CAPTCHA is not retried
    Given an embedded login CAPTCHA followed by a credential mismatch
    When the HH client authorizes
    Then authorization fails with an authentication error
    And HH received exactly one embedded CAPTCHA answer

  Scenario: Embedded login CAPTCHA without stable state fails closed
    Given an embedded login CAPTCHA without a state
    When the HH client authorizes
    Then authorization fails with a protocol error
    And HH requested no embedded CAPTCHA image

  Scenario: Embedded login CAPTCHA state cannot change during retries
    Given an embedded login CAPTCHA changing state after an answer
    When the HH client authorizes
    Then authorization fails with a protocol error
    And HH received exactly one embedded CAPTCHA answer

  Scenario: Dormant reCAPTCHA metadata does not block browserless login
    Given no persisted state and dormant reCAPTCHA metadata
    When the HH client authorizes
    Then authorization succeeds
    And HH received the complete credential OAuth flow

  Scenario: A non-applicant healthcheck is rejected
    Given a valid token for a non-applicant account
    When the HH client authorizes
    Then authorization fails with an authentication error
    And HH received only an applicant healthcheck

  Scenario: A rejected post-login healthcheck does not loop
    Given no persisted state and a rejected post-login healthcheck
    When the HH client authorizes
    Then authorization fails with an authentication error
    And HH received only one credential OAuth flow

  Scenario: Persisted state survives a new client instance
    Given no persisted HH authentication state
    When the HH client authorizes twice with a restart
    Then both authorizations succeed
    And the second client used the persisted token healthcheck

  Scenario: Every non-auth action performs authentication preflight
    Given a valid persisted HH token
    When every non-auth HH action is invoked
    Then each action performed an applicant healthcheck
    And every non-auth action succeeds

  Scenario: Saving authentication state on Windows skips the POSIX-only chmod
    Given Windows simulated for the HH token store
    When a token state snapshot is saved
    Then the save succeeds and the snapshot roundtrips without fchmod

  Scenario: A failed browser engine preflight fails the action fast
    Given a valid persisted HH token
    And a browser engine whose startup preflight fails
    When the HH client authorizes
    Then authorization fails with a browser engine error
    And HH received no requests at all
