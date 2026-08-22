from __future__ import annotations

import hashlib
import html
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import duckdb

from .contracts import EvidenceItem, ValidationCheck, utc_now
from .dataset import file_sha256

METRICS = {
    "planted_area_ha": ("ha", 3),
    "production_tonnes": ("tonnes", 4),
    "yield_kg_ha": ("kg/ha", 5),
    "production_value_thousand_brl": ("thousand BRL", 6),
}
NATIONAL_QUERY = """
SELECT crop_code, crop_name, year, planted_area_ha, production_tonnes,
       yield_kg_ha, production_value_thousand_brl
FROM national_crop_year
ORDER BY crop_code, year
""".strip()


def profile_dataset(dataset_path: Path) -> dict[str, Any]:
    connection = duckdb.connect(str(dataset_path), read_only=True)
    try:
        row = connection.execute(
            """
            SELECT count(*) AS rows,
                   count(DISTINCT municipality_code) AS municipalities,
                   count(DISTINCT crop_code) AS crops,
                   min(year) AS first_year,
                   max(year) AS last_year,
                   count(*) - count(DISTINCT (municipality_code, year, crop_code)) AS duplicate_keys,
                   avg((planted_area_ha IS NULL)::INTEGER) AS planted_null_rate,
                   avg((harvested_area_ha IS NULL)::INTEGER) AS harvested_null_rate,
                   avg((production_tonnes IS NULL)::INTEGER) AS production_null_rate,
                   avg((production_value_thousand_brl IS NULL)::INTEGER) AS value_null_rate,
                   (SELECT count(*) FROM source_chunks) AS source_chunks
            FROM crop_metrics
            """
        ).fetchone()
        schema = connection.execute("DESCRIBE crop_metrics").fetchall()
    finally:
        connection.close()
    manifest_path = dataset_path.with_suffix(".manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    return {
        "rows": row[0],
        "municipalities": row[1],
        "crops": row[2],
        "first_year": row[3],
        "last_year": row[4],
        "duplicate_keys": row[5],
        "null_rates": {
            "planted_area": row[6],
            "harvested_area": row[7],
            "production": row[8],
            "production_value": row[9],
        },
        "schema": [{"column": item[0], "type": item[1]} for item in schema],
        "source_chunks": row[10],
        "manifest_present": bool(manifest),
        "manifest_mode": manifest.get("mode"),
        "dataset_sha256": file_sha256(dataset_path),
    }


def _evidence_from_aggregates(
    rows: list[tuple[Any, ...]], branch: str, method: str, dataset_sha256: str
) -> list[EvidenceItem]:
    grouped: dict[str, dict[int, tuple[Any, ...]]] = defaultdict(dict)
    crop_names: dict[str, str] = {}
    for row in rows:
        crop_code, crop_name, year = str(row[0]), str(row[1]), int(row[2])
        grouped[crop_code][year] = row
        crop_names[crop_code] = crop_name
    evidence: list[EvidenceItem] = []
    for crop_code, by_year in sorted(grouped.items()):
        years = sorted(by_year)
        if len(years) < 2:
            continue
        start_year, end_year = years[0], years[-1]
        for metric, (unit, index) in METRICS.items():
            start_raw, end_raw = by_year[start_year][index], by_year[end_year][index]
            start_value = float(start_raw) if start_raw is not None else None
            end_value = float(end_raw) if end_raw is not None else None
            change = None
            if start_value not in (None, 0.0) and end_value is not None:
                change = (end_value - start_value) / abs(start_value) * 100.0
            evidence.append(
                EvidenceItem(
                    evidence_id=f"{branch}:{crop_code}:{metric}",
                    match_key=f"{crop_code}:{metric}",
                    branch=branch,
                    crop_code=crop_code,
                    crop_name=crop_names[crop_code],
                    metric=metric,
                    start_year=start_year,
                    end_year=end_year,
                    start_value=start_value,
                    end_value=end_value,
                    change_percent=change,
                    unit=unit,
                    method=method,
                    provenance={
                        "source_table": 5457,
                        "grain": "national crop-year",
                        "dataset_sha256": dataset_sha256,
                        "method_sha256": hashlib.sha256(method.encode("utf-8")).hexdigest(),
                    },
                )
            )
    return evidence


