# jobfucker UI — Authoritative Content Spec

> **Read this first.** This document is the **authoritative source of UI _content_**
> for the jobfucker GUI: screens, components, actions, and interaction logic. It is a **contract** —
> what the UI must offer and how it behaves.
>
> The visual reference is the mockup **`docs/ui/B2-headerbar-tabs.html`** — _structure and
> inspiration only_. That file's CSS is a **placeholder** and **must never be ported to QML**;
> the QML UI relies **fully on native Qt themes** (Fusion / Breeze light-or-dark / system theme).
>
> **Authority:** when this doc and the HTML disagree, **this doc wins for content and behavior**;
> the HTML wins only for pure spatial/visual placement. When this doc and the architecture doc
> (`docs/specs/jobfucker.architecture.md`) disagree, the architecture doc wins for the domain/core;
> this doc wins for UI behavior.

---

## 1. Objective

The GUI is a **first-party interface to the jobfucker engine** (full CLI parity), not a
read-only observer. From the GUI the user can:

- **Manage pipelines** — create, import (YAML), edit, activate/deactivate, duplicate, delete; view version history; export YAML.
- **Run the automation stages** — Fetch, Score, Generate CV, Apply — individually or as a chain, with a batch scope, live progress, and cancel.
- **Work the vacancies** — browse/filter/search/sort, multi-select, review results, edit result fields (score, cover letter, notes), skip/archive.
- **Observe the system** — daily-limit usage, operation history (audit), live run logs.
- **Verify human-in-the-loop** — complete captcha / SMS / one-time-code prompts surfaced by the client.

The CLI stays; the GUI must not **require** the user to leave it.

> Note: an interactive "command builder" (generating `jobfucker …` strings) is a **future backlog
> feature** and is deliberately not part of this spec.

---

## 2. Non-goals (now)

- **No visual fidelity to the mockup.** No styling is ported from HTML → QML.
- **No custom themes.** Native Qt theme only.
- **No real HH client dependency.** The GUI is built and demoed against the **Mock client**. The UI
  rewrite is planned **after** the async migration (see §7), so live progress is a real async stream,
  not a simulation, even though the data comes from the Mock client.
- **No browser/Web stack.** This is a Qt Quick (QML) desktop app.
- **No dashboard tab.** There is no separate summary/dashboard page.

---

## 3. Overall structure (one main window)

Chosen interaction family: **B2 — desktop-native, top TabBar, no headerbar**. One main window with
**three** top-level pages (tabs), persistent chrome above and below. There is **no headerbar**
(its actions live in each tab's toolbar) and **no global pipeline selector** (it lives inside the
Workspace tab).

