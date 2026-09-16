Feature: HH screening-test solving
    In order to answer hh.ru employer screening tests
    As jobfucker
    I want a board-agnostic solver seam with validated answers

    Scenario: A complete answer document solves the whole test
        Given a screening test with a free-text and a choice task
        When the solution is validated
        Then the solution carries both task answers

    Scenario: A missing task answer is rejected
        Given a screening test with a free-text and a choice task missing the choice answer
        When the solution is validated
        Then validation fails mentioning the unanswered task

    Scenario: An unknown option id is rejected
        Given a screening test with a free-text and a choice task choosing an unknown option
        When the solution is validated
        Then validation fails

    Scenario: A free-text answer carrying an option is rejected
        Given a screening test whose free-text task carries an option
        When the solution is validated
        Then validation fails

    Scenario: The file solver reads its vacancy's answers
        Given a screening test with a free-text and a choice task
        And an answers file containing a valid document for that vacancy
        When the file solver solves the test
        Then the solve returns both task answers

    Scenario: The file solver rejects a vacancy missing from the file
        Given a screening test with a free-text and a choice task
        And an answers file without that vacancy
        When the file solver solves the test
        Then the solve fails mentioning the vacancy

    Scenario: The file solver reads an answers file referenced through ~
        Given a screening test with a free-text and a choice task
        And an answers file containing a valid document for that vacancy referenced through ~
        When the file solver solves the test
        Then the solve returns both task answers

    Scenario: The AI solver answers the whole test in one completion
        Given a screening test with a free-text and a choice task
        And an AI completion returning a complete answer document
        When the AI solver solves the test
        Then the solve returns both task answers
        And the AI completion ran once

    Scenario: The AI solver rejects an invalid completion
        Given a screening test with a free-text and a choice task
        And an AI completion returning an invalid answer document
        When the AI solver solves the test
        Then the solve fails

    Scenario: The AI solver retries a transiently invalid completion
        Given a screening test with a free-text and a choice task
        And an AI completion returning an invalid then a complete answer document
        When the AI solver solves the test
        Then the solve returns both task answers
        And the AI completion ran twice

    Scenario: The AI solver does not recognize a decline when the fallback is off
        Given a screening test with a free-text and a choice task
        And an AI completion returning a decline document
        When the AI solver solves the test
        Then the solve fails

    Scenario: The AI solver declines when the fallback is enabled
        Given a screening test with a free-text and a choice task
        And an AI completion returning a decline document with the fallback enabled
        When the AI solver solves the test
        Then the solve is declined with the comment

    Scenario: The prompt offers the decline fallback only when enabled
        Given a screening test with a free-text and a choice task
        And an AI completion returning answers with the fallback enabled
        When the AI solver solves the test
        Then the solve returns both task answers
        And the prompt offers the decline fallback

    Scenario: The prompt omits the decline fallback by default
        Given a screening test with a free-text and a choice task
        And an AI completion returning a complete answer document
        When the AI solver solves the test
        Then the solve returns both task answers
        And the prompt offers no decline fallback

    Scenario: The selector prefers the answers file over AI
        Given an hh test-solving config with a prompt and an openai section
        And a test-answers file argument
        When the hh test solver is selected
        Then the file solver is selected

    Scenario: The selector uses AI when configured
        Given an hh test-solving config with a prompt and an openai section
        When the hh test solver is selected
        Then the AI solver is selected

    Scenario: The selector threads the decline fallback into the AI solver
        Given a screening test with a free-text and a choice task
        And an AI completion returning a decline document is the default completion
        And an hh test-solving config with the AI decline fallback enabled
        When the hh test solver is selected
        And the selected solver solves the test
        Then the solve is declined with the comment

    Scenario: The selector disables AI on demand
        Given an hh test-solving config with a prompt and an openai section
        When the hh test solver is selected with AI disabled
        Then no solver is selected

    Scenario: No hh test-solving config selects no solver
        Given no hh test-solving config
        When the hh test solver is selected
        Then no solver is selected

    Scenario: A disabled hh test-solving section selects no solver
        Given an hh test-solving config with AI disabled
        When the hh test solver is selected
        Then no solver is selected

    Scenario: An invalid prompt template selects no solver
        Given an hh test-solving config with an invalid prompt template
        When the hh test solver is selected
        Then no solver is selected

    Scenario: A problem that gained a task is rejected
        Given a screening test with an extra unanswered task
        When the solution is validated
        Then validation fails mentioning the unanswered task

    Scenario: The dump document carries tasks and vacancy meta
        Given a screening test with a free-text and a choice task
        When the test is dumped
        Then the dump document carries the test name, the vacancy meta and both task ids

    Scenario: The prompt renders the resume, the vacancy and the whole test
        Given a screening test with a free-text and a choice task
        And an hh test prompt template injecting resume, vacancy and test
        When the hh test prompt is rendered
        Then the rendered prompt carries the resume, the vacancy and both task prompts