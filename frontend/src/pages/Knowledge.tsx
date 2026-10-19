import { useState } from 'react';

import { Api, auth, type KBItem } from '../api';
import { Async, Card, useAsync } from '../ui';

const TYPES = ['faq', 'policy', 'prep', 'address', 'other'];

export default function Knowledge() {
  const tenantId = auth.tenantId!;
  const items = useAsync(() => Api.kb(tenantId), [tenantId]);
  const [editing, setEditing] = useState<Partial<KBItem> | null>(null);
  const [busy, setBusy] = useState(false);

  async function save() {
    if (!editing?.question || !editing?.answer) return;
    setBusy(true);
    try {
      const body = {
        type: editing.type || 'faq',
        question: editing.question,
        answer: editing.answer,
        lang: editing.lang || 'ru',
        verbatim: Boolean(editing.verbatim),
        active: editing.active ?? true,
        tags: [],
      };
      if (editing.id) await Api.saveKb(tenantId, editing.id, body);
      else await Api.createKb(tenantId, body);
      setEditing(null);
      items.reload();
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <h1>База знаний</h1>
        <p>
          Бот отвечает только тем, что есть здесь. Чего нет — он передаёт администратору, а не
          придумывает.
        </p>
      </div>

      <Card
        title="Факты"
        actions={
          <button className="primary small" onClick={() => setEditing({ type: 'faq', lang: 'ru' })}>
            Добавить
          </button>
        }
      >
        <Async state={items}>
          {(rows) => (
            <table>
              <thead>
                <tr>
                  <th style={{ width: 90 }}>Тип</th>
                  <th>Вопрос</th>
                  <th>Ответ</th>
                  <th style={{ width: 90 }}>Дословно</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((item) => (
                  <tr key={item.id}>
                    <td className="muted">{item.type}</td>
                    <td>{item.question}</td>
                    <td className="muted">{item.answer}</td>
                    <td>{item.verbatim ? 'да' : '—'}</td>
                    <td style={{ whiteSpace: 'nowrap' }}>
                      <button className="small" onClick={() => setEditing(item)}>
                        Изменить
                      </button>
                      <button
                        className="small danger"
                        style={{ marginLeft: 4 }}
                        onClick={async () => {
                          await Api.deleteKb(tenantId, item.id);
                          items.reload();
                        }}
                      >
                        Удалить
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Async>
      </Card>

      {editing && (
        <Card title={editing.id ? 'Изменить факт' : 'Новый факт'}>
          <div className="row">
            <div>
              <label>Тип</label>
              <select
                value={editing.type || 'faq'}
                onChange={(event) => setEditing({ ...editing, type: event.target.value })}
              >
                {TYPES.map((type) => (
                  <option key={type} value={type}>
                    {type}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label>Язык</label>
              <select
                value={editing.lang || 'ru'}
                onChange={(event) => setEditing({ ...editing, lang: event.target.value })}
              >
                {['ru', 'kz', 'uz'].map((lang) => (
                  <option key={lang} value={lang}>
                    {lang}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <div className="field">
            <label>Вопрос пациента</label>
            <input
              value={editing.question || ''}
              onChange={(event) => setEditing({ ...editing, question: event.target.value })}
            />
          </div>
          <div className="field">
            <label>Ответ</label>
            <textarea
              rows={3}
              value={editing.answer || ''}
              onChange={(event) => setEditing({ ...editing, answer: event.target.value })}
            />
          </div>
          <label style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 12 }}>
            <input
              type="checkbox"
              style={{ width: 'auto' }}
              checked={Boolean(editing.verbatim)}
              onChange={(event) => setEditing({ ...editing, verbatim: event.target.checked })}
            />
            Цитировать дословно (условия возврата, политика отмены)
          </label>
          <button className="primary" onClick={save} disabled={busy}>
            Сохранить
          </button>
          <button style={{ marginLeft: 8 }} onClick={() => setEditing(null)}>
            Отмена
          </button>
        </Card>
      )}
    </>
  );
}
