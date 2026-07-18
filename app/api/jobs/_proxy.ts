const PROCESSOR_URL = (
  process.env.SPOTTED_PROCESSOR_URL ?? "http://127.0.0.1:8000"
).replace(/\/$/, "");

const REQUEST_HEADERS = [
  "accept",
  "content-type",
  "last-event-id",
  "range",
] as const;

const RESPONSE_HEADERS = [
  "accept-ranges",
  "cache-control",
  "content-disposition",
  "content-length",
  "content-range",
  "content-type",
] as const;

export async function proxyProcessor(
  request: Request,
  path: string,
): Promise<Response> {
  const headers = new Headers();
  for (const name of REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }

  const init: RequestInit = {
    method: request.method,
    headers,
    redirect: "manual",
  };

  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = await request.arrayBuffer();
  }

  try {
    const upstream = await fetch(`${PROCESSOR_URL}${path}`, init);
    const responseHeaders = new Headers();
    for (const name of RESPONSE_HEADERS) {
      const value = upstream.headers.get(name);
      if (value) responseHeaders.set(name, value);
    }

    if (responseHeaders.get("content-type")?.includes("text/event-stream")) {
      responseHeaders.set("x-accel-buffering", "no");
    }

    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers: responseHeaders,
    });
  } catch {
    return Response.json(
      {
        error: "processor_unavailable",
        message:
          "The Spotted video processor is offline. Start it and try again.",
      },
      { status: 503 },
    );
  }
}
