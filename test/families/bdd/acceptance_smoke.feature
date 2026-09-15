Feature: Apply eligibility
  In order to smoke-test the pytest-bdd Gherkin harness
  As a BDD test author
  I want a tiny apply-eligibility behaviour
  So that I know the full pytest-bdd pipeline (feature -> steps -> assertion) runs

  Scenario: A vacancy at or above the threshold is eligible to apply
    Given a vacancy scored 4
    When the minimum required score is 3
    Then the vacancy is eligible for apply

  Scenario: A vacancy below the threshold is not eligible
    Given a vacancy scored 2
    When the minimum required score is 3
    Then the vacancy is not eligible for apply
