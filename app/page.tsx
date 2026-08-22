"use client";

import { useCallback, useEffect, useState } from "react";

const frozenPrompt =
  "Analyze the Brazilian municipal agricultural production database. Identify the most relevant changes in planted area, production, yield, and production value, then present evidence-backed business insights.";

const gates = ["Schema", "Python", "SQL", "Agreement", "Provenance", "Visual"];

type RunState = "idle" | "submitting" | "accepted" | "running" | "completed" | "failed" | "error";
type ConnectionState = "checking" | "connected" | "offline";

type ModelStatus = {
  connected: boolean;
  model: string | null;
  message: string;
  contextLength?: number | null;
  quantization?: string | null;
};

type RunEvent = {
  sequence: number;
  node: string;
  event_type: string;
  message: string;
};

type RunSnapshot = {
  run_id: string;
  status: "queued" | "running" | "completed" | "failed";
  events: RunEvent[];
  error?: string | null;
};

const gateNodes: Record<string, string> = {
  Schema: "data_profiler",
  Python: "python_analyst",
  SQL: "sql_analyst",
  Agreement: "evidence_reconciler",
  Provenance: "dashboard_engineer",
  Visual: "visual_reviewer",
};

function BrandIcon() {
  return (
    <svg viewBox="0 0 40 40" aria-hidden="true">
      <path d="M9 29V15l11-6 11 6v14l-11 6-11-6Z" />
      <path d="M20 9v26M9 15l11 7 11-7" />
    </svg>
  );
}

function ArrowIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <path d="M4 10h11M11 6l4 4-4 4" />
    </svg>
  );
}

function RefreshIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <path d="M15.2 7A6 6 0 1 0 16 11" />
      <path d="M12 7h3.5V3.5" />
    </svg>
  );
}

function AgentNode({ id, title, detail, active = false }: { id: string; title: string; detail: string; active?: boolean }) {
  return (
    <div className={`agent-node ${active ? "active" : ""}`}>
      <span>{id}</span>
      <div><strong>{title}</strong><small>{detail}</small></div>
      <i />
    </div>
  );
}

