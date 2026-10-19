// Thin typed API client. One place knows about auth headers and error shapes.
const BASE = (import.meta.env.VITE_API_BASE || 'http://localhost:8000').replace(/\/$/, '');

const TOKEN_KEY = 'arix.token';
const TENANT_KEY = 'arix.tenant';
const ADMIN_KEY = 'arix.adminToken';

export const auth = {
  get token() {
    return localStorage.getItem(TOKEN_KEY);
  },
  get tenantId() {
    return localStorage.getItem(TENANT_KEY);
  },
  get adminToken() {
    return localStorage.getItem(ADMIN_KEY);
  },
  save(token: string, tenantId: string | null) {
    localStorage.setItem(TOKEN_KEY, token);
    if (tenantId) localStorage.setItem(TENANT_KEY, tenantId);
  },
  saveAdmin(token: string) {
    localStorage.setItem(ADMIN_KEY, token);
  },
  clear() {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(TENANT_KEY);
    localStorage.removeItem(ADMIN_KEY);
  },
  get isAuthenticated() {
    return Boolean(this.token || this.adminToken);
  },
};

export class ApiError extends Error {
  constructor(readonly status: number, message: string) {
    super(message);
  }
}

type Options = {
  method?: string;
  body?: unknown;
  initData?: string;
  query?: Record<string, string | number | boolean | undefined | null>;
};

