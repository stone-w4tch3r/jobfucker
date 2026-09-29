Feature: Habr session lifecycle reuses cookies, retries remember_user_token, and logs in via SSO
  In order to authenticate once and reuse the session across runs
  As the Habr client
  I want persisted-cookie reuse, a bounded remember_user_token retry, and a browserless SSO login

  Scenario: A valid persisted session is reused with one identity check
    Given a valid persisted Habr session
    When the Habr client authorizes
    Then authorization succeeds
    And the identity carries the account alias, name, and email
    And the persisted session is reused with exactly one identity check

  Scenario: An anonymous session runs the full SSO login chain
    Given no persisted Habr session
    When the Habr client authorizes
    Then authorization succeeds
    And the full login chain ran
    And the login submission carried the browser captcha token
    And the session was persisted with career cookies and the identity

  Scenario: A lone remember_user_token re-issues the session on retry
    Given a persisted session with only a remember_user_token
    When the Habr client authorizes
    Then authorization succeeds
    And the remember_user_token was retried exactly once without a login chain
    And the session was persisted with career cookies and the identity

  Scenario: A re-issued session cookie is persisted after a reused session
    Given a valid persisted Habr session whose identity call re-issues the session cookie
    When the Habr client authorizes
    Then authorization succeeds
    And the persisted session carries the cookie value "reissued"

  Scenario: A longer OAuth chain is walked hop by hop
    Given no persisted Habr session and a longer OAuth login chain
    When the Habr client authorizes
    Then authorization succeeds
    And the full login chain ran through the extra hop

  Scenario Outline: A stale persisted session recovers through a full login
    Given a stale persisted Habr session whose identity call answers <status>
    When the Habr client authorizes
    Then authorization succeeds
    And the full login chain ran

    Examples:
      | status |
      | 302    |
      | 401    |
      | 403    |

  Scenario: A remember_user_token that stays anonymous still runs the full login
    Given a persisted session whose remember_user_token retry stays anonymous
    When the Habr client authorizes
    Then authorization succeeds
    And the full login chain ran after the remember retry

  Scenario: A configured resume_id mismatch stops the pipeline
    Given a valid persisted Habr session and a configured resume_id different from the account alias
    When the Habr client authorizes
    Then authorization fails with a configuration error

  Scenario: A malformed identity response is a protocol error
    Given no persisted Habr session and a malformed identity response
    When the Habr client authorizes
    Then authorization fails with a protocol error

  Scenario: Closing the client twice is idempotent
    Given no persisted Habr session
    When the Habr client authorizes and closes twice
    Then authorization succeeds
    And the transport is closed exactly once
