// One WhatsApp Web session per tenant.
//
// Baileys speaks the multi-device protocol, so the clinic links its existing
// number once by scanning a QR code and there is no per-message fee and no
// template approval. The trade-offs we accept deliberately:
//   * it is an unofficial client, so we pace sends and never bulk-blast;
//   * credentials live on disk and must be backed up with the volume;
//   * a logout on the phone ends the session and the backend is told at once.
import { mkdir } from 'node:fs/promises';
import path from 'node:path';

import makeWASocket, {
  Browsers,
  DisconnectReason,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys';
import QRCode from 'qrcode';

import { config } from './config.js';
import { logger } from './logger.js';
import { deliver } from './webhook.js';

const sessions = new Map();

const TEXT_TYPES = ['conversation', 'extendedTextMessage'];

function extractText(message) {
  const content = message.message || {};
  if (content.conversation) return content.conversation;
  if (content.extendedTextMessage?.text) return content.extendedTextMessage.text;
  if (content.imageMessage?.caption) return content.imageMessage.caption;
  if (content.videoMessage?.caption) return content.videoMessage.caption;
  if (content.buttonsResponseMessage?.selectedDisplayText) {
    return content.buttonsResponseMessage.selectedDisplayText;
  }
  if (content.listResponseMessage?.title) return content.listResponseMessage.title;
  return null;
}

function classify(message) {
  const content = message.message || {};
  if (content.audioMessage) return 'voice';
  if (content.imageMessage) return 'image';
  if (content.documentMessage) return 'document';
  if (content.videoMessage) return 'video';
  if (content.locationMessage) return 'location';
  if (TEXT_TYPES.some((key) => content[key])) return 'text';
  return 'other';
}

class Session {
  constructor(slug) {
    this.slug = slug;
    this.socket = null;
    this.status = 'idle';
    this.qrDataUrl = null;
    this.phone = null;
    this.lastError = null;
    this.starting = false;
    // Send pacing state.
    this.lastSentAt = 0;
    this.sentTimestamps = [];
    this.queue = Promise.resolve();
  }

  get authPath() {
    return path.join(config.authDir, this.slug);
  }

  async start() {
    if (this.starting || this.status === 'connected') return this.describe();
    this.starting = true;
    try {
      await mkdir(this.authPath, { recursive: true });
      const { state, saveCreds } = await useMultiFileAuthState(this.authPath);
      const { version } = await fetchLatestBaileysVersion();

      this.socket = makeWASocket({
        version,
        auth: state,
        printQRInTerminal: false,
        browser: Browsers.appropriate('ARIX'),
        syncFullHistory: false,
        markOnlineOnConnect: false,
        logger: logger.child({ slug: this.slug, lib: 'baileys' }),
      });
      this.status = 'connecting';

      this.socket.ev.on('creds.update', saveCreds);
      this.socket.ev.on('connection.update', (update) => this.onConnection(update));
      this.socket.ev.on('messages.upsert', (event) => this.onMessages(event));
    } finally {
      this.starting = false;
    }
    return this.describe();
  }

  async onConnection(update) {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      this.qrDataUrl = await QRCode.toDataURL(qr);
      this.status = 'qr';
      await deliver(this.slug, { event: 'qr', state: 'qr', qr: this.qrDataUrl });
      logger.info({ slug: this.slug }, 'qr ready');
    }
    if (connection === 'open') {
      this.status = 'connected';
      this.qrDataUrl = null;
      this.phone = this.socket?.user?.id?.split(':')[0] || null;
      await deliver(this.slug, { event: 'connection.update', state: 'open', phone: this.phone });
      logger.info({ slug: this.slug, phone: this.phone }, 'connected');
    }
    if (connection === 'close') {
      const statusCode = lastDisconnect?.error?.output?.statusCode;
      const loggedOut = statusCode === DisconnectReason.loggedOut;
      this.status = loggedOut ? 'logged_out' : 'disconnected';
      this.lastError = lastDisconnect?.error ? String(lastDisconnect.error) : null;
      await deliver(this.slug, {
        event: loggedOut ? 'logout' : 'connection.update',
        state: loggedOut ? 'logout' : 'close',
        reason: this.lastError,
      });
      logger.warn({ slug: this.slug, statusCode, loggedOut }, 'connection closed');
      if (!loggedOut) {
        // Reconnect with a short backoff; the phone being offline is normal.
        setTimeout(() => this.start().catch(() => {}), 5000);
      }
    }
  }

  async onMessages(event) {
    if (event.type !== 'notify') return;
    const messages = [];
    for (const message of event.messages || []) {
      const jid = message.key?.remoteJid || '';
      if (!jid || jid === 'status@broadcast' || jid.endsWith('@g.us')) continue;
      messages.push({
        id: message.key.id,
        from: jid,
        fromMe: Boolean(message.key.fromMe),
        pushName: message.pushName || null,
        timestamp: Number(message.messageTimestamp) || Math.floor(Date.now() / 1000),
        type: classify(message),
        text: extractText(message),
        mimetype:
          message.message?.audioMessage?.mimetype ||
          message.message?.imageMessage?.mimetype ||
          message.message?.documentMessage?.mimetype ||
          null,
      });
    }
    if (messages.length === 0) return;
    await deliver(this.slug, { event: 'messages.upsert', messages });
  }

  async throttle() {
    const now = Date.now();
    this.sentTimestamps = this.sentTimestamps.filter((ts) => now - ts < 60_000);
    let wait = Math.max(0, this.lastSentAt + config.minSendIntervalMs - now);
    if (this.sentTimestamps.length >= config.maxSendPerMinute) {
      wait = Math.max(wait, 60_000 - (now - this.sentTimestamps[0]));
    }
    if (wait > 0) await new Promise((resolve) => setTimeout(resolve, wait));
    this.lastSentAt = Date.now();
    this.sentTimestamps.push(this.lastSentAt);
  }

  send(to, text) {
    // Serialise sends per session: WhatsApp Web dislikes bursts, and message
    // order inside one chat has to be preserved.
    const task = this.queue.then(() => this.doSend(to, text));
    this.queue = task.catch(() => {});
    return task;
  }

  async doSend(to, text) {
    if (this.status !== 'connected' || !this.socket) {
      const error = new Error(this.status === 'logged_out' ? 'logged_out' : 'not_connected');
      error.code = 409;
      throw error;
    }
    await this.throttle();
    const jid = to.includes('@') ? to : `${to.replace(/\D/g, '')}@s.whatsapp.net`;
    try {
      await this.socket.presenceSubscribe(jid);
      await this.socket.sendPresenceUpdate('composing', jid);
      await new Promise((resolve) => setTimeout(resolve, config.presenceDelayMs));
      await this.socket.sendPresenceUpdate('paused', jid);
    } catch {
      // Presence is cosmetic; never fail a reply because of it.
    }
    const result = await this.socket.sendMessage(jid, { text });
    return result?.key?.id || null;
  }

  async logout() {
    try {
      await this.socket?.logout();
    } catch (error) {
      logger.warn({ slug: this.slug, err: String(error) }, 'logout failed');
    }
    this.status = 'logged_out';
    this.socket = null;
  }

  describe() {
    return {
      slug: this.slug,
      status: this.status,
      phone: this.phone,
      error: this.lastError,
      hasQr: Boolean(this.qrDataUrl),
    };
  }
}

export function getSession(slug) {
  if (!sessions.has(slug)) sessions.set(slug, new Session(slug));
  return sessions.get(slug);
}

export function listSessions() {
  return [...sessions.values()].map((session) => session.describe());
}
