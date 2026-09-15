"""HH-specific screening-test solving (the hh-scoped capability contract).

This package is the second, board-scoped contract surface besides the generic
``clients.base`` boundary (recorded arch amendment: core MAY host client-specific
features under a dedicated, board-named module + a ``*Capable`` capability
protocol — never random per-client ``if``s in generic code).

Modules (import submodules directly — this ``__init__`` stays import-free so the
package can be reached from the config/factory import chain without a cycle):

- :mod:`contract` — board-neutral-adjacent problem/solution types, the
  ``HhTestCapable`` runtime-checkable capability protocol, the ``HhTestSolver``
  seam, and solution validation (imports only ``clients.base``);
- :mod:`prompt` — user-authored prompt rendering (resume + vacancy + formatted
  test injected, the existing ``stages.prompts`` pattern);
- :mod:`ai` — AI solver; one chat completion per whole test (all tasks at
  once, structured JSON output);
- :mod:`file` — answers-file solver (the async/bulk transport);
- :mod:`dump` — the ``hh-tests dump`` problems-document serializer;
- :mod:`selector` — solver selection: answers file wins, then configured AI.

Only the HH client imports this contract; mock and future boards never do.
"""

from __future__ import annotations
