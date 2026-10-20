# Общее ядро для `clinic-ai` и `edu-ai`

ТЗ требует одно ядро на две вертикали, а репозитория два — чтобы клинику и
учебный центр можно было деплоить и продавать независимо (и чтобы у KZ-клиник
был отдельный инстанс в РК, см. [SECURITY.md](SECURITY.md)).

## Что именно общее

Байт-в-байт одинаково в обоих репозиториях:

```
backend/arix/config.py        backend/arix/logging_setup.py
backend/arix/ops.py           backend/arix/payments.py
backend/arix/stt.py           backend/arix/cli.py          (кроме вертикальных команд)
backend/arix/core/            контекст, конфиг, PII, события, время, крипто, пароли
backend/arix/db/              база, сессии, модели ядра
backend/arix/channels/        Telegram, Telegram Business, WhatsApp, dry-run
backend/arix/domain/          контакты, диалоги, доставка, передача человеку, уведомления
backend/arix/agent/           промпт, инструменты ядра, оба «мозга», guardrails, runtime
backend/arix/kb/              поиск по базе знаний
backend/arix/scheduler/       очередь заданий, воркер, обработчики ядра, реестр
backend/arix/templates/       движок текстов (наборы текстов различаются)
waweb/                        сайдкар WhatsApp
```

Различается только вертикаль:

```
clinic-ai: backend/arix/verticals/clinic/   doctors, services, slots, visits, reviews
edu-ai:    backend/arix/verticals/edu/      courses, groups, students, invoices, attendance
```

## Как ядро не знает о вертикали

Два узких контракта:

1. **`VerticalAdapter`** (`agent/runtime.py`) — протокол с методами
   `playbook` (детерминированный диалог вертикали), `handle_button`
   (нажатие кнопки) и `on_conversion` (что делать после записи или
   зачисления). Рантайм получает объект вертикали в конструкторе и больше
   ничего о ней не знает.
2. **Реестр планировщика** (`scheduler/registry.py`) — вертикаль регистрирует
   свои обработчики заданий, ежедневную постановку через `register_daily`
   и два провайдера, которые нужны заданиям ядра: подсказку со слотами для
   текста дожима и построитель дневного отчёта.

Поэтому в `arix/agent`, `arix/core`, `arix/channels` и `arix/scheduler` нет ни
одного `import` из `arix.verticals.*` — это легко проверить:

```bash
grep -rn "verticals" backend/arix/{agent,core,channels,scheduler,domain,kb} && echo "НАРУШЕНО" || echo "ядро чистое"
```

## Как синхронизировать

```bash
./scripts/sync-core.sh ../edu-ai        # из clinic-ai в edu-ai
./scripts/sync-core.sh ../clinic-ai     # или наоборот
```

Скрипт копирует только общие каталоги, не трогает `verticals/`, миграции,
шаблоны YAML и тесты вертикали, и в конце печатает `git status` целевого репо,
чтобы изменения можно было просмотреть перед коммитом.

Порядок работы: правка ядра делается в одном репозитории, прогоняется
`make test` там, затем `sync-core.sh`, затем `make test` во втором. Правка
вертикали остаётся в своём репозитории.

## Почему не отдельный пакет в PyPI

Пока вертикалей две и обе наши, публикация пакета добавила бы версионирование
и релизный цикл, но не убрала бы ни одной ошибки: 95% правок ядра всё равно
нужно проверить на обеих вертикалях сразу. Когда появится третья вертикаль или
внешний интегратор, `arix-core` выносится в пакет — контракты для этого уже
узкие, менять код не придётся.
