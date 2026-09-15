Feature: HH owned-resume listing and application validation
  get_resumes must page through GET /resumes/mine and map only ResumeInfo fields;
  applying must verify the configured resume is owned and published exactly once
  per client, and a rejected resume must stop with a configuration error.

  Scenario: A multi-page listing reports every owned resume in order
    Given a healthy HH session whose resumes span two pages
    When the client lists the owned resumes
    Then the owned resumes are reported
    And the listing contains exactly 2 resumes
    And the reported resumes keep the board order and fields
    And HH receives exactly two resume reads

  Scenario: An account without resumes reports an empty list
    Given a healthy HH session whose account holds no resumes
    When the client lists the owned resumes
    Then the owned resumes are reported
    And the listing contains exactly 0 resumes

  Scenario: A nullable can_publish_or_update field decodes without breaking the listing
    Given a healthy HH session whose resume carries a null publish flag
    When the client lists the owned resumes
    Then the owned resumes are reported
    And the listing contains exactly 1 resumes

  Scenario: A malformed resume page is a protocol failure
    Given a healthy HH session whose resume page is malformed
    When the client lists the owned resumes
    Then the listing is a protocol failure

  Scenario: A bare 401 resume read is an authorization failure
    Given a healthy HH session whose resume read is rejected as unauthorized
    When the client lists the owned resumes
    Then the listing fails with an authorization error

  Scenario: A resume-read challenge is solved and the read replayed once
    Given a healthy HH session whose first resume read meets a challenge
    When the client lists the owned resumes
    Then the owned resumes are reported
    And the browser engine submitted one answer through the real page
    And HH receives two resume reads after the solved challenge

  Scenario: An unhealthy session short-circuits before any resume read
    Given a session whose healthcheck fails with a server error
    When the client lists the owned resumes
    Then the listing fails before any resume read
    And no resume read is sent

  Scenario: An unpublished configured resume stops the application
    Given a healthy HH session whose configured resume is unpublished
    When the client applies to the vacancy with a cover letter
    Then the apply stops with a configuration error
    And no submission POST is sent

  Scenario: A foreign configured resume stops the application
    Given a healthy HH session whose configured resume is not owned
    When the client applies to the vacancy with a cover letter
    Then the apply stops with a configuration error
    And no submission POST is sent

  Scenario: The resume validation runs once for a whole apply batch
    Given a healthy HH session whose configured resume is published
    When the client applies to the vacancy twice with a cover letter
    Then both applications succeed
    And the resume listing is requested exactly once across both applications
