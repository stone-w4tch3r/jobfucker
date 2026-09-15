Feature: Pipeline configuration loading and validation
  In order to run an automated job-application pipeline safely
  As the jobfucker core
  I want to load pipeline.yaml, inject referenced files and validate caps

  Scenario: A valid mock pipeline loads with referenced files injected
    Given a valid mock pipeline file is written into the runtime config dir
    When the pipeline config is loaded
    Then the config is Ok with the name "mock-demo"
    And the mock section carries resume_id "mock-resume-1" and mock filter params
    And the credential and resume contents are injected

  Scenario: A pipeline whose daily limit exceeds the mock cap is rejected
    Given a mock pipeline file with daily_apply_limit 300 is written
    When the config-time cap validation runs
    Then validation is an Err mentioning the per-auth cap

  Scenario: A pipeline that specifies all contents inline loads without any files
    Given an inline pipeline file is written into the runtime config dir
    When the pipeline config is loaded
    Then the config is Ok with the name "inline-demo"
    And the inline contents are carried exactly as written
