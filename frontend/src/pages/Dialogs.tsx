import { useState } from 'react';

import { Api, auth } from '../api';
import { Async, Card, StatusBadge, fmtDateTime, useAsync } from '../ui';

export default function Dialogs() {
  const tenantId = auth.tenantId!;
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);

  const conversations = useAsync(() => Api.conversations(tenantId), [tenantId]);
  const handoffs = useAsync(() => Api.handoffs(tenantId), [tenantId]);
  const messages = useAsync(
    () => (selected ? Api.messages(tenantId, selected) : Promise.resolve([])),
    [tenantId, selected],
  );

  async function send() {
    if (!selected || !draft.trim()) return;
    setBusy(true);
    try {
      await Api.reply(tenantId, selected, draft.trim());
      setDraft('');
      messages.reload();
      conversations.reload();
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="page-head">
        <h1>Диалоги</h1>
        <p>Как только вы отвечаете сами, бот замолкает в этом диалоге.</p>
      </div>

      <Async state={handoffs} empty={null}>
        {(rows) =>
          rows.length === 0 ? null : (
            <Card title={`Передачи человеку (${rows.length})`}>
              <table>
                <tbody>
                  {rows.map((handoff) => (
                    <tr
                      key={handoff.id}
                      className="clickable"
                      onClick={() => setSelected(handoff.conversation_id)}
                    >
                      <td style={{ width: 60 }}>#{handoff.number}</td>
                      <td>
                        <span className={`badge ${handoff.priority === 'urgent' ? 'danger' : 'warn'}`}>
                          {handoff.reason}
                        </span>
                      </td>
                      <td>{handoff.summary || '—'}</td>
                      <td className="muted" style={{ whiteSpace: 'nowrap' }}>
                        {fmtDateTime(handoff.created_at)}
                      </td>
                      <td>{handoff.taken_at ? 'взята' : 'ждёт'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )
        }
      </Async>

      <div className="grid two">
        <Card title="Все диалоги">
          <Async state={conversations}>
            {(rows) => (
              <table>
                <tbody>
                  {rows.map((conversation) => (
                    <tr
                      key={conversation.id}
                      className="clickable"
                      onClick={() => setSelected(conversation.id)}
                      style={
                        selected === conversation.id
                          ? { outline: '2px solid var(--accent)', outlineOffset: -2 }
                          : undefined
                      }
                    >
                      <td>
                        {conversation.contact.name || 'без имени'}
                        <div className="muted" style={{ fontSize: 12 }}>
                          {conversation.contact.phone || conversation.channel.toUpperCase()}
                        </div>
                      </td>
                      <td>
                        <StatusBadge status={conversation.state} />
                        <div className="muted" style={{ fontSize: 12 }}>
                          {conversation.assigned_to === 'bot' ? 'бот' : 'человек'}
                        </div>
                      </td>
                      <td className="muted" style={{ whiteSpace: 'nowrap', fontSize: 12 }}>
                        {conversation.last_inbound_at
                          ? fmtDateTime(conversation.last_inbound_at)
                          : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Async>
        </Card>

        <Card
          title="Переписка"
          actions={
            selected && (
              <button
                className="small"
                onClick={async () => {
                  await Api.returnToBot(tenantId, selected);
                  conversations.reload();
                }}
              >
                Вернуть боту
              </button>
            )
          }
        >
          {!selected ? (
            <p className="muted">Выберите диалог слева.</p>
          ) : (
            <>
              <Async state={messages} empty={<p className="muted">Сообщений пока нет.</p>}>
                {(rows) => (
                  <div className="chat">
                    {rows.map((message) => (
                      <div key={message.id} className={`bubble ${message.direction}`}>
                        {message.text || `[${message.type}]`}
                        <span className="meta">
                          {message.sender} · {fmtDateTime(message.created_at)}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </Async>
              <div className="row" style={{ marginTop: 12 }}>
                <input
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' && !event.shiftKey) {
                      event.preventDefault();
                      void send();
                    }
                  }}
                  placeholder="Ответить пациенту…"
                />
                <button className="primary shrink" onClick={send} disabled={busy || !draft.trim()}>
                  Отправить
                </button>
              </div>
            </>
          )}
        </Card>
      </div>
    </>
  );
}