export async function api<T>(path: string, options: Options = {}): Promise<T> {
  const url = new URL(BASE + path);
  for (const [key, value] of Object.entries(options.query || {})) {
    if (value !== undefined && value !== null && value !== '') {
      url.searchParams.set(key, String(value));
    }
  }

  const headers: Record<string, string> = { 'content-type': 'application/json' };
  if (options.initData) headers['x-telegram-init-data'] = options.initData;
  else if (auth.adminToken) headers['x-arix-admin-token'] = auth.adminToken;
  else if (auth.token) headers.authorization = `Bearer ${auth.token}`;

  const response = await fetch(url.toString(), {
    method: options.method || 'GET',
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });

  if (response.status === 401 && !options.initData) {
    auth.clear();
    throw new ApiError(401, 'Сессия истекла, войдите заново');
  }
  if (!response.ok) {
    let detail = `Ошибка ${response.status}`;
    try {
      const payload = await response.json();
      detail = typeof payload.detail === 'string' ? payload.detail : JSON.stringify(payload.detail);
    } catch {
      /* keep the generic message */
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

// ─── types ────────────────────────────────────────────────────────────────────
export type Service = {
  id: string;
  code: string;
  name: Record<string, string>;
  category: string | null;
  price_from: number | null;
  price_to: number | null;
  currency: string;
  duration_min: number;
  buffer_min: number;
  online_bookable: boolean;
  recall_days: number | null;
  active: boolean;
  needs_consultation_first: boolean;
  complaint_keywords: string[];
  sort: number;
};

export type Doctor = {
  id: string;
  name: string;
  specialty: string | null;
  langs: string[];
  bio_short: string | null;
  active: boolean;
};

export type Visit = {
  id: string;
  starts_at: string;
  ends_at: string;
  status: string;
  source: string;
  attribution: string;
  doctor: string;
  doctor_id: string;
  service: string;
  service_id: string;
  contact_id: string;
  contact_name: string | null;
  contact_phone: string | null;
};

export type Slot = {
  slot_key: string;
  start: string;
  end: string;
  doctor: string;
  doctor_id: string;
  service_id: string;
  service: string;
  time: string;
  date: string;
};

export type Conversation = {
  id: string;
  channel: string;
  state: string;
  assigned_to: string;
  last_inbound_at: string | null;
  contact: { id: string; name: string | null; phone: string | null; lang: string };
};

export type Message = {
  id: string;
  direction: 'in' | 'out';
  type: string;
  text: string | null;
  sender: string;
  created_at: string;
};

export type Handoff = {
  id: string;
  number: number;
  reason: string;
  priority: string;
  summary: string | null;
  conversation_id: string;
  created_at: string;
  taken_at: string | null;
  resolved_at: string | null;
};

export type KBItem = {
  id: string;
  type: string;
  question: string;
  answer: string;
  lang: string;
  verbatim: boolean;
  active: boolean;
};

export type DailyReport = { data: Record<string, unknown>; text: string };

export type Channel = {
  id: string;
  type: string;
  status: string;
  status_detail: string | null;
  display_name: string | null;
  settings: Record<string, unknown>;
  last_seen_at: string | null;
};

// ─── endpoints ────────────────────────────────────────────────────────────────
const panel = (tenantId: string) => `/panel/${tenantId}`;

export const Api = {
  login: (email: string, password: string) =>
    api<{ access_token: string; role: string; tenant_id: string | null }>('/auth/login', {
      method: 'POST',
      body: { email, password },
    }),
  tenants: () => api<{ id: string; slug: string; name: string; status: string }[]>('/admin/tenants'),
  report: (tenantId: string, day?: string) =>
    api<DailyReport>(`${panel(tenantId)}/report/daily`, { query: { day } }),
  funnel: (tenantId: string, days = 7) =>
    api<{ events: Record<string, number>; handoff_reasons: Record<string, number>; daily: Record<string, unknown>[] }>(
      `${panel(tenantId)}/stats/funnel`,
      { query: { days } },
    ),
  services: (tenantId: string) => api<Service[]>(`${panel(tenantId)}/services`),
  saveService: (tenantId: string, id: string, body: Partial<Service>) =>
    api<Service>(`${panel(tenantId)}/services/${id}`, { method: 'PUT', body }),
  doctors: (tenantId: string) => api<Doctor[]>(`${panel(tenantId)}/doctors`),
  visits: (tenantId: string, day: string, days = 1) =>
    api<Visit[]>(`${panel(tenantId)}/visits`, { query: { day, days } }),
  slots: (tenantId: string, serviceId: string, doctorId?: string, dateFrom?: string) =>
    api<Slot[]>(`${panel(tenantId)}/slots`, {
      query: { service_id: serviceId, doctor_id: doctorId, date_from: dateFrom, count: 24 },
    }),
  createVisit: (
    tenantId: string,
    body: {
      service_id: string;
      doctor_id: string;
      start: string;
      contact_name?: string;
      contact_phone?: string;
      note?: string;
      status?: string;
    },
  ) => api<Visit>(`${panel(tenantId)}/visits`, { method: 'POST', body }),
  setVisitStatus: (tenantId: string, id: string, status: string, reason?: string) =>
    api<{ id: string; status: string }>(`${panel(tenantId)}/visits/${id}/status`, {
      method: 'PATCH',
      body: { status, reason },
    }),
  conversations: (tenantId: string) => api<Conversation[]>(`${panel(tenantId)}/conversations`),
  messages: (tenantId: string, id: string) =>
    api<Message[]>(`${panel(tenantId)}/conversations/${id}/messages`),
  reply: (tenantId: string, id: string, text: string) =>
    api<{ ok: boolean; error: string | null }>(
      `${panel(tenantId)}/conversations/${id}/reply`,
      { method: 'POST', query: { text } },
    ),
  returnToBot: (tenantId: string, id: string) =>
    api<{ ok: boolean }>(`${panel(tenantId)}/conversations/${id}/return-to-bot`, {
      method: 'POST',
    }),
  handoffs: (tenantId: string) => api<Handoff[]>(`${panel(tenantId)}/handoffs`),
  kb: (tenantId: string) => api<KBItem[]>(`${panel(tenantId)}/kb`),
  saveKb: (tenantId: string, id: string, body: Partial<KBItem>) =>
    api<{ id: string }>(`${panel(tenantId)}/kb/${id}`, { method: 'PUT', body }),
  createKb: (tenantId: string, body: Partial<KBItem>) =>
    api<{ id: string }>(`${panel(tenantId)}/kb`, { method: 'POST', body }),
  deleteKb: (tenantId: string, id: string) =>
    api<void>(`${panel(tenantId)}/kb/${id}`, { method: 'DELETE' }),
  channels: (tenantId: string) => api<Channel[]>(`/admin/tenants/${tenantId}/channels`),
  connectTelegram: (tenantId: string, body: { bot_token?: string; business?: boolean }) =>
    api<Record<string, unknown>>(`/admin/tenants/${tenantId}/channels/telegram`, {
      method: 'POST',
      body: { ...body, set_webhook: true },
    }),
  connectWhatsApp: (tenantId: string) =>
    api<Record<string, unknown>>(`/admin/tenants/${tenantId}/channels/whatsapp`, {
      method: 'POST',
      body: { mode: 'baileys' },
    }),
  whatsappQr: (tenantId: string) =>
    api<{ status: string; qr: string | null; hint?: string }>(
      `/admin/tenants/${tenantId}/channels/whatsapp/qr`,
    ),
};

// ─── Mini App ─────────────────────────────────────────────────────────────────
export const MiniApi = {
  profile: (slug: string, initData: string) =>
    api<{
      tenant: {
        name: string;
        city: string | null;
        address: string | null;
        map_url: string | null;
        phone: string | null;
        currency: string;
        payments_enabled: boolean;
      };
      contact: { name: string | null; phone: string | null; consent_pd: boolean; lang: string };
    }>(`/miniapp/${slug}/profile`, { initData }),
  services: (slug: string, initData: string) =>
    api<
      {
        service_id: string;
        name: string;
        category: string | null;
        price: string;
        duration_min: number;
        online_bookable: boolean;
      }[]
    >(`/miniapp/${slug}/services`, { initData }),
  doctors: (slug: string, initData: string, serviceId?: string) =>
    api<{ doctor_id: string; name: string; specialty: string | null }[]>(
      `/miniapp/${slug}/doctors`,
      { initData, query: { service_id: serviceId } },
    ),
  slots: (slug: string, initData: string, serviceId: string, doctorId?: string) =>
    api<{ slot_key: string; doctor: string; date: string; time: string; when: string }[]>(
      `/miniapp/${slug}/slots`,
      { initData, query: { service_id: serviceId, doctor_id: doctorId, count: 24 } },
    ),
  book: (
    slug: string,
    initData: string,
    body: { slot_key: string; name?: string; phone?: string; consent_pd: boolean },
  ) =>
    api<{
      visit_id: string;
      when: string;
      doctor: string;
      service: string;
      address: string | null;
      map_url: string | null;
    }>(`/miniapp/${slug}/book`, { method: 'POST', initData, body }),
  myVisits: (slug: string, initData: string) =>
    api<
      {
        visit_id: string;
        when: string;
        doctor: string;
        service: string;
        status: string;
      }[]
    >(`/miniapp/${slug}/my/visits`, { initData }),
  cancel: (slug: string, initData: string, visitId: string) =>
    api<{ ok: boolean }>(`/miniapp/${slug}/my/visits/${visitId}/cancel`, {
      method: 'POST',
      initData,
    }),
};
