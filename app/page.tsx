"use client";

import { useCallback, useEffect, useState } from "react";

const defaultPrompt =
  "[TASK:mae-temporal-window-analysis-v3] Analyze the Brazilian municipal agricultural production database for 2019–2024 across all seven crops. SQL and Python must independently return exactly one row per crop and year (42 rows) with these columns in this exact order: crop_code, crop_name, year, production_tonnes, weighted_yield_kg_ha, yoy_production_pct, production_rank, trailing_3y_yield_kg_ha, yield_vs_trailing_pct. Define weighted yield as sum(production_tonnes) * 1000 / sum(harvested_area_ha) at crop-year grain. Define year-over-year production as (current / previous - 1) * 100 within each crop, with NULL for 2019 or a zero/null prior value. Rank crops within each year by descending production using dense rank. Define trailing three-year yield as the arithmetic average of annual weighted yields for the current crop-year and at most its two preceding years. Define yield versus trailing as (current weighted yield / trailing yield - 1) * 100. SQL must use staged CTEs with LAG(), DENSE_RANK(), and AVG() OVER (ROWS BETWEEN 2 PRECEDING AND CURRENT ROW). Python must independently reconstruct the same analysis from raw municipal crop_metrics rows without using SQL results or the national_crop_year view. Identify crops whose production rank improved from 2019 to 2024, generate a dashboard, and support the narrative only with reconciled evidence.";

