import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(new Request("http://localhost/", { headers: { accept: "text/html" } }), {
    ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) },
  }, { waitUntil() {}, passThroughOnException() {} });
}

test("server-renders the robust harness console", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /<title>Robust Harness · Agricultural Agent Lab<\/title>/i);
  assert.match(html, /ROBUST HARNESS/);
  assert.match(html, /Municipal crop analysis/);
  assert.match(html, /Make evidence travel/);
  assert.match(html, /Validated agent graph|State graph/);
  assert.match(html, /Evidence Reconciler|Results Match Reconciler/);
  assert.match(html, /6 gates/);
  assert.match(html, /Certified Release Challenge/);
  assert.doesNotMatch(html, /codex-preview|react-loading-skeleton/i);
});

test("keeps provider credentials out of the browser bundle", async () => {
  const [page, route, statusRoute] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/api/run/route.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/api/model-status/route.ts", import.meta.url), "utf8"),
  ]);
  assert.match(page, /fetch\("\/api\/run"/);
  assert.match(page, /fetch\("\/api\/model-status"/);
  assert.doesNotMatch(page, /API_KEY|Authorization|Bearer/);
  assert.match(route, /AGENT_RUNTIME_URL/);
  assert.match(route, /X-Harness-Variant": "robust"/);
  assert.match(statusRoute, /MODEL_BASE_URL/);
  assert.doesNotMatch(`${page}\n${route}\n${statusRoute}`, /100\.79\.155\.79/);
});