def run_sql_analysis(dataset_path: Path) -> list[EvidenceItem]:
    connection = duckdb.connect(str(dataset_path), read_only=True)
    try:
        rows = connection.execute(NATIONAL_QUERY).fetchall()
    finally:
        connection.close()
    return _evidence_from_aggregates(rows, "sql", NATIONAL_QUERY, file_sha256(dataset_path))


def run_python_analysis(dataset_path: Path) -> list[EvidenceItem]:
    connection = duckdb.connect(str(dataset_path), read_only=True)
    try:
        raw_rows = connection.execute(
            """
            SELECT crop_code, crop_name, year, planted_area_ha, harvested_area_ha,
                   production_tonnes, production_value_thousand_brl
            FROM crop_metrics
            ORDER BY crop_code, year, municipality_code
            """
        ).fetchall()
    finally:
        connection.close()
    aggregates: dict[tuple[str, str, int], dict[str, float]] = defaultdict(
        lambda: {"planted_area": 0.0, "harvested_area": 0.0, "production": 0.0, "value": 0.0}
    )
    for crop_code, crop_name, year, planted_area, harvested_area, production, value in raw_rows:
        totals = aggregates[(str(crop_code), str(crop_name), int(year))]
        if planted_area is not None:
            totals["planted_area"] += float(planted_area)
        if harvested_area is not None:
            totals["harvested_area"] += float(harvested_area)
        if production is not None:
            totals["production"] += float(production)
        if value is not None:
            totals["value"] += float(value)
    rows: list[tuple[Any, ...]] = []
    for (crop_code, crop_name, year), totals in sorted(aggregates.items()):
        weighted_yield = (
            totals["production"] * 1000.0 / totals["harvested_area"] if totals["harvested_area"] else None
        )
        rows.append(
            (
                crop_code,
                crop_name,
                year,
                totals["planted_area"],
                totals["production"],
                weighted_yield,
                totals["value"],
            )
        )
    return _evidence_from_aggregates(
        rows,
        "python",
        "Python sums over municipal fact rows",
        file_sha256(dataset_path),
    )


def _relative_error(left: float | None, right: float | None) -> float:
    if left is None or right is None:
        return 0.0 if left is right else math.inf
    denominator = max(abs(left), abs(right), 1.0)
    return abs(left - right) / denominator


def reconcile_evidence(
    sql_evidence: list[EvidenceItem],
    python_evidence: list[EvidenceItem],
    tolerance: float,
) -> tuple[list[ValidationCheck], list[EvidenceItem]]:
    python_by_key = {item.match_key: item for item in python_evidence}
    checks: list[ValidationCheck] = []
    approved: list[EvidenceItem] = []
    for sql_item in sql_evidence:
        python_item = python_by_key.get(sql_item.match_key)
        start_error = _relative_error(sql_item.start_value, python_item.start_value if python_item else None)
        end_error = _relative_error(sql_item.end_value, python_item.end_value if python_item else None)
        same_contract = bool(
            python_item
            and sql_item.unit == python_item.unit
            and sql_item.start_year == python_item.start_year
            and sql_item.end_year == python_item.end_year
        )
        passed = same_contract and max(start_error, end_error) <= tolerance
        checks.append(
            ValidationCheck(
                check_id=f"agreement:{sql_item.match_key}",
                passed=passed,
                message=(
                    "SQL and Python agree within tolerance." if passed else "Independent results disagree."
                ),
                details={
                    "start_relative_error": start_error,
                    "end_relative_error": end_error,
                    "same_unit_and_period": same_contract,
                },
            )
        )
        if passed:
            approved.append(sql_item)
    return checks, approved