```
┌─────────────────────────────────────────────────────────────────────────┐
│ title strip   ● ● ●                            jobfucker        [−][□][✕]│
├─────────────────────────────────────────────────────────────────────────┤
│ TABBAR   [ Pipelines ] [ Workspace ] [ Audit ]                          │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│                          < active page content >                       │
│                                                                         │
├─────────────────────────────────────────────────────────────────────────┤
│ statusbar   pipeline: AI Engineer · stage: idle · 12/25 applied · 14:32 │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Global chrome

| Zone        | Content                                                                                          | Notes                                                 |
| ----------- | ------------------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| Title strip | Window controls (native)                                                                         | Provided by the window manager / `ApplicationWindow`. |
| TabBar      | Pipelines · Workspace · Audit                                                                    | Primary navigation (`Ctrl+1..3`).                     |
| Status bar  | Current pipeline · current stage state · today's applied/limit · last-updated · connection state | Read-mostly, always-on.                               |

There is **no headerbar / app-menu bar and no global pipeline selector**. Each tab carries the
actions it needs in its own toolbar. The status bar shows the _currently scoped_ pipeline
(read-only); the place to _choose_ it is the **Workspace** tab.

---

## 5. Pages

### 5.1 Pipelines (manage)

List of **distinct pipelines** (one row per pipeline identity — never one row per config snapshot).

```
+  Pipelines                                     [+ New] [Import YAML] [Export…]
│  [●] AI Engineer — hh        active   updated 14:02      [Edit] [▾]
│  [●] Backend Python — hh     active   updated 09:31      [Edit] [▾]
│  [○] Frontend TS — hh        inactive updated Jul 12     [Edit] [▾]
│  ────────────────────────────────────────────────────────────────────
│  2 active · 1 inactive
```

- **Row content:** name, service, active/inactive indicator, last-updated.
- **Row actions:** `Edit` opens the **Pipeline Editor** dialog; `▾` menu = Activate/Deactivate, Duplicate, Delete (confirm), Export YAML.
- **Active** pipelines are the ones stages can run / that count against limits; inactive ones are preserved but not run by default.
- **Delete** is soft-delete (consistent with storage) + confirm dialog.

### 5.2 Workspace (run stages + work vacancies)

The core tab, and the heart of the first-party GUI. It is deliberately **the merge of "run actions"
and "vacancies"**: you run stages on the very vacancies you select, preview, and edit here, so a
single surface serves both roles (no duplicated list, no losing selection between tabs).

The top is the **stage runner** for the selected pipeline; the middle is the **vacancy working
set** (multi-selectable); the right is the **detail/editor**; the bottom is a **limits strip** and a
collapsible **run log**.

```
+  Workspace                              pipeline: [ v AI Engineer ]
│  FETCH        SCORE      GENERATE CV       APPLY
│  [fetch ✓ 42] [score ○12] [cv ○5]          [apply ○3]      [▶ Full run]
│  apply mode: (•) dry-run   ( ) ask confirmation   ( ) send directly
│  scope: (•) all   ( ) from [ _ ] to [ _ ]   ( ) take [ _ ]   [skip ✓]
│  ▓▓▓▓▓▓▓▓░░░░░░░░░░  42%   scoring vacancy 17/40          [Cancel]
│  ─────────────────────────────────────────────────────────────────────
│  [🔍 search] [filter ▾] [sort ▾]   [ All | Needs action | Archived ]
│   ☑ Title             Score  Status   Cover letter
│   ☑ Senior Python     ★★★★   pending  ✓ generated
│   ☐ ML Engineer       ★★★★★  pending  ○ missing
│   ☐ Backend Go        ★★★    failed   ✗ error
│   ...
│  ──────────────────────────── detail / editor (openable as modal) ────
│   description · salary · company · score(1-5) · reasoning
│   cover letter preview+edit · notes · skip · archive
│                    [Score now] [Generate CV] [Apply]
│  ── today: 12/25 applied ▓▓▓▓▓░░░ 48% ─────────────────────────────
│  ▸ run log: 14:32 fetch: 42 retrieved · 14:33 score: 12 scored, 2 errors
```

**Stage runner (top):**

- **Stage bar** = the four stages in order, each a chip with state + count:
  - state: `idle` (gray) / `running` (amber) / `done` (green) / `error` (red); count = how many vacancies are pending/in the last result.
  - Click a chip to run **that stage alone**; `▶ Full run` runs fetch → score → generate_cv → apply in sequence.
- **Apply mode — 3-state control** (radios or dropdown), replaces a simple dry-run toggle:
  - `dry-run` — produce the previews/targets but send nothing.
  - `ask confirmation` — run normally but show the **Apply preview** dialog before sending.
  - `send directly` — no preview dialog, send immediately.
    (This state persists; there is deliberately **no separate "don't ask again" checkbox** — the mode
    itself already controls whether a confirmation appears.)
- **Batch scope** control: All / `from–to` / `take` (+ `skip already processed` toggle). Applies to
  Score, Generate CV, Apply (Fetch uses separate native page controls in the CLI). Same semantics as the engine
  `BatchSelector` — an invalid combo (`to`+`take`) is rejected inline.
- **Progress + Cancel:** while running, an animated progress bar (stage + "vacancy n/total"), a live
  run log line, and a **Cancel** button. Maps to the engine's async progress stream (§7).

**Vacancy working set (middle):**

- **Multi-select** checkboxes — the basis for bulk actions (Score / Generate CV / Apply / Archive on
  the checked subset).
- **Sub-tabs / filter presets:** All · Needs action (unscored / CV missing / apply pending / failed) · Hidden (soft-deleted).
- **Search** across title/company/description; **filter** by status/score; **sort** by any column.
- **Status coloring** (native semantic colors): applied/success = success, pending = neutral,
  failed/error = error, running = attention, skipped/hidden (soft-deleted) = muted, stale (score/letter/user) = attention.
- **Row actions:** Score now · Generate CV · Preview & edit · Apply (→ respects apply mode) · Skip (with reason) · Hide/Archive (soft-delete).

**Detail / editor (right):**

- A right-side panel, **also openable as a modal** — description, salary, company, interactive 1–5
  score, score reasoning, cover-letter preview + edit, notes, skip/hide toggles, stale badges, per-row actions.
- Edits are **optimistic** and write an **audit** entry.

**Limits strip:** today's applied/limit + progress for the scoped pipeline. This is the dedicated
home for limits (there is **no Limits tab**); it is mirrored in the status bar.

**Run log (collapsible):** one line per stage completion (counts, errors), retained for the session.

### 5.3 Audit (observe)

A filterable, structured **operation log**. It renders the existing `audit_log` table — whose rows
already carry a structured JSON `details` payload — as human-readable entries (no separate logging
subsystem is introduced).

```
+  Audit                                            [filter ▾]  [pipeline ▾]
│  time    pipeline    action      details
│  14:33   A           ui_edit     score → 4
│  14:32   A           run_apply   applied 3 vacancies
│  ...
```

- Read-only table: time, pipeline, action, human-rendered details.
- Filter by pipeline and/or action type.
- Daily limits are **not** shown here (they live on the Workspace strip and status bar).

---

## 6. Dialogs / overlays

- **6.1 Pipeline Editor** — opens from New/Edit in Pipelines (§5.1).
- **6.2 Apply preview / confirmation** — shown when Apply is run with mode `ask confirmation`
  (or `dry-run`); lists the selected vacancies with per-item cover-letter preview and opt-out
  checkboxes; offers a clearly destructive **"Send N applications"** and Cancel. No persistent
  "don't ask again" — the 3-state Apply mode (§5.2) already controls when this appears.
- **6.3 Verification (human-in-the-loop)** — captcha image / SMS or one-time-code entry, surfaced
  by the client's injectable interaction provider. Exercisable with the Mock client; the real HH
  client plugs the same modal later.
- **6.4 Confirm (destructive)** — generic strong-warning confirm (delete pipeline, archive, etc.).
- **6.5 File dialogs** — native (XDG desktop portal / Qt) for Import/Export YAML.
- **6.6 Notification / toast** — completion/failure feedback: internal toast when the window is in
  focus, OS notification when out of focus. Full detail lives in the Workspace run log and Audit.

### 6.3 Verification modal (captcha / SMS / one-time code)

```
┌──────────────────────  Human verification required  ──────────────────┐
│  The host requires verification before continuing.                     │
│                                                                         │
│   [ display captcha image / or "enter SMS code sent to +7…" ]         │
│   [  input field                       ]   [Verify]                   │
│                                                          [Cancel]     │
└────────────────────────────────────────────────────────────────────────┘
```

- While a stage is blocked on verification, the stage stays `running`/`waiting` with a clear
  "awaiting verification" marker and a Cancel that aborts the stage.

---

## 7. Async / execution model (interaction logic)

> **The UI rewrite happens AFTER the async migration.** This section is aligned with the
> async model; see `docs/specs/jobfucker.architecture.md#execution-model`.

