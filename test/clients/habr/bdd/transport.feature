Feature: Habr transport owns CSRF, error decoding, and its request policy
  In order to keep every raw Habr HTTP concern in one typed boundary
  As the Habr client
  I want CSRF, error envelopes, cookies, and pacing owned by the transport

  Scenario: A mutation scrapes the CSRF token and sends it with the XHR headers
    Given a Habr transport whose CSRF page issues "csrf-token-1"
    When a mutation is submitted
    Then the transport sent a CSRF-scraping GET first
    And the mutation carried the CSRF token "csrf-token-1", the XHR header, and a JSON accept

  Scenario: A CSRF page with the content attribute before the name still yields the token
    Given a Habr transport whose CSRF page issues "csrf-token-content-first" with the content attribute first
    When a mutation is submitted
    Then the mutation carried the CSRF token "csrf-token-content-first", the XHR header, and a JSON accept

  Scenario: A 422 refreshes the CSRF token once and replays
    Given a Habr transport whose CSRF pages issue "csrf-token-1" then "csrf-token-2" and whose mutation answers 422 then 200
    When a mutation is submitted
    Then the mutation result status is 200
    And the transport sent two CSRF-scraping GETs
    And the transport sent two mutations, the second carrying the CSRF token "csrf-token-2"

  Scenario: A persistent 422 replays only once
    Given a Habr transport whose CSRF pages issue "csrf-token-1" then "csrf-token-2" and whose mutation always answers 422
    When a mutation is submitted
    Then the mutation result status is 422
    And the transport sent exactly two mutations and two CSRF-scraping GETs

  Scenario: The plain error envelope decodes a string error
    Given the raw Habr error body {"error":"Not found"}
    When the body is decoded as an error envelope
    Then the envelope error message is "Not found"

  Scenario: The plain error envelope decodes an object error
    Given the raw Habr error body {"error":{"message":"Vacancy not found"}}
    When the body is decoded as an error envelope
    Then the envelope error message is "Vacancy not found"

  Scenario: The structured error envelope decodes
    Given the raw Habr error body {"httpCode":422,"errorCode":"BAD_REQUEST","message":"Nope"}
    When the body is decoded as a structured error
    Then the structured error message is "Nope"

  Scenario: A promised JSON body that is not JSON is a protocol error
    Given the raw Habr error body <html>nope</html>
    When the body is decoded as a login response
    Then decoding fails with a protocol error

  Scenario: Analytics cookies are dropped from the session snapshot
    Given a Habr transport whose cookie jar holds a career session and an analytics cookie
    When the session cookies are snapshotted
    Then only the career session cookie is persisted

  Scenario: The transport policy matches the Habr contract
    Given the Habr transport policy
    Then the read pacing is at least 0.3 seconds
    And redirects are not followed
    And the user agent is a desktop Chrome, never HeadlessChrome
    And only Habr-family cookie domains are allowed
    And analytics cookie names are recognised
    And the retryable statuses are transient 5xx only

  Scenario: A GET whose URL embeds a query keeps that query
    Given a Habr transport that records an absolute GET
    When the transport GETs the OAuth authorize URL with its embedded query
    Then the recorded request kept the embedded response_type and client_id

  Scenario: A GET with explicit params still sends them
    Given a Habr transport that records an absolute GET
    When the transport GETs the authorize URL with explicit params
    Then the recorded request carried the explicit state and action params
