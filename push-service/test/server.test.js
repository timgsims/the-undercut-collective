// The push service end to end: a pretend phone subscribes through the public API, the
// pipeline sends through /internal/send, and the pretend push service checks the VAPID
// signature and decrypts what arrives. The phone helper is the one Pit Wall's tests use.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createECDH, createDecipheriv, createPublicKey, hkdfSync, randomBytes, verify } from 'node:crypto';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { openDb, createApp } from '../src/server.js';

const b64u = b => Buffer.from(b).toString('base64url');
const ORIGIN = 'https://f1fantasy.simsey.co.nz';

function phone() {
  const ecdh = createECDH('prime256v1'); ecdh.generateKeys();
  const authSecret = randomBytes(16);
  const keys = { p256dh: b64u(ecdh.getPublicKey()), auth: b64u(authSecret) };
  function decrypt(body) {
    const salt = body.subarray(0, 16), idlen = body[20], asPub = body.subarray(21, 21 + idlen), ct = body.subarray(21 + idlen);
    const secret = ecdh.computeSecret(asPub);
    const info = Buffer.concat([Buffer.from('WebPush: info\0'), ecdh.getPublicKey(), asPub]);
    const ikm = Buffer.from(hkdfSync('sha256', secret, authSecret, info, 32));
    const cek = Buffer.from(hkdfSync('sha256', ikm, salt, Buffer.from('Content-Encoding: aes128gcm\0'), 16));
    const nonce = Buffer.from(hkdfSync('sha256', ikm, salt, Buffer.from('Content-Encoding: nonce\0'), 12));
    const d = createDecipheriv('aes-128-gcm', cek, nonce);
    d.setAuthTag(ct.subarray(ct.length - 16));
    const plain = Buffer.concat([d.update(ct.subarray(0, ct.length - 16)), d.final()]);
    return JSON.parse(plain.subarray(0, plain.length - 1).toString());
  }
  return { keys, decrypt };
}

function checkVapid(header, endpoint, publicKey) {
  const m = /^vapid t=([^,]+), k=(.+)$/.exec(header);
  assert.ok(m);
  assert.equal(m[2], publicKey);
  const [h, c, sig] = m[1].split('.');
  const raw = Buffer.from(publicKey, 'base64url');
  const key = createPublicKey({ key: { kty: 'EC', crv: 'P-256', x: b64u(raw.subarray(1, 33)), y: b64u(raw.subarray(33)) }, format: 'jwk' });
  assert.ok(verify('sha256', Buffer.from(`${h}.${c}`), { key, dsaEncoding: 'ieee-p1363' }, Buffer.from(sig, 'base64url')));
}

async function setup() {
  const db = openDb(mkdtempSync(join(tmpdir(), 'ucpush-')));
  const inbox = [];   // what the pretend push service received
  const fetchImpl = async (url, opts) => {
    inbox.push({ url, opts });
    return { ok: true, status: 201, text: async () => '' };
  };
  const app = createApp({ db, origin: ORIGIN, internalToken: 'secret', subject: ORIGIN, fetchImpl });
  await new Promise(r => app.listen(0, r));
  const base = `http://127.0.0.1:${app.address().port}`;
  const call = (path, body, headers = {}) => fetch(base + path, {
    method: body ? 'POST' : 'GET', headers: { 'Content-Type': 'application/json', Origin: ORIGIN, ...headers },
    body: body ? JSON.stringify(body) : undefined,
  });
  return { app, inbox, call };
}

test('subscribe, choose a manager, receive only matching messages', async () => {
  const { app, inbox, call } = await setup();
  try {
    const { key } = await (await call('/api/key')).json();
    const tim = phone(), anon = phone();
    const subTim = { endpoint: 'https://push.example/tim', keys: tim.keys };
    const subAnon = { endpoint: 'https://push.example/anon', keys: anon.keys };
    assert.equal((await call('/api/subscribe', { subscription: subTim, manager: 'Tim', prefs: { prices: false }, device: 'iPhone' })).status, 200);
    assert.equal((await call('/api/subscribe', { subscription: subAnon, manager: '', prefs: {} })).status, 200);

    const st = await (await call('/api/status', { endpoint: subTim.endpoint })).json();
    assert.equal(st.manager, 'Tim');
    assert.equal(st.prefs.prices, false);
    assert.equal(st.prefs.lock, true);
    assert.equal(st.prefs.final, false);   // spoilers start off

    // no token, or through the tunnel: refused
    assert.equal((await call('/internal/send', { messages: [] })).status, 403);
    assert.equal((await call('/internal/send', { messages: [] }, { Authorization: 'Bearer secret', 'cf-ray': 'x' })).status, 404);

    const r = await (await call('/internal/send', { messages: [
      { type: 'lock', manager: 'Tim', title: 'Tim: team locks soon', body: '2 free transfers' },
      { type: 'lock', manager: null, title: 'Team locks soon', body: 'Brazil' },
      { type: 'prices', manager: 'Tim', title: 'Prices are out', body: '+0.4' },   // Tim turned prices off
      { type: 'final', manager: 'Dan', title: 'Dan: Mexico is final' },              // nobody picked Dan
      { type: 'final', manager: null, title: 'Mexico is final' },                    // spoiler, not chosen
    ] }, { Authorization: 'Bearer secret' })).json();
    assert.equal(r.sent, 2);
    assert.equal(inbox.length, 2);
    const byUrl = Object.fromEntries(inbox.map(x => [x.url, x.opts]));
    checkVapid(byUrl[subTim.endpoint].headers.Authorization, subTim.endpoint, key);
    assert.equal(tim.decrypt(byUrl[subTim.endpoint].body).title, 'Tim: team locks soon');
    assert.equal(anon.decrypt(byUrl[subAnon.endpoint].body).title, 'Team locks soon');

    // turning it off removes the device
    await call('/api/unsubscribe', { endpoint: subTim.endpoint });
    assert.equal((await (await call('/api/status', { endpoint: subTim.endpoint })).json()).on, false);
  } finally { app.close(); }
});

test('only the dashboard may call the public API from a browser', async () => {
  const { app, call } = await setup();
  try {
    const ok = await call('/api/key');
    assert.equal(ok.headers.get('access-control-allow-origin'), ORIGIN);
    const other = await call('/api/key', null, { Origin: 'https://evil.example' });
    assert.equal(other.headers.get('access-control-allow-origin'), null);
  } finally { app.close(); }
});

test('a phone that removed the app is forgotten', async () => {
  const db = openDb(mkdtempSync(join(tmpdir(), 'ucpush-')));
  const app = createApp({ db, origin: ORIGIN, internalToken: 't', subject: ORIGIN,
    fetchImpl: async () => ({ ok: false, status: 410, text: async () => '' }) });
  await new Promise(r => app.listen(0, r));
  const base = `http://127.0.0.1:${app.address().port}`;
  try {
    const p = phone();
    await fetch(base + '/api/subscribe', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ subscription: { endpoint: 'https://push.example/x', keys: p.keys }, manager: null }) });
    await fetch(base + '/internal/send', { method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer t' },
      body: JSON.stringify({ messages: [{ type: 'lock', manager: null, title: 'x' }] }) });
    assert.equal(db.prepare('SELECT COUNT(*) n FROM subs').get().n, 0);
  } finally { app.close(); }
});
