Feature: Habr client conformance to the board-neutral Client contract
  The client must reject the wrong service section, advertise the board's quota
  and search cap, resolve a pool entry before any request, and close idempotently.

  Scenario: Construction rejects a non-Habr service section
    Given a mock service section
    When a Habr client is constructed from it
    Then construction raises a type error

  Scenario: The client advertises its board contract
    Given a Habr client factory over an all-type search
    When the client is inspected
    Then the client is a board-neutral Client
    And the service info reports 150 applications per month and 1000 search items

  Scenario: An out-of-range search index is a configuration error
    Given a Habr client factory over an all-type search with a strict router
    When the client searches with index 5
    Then the search fails with a configuration error
    And no Habr request is sent

  Scenario: Closing the client twice is idempotent
    Given a Habr client factory over a recording transport
    When the client is closed twice
    Then the underlying transport is closed exactly once
