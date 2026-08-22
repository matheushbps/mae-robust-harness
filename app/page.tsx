"use client";

import { useCallback, useEffect, useState } from "react";

const defaultPrompt =
  "Analyze the Brazilian municipal agricultural production database. Identify the most relevant changes in planted area, production, yield, and production value, then present evidence-backed business insights.";

const promptPresets = [
  {
    id: "benchmark",
    label: "Benchmark Default",
    description: "Standard PAM 2019-2024 analysis across 7 crops",
    prompt: defaultPrompt,
  },
  {
    id: "grains",
    label: "Grains Focus (Soy, Corn, Wheat)",
    description: "Compare yield efficiency vs acreage growth in key grain crops",
    prompt:
      "Focus specifically on grain production dynamics (soybeans, corn, and wheat) between 2019 and 2024. Compare yield efficiency (kg/ha) vs planted area expansion and identify market saturation and land allocation patterns.",
  },
  {
    id: "value",
    label: "Production Value Surge",
    description: "Investigate nominal value growth vs physical output",
    prompt:
      "Analyze the dramatic surge in production value (thousand BRL) across all Brazilian crops from 2019 to 2024. Reconcile whether value gains were driven by volume growth or nominal commodity pricing.",
  },
  {
    id: "productivity",
    label: "Productivity & Yield Gains",
    description: "Analyze agricultural technological efficiency",
    prompt:
      "Evaluate agricultural productivity gains (yield in kg/ha) between 2019 and 2024 across all commodities. Which crops demonstrated real technological/efficiency gains versus pure acreage expansion?",
  },
];

const defaultAgents = [
  {
    id: "business_analyst",
    index: "01",
    role: "Business Analyst",
    system:
      "Convert the frozen request into explicit questions, metrics, units, and acceptance criteria. Do not perform analysis or invent facts.",
    tools: ["dataset_catalog", "schema_reader"],
  },
  {
    id: "data_profiler",
    index: "02",
    role: "Data Profiler",
    system:
      "Profile grain, coverage, missingness, ranges, and duplicate keys deterministically before analysis.",
    tools: ["readonly_duckdb_metadata", "dataset_profiler"],
  },
  {
    id: "sql_analyst",
    index: "03",
    role: "SQL Analyst",
    system:
      "Produce auditable agricultural aggregations with read-only DuckDB SQL. Attach the query and unit to every evidence item.",
    tools: ["readonly_sql"],
  },
  {
    id: "python_analyst",
    index: "04",
    role: "Python Analyst",
    system:
      "Independently reproduce key totals and changes with bounded Python analytics. Do not reuse SQL aggregate results.",
    tools: ["python_analytics"],
  },
  {
    id: "evidence_reconciler",
    index: "05",
    role: "Evidence Reconciler",
    system:
      "Compare independently produced evidence within declared tolerances. Reject mismatches and unsupported claims.",
    tools: ["evidence_store", "numeric_validator"],
  },
  {
    id: "dashboard_engineer",
    index: "06",
    role: "Dashboard Engineer",
    system:
      "Build dashboard artifacts from approved evidence only. Preserve units, periods, labels, and provenance IDs.",
    tools: ["approved_evidence_reader", "artifact_writer"],
  },
  {
    id: "visual_reviewer",
    index: "07",
    role: "Visual Reviewer",
    system:
      "Check rendered artifacts for missing labels, misleading scales, clipping, accessibility, and unsupported values.",
    tools: ["artifact_reader", "visual_checklist"],
  },
  {
    id: "final_editor",
    index: "08",
    role: "Final Editor",
    system:
      "Write the executive narrative using approved evidence IDs only. State limitations, failed gates, and uncertainty.",
    tools: ["approved_evidence_reader", "validation_ledger"],
  },
];

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

function AgentNode({
  id,
  title,
  detail,
  active = false,
}: {
  id: string;
  title: string;
  detail: string;
  active?: boolean;
}) {
  return (
    <div className={`agent-node ${active ? "active" : ""}`}>
      <span>{id}</span>
      <div>
        <strong>{title}</strong>
        <small>{detail}</small>
      </div>
      <i />
    </div>
  );
}

