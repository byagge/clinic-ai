import { useMemo, useState } from 'react';

import { Api, auth, type Slot } from '../api';
import { Async, Card, StatusBadge, fmtTime, today, useAsync } from '../ui';

const STATUS_ACTIONS: [string, string][] = [
  ['arrived', 'Пришёл'],
  ['no_show', 'Не пришёл'],
  ['done', 'Завершён'],
  ['cancelled', 'Отменить'],
];

export default function Calendar() {
  const tenantId = auth.tenantId!;
  const [day, setDay] = useState(today());
  const visits = useAsync(() => Api.visits(tenantId, day), [tenantId, day]);
  const services = useAsync(() => Api.services(tenantId), [tenantId]);
  const doctors = useAsync(() => Api.doctors(tenantId), [tenantId]);

  return (
    <>
      <div className="page-head">
        <h1>Календарь</h1>
        <p>
          Телефонные записи добавляйте здесь — иначе бот предложит это время другому пациенту.
        </p>
      </div>

      <Card
        title={`Записи на ${day}`}
        actions={
          <input
            type="date"
            value={day}
            onChange={(event) => setDay(event.target.value)}
            style={{ width: 170 }}
          />
        }
      >
        <Async state={visits} empty={<p className="muted">На этот день записей нет.</p>}>
          {(rows) => (
            <table>
              <thead>
                <tr>
                  <th>Время</th>
                  <th>Пациент</th>
                  <th>Услуга</th>
                  <th>Врач</th>
                  <th>Статус</th>
                  <th>Источник</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((visit) => (
                  <tr key={visit.id}>
                    <td>
                      {fmtTime(visit.starts_at)}–{fmtTime(visit.ends_at)}
                    </td>
                    <td>
                      {visit.contact_name || 'без имени'}
                      {visit.contact_phone && (
                        <div className="muted" style={{ fontSize: 12 }}>
                          {visit.contact_phone}
                        </div>
                      )}
                    </td>
                    <td>{visit.service}</td>
                    <td>{visit.doctor}</td>
                    <td>
                      <StatusBadge status={visit.status} />
                    </td>
                    <td className="muted">{visit.attribution}</td>
                    <td style={{ whiteSpace: 'nowrap' }}>
                      {STATUS_ACTIONS.filter(([status]) => status !== visit.status).map(
                        ([status, label]) => (
                          <button
                            key={status}
                            className={`small ${status === 'cancelled' ? 'danger' : ''}`}
                            style={{ marginLeft: 4 }}
                            onClick={async () => {
                              await Api.setVisitStatus(tenantId, visit.id, status);
                              visits.reload();
                            }}
                          >
                            {label}
                          </button>
                        ),
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Async>
      </Card>

      {services.data && doctors.data && (
        <ManualBooking
          tenantId={tenantId}
          services={services.data}
          doctors={doctors.data}
          day={day}
          onBooked={visits.reload}
        />
      )}
    </>
  );
}

function ManualBooking({
  tenantId,
  services,
  doctors,
  day,
  onBooked,
}: {
  tenantId: string;
  services: { id: string; name: Record<string, string>; code: string }[];
  doctors: { id: string; name: string }[];
  day: string;
  onBooked: () => void;
}) {
  const [serviceId, setServiceId] = useState(services[0]?.id ?? '');
  const [doctorId, setDoctorId] = useState('');
  const [name, setName] = useState('');
  const [phone, setPhone] = useState('');
  const [note, setNote] = useState('');
  const [slot, setSlot] = useState<Slot | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const slots = useAsync(
    () => (serviceId ? Api.slots(tenantId, serviceId, doctorId || undefined, day) : Promise.resolve([])),
    [tenantId, serviceId, doctorId, day],
  );
  const sameDay = useMemo(
    () => (slots.data || []).filter((item) => item.date === day),
    [slots.data, day],
  );

  async function book() {
    if (!slot) return;
    setBusy(true);
    setMessage(null);
    try {
      await Api.createVisit(tenantId, {
        service_id: slot.service_id,
        doctor_id: slot.doctor_id,
        start: slot.start,
        contact_name: name || undefined,
        contact_phone: phone || undefined,
        note: note || undefined,
        status: 'confirmed',
      });
      setMessage('Запись добавлена.');
      setSlot(null);
      setName('');
      setPhone('');
      setNote('');
      slots.reload();
      onBooked();
    } catch (error) {
      setMessage((error as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Добавить запись с телефона">
      <div className="row">
        <div>
          <label>Услуга</label>
          <select value={serviceId} onChange={(event) => { setServiceId(event.target.value); setSlot(null); }}>
            {services.map((service) => (
              <option key={service.id} value={service.id}>
                {service.name.ru || service.code}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label>Врач</label>
          <select value={doctorId} onChange={(event) => { setDoctorId(event.target.value); setSlot(null); }}>
            <option value="">любой</option>
            {doctors.map((doctor) => (
              <option key={doctor.id} value={doctor.id}>
                {doctor.name}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label>Имя пациента</label>
          <input value={name} onChange={(event) => setName(event.target.value)} />
        </div>
        <div>
          <label>Телефон</label>
          <input
            value={phone}
            onChange={(event) => setPhone(event.target.value)}
            placeholder="+7 701 000 00 00"
          />
        </div>
      </div>

      <div className="field" style={{ marginTop: 12 }}>
        <label>Свободное время на {day}</label>
        <Async state={slots} empty={<p className="muted">Свободных окон на этот день нет.</p>}>
          {() =>
            sameDay.length === 0 ? (
              <p className="muted">Свободных окон на этот день нет.</p>
            ) : (
              <div className="slots">
                {sameDay.map((item) => (
                  <button
                    key={item.slot_key}
                    className={slot?.slot_key === item.slot_key ? 'selected' : ''}
                    onClick={() => setSlot(item)}
                  >
                    {item.time} · {item.doctor}
                  </button>
                ))}
              </div>
            )
          }
        </Async>
      </div>

      <div className="field">
        <label>Комментарий</label>
        <input value={note} onChange={(event) => setNote(event.target.value)} />
      </div>

      <button className="primary" onClick={book} disabled={!slot || busy}>
        {slot ? `Записать на ${slot.time}` : 'Выберите время'}
      </button>
      {message && <p className={message.includes('добавлена') ? 'muted' : 'error'}>{message}</p>}
    </Card>
  );
}
