import { proxyProcessor } from "../../../_proxy";

export const dynamic = "force-dynamic";

type RouteContext = {
  params: Promise<{ id: string; filename: string }>;
};

export async function GET(request: Request, context: RouteContext) {
  const { id, filename } = await context.params;
  return proxyProcessor(
    request,
    `/api/jobs/${encodeURIComponent(id)}/frames/${encodeURIComponent(filename)}`,
  );
}
