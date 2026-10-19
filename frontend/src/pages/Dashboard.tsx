import { Api, auth } from '../api';
import { Async, Card, Tile, useAsync } from '../ui';

export default function Dashboard() {
  const tenantId = auth.tenantId!;
  const report = useAsync(() => Api.report(tenantId), [tenantId]);
  const funnel = useAsync(() => Api.funnel(tenantId, 7), [tenantId]);

  return (
    <>
      <div className="page-head">
        <h1>Сводка</h1>
        <p>Те же цифры, что владелец получает в Telegram в 08:00.</p>
      </div>

      <Async state={report}>
        {(data) => {
          const d = data.data as Record<string, number | null>;
          return (
            <>
              <div className="grid tiles" style={{ marginBottom: 16 }}>
                <Tile label="Обращений вчера" value={d.conversations ?? 0} />
                <Tile label="Записались" value={d.bookings ?? 0} />
                <Tile label="Из них ночью" value={d.bookings_at_night ?? 0} />
                <Tile
                  label="Первый ответ, сек"
                  value={d.first_response_p50 === null ? '—' : d.first_response_p50}
                />
                <Tile label="Неявок" value={d.no_shows ?? 0} />
                <Tile label="Открытых передач" value={d.open_handoffs ?? 0} />
              </div>
              <Card title="Утренний отчёт">
                <pre className="report">{data.text}</pre>
              </Card>
            </>
          );
        }}
      </Async>

      <div className="grid two">
        <Async state={funnel}>
          {(data) => (
            <Card title="Воронка за 7 дней">
              <table>
                <tbody>
                  {[
                    ['Обращения', 'message_in'],
                    ['Первый ответ', 'first_response'],
                    ['Интерес', 'interest'],
                    ['Записались', 'booking_confirmed'],
                    ['Пришли', 'visit_done'],
                    ['Неявки', 'visit_no_show'],
                    ['Отзывы', 'review_received'],
                    ['Дожим отправлен', 'followup_sent'],
                    ['Передачи человеку', 'handoff_created'],
                  ].map(([label, key]) => (
                    <tr key={key}>
                      <td>{label}</td>
                      <td style={{ textAlign: 'right', fontWeight: 600 }}>
                        {data.events[key] ?? 0}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )}
        </Async>

        <Async state={funnel}>
          {(data) => (
            <Card title="Причины передач человеку">
              {Object.keys(data.handoff_reasons).length === 0 ? (
                <p className="muted">За неделю передач не было.</p>
              ) : (
                <table>
                  <tbody>
                    {Object.entries(data.handoff_reasons)
                      .sort((a, b) => b[1] - a[1])
                      .map(([reason, count]) => (
                        <tr key={reason}>
                          <td>{REASONS[reason] || reason}</td>
                          <td style={{ textAlign: 'right', fontWeight: 600 }}>{count}</td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              )}
            </Card>
          )}
        </Async>
      </div>
    </>
  );
}

const REASONS: Record<string, string> = {
  patient_request: 'просят человека',
  red_flag: 'красный флаг',
  complaint: 'жалоба',
  discount: 'торг / скидка',
  refund: 'возврат денег',
  out_of_kb: 'вопрос вне базы',
  media: 'фото или документ',
  not_online_bookable: 'запись только через админа',
  confusion: 'бот не понял',
  other: 'другое',
};
