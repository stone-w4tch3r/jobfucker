Feature: Habr listing-only vacancy search (preview path)
  The listing-only `list_vacancies` walks the same 1-based native pages as the
  enriched search but decodes only the short listing fields — no /vacancies/{id}
  detail request ever happens. This is the board-side half of the
  `jobfucker search` preview command.

  Scenario: Listing returns short items with envelope metadata and zero detail calls
    Given a Habr client configured for listing a rich page
    When positions 0 to 4 are listed with 5 items per page on entry 0
    Then the listing contains one short vacancy with decoded listing fields
    And the listing carries the board-reported total and a client-built search URL
    And no vacancy detail is requested

  Scenario: Listing walks only the pages overlapping the window
    Given a Habr client configured for listing a dense catalog
    When positions 8 to 12 are listed with 5 items per page on entry 0
    Then the listing walks native pages 2 and 3 only
    And the listing spans exactly the requested positions

  Scenario: A failed page fails the whole listing
    Given a Habr client configured for listing whose second page fails
    When positions 0 to 9 are listed with 5 items per page on entry 0
    Then the listing fails with the page error and no partial result

  Scenario: A null locations list maps to a null area
    Given a Habr client configured for listing a page with null locations
    When positions 0 to 0 are listed with 5 items per page on entry 0
    Then the listing maps the null locations to a null area
