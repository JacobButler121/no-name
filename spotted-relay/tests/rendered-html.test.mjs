import assert from "node:assert/strict";
import test from "node:test";

async function loadWorker() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  return (await import(workerUrl.href)).default;
}

function testEnvironment() {
  const values = new Map();
  return {
    values,
    env: {
      SPOTTED_RELAY_TOKEN: "relay-test-secret",
      CROPS: {
        async put(key, value, options) {
          values.set(key, {
            bytes: new Uint8Array(value),
            customMetadata: options.customMetadata,
            contentType: options.httpMetadata.contentType,
          });
        },
        async get(key) {
          const value = values.get(key);
          if (!value) return null;
          return {
            body: new Blob([value.bytes]).stream(),
            size: value.bytes.byteLength,
            customMetadata: value.customMetadata,
            writeHttpMetadata(headers) { headers.set("Content-Type", value.contentType); },
          };
        },
        async delete(key) { values.delete(key); },
      },
    },
  };
}

test("exposes only a minimal relay health response", async () => {
  const worker = await loadWorker();
  const { env } = testEnvironment();
  const health = await worker.fetch(new Request("https://relay.example/health"), env);
  assert.equal(health.status, 200);
  assert.deepEqual(await health.json(), { service: "spotted-image-relay", status: "ok" });
  assert.equal((await worker.fetch(new Request("https://relay.example/admin"), env)).status, 404);
});

test("requires authenticated uploads and signed short-lived reads", async () => {
  const worker = await loadWorker();
  const { env } = testEnvironment();
  const unauthorized = await worker.fetch(new Request("https://relay.example/api/lens-crops", {
    method: "POST", body: "image", headers: { "Content-Type": "image/png" },
  }), env);
  assert.equal(unauthorized.status, 401);

  const upload = await worker.fetch(new Request("https://relay.example/api/lens-crops", {
    method: "POST",
    body: "image",
    headers: { "Authorization": "Bearer relay-test-secret", "Content-Type": "image/png" },
  }), env);
  assert.equal(upload.status, 201);
  const payload = await upload.json();
  assert.match(payload.url, /expires=\d+&signature=[0-9a-f]{64}$/);

  const first = await worker.fetch(new Request(payload.url), env);
  assert.equal(first.status, 200);
  assert.equal(await first.text(), "image");
  const second = await worker.fetch(new Request(payload.url), env);
  assert.equal(second.status, 200, "a crawler probe must not consume the crop");
  assert.equal(await second.text(), "image");

  const unsigned = new URL(payload.url);
  unsigned.searchParams.delete("signature");
  assert.equal((await worker.fetch(new Request(unsigned), env)).status, 404);
  assert.equal((await worker.fetch(new Request(payload.deleteUrl, {
    method: "DELETE", headers: { "Authorization": "Bearer relay-test-secret" },
  }), env)).status, 204);
  assert.equal((await worker.fetch(new Request(payload.url), env)).status, 404);
});
