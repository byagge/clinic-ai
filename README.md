# ARIX — ИИ-менеджер для клиники

Отвечает пациентам в Telegram и WhatsApp 24/7, записывает на свободное время,
напоминает, дожимает тех, кто спросил цену и пропал, возвращает базу и каждое
утро присылает владельцу отчёт.

Это реализация ТЗ «ИИ-менеджер ARIX — ядро + модуль „Клиника“» v1.0.
Модуль «Учебный центр» живёт в отдельном репозитории (`edu-ai`) на том же ядре,
см. [docs/CORE_SYNC.md](docs/CORE_SYNC.md).

---

## Демо за 5 минут

```bash
git clone <this repo> && cd clinic-ai
cp .env.example .env
make demo
```

Готово:

| Что | Где |
|---|---|
| OpenAPI и Swagger | http://localhost:8000/docs |
| Панель клиники | http://localhost:3000 (логин `owner@demo-dental.local` / `demo12345`) |
| Health и очередь заданий | http://localhost:8000/health/ready |
| QR для WhatsApp | панель → Каналы → WhatsApp |

**Ключ Anthropic не обязателен.** Без `ANTHROPIC_API_KEY` работает
детерминированный движок (`arix/agent/scripted.py`): он вызывает те же
инструменты, соблюдает те же ограничения и проходит тот же eval-набор. Это
нужно, чтобы демо показывалось без сети, а CI не зависел от внешнего API.
С ключом включается Claude с tool use — остальное не меняется.

Проверить диалог без подключения каналов:

```bash
make eval            # 64 диалога из ТЗ §15 с приёмкой
make test            # полный набор тестов
```

---

## Что уже работает

**Ядро**

- мультитенантность: `tenant_id` во всех таблицах + RLS в Postgres;
- конфиг вместо кода: тон, часы, интервалы, тексты, фичи — в `tenant_config`;
- факты только из инструментов: цены, врачи, слоты, политика — из базы;
- идемпотентность: повтор вебхука не создаёт дубль, повтор решения — дубль задания;
- всё — событие: `events` питает отчёты и атрибуцию выручки;
- PII Guard: телефоны, ИИН, e-mail, имена маскируются до отправки в LLM;
- планировщик на таблице `scheduled_jobs` + воркер, без `sleep`;
- Telegram (обычный бот и **Telegram Business**), WhatsApp через **Baileys**;
- боты администратора, владельца и врача на одном вебхуке;
- демо-генератор по карточке 2ГИС (ТЗ §12).

**Модуль «Клиника»**

- слоты с перерывами, исключениями, буферами и часовым поясом;
- запись, бронь на 10 минут, перенос, отмена, лист ожидания;
- **двойная запись физически невозможна**: `EXCLUDE USING gist` в `visits`
  (тест на 50 одновременных диалогов — `tests/test_booking.py`);
- напоминания −24 ч / −2 ч с кнопками, задача «позвонить» администратору;
- дожим +1 ч / +1 д / +3 д со стоп-условиями, recall, реактивация базы;
- отзывы: 5★ → ссылка на 2ГИС, 1–3★ → алерт владельцу;
- утренний отчёт владельцу в 08:00 и панель с воронкой;
- Mini App: услуги → врач → слот → подтверждение, «мои записи».

Границы: мы не ставим диагнозы, не анализируем снимки, не звоним голосом и не
являемся МИС. См. ТЗ §17.

---

## Как это устроено

```
Каналы (Telegram / Telegram Business / WhatsApp / Mini App)
   │ webhooks
   ▼
Channel Gateway      arix/channels      нормализация, STT, дедупликация
   ▼
Conversation Engine  arix/domain        контакты, диалоги, история, согласия
   ▼
PII Guard            arix/core/pii.py   маскирование до LLM, восстановление после
   ▼
Agent                arix/agent         Claude с tool use  |  детерминированный движок
   │                                    + guardrails до и после модели
   ▼
Domain Services      arix/verticals/clinic   слоты, запись, отзывы, отчёты
   ▼
Scheduler            arix/scheduler     durable-задания, ретраи, тихие часы
   ▼
Боты и панель        arix/bots, arix/api, frontend
```

Подробно: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Документация

| Документ | О чём |
|---|---|
| [docs/DEMO.md](docs/DEMO.md) | как показать демо, в том числе с живыми каналами |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | слои, модель данных, инварианты |
| [docs/TELEGRAM_BUSINESS.md](docs/TELEGRAM_BUSINESS.md) | подключение Telegram Business |
| [docs/WHATSAPP_BAILEYS.md](docs/WHATSAPP_BAILEYS.md) | WhatsApp без оплаты Meta: как и какие риски |
| [docs/ONBOARDING.md](docs/ONBOARDING.md) | настройка новой клиники за 3–4 часа |
| [docs/SECURITY.md](docs/SECURITY.md) | ПД, KZ/UZ, маскирование, доступы |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | прод, миграции, мониторинг, инциденты |
| [docs/TESTING.md](docs/TESTING.md) | тесты, eval, нагрузка |
| [docs/CORE_SYNC.md](docs/CORE_SYNC.md) | как ядро делится с `edu-ai` |

---

## Разработка

```bash
cd backend
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

createdb arix_dev
DATABASE_URL=postgresql+asyncpg://localhost/arix_dev python -m alembic upgrade head

uvicorn arix.main:app --reload                 # API
python -m arix.scheduler.worker                # планировщик
python -m arix.cli seed-demo                   # демо-клиника
```

Тесты требуют Postgres 16 с `btree_gist` и `pg_trgm`:

```bash
createdb arix_test
TEST_DATABASE_URL=postgresql+asyncpg://localhost/arix_test python -m pytest -q
```

Без базы юнит-тесты (слоты, тихие часы, guardrails, PII) всё равно проходят,
интеграционные помечаются как skipped.

---

## Стек

Python 3.12 · FastAPI · SQLAlchemy 2 (async) · Alembic · PostgreSQL 16 ·
Redis 7 · Claude (Anthropic API, tool use) · aiogram-совместимый Telegram-слой ·
Baileys (Node) для WhatsApp · React + Vite + TypeScript · Docker Compose.
