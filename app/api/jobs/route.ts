import { proxyProcessor } from "./_proxy";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  return proxyProcessor(request, "/api/jobs");
}
