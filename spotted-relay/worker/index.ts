interface Env {
  CROPS: R2Bucket;
  SPOTTED_RELAY_TOKEN?: string;
}

const MAX_CROP_BYTES = 5 * 1024 * 1024;
const CROP_TTL_MS = 5 * 60 * 1000;
const CROP_PATH = /^\/api\/lens-crops\/([0-9a-f-]{36}\.(?:jpg|png|webp))$/i;
const CROP_TYPES = new Map([
  ["image/jpeg", "jpg"],
  ["image/png", "png"],
  ["image/webp", "webp"],
]);

function authorized(request: Request, token: string | undefined): boolean {
  return Boolean(token && request.headers.get("authorization") === `Bearer ${token}`);
}

function bytesToHex(value: ArrayBuffer): string {
  return [...new Uint8Array(value)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function signature(token: string, id: string, expiresAt: number): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(token),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  return bytesToHex(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`${id}:${expiresAt}`)));
}

async function validSignature(
  token: string | undefined,
  id: string,
  expiresAt: number,
  provided: string | null,
): Promise<boolean> {
  if (!token || !provided || !Number.isSafeInteger(expiresAt) || expiresAt < Date.now()) return false;
  const expected = await signature(token, id, expiresAt);
  if (provided.length !== expected.length) return false;
  let difference = 0;
  for (let index = 0; index < expected.length; index += 1) {
    difference |= expected.charCodeAt(index) ^ provided.charCodeAt(index);
  }
  return difference === 0;
}

function securityHeaders(): Headers {
  return new Headers({
    "Cache-Control": "private, no-store, max-age=0",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
  });
}

const worker = {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/" || url.pathname === "/health") {
      return Response.json(
        { service: "spotted-image-relay", status: "ok" },
        { headers: { "Cache-Control": "no-store", "X-Robots-Tag": "noindex" } },
      );
    }

    if (url.pathname === "/api/lens-crops") {
      if (request.method !== "POST") return new Response("Method not allowed", { status: 405 });
      if (!authorized(request, env.SPOTTED_RELAY_TOKEN)) {
        return Response.json({ error: "unauthorized" }, { status: 401 });
      }
      if (!env.CROPS) return Response.json({ error: "storage_unavailable" }, { status: 503 });
      const contentType = request.headers.get("content-type")?.split(";", 1)[0].trim();
      const extension = contentType ? CROP_TYPES.get(contentType) : undefined;
      if (!contentType || !extension) return Response.json({ error: "unsupported_image_type" }, { status: 415 });
      const declaredLength = Number(request.headers.get("content-length") || "0");
      if (declaredLength > MAX_CROP_BYTES) return Response.json({ error: "crop_too_large" }, { status: 413 });
      const body = await request.arrayBuffer();
      if (!body.byteLength || body.byteLength > MAX_CROP_BYTES) {
        return Response.json({ error: "invalid_crop_size" }, { status: 413 });
      }
      const id = `${crypto.randomUUID()}.${extension}`;
      const expiresAt = Date.now() + CROP_TTL_MS;
      await env.CROPS.put(`lens/${id}`, body, {
        httpMetadata: { contentType },
        customMetadata: { expiresAt: String(expiresAt) },
      });
      const cropUrl = new URL(`/api/lens-crops/${id}`, request.url);
      cropUrl.searchParams.set("expires", String(expiresAt));
      cropUrl.searchParams.set("signature", await signature(env.SPOTTED_RELAY_TOKEN!, id, expiresAt));
      return Response.json(
        { url: cropUrl.toString(), deleteUrl: new URL(`/api/lens-crops/${id}`, request.url).toString(), expiresAt },
        { status: 201, headers: { "Cache-Control": "no-store" } },
      );
    }

    const match = CROP_PATH.exec(url.pathname);
    if (!match) return new Response("Not found", { status: 404, headers: securityHeaders() });
    const id = match[1];
    const key = `lens/${id}`;
    if (request.method === "DELETE") {
      if (!authorized(request, env.SPOTTED_RELAY_TOKEN)) {
        return Response.json({ error: "unauthorized" }, { status: 401 });
      }
      await env.CROPS?.delete(key);
      return new Response(null, { status: 204 });
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed", { status: 405 });
    }
    const expiresAt = Number(url.searchParams.get("expires"));
    if (!(await validSignature(env.SPOTTED_RELAY_TOKEN, id, expiresAt, url.searchParams.get("signature")))) {
      return new Response("Not found", { status: 404, headers: securityHeaders() });
    }
    const crop = await env.CROPS?.get(key);
    if (!crop) return new Response("Not found", { status: 404, headers: securityHeaders() });
    const storedExpiry = Number(crop.customMetadata?.expiresAt || "0");
    if (!storedExpiry || storedExpiry !== expiresAt || storedExpiry < Date.now()) {
      await env.CROPS.delete(key);
      return new Response("Expired", { status: 410, headers: securityHeaders() });
    }
    const headers = securityHeaders();
    crop.writeHttpMetadata(headers);
    headers.set("Content-Length", String(crop.size));
    return new Response(request.method === "HEAD" ? null : crop.body, { headers });
  },
};

export default worker;
