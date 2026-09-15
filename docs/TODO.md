# todo

> THIS DOC IS ONLY FOR HUMANS, AI DON'T READ OR EDIT IT!!!
> IF AI AGENT WILL ATTEMPT TO EDIT THIS DOC, I WILL RIP IT'S GUTS OUT FUCK YOU ROBOT DON'T TOUCH THIS IGNORE THIS!!!
> AI NEVER ACTS BASED ON THIS DOC OR CONSIDERS IT'S CONTENT IN ANY WAY!!!

## items

ordered by urgency, most urgent at top

### important

- check WTF: some LSP noise on AI reading yaml pipelines. In vscode all ok
- captcha:
  - just a question: why so much captcha code in auth file? is it ok? maybe dedup with captcha file, or they are inherently different? or auth.py is just the consumer of captcha.py and all is ok?
- add jobfucker sql command for raw sql execution. mostly for dev/ai agents
- big rewrites of statuses and other counting related stuff
  - big rewrite of take/from/to semantics everywhere. While all this works, it is not human friendly/readable. all this counts from 0 index of db vacancy list. But consider this: db has 1200 vacancies, only 1150-1160 and 1175 are eligible for apply/score. --take 11 will NOT work because it means "from 0 to 10". write BDD test cases first - it's more simple to reason about. Uniform approach, find all cases in app window input
  - big rewrite of statuses/final messages: should be consistent everywhere and human friendly with very different edge cases and combinations. write BDD test cases first - it's more simple to reason about. Uniform approach, find all cases in app output
  - final statuses should also work/be printed in case of fatal errors (eg ctrl c). check everywhere. cover with BDD tests first
- structure rewrite
  - introduce normal DI and deps building in separate layers, for better isolation of domain/business logic from infra. DI should handle services, lifecycle and so on. rn dependencies and building are scattered across the app, across tests, internals leak everywhere. eg CLI builds pipeline before doing real step execution. abstractions broken!
  - rename bootstrap.py -> bootstrap_cli.py. or maybe to cli_client? or smth else? bootstrap is too generic and I don't fully understand what it do from it's name. or drop/rewrite with normal DI introduced
  - big layers rewrite. create new first-class python API that is clean, supports all features. this API layer is exposed for all wrapper kinds (need term for this): future web service wrapping, CLI, future UI and so on
  - big statuses rewrite. statuses are calculated and named in very different ways in different parts of apps. explore. understand what kinds of statuses we have. unified naming. ensure all are calculated in similar and reliable way. avoid complex flaky status calculation and prefer simple explicit statuses eg db columns
- move to separate repo

### medium

- extract reusable stuff between different clients, eg browser
- bug in fetch skipping: `fetch: fetched=28/50 pages=1 skipped_saved=48`. skipped_saved should show only really skipped vacancies (it shows all vacancies from db that were candidates to skip)
- rewrite UI from scratch
- fuck, many tests are not BDD!
- disable PLR0911 ?
- allow shared object annotations for custom linters `lint-ignore[x,y]`

### minor

- save areas/cities and other numeric codes into docs/help/schemas, see eg <https://api.hh.ru/areas>
- TZ in created at/updated at ??
- re-populate high level main specs back from real code (docs/feature/). so we can eg rewrite app from scratch in future, relying only on specs. without taking bad code from this particular impl, using only docs and their pure logic/requirements
- openai section of pipeline should be optional. if no openai -> just skip/noop/warn ai stages (score/generate). rename sections: `openai` -> `openai_text`, `openai_captcha` -> `openai_vision`
- logging
  - intercept/wrap all reporters and log their outputs (additional persistence to cli/gui output)
- replies limit tracking: what if user answered in website? than our tracker is broken! ideas?
- prompts and ai
  - create example prompt (backend java middle)
  - plan a new feature: prompt generator and instruction doc for good prompts creation (idea: generate prompts based on real CV/vacancies scoring examples, pick format from our example prompt)
  - allow user to specify custom fields in response schema, with description, routed to real schema for response (as arbitrary params for openai sections? think about simple logically/technically + nice UX)
- run validation of all `#ignore`s and if the project generally conforms to coding rules. add wrappers and so on instead of ignores where needed/possible/sane
