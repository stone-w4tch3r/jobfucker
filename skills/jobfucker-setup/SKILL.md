---
name: jobfucker-setup
description: >-
  Первичная настройка jobfucker с нуля: клонирование, зависимости, хранение данных, доступ к HH (логин/пароль, resume_id), резюме, AI-секреты, pipeline.yaml, капча, init, doctor и первый smoke-тест.
  Используй при установке jobfucker, первом запуске, настройке нового pipeline, подключении HH-аккаунта или AI-API, настройке капчи, когда init падает, когда нужно проверить что настройка работает (identity/капча/AI), или когда пользователь спрашивает «с чего начать».
---

# Первичная настройка jobfucker

Свежий клон → работающий pipeline.

## Модель работы (объясни пользователю)

Два ИИ, не путать:

- **внешний агент** (ты) — читает скиллы, дёргает CLI, читает дампы, правит pipeline и промпты;
- **встроенный ИИ проекта** — секции `openai` / `openai_captcha`; делает score, generate, hh-tests и распознаёт капчу.

Человек управляет агентом, агент — CLI. Сложные операции — только через агента. Простые может делать человек напрямую.

## Спроси пользователя СНАЧАЛА

1. Целевая роль/профиль и готовое резюме.
2. AI-провайдер: `base_url`, модель(и), API-ключ. Для капчи нужна vision-модель.
3. HH-аккаунт: логин/пароль и id резюме.

## Шаги

### 1. Клон и зависимости

- Клонировать репозиторий, `uv sync`.
- Проверка: `uv run jobfucker -h`.

### 2. Изоляция состояния

- cookies, токены, `jobfucker.db` - по умолчанию - ОС-специфичные пути (например ~/.config/jobfucker и ~/.local/share/jobfucker). Оставь как есть. Если пользователь спрашивает - можно переопределить через `CONFIG_DIR` (конфиги), `DATA_DIR`.

### 3. AI-доступ

- `openai` — текст (score/generate/hh-tests). `openai_captcha` — vision. **Секции независимы**, `openai_captcha` не наследует `openai`.
- Ключ: `api_key_file` (файл в `secrets/`, gitignored) или inline.
- `reasoning_effort` — OpenAI: low/medium/high; Gemini: true/false и тд. Для дешёвого скоринга хватает `low`/`none`. Высокий reasoning НЕ рекомендуется тк сделает запросы существенно дороже и дольше без значимого преимущества.
- Модели: подойдёт любой OpenAI-совместимый провайдер/gateway. Рабочий пример — одна быстрая flash-класс модель на оба слота; для vision обязательна поддержка изображений.
- `openai_captcha.consensus_requests` (K, по умолчанию 4) — число vision-голосов на попытку, нужно чтобы увеличить вероятность успешного распознавания капчи путем увеличения числа попыток и выведения консенсуса.
- Бесплатные варианты: OpenRouter и Kilo Gateway предоставляют free модели (нужна регистрация).

### 4. HH-доступ

- `auth.login_file` / `password_file` (или inline). Файлы — в `secrets/`, gitignored.
- `service.hh.resume_id` — id резюме на HH (из URL резюме на сайте). Позже посмотреть: `jobfucker resumes --pipeline-id <id>`.
- `service.hh.search_mode`: `catalog` (по умолчанию) или `resume_similar`.

### 5. Резюме

- `resume.path` (md/txt/yaml) или `resume.contents` inline.
- Чтобы обновить содержимое резюме, надо явно сделать `update`, оно не подцепится автоматически.

### 6. Скелет pipeline.yaml

- Образец: `pipeline.hh-fullstack.example.yaml` (в комплекте с этим скиллом, или см `docs/examples/`). Схема: `docs/schemas/pipeline.schema.json`.
- Секции: `service.<board>` (ровно один board), `auth`, `resume`, `openai`, `openai_captcha`, `scoring`, `apply`, опц. `hh_test_solving`, `limits`.
- Каждый контентный слот — XOR: file-reference **или** inline.
- **Поисковый пул не выдумывай** — скилл `jobfucker-collecting-hh-vacancies`.
- **Скоринг-промпт не пиши с нуля** — скилл `jobfucker-creating-prompts`.
- **Apply-промпт (сопроводительное) — личный.** Не копируй чужой, не выдумывай имя/контакты/подпись. Спроси у пользователя имя, контакты, тон, длину, язык; базовый каркас — `docs/examples/apply-java-fullstack.xml.j2` (приложен к скиллу). Методика — тот же скилл.
- Личные `pipelines/`, `secrets/` — в `.gitignore`, никогда не коммитить.

### 7. Капча

- HH text-captcha. Пути: AI vision (`openai_captcha`), ручной терминал (`--use-sixel` / `--use-kitty`, нужен capable terminal), `--no-captcha-ai` — выключить AI.
- AI: `consensus_requests` голосов на попытку, свежая картинка на попытку, не более `service.hh.captcha_max_attempts`.
- Исчерпание попыток → typed error с **recovery URL**: человек решает в браузере, прогон возобновляется позже. Бесконечных ретраев нет.
- Проверка капчи и настроек (AI или терминал) - `doctor`, шаг 9.

### 8. init

- `jobfucker init --config <file>` — создает и печатает pipeline-id.
- Правки: `jobfucker update --config <file>` — id стабилен, снапшот добавляется, ничего не удаляется.

### 9. Doctor и smoke-тест

1. `jobfucker doctor --pipeline-id <id>` — три независимые проверки реальной настройки, ноль записей в БД:
   - `identity` — авторизация HH (whoami: id/имя/email);
   - `captcha` — реальный handler (AI vision или терминал sixel/kitty) решает приложенный png мок-капчи;
   - `scoring` — один throwaway AI score по приложенной вакансии с реальным резюме и промптом.
2. Только после зелёного doctor: `fetch --take 5` (по одобрению пользователя), затем `jobfucker status --pipeline-id <id>`.

## Гейты — готово только когда

- `uv run jobfucker -h` работает.
- YAML валиден по схеме, `init` успешен, pipeline-id получен.
- Ключи и логины не в git.
- `jobfucker doctor --pipeline-id <id>` зелёный (identity + captcha + scoring).
- Поисковый пул и промпты пришли из профильных скиллов, не выдуманы.

## Анти-паттерны

- Секреты/инлайн-ключи в git.
- Выдуманные `resume_id`, поиски, архетипы.
- Реальный fetch без одобрения.

## Где искать команды

`AGENTS.md`, `uv run jobfucker <cmd> -h`, `docs/examples/pipeline.hh-fullstack.example.yaml`, `docs/schemas/pipeline.schema.json`.