def execute_readonly_sql(dataset_path: Path, query: str, max_rows: int = 500) -> dict[str, Any]:
    normalized = query.strip().rstrip(";")
    lowered = re.sub(r"\s+", " ", normalized.lower())
    if not (lowered.startswith("select ") or lowered.startswith("with ")):
        raise ValueError("Only SELECT or WITH queries are allowed.")
    forbidden = re.compile(
        r"\b(insert|update|delete|drop|alter|create|attach|detach|copy|export|import|install|load|pragma)\b"
    )
    if forbidden.search(lowered) or ";" in normalized:
        raise ValueError("The query contains a forbidden operation.")
    connection = duckdb.connect(str(dataset_path), read_only=True)
    try:
        cursor = connection.execute(f"SELECT * FROM ({normalized}) AS bounded_query LIMIT {max_rows + 1}")
        columns = [description[0] for description in cursor.description]
        rows = cursor.fetchall()
    finally:
        connection.close()
    return {
        "columns": columns,
        "rows": [list(row) for row in rows[:max_rows]],
        "truncated": len(rows) > max_rows,
    }


def _format_number(value: float | None, unit: str) -> str:
    if value is None:
        return "—"
    if unit == "ha" or unit == "tonnes":
        return f"{value:,.0f}"
    if unit == "kg/ha":
        return f"{value:,.1f}"
    if unit == "thousand BRL":
        return f"R$ {value:,.0f}k"
    return f"{value:,.2f}"


def _metric_label(metric: str) -> str:
    labels = {
        "planted_area_ha": "Planted Area",
        "production_tonnes": "Production",
        "yield_kg_ha": "Yield",
        "production_value_thousand_brl": "Production Value",
    }
    return labels.get(metric, metric.replace("_", " ").title())


