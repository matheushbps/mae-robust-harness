export function runtimeHeaders(extra: Record<string, string> = {}): Record<string, string> {
  const token = process.env.AGENT_RUNTIME_TOKEN?.trim();
  return {
    ...extra,
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}
