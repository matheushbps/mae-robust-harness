type ModelMetadata = {
  id?: string;
  state?: string;
  loaded_context_length?: number;
  max_context_length?: number;
  quantization?: string;
  type?: string;
  capabilities?: string[];
};

export async function GET() {
  const baseUrl = (process.env.MODEL_BASE_URL ?? "http://127.0.0.1:1234/v1").replace(/\/$/, "");
  const serverRoot = baseUrl.endsWith("/v1") ? baseUrl.slice(0, -3) : baseUrl;
  const apiKey = process.env.MODEL_API_KEY;
  const targetModel = process.env.MODEL_ID?.trim();
  const headers = {
    Accept: "application/json",
    ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
  };

  try {
    const response = await fetch(`${baseUrl}/models`, {
      headers,
      cache: "no-store",
      signal: AbortSignal.timeout(4_500),
    });

    if (!response.ok) {
      return Response.json(
        { connected: false, model: null, message: `Model server returned HTTP ${response.status}.` },
        { headers: { "Cache-Control": "no-store" } },
      );
    }

    const payload = await response.json().catch(() => null);
    const models = Array.isArray(payload?.data)
      ? payload.data.map((item: { id?: unknown }) => item?.id).filter((id: unknown): id is string => typeof id === "string")
      : [];

    if (models.length === 0) {
      return Response.json(
        { connected: false, model: null, message: "The endpoint responded, but no model is loaded." },
        { headers: { "Cache-Control": "no-store" } },
      );
    }

    if (targetModel && !models.includes(targetModel)) {
      return Response.json(
        { connected: false, model: null, models, message: `The configured model ${targetModel} is not available.` },
        { headers: { "Cache-Control": "no-store" } },
      );
    }

    const model = targetModel ?? models[0];

    let metadata: ModelMetadata | undefined;
    try {
      const metadataResponse = await fetch(`${serverRoot}/api/v0/models`, {
        headers,
        cache: "no-store",
        signal: AbortSignal.timeout(4_500),
      });
      if (metadataResponse.ok) {
        const metadataPayload = await metadataResponse.json().catch(() => null);
        metadata = Array.isArray(metadataPayload?.data)
          ? metadataPayload.data.find((item: ModelMetadata) => item?.id === model)
          : undefined;
      }
    } catch {
      // Generic OpenAI-compatible servers may not expose LM Studio metadata.
    }

    if (metadata && metadata.state !== "loaded") {
      return Response.json(
        { connected: false, model, models, message: `The configured model is registered but currently ${metadata.state ?? "not loaded"}.` },
        { headers: { "Cache-Control": "no-store" } },
      );
    }

    return Response.json(
      {
        connected: true,
        model,
        models,
        contextLength: metadata?.loaded_context_length ?? null,
        maxContextLength: metadata?.max_context_length ?? null,
        quantization: metadata?.quantization ?? null,
        modelType: metadata?.type ?? null,
        capabilities: metadata?.capabilities ?? [],
        message: metadata?.loaded_context_length
          ? `Target model loaded with ${metadata.loaded_context_length.toLocaleString("en-US")}-token context.`
          : `${models.length} model${models.length === 1 ? "" : "s"} available; target model verified.`,
      },
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch {
    return Response.json(
      { connected: false, model: null, message: "The local model server is unreachable." },
      { headers: { "Cache-Control": "no-store" } },
    );
  }
}
