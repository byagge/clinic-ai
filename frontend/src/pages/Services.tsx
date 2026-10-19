import { useState } from 'react';

import { Api, auth, type Service } from '../api';
import { Async, Card, money, useAsync } from '../ui';

export default function Services() {
  const tenantId = auth.tenantId!;
  const services = useAsync(() => Api.services(tenantId), [tenantId]);
  const [draft, setDraft] = useState<Record<string, Partial<Service>>>({});
  const [saved, setSaved] = useState<string | null>(null);

  function patch(id: string, change: Partial<Service>) {
    setDraft((current) => ({ ...current, [id]: { ...current[id], ...change } }));
  }

  async function save(service: Service) {
    const change = draft[service.id];
    if (!change) return;
    await Api.saveService(tenantId, service.id, { ...service, ...change });
    setDraft((current) => {
      const next = { ...current };
      delete next[service.id];
      return next;
    });
    setSaved(service.id);
    services.reload();
  }

  return (
    <>
      <div className="page-head">
        <h1>Прайс и услуги</h1>
        <p>
          Бот называет только эти цены. Пустая цена значит «уточню у администратора» — выдумывать
          он не станет.
        </p>
      </div>

      <Card>
        <Async state={services}>
          {(rows) => (
            <table>
              <thead>
                <tr>
                  <th>Услуга</th>
                  <th style={{ width: 110 }}>Цена от</th>
                  <th style={{ width: 110 }}>Цена до</th>
                  <th style={{ width: 80 }}>Мин</th>
                  <th style={{ width: 80 }}>Буфер</th>
                  <th style={{ width: 90 }}>Онлайн</th>
                  <th style={{ width: 90 }}>Recall</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((service) => {
                  const change = draft[service.id] || {};
                  const value = { ...service, ...change };
                  return (
                    <tr key={service.id}>
                      <td>
                        {service.name.ru || service.code}
                        <div className="muted" style={{ fontSize: 12 }}>
                          {service.category || '—'} · {service.code}
                        </div>
                      </td>
                      <td>
                        <input
                          type="number"
                          value={value.price_from ?? ''}
                          onChange={(event) =>
                            patch(service.id, {
                              price_from: event.target.value === '' ? null : Number(event.target.value),
                            })
                          }
                        />
                      </td>
                      <td>
                        <input
                          type="number"
                          value={value.price_to ?? ''}
                          onChange={(event) =>
                            patch(service.id, {
                              price_to: event.target.value === '' ? null : Number(event.target.value),
                            })
                          }
                        />
                      </td>
                      <td>
                        <input
                          type="number"
                          value={value.duration_min}
                          onChange={(event) =>
                            patch(service.id, { duration_min: Number(event.target.value) })
                          }
                        />
                      </td>
                      <td>
                        <input
                          type="number"
                          value={value.buffer_min}
                          onChange={(event) =>
                            patch(service.id, { buffer_min: Number(event.target.value) })
                          }
                        />
                      </td>
                      <td style={{ textAlign: 'center' }}>
                        <input
                          type="checkbox"
                          style={{ width: 'auto' }}
                          checked={value.online_bookable}
                          onChange={(event) =>
                            patch(service.id, { online_bookable: event.target.checked })
                          }
                        />
                      </td>
                      <td>
                        <input
                          type="number"
                          value={value.recall_days ?? ''}
                          onChange={(event) =>
                            patch(service.id, {
                              recall_days:
                                event.target.value === '' ? null : Number(event.target.value),
                            })
                          }
                        />
                      </td>
                      <td style={{ whiteSpace: 'nowrap' }}>
                        <button
                          className="small primary"
                          disabled={!draft[service.id]}
                          onClick={() => save(service)}
                        >
                          {saved === service.id && !draft[service.id] ? 'Сохранено' : 'Сохранить'}
                        </button>
                        <div className="muted" style={{ fontSize: 12, marginTop: 4 }}>
                          {money(service.price_from, service.currency)}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Async>
      </Card>
    </>
  );
}
