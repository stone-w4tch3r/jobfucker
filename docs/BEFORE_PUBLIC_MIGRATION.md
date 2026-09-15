# Before Public Migration

## top-level `shared/` package in the wheel

`pyproject.toml` `[tool.hatch.build.targets.wheel] packages = ["src/jobfucker", "shared"]`
installs a **second top-level importable package** named `shared` (maximally
generic name; a `shared` package also exists on PyPI). Harmless for
clone-and-run usage (repo root wins on `sys.path`), collides for any
wheel-based install (`pip`, `uv tool install`, `uvx --from git+...`).
Options:

### Option 1 — fold into the `jobfucker` namespace (recommended)

`shared/` → `src/jobfucker/shared/`, imports become
`from jobfucker.shared...`. One package, zero namespace pollution.

- Cost: mechanical import rewrite + config touchpoints: `pyproject` packages/
  includes, ruff/pyright `include`, coverage `source`, poe `lint_*` find
  roots, `shared_tests/` placement.
- Folding in now does not block extracting later (the directory is
  self-contained).

### Option 2 — rename the top-level package

`shared` → `jobfucker_shared` (or similar), stays a separate top-level
package in the same wheel.

- Cost: grep-replace of imports (least churn).
- Downside: still ships an extra top-level package — pollution with a
  less-collidey name. Half-measure.

### Option 3 — extract to its own repo / PyPI package

Jobfucker declares it as a normal dependency.

- Only "correct" if the code is genuinely reused across projects; today it is
  template copy-paste code (shadcn-style by design), so this buys a second
  repo + versioning + release flow for code nothing else uses. YAGNI for now.

### Option 4 — keep as-is

Works only for clone-and-run usage; any wheel-based install hits the
collision. Conflicts with the public-repo goal unless the documented install
method is "clone and run".

## Auto install browser

See `pyproject.toml` - now requires manual script call. Should auto check/install from browser engine controller.

## Ensure cross-os compatibility

Was tested only on linux.

Eg dev scripts are heavy POSIX