def render_dashboard_html(
    payload: dict[str, Any],
    narrative: str | None = None,
) -> str:
    title = str(payload.get("title", "Brazilian Municipal Crop Intelligence"))
    source = str(payload.get("source", "IBGE SIDRA PAM table 5457"))
    evidence_list: list[dict[str, Any]] = payload.get("evidence", [])
    validation_list: list[dict[str, Any]] = payload.get("validation", [])
    harness_name = str(payload.get("harness", "MAE Agricultural Benchmark"))
    run_id = str(payload.get("run_id", "local-run"))
    created_at = str(payload.get("created_at", utc_now()))

    # Aggregate metric summaries
    metric_totals: dict[str, dict[str, float]] = defaultdict(lambda: {"start": 0.0, "end": 0.0})
    for item in evidence_list:
        m = item.get("metric", "")
        if (
            item.get("start_value") is not None
            and item.get("end_value") is not None
            and m in ("planted_area_ha", "production_tonnes", "production_value_thousand_brl")
        ):
            metric_totals[m]["start"] += float(item["start_value"])
            metric_totals[m]["end"] += float(item["end_value"])

    kpi_cards = []
    for metric_key, label, unit in [
        ("planted_area_ha", "Total Planted Area", "ha"),
        ("production_tonnes", "Total Production", "tonnes"),
        ("production_value_thousand_brl", "Total Production Value", "thousand BRL"),
    ]:
        start = metric_totals[metric_key]["start"]
        end = metric_totals[metric_key]["end"]
        change = ((end - start) / start * 100.0) if start > 0 else 0.0
        sign = "+" if change >= 0 else ""
        css_class = "positive" if change >= 0 else "negative"
        fmt_end = _format_number(end, unit)
        fmt_start = _format_number(start, unit)
        kpi_cards.append(
            f"""<div class="kpi-card">
              <span class="kpi-label">{html.escape(label)}</span>
              <div class="kpi-value">{fmt_end} <span class="kpi-unit">{html.escape(unit)}</span></div>
              <div class="kpi-trend {css_class}">{sign}{change:.1f}% vs 2019 ({fmt_start})</div>
            </div>"""
        )

    # Chart data: group changes by crop
    crops = sorted({item.get("crop_name", "") for item in evidence_list if item.get("crop_name")})
    chart_bars = []
    for crop in crops:
        crop_items = [it for it in evidence_list if it.get("crop_name") == crop]
        for it in crop_items:
            m = it.get("metric", "")
            chg = it.get("change_percent")
            if chg is not None:
                val = float(chg)
                width = min(abs(val), 100.0)
                bar_class = "bar-positive" if val >= 0 else "bar-negative"
                sign = "+" if val >= 0 else ""
                unit_esc = html.escape(it.get("unit", ""))
                crop_esc = html.escape(crop)
                metric_esc = html.escape(m)
                m_label = _metric_label(m)
                chart_bars.append(
                    f"""<div class="chart-row" data-crop="{crop_esc}" data-metric="{metric_esc}">
                      <div class="crop-meta">
                        <strong>{crop_esc}</strong>
                        <small>{m_label} ({unit_esc})</small>
                      </div>
                      <div class="bar-container">
                        <div class="bar-fill {bar_class}" style="width: {width:.1f}%;"></div>
                      </div>
                      <span class="bar-value {bar_class}">{sign}{val:.1f}%</span>
                    </div>"""
                )

    # Evidence Table Rows
    evidence_rows = []
    for it in evidence_list:
        ev_id = it.get("evidence_id", "")
        crop = it.get("crop_name", "")
        m = it.get("metric", "")
        unit = it.get("unit", "")
        st_val = it.get("start_value")
        end_val = it.get("end_value")
        chg = it.get("change_percent")
        sign = "+" if chg is not None and chg >= 0 else ""
        chg_str = f"{sign}{chg:.2f}%" if chg is not None else "—"
        prov = it.get("provenance", {})
        ds_hash = prov.get("dataset_sha256", "")[:12] + "…" if prov.get("dataset_sha256") else "—"
        method_str = html.escape(it.get("method", "").strip())
        badge_type = "badge-pos" if chg and chg >= 0 else "badge-neg"
        evidence_rows.append(
            f"""<tr data-crop="{html.escape(crop)}" data-metric="{html.escape(m)}">
              <td><code>{html.escape(ev_id)}</code></td>
              <td><strong>{html.escape(crop)}</strong></td>
              <td>{_metric_label(m)}</td>
              <td><span class="badge unit-badge">{html.escape(unit)}</span></td>
              <td>{_format_number(st_val, unit)}</td>
              <td>{_format_number(end_val, unit)}</td>
              <td><span class="badge {badge_type}">{chg_str}</span></td>
              <td>
                <details>
                  <summary>Query</summary>
                  <pre><code>{method_str}\n\nSHA: {ds_hash}</code></pre>
                </details>
              </td>
            </tr>"""
        )

    # Validation Checks Rows
    val_rows = []
    for v in validation_list:
        passed = v.get("passed", False)
        badge_cls = "badge-pass" if passed else "badge-fail"
        status_txt = "PASSED" if passed else "FAILED"
        val_rows.append(
            f"""<tr>
              <td><code>{html.escape(v.get('check_id', ''))}</code></td>
              <td><span class="badge {badge_cls}">{status_txt}</span></td>
              <td>{html.escape(v.get('message', ''))}</td>
            </tr>"""
        )

    narrative_section = ""
    if narrative:
        # Highlight evidence IDs in narrative
        highlighted = re.sub(
            r"\[(sql:[^\]]+|python:[^\]]+)\]",
            r'<span class="evidence-tag">[\1]</span>',
            html.escape(narrative),
        )
        highlighted_lines = "\n".join(f"<p>{line}</p>" for line in highlighted.split("\n") if line.strip())
        narrative_section = f"""<section class="card" id="narrative">
          <div class="card-header">
            <h2>Executive Report Narrative</h2>
            <span class="badge badge-accent">Provenance-Linked</span>
          </div>
          <div class="narrative-content">
            {highlighted_lines}
          </div>
        </section>"""

    rendered_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{html.escape(title)} · Run Artifact</title>
  <style>
    :root {{
      --bg: #090d16;
      --surface: #111726;
      --surface-border: #1e293b;
      --surface-hover: #172033;
      --text: #f1f5f9;
      --text-muted: #94a3b8;
      --accent: #38bdf8;
      --accent-glow: rgba(56, 189, 248, 0.15);
      --positive: #10b981;
      --positive-glow: rgba(16, 185, 129, 0.15);
      --negative: #f43f5e;
      --negative-glow: rgba(244, 63, 94, 0.15);
      --font: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: var(--bg);
      color: var(--text);
      font-family: var(--font);
      line-height: 1.5;
      padding: 2rem;
      max-width: 1300px;
      margin: 0 auto;
    }}
    header.dashboard-header {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 1rem;
      padding-bottom: 1.5rem;
      border-bottom: 1px solid var(--surface-border);
      margin-bottom: 2rem;
    }}
    .header-titles h1 {{
      font-size: 1.75rem;
      font-weight: 700;
      letter-spacing: -0.02em;
      color: #fff;
    }}
    .header-titles p {{
      color: var(--text-muted);
      font-size: 0.9rem;
      margin-top: 0.25rem;
    }}
    .header-meta {{
      display: flex;
      gap: 0.5rem;
      flex-wrap: wrap;
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      padding: 0.25rem 0.6rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 600;
      letter-spacing: 0.02em;
    }}
    .badge-accent {{ background: var(--accent-glow); color: var(--accent); border: 1px solid var(--accent); }}
    .badge-pass {{
      background: var(--positive-glow);
      color: var(--positive);
      border: 1px solid var(--positive);
    }}
    .badge-fail {{
      background: var(--negative-glow);
      color: var(--negative);
      border: 1px solid var(--negative);
    }}
    .badge-pos {{ background: var(--positive-glow); color: var(--positive); }}
    .badge-neg {{ background: var(--negative-glow); color: var(--negative); }}
    .unit-badge {{ background: #1e293b; color: var(--text-muted); }}
    
    .card {{
      background: var(--surface);
      border: 1px solid var(--surface-border);
      border-radius: 12px;
      padding: 1.5rem;
      margin-bottom: 2rem;
      box-shadow: 0 4px 20px rgba(0,0,0,0.3);
    }}
    .card-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1.25rem;
      padding-bottom: 0.75rem;
      border-bottom: 1px solid var(--surface-border);
    }}
    .card-header h2 {{
      font-size: 1.25rem;
      font-weight: 600;
    }}
    
    /* KPI Grid */
    #kpis {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 1rem;
      margin-bottom: 2rem;
    }}
    .kpi-card {{
      background: var(--surface);
      border: 1px solid var(--surface-border);
      border-radius: 12px;
      padding: 1.25rem;
    }}
    .kpi-label {{
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--text-muted);
      font-weight: 600;
    }}
    .kpi-value {{
      font-size: 1.75rem;
      font-weight: 700;
      margin: 0.5rem 0;
      color: #fff;
      font-family: var(--mono);
    }}
    .kpi-unit {{
      font-size: 0.9rem;
      color: var(--text-muted);
      font-weight: 400;
    }}
    .kpi-trend {{
      font-size: 0.85rem;
      font-weight: 600;
    }}
    .positive {{ color: var(--positive); }}
    .negative {{ color: var(--negative); }}
    
    /* Charts */
    .filter-bar {{
      display: flex;
      gap: 0.5rem;
      flex-wrap: wrap;
      margin-bottom: 1rem;
    }}
    .filter-btn {{
      background: var(--surface);
      border: 1px solid var(--surface-border);
      color: var(--text-muted);
      padding: 0.4rem 0.8rem;
      border-radius: 6px;
      font-size: 0.8rem;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.2s;
    }}
    .filter-btn:hover, .filter-btn.active {{
      background: var(--accent);
      color: #000;
      border-color: var(--accent);
    }}
    .chart-container {{
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
      margin-top: 1rem;
    }}
    .chart-row {{
      display: grid;
      grid-template-columns: 220px 1fr 90px;
      align-items: center;
      gap: 1rem;
      padding: 0.5rem 0.75rem;
      background: rgba(255, 255, 255, 0.02);
      border-radius: 6px;
    }}
    .crop-meta strong {{
      display: block;
      font-size: 0.9rem;
    }}
    .crop-meta small {{
      color: var(--text-muted);
      font-size: 0.75rem;
    }}
    .bar-container {{
      background: #1e293b;
      height: 14px;
      border-radius: 7px;
      overflow: hidden;
      position: relative;
    }}
    .bar-fill {{
      height: 100%;
      border-radius: 7px;
      transition: width 0.3s ease;
    }}
    .bar-positive {{ background: linear-gradient(90deg, #059669, #10b981); color: var(--positive); }}
    .bar-negative {{ background: linear-gradient(90deg, #e11d48, #f43f5e); color: var(--negative); }}
    .bar-value {{
      font-family: var(--mono);
      font-size: 0.85rem;
      font-weight: 600;
      text-align: right;
    }}
    
    /* Tables */
    .table-wrapper {{
      overflow-x: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
      text-align: left;
    }}
    th, td {{
      padding: 0.75rem 1rem;
      border-bottom: 1px solid var(--surface-border);
    }}
    th {{
      color: var(--text-muted);
      font-weight: 600;
      text-transform: uppercase;
      font-size: 0.75rem;
      letter-spacing: 0.05em;
      background: rgba(0,0,0,0.2);
    }}
    tr:hover td {{
      background: var(--surface-hover);
    }}
    code {{
      font-family: var(--mono);
      font-size: 0.8rem;
      color: var(--accent);
    }}
    pre {{
      background: #060910;
      padding: 0.5rem;
      border-radius: 4px;
      overflow-x: auto;
      margin-top: 0.25rem;
      color: var(--text-muted);
      font-size: 0.75rem;
    }}
    details summary {{
      cursor: pointer;
      color: var(--accent);
    }}
    
    /* Narrative */
    .narrative-content p {{
      margin-bottom: 1rem;
      color: #e2e8f0;
      font-size: 0.95rem;
      line-height: 1.7;
    }}
    .evidence-tag {{
      background: rgba(56, 189, 248, 0.15);
      color: #38bdf8;
      border: 1px solid rgba(56, 189, 248, 0.4);
      padding: 0.1rem 0.4rem;
      border-radius: 4px;
      font-family: var(--mono);
      font-size: 0.8rem;
      font-weight: 600;
    }}

    @media (max-width: 768px) {{
      body {{ padding: 1rem; }}
      .chart-row {{ grid-template-columns: 1fr; }}
    }}
    @media print {{
      body {{ background: #fff; color: #000; padding: 0; }}
      .card {{ border: 1px solid #ccc; box-shadow: none; page-break-inside: avoid; }}
      .filter-bar {{ display: none; }}
    }}
  </style>
</head>
<body>
  <header class="dashboard-header">
    <div class="header-titles">
      <h1>{html.escape(title)}</h1>
      <p>Source: {html.escape(source)} · Period: 2019–2024</p>
    </div>
    <div class="header-meta">
      <span class="badge badge-accent">{html.escape(harness_name)}</span>
      <span class="badge unit-badge">Run ID: {html.escape(run_id)}</span>
      <span class="badge unit-badge">{html.escape(created_at)}</span>
    </div>
  </header>

  <section id="kpis">
    {"".join(kpi_cards)}
  </section>

  <section class="card" id="charts">
    <div class="card-header">
      <h2>Municipal Agricultural Shifts (2019–2024)</h2>
      <div class="filter-bar" id="chart-filters">
        <button class="filter-btn active" onclick="filterCharts('all')">All Metrics</button>
        <button class="filter-btn" onclick="filterCharts('planted_area_ha')">Planted Area</button>
        <button class="filter-btn" onclick="filterCharts('production_tonnes')">Production</button>
        <button class="filter-btn" onclick="filterCharts('yield_kg_ha')">Yield</button>
        <button class="filter-btn" onclick="filterCharts('production_value_thousand_brl')">Value</button>
      </div>
    </div>
    <div class="chart-container" id="chart-container">
      {"".join(chart_bars)}
    </div>
  </section>

  {narrative_section}

  <section class="card" id="evidence-ledger">
    <div class="card-header">
      <h2>Approved Evidence & Provenance Ledger</h2>
      <span class="badge badge-pass">{len(evidence_list)} Verified Claims</span>
    </div>
    <div class="table-wrapper">
      <table id="evidence-table">
        <thead>
          <tr>
            <th>Evidence ID</th>
            <th>Crop</th>
            <th>Metric</th>
            <th>Unit</th>
            <th>2019 Value</th>
            <th>2024 Value</th>
            <th>Change %</th>
            <th>Method & Hash</th>
          </tr>
        </thead>
        <tbody>
          {"".join(evidence_rows)}
        </tbody>
      </table>
    </div>
  </section>

  <section class="card" id="validation-ledger">
    <div class="card-header">
      <h2>Validation & Quality Gates</h2>
      <span class="badge badge-pass">{len(validation_list)} Checks</span>
    </div>
    <div class="table-wrapper">
      <table>
        <thead>
          <tr>
            <th>Check ID</th>
            <th>Status</th>
            <th>Verification Rule</th>
          </tr>
        </thead>
        <tbody>
          {"".join(val_rows)}
        </tbody>
      </table>
    </div>
  </section>

  <script>
    function filterCharts(metric) {{
      const buttons = document.querySelectorAll('#chart-filters .filter-btn');
      buttons.forEach(b => b.classList.remove('active'));
      event.target.classList.add('active');
      
      const rows = document.querySelectorAll('#chart-container .chart-row');
      rows.forEach(r => {{
        if (metric === 'all' || r.getAttribute('data-metric') === metric) {{
          r.style.display = 'grid';
        }} else {{
          r.style.display = 'none';
        }}
      }});
    }}
  </script>
</body>
</html>"""
    return rendered_html


def write_dashboard_artifact(
    output_dir: Path,
    evidence: list[EvidenceItem],
    validation: list[ValidationCheck],
    narrative: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_target = output_dir / "dashboard.json"
    html_target = output_dir / "dashboard.html"

    payload: dict[str, Any] = {
        "title": "Brazilian Municipal Crop Intelligence",
        "source": "IBGE SIDRA PAM table 5457",
        "evidence": [item.model_dump(mode="json") for item in evidence],
        "validation": [item.model_dump(mode="json") for item in validation],
        "charts": [
            {"id": "change-by-crop", "type": "bar", "metric": "change_percent"},
            {"id": "start-end-comparison", "type": "slope", "metric": "end_value"},
        ],
    }
    if narrative:
        payload["narrative"] = narrative
    if metadata:
        payload.update(metadata)

    json_target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    
    html_content = render_dashboard_html(payload, narrative=narrative)
    html_target.write_text(html_content, encoding="utf-8")
    
    return json_target


def validate_dashboard(path: Path) -> list[ValidationCheck]:
    json_path = path if path.suffix == ".json" else path / "dashboard.json"
    html_path = json_path.with_name("dashboard.html")

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    html_exists = html_path.exists() and html_path.stat().st_size > 0
    html_content = html_path.read_text(encoding="utf-8") if html_exists else ""

    checks = [
        ValidationCheck(
            check_id="visual:has_title",
            passed=bool(payload.get("title")),
            message="Dashboard has a title.",
        ),
        ValidationCheck(
            check_id="visual:has_evidence",
            passed=bool(payload.get("evidence")),
            message="Dashboard contains approved evidence.",
        ),
        ValidationCheck(
            check_id="visual:has_source",
            passed=bool(payload.get("source")),
            message="Dashboard has an explicit source note.",
        ),
        ValidationCheck(
            check_id="visual:chart_contracts",
            passed=all(chart.get("type") and chart.get("metric") for chart in payload.get("charts", [])),
            message="Every chart has a type and metric.",
        ),
        ValidationCheck(
            check_id="visual:html_artifact_present",
            passed=html_exists,
            message="Interactive HTML dashboard artifact exists and is non-empty.",
        ),
        ValidationCheck(
            check_id="visual:html_structure",
            passed=bool(
                "<!DOCTYPE html>" in html_content
                and "<title>" in html_content
                and "id=\"kpis\"" in html_content
                and "id=\"evidence-ledger\"" in html_content
                and "id=\"charts\"" in html_content
            ),
            message="HTML dashboard satisfies DOM structural contract.",
        ),
    ]
    return checks
