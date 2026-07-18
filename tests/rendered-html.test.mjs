import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(
    new Request("http://localhost/", { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
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
  assert.match(page, /verified products/);
  assert.match(page, /not shown as shopping results/);
  assert.doesNotMatch(page, /Possible finds/);
  assert.match(page, /retrieval_blocked/);
  assert.match(page, /merging_duplicates/);
  assert.match(page, /possible/);
  assert.doesNotMatch(page, /const\s+fixture|NuPhy|Anglepoise|WH-1000XM5/);
  assert.match(css, /prefers-reduced-motion/);
});
