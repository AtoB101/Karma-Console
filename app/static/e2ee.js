/* KramaE2EE: client-side encryption helpers for the owner-only E2EE vault (P42).
 *
 * The server stores only the ciphertext envelope; all key derivation, encryption
 * and decryption happen here in the browser. The server never receives the key.
 *
 * Pure crypto (derive/encrypt/decrypt/blind index) is environment-independent and
 * unit-tested under Node. WebAuthn/Passkey helpers require a real platform
 * authenticator (Windows Hello / Touch ID) and are guarded by feature checks.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.KramaE2EE = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const subtle = (globalThis.crypto && globalThis.crypto.subtle) || null;

  function b64encode(bytes) {
    if (typeof Buffer !== "undefined") return Buffer.from(bytes).toString("base64");
    let bin = "";
    bytes.forEach((b) => (bin += String.fromCharCode(b)));
    return btoa(bin);
  }

  function b64decode(str) {
    if (typeof Buffer !== "undefined") return new Uint8Array(Buffer.from(str, "base64"));
    const bin = atob(str);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  function utf8Encode(s) {
    if (typeof TextEncoder !== "undefined") return new TextEncoder().encode(s);
    return Uint8Array.from(unescape(encodeURIComponent(s)), (c) => c.charCodeAt(0));
  }

  function utf8Decode(bytes) {
    if (typeof TextDecoder !== "undefined") return new TextDecoder().decode(bytes);
    return decodeURIComponent(escape(String.fromCharCode(...bytes)));
  }

  function requireSubtle() {
    if (!subtle) throw new Error("WebCrypto subtle is unavailable in this environment");
  }

  /** Derive an AES-GCM 256 key from a password + salt via PBKDF2. */
  async function deriveKeyFromPassword(password, saltB64, iterations = 200_000) {
    requireSubtle();
    const base = await subtle.importKey(
      "raw", utf8Encode(password), "PBKDF2", false, ["deriveKey"]
    );
    return subtle.deriveKey(
      { name: "PBKDF2", salt: b64decode(saltB64), iterations, hash: "SHA-256" },
      base,
      { name: "AES-GCM", length: 256 },
      false,
      ["encrypt", "decrypt"]
    );
  }

  /** Encrypt a UTF-8 string; return the envelope sent to the P42 vault. */
  async function encryptString(plaintext, key) {
    requireSubtle();
    const nonce = globalThis.crypto.getRandomValues(new Uint8Array(12));
    const ct = await subtle.encrypt(
      { name: "AES-GCM", iv: nonce }, key, utf8Encode(plaintext)
    );
    return {
      alg: "aes-gcm",
      nonce: b64encode(nonce),
      ciphertext: b64encode(new Uint8Array(ct)),
    };
  }

  /** Decrypt an envelope produced by encryptString; throws on wrong key/tamper. */
  async function decryptString(env, key) {
    requireSubtle();
    const pt = await subtle.decrypt(
      { name: "AES-GCM", iv: b64decode(env.nonce) }, key, b64decode(env.ciphertext)
    );
    return utf8Decode(new Uint8Array(pt));
  }

  /** Keyed HMAC-SHA256 blind index for exact-equality lookup (no fuzzy/range). */
  async function blindIndex(masterB64, label) {
    requireSubtle();
    const key = await subtle.importKey(
      "raw", b64decode(masterB64), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]
    );
    const sig = await subtle.sign("HMAC", key, utf8Encode(label));
    return b64encode(new Uint8Array(sig));
  }

  // --- WebAuthn/Passkey (requires a real platform authenticator) ---
  const webAuthnAvailable =
    typeof navigator !== "undefined" && !!navigator.credentials &&
    typeof navigator.credentials.create === "function";

  async function registerPasskey(_user, _challengeB64) {
    if (!webAuthnAvailable) throw new Error("WebAuthn unavailable (need real browser + platform authenticator)");
    throw new Error("registerPasskey: wire to your relying-party attestation endpoint before use");
  }

  async function getPasskey(_challengeB64) {
    if (!webAuthnAvailable) throw new Error("WebAuthn unavailable (need real browser + platform authenticator)");
    throw new Error("getPasskey: wire to your relying-party assertion endpoint before use");
  }

  // --- P42 vault REST helpers (server stores, never decrypts) ---
  async function putEnvelope(workspaceId, headers, payload) {
    const res = await fetch(`/workspaces/${workspaceId}/e2ee`, {
      method: "PUT", headers,
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error("e2ee store failed: HTTP " + res.status);
    return res.json();
  }

  async function getEnvelope(workspaceId, headers, ownerRef, field) {
    const q = new URLSearchParams({ owner_ref: ownerRef, field }).toString();
    const res = await fetch(`/workspaces/${workspaceId}/e2ee?${q}`, { headers });
    if (res.status === 404) return null;
    if (!res.ok) throw new Error("e2ee fetch failed: HTTP " + res.status);
    return res.json();
  }

  async function findEnvelope(workspaceId, headers, field, blindIndexValue) {
    const q = new URLSearchParams({ field, blind_index: blindIndexValue }).toString();
    const res = await fetch(`/workspaces/${workspaceId}/e2ee/find?${q}`, { headers });
    if (!res.ok) throw new Error("e2ee find failed: HTTP " + res.status);
    return res.json();
  }

  return {
    b64encode, b64decode,
    deriveKeyFromPassword, encryptString, decryptString, blindIndex,
    registerPasskey, getPasskey, webAuthnAvailable,
    putEnvelope, getEnvelope, findEnvelope,
  };
});
