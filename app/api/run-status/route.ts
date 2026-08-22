export async function GET(request: Request) {
  const runId = new URL(request.url).searchParams.get("run_id") ?? "";
  if (!/^[a-f0-9]{16}$/.test(runId)) {
    return Response.json({ error: "A valid run_id is required." }, { status: 400 });
  }
  const runtimeUrl = (process.env.AGENT_RUNTIME_URL ?? "http://127.0.0.1:8788").replace(/\/$/, "");
  try {
    const upstream = await fetch(`${runtimeUrl}/runs/${runId}`, {
      cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
    const payload = await upstream.json().catch(() => ({ error: "Runtime returned non-JSON status." }));
    return Response.json(payload, { status: upstream.status, headers: { "Cache-Control": "no-store" } });
  } catch {
    return Response.json({ error: "The robust runtime is unreachable." }, { status: 503 });
  }
}
