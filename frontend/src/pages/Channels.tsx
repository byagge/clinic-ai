import { useState } from 'react';

import { Api, auth } from '../api';
import { Async, Card, StatusBadge, useAsync } from '../ui';

export default function Channels() {
  const tenantId = auth.tenantId!;
  const channels = useAsync(() => Api.channels(tenantId), [tenantId]);
  const [token, setToken] = useState('');
  const [business, setBusiness] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [qr, setQr] = useState<{ status: string; qr: string | null; hint?: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function connectTelegram() {
    setBusy(true);
    setResult(null);
    try {
      const response = await Api.connectTelegram(tenantId, {
        bot_token: token || undefined,
        business,
      });
      setResult(JSON.stringify(response, null, 2));
      channels.reload();
    } catch (error) {
      setResult((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function connectWhatsApp() {
    setBusy(true);
    setResult(null);
    try {
      await Api.connectWhatsApp(tenantId);
      setQr(await Api.whatsappQr(tenantId));
      channels.reload();
    } catch (error) {
      setResult((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <h1>Каналы</h1>
        <p>Telegram (включая Telegram Business) и WhatsApp по QR — без оплаты Meta.</p>
      </div>

      <Card title="Подключённые каналы">
        <Async state={channels} empty={<p className="muted">Каналов пока нет.</p>}>
          {(rows) => (
            <table>
              <thead>
                <tr>
                  <th>Канал</th>
                  <th>Статус</th>
                  <th>Имя</th>
                  <th>Детали</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((channel) => (
                  <tr key={channel.id}>
                    <td>{channel.type.toUpperCase()}</td>
                    <td>
                      <StatusBadge status={channel.status} />
                    </td>
                    <td>{channel.display_name || '—'}</td>
                    <td className="muted" style={{ fontSize: 12 }}>
                      {channel.status_detail || JSON.stringify(channel.settings)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Async>
      </Card>

      <div className="grid two">
        <Card title="Telegram">
          <div className="field">
            <label>Токен бота (пусто = общий бот ARIX)</label>
            <input
              value={token}
              onChange={(event) => setToken(event.target.value)}
              placeholder="123456:ABC-DEF…"
            />
          </div>
          <label style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 12 }}>
            <input
              type="checkbox"
              style={{ width: 'auto' }}
              checked={business}
              onChange={(event) => setBusiness(event.target.checked)}
            />
            Telegram Business (бот отвечает в аккаунте клиники)
          </label>
          <button className="primary" onClick={connectTelegram} disabled={busy}>
            Подключить и поставить вебхук
          </button>
          <p className="muted" style={{ fontSize: 12 }}>
            Для Business после подключения: Telegram → Настройки → Telegram для бизнеса → Чат-боты →
            выбрать этого бота и разрешить отвечать.
          </p>
        </Card>

        <Card title="WhatsApp (Baileys)">
          <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
            Клиника привязывает свой номер один раз по QR: WhatsApp на телефоне → Настройки →
            Связанные устройства.
          </p>
          <button className="primary" onClick={connectWhatsApp} disabled={busy}>
            Получить QR
          </button>
          {qr?.qr && <img className="qr" src={qr.qr} alt="QR для привязки WhatsApp" style={{ marginTop: 12 }} />}
          {qr && !qr.qr && (
            <p className="muted" style={{ marginTop: 12 }}>
              Статус: {qr.status}. {qr.hint}
            </p>
          )}
        </Card>
      </div>

      {result && (
        <Card title="Ответ API">
          <pre className="report">{result}</pre>
        </Card>
      )}
    </>
  );
}
