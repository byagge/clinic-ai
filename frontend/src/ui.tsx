import { type ReactNode, useEffect, useState } from 'react';

export function Tile({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="tile">
      <div className="value">{value}</div>
      <div className="label">{label}</div>
    </div>
  );
}

export function Card({ title, children, actions }: { title?: string; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="card">
      {(title || actions) && (
        <div className="page-head" style={{ marginBottom: 12 }}>
          {title && <h2 style={{ margin: 0 }}>{title}</h2>}
          <div style={{ marginLeft: 'auto' }}>{actions}</div>
        </div>
      )}
      {children}
    </section>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const tone =
    status === 'connected' || status === 'done' || status === 'arrived' || status === 'confirmed'
      ? 'ok'
      : status === 'error' || status === 'no_show' || status === 'cancelled'
        ? 'danger'
        : status === 'hold' || status === 'connecting' || status === 'booked'
          ? 'warn'
          : '';
  const label: Record<string, string> = {
    hold: 'бронь',
    booked: 'записан',
    confirmed: 'подтвердил',
    arrived: 'пришёл',
    done: 'завершён',
    no_show: 'не пришёл',
    cancelled: 'отменён',
    connected: 'подключён',
    connecting: 'подключается',
    error: 'ошибка',
    pending: 'ожидает',
    disabled: 'выключен',
  };
  return <span className={`badge ${tone}`}>{label[status] || status}</span>;
}

/** Loads data once per dependency change and reports loading/error state. */
export function useAsync<T>(loader: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    loader()
      .then((value) => {
        if (!cancelled) setData(value);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  return { data, error, loading, reload: () => setNonce((value) => value + 1) };
}

export function Async<T>({
  state,
  children,
  empty,
}: {
  state: { data: T | null; error: string | null; loading: boolean };
  children: (data: T) => ReactNode;
  empty?: ReactNode;
}) {
  if (state.loading && state.data === null) return <p className="muted">Загрузка…</p>;
  if (state.error) return <p className="error">{state.error}</p>;
  if (state.data === null) return <>{empty ?? <p className="muted">Нет данных</p>}</>;
  if (Array.isArray(state.data) && state.data.length === 0) {
    return <>{empty ?? <p className="muted">Пусто</p>}</>;
  }
  return <>{children(state.data)}</>;
}

export function fmtTime(iso: string): string {
  return new Date(iso).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}

export function fmtDateTime(iso: string): string {
  return new Date(iso).toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function today(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(
    now.getDate(),
  ).padStart(2, '0')}`;
}

export function money(value: number | null, currency = 'KZT'): string {
  if (value === null || value === undefined) return '—';
  const symbol = { KZT: '₸', UZS: 'сум', RUB: '₽' }[currency] || currency;
  return `${Math.round(value).toLocaleString('ru-RU')} ${symbol}`;
}
