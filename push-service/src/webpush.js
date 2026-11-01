// Web push with no dependencies (27 Oct 2026): VAPID (RFC 8292) and aes128gcm message
// encryption (RFC 8291), using node:crypto. Apple, Google and Mozilla's push services all take
// this. iPhones only allow it for an app added to the home screen (iOS 16.4+), not in Safari.
import { createECDH, createCipheriv, generateKeyPairSync, createPrivateKey, hkdfSync, randomBytes, sign } from 'node:crypto';

const b64u = buf => Buffer.from(buf).toString('base64url');
const unb64u = s => Buffer.from(s, 'base64url');

/** A new VAPID key pair: { publicKey (base64url, 65-byte raw point), privateJwk }. Made once, kept in settings. */
export function makeVapidKeys() {
  const { privateKey } = generateKeyPairSync('ec', { namedCurve: 'P-256' });
  const jwk = privateKey.export({ format: 'jwk' });
  return { publicKey: b64u(Buffer.concat([Buffer.from([4]), unb64u(jwk.x), unb64u(jwk.y)])), privateJwk: jwk };
}

/** The signed token that tells the push service who's sending (ES256 JWT). */
function vapidAuth(endpoint, vapid, subject) {
  const aud = new URL(endpoint).origin;
  const head = b64u(JSON.stringify({ typ: 'JWT', alg: 'ES256' }));
  const claims = b64u(JSON.stringify({ aud, exp: Math.floor(Date.now() / 1000) + 12 * 3600, sub: subject }));
  const key = createPrivateKey({ key: vapid.privateJwk, format: 'jwk' });
  const sig = sign('sha256', Buffer.from(`${head}.${claims}`), { key, dsaEncoding: 'ieee-p1363' });
  return `vapid t=${head}.${claims}.${b64u(sig)}, k=${vapid.publicKey}`;
}

/** Encrypt a payload for one subscription (RFC 8291, single aes128gcm record). */
export function encrypt(payload, { p256dh, auth }, { serverEcdh = null, salt = randomBytes(16) } = {}) {
  const ecdh = serverEcdh || createECDH('prime256v1');
  if (!serverEcdh) ecdh.generateKeys();
  const asPub = ecdh.getPublicKey();
  const uaPub = unb64u(p256dh);
  const secret = ecdh.computeSecret(uaPub);
  const info = Buffer.concat([Buffer.from('WebPush: info\0'), uaPub, asPub]);
  const ikm = Buffer.from(hkdfSync('sha256', secret, unb64u(auth), info, 32));
  const cek = Buffer.from(hkdfSync('sha256', ikm, salt, Buffer.from('Content-Encoding: aes128gcm\0'), 16));
  const nonce = Buffer.from(hkdfSync('sha256', ikm, salt, Buffer.from('Content-Encoding: nonce\0'), 12));
  const c = createCipheriv('aes-128-gcm', cek, nonce);
  const body = Buffer.concat([c.update(Buffer.concat([Buffer.from(payload), Buffer.from([2])])), c.final(), c.getAuthTag()]);
  const rs = Buffer.alloc(4); rs.writeUInt32BE(4096);
  return Buffer.concat([salt, rs, Buffer.from([asPub.length]), asPub, body]);
}

/**
 * Send one push. Returns 'ok', 'gone' (the phone unsubscribed or the app was deleted: drop the
 * subscription) or throws for anything else.
 */
export async function sendPush(sub, message, { vapid, subject, fetchImpl = fetch, ttl = 24 * 3600, urgency = 'normal' }) {
  const r = await fetchImpl(sub.endpoint, {
    method: 'POST',
    headers: {
      Authorization: vapidAuth(sub.endpoint, vapid, subject), 'Content-Encoding': 'aes128gcm', 'Content-Type': 'application/octet-stream',
      TTL: String(ttl), Urgency: urgency,
    },
    body: encrypt(JSON.stringify(message), sub),
    signal: AbortSignal.timeout(10000),
  });
  if (r.status === 404 || r.status === 410) return 'gone';
  if (!r.ok) throw new Error(`push service said ${r.status} ${(await r.text().catch(() => '')).slice(0, 120)}`);
  return 'ok';
}
