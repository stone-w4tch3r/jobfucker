Feature: Habr owned-resume listing
  Habr has no owned-resume JSON and no numeric resume id: one account exposes
  exactly one resume whose id is the account alias and whose updated_at is not
  established by the board.

  Scenario: The authorized account reports its single alias resume
    Given a Habr client with a healthy session
    When the client lists the owned resumes
    Then the owned resumes contain one entry
    And the entry carries the account alias, the profile name, and no updated timestamp
    And the identity endpoint was checked exactly once
