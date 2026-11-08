// Push notifications for The Undercut Collective (1 Nov 2026).
//
// The dashboard is a static page on GitHub Pages, so this small service holds
// the phones' push subscriptions and sends the messages. It runs on
// mako-sentinel as container `undercut-push`, reached by the site at
// https://f1push.simsey.co.nz (Cloudflare tunnel, no Access: league members
// aren't on Access). notify.py in the pipeline decides what to send and posts
// it to /internal/send on the loopback port. No dependencies: node:http,
// node:sqlite, and webpush.js (copied from Pit Wall, where it's proven on Tim's
// iPhone).
//
// Public API (CORS: the dashboard's origin only). A device is identified by
// its push endpoint, which is long and unguessable:
//   GET  /api/key                         VAPID public key
//   POST /api/subscribe   {subscription, manager, prefs, device}
//   POST /api/status      {endpoint}      -> {on, manager, prefs}
//   POST /api/prefs       {endpoint, manager, prefs}
//   POST /api/unsubscribe {endpoint}
//   POST /api/test        {endpoint}      one test message to that device
// Internal (Authorization: Bearer $INTERNAL_TOKEN, never through the tunnel):
//   POST /internal/send   {messages: [{type, manager|null, title, body, url, tag}]}
//     A message with a manager goes to devices that picked that manager; one
//     with manager null to devices that picked none. Only to devices with
//     that type switched on.
import http from 'node:http';
import { DatabaseSync } from 'node:sqlite';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { makeVapidKeys, sendPush } from './webpush.js';

export const TYPES = ['lock', 'fp1', 'sim', 'penalty', 'quali', 'final', 'prices', 'inactive', 'chips'];
// These give away results, so a device gets them only once it switches them on (Tim, 1 Nov 2026).
const OFF_UNLESS_CHOSEN = new Set(['quali', 'final']);

export function openDb(dir) {
  mkdirSync(dir, { recursive: true });
  const db = new DatabaseSync(join(dir, 'push.db'));
  db.exec(`
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS subs (
      endpoint TEXT PRIMARY KEY, p256dh TEXT NOT NULL, auth TEXT NOT NULL,
      manager TEXT, prefs TEXT NOT NULL, device TEXT,
      created TEXT NOT NULL, last_ok TEXT, fails INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS sent (
      at TEXT NOT NULL, type TEXT, manager TEXT, title TEXT, devices INTEGER, failed INTEGER);
  `);
  return db;
}

/** The VAPID key pair, made on first start. Never change it: every phone's subscription is tied to it. */
function vapidKeys(db) {
  const row = db.prepare("SELECT value FROM settings WHERE key = 'vapid'").get();
  if (row) return JSON.parse(row.value);
  const v = makeVapidKeys();
  db.prepare("INSERT INTO settings VALUES ('vapid', ?)").run(JSON.stringify(v));
  return v;
}

const now = () => new Date().toISOString();

function cleanPrefs(p) {
  const out = {};
  for (const t of TYPES) out[t] = p && typeof p[t] === 'boolean' ? p[t] : !OFF_UNLESS_CHOSEN.has(t);
  return out;
}

function cleanManager(m) {
  return typeof m === 'string' && m.trim() ? m.trim().slice(0, 40) : null;
}

