Feature: Habr SmartCaptcha is solved browser-first with a browserless vision fallback
  In order to clear the Habr Account login challenge without silent guessing
  As the Habr client
  I want a real checkbox click, a bounded vision ladder, and a valid proof-of-work

  Scenario: The browser click returns the smart-token
    Given a browser SmartCaptcha page whose checkbox yields token "spravka-token"
    When the browser solver solves the login captcha
    Then the solution is "spravka-token"
    And the solver clicked the checkbox inside the SmartCaptcha frame
    And the solver awaited the SmartCaptcha spinner and the smart-token field

  Scenario: An escalated browser challenge fails without a token
    Given a browser SmartCaptcha page that escalates to the advanced challenge
    When the browser solver solves the login captcha
    Then the solution fails with an escalation CAPTCHA error
    And the browser was opened exactly once

  Scenario: A dropped first click is retried inside the same page
    Given a browser SmartCaptcha page whose token appears only on the second click
    When the browser solver solves the login captcha
    Then the solution is "browser-captcha-token"
    And the solver clicked the checkbox twice
    And the browser was opened exactly once

  Scenario: The vision ladder solves a checkbox then an image challenge
    Given a vision captcha ladder that offers a checkbox then an image solved with "abcde"
    When the vision solver solves the login captcha
    Then the solution is the spravka token
    And the solver submitted one captcha image

  Scenario: A checkbox accepted without an image returns the spravka
    Given a vision captcha ladder whose checkbox escalates into a pass
    When the vision solver solves the login captcha
    Then the solution is the spravka token
    And no captcha image was submitted

  Scenario: A wrong image answer receives a fresh image
    Given a vision captcha ladder that rejects "wrong" then accepts "right"
    When the vision solver solves the login captcha
    Then the solution is the spravka token
    And the solver submitted two captcha images

  Scenario: Exhausted vision attempts fail with a recovery URL
    Given a vision captcha ladder that rejects every answer with 2 attempts allowed
    When the vision solver solves the login captcha
    Then the solution fails with a CAPTCHA error carrying the login URL

  Scenario: An unknown challenge type fails before any OCR
    Given a vision captcha ladder that offers an unknown challenge
    When the vision solver solves the login captcha
    Then the solution fails with a CAPTCHA error
    And no captcha image was submitted

  Scenario: An implausibly hard proof-of-work fails as unsolvable
    Given a vision captcha ladder whose checkbox demands an implausible proof-of-work
    When the vision solver solves the login captcha
    Then the solution fails with a CAPTCHA error carrying the login URL
    And no captcha image was submitted
    And the vision solver made exactly one check request

  Scenario: The coordinator preflights the engine before the browser click
    Given a coordinator with a working browser whose checkbox yields token "browser-captcha-token"
    When the login captcha coordinator solves the login captcha
    Then the solution is "browser-captcha-token"
    And the browser engine was preflighted exactly once
    And the browser engine was preflighted before the browser attempt
    And the browser was opened exactly once

  Scenario: A missing browser engine skips the browser and falls back to vision
    Given a coordinator whose browser engine is unavailable
    When the login captcha coordinator solves the login captcha
    Then the solution is the spravka token
    And the browser engine was preflighted exactly once
    And the browser was never opened

  Scenario: The coordinator prefers the browser and falls back to vision
    Given a failing browser solver and a vision ladder solved with "abcde"
    When the login captcha coordinator solves the login captcha
    Then the solution is the spravka token
    And the vision ladder ran after the browser attempt

  Scenario: A proof-of-work solution hashes under the complexity target
    Given a proof-of-work prefix of "743d313b703d70726f62653b" with complexity 10
    When the proof-of-work is solved
    Then the nonce is 16 bytes and the hash has at least 10 leading zero bits
    And the encoded pdata carries the nonce, the prefix, and a positive calc time

  Scenario: The image loader decodes to the real image URL
    Given the widget image loader for "https://img.smartcaptcha.yandexcloud.net/image?key=k1"
    When the loader is decoded
    Then the decoded image URL is "https://img.smartcaptcha.yandexcloud.net/image?key=k1"
