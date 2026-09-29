Feature: Habr vacancy search
  Fetching a slice must walk the 1-based native pages, encode the verified filters, authorize only
  when the entry needs it, enrich only the requested items, and map the SSR detail into the
  board-neutral contract.

  Scenario: An all-type slice is enriched without forcing a login
    Given a Habr client configured with an all-type search
    When positions 10 to 14 are searched with 5 items per page on entry 0
    Then Habr receives native page 3 and the all search without a login
    And the vacancy is returned with normalized full details

  Scenario: A reordered SSR state script is still extracted
    Given a Habr client configured with an all-type search and a reordered SSR script
    When positions 0 to 4 are searched with 5 items per page on entry 0
    Then the vacancy is returned with normalized full details

  Scenario: A suitable-type slice authorizes first and sends the suitable flag
    Given a Habr client with a healthy session configured with a suitable search
    When positions 0 to 4 are searched with 5 items per page on entry 0
    Then Habr receives native page 1 and the suitable search
    And the identity endpoint was checked once

  Scenario: A suitable search without credentials fails closed before any request
    Given a Habr client without credentials configured with a suitable search
    When positions 0 to 4 are searched with 5 items per page on entry 0
    Then the search fails with a configuration error
    And no Habr request is sent

  Scenario Outline: A suitable search walks with the board's fixed 25-item stride
    Given a Habr client with a healthy session configured with a suitable search and a dense catalog
    When positions 0 to 39 are searched with <page_size> items per page on entry 0
    Then Habr requests per page 25 on native pages 1 and 2

    Examples:
      | page_size |
      | 20        |
      | 50        |

  Scenario: A suitable page whose reported size disagrees with the effective stride is a protocol failure
    Given a Habr client with a healthy session configured with a suitable search whose pages report 20 items
    When positions 0 to 0 are searched with 20 items per page on entry 0
    Then the search page is a partial slice with a protocol failure

  Scenario: A page size above the cap is clamped to 50
    Given a Habr client configured with an all-type search and a single page item
    When positions 0 to 0 are searched with 100 items per page on entry 0
    Then Habr receives per page 50 on native page 1

  Scenario: A multi-page slice enriches only the requested items
    Given a Habr client configured with an all-type search and a dense catalog
    When positions 8 to 12 are searched with 5 items per page on entry 0
    Then Habr walks native pages 2 and 3 only
    And exactly 5 vacancy details are requested
    And the slice spans the requested positions

  Scenario: Stored vacancies are skipped before enrichment
    Given a Habr client configured with an all-type search and a dense catalog
    When positions 8 to 12 are searched with 5 items per page on entry 0 excluding 8 and 10
    Then the slice contains only the new vacancies
    And no detail request is made for the stored vacancies

  Scenario: A rich filter set is encoded exactly onto the search request
    Given a Habr client configured with a rich all-type filter
    When positions 0 to 4 are searched with 5 items per page on entry 0
    Then Habr receives the exact configured filter set

  Scenario: A concealed vacancy with only a predicted salary maps to nulls
    Given a Habr client configured with an all-type search and a concealed vacancy
    When positions 0 to 0 are searched with 5 items per page on entry 0
    Then the concealed vacancy maps to a null company and a null salary

  Scenario: A short page exhausts the listing
    Given a Habr client configured with an all-type search and a short page
    When positions 0 to 4 are searched with 5 items per page on entry 0
    Then the short page exhausts the listing

  Scenario: An empty 200 page exhausts the listing
    Given a Habr client configured with an all-type search and an empty catalog
    When positions 0 to 4 are searched with 5 items per page on entry 0
    Then the empty page exhausts the listing

  Scenario: A past-total not-found page exhausts the listing
    Given a Habr client configured with an all-type search whose past-total page is not found
    When positions 5 to 9 are searched with 5 items per page on entry 0
    Then the not-found page exhausts the listing

  Scenario: An offset past the accessible cap exhausts the listing
    Given a Habr client configured with an all-type search and a catalog capped below the requested offset
    When positions 1000 to 1004 are searched with 50 items per page on entry 0
    Then the over-cap window exhausts the listing

  Scenario: A mid-slice page failure yields a partial slice
    Given a Habr client configured with an all-type search whose second page fails
    When positions 0 to 9 are searched with 5 items per page on entry 0
    Then the search page is a partial slice with a transport failure

  Scenario: A malformed listing page is a protocol failure
    Given a Habr client configured with an all-type search and a malformed catalog
    When positions 0 to 4 are searched with 5 items per page on entry 0
    Then the search page is a partial slice with a protocol failure

  Scenario: Null list fields on the listing and detail map to empty values
    Given a Habr client configured with an all-type search whose listing and detail lists are null
    When positions 0 to 0 are searched with 5 items per page on entry 0
    Then the vacancy maps the null lists to no key skills

  Scenario: A state script that is not application/json is a protocol failure
    Given a Habr client configured with an all-type search whose detail script is not JSON
    When positions 0 to 4 are searched with 5 items per page on entry 0
    Then the search page is a partial slice with a protocol failure
