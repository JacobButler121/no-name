export const dynamic = "force-dynamic";

const MAX_CROP_BYTES = 5 * 1024 * 1024;
const CROP_TTL_MS = 5 * 60 * 1000;
const ALLOWED_CONTENT_TYPES = new Map([
  ["image/jpeg", "jpg"],
  ["image/png", "png"],
  ["image/webp", "webp"],
]);

type CropBucket = {
  put(
    key: string,
    value: ArrayBuffer,
    options: {
      httpMetadata: { contentType: string };
      customMetadata: Record<string, string>;
    },
  ): Promise<unknown>;
};

type RuntimeEnv = {
  CROPS?: CropBucket;
  SPOTTED_RELAY_TOKEN?: string;
};

async function runtimeEnv(): Promise<RuntimeEnv> {
  const { env } = await import("cloudflare:workers");
  return env as unknown as RuntimeEnv;
}

function authorized(request: Request, token: string | undefined): boolean {
  if (!token) return false;
  return request.headers.get("authorization") === `Bearer ${token}`;
}

export async function POST(request: Request) {
  const runtime = await runtimeEnv();
  if (!authorized(request, runtime.SPOTTED_RELAY_TOKEN)) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  if (!runtime.CROPS) {
    return Response.json({ error: "crop_storage_unavailable" }, { status: 503 });
  }

  const contentType = request.headers.get("content-type")?.split(";", 1)[0].trim();
  const extension = contentType ? ALLOWED_CONTENT_TYPES.get(contentType) : undefined;
  if (!contentType || !extension) {
    return Response.json({ error: "unsupported_image_type" }, { status: 415 });
  }

  const declaredLength = Number(request.headers.get("content-length") || "0");
  if (declaredLength > MAX_CROP_BYTES) {
    return Response.json({ error: "crop_too_large" }, { status: 413 });
  }
  const body = await request.arrayBuffer();
  if (!body.byteLength || body.byteLength > MAX_CROP_BYTES) {
    return Response.json({ error: "invalid_crop_size" }, { status: 413 });
  }

  const id = `${crypto.randomUUID()}.${extension}`;
  const key = `lens/${id}`;
  const expiresAt = Date.now() + CROP_TTL_MS;
  await runtime.CROPS.put(key, body, {
    httpMetadata: { contentType },
    customMetadata: { expiresAt: String(expiresAt) },
  });

  const cropUrl = new URL(`/api/lens-crops/${id}`, request.url).toString();
  return Response.json(
    { url: cropUrl, deleteUrl: cropUrl, expiresAt },
    { status: 201, headers: { "Cache-Control": "no-store" } },
  );
}
