// Configuration for the sidecar. Everything comes from the environment so the
// container is immutable.
export const config = {
  port: Number(process.env.PORT || 3000),
  // Shared bearer token the backend uses to call us.
  token: process.env.WA_BAILEYS_TOKEN || 'baileys-shared-token',
  // Where to POST inbound messages and connection events.
  backendUrl: (process.env.ARIX_BACKEND_URL || 'http://backend:8000').replace(/\/$/, ''),
  // HMAC secret the backend verifies on every delivery.
  webhookSecret: process.env.WA_WEBHOOK_SECRET || 'wa-webhook-secret',
  // Where multi-device credentials live. Mount a volume here.
  authDir: process.env.WA_AUTH_DIR || '/data/sessions',
  // Pacing: an unofficial client that blasts messages gets the number banned.
  minSendIntervalMs: Number(process.env.WA_MIN_SEND_INTERVAL_MS || 1200),
  maxSendPerMinute: Number(process.env.WA_MAX_SEND_PER_MINUTE || 20),
  // Typing indicator before each reply makes the clinic look human-paced.
  presenceDelayMs: Number(process.env.WA_PRESENCE_DELAY_MS || 600),
  logLevel: process.env.LOG_LEVEL || 'info',
};