export default function Home() {
  const [runState, setRunState] = useState<RunState>("idle");
  const [runMessage, setRunMessage] = useState("Awaiting a connected model and graph runtime.");
  const [connection, setConnection] = useState<ConnectionState>("checking");
  const [modelStatus, setModelStatus] = useState<ModelStatus | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [runEvents, setRunEvents] = useState<RunEvent[]>([]);

  const checkModel = useCallback(async () => {
    setConnection("checking");
    try {
      const response = await fetch("/api/model-status", { cache: "no-store" });
      const data = (await response.json()) as ModelStatus;
      setModelStatus(data);
      setConnection(data.connected ? "connected" : "offline");
    } catch {
      setModelStatus({ connected: false, model: null, message: "Model status check failed." });
      setConnection("offline");
    }
  }, []);

  useEffect(() => {
    const initialCheck = window.setTimeout(() => void checkModel(), 0);
    const interval = window.setInterval(() => void checkModel(), 30_000);
    return () => {
      window.clearTimeout(initialCheck);
      window.clearInterval(interval);
    };
  }, [checkModel]);

  useEffect(() => {
    if (!runId || ["completed", "failed", "error"].includes(runState)) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const response = await fetch(`/api/run-status?run_id=${runId}`, { cache: "no-store" });
        const snapshot = (await response.json()) as RunSnapshot & { error?: string };
        if (!response.ok) throw new Error(snapshot.error ?? "Unable to read graph status.");
        if (cancelled) return;
        setRunEvents(snapshot.events ?? []);
        const latest = snapshot.events?.at(-1);
        setRunMessage(latest?.message ?? `Run ${runId} is ${snapshot.status}.`);
        setRunState(snapshot.status === "queued" ? "accepted" : snapshot.status);
        if (snapshot.status === "failed" && snapshot.error) setRunMessage(snapshot.error);
      } catch (error) {
        if (!cancelled) {
          setRunState("error");
          setRunMessage(error instanceof Error ? error.message : "Graph polling failed.");
        }
      }
    };
    void poll();
    const interval = window.setInterval(() => void poll(), 2_000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [runId, runState]);

  async function runGraph() {
    setRunState("submitting");
    setRunMessage("Validating the frozen contract and creating graph state…");
    try {
      const response = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: frozenPrompt, provider: "local-qwen" }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error ?? "The runtime rejected this run.");
      setRunId(data.run_id);
      setRunEvents([]);
      setRunState("accepted");
      setRunMessage(data.message ?? `Run ${data.run_id ?? "accepted"} entered the validated graph.`);
    } catch (error) {
      setRunState("error");
      setRunMessage(error instanceof Error ? error.message : "Unable to reach the agent runtime.");
    }
  }

  const modelLabel =
    connection === "checking" ? "Checking local endpoint" : modelStatus?.model ?? "Model server unreachable";
  const canRun = connection === "connected" && !["submitting", "accepted", "running"].includes(runState);
  const latestNode = runEvents.at(-1)?.node;
  const gateStatus = (gate: string) => {
    const events = runEvents.filter((event) => event.node === gateNodes[gate]);
    if (events.some((event) => event.event_type === "failed")) return "FAILED";
    if (events.some((event) => event.event_type === "completed")) return "PASS";
    return "WAIT";
  };

  return (
    <main className="app-frame variant-robust">
      <aside className="rail">
        <div className="brand-lockup">
          <span className="brand-icon"><BrandIcon /></span>
          <span><b>MAE</b><small>Agent benchmark</small></span>
        </div>

        <nav className="rail-nav" aria-label="Workspace sections">
          <a className="active" href="#workspace"><span>01</span>Run console</a>
          <a href="#method"><span>02</span>Method</a>
          <a href="#evidence"><span>03</span>Evidence</a>
        </nav>

        <div className="rail-note">
          <span className="condition-token">CONDITION B</span>
          <strong>Validated orchestration</strong>
          <p>Typed state, scoped tools, evidence gates, and bounded repair paths.</p>
        </div>

        <div className="rail-footer">
          <span className={`status-dot ${connection}`} />
          <div><strong>{connection === "connected" ? "Inference online" : connection === "checking" ? "Checking inference" : "Inference offline"}</strong><small>OpenAI-compatible endpoint</small></div>
        </div>
      </aside>

      <section className="workspace" id="workspace">
        <header className="topbar">
          <div className="title-group">
            <span className="kicker">AGRICULTURAL INTELLIGENCE / EXPERIMENT 01</span>
            <h1>Municipal crop analysis</h1>
          </div>
          <div className={`model-chip ${connection}`}>
            <span className="status-dot" />
            <div><small>LOCAL INFERENCE</small><strong title={modelLabel}>{modelLabel}</strong></div>
            <button type="button" onClick={() => void checkModel()} aria-label="Check model connection" disabled={connection === "checking"}><RefreshIcon /></button>
          </div>
        </header>

        <section className="hero-band">
          <div>
            <span className="section-label">ROBUST HARNESS · FROZEN GRAPH</span>
            <h2>Constrain the model.<br />Make evidence travel.</h2>
            <p>The intervention adds specialist context, typed state, independent analysis paths, validation gates, and inspectable recovery.</p>
          </div>
          <div className="hero-index" aria-label="Experiment condition B"><span>B</span><small>OF 2 CONDITIONS</small></div>
        </section>

        <section className="metric-grid" aria-label="Harness summary">
          <article><small>TOPOLOGY</small><strong>State graph</strong><span>Conditional routing</span></article>
          <article><small>MODEL ROLES</small><strong>8</strong><span>Scoped responsibility</span></article>
          <article><small>VALIDATION</small><strong>6 gates</strong><span>Evidence before prose</span></article>
          <article><small>RETRY POLICY</small><strong>2×</strong><span>Targeted, then stop</span></article>
        </section>

        <section className="workbench">
          <article className="card prompt-card">
            <div className="card-head">
              <div><span className="card-index">01</span><div><small>CONTROLLED INPUT</small><h3>Business prompt</h3></div></div>
              <span className="tag">CONTRACT V1.0 · LOCKED</span>
            </div>
            <label className="sr-only" htmlFor="business-prompt">Frozen business prompt</label>
            <textarea id="business-prompt" value={frozenPrompt} readOnly />
            <div className="prompt-meta">
              <div><small>PROVIDER</small><strong>Local Qwen</strong><span>OpenAI-compatible API</span></div>
              <div><small>DATA SCOPE</small><strong>IBGE PAM</strong><span>Municipal · 2019–2024</span></div>
              <div><small>MODEL WINDOW</small><strong>{modelStatus?.contextLength ? `${modelStatus.contextLength.toLocaleString("en-US")} tokens` : "65K configured"}</strong><span>{modelStatus?.quantization ?? "Pending metadata"}</span></div>
            </div>
            <div className="action-row">
              <p><span className={`status-dot ${connection}`} />{connection === "connected" ? "Model endpoint verified. Graph submission is enabled." : "Connect the model server to enable a benchmark run."}</p>
              <button className="primary-action" type="button" disabled={!canRun} onClick={runGraph}>
                {runState === "submitting" ? "Creating graph state…" : "Start benchmark run"}<ArrowIcon />
              </button>
            </div>
          </article>

          <article className="card method-card" id="method">
            <div className="card-head">
              <div><span className="card-index">02</span><div><small>EXECUTION DESIGN</small><h3>Validated agent graph</h3></div></div>
              <span className="tag">LANGGRAPH · TYPED STATE</span>
            </div>
            <div className="agent-graph" aria-label="Robust harness agent graph">
              <div className="graph-single"><AgentNode id="01" title="Business Analyst" detail="Metric contract" active={runState === "submitting" || latestNode === "business_analyst"} /></div>
              <span className="down-link" />
              <div className="graph-single"><AgentNode id="02" title="Data Profiler" detail="Schema + quality" active={latestNode === "data_profiler"} /></div>
              <div className="fork-link"><i /><i /><i /></div>
              <div className="parallel-row">
                <AgentNode id="03" title="SQL Analyst" detail="Read-only DuckDB" active={latestNode === "sql_analyst"} />
                <AgentNode id="04" title="Python Analyst" detail="Sandboxed analysis" active={latestNode === "python_analyst"} />
              </div>
              <div className="join-link"><i /><i /><i /></div>
              <div className="graph-single"><AgentNode id="05" title="Evidence Reconciler" detail="Numeric agreement gate" active={latestNode === "evidence_reconciler"} /></div>
              <span className="down-link" />
              <div className="final-row">
                <AgentNode id="06" title="Dashboard" detail="Approved facts" active={latestNode === "dashboard_engineer"} />
                <AgentNode id="07" title="Visual QA" detail="Rendered review" active={latestNode === "visual_reviewer"} />
                <AgentNode id="08" title="Final Editor" detail="Cited narrative" active={latestNode === "final_editor"} />
              </div>
            </div>
            <div className="method-foot"><span>Checkpointed state</span><span>Role-scoped tools</span><span>Bounded repair edges</span></div>
          </article>
        </section>

        <section className="evidence-grid" id="evidence">
          <article className={`card run-card ${runState}`}>
            <div className="mini-head"><div><span className="pulse-mark" /><div><small>RUN CONTROL</small><h3>Validation ledger</h3></div></div><span>{runState.toUpperCase()}</span></div>
            <p className="run-message" aria-live="polite">{runMessage}</p>
            <div className="gate-list">
              {gates.map((gate) => <span key={gate}><i />{gate}<b>{runState === "error" ? "ERROR" : gateStatus(gate)}</b></span>)}
            </div>
            <div className="trace-list">
              <div><span>RUN ID</span><strong>{runId ?? "Not started"}</strong></div>
              <div><span>EVENTS</span><strong>{runEvents.length} graph events</strong></div>
            </div>
            {runId && (
              <div style={{ marginTop: "1.25rem", paddingTop: "1rem", borderTop: "1px solid rgba(255,255,255,0.08)" }}>
                <a
                  href={`/api/run-artifact?run_id=${runId}&file=dashboard.html`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="primary-action"
                  style={{
                    display: "inline-flex",
                    alignItems: "center",
                    gap: "0.5rem",
                    textDecoration: "none",
                    fontSize: "0.85rem",
                    padding: "0.5rem 1rem",
                    borderRadius: "6px",
                    width: "100%",
                    justifyContent: "center",
                  }}
                >
                  Open Interactive HTML Dashboard <ArrowIcon />
                </a>
              </div>
            )}
          </article>

          <article className="card data-card">
            <div className="mini-head"><div><span className="dataset-mark">DB</span><div><small>FIXED EVIDENCE BASE</small><h3>Dataset contract</h3></div></div><span>PUBLIC DATA</span></div>
            <div className="data-name"><strong>IBGE SIDRA · PAM 5457</strong><span>Municipal Agricultural Production</span></div>
            <div className="data-specs"><div><small>PERIOD</small><strong>2019–2024</strong></div><div><small>ENGINE</small><strong>DuckDB</strong></div><div><small>CROPS</small><strong>7 selected</strong></div></div>
            <p>Each approved claim will carry its query, artifact, dataset hash, unit, and numeric tolerance.</p>
          </article>
        </section>

        <footer className="page-footer"><span>MAE / HARNESS ENGINEERING CASE STUDY</span><span>Robust condition · validated interface</span></footer>
      </section>
    </main>
  );
}
