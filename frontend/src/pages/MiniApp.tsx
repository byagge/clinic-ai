// Telegram Mini App for patients: услуги → врач → слот → подтверждение.
//
// Auth is the Telegram WebApp initData signature, verified server-side, so the
// patient can only ever see and change their own visits.
import { useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';

import { MiniApi } from '../api';
import { Async, useAsync } from '../ui';

type TelegramWebApp = {
  initData: string;
  ready: () => void;
  expand: () => void;
  MainButton?: { hide: () => void };
  initDataUnsafe?: { user?: { first_name?: string; last_name?: string } };
};

function webApp(): TelegramWebApp | null {
  const w = window as unknown as { Telegram?: { WebApp?: TelegramWebApp } };
  return w.Telegram?.WebApp ?? null;
}

export default function MiniApp() {
  const { slug = '' } = useParams();
  const app = useMemo(() => {
    const instance = webApp();
    instance?.ready();
    instance?.expand();
    return instance;
  }, []);
  const initData = app?.initData || '';

  const [step, setStep] = useState<'services' | 'slots' | 'form' | 'done' | 'visits'>('services');
  const [serviceId, setServiceId] = useState<string | null>(null);
  const [slotKey, setSlotKey] = useState<string | null>(null);
  const [name, setName] = useState('');
  const [phone, setPhone] = useState('');
  const [consent, setConsent] = useState(false);
  const [booked, setBooked] = useState<{ when: string; doctor: string; service: string; address: string | null } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const profile = useAsync(
    () => (initData ? MiniApi.profile(slug, initData) : Promise.reject(new Error('no initData'))),
    [slug, initData],
  );
  const services = useAsync(
    () => (initData ? MiniApi.services(slug, initData) : Promise.resolve([])),
    [slug, initData],
  );
  const slots = useAsync(
    () =>
      initData && serviceId ? MiniApi.slots(slug, initData, serviceId) : Promise.resolve([]),
    [slug, initData, serviceId],
  );
  const visits = useAsync(
    () => (initData && step === 'visits' ? MiniApi.myVisits(slug, initData) : Promise.resolve([])),
    [slug, initData, step],
  );

  if (!initData) {
    return (
      <div className="mini">
        <h1>Откройте через Telegram</h1>
        <p className="muted">
          Эта страница работает только внутри Telegram Mini App — там мы можем проверить, что вы это
          вы, и показать ваши записи.
        </p>
      </div>
    );
  }

  async function book() {
    if (!slotKey) return;
    setBusy(true);
    setError(null);
    try {
      const result = await MiniApi.book(slug, initData, {
        slot_key: slotKey,
        name: name || undefined,
        phone: phone || undefined,
        consent_pd: consent,
      });
      setBooked(result);
      setStep('done');
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const needsForm = profile.data
    ? !profile.data.contact.name || !profile.data.contact.phone || !profile.data.contact.consent_pd
    : true;

  return (
    <div className="mini">
      <Async state={profile}>
        {(data) => (
          <>
            <h1>{data.tenant.name}</h1>
            <p className="muted" style={{ marginTop: 0 }}>
              {data.tenant.address || data.tenant.city || ''}
            </p>
          </>
        )}
      </Async>

      <div className="steps">
        <span className={step === 'services' ? 'active' : ''}>услуга</span>
        <span>›</span>
        <span className={step === 'slots' ? 'active' : ''}>время</span>
        <span>›</span>
        <span className={step === 'form' || step === 'done' ? 'active' : ''}>подтверждение</span>
        <button
          className="small"
          style={{ marginLeft: 'auto' }}
          onClick={() => setStep(step === 'visits' ? 'services' : 'visits')}
        >
          {step === 'visits' ? 'Записаться' : 'Мои записи'}
        </button>
      </div>

      {step === 'services' && (
        <div className="card">
          <Async state={services} empty={<p className="muted">Услуги пока не заполнены.</p>}>
            {(rows) =>
              rows.map((service) => (
                <button
                  key={service.service_id}
                  className="option"
                  disabled={!service.online_bookable}
                  onClick={() => {
                    setServiceId(service.service_id);
                    setStep('slots');
                  }}
                >
                  <span>
                    {service.name}
                    <br />
                    <span className="muted" style={{ fontSize: 12 }}>
                      {service.duration_min} мин
                      {service.online_bookable ? '' : ' · только через администратора'}
                    </span>
                  </span>
                  <span className="price">{service.price}</span>
                </button>
              ))
            }
          </Async>
        </div>
      )}

      {step === 'slots' && (
        <div className="card">
          <Async state={slots} empty={<p className="muted">Свободных окон нет, напишите в чат.</p>}>
            {(rows) => (
              <>
                <div className="slots">
                  {rows.map((slot) => (
                    <button
                      key={slot.slot_key}
                      className={slotKey === slot.slot_key ? 'selected' : ''}
                      onClick={() => setSlotKey(slot.slot_key)}
                    >
                      {slot.date.slice(5).split('-').reverse().join('.')} {slot.time}
                      <br />
                      <span style={{ fontSize: 11 }}>{slot.doctor}</span>
                    </button>
                  ))}
                </div>
                <button
                  className="primary"
                  style={{ marginTop: 14 }}
                  disabled={!slotKey}
                  onClick={() => (needsForm ? setStep('form') : book())}
                >
                  Продолжить
                </button>
                <button style={{ marginTop: 8 }} onClick={() => setStep('services')}>
                  Назад
                </button>
              </>
            )}
          </Async>
        </div>
      )}

      {step === 'form' && (
        <div className="card">
          <div className="field">
            <label>Как вас записать</label>
            <input value={name} onChange={(event) => setName(event.target.value)} />
          </div>
          <div className="field">
            <label>Телефон</label>
            <input
              value={phone}
              onChange={(event) => setPhone(event.target.value)}
              placeholder="+7 701 000 00 00"
              inputMode="tel"
            />
          </div>
          <label style={{ display: 'flex', gap: 8, alignItems: 'flex-start', marginBottom: 12 }}>
            <input
              type="checkbox"
              style={{ width: 'auto', marginTop: 3 }}
              checked={consent}
              onChange={(event) => setConsent(event.target.checked)}
            />
            <span style={{ fontSize: 13 }}>
              Согласен на обработку персональных данных: имя и телефон — только для записи.
            </span>
          </label>
          <button className="primary" onClick={book} disabled={busy || !consent}>
            Записаться
          </button>
          {error && <p className="error">{error}</p>}
        </div>
      )}

      {step === 'done' && booked && (
        <div className="card">
          <h2>Записали вас</h2>
          <p>
            {booked.service}
            <br />
            {booked.when}
            <br />
            врач {booked.doctor}
          </p>
          {booked.address && <p className="muted">{booked.address}</p>}
          <button onClick={() => setStep('visits')}>Мои записи</button>
        </div>
      )}

      {step === 'visits' && (
        <div className="card">
          <Async state={visits} empty={<p className="muted">Активных записей нет.</p>}>
            {(rows) =>
              rows.map((visit) => (
                <div key={visit.visit_id} className="option">
                  <span>
                    {visit.service}
                    <br />
                    <span className="muted" style={{ fontSize: 12 }}>
                      {visit.when} · {visit.doctor}
                    </span>
                  </span>
                  {['booked', 'confirmed', 'hold'].includes(visit.status) && (
                    <button
                      className="small danger"
                      onClick={async () => {
                        await MiniApi.cancel(slug, initData, visit.visit_id);
                        visits.reload();
                      }}
                    >
                      Отменить
                    </button>
                  )}
                </div>
              ))
            }
          </Async>
        </div>
      )}
    </div>
  );
}