- The GUI drives the **same fully-async engine core** the CLI uses (`bootstrap.build_engine_from_pipeline`),
  never duplicating business logic (`building-multi-ui-apps`).
- **Managers `await` async storage directly on the qasync loop.** The `ThreadPoolExecutor` bridge is
  gone (async-migration D3); the window never freezes.
- **Progress:** the engine exposes an async progress stream — stage, index, total, item label. The
  Workspace runner advances its bar/log from this stream and supports **cancel** (best-effort, cooperative).
- **Result handling:** each stage returns its report → the UI maps it to counters, run-log lines,
  and model refreshes. Per-item errors are field values, not batch aborts — the log shows counts and
  the vacancy rows surface `error` states.
- **Refresh:** after a stage completes, the relevant models (`pipelines`, `vacancies`, `audit`,
  `limits`) refresh so every tab reflects new state. A periodic auto-refresh (≈30s) keeps the
  status bar / limits current.
- **Shutdown:** `await engine.dispose()` on `aboutToQuit` (D3 / D2 dispose discipline).

---

## 8. Keyboard shortcuts

| Shortcut        | Action                                     |
| --------------- | ------------------------------------------ |
| `Ctrl+N`        | New pipeline                               |
| `Ctrl+O`        | Import YAML                                |
| `Ctrl+S`        | Save (dialog in focus)                     |
| `Ctrl+F`        | Focus search (Workspace)                   |
| `Ctrl+R` / `F5` | Refresh current view                       |
| `Esc`           | Close dialog / cancel run                  |
| `F9`            | Toggle run log (Workspace)                 |
| `Ctrl+1..3`     | Switch tab (Pipelines / Workspace / Audit) |

