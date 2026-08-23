import { runtimeHeaders } from "../runtime-headers";

const allowedProviders = new Set(["local-qwen"]);

export async function POST(request: Request) {
  const body = await request.json().catch(() => null);
  const prompt = typeof body?.prompt === "string" ? body.prompt.trim() : "";
  const provider = typeof body?.provider === "string" ? body.provider : "local-qwen";

  if (prompt.length < 20 || prompt.length > 20_000) {
    return Response.json({ error: "The prompt must contain between 20 and 20,000 characters." }, { status: 400 });
  }
  if (!allowedProviders.has(provider)) {
    return Response.json({ error: "Unsupported inference provider." }, { status: 400 });
  }

  const agent_prompts =
    typeof body?.agent_prompts === "object" && body?.agent_prompts !== null
      ? (body.agent_prompts as Record<string, string>)
      : undefined;

  const runtimeUrl = (process.env.AGENT_RUNTIME_URL ?? "http://127.0.0.1:8788").replace(/\/$/, "");
  try {
    const upstream = await fetch(`${runtimeUrl}/runs`, {
      method: "POST",
      headers: runtimeHeaders({ "Content-Type": "application/json", "X-Harness-Variant": "robust" }),
      body: JSON.stringify({ harness: "robust", prompt, provider, agent_prompts }),
      signal: AbortSignal.timeout(120_000),
    });
    const payload = await upstream.json().catch(() => ({ error: "The runtime returned a non-JSON response." }));
    return Response.json(payload, { status: upstream.status });
  } catch {
    return Response.json(
      { error: "The agent runtime is not reachable. Start the local runtime and try again." },
      { status: 503 },
    );
  }
}
