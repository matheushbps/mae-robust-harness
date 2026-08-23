import { runtimeHeaders } from "../runtime-headers";

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const runId = searchParams.get("run_id") ?? "";
  const file = searchParams.get("file") ?? "dashboard.html";
  if (!/^[a-f0-9]{16}$/.test(runId)) {
    return new Response("A valid run_id is required.", { status: 400 });
  }
  if (!["dashboard.html", "dashboard.json"].includes(file)) {
    return new Response("Invalid artifact requested.", { status: 400 });
  }
  const runtimeUrl = (process.env.AGENT_RUNTIME_URL ?? "http://127.0.0.1:8788").replace(/\/$/, "");
  try {
    const upstream = await fetch(`${runtimeUrl}/runs/${runId}/artifacts/${file}`, {
      cache: "no-store",
      headers: runtimeHeaders(),
      signal: AbortSignal.timeout(10_000),
    });
    if (!upstream.ok) {
      return new Response(`Artifact not found on runtime: ${upstream.statusText}`, { status: upstream.status });
    }
    const contentType = file.endsWith(".html") ? "text/html; charset=utf-8" : "application/json";
    const body = await upstream.text();
    return new Response(body, {
      status: 200,
      headers: {
        "Content-Type": contentType,
        "Cache-Control": "no-store",
      },
    });
  } catch {
    return new Response("The robust runtime is unreachable.", { status: 503 });
  }
}
