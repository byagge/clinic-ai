// REST surface used by the ARIX backend.
import express from 'express';

import { config } from './config.js';
import { logger } from './logger.js';
import { getSession, listSessions } from './session.js';

const app = express();
app.use(express.json({ limit: '1mb' }));

app.get('/health', (_req, res) => {
  res.json({ ok: true, sessions: listSessions() });
});

// Everything below requires the shared bearer token.
app.use((req, res, next) => {
  const header = req.headers.authorization || '';
  if (header !== `Bearer ${config.token}`) {
    return res.status(401).json({ ok: false, error: 'unauthorized' });
  }
  return next();
});

app.post('/sessions/:slug/start', async (req, res) => {
  try {
    const session = getSession(req.params.slug);
    res.json({ ok: true, ...(await session.start()) });
  } catch (error) {
    logger.error({ err: String(error) }, 'start failed');
    res.status(500).json({ ok: false, error: String(error) });
  }
});

app.get('/sessions/:slug', (req, res) => {
  res.json({ ok: true, ...getSession(req.params.slug).describe() });
});

app.get('/sessions/:slug/qr', async (req, res) => {
  const session = getSession(req.params.slug);
  if (session.status === 'idle') await session.start();
  res.json({
    ok: true,
    status: session.status,
    qr: session.qrDataUrl,
    phone: session.phone,
    hint:
      session.status === 'connected'
        ? 'Номер уже подключён.'
        : 'WhatsApp на телефоне → Настройки → Связанные устройства → Привязать устройство.',
  });
});

app.post('/sessions/:slug/logout', async (req, res) => {
  await getSession(req.params.slug).logout();
  res.json({ ok: true });
});

app.post('/sessions/:slug/messages', async (req, res) => {
  const { to, text } = req.body || {};
  if (!to || !text) return res.status(400).json({ ok: false, error: 'to and text are required' });
  try {
    const id = await getSession(req.params.slug).send(to, text);
    return res.json({ ok: true, id });
  } catch (error) {
    const status = error.code === 409 ? 409 : 500;
    return res.status(status).json({ ok: false, error: error.message || String(error) });
  }
});

app.listen(config.port, () => {
  logger.info({ port: config.port, backend: config.backendUrl }, 'waweb listening');
});

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => {
    logger.info({ signal }, 'shutting down');
    process.exit(0);
  });
}
