Feature: HH vacancy search
  Fetching a slice must preserve native paging, apply the supported filters, enrich only the
  requested items, and return enriched vacancies.

  Scenario: A catalog page is filtered and fully enriched
    Given a healthy HH session and a catalog page with one vacancy
    When positions 10 to 14 are searched with 5 items per page
    Then HH receives native page 2 and the configured basic filters
    And the vacancy is returned with normalized full details

  Scenario: An empty native page is a successful empty result
    Given a healthy HH session and an empty catalog page
    When positions 15 to 19 are searched with 5 items per page
    Then the search returns an empty vacancy list
    And no vacancy detail is requested

  Scenario: A rich filter set is encoded exactly onto the catalog request
    Given a healthy HH session with a rich advanced filter
    When positions 0 to 4 are searched with 5 items per page
    Then HH receives the exact configured filter set
    And the vacancy is returned with normalized full details

  Scenario: A search_field filter without a query fails closed
    Given a healthy HH session with a search-field filter and no query
    When positions 0 to 4 are searched with 5 items per page without a query
    Then the search walk fails with a configuration error
    And no vacancy catalog request is sent

  Scenario: An error envelope with HTTP 200 is rejected
    Given a healthy HH session and a catalog error envelope
    When positions 0 to 4 are searched with 5 items per page
    Then the search walk fails with a bad request error

  Scenario: A multi-page slice enriches only the requested items
    Given a healthy HH session and a dense catalog
    When positions 8 to 13 are searched with 5 items per page
    Then HH walks native pages 1 and 2 only
    And exactly 6 vacancy details are requested
    And the slice spans the requested positions

  Scenario: Stored vacancies are skipped before enrichment
    Given a healthy HH session and a dense catalog
    When positions 0 to 4 are searched with 5 items per page excluding d-0 and d-2
    Then the slice contains only the new vacancies
    And no detail request is made for the stored vacancies
