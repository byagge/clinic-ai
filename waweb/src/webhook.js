import { createHmac } from 'node:crypto';
import { config } from './config.js';
import { logger } from './logger.js';

// Deliver an event to the backend, signed so it cannot be forged.
export async function deliver(tenantSlug, payload) {
  const body = JSON.stringify(payload);
  const signature = createHmac('sha256', config.webhookSecret).update(body).digest('hex');
  const url = `${config.backendUrl}/webhooks/wa/baileys/${encodeURIComponent(tenantSlug)}`;

  // Retry a few times: the backend may be mid-restart and a lost patient
  // message is worse than a duplicate (the backend deduplicates by message id).
  for (let attempt = 1; attempt <= 4; attempt += 1) {
    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'content-type': 'application/json', 'x-arix-signature': signature },
        body,
        signal: AbortSignal.timeout(15000),
      });
      if (response.ok) return true;
      logger.warn({ status: response.status, attempt }, 'webhook rejected');
    } catch (error) {
      logger.warn({ err: String(error), attempt }, 'webhook failed');
    }
    await new Promise((resolve) => setTimeout(resolve, 2 ** attempt * 500));
  }
  return false;
}