---

## 9. State & data bindings (what feeds what)

| UI surface                          | Source                                                   |
| ----------------------------------- | -------------------------------------------------------- |
| Pipelines page                      | `pipelines` repo (non-soft-deleted, distinct identities) |
| Version history (editor)            | pipeline snapshots for the same identity                 |
| Workspace vacancy set + selection   | `vacancies` repo filtered by the scoped pipeline         |
| Workspace run log + stage counts    | stage reports + progress stream                          |
| Workspace limits strip + status bar | `daily_limits` repo                                      |
| Audit page                          | `audit_log` repo                                         |
| Verification modal                  | client interaction provider (mock now, hh later)         |

Managers are thin `QObject` facades over these repos + the engine; QML binds to models and calls
typed slots. Business logic stays in Python (`engineering-principles`), QML is presentation.

---

## 10. Open questions (to improve together)

1. **Apply-mode default:** for a freshly created pipeline, should the default be `dry-run`, `ask
confirmation`, or `send directly`? (Recommend: `ask confirmation` for safety.)
2. **Workspace scope:** is the Workspace vacancy set always scoped to exactly one pipeline (via its
   selector), or should it support an "all pipelines" view? (Recommend: single pipeline for v1.)
3. **Multi-select vs. full run scope:** when running a stage, does the batch scope operate on the
   _checked_ rows, the _filtered_ view, or the whole pipeline? (Recommend: `all`/`from–to`/`take`
   act on the whole list; the checked rows are a separate quick target.)

---

## 11. Success criteria

- A user can go from an empty DB to a fully managed setup **without leaving the GUI**: create/import
  a pipeline, fetch, score, review, apply (with `ask confirmation` + dry-run + verification),
  archive/skip — all achievable in the app.
- No clipboard/terminal is required for any core workflow.
- All engine stages, pipeline CRUD, and verification are reachable from the GUI (true CLI parity).
- The GUI never blocks/freezes during a long run; progress and cancel are always visible.
- The Workspace tab lets the user select which vacancies a stage runs on, preview/edit them, and
  run stages — without tab-switching.
- The QML uses native Qt themes only; **zero** CSS from the HTML mockup is present in `src/`.
