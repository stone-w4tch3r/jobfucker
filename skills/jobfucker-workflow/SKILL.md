---
name: jobfucker-workflow
description: >-
  Верхнеуровневый операционный цикл jobfucker и роутер по скиллам: fetch → score → ревью и итерация промпта → triage requires_attention → generate → apply, плюс batch-флаги, откат скоринга, hh-тесты, status, диагностика и лимиты.
  Используй при прогоне pipeline, повседневной работе с jobfucker, вопросах «что дальше», разборе проскоренных вакансий, обработке requires_attention, повторном скоринге после правки промпта, диагностике падений (авторизация/капча/AI), выборе между ручным шагом и агентом.
---

Для первичной настройки — jobfucker-setup; для дизайна поисков — jobfucker-collecting-vacancies; для промптов — jobfucker-creating-prompts.

# Операционный цикл jobfucker

Ментальная модель: человек решает → агент дёргает CLI → встроенный ИИ проекта делает score/generate/tests/captcha. Агент не заменяет решения пользователя.

## Роутер

| Задача | Скилл |
| --- | --- |
| Установка, первый запуск, новый pipeline (hh.ru / Habr Career), капча | `jobfucker-setup` |
| Дизайн / расширение / бэкфилл поискового пула (любая доска) | `jobfucker-collecting-vacancies` |
| Создание / калибровка / итерация скоринг- и apply-промптов | `jobfucker-creating-prompts` |

## Цикл

### 0. Одобрение

Сетевые/пишущие/платные шаги (`fetch`, `score`, `generate`, `apply`) — только по явному одобрению пользователя. `doctor` тоже сетевой (реальная авторизация + AI-вызовы), но безопасный, только читает — записи не делает, его можно вызывать без доп разрешений.

### Диагностика

Что-то падает (авторизация, капча, AI) → `jobfucker doctor --pipeline-id <id>` — прогоняет три независимые проверки (identity / captcha / scoring) и показывает, какой контур сломан.

### 1. fetch

`jobfucker fetch --pipeline-id <id> [--use-search-config N] [--refresh] [window/slice flags]`

- Идемпотентен, insert-only; повторный fetch не портит обработанное.
- `--refresh` перезаписывает только листинговые поля.
- Window/slice: любой заданный флаг = run-wide override всех записей пула.

### 2. score

`jobfucker score --pipeline-id <id> [batch flags]`

- AI, строгий вывод `{fit_score 1..5, comment}`.
- Ошибка по вакансии не рушит батч (пишется `score_error`).

### 3. Ревью и итерация промпта (самая сложная часть)

- `jobfucker vacancies dump [--pipeline-id <id>]` → читай `editable.score` и `editable.score_reasoning`.
- Промпт отработал плохо → **откат**: в дампе очисти `score` и `score_reasoning` (в `null`), затем `jobfucker vacancies apply --source FILE` (сначала `--dry-run`), затем `score` заново с обновлённым промптом.
- Методика правки промпта (какой блок менять) — `jobfucker-creating-prompts`, этапы IV–VI.
- Если правка промпта меняет смысл оценок — перезапусти и `generate` (старые письма могли устареть).

### 4. Triage requires_attention

- «requires_attention» — **конвенция тега в `comment`** скоринга (см. `jobfucker-creating-prompts`), не колонка БД.
- Найди такие вакансии в дампе (по тексту комментария).
- Варианты (решает пользователь):
  - **skip/pause** — `manual_skip: true` + `manual_skip_reason` в дампе → `vacancies apply`; в `status` уйдёт в bucket `paused`;
  - **вручную** — поправить поля или вручную написать сопроводительное одну вакансию `apply --vacancy-id ID`;
  - **агенту** — агент читает дамп, разбирает вакансию, предлагает решение;
  - **оставить** — pending, ничего не делать.
  - **убрать/игнорировать** — обновить `comment` и сделать `apply`.
  - **другое** — по запросу пользователя.

### 5. generate

`jobfucker generate --pipeline-id <id> [batch flags]` — только score ≥ `min_required_score`. Ошибки per-item.

### 6. apply

`jobfucker apply --pipeline-id <id> [batch flags]`

- eligible = есть письмо + score ≥ min + не `manual_skip` + не решён.
- Релаксации на прогон: `--min-score N`, `--include-unscored`, `--allow-without-letter`, `--vacancy-id ID ...`, `--force`.
- **Скрининг-тесты и hh-специфичные флаги — только hh.ru:** `--test-answers FILE`, `--no-test-ai`, `--has-hh-tests`, `hh-tests dump`. У Habr Career тестов нет, флаги не применяются.
- Лимиты: cap и окно — от доски (hh.ru — сутки, Habr Career — **150/месяц**). Ответ `limit_exceeded` останавливает батч, остаток pending.
- Капча на apply — только у hh.ru; у Habr Career капча лишь на логине.
- Habr Career: пейсинг ≥10 c между откликами → длинные батчи идут медленно.
- Фатальные ошибки (`ConfigurationError`, `AuthError`) останавливают батч так же.

### 7. status

`jobfucker status [--pipeline-id]` — action buckets + ledger. Показывает, что запускать дальше.

## Когда человеку, а когда агенту

- **Агент:** dump-анализ, правка pipeline/промптов, batch-команды, откат скоринга, разбор triage.
- **Человек:** решения по fit/грейду/лимитам, решение капчи, финальное одобрение fetch/apply, ручная правка письма.

## Гейты — готово только когда

- Каждый сетевой/платный шаг одобрен.
- Откат скоринга сделан через дамп (`vacancies apply`), не правкой БД напрямую.
- Секреты не в git.
- Итог прогона сверен с `status`.

## Анти-паттерны

- fetch/apply без одобрения.
- Правка БД в обход `vacancies dump|apply`.
- Итерация промпта без снимка дампа.
- Путать `manual_skip` (pause) с `deleted` (soft-delete).

## Где искать команды

`AGENTS.md`, `uv run jobfucker <cmd> -h`, `docs/specs/jobfucker.md`.
