Feature: Habr search filter contract maps onto the verified wire vocabulary
  In order to never send a filter the board silently ignores
  As a pipeline author
  I want every supported filter to have exactly one verified wire spelling

  Scenario: The default filter encodes to the default wire filters
    Given a default Habr filter
    When the filter is encoded to wire filters
    Then the wire filter keeps the defaults

  Scenario: A fully populated filter encodes every V1 field
    Given a Habr filter with every V1 field set
    When the filter is encoded to wire filters
    Then the wire sort is date
    And the wire qid is 5
    And the wire currency is RUR
    And the wire employment type is full_time
    And the wire salary is 100000
    And the wire skills are 1 and 2
    And the wire locations are c_1 and r_2
    And the wire filter marks remote
    And the wire filter marks with-salary

  Scenario Outline: A qualification grade maps to its numeric qid
    Given a Habr filter with qualification <grade>
    When the filter is encoded to wire filters
    Then the wire qid is <qid>

    Examples:
      | grade  | qid |
      | intern | 1   |
      | junior | 3   |
      | middle | 4   |
      | senior | 5   |
      | lead   | 6   |

  Scenario Outline: A sort maps to its wire spelling
    Given a Habr filter with sort <sort>
    When the filter is encoded to wire filters
    Then the wire sort is <sort>

    Examples:
      | sort        |
      | relevance   |
      | date        |
      | salary_desc |
      | salary_asc  |

  Scenario Outline: A currency maps to its uppercase wire code
    Given a Habr filter with currency <currency>
    When the filter is encoded to wire filters
    Then the wire currency is <code>

    Examples:
      | currency | code |
      | rur      | RUR  |
      | eur      | EUR  |
      | usd      | USD  |
      | uah      | UAH  |
      | kzt      | KZT  |

  Scenario Outline: An employment maps to its wire spelling
    Given a Habr filter with employment <employment>
    When the filter is encoded to wire filters
    Then the wire employment type is <employment>

    Examples:
      | employment |
      | full_time  |
      | part_time  |

  Scenario Outline: A search type maps to its wire spelling
    Given a Habr search entry with search type <kind>
    When the entry's search type is encoded
    Then the encoded search type is <kind>

    Examples:
      | kind     |
      | all      |
      | suitable |

  Scenario: A bare numeric location is rejected
    Given a Habr filter with a bare location "123"
    When the filter is constructed
    Then the filter is rejected

  Scenario: A non-positive skill id is rejected
    Given a Habr filter with skill id 0
    When the filter is constructed
    Then the filter is rejected
