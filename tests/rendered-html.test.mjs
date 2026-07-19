import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const worker = await loadWorker();
  return worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

async function loadWorker() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker;
}

test("server-renders the Spotted experience", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);
  const html = await response.text();
  assert.match(html, /<title>Spotted — Spot it\. Buy it\.<\/title>/i);
  assert.match(html, /Spot it\. Buy it\./);
  assert.match(html, /Paste a YouTube, TikTok, or Instagram link/);
  assert.match(html, /What should Spotted look for\?/);
  assert.doesNotMatch(html, /AI product discovery|Shop what you watch/);
  assert.match(html, /Upload a video instead/);
  assert.doesNotMatch(html, /SceneCart|Starter Project|codex-preview/);
});

test("keeps the live processor contract and an honest empty UI", async () => {
  const [page, css] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);
  assert.match(page, /fetch\("\/api\/jobs"/);
  assert.match(page, /\/api\/jobs\/upload/);
  assert.match(page, /EventSource/);
  assert.match(page, /youtube\.com\/iframe_api/);
  assert.match(page, /youtube-nocookie\.com/);
  assert.match(page, /seekTo/);
  assert.match(page, /verified ·.*possible/);
  assert.doesNotMatch(page, /not shown as shopping results/);
  assert.match(page, /Possible matches/);
  assert.match(page, /not verified as exact/);
  assert.match(page, /retrieval_blocked/);
  assert.match(page, /merging_duplicates/);
  assert.match(page, /possible/);
  assert.doesNotMatch(page, /const\s+fixture|NuPhy|Anglepoise|WH-1000XM5/);
  assert.match(css, /prefers-reduced-motion/);
});

test("serves signed relay crops before the private application handler", async () => {
  const worker = await loadWorker();
  const values = new Map();
  const bucket = {
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
  };
  const env = {
    ASSETS: { fetch: async () => new Response("private app", { status: 401 }) },
    CROPS: bucket,
    SPOTTED_RELAY_TOKEN: "test-relay-secret",
  };
  const context = { waitUntil() {}, passThroughOnException() {} };
  const unauthorized = await worker.fetch(
    new Request("https://spotted.example/api/lens-crops", { method: "POST", body: "image", headers: { "Content-Type": "image/jpeg" } }),
    env,
    context,
  );
  assert.equal(unauthorized.status, 401);

  const upload = await worker.fetch(
    new Request("https://spotted.example/api/lens-crops", {
      method: "POST",
      body: "image",
      headers: { "Authorization": "Bearer test-relay-secret", "Content-Type": "image/jpeg" },
    }),
    env,
    context,
  );
  assert.equal(upload.status, 201);
  const payload = await upload.json();
  assert.match(payload.url, /expires=\d+&signature=[0-9a-f]{64}$/);

  const first = await worker.fetch(new Request(payload.url), env, context);
  assert.equal(first.status, 200);
  assert.equal(await first.text(), "image");
  const second = await worker.fetch(new Request(payload.url), env, context);
  assert.equal(second.status, 200, "a harmless probe must not consume the Lens crop");
  assert.equal(await second.text(), "image");

  const unsigned = new URL(payload.url);
  unsigned.searchParams.delete("signature");
  assert.equal((await worker.fetch(new Request(unsigned), env, context)).status, 404);
  assert.equal(
    (await worker.fetch(new Request(payload.deleteUrl, { method: "DELETE", headers: { "Authorization": "Bearer test-relay-secret" } }), env, context)).status,
    204,
  );
  assert.equal((await worker.fetch(new Request(payload.url), env, context)).status, 404);
});
