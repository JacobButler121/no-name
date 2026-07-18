export const dynamic = "force-dynamic";

type StoredCrop = {
  arrayBuffer(): Promise<ArrayBuffer>;
  httpMetadata?: { contentType?: string };
  customMetadata?: Record<string, string>;
};

type CropBucket = {
  get(key: string): Promise<StoredCrop | null>;
  delete(key: string): Promise<void>;
};

type RuntimeEnv = {
  CROPS?: CropBucket;
  SPOTTED_RELAY_TOKEN?: string;
};

type RouteContext = { params: Promise<{ id: string }> };

async function runtimeEnv(): Promise<RuntimeEnv> {
  const { env } = await import("cloudflare:workers");
  return env as unknown as RuntimeEnv;
}

function cropKey(id: string): string | null {
  return /^[0-9a-f-]{36}\.(?:jpg|png|webp)$/i.test(id) ? `lens/${id}` : null;
}

function authorized(request: Request, token: string | undefined): boolean {
  if (!token) return false;
  return request.headers.get("authorization") === `Bearer ${token}`;
}

export async function GET(_request: Request, context: RouteContext) {
  const runtime = await runtimeEnv();
  const { id } = await context.params;
  const key = cropKey(id);
  if (!runtime.CROPS || !key) {
    return new Response("Not found", { status: 404 });
  }

  const crop = await runtime.CROPS.get(key);
  if (!crop) {
    return new Response("Not found", { status: 404 });
  }
  const expiresAt = Number(crop.customMetadata?.expiresAt || "0");
  if (!expiresAt || expiresAt < Date.now()) {
    await runtime.CROPS.delete(key);
    return new Response("Expired", { status: 410 });
  }

  // Lens receives a single-use response; removing the R2 object here ensures
  // cleanup even if the processor is interrupted before its DELETE request.
  const image = await crop.arrayBuffer();
  await runtime.CROPS.delete(key);
  return new Response(image, {
    headers: {
      "Content-Type": crop.httpMetadata?.contentType || "image/jpeg",
      "Content-Length": String(image.byteLength),
      "Cache-Control": "no-store, max-age=0",
      "X-Content-Type-Options": "nosniff",
      "X-Robots-Tag": "noindex, nofollow, noarchive",
    },
  });
}

export async function DELETE(request: Request, context: RouteContext) {
  const runtime = await runtimeEnv();
  if (!authorized(request, runtime.SPOTTED_RELAY_TOKEN)) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  const { id } = await context.params;
  const key = cropKey(id);
  if (!runtime.CROPS || !key) {
    return new Response(null, { status: 204 });
  }
  await runtime.CROPS.delete(key);
  return new Response(null, { status: 204 });
}
