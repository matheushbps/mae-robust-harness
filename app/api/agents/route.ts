export async function GET() {
  const runtimeUrl = (process.env.AGENT_RUNTIME_URL ?? "http://127.0.0.1:8788").replace(/\/$/, "");
  try {
    const upstream = await fetch(`${runtimeUrl}/agents`, {
      cache: "no-store",
      headers: { "X-Harness-Variant": "robust" },
      signal: AbortSignal.timeout(10_000),
    });
    const payload = await upstream.json().catch(() => []);
    return Response.json(payload, { status: upstream.status });
  } catch {
    return Response.json(
      { error: `The agent runtime is not reachable at ${runtimeUrl}.` },
      { status: 503 },
    );
  }
}
