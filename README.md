# Java Agent — AI-ассистент для разработки на Java

Интерактивный CLI-инструмент на базе LangGraph для помощи в Java-разработке.  
Работает с **GigaChat** (Сбер), OpenAI, и любым OpenAI-совместимым API.

---

## Содержание

- [Что умеет](#что-умеет)
- [Требования](#требования)
- [Установка](#установка)
- [Настройка GigaChat](#настройка-gigachat)
- [Настройка OpenAI / других провайдеров](#настройка-openai--других-провайдеров)
- [Конфигурационный файл](#конфигурационный-файл)
- [Запуск](#запуск)
- [Команды](#команды)
- [Скиллы](#скиллы)
- [Инструменты агента](#инструменты-агента)
- [AGENT.md — контекст проекта](#agentmd--контекст-проекта)
- [Советы по работе](#советы-по-работе)

---

## Что умеет

- Читает и пишет файлы Java-проекта, запускает Maven/Gradle
- Автоматически выбирает стратегию работы (скилл) по смыслу запроса
- Помнит всю историю разговора внутри сессии
- Умеет сжимать длинные диалоги (`/compact`) чтобы не выходить за контекст
- Сканирует проект и генерирует `AGENT.md` (`/init`) — файл с контекстом, который подгружается автоматически при каждом запуске
- Поддерживает сложные многошаговые сценарии: автоматическая итерация компиляции и тестирования пока всё не заработает

---

## Требования

- Python **3.10+**
- Java + Maven или Gradle (в PATH)
- Аккаунт на [developers.sber.ru](https://developers.sber.ru/studio) (для GigaChat) **или** OpenAI API ключ

---

## Установка

```bash
# 1. Клонируй репозиторий
git clone <url> java-agent
cd java-agent

# 2. Создай виртуальное окружение
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Установи зависимости
pip install -r requirements.txt

# 4. Для GigaChat — установи дополнительный пакет
pip install langchain-gigachat
```

---

## Настройка GigaChat

### Шаг 1 — Получи ключ авторизации

1. Зайди на [developers.sber.ru/studio](https://developers.sber.ru/studio)
2. Создай проект → перейди в раздел **GigaChat API**
3. Создай сервисный ключ (Client ID + Client Secret) или возьми готовый **Authorization Key**
4. Выбери подходящий тип доступа:
   - `GIGACHAT_API_PERS` — личный аккаунт физлица
   - `GIGACHAT_API_CORP` — корпоративный аккаунт
   - `GIGACHAT_API_B2B` — B2B тариф

### Шаг 2 — Добавь ключ в `.env`

Создай файл `.env` в корне проекта:

```env
GIGACHAT_CREDENTIALS=ВАШ_АВТОРИЗАЦИОННЫЙ_КЛЮЧ
```

> Env-переменная имеет приоритет над значением в `java_config.yaml`.  
> Файл `.env` автоматически подгружается при запуске `cli.py`.

### Шаг 3 — Настрой `java_config.yaml`

Раскомментируй и заполни секцию `gigachat`, затем смени модели:

```yaml
# Убери api: секцию или оставь пустой — для GigaChat она не нужна

gigachat:
  credentials: ""                  # или GIGACHAT_CREDENTIALS в .env
  scope: "GIGACHAT_API_PERS"       # тип доступа
  verify_ssl_certs: false          # false = не проверять российские CA

models:
  research:      "gigachat:GigaChat-Max"   # основная модель агента
  summarization: "gigachat:GigaChat-Pro"   # для /compact
  compression:   "gigachat:GigaChat-Pro"
  final_report:  "gigachat:GigaChat-Max"
```

### Доступные модели GigaChat

| Идентификатор в конфиге | Описание |
|---|---|
| `gigachat:GigaChat` | Базовая, быстрая и дешёвая |
| `gigachat:GigaChat-Pro` | Продвинутая, хорошо для кода |
| `gigachat:GigaChat-Max` | Максимальная, лучшее качество |
| `gigachat:GigaChat-2` | Новое поколение, базовая |
| `gigachat:GigaChat-2-Pro` | Новое поколение, продвинутая |
| `gigachat:GigaChat-2-Max` | Новое поколение, максимальная |

**Рекомендация:** `GigaChat-Max` для сложных задач (CRUD, рефакторинг), `GigaChat-Pro` для рутины (объяснения, документация).

### SSL сертификаты

GigaChat использует российские CA, которых нет в стандартных хранилищах.  
Параметр `verify_ssl_certs: false` отключает проверку — это нормальная практика для работы с API Сбера.  
Если хочешь полноценную проверку — установи сертификаты Минцифры и укажи путь через `base_url` или переменную среды `REQUESTS_CA_BUNDLE`.

---

## Настройка OpenAI / других провайдеров

Если хочешь использовать OpenAI, Anthropic, Ollama или другой OpenAI-совместимый endpoint:

```yaml
api:
  base_url: "https://api.openai.com/v1"
  api_key: ""     # или OPENAI_API_KEY в .env

models:
  research:      "openai:gpt-4o"
  summarization: "openai:gpt-4o-mini"
  compression:   "openai:gpt-4o"
  final_report:  "openai:gpt-4o"
```

`.env` для OpenAI:
```env
OPENAI_API_KEY=sk-...
```

Для локального Ollama:
```bash
pip install langchain-ollama
```
```yaml
models:
  research: "ollama:qwen2.5-coder:32b"
  summarization: "ollama:qwen2.5-coder:7b"
```

Для любого OpenAI-совместимого API (LM Studio, vLLM, OpenRouter и т.д.):
```yaml
api:
  base_url: "http://localhost:1234/v1"
  api_key: "not-needed"
models:
  research: "openai:local-model"
```

---

## Конфигурационный файл

Полный пример `java_config.yaml` с объяснением всех секций:

```yaml
# ── Секция GigaChat ──────────────────────────────────────────────
gigachat:
  credentials: ""                  # ключ авторизации (лучше через .env)
  scope: "GIGACHAT_API_PERS"       # тип API доступа
  verify_ssl_certs: false          # отключить проверку SSL (для РФ CA)
  base_url: null                   # кастомный endpoint (обычно не нужен)

# ── Модели ───────────────────────────────────────────────────────
models:
  research:      "gigachat:GigaChat-Max"   # основная модель агента
  summarization: "gigachat:GigaChat-Pro"   # используется в /compact

# ── Java проект ──────────────────────────────────────────────────
java:
  project_root: "."                # корень проекта (или передай через --project)
  build_tool: "maven"              # maven | gradle | none
  java_version: "21"
  command_timeout: 120             # таймаут команды в секундах
  max_output_chars: 8000           # обрезка длинного вывода

  # Allowlist — агент не запустит команду которой нет в списке
  allowed_commands:
    - mvn
    - gradle
    - gradlew
    - ./gradlew
    - java
    - javac
    - git
    - find
    - grep
    - cat
    - ls

# ── MCP серверы (опционально) ────────────────────────────────────
mcp:
  servers: []
  # - name: git
  #   transport: stdio
  #   command: python
  #   args: ["-m", "mcp_server_git", "--repository", "."]

# ── Промты агента (опционально, есть разумные дефолты) ──────────
java_prompts:
  system: |
    Ты — опытный Java-разработчик.
    Пиши современный Java 17+, только production-ready код.
```

---

## Запуск

```bash
# Базовый запуск (берёт java_config.yaml из текущей директории)
python3 cli.py

# Указать проект явно
python3 cli.py --project /path/to/my-java-app

# Указать другой конфиг
python3 cli.py --config my_config.yaml

# Указать и конфиг и проект
python3 cli.py --config my_config.yaml --project /path/to/project
```

При запуске агент:
1. Показывает приветственную панель с информацией о проекте и модели
2. Если нет API-ключа — предупреждает, но не падает
3. Если в проекте есть `AGENT.md` — тихо загружает его (контекст будет влит при первом сообщении)

---

## Команды

Все команды начинаются с `/`. В промте показывается количество сообщений в истории.

```
(12) ~>         # 12 сообщений в истории, авто-режим
(12) [crud_generator] ~>   # активен скилл crud_generator
```

| Команда | Описание |
|---|---|
| `/help` | Показать список команд |
| `/init` | Сканировать проект и создать `AGENT.md` |
| `/compact [N]` | Сжать историю: суммаризировать старые сообщения, оставить последние N (по умолч. 10) |
| `/skills` | Список всех доступных скиллов с описаниями |
| `/skill <name>` | Принудительно активировать скилл |
| `/skill off` | Отключить активный скилл, вернуться в авто-режим |
| `/tools` | Список всех инструментов агента |
| `/config` | Показать текущую конфигурацию |
| `/project <path>` | Сменить рабочий проект без перезапуска |
| `/verbose` | Вкл/выкл показ результатов вызовов инструментов |
| `/reset` | Очистить историю диалога (новая сессия) |
| `/exit` или `/quit` | Выйти |

### `/init` — инициализация проекта

Запусти один раз на новом проекте:

```
~> /init
```

Агент:
1. Изучит структуру проекта
2. Прочитает `pom.xml` / `build.gradle`
3. Обойдёт ключевые Java-файлы
4. Создаст `AGENT.md` — документ с контекстом проекта

При следующих запусках `AGENT.md` подгружается автоматически — агент сразу знает о проекте.

### `/compact` — сжатие истории

При длинном диалоге у модели заканчивается контекстное окно. Используй `/compact`:

```
(35) ~> /compact
# или оставить больше сообщений:
(35) ~> /compact 15
```

Агент суммаризирует старые сообщения через LLM (`models.summarization`) и создаёт новый тред с компактной историей. Агент сам напомнит о `/compact` когда накопится 30 сообщений.

---

## Скиллы

Агент автоматически определяет нужный скилл по смыслу запроса. Можно принудительно задать скилл через `/skill <name>`.

| Скилл | Когда активируется | Что делает |
|---|---|---|
| `crud_generator` | «создай CRUD для сущности», «REST для Product» | Генерирует Controller + Service + Repository + Entity + DTO |
| `fix_build` | «ошибка компиляции», «не компилируется», `cannot find symbol` | Итеративно компилирует и правит ошибки до успеха (до 10 итераций) |
| `fix_tests` | «тесты упали», «failing tests», «тест не проходит» | Запускает тесты, анализирует падения, правит код, повторяет |
| `spring_boot` | «Spring Boot», `@RestController`, «JWT авторизация», «actuator» | Помогает с конфигурацией и архитектурой Spring Boot |
| `code_review` | «ревью», «проверь код», «качество кода» | Глубокое ревью: SOLID, производительность, безопасность |
| `explain_code` | «объясни», «как работает», «не понимаю» | Пошаговое объяснение кода |
| `add_tests` | «напиши тесты», «добавь JUnit», «покрытие тестами» | JUnit 5 + AssertJ + Mockito тесты с edge cases |
| `modernize` | «модернизируй», «Java 21», «перепиши на records» | Рефакторинг на современный Java: records, sealed, pattern matching |
| `extract_service` | «вынеси в сервис», «жирный контроллер», SRP | Выносит бизнес-логику из контроллера в сервисный слой |
| `generate_docs` | «javadoc», «документация», «README» | Javadoc, README, описание API |
| `dependency_audit` | «аудит зависимостей», «устаревшие библиотеки», «уязвимости» | Проверяет и обновляет зависимости, ищет CVE |
| `design_pattern` | «паттерн», «factory», «observer», «strategy» | Применяет паттерны GoF к существующему коду |
| `find_library` | «найди библиотеку», «посоветуй зависимость» | Подбирает библиотеки с обоснованием и добавляет в pom.xml |

`fix_build` и `fix_tests` — особые скиллы-подграфы: они запускают отдельный автономный цикл исправлений.

### Примеры запросов

```
~> создай CRUD для сущности Order со статусами
⚡ авто-скилл: crud_generator

~> проект не компилируется после добавления новой зависимости
⚡ авто-скилл: fix_build

~> объясни как работает наш AuthFilter
⚡ авто-скилл: explain_code

~> добавь паттерн Strategy для расчёта скидок
⚡ авто-скилл: design_pattern
```

### Принудительная активация скилла

```
~> /skill add_tests
[add_tests] ~> напиши тесты для OrderService
```

---

## Инструменты агента

Агент использует встроенные Python-инструменты (никаких Node.js, только open-source):

| Инструмент | Описание |
|---|---|
| `read_file` | Прочитать файл проекта |
| `write_file` | Создать или перезаписать файл |
| `create_directory` | Создать директорию |
| `delete_file` | Удалить файл |
| `move_file` | Переместить / переименовать файл |
| `list_directory` | Список файлов в директории |
| `find_files` | Найти файлы по паттерну (glob) |
| `search_in_files` | Поиск текста в файлах (grep-like) |
| `run_command` | Выполнить команду из allowlist |
| `run_maven` | Запустить `mvn <goal>` |
| `run_gradle` | Запустить `gradle <task>` |
| `get_project_structure` | Получить дерево проекта |
| `read_pom_or_build_file` | Прочитать pom.xml или build.gradle |

Все команды через `run_command` фильтруются по `allowed_commands` в конфиге — агент не сможет запустить ничего лишнего.

Посмотреть список инструментов в CLI: `/tools`

---

## AGENT.md — контекст проекта

`AGENT.md` — это файл в корне проекта, который агент читает при каждом запуске. Он экономит токены и ускоряет работу: агент не тратит время на изучение проекта с нуля каждый раз.

### Создание автоматически через `/init`

```
~> /init
```

### Создание вручную

Создай `AGENT.md` в корне проекта:

```markdown
# Project: my-shop

## Обзор
Интернет-магазин на Spring Boot. REST API для управления заказами и товарами.

## Стек
- Java 21
- Maven 3.9
- Spring Boot 3.2
- PostgreSQL 16
- Redis (кэш сессий)

## Ключевые зависимости
- spring-boot-starter-web
- spring-boot-starter-data-jpa
- spring-boot-starter-security
- lombok
- mapstruct

## Структура пакетов
com.example.shop/
  controller/   — REST контроллеры
  service/      — бизнес-логика
  repository/   — JPA репозитории
  entity/       — JPA сущности
  dto/          — DTO запросов/ответов
  config/       — конфигурация Security, Redis

## Соглашения
- DI через конструктор (не @Autowired на полях)
- Транзакции только в сервисном слое
- MapStruct для маппинга Entity ↔ DTO
- ResponseEntity<T> для всех контроллеров
```

### Обновление AGENT.md

После значимых изменений в проекте (новый модуль, смена стека, рефакторинг):

```
~> /init
AGENT.md уже существует. Перезаписать? [y/N]: y
```

---

## Советы по работе

### Первый запуск с новым проектом

```bash
python3 cli.py --project /path/to/project
~> /init          # создать AGENT.md (одноразово)
~> /config        # убедиться что всё настроено верно
```

### Эффективная работа с контекстом

- Запускай `/compact` когда счётчик сообщений в промте достигает ~30
- GigaChat-Max имеет контекстное окно ~32k токенов — этого хватает примерно на 20-25 обменов с кодом
- Если нужно начать чистый диалог — `/reset` (история сотрётся, `AGENT.md` останется)

### Работа с несколькими проектами

Не нужно перезапускать CLI — достаточно `/project`:

```
~> /project /path/to/project-a
~> /project /path/to/project-b
```

### Verbose режим для отладки

```
~> /verbose
```

Включает показ результатов каждого вызова инструмента — полезно когда агент делает что-то неожиданное.

### Структура `.env`

Рекомендуемый способ хранения ключей:

```env
# GigaChat
GIGACHAT_CREDENTIALS=ВАШ_КЛЮЧ_ИЗ_СБЕРА

# Или OpenAI
# OPENAI_API_KEY=sk-...
# OPENAI_BASE_URL=https://api.openai.com/v1
```

`.env` автоматически загружается при старте. Файл **не** должен попадать в git — добавь его в `.gitignore`.

---

## Структура проекта

```
deep_research_agent/
├── cli.py              # точка входа, интерактивный CLI
├── session.py          # LangGraph сессия, streaming, compact
├── config.py           # Pydantic модели конфигурации
├── router.py           # авто-определение скилла по тексту
├── skill_loader.py     # загрузчик YAML и Python скиллов
├── java_tools.py       # 13 инструментов для работы с Java-проектом
├── java_config.yaml    # конфигурация (редактируй этот файл)
├── requirements.txt    # зависимости Python
└── skills/             # скиллы
    ├── crud_generator.yaml
    ├── fix_build.yaml
    ├── fix_build.py    # subgraph: компиляция → правка → повтор
    ├── fix_tests.yaml
    ├── fix_tests.py    # subgraph: тесты → анализ → правка → повтор
    ├── spring_boot.yaml
    └── ...             # ещё 8 YAML-скиллов
```