export function createApp({ db, origin, internalToken, subject, fetchImpl = fetch }) {
  const vapid = vapidKeys(db);
  const getSub = db.prepare('SELECT * FROM subs WHERE endpoint = ?');
  const recent = new Map();   // endpoint -> last test time, to stop test spam

  async function deliver(sub, message) {
    try {
      const r = await sendPush(sub, message, { vapid, subject, fetchImpl });
      if (r === 'gone') { db.prepare('DELETE FROM subs WHERE endpoint = ?').run(sub.endpoint); return false; }
      db.prepare('UPDATE subs SET last_ok = ?, fails = 0 WHERE endpoint = ?').run(now(), sub.endpoint);
      return true;
    } catch (err) {
      db.prepare('UPDATE subs SET fails = fails + 1 WHERE endpoint = ?').run(sub.endpoint);
      console.error(`push to ${sub.device || 'device'} failed: ${err.message}`);
      return false;
    }
  }

  async function sendAll(messages) {
    const subs = db.prepare('SELECT * FROM subs').all();
    let sent = 0, failed = 0;
    for (const m of messages) {
      if (!TYPES.includes(m.type) || !m.title) continue;
      const to = subs.filter(s => (s.manager || null) === (m.manager || null) && JSON.parse(s.prefs)[m.type] !== false);
      let ok = 0;
      for (const s of to) (await deliver(s, { title: m.title, body: m.body || '', url: m.url || '/', tag: m.tag || m.type })) ? ok++ : failed++;
      sent += ok;
      db.prepare('INSERT INTO sent VALUES (?,?,?,?,?,?)').run(now(), m.type, m.manager || null, m.title, ok, to.length - ok);
    }
    return { sent, failed };
  }

  function cors(req, res) {
    if (req.headers.origin === origin) {
      res.setHeader('Access-Control-Allow-Origin', origin);
      res.setHeader('Vary', 'Origin');
      res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
      res.setHeader('Access-Control-Allow-Headers', 'Content-Type');
      res.setHeader('Access-Control-Max-Age', '86400');
    }
  }

  function send(res, status, body) {
    res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
    res.end(JSON.stringify(body));
  }

  function readJson(req) {
    return new Promise((resolve, reject) => {
      let size = 0; const chunks = [];
      req.on('data', c => { size += c.length; if (size > 64 * 1024) { reject(new Error('too big')); req.destroy(); } else chunks.push(c); });
      req.on('end', () => { try { resolve(chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : {}); } catch { reject(new Error('bad json')); } });
      req.on('error', reject);
    });
  }

  return http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://x');
    try {
      if (url.pathname === '/healthz') return send(res, 200, { ok: true });

      if (url.pathname.startsWith('/internal/')) {
        // Only from the pipeline on Sentinel itself: never through the tunnel.
        if (req.headers['cf-connecting-ip'] || req.headers['cf-ray']) return send(res, 404, { error: 'not found' });
        if (!internalToken || req.headers.authorization !== `Bearer ${internalToken}`) return send(res, 403, { error: 'no' });
        if (req.method === 'POST' && url.pathname === '/internal/send') {
          const { messages } = await readJson(req);
          return send(res, 200, await sendAll(Array.isArray(messages) ? messages : []));
        }
        if (req.method === 'GET' && url.pathname === '/internal/stats') {
          const subs = db.prepare('SELECT manager, device, prefs, created, last_ok, fails FROM subs').all();
          const log = db.prepare('SELECT * FROM sent ORDER BY at DESC LIMIT 30').all();
          return send(res, 200, { subs, log });
        }
        return send(res, 404, { error: 'not found' });
      }

      cors(req, res);
      if (req.method === 'OPTIONS') { res.writeHead(204); return res.end(); }
      if (!url.pathname.startsWith('/api/')) return send(res, 404, { error: 'not found' });
      if (req.method === 'GET' && url.pathname === '/api/key') return send(res, 200, { key: vapid.publicKey, types: TYPES });
      if (req.method !== 'POST') return send(res, 405, { error: 'POST only' });
      const b = await readJson(req);

      if (url.pathname === '/api/subscribe') {
        const s = b.subscription || {};
        const keys = s.keys || {};
        if (typeof s.endpoint !== 'string' || !/^https:\/\//.test(s.endpoint) || !keys.p256dh || !keys.auth)
          return send(res, 400, { error: 'That subscription looks wrong.' });
        db.prepare(`INSERT INTO subs (endpoint, p256dh, auth, manager, prefs, device, created) VALUES (?,?,?,?,?,?,?)
          ON CONFLICT(endpoint) DO UPDATE SET p256dh=excluded.p256dh, auth=excluded.auth, manager=excluded.manager,
          prefs=excluded.prefs, device=excluded.device, fails=0`)
          .run(s.endpoint, keys.p256dh, keys.auth, cleanManager(b.manager), JSON.stringify(cleanPrefs(b.prefs)),
               String(b.device || '').slice(0, 40), now());
        return send(res, 200, { ok: true });
      }
      const sub = typeof b.endpoint === 'string' ? getSub.get(b.endpoint) : null;
      if (url.pathname === '/api/status') {
        return send(res, 200, sub ? { on: true, manager: sub.manager, prefs: JSON.parse(sub.prefs) } : { on: false });
      }
      if (!sub) return send(res, 404, { error: 'Notifications aren\'t on for this device.' });
      if (url.pathname === '/api/prefs') {
        db.prepare('UPDATE subs SET manager = ?, prefs = ? WHERE endpoint = ?')
          .run(cleanManager(b.manager), JSON.stringify(cleanPrefs(b.prefs)), sub.endpoint);
        return send(res, 200, { ok: true });
      }
      if (url.pathname === '/api/unsubscribe') {
        db.prepare('DELETE FROM subs WHERE endpoint = ?').run(sub.endpoint);
        return send(res, 200, { ok: true });
      }
      if (url.pathname === '/api/test') {
        const last = recent.get(sub.endpoint) || 0;
        if (Date.now() - last < 20000) return send(res, 429, { error: 'Give it a few seconds between tests.' });
        recent.set(sub.endpoint, Date.now());
        const ok = await deliver(sub, { title: 'The Undercut Collective', body: 'Notifications are working on this device. 🏁', url: '/', tag: 'test' });
        return ok ? send(res, 200, { ok: true }) : send(res, 502, { error: 'The push service didn\'t take it. Try turning notifications off and on.' });
      }
      return send(res, 404, { error: 'not found' });
    } catch (err) {
      return send(res, 400, { error: err.message });
    }
  });
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const db = openDb(process.env.DATA_DIR || './data');
  const app = createApp({
    db,
    origin: process.env.ALLOWED_ORIGIN || 'https://f1fantasy.simsey.co.nz',
    internalToken: process.env.INTERNAL_TOKEN,
    subject: process.env.APP_URL || 'https://f1fantasy.simsey.co.nz',
  });
  const port = Number(process.env.PORT || 3000);
  app.listen(port, () => console.log(`undercut-push on ${port}`));
}
