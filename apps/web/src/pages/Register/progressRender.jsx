// src/pages/Register/progressRender.jsx
// Pure render (no DOM mutation) ported from legacy app.js:
//   renderRunHeader / renderKpiGrid / renderBars / renderStepRail /
//   renderStatusCard / renderRecentWrites / renderTimeline.
// All pure helpers: given (run, overview) → { ... } plain data for JSX.
import { fmtNum, pct } from "../../lib/format.js";

// ── Run header (hero) ────────────────────────────────────────────────────────
// returns { state, word, dotKind, chips: [{ label, danger, title }] }
export function runHeader(run) {
  const alive = !!(run && run.alive);
  const stuck = !!(run && run.stuck);
  const state = stuck ? "stuck" : alive ? "alive" : "idle";
  const word = alive ? "ALIVE" : run ? "idle" : "无任务";
  const dotKind = stuck ? "err" : alive ? "busy" : "idle";
  const meta = (run && run.meta) || {};
  const tag = (run && run.tag) || meta.tag;
  const chips = [];
  if (tag) chips.push({ label: `tag=${tag}` });
  if (run && run.pid != null) chips.push({ label: `pid=${run.pid}` });
  if (run && run.mode) chips.push({ label: String(run.mode) });
  if (run && run.kind) chips.push({ label: String(run.kind) });
  if (run && run.complete != null) {
    const goal = run.goal_complete != null ? ` / ${run.goal_complete}` : "";
    chips.push({ label: `complete=${run.complete}${goal}` });
  }
  if (stuck) {
    const reason =
      (run && (run.stuck_reason || (run.summary && run.summary.fatal_reason))) ||
      "";
    chips.push({ label: "stuck", danger: true, title: reason });
  }
  return { state, word, dotKind, chips };
}

// ── KPI grid ────────────────────────────────────────────────────────────────
// returns [{ label, value, hint, cls }]
export function kpiGrid(run, overview, lastProductOk) {
  const alive = !!(run && run.alive);
  const complete = run && run.complete != null ? run.complete : null;
  const goal = run && run.goal_complete != null ? run.goal_complete : null;
  const remain = run && run.remain != null ? run.remain : null;
  const gained = run && run.batch_gained != null ? run.batch_gained : null;
  const target = run && (run.target != null ? run.target : run.target_new);
  const batchRemain = run && run.batch_remain != null ? run.batch_remain : null;
  const sub = run && run.sub != null ? run.sub : null;
  const zero = run && run.consecutive_zero != null ? run.consecutive_zero : null;
  const disk =
    overview && overview.product_ok != null ? overview.product_ok : lastProductOk;
  const nodes = overview && overview.nodes ? overview.nodes : null;

  const zeroClass =
    run && run.stuck ? "danger" : zero != null && Number(zero) >= 4 ? "warn" : "";
  const completeClass = alive && remain === 0 ? "ok" : "";
  const diskClass =
    disk != null && Number(disk) > 0 ? "ok" : disk == null ? "" : "warn";

  return [
    {
      label: "complete / goal",
      value: `${fmtNum(complete)}${goal != null ? " / " + goal : ""}`,
      hint: remain != null ? `剩余 ${remain}` : "",
      cls: completeClass,
    },
    {
      label: "本批 gained",
      value: `${fmtNum(gained)}${target != null ? " / " + target : ""}`,
      hint: batchRemain != null ? `剩余 ${batchRemain}` : "",
      cls: "",
    },
    {
      label: "disk product_ok",
      value: fmtNum(disk),
      hint: "",
      cls: diskClass,
    },
    {
      label: "sub · zero",
      value: `${fmtNum(sub)} · ${fmtNum(zero)}`,
      hint: run && run.chunk != null ? `chunk ${run.chunk}` : "",
      cls: zeroClass,
    },
    {
      label: "mode",
      value: fmtNum(run && run.mode),
      hint: run && run.kind ? String(run.kind) : "",
      cls: "",
    },
    {
      label: "nodes healthy",
      value: nodes ? `${fmtNum(nodes.healthy)} / ${fmtNum(nodes.total)}` : "—",
      hint: nodes && nodes.enabled != null ? `enabled ${nodes.enabled}` : "",
      cls: "",
    },
  ];
}

// ── Bars ────────────────────────────────────────────────────────────────────
// returns [{ label, a, b, value: pctVal|null, cls }]
export function bars(run) {
  const complete = run && run.complete != null ? run.complete : null;
  const goal = run && run.goal_complete != null ? run.goal_complete : null;
  const gained = run && run.batch_gained != null ? run.batch_gained : null;
  const target = run && (run.target != null ? run.target : run.target_new);
  const stuck = !!(run && run.stuck);
  const gp = pct(complete, goal);
  const bp = pct(gained, target);
  return [
    { label: "全局", a: complete, b: goal, value: gp, cls: stuck ? "danger" : "ok" },
    { label: "本批", a: gained, b: target, value: bp, cls: stuck ? "warn" : "ok" },
  ];
}

// ── Step rail ───────────────────────────────────────────────────────────────
// returns [{ state, title, desc }] (state: done|active|pending)
export function stepRail(steps) {
  if (!Array.isArray(steps) || !steps.length) return [];
  return steps.map((s) => {
    const state = s && s.state ? String(s.state) : "pending";
    const title = s && (s.title || s.id) ? String(s.title || s.id) : "";
    const desc = s && s.desc ? String(s.desc) : "";
    return { state, title, desc };
  });
}

// ── Status card ─────────────────────────────────────────────────────────────
// returns { title, body }
export function statusCard(run) {
  const alive = !!(run && run.alive);
  const phase = (run && (run.phase_title || run.phase)) || "—";
  const title = `任务状态: ${alive ? "运行中" : run ? "空闲" : "—"} · ${phase}`;
  const lines = [];
  if (run) {
    if (run.phase_detail) lines.push(run.phase_detail);
    const fatal = run.summary && run.summary.fatal_reason;
    if (fatal) lines.push(`fatal: ${fatal}`);
    if (run.worker_log) lines.push(`worker: ${run.worker_log}`);
    if (run.supervisor_log) lines.push(`supervisor: ${run.supervisor_log}`);
    if (run.stuck_reason && !fatal) lines.push(`stuck: ${run.stuck_reason}`);
    if (!lines.length) lines.push("已加载当前任务。");
  } else {
    lines.push("当前无活动 supervisor。可在左侧填参数后点「开始」。");
  }
  return { title, body: lines.join("\n") };
}

// ── Recent writes ────────────────────────────────────────────────────────────
// returns [{ name, raw }] (last ≤5)
export function recentWrites(writes) {
  if (!Array.isArray(writes) || !writes.length) return [];
  return writes.slice(-5).map((p) => {
    const raw = String(p);
    const name = raw.split(/[\\/]/).pop() || raw;
    return { name, raw };
  });
}

// ── Timeline ──────────────────────────────────────────────────────────────────
// returns [{ src, title, line }] (last ≤6)
export function timeline(items) {
  const cap = 6;
  const rows = Array.isArray(items) ? items.slice(-cap) : [];
  return rows.map((it) => ({
    src: String((it && (it.source || it.phase)) || "log"),
    title: String((it && it.title) || ""),
    line: String((it && it.line) || "").slice(0, 300),
  }));
}
