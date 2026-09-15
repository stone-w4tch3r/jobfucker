Feature: Applying with hh screening tests
    In order to apply to test-bearing hh vacancies
    As the apply stage
    I want to solve the test through the capable-client seam

    Scenario: A solver answers a test vacancy and it is applied
        Given a pipeline with a test-bearing eligible vacancy
        And an hh-capable client whose test fetch returns a screening test
        And a scripted test solver returning answers
        When the apply stage runs
        Then the vacancy is applied
        And the test answers were submitted through the capability

    Scenario: A failing solver fails the vacancy
        Given a pipeline with a test-bearing eligible vacancy
        And an hh-capable client whose test fetch returns a screening test
        And a failing test solver
        When the apply stage runs
        Then the vacancy is failed mentioning the solver error

    Scenario: An AI decline skips the vacancy instead of failing it
        Given a pipeline with a test-bearing eligible vacancy
        And an hh-capable client whose test fetch returns a screening test
        And a declining test solver
        When the apply stage runs
        Then the vacancy is skipped mentioning the solver comment

    Scenario: A test vacancy without any solver is failed
        Given a pipeline with a test-bearing eligible vacancy
        And an hh-capable client whose test fetch returns a screening test
        And no test solver
        When the apply stage runs
        Then the vacancy is failed mentioning no solver

    Scenario: A test removed before submission applies plainly
        Given a pipeline with a test-bearing eligible vacancy
        And an hh-capable client whose test fetch returns no test
        And a scripted test solver returning answers
        When the apply stage runs
        Then the vacancy is applied

    Scenario: A test removed before submission applies plainly without a solver
        Given a pipeline with a test-bearing eligible vacancy
        And an hh-capable client whose test fetch returns no test
        And no test solver
        When the apply stage runs
        Then the vacancy is applied

    Scenario: A failed test fetch fails the vacancy
        Given a pipeline with a test-bearing eligible vacancy
        And an hh-capable client whose test fetch fails
        And a scripted test solver returning answers
        When the apply stage runs
        Then the vacancy is failed mentioning the test fetch error

    Scenario: An auth-rejected test fetch stops the batch instead of failing the vacancy
        Given a pipeline with a test-bearing eligible vacancy
        And an hh-capable client whose test fetch is rejected as unauthorized
        And a scripted test solver returning answers
        When the apply stage runs
        Then the batch stops and the vacancy stays pending

    Scenario: The test filter selects only test-bearing vacancies
        Given a pipeline with one test-bearing and one testless eligible vacancy
        And an hh-capable client whose test fetch returns a screening test
        And a scripted test solver returning answers
        When the apply stage runs selecting only the test-bearing vacancies
        Then only the test-bearing vacancy is applied

    Scenario: The test filter selects only testless vacancies
        Given a pipeline with one test-bearing and one testless eligible vacancy
        And an hh-capable client whose test fetch returns a screening test
        And a scripted test solver returning answers
        When the apply stage runs selecting only the testless vacancies
        Then only the testless vacancy is applied

    Scenario: An unscored already-applied vacancy is not re-attempted
        Given a pipeline with an already-applied unscored test-bearing vacancy
        And an hh-capable client whose test fetch returns a screening test
        And a scripted test solver returning answers
        When the apply stage runs allowing unscored vacancies
        Then the vacancy is not re-attempted and stays applied