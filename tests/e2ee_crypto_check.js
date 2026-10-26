// Node-only sanity check for app/static/e2ee.js pure crypto (not pytest).
const assert = require("assert");
const E = require("../app/static/e2ee.js");

(async () => {
  const salt = E.b64encode(crypto.getRandomValues(new Uint8Array(16)));
  const key = await E.deriveKeyFromPassword("owner-passphrase-例", salt);

  // 1. round-trip
  const plain = "真实姓名：张三，金额 ¥12,345.67，私聊原文 hello 世界";
  const env = await E.encryptString(plain, key);
  assert.strictEqual(env.alg, "aes-gcm");
  const back = await E.decryptString(env, key);
  assert.strictEqual(back, plain);

  // 2. wrong key fails
  const otherKey = await E.deriveKeyFromPassword("wrong", salt);
  await assert.rejects(() => E.decryptString(env, otherKey));

  // 3. blind index stable per label, differs across labels
  const master = E.b64encode(crypto.getRandomValues(new Uint8Array(32)));
  const bi1 = await E.blindIndex(master, "real_name:张三");
  const bi1b = await E.blindIndex(master, "real_name:张三");
  const bi2 = await E.blindIndex(master, "real_name:李四");
  assert.strictEqual(bi1, bi1b);
  assert.notStrictEqual(bi1, bi2);

  // 4. WebAuthn guarded in node (no platform authenticator)
  assert.strictEqual(typeof E.registerPasskey, "function");
  await assert.rejects(() => E.getPasskey("x"), /WebAuthn unavailable/);

  console.log("e2ee crypto check OK");
})().catch((e) => { console.error(e); process.exit(1); });
