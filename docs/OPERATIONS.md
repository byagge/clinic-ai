# Эксплуатация

## Прод-деплой

```bash
git clone <repo> && cd clinic-ai
cp .env.example .env            # заполнить секреты, PUBLIC_BASE_URL на домен
docker compose run --rm migrate
docker compose up -d backend worker waweb frontend
docker compose exec backend python -m arix.cli arm-daily
```

Что обязательно поменять в `.env` перед продом:

- `ENV=prod`, `DEBUG=false`, `LOG_JSON=true`;
- `SECRET_KEY`, `ADMIN_API_TOKEN`, `CREDENTIALS_ENCRYPTION_KEY`,
  `TELEGRAM_WEBHOOK_SECRET`, `WA_WEBHOOK_SECRET`, `WA_BAILEYS_TOKEN`;
- `PUBLIC_BASE_URL` — реальный HTTPS-домен (от него зависят вебхуки);
- `POSTGRES_PASSWORD` и `POSTGRES_SUPERUSER_PASSWORD`;
- `DATA_REGION=kz` для казахстанского инстанса.

Перед бэкендом — reverse proxy с TLS. Бэкенд запускается с `--proxy-headers`,
так что реальный IP берётся из `X-Forwarded-For` (используется в `audit_log`).

В проде **не** полагайтесь на автоприменение миграций в `backend`: сервис
`migrate` запускается отдельно, чтобы два инстанса не мигрировали параллельно.

## Масштабирование

| Компонент | Как масштабировать |
|---|---|
| `backend` | несколько реплик за балансировщиком, состояние только в БД |
| `worker` | несколько реплик: аренда через `FOR UPDATE SKIP LOCKED`, двойной доставки нет |
| `waweb` | **одна** реплика на сессию WhatsApp: учётные данные привязаны к тому |
| `postgres` | вертикально + реплика для чтения отчётов |

## Миграции

```bash
make revision m="add waitlist offer"     # автогенерация
docker compose run --rm migrate          # применить
```

Правила: ни одной деструктивной операции без двух шагов (сначала добавить и
заполнить, потом в следующем релизе удалить); перечисления — строки с `CHECK`,
чтобы новое значение не требовало блокировки таблицы.

## Мониторинг

```bash
curl -s localhost:8000/health/ready      # БД, версия миграции, лаг очереди, упавшие каналы
curl -s localhost:8000/metrics           # Prometheus-формат
```

`/health/ready` отдаёт `ok: false`, если версии миграции нет или очередь
отстала больше чем на 10 минут — это и есть liveness для балансировщика.

Алерты в Telegram ARIX (`TELEGRAM_OPS_BOT_TOKEN`, `TELEGRAM_OPS_CHAT_ID`):

| Событие | Когда |
|---|---|
| мёртвое задание | после `JOB_MAX_ATTEMPTS` попыток |
| канал отвалился | провайдер вернул «unauthorized / logged out» |
| очередь отстаёт | старейшее задание ждёт больше 5 минут |
| WhatsApp Cloud API | израсходовано 80% бесплатной квоты |
| неизвестный тип задания | в очереди задание без обработчика |

Что смотреть в логах (они структурированные, `LOG_JSON=true` в проде):

```bash
docker compose logs -f backend | grep guardrail_blocked   # бот пытался сказать лишнее
docker compose logs -f worker  | grep job_failed
docker compose logs -f backend | grep '"turn"'            # исход каждого хода диалога
```

## Частые инциденты

| Симптом | Диагноз | Действие |
|---|---|---|
| бот молчит в одном диалоге | диалог взял человек | панель → Диалоги → «Вернуть боту» |
| бот молчит везде | `/pause_bot` или упал воркер | `/resume_bot`; `docker compose ps worker` |
| «это время только что заняли» часто | админ пишет записи мимо панели | заносить телефонные записи в панель (Календарь) |
| напоминания не приходят | воркер не запущен или тихие часы | `docker compose logs worker`; проверить `quiet_hours` |
| пациент получил два сообщения | разные задания с разными `dedupe_key` | найти в `scheduled_jobs` по `tenant_id` и `kind` |
| цены «не те» | прайс в БД отличается от бумажного | панель → Прайс или `/prices код от до` |
| бот передаёт человеку слишком часто | в базе знаний нет ответов | панель → База знаний, добавить факты |

Диагностика одного диалога:

```sql
SELECT m.seq, m.direction, m.sender, left(coalesce(m.stt_text, m.text), 120)
FROM messages m
JOIN conversations c ON c.id = m.conversation_id
WHERE c.tenant_id = '<tenant>' AND c.peer_id = '<peer>'
ORDER BY m.seq;
```

Что система решала на каждом ходу:

```sql
SELECT created_at, type, entity_ref, data
FROM events WHERE tenant_id = '<tenant>' AND conversation_id = '<conv>'
ORDER BY created_at;
```

## Бэкапы

```bash
docker compose exec -T postgres pg_dump -U postgres arix | gzip > arix-$(date +%F).sql.gz
docker run --rm -v arix-clinic_wasessions:/data -v "$PWD":/backup alpine \
  tar czf /backup/wasessions-$(date +%F).tar.gz -C /data .
```

Том `wasessions` — это и есть привязка номеров WhatsApp. Без него придётся
сканировать QR заново.

Восстановление:

```bash
gunzip -c arix-2026-10-19.sql.gz | docker compose exec -T postgres psql -U postgres arix
```

## Пауза и архивация тенанта

```bash
curl -X PATCH "$PUBLIC_BASE_URL/admin/tenants/<id>/status?new_status=paused" \
  -H "X-Arix-Admin-Token: $ADMIN_API_TOKEN"
```

Воркер пропускает задания тенантов не в статусах `demo/pilot/active`, поэтому
пауза мгновенно останавливает все исходящие. Демо-тенанты архивируются
автоматически через `DEMO_TTL_DAYS`.
