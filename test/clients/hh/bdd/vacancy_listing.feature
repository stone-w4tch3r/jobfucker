Feature: HH listing-only vacancy search (preview path)
  The listing-only `list_vacancies` walks the same native pages as the
  enriched search but decodes only the short listing fields — no
  /vacancies/{id} detail request ever happens. This is the board-side half
  of the `jobfucker search` preview command.

  Scenario: Listing returns short items with envelope metadata and zero detail calls
    Given a healthy HH session and a rich catalog page with one vacancy
    When the whole first page is listed with 5 items per page
    Then the listing contains one short vacancy with decoded listing fields
    And the listing carries the board-reported total and web search URL
    And no vacancy detail is requested

  Scenario: Listing walks only the pages overlapping the window
    Given a healthy HH session and a dense catalog
    When positions 8 to 12 are listed with 5 items per page
    Then HH walks native pages 1 and 2 only
    And the listing spans exactly positions 8 to 12

  Scenario: A failed page fails the whole listing
    Given a healthy HH session and a catalog whose second page fails
    When the listing page fails mid-window
    Then the listing fails with the page error and no partial result
