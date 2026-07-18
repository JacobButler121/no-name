import { proxyProcessor } from "../_proxy";

export const dynamic = "force-dynamic";

type RouteContext = { params: Promise<{ id: string }> };

export async function GET(request: Request, context: RouteContext) {
  const { id } = await context.params;
  return proxyProcessor(request, `/api/jobs/${encodeURIComponent(id)}`);
}

export async function DELETE(request: Request, context: RouteContext) {
  const { id } = await context.params;
  return proxyProcessor(request, `/api/jobs/${encodeURIComponent(id)}`);
}