export default function Home() {
  const [prompt, setPrompt] = useState(defaultPrompt);
  const [agentPrompts, setAgentPrompts] = useState<Record<string, string>>({});
  const [showAgentConfig, setShowAgentConfig] = useState(true);
  const [runState, setRunState] = useState<RunState>("idle");
  const [runMessage, setRunMessage] = useState("Awaiting a connected model and graph runtime.");
  const [connection, setConnection] = useState<ConnectionState>("checking");
  const [modelStatus, setModelStatus] = useState<ModelStatus | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [runEvents, setRunEvents] = useState<RunEvent[]>([]);

  const isCustomPrompt = prompt.trim() !== defaultPrompt.trim();
  const customAgentsCount = Object.keys(agentPrompts).filter(
    (id) => agentPrompts[id] !== defaultAgents.find((a) => a.id === id)?.system,
  ).length;

  const getAgentSystemPrompt = (id: string) => {
    return agentPrompts[id] ?? defaultAgents.find((a) => a.id === id)?.system ?? "";
  };

  const handleAgentPromptChange = (id: string, text: string) => {
    setAgentPrompts((prev) => ({ ...prev, [id]: text }));
  };

  const handleResetAgentPrompt = (id: string) => {
    setAgentPrompts((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
  };

  const handleResetAllAgentPrompts = () => {
    setAgentPrompts({});
  };

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
    if (prompt.trim().length < 20) return;
    setRunState("submitting");
    setRunMessage("Validating prompt and initializing LangGraph state…");
    try {
      const activeCustomPrompts =
        customAgentsCount > 0
          ? Object.fromEntries(
              Object.entries(agentPrompts).filter(
                ([id, text]) => text !== defaultAgents.find((a) => a.id === id)?.system,
              ),
            )
          : undefined;

      const response = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt: prompt.trim(),
          provider: "local-qwen",
          agent_prompts: activeCustomPrompts,
        }),
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
  const canRun =
    connection === "connected" &&
    prompt.trim().length >= 20 &&
    !["submitting", "accepted", "running"].includes(runState);
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
          <span className="brand-icon">
            <BrandIcon />
          </span>
          <span>
            <b>MAE</b>
            <small>Agent benchmark</small>
          </span>
        </div>

        <nav className="rail-nav" aria-label="Workspace sections">
          <a className="active" href="#workspace">
            <span>01</span>Run console
          </a>
          <a href="#agents">
            <span>02</span>Agent prompts
          </a>
          <a href="#method">
            <span>03</span>Method
          </a>
          <a href="#evidence">
            <span>04</span>Evidence
          </a>
        </nav>

        <div className="rail-note">
          <span className="condition-token">CONDITION B</span>
          <strong>Validated orchestration</strong>
          <p>Typed state, scoped tools, evidence gates, and bounded repair paths.</p>
        </div>

        <div className="rail-footer">
          <span className={`status-dot ${connection}`} />
          <div>
            <strong>
              {connection === "connected"
                ? "Inference online"
                : connection === "checking"
                  ? "Checking inference"
                  : "Inference offline"}
            </strong>
            <small>OpenAI-compatible endpoint</small>
          </div>
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
            <div>
              <small>LOCAL INFERENCE</small>
              <strong title={modelLabel}>{modelLabel}</strong>
            </div>
            <button
              type="button"
              onClick={() => void checkModel()}
              aria-label="Check model connection"
              disabled={connection === "checking"}
            >
              <RefreshIcon />
            </button>
          </div>
        </header>

        <section className="hero-band">
          <div>
            <span className="section-label">ROBUST HARNESS · FROZEN GRAPH</span>
            <h2>
              Constrain the model.
              <br />
              Make evidence travel.
            </h2>
            <p>
              The intervention adds specialist context, typed state, independent analysis paths, validation
              gates, and inspectable recovery.
            </p>
          </div>
          <div className="hero-index" aria-label="Experiment condition B">
            <span>B</span>
            <small>OF 2 CONDITIONS</small>
          </div>
        </section>

        <section className="metric-grid" aria-label="Harness summary">
          <article>
            <small>TOPOLOGY</small>
            <strong>State graph</strong>
            <span>Conditional routing</span>
          </article>
          <article>
            <small>MODEL ROLES</small>
            <strong>8 specialists</strong>
            <span>Scoped responsibility</span>
          </article>
          <article>
            <small>VALIDATION</small>
            <strong>6 gates</strong>
            <span>Evidence before prose</span>
          </article>
          <article>
            <small>RETRY POLICY</small>
            <strong>2×</strong>
            <span>Targeted, then stop</span>
          </article>
        </section>

        <section className="workbench">
          <article className="card prompt-card">
            <div className="card-head">
              <div>
                <span className="card-index">01</span>
                <div>
                  <small>INPUT CONTRACT</small>
                  <h3>Business prompt</h3>
                </div>
              </div>
              <span className={`tag ${isCustomPrompt ? "tag-custom" : ""}`}>
                {isCustomPrompt ? "CUSTOM PROMPT · EDITABLE" : "BENCHMARK V1.0 · DEFAULT"}
              </span>
            </div>

            <div className="preset-selector">
              <small>ANALYTICAL PRESETS:</small>
              <div className="preset-chips">
                {promptPresets.map((preset) => (
                  <button
                    key={preset.id}
                    type="button"
                    className={`preset-chip ${prompt === preset.prompt ? "active" : ""}`}
                    onClick={() => setPrompt(preset.prompt)}
                    title={preset.description}
                  >
                    {preset.label}
                  </button>
                ))}
                {isCustomPrompt && (
                  <button
                    type="button"
                    className="preset-chip reset-chip"
                    onClick={() => setPrompt(defaultPrompt)}
                    title="Reset to benchmark default"
                  >
                    Reset
                  </button>
                )}
              </div>
            </div>

            <label className="sr-only" htmlFor="business-prompt">
              Business research prompt
            </label>
            <textarea
              id="business-prompt"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="Enter your custom agricultural research prompt (minimum 20 characters)..."
              disabled={["submitting", "accepted", "running"].includes(runState)}
            />
            <div className="prompt-footer">
              <small>
                {prompt.trim().length} / 20,000 chars{" "}
                {prompt.trim().length < 20 ? "(minimum 20 chars required)" : ""}
              </small>
            </div>

            <div className="prompt-meta">
              <div>
                <small>PROVIDER</small>
                <strong>Local Qwen</strong>
                <span>OpenAI-compatible API</span>
              </div>
              <div>
                <small>DATA SCOPE</small>
                <strong>IBGE PAM</strong>
                <span>Municipal · 2019–2024</span>
              </div>
              <div>
                <small>MODEL WINDOW</small>
                <strong>
                  {modelStatus?.contextLength
                    ? `${modelStatus.contextLength.toLocaleString("en-US")} tokens`
                    : "65K configured"}
                </strong>
                <span>{modelStatus?.quantization ?? "Pending metadata"}</span>
              </div>
            </div>
            <div className="action-row">
              <p>
                <span className={`status-dot ${connection}`} />
                {connection === "connected"
                  ? "Model endpoint verified. Graph submission is enabled."
                  : "Connect the model server to enable a benchmark run."}
              </p>
              <button className="primary-action" type="button" disabled={!canRun} onClick={runGraph}>
                {runState === "submitting" ? "Creating graph state…" : "Start benchmark run"}
                <ArrowIcon />
              </button>
            </div>
          </article>

          <article className="card method-card" id="method">
            <div className="card-head">
              <div>
                <span className="card-index">02</span>
                <div>
                  <small>EXECUTION DESIGN</small>
                  <h3>Validated agent graph</h3>
                </div>
              </div>
              <span className="tag">LANGGRAPH · TYPED STATE</span>
            </div>
            <div className="agent-graph" aria-label="Robust harness agent graph">
              <div className="graph-single">
                <AgentNode
                  id="01"
                  title="Business Analyst"
                  detail="Metric contract"
                  active={runState === "submitting" || latestNode === "business_analyst"}
                />
              </div>
              <span className="down-link" />
              <div className="graph-single">
                <AgentNode
                  id="02"
                  title="Data Profiler"
                  detail="Schema + quality"
                  active={latestNode === "data_profiler"}
                />
              </div>
              <div className="fork-link">
                <i />
                <i />
                <i />
              </div>
              <div className="parallel-row">
                <AgentNode
                  id="03"
                  title="SQL Analyst"
                  detail="Read-only DuckDB"
                  active={latestNode === "sql_analyst"}
                />
                <AgentNode
                  id="04"
                  title="Python Analyst"
                  detail="Sandboxed analysis"
                  active={latestNode === "python_analyst"}
                />
              </div>
              <div className="join-link">
                <i />
                <i />
                <i />
              </div>
              <div className="graph-single">
                <AgentNode
                  id="05"
                  title="Evidence Reconciler"
                  detail="Numeric agreement gate"
                  active={latestNode === "evidence_reconciler"}
                />
              </div>
              <span className="down-link" />
              <div className="final-row">
                <AgentNode
                  id="06"
                  title="Dashboard"
                  detail="Approved facts"
                  active={latestNode === "dashboard_engineer"}
                />
                <AgentNode
                  id="07"
                  title="Visual QA"
                  detail="Rendered review"
                  active={latestNode === "visual_reviewer"}
                />
                <AgentNode
                  id="08"
                  title="Final Editor"
                  detail="Cited narrative"
                  active={latestNode === "final_editor"}
                />
              </div>
            </div>
            <div className="method-foot">
              <span>Checkpointed state</span>
              <span>Role-scoped tools</span>
              <span>Bounded repair edges</span>
            </div>
          </article>
        </section>

        <section className="agent-config-card" id="agents">
          <div className="card-head">
            <div>
              <span className="card-index">03</span>
              <div>
                <small>ROLE ORCHESTRATION</small>
                <h3>Agent System Messages & Prompts (8 Roles)</h3>
              </div>
            </div>
            <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
              <span className={`tag ${customAgentsCount > 0 ? "tag-custom" : ""}`}>
                {customAgentsCount > 0
                  ? `${customAgentsCount} ROLE${customAgentsCount > 1 ? "S" : ""} MODIFIED`
                  : "ALL DEFAULT PROMPTS"}
              </span>
              {customAgentsCount > 0 && (
                <button
                  type="button"
                  className="preset-chip reset-chip"
                  onClick={handleResetAllAgentPrompts}
                  title="Reset all system messages to defaults"
                  style={{ padding: "3px 8px", fontSize: "8px" }}
                >
                  Reset All
                </button>
              )}
            </div>
          </div>
          <p style={{ fontSize: "11.5px", color: "var(--muted)", marginTop: "6px", marginBottom: "12px" }}>
            Inspect and customize the system message for each specialist agent. Custom prompts apply to
            subsequent runs in-memory without modifying the underlying repository files.
          </p>

          <div className="agent-config-grid">
            {defaultAgents.map((agent) => {
              const currentSystem = getAgentSystemPrompt(agent.id);
              const isModified = currentSystem !== agent.system;
              return (
                <div key={agent.id} className={`agent-prompt-box ${isModified ? "customized" : ""}`}>
                  <div className="agent-prompt-header">
                    <div>
                      <strong>
                        {agent.index} · {agent.role}
                      </strong>
                      <small style={{ display: "block" }}>id: {agent.id}</small>
                    </div>
                    <span className={`tag ${isModified ? "tag-custom" : ""}`} style={{ fontSize: "6.5px" }}>
                      {isModified ? "CUSTOMIZED" : "DEFAULT"}
                    </span>
                  </div>
                  <textarea
                    className="agent-prompt-textarea"
                    value={currentSystem}
                    onChange={(e) => handleAgentPromptChange(agent.id, e.target.value)}
                    placeholder={`System message for ${agent.role}...`}
                    disabled={["submitting", "accepted", "running"].includes(runState)}
                  />
                  <div className="agent-prompt-footer">
                    <small style={{ fontSize: "8px", color: "var(--muted)" }}>
                      {currentSystem.length} chars · Tools: {agent.tools.join(", ")}
                    </small>
                    {isModified && (
                      <button
                        type="button"
                        className="reset-btn"
                        onClick={() => handleResetAgentPrompt(agent.id)}
                      >
                        Reset
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </section>

        <section className="evidence-grid" id="evidence">
          <article className={`card run-card ${runState}`}>
            <div className="mini-head">
              <div>
                <span className="pulse-mark" />
                <div>
                  <small>RUN CONTROL</small>
                  <h3>Validation ledger</h3>
                </div>
              </div>
              <span>{runState.toUpperCase()}</span>
            </div>
            <p className="run-message" aria-live="polite">
              {runMessage}
            </p>
            <div className="gate-list">
              {gates.map((gate) => (
                <span key={gate}>
                  <i />
                  {gate}
                  <b>{runState === "error" ? "ERROR" : gateStatus(gate)}</b>
                </span>
              ))}
            </div>
            <div className="trace-list">
              <div>
                <span>RUN ID</span>
                <strong>{runId ?? "Not started"}</strong>
              </div>
              <div>
                <span>EVENTS</span>
                <strong>{runEvents.length} graph events</strong>
              </div>
            </div>
            {runId && (
              <div
                style={{
                  marginTop: "1.25rem",
                  paddingTop: "1rem",
                  borderTop: "1px solid rgba(255,255,255,0.08)",
                }}
              >
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
            <div className="mini-head">
              <div>
                <span className="dataset-mark">DB</span>
                <div>
                  <small>FIXED EVIDENCE BASE</small>
                  <h3>Dataset contract</h3>
                </div>
              </div>
              <span>PUBLIC DATA</span>
            </div>
            <div className="data-name">
              <strong>IBGE SIDRA · PAM 5457</strong>
              <span>Municipal Agricultural Production</span>
            </div>
            <div className="data-specs">
              <div>
                <small>PERIOD</small>
                <strong>2019–2024</strong>
              </div>
              <div>
                <small>ENGINE</small>
                <strong>DuckDB</strong>
              </div>
              <div>
                <small>CROPS</small>
                <strong>7 selected</strong>
              </div>
            </div>
            <p>Each approved claim will carry its query, artifact, dataset hash, unit, and numeric tolerance.</p>
          </article>
        </section>

        <footer className="page-footer">
          <span>MAE / HARNESS ENGINEERING CASE STUDY</span>
          <span>Robust condition · validated interface</span>
        </footer>
      </section>
    </main>
  );
}
