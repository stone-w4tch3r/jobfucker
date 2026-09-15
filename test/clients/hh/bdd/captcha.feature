Feature: HH standalone CAPTCHA recovery
  A standalone challenge is cleared by the stealth browser engine: the page image
  is solved by the injected solver and the answer is submitted through the real page.

  Scenario: A correct first answer clears the challenge and replays the healthcheck
    Given a persisted HH token blocked by a standalone text CAPTCHA
    When the HH client authorizes through CAPTCHA recovery
    Then CAPTCHA recovery succeeds
    And the browser engine submitted one answer through the real page
    And the healthcheck was replayed once

  Scenario: A wrong answer reloads the challenge before a second attempt
    Given a standalone text CAPTCHA rejecting its first answer
    When the HH client authorizes through CAPTCHA recovery
    Then CAPTCHA recovery succeeds
    And the browser engine submitted two answers through the real page
    And the healthcheck was replayed once

  Scenario: Wrong answers stop after three fresh challenge attempts
    Given a standalone text CAPTCHA rejecting every answer
    When the HH client authorizes through CAPTCHA recovery
    Then CAPTCHA recovery fails with its safe recovery URL
    And the browser engine submitted exactly three answers
    And the blocked healthcheck was not replayed

  Scenario: A configured lower answer bound is enforced
    Given a standalone text CAPTCHA configured for one rejected answer
    When the HH client authorizes through CAPTCHA recovery
    Then CAPTCHA recovery fails with its safe recovery URL
    And the browser engine submitted exactly one answer
    And the blocked healthcheck was not replayed

  Scenario: Solver failure stops without submitting an answer
    Given a standalone text CAPTCHA whose solver fails
    When the HH client authorizes through CAPTCHA recovery
    Then CAPTCHA recovery fails with its safe recovery URL
    And the browser engine submitted no answer

  Scenario: A hostile CAPTCHA URL is rejected before the browser opens
    Given a CAPTCHA response containing a non-HH recovery URL
    When the HH client authorizes through CAPTCHA recovery
    Then CAPTCHA recovery fails with a protocol error
    And the browser engine never opened

  Scenario: A foreign-origin success marker does not prove CAPTCHA success
    Given a standalone text CAPTCHA redirecting to a foreign origin after submit
    When the HH client authorizes through CAPTCHA recovery
    Then CAPTCHA recovery fails with its safe recovery URL
    And the blocked healthcheck was not replayed

  Scenario: CAPTCHA re-entry after the one replay is bounded
    Given a standalone text CAPTCHA that returns after recovery
    When the HH client authorizes through CAPTCHA recovery
    Then CAPTCHA recovery fails with its safe recovery URL
    And the healthcheck was replayed once
    And the browser engine opened exactly one session

  Scenario: An active reCAPTCHA fails without invoking the browser engine
    Given a persisted HH token blocked by reCAPTCHA
    When the HH client authorizes through CAPTCHA recovery
    Then CAPTCHA recovery fails without a recovery URL
    And the browser engine never opened

  Scenario: An unknown CAPTCHA provider fails closed
    Given a persisted HH token blocked by an unknown CAPTCHA
    When the HH client authorizes through CAPTCHA recovery
    Then CAPTCHA recovery fails without a recovery URL
    And the browser engine never opened