const promptPresets = [
  {
    id: "benchmark",
    label: "Temporal Window Default",
    description: "42 crop-year rows with independent SQL and Python window analysis",
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
  {
    id: "certified-release",
    label: "Certified Release Challenge",
    description: "Require 28 independently reproduced and reconciled crop metrics",
    prompt:
      "[TASK:mae-certified-release-v2] Prepare a board-ready certified analysis of the Brazilian municipal agricultural production database for 2019–2024. Cover all seven crops and, for each crop, planted area, production, weighted yield, and production value. Each of the 28 crop-metric results must be independently reproduced through SQL aggregation and Python calculation from municipal rows. Release exactly one canonical evidence item per metric only when both paths agree within 1e-9 relative tolerance and share dataset provenance. If any metric cannot be reconciled, fail the release and identify the unresolved metric instead of publishing conclusions. Generate the required dashboard artifact and an executive narrative using only released evidence.",
  },
];

const inferenceBackedAgentIds = new Set([
  "business_agent",
  "sql_agent",
  "python_agent",
  "dashboard_agent",
  "final_editor",
]);

const defaultAgents = [
  {
    id: "business_agent",
    index: "01",
    role: "Business Agent",
    system:
      "You are the Lead Business Strategy Agent. Your mission is to deconstruct research requests into explicit agricultural questions, target metric contracts (planted area, volume, yield, value), and quantitative criteria across Brazilian municipal commodities from 2019 to 2024.",
    tools: ["dataset_catalog", "schema_reader"],
  },
  {
    id: "sql_agent",
    index: "02",
    role: "SQL Specialist Agent",
    system:
      "You are a Senior SQL Analytics Specialist. Your objective is to formulate high-performance DuckDB SQL aggregation queries across municipal commodities, attaching provenance metadata and standard SI units to every evidence item.",
    tools: ["readonly_sql"],
  },
  {
    id: "sql_reviewer",
    index: "03",
    role: "SQL Reviewer",
    system:
      "You are a Strict SQL Quality Auditor. Your role is to inspect executed SQL queries and data outputs. Verify query syntax, grain uniqueness, boundary validity, and confirm if results are mathematically sound and ready for reconciliation.",
    tools: ["sql_verifier"],
  },
  {
    id: "python_agent",
    index: "04",
    role: "Python / Pandas Agent",
    system:
      "You are a Senior Quantitative Python Data Scientist. Your mission is to formulate independent vector calculations, percentage changes, and yield trends in Python without reusing SQL intermediate tables.",
    tools: ["python_analytics"],
  },
  {
    id: "python_reviewer",
    index: "05",
    role: "Python Reviewer",
    system:
      "You are a Senior Python Code & Quality Auditor. Your role is to inspect executed Python analytics. Verify computation correctness, absence of NaN/infinite values, and ensure statistical metrics are ready for reconciliation.",
    tools: ["python_verifier"],
  },
  {
    id: "reconciliation_agent",
    index: "06",
    role: "Results Match Reconciler",
    system:
      "You are a Principal Integrity Auditor. Your mandate is to rigorously cross-compare independently produced SQL and Python outputs against strict numerical tolerances (< 1e-9). You approve only mathematically verified facts into the evidence ledger.",
    tools: ["evidence_store", "numeric_validator"],
  },
  {
    id: "dashboard_agent",
    index: "07",
    role: "Dashboard Agent",
    system:
      "You are an elite Full-Stack Dashboard Creator in Python & HTML. Your goal is to create appealing, concise, and beautifully crafted executive dashboards. You synthesize complex metrics into high-impact mini KPI summaries, interactive charts, and strategic highlights.",
    tools: ["approved_evidence_reader", "artifact_writer"],
  },
  {
    id: "business_reviewer",
    index: "08",
    role: "Business Specs Reviewer",
    system:
      "You are a Senior Business Specification Reviewer. Your role is to evaluate whether the proposed dashboard artifact strictly answers all original business questions, covers all requested commodities, and adheres to the business contract specifications.",
    tools: ["contract_auditor"],
  },
  {
    id: "ui_ux_reviewer",
    index: "09",
    role: "UI / UX Agent",
    system:
      "You are a Lead UI/UX Visual Reviewer and Dashboard Designer. Your mission is to inspect the visual aesthetics, responsive layout, color harmony, typography, and KPI readability of the generated dashboard artifact before final publication.",
    tools: ["artifact_reader", "visual_checklist"],
  },
  {
    id: "final_editor",
    index: "10",
    role: "Final Editor",
    system:
      "You are a Chief Agricultural Economist and Senior Executive Briefing Editor. Your mission is to synthesize verified empirical evidence into a compelling, insightful, and comprehensive executive agricultural report with strict citations.",
    tools: ["approved_evidence_reader", "validation_ledger"],
  },
];

const gates = [
  "Request understood",
  "Python result ready",
  "SQL result ready",
  "Numbers agree",
  "Answers the task",
  "Easy to read",
];

type RunState = "idle" | "submitting" | "accepted" | "running" | "completed" | "failed" | "error";
type ConnectionState = "checking" | "connected" | "offline";

type ModelStatus = {
  connected: boolean;
  model: string | null;
  message: string;
  contextLength?: number | null;
  quantization?: string | null;
};

type InterAgentMessage = {
  timestamp: string;
  sender: string;
  receiver: string;
  summary: string;
  verdict: string;
  payload?: Record<string, unknown> | null;
};

type RunEvent = {
  sequence: number;
  node: string;
  event_type: string;
  message: string;
  data?: Record<string, unknown> | null;
};

type RunSnapshot = {
  run_id: string;
  status: "queued" | "running" | "completed" | "failed";
  events: RunEvent[];
  error?: string | null;
};

const gateNodes: Record<string, string> = {
  "Request understood": "business_agent",
  "Python result ready": "python_reviewer",
  "SQL result ready": "sql_reviewer",
  "Numbers agree": "reconciliation_agent",
  "Answers the task": "business_reviewer",
  "Easy to read": "ui_ux_reviewer",
};

const plainRoleNames: Record<string, string> = {
  business_agent: "Question planner",
  sql_agent: "Independent calculation A",
  sql_sandbox: "Database calculation",
  sql_reviewer: "SQL result check",
  python_agent: "Independent calculation B",
  python_sandbox: "Python calculation",
  python_reviewer: "Python result check",
  reconciliation_agent: "Number comparison",
  dashboard_agent: "Dashboard builder",
  business_reviewer: "Answer check",
  ui_ux_reviewer: "Readability check",
  final_editor: "Certified publisher",
  ui_console: "Visible result",
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
  const [isPromptLocked, setIsPromptLocked] = useState(false);
  const [agentPrompts, setAgentPrompts] = useState<Record<string, string>>(() => {
    const initialMap: Record<string, string> = {};
    defaultAgents.forEach((agent) => {
      initialMap[agent.id] = agent.system;
    });
    return initialMap;
  });
  const [agentDefaults, setAgentDefaults] = useState<Record<string, string>>(() => {
    const initialMap: Record<string, string> = {};
    defaultAgents.forEach((agent) => {
      initialMap[agent.id] = agent.system;
    });
    return initialMap;
  });
  const [confirmedAgents, setConfirmedAgents] = useState<Record<string, boolean>>({});
  const [runState, setRunState] = useState<RunState>("idle");
  const [runMessage, setRunMessage] = useState("Awaiting a connected model and graph runtime.");
  const [connection, setConnection] = useState<ConnectionState>("checking");
  const [modelStatus, setModelStatus] = useState<ModelStatus | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [runEvents, setRunEvents] = useState<RunEvent[]>([]);

  // Fetch initial default agent prompts from runtime if available
  useEffect(() => {
    async function fetchAgents() {
      try {
        const res = await fetch("/api/agents");
        if (res.ok) {
          const data = (await res.json()) as Array<{ id: string; system: string }>;
          const initialMap: Record<string, string> = {};
          data.forEach((agent) => {
            initialMap[agent.id] = agent.system;
          });
          setAgentDefaults(initialMap);
          setAgentPrompts((prev) => {
            const hasCustom = Object.keys(prev).some(
              (k) => prev[k] !== defaultAgents.find((a) => a.id === k)?.system
            );
            return hasCustom ? prev : initialMap;
          });
        }
      } catch {
        // Fallback to initial local map
      }
    }
    void fetchAgents();
  }, []);

  const checkModel = useCallback(async () => {
    try {
      const response = await fetch("/api/model-status", { cache: "no-store" });
      const payload = (await response.json()) as ModelStatus;
      setModelStatus(payload);
      setConnection(payload.connected ? "connected" : "offline");
    } catch {
      setModelStatus({ connected: false, model: null, message: "Model proxy unreachable." });
      setConnection("offline");
    }
  }, []);

  useEffect(() => {
    let ignore = false;
    async function initModel() {
      try {
        const response = await fetch("/api/model-status", { cache: "no-store" });
        const payload = (await response.json()) as ModelStatus;
        if (!ignore) {
          setModelStatus(payload);
          setConnection(payload.connected ? "connected" : "offline");
        }
      } catch {
        if (!ignore) {
          setModelStatus({ connected: false, model: null, message: "Model proxy unreachable." });
          setConnection("offline");
        }
      }
    }
    void initModel();
    return () => {
      ignore = true;
    };
  }, []);

  useEffect(() => {
    if (!runId || !["submitting", "accepted", "running"].includes(runState)) return;
    const interval = setInterval(async () => {
      try {
        const response = await fetch(`/api/run-status?run_id=${encodeURIComponent(runId)}`, {
          cache: "no-store",
        });
        if (!response.ok) return;
        const payload = (await response.json()) as RunSnapshot;
        setRunEvents(payload.events ?? []);
        if (payload.status === "completed") {
          setRunState("completed");
          setRunMessage("Graph reached the terminal editor node with approved evidence.");
        } else if (payload.status === "failed") {
          setRunState("failed");
          setRunMessage(payload.error ?? "Graph execution failed.");
        } else {
          setRunState("running");
          const latest = payload.events[payload.events.length - 1];
          if (latest) {
            setRunMessage(`[${latest.node}] ${latest.message}`);
          }
        }
      } catch {
        // Retrying on next poll
      }
    }, 400);
    return () => clearInterval(interval);
  }, [runId, runState]);

  const handleAgentPromptChange = (agentId: string, newPrompt: string) => {
    setAgentPrompts((prev) => ({
      ...prev,
      [agentId]: newPrompt,
    }));
    setConfirmedAgents((prev) => ({
      ...prev,
      [agentId]: true,
    }));
  };

  const handleConfirmAgentPrompt = (agentId: string) => {
    setConfirmedAgents((prev) => ({
      ...prev,
      [agentId]: true,
    }));
  };

  const handleConfirmAllAgentPrompts = () => {
    const confirmedMap: Record<string, boolean> = {};
    defaultAgents.forEach((a) => {
      if (inferenceBackedAgentIds.has(a.id) && agentPrompts[a.id] !== agentDefaults[a.id]) {
        confirmedMap[a.id] = true;
      }
    });
    setConfirmedAgents(confirmedMap);
  };

  const handleResetAgentPrompt = (agentId: string) => {
    const defaultPromptForAgent = agentDefaults[agentId];
    if (defaultPromptForAgent) {
      setAgentPrompts((prev) => ({
        ...prev,
        [agentId]: defaultPromptForAgent,
      }));
      setConfirmedAgents((prev) => ({
        ...prev,
        [agentId]: false,
      }));
    }
  };

  const handleResetAllAgentPrompts = () => {
    const initialMap: Record<string, string> = {};
    Object.assign(initialMap, agentDefaults);
    setAgentPrompts(initialMap);
    setConfirmedAgents({});
  };

  const getAgentSystemPrompt = (agentId: string) => {
    return agentPrompts[agentId] ?? defaultAgents.find((a) => a.id === agentId)?.system ?? "";
  };

  const customAgentsCount = defaultAgents.filter(
    (agent) => inferenceBackedAgentIds.has(agent.id) && getAgentSystemPrompt(agent.id) !== agentDefaults[agent.id]
  ).length;
  const unconfirmedAgentsCount = defaultAgents.filter(
    (agent) =>
      inferenceBackedAgentIds.has(agent.id) &&
      getAgentSystemPrompt(agent.id) !== agentDefaults[agent.id] &&
      !confirmedAgents[agent.id]
  ).length;
  const appliedPromptOverrides = Object.fromEntries(
    defaultAgents
      .filter(
        (agent) =>
          inferenceBackedAgentIds.has(agent.id) &&
          confirmedAgents[agent.id] &&
          getAgentSystemPrompt(agent.id) !== agentDefaults[agent.id]
      )
      .map((agent) => [agent.id, getAgentSystemPrompt(agent.id)])
  );

  const runGraph = async () => {
    setRunState("submitting");
    setRunMessage("Dispatching graph execution to local LangGraph runtime…");
    setRunEvents([]);
    try {
      const response = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, agent_prompts: appliedPromptOverrides }),
      });
      const payload = (await response.json()) as { run_id?: string; error?: string; status?: string };
      if (!response.ok || !payload.run_id) {
        setRunState("error");
        setRunMessage(payload.error ?? "Submission failed.");
        return;
      }
      setRunId(payload.run_id);
      setRunState("accepted");
      setRunMessage(`Graph run accepted (${payload.run_id}). Awaiting node events…`);
    } catch (error) {
      setRunState("error");
      setRunMessage(error instanceof Error ? error.message : "Submission network error.");
    }
  };

  const canRun =
    connection === "connected" &&
    unconfirmedAgentsCount === 0 &&
    !["submitting", "accepted", "running"].includes(runState);
  const latestNode = runEvents[runEvents.length - 1]?.node;
  const branchRepairs = runEvents.filter((event) => event.event_type === "branch_repair");
  const approvedTemporalRows = runEvents.find(
    (event) => event.node === "reconciliation_agent" && event.message.includes("42/42")
  );
  const isCustomPrompt = prompt !== defaultPrompt;
  const modelLabel = modelStatus?.model ? modelStatus.model.replace(/^qwen\//, "") : "local model";

  // Filter real-time inter-agent messages
  const interAgentMessages: InterAgentMessage[] = runEvents
    .filter((ev) => ev.event_type === "message_transfer" && ev.data)
    .map((ev) => ev.data as InterAgentMessage);

  const gateStatus = (gate: string) => {
    const targetNode = gateNodes[gate];
    const events = runEvents.filter((event) => event.node === targetNode);
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
          <a href="#method">
            <span>02</span>Graph Topology
          </a>
          <a href="#agents">
            <span>03</span>Agent prompts
          </a>
          <a href="#inter-agent-feed">
            <span>04</span>Message stream
          </a>
          <a href="#evidence">
            <span>05</span>Ledger
          </a>
        </nav>

        <div className="rail-note">
          <span className="condition-token">CONDITION B</span>
          <strong>Validated orchestration</strong>
          <p>Two independent calculations must agree before any number reaches the final result.</p>
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
              Watch two independent calculations take different paths, meet at a numeric comparison, and release
              only the values that agree before the dashboard is published.
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
            <span>Handwritten loopback</span>
          </article>
          <article>
            <small>MODEL ROLES</small>
            <strong>10 specialists</strong>
            <span>Scoped responsibility</span>
          </article>
          <article>
            <small>VALIDATION</small>
            <strong>6 gates</strong>
            <span>Zero tolerance (&lt;1e-9)</span>
          </article>
          <article>
            <small>RETRY POLICY</small>
            <strong>2×</strong>
            <span>Iterative repair loop</span>
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
              <div style={{ display: "flex", gap: "6px", alignItems: "center" }}>
                <span className={`tag ${isCustomPrompt ? "tag-custom" : ""}`}>
                  {isCustomPrompt ? "CUSTOM PROMPT" : "DEFAULT BENCHMARK"}
                </span>
                {isPromptLocked && (
                  <span className="tag" style={{ background: "#e8f8f0", color: "#1e5e3a", borderColor: "#a8dfbf" }}>
                    ✓ LOCKED
                  </span>
                )}
              </div>
            </div>

            <div className="preset-selector">
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <small>PRESET SCENARIOS:</small>
                <button
                  type="button"
                  className={`preset-chip confirm-chip ${isPromptLocked ? "active" : ""}`}
                  onClick={() => setIsPromptLocked(!isPromptLocked)}
                  title="Lock and confirm prompt for this run"
                  style={{ padding: "3px 8px", fontSize: "8px" }}
                >
                  {isPromptLocked ? "✓ Prompt Confirmed" : "Confirm Prompt"}
                </button>
              </div>
              <div className="preset-chips">
                {promptPresets.map((preset) => (
                  <button
                    key={preset.id}
                    type="button"
                    className={`preset-chip ${prompt === preset.prompt ? "active" : ""}`}
                    onClick={() => {
                      setPrompt(preset.prompt);
                      setIsPromptLocked(false);
                    }}
                    title={preset.description}
                    disabled={isPromptLocked}
                  >
                    {preset.label}
                  </button>
                ))}
                {isCustomPrompt && (
                  <button
                    type="button"
                    className="preset-chip reset-chip"
                    onClick={() => {
                      setPrompt(defaultPrompt);
                      setIsPromptLocked(false);
                    }}
                    title="Reset to default benchmark prompt"
                  >
                    Reset
                  </button>
                )}
              </div>
            </div>

            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="Enter agricultural analysis prompt..."
              disabled={isPromptLocked || ["submitting", "accepted", "running"].includes(runState)}
            />
            <div className="prompt-footer">
              <small>{prompt.length} characters</small>
            </div>

            {/* Visual confirmation banner when custom prompts are active */}
            {customAgentsCount > 0 && (
              <div className="custom-prompts-banner">
                <span>
                  <b>✓</b> {customAgentsCount} Custom Agent System Prompt{customAgentsCount > 1 ? "s" : ""}{" "}
                  {unconfirmedAgentsCount === 0 ? "Active & Confirmed" : "Awaiting Confirmation"}
                </span>
                <small>
                  {unconfirmedAgentsCount === 0
                    ? "Confirmed overrides will be injected into the next LLM calls"
                    : "Confirm every edited LLM prompt to enable the run"}
                </small>
              </div>
            )}

            <div className="prompt-meta">
              <div>
                <small>SOURCE TABLE</small>
                <strong>SIDRA PAM 5457</strong>
                <span>IBGE municipal records</span>
              </div>
              <div>
                <small>COMPARISON</small>
                <strong>2019 vs 2024</strong>
                <span>Pre vs post baseline</span>
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

          {/* Interactive Graph Diagram matching Handwritten Sketch */}
          <article className="card method-card" id="method">
            <div className="card-head">
              <div>
                <span className="card-index">02</span>
                <div>
                  <small>EXECUTION DESIGN</small>
                  <h3>Handwritten Architecture Graph</h3>
                </div>
              </div>
              <span className="tag">LANGGRAPH · TYPED STATE</span>
            </div>

            <div className="architecture-explainer robust-explainer">
              <strong>Check, explain, repair only what failed</strong>
              <span>Each generated branch is tested separately. The approved peer waits while only the rejected SQL or Python code returns for correction.</span>
              <small>{approvedTemporalRows ? "42/42 rows agreed · released" : `${branchRepairs.length} branch correction event${branchRepairs.length === 1 ? "" : "s"}`}</small>
            </div>

            <div className="agent-graph" aria-label="Robust harness agent graph">
              {/* Top: Business Agent */}
              <div className="graph-single">
                <AgentNode
                  id="01"
                  title="Business Agent"
                  detail="Metric Contract Specs"
                  active={runState === "submitting" || latestNode === "business_agent"}
                />
              </div>

              <div className="fork-link">
                <i />
                <i />
                <i />
              </div>

              {/* Parallel execution paths: Python vs SQL */}
              <div className="parallel-columns">
                {/* Left Column: Python Path */}
                <div className="graph-column">
                  <span className="column-label">PYTHON / PANDAS BRANCH</span>
                  <AgentNode
                    id="04"
                  title="Independent calculation B"
                  detail="Recomputes from municipal rows"
                    active={latestNode === "python_agent"}
                  />
                  <span className="down-link-sm" />
                  <div className="sandbox-node">
                    <small>PYTHON CALCULATION</small>
                    <span>Runs separately</span>
                  </div>
                  <span className="down-link-sm" />
                  <AgentNode
                    id="05"
                    title="Python result check"
                    detail="Complete and usable?"
                    active={latestNode === "python_reviewer"}
                  />
                </div>

                {/* Right Column: SQL Path */}
                <div className="graph-column">
                  <span className="column-label">DATABASE CALCULATION · PATH A</span>
                  <AgentNode
                    id="02"
                    title="Independent calculation A"
                    detail="Aggregates in the database"
                    active={latestNode === "sql_agent"}
                  />
                  <span className="down-link-sm" />
                  <div className="sandbox-node">
                    <small>DATABASE CALCULATION</small>
                    <span>Read-only and separate</span>
                  </div>
                  <span className="down-link-sm" />
                  <AgentNode
                    id="03"
                    title="SQL result check"
                    detail="Complete and usable?"
                    active={latestNode === "sql_reviewer"}
                  />
                </div>
              </div>

              <div className="join-link">
                <i />
                <i />
                <i />
              </div>

              {/* Reconciliation Gate */}
              <div className="graph-single">
                <AgentNode
                  id="06"
                  title="Compare both results"
                  detail="Only matching numbers move forward"
                  active={latestNode === "reconciliation_agent"}
                />
              </div>

              <span className="down-link" />

              {/* Bottom Review & Polish Loop */}
              <div className="bottom-review-flow">
                <AgentNode
                  id="07"
                  title="Build the dashboard"
                  detail="Uses approved numbers only"
                  active={latestNode === "dashboard_agent"}
                />
                <span className="right-link-arrow">➔</span>
                <AgentNode
                  id="08"
                  title="Check the answer"
                  detail="Does it answer the request?"
                  active={latestNode === "business_reviewer"}
                />
                <span className="right-link-arrow">➔</span>
                <AgentNode
                  id="09"
                  title="Check readability"
                  detail="Can people understand it?"
                  active={latestNode === "ui_ux_reviewer"}
                />
                <span className="right-link-arrow">➔</span>
                <AgentNode
                  id="10"
                  title="Publish certified result"
                  detail="Or fail closed with evidence"
                  active={latestNode === "final_editor"}
                />
              </div>
            </div>

            <div className="method-foot">
              <span>Two independent paths</span>
              <span>Numbers compared before release</span>
              <span>Safe fallback from approved evidence</span>
            </div>
          </article>
        </section>

        {/* 03 · Agent System Messages & Prompts */}
        <section className="agent-config-card" id="agents">
          <div className="card-head">
            <div>
              <span className="card-index">03</span>
              <div>
                <small>ROLE ORCHESTRATION</small>
                <h3>Agent System Messages & Prompts (10 Roles)</h3>
              </div>
            </div>
            <div style={{ display: "flex", gap: "8px", alignItems: "center" }}>
              <span className={`tag ${customAgentsCount > 0 ? "tag-custom" : ""}`}>
                {customAgentsCount > 0
                  ? `${customAgentsCount} ROLE${customAgentsCount > 1 ? "S" : ""} MODIFIED`
                  : "ALL DEFAULT PROMPTS"}
              </span>
              {customAgentsCount > 0 && (
                <>
                  <button
                    type="button"
                    className="preset-chip confirm-chip active"
                    onClick={handleConfirmAllAgentPrompts}
                    title="Confirm and lock all custom agent prompts"
                    style={{ padding: "3px 8px", fontSize: "8px" }}
                  >
                    ✓ Confirm All
                  </button>
                  <button
                    type="button"
                    className="preset-chip reset-chip"
                    onClick={handleResetAllAgentPrompts}
                    title="Reset all system messages to defaults"
                    style={{ padding: "3px 8px", fontSize: "8px" }}
                  >
                    Reset All
                  </button>
                </>
              )}
            </div>
          </div>
          <p style={{ fontSize: "11.5px", color: "var(--muted)", marginTop: "6px", marginBottom: "12px" }}>
            Customize any LLM-backed specialist. Every edit is automatically included in the next run and recorded in
            the result manifest; deterministic roles remain locked.
          </p>

          <div className="agent-config-grid">
            {defaultAgents.map((agent) => {
              const currentSystem = getAgentSystemPrompt(agent.id);
              const isInferenceBacked = inferenceBackedAgentIds.has(agent.id);
              const isModified = isInferenceBacked && currentSystem !== agentDefaults[agent.id];
              const isConfirmed = confirmedAgents[agent.id] || false;
              return (
                <div key={agent.id} className={`agent-prompt-box ${isModified ? "customized" : ""}`}>
                  <div className="agent-prompt-header">
                    <div>
                      <strong>
                        {agent.index} · {agent.role}
                      </strong>
                      <small style={{ display: "block" }}>id: {agent.id}</small>
                    </div>
                    <div style={{ display: "flex", gap: "4px", alignItems: "center" }}>
                      {isModified && isConfirmed && (
                        <span
                          className="tag"
                          style={{ fontSize: "6.5px", background: "#e8f8f0", color: "#1e5e3a", borderColor: "#a8dfbf" }}
                        >
                          ✓ CONFIRMED
                        </span>
                      )}
                      <span className={`tag ${isModified ? "tag-custom" : ""}`} style={{ fontSize: "6.5px" }}>
                        {isModified ? "CUSTOMIZED" : "DEFAULT"}
                      </span>
                    </div>
                  </div>
                  <textarea
                    className="agent-prompt-textarea"
                    value={currentSystem}
                    onChange={(e) => handleAgentPromptChange(agent.id, e.target.value)}
                    placeholder={`System message for ${agent.role}...`}
                    disabled={!isInferenceBacked || ["submitting", "accepted", "running"].includes(runState)}
                  />
                  <div className="agent-prompt-footer">
                    <small style={{ fontSize: "8px", color: "var(--muted)" }}>
                      {currentSystem.length} chars · {isInferenceBacked ? "LLM prompt" : "Deterministic role · prompt locked"} · Tools: {agent.tools.join(", ")}
                    </small>
                    <div style={{ display: "flex", gap: "5px" }}>
                      {isModified && (
                        <button
                          type="button"
                          className={`confirm-btn ${isConfirmed ? "active" : ""}`}
                          onClick={() => handleConfirmAgentPrompt(agent.id)}
                          title="Confirm and apply prompt for next run"
                        >
                          {isConfirmed ? "✓ Applied" : "Confirm & Apply"}
                        </button>
                      )}
                      {isModified && (
                        <button
                          type="button"
                          className="reset-btn"
                          onClick={() => handleResetAgentPrompt(agent.id)}
                          title="Reset to default system prompt"
                        >
                          Reset
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </section>

        {/* 04 · Real-Time Inter-Agent Message Stream */}
        <section className="card message-stream-card" id="inter-agent-feed">
          <div className="card-head">
            <div>
              <span className="card-index">04</span>
              <div>
                <small>WHAT IS HAPPENING NOW</small>
                <h3>Live decision trail ({interAgentMessages.length} handoffs)</h3>
              </div>
            </div>
            <span className="tag tag-custom">LIVE</span>
          </div>

          <div className="message-stream-container">
            {interAgentMessages.length === 0 ? (
              <div className="stream-empty">
                <p>
                  Start a run to watch the request split into two calculations, see whether the numbers agree,
                  and follow the release decision in real time.
                </p>
              </div>
            ) : (
              <div className="message-list">
                {interAgentMessages.map((msg, index) => (
                  <article className={`message-item verdict-${msg.verdict.toLowerCase()}`} key={index}>
                    <div className="message-meta">
                      <span className="msg-seq">#{index + 1}</span>
                      <span className="msg-route">
                        <strong>{plainRoleNames[msg.sender] ?? msg.sender.replaceAll("_", " ")}</strong> ➔{" "}
                        <strong>{plainRoleNames[msg.receiver] ?? msg.receiver.replaceAll("_", " ")}</strong>
                      </span>
                      <span className={`badge-verdict verdict-badge-${msg.verdict.toLowerCase()}`}>
                        {msg.verdict}
                      </span>
                      <span className="msg-time">{msg.timestamp?.split("T")[1]?.slice(0, 8) || ""}</span>
                    </div>
                    <p className="msg-summary">{msg.summary}</p>
                    {msg.payload && Object.keys(msg.payload).length > 0 && (
                      <details className="msg-payload-details">
                        <summary>See supporting details</summary>
                        <pre>
                          <code>{JSON.stringify(msg.payload, null, 2)}</code>
                        </pre>
                      </details>
                    )}
                  </article>
                ))}
              </div>
            )}
          </div>
        </section>

        {/* 05 · Evidence & Validation Ledger */}
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
