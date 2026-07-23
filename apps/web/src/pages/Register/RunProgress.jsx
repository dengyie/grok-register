// src/pages/Register/RunProgress.jsx
// Right panel: live run progress. Renders from run store signals.
// Pure render pieces come from progressRender.jsx.
import { useState } from "preact/hooks";
import { StatusDot, Chip, Kpi } from "../../ui/index.js";
import {
  currentRunState,
  overviewState,
  lastProductOk,
} from "../../store/run.js";
import {
  runHeader,
  kpiGrid,
  bars,
  stepRail,
  statusCard,
  recentWrites,
  timeline,
} from "./progressRender.jsx";

function barKind(cls) {
  if (cls === "danger") return "err";
  if (cls === "warn") return "warn";
  return "ok";
}

export function RunProgress({ onGotoLogs }) {
  const [timelineOpen, setTimelineOpen] = useState(true);
  const [writesOpen, setWritesOpen] = useState(true);
  const run = currentRunState.value;
  const ov = overviewState.value;

  const head = runHeader(run);
  const kpis = kpiGrid(run, ov, lastProductOk.value);
  const brs = bars(run);
  const steps = stepRail(run && run.steps);
  const sc = statusCard(run);
  const writes = recentWrites(run && run.recent_writes);
  const tl = timeline(run && run.timeline);

  return (
    <div class="run-progress">
      <div
        class={`run-header run-header-${head.state}`}
        data-state={head.state}
      >
        <StatusDot kind={head.dotKind} />
        <span class="status-word">{head.word}</span>
        <span class="meta-chips">
          {head.chips.map((c, i) => (
            <Chip
              key={i}
              kind={c.danger ? "err" : "default"}
              class="mini"
              title={c.title}
            >
              {c.label}
            </Chip>
          ))}
        </span>
      </div>

      <div class="kpi-grid">
        {kpis.map((k, i) => (
          <Kpi
            key={i}
            label={k.label}
            value={k.value}
            hint={k.hint}
            class={k.cls}
          />
        ))}
      </div>

      <div class="bars">
        {brs.map((b, i) => {
          const p = b.value == null ? 0 : b.value;
          const cap = b.value == null
            ? "—"
            : `${fmtN(b.a)} / ${fmtN(b.b)} (${Math.round(b.value)}%)`;
          return (
            <div key={i} class="bar-row">
              <span class="bar-label">{b.label}</span>
              <div
                class="bar-track"
                role="progressbar"
                aria-valuenow={Math.round(p)}
                aria-valuemin={0}
                aria-valuemax={100}
              >
                <div
                  class={`bar-fill bar-${barKind(b.cls)}`}
                  style={{ width: `${p.toFixed(1)}%` }}
                />
              </div>
              <span class={`bar-caption ${b.cls || ""}`}>{cap}</span>
            </div>
          );
        })}
      </div>

      {steps.length > 0 ? (
        <div class="step-rail">
          {steps.map((s, i) => (
            <span
              key={i}
              class={`step ${s.state}`}
              title={s.desc}
            >
              {s.title}
            </span>
          ))}
        </div>
      ) : null}

      <div class="status-card">
        <div class="status-title">{sc.title}</div>
        <pre class="status-body">{sc.body}</pre>
      </div>

      {writes.length > 0 ? (
        <details
          class="writes-wrap"
          open={writesOpen}
          onToggle={(e) => setWritesOpen(e.currentTarget.open)}
        >
          <summary>最近落盘 ({writes.length})</summary>
          <div class="run-writes">
            {writes.map((w, i) => (
              <span key={i} class="write-chip" title={w.raw}>
                {w.name}
              </span>
            ))}
          </div>
        </details>
      ) : null}

      <details
        class="timeline-wrap"
        open={timelineOpen}
        onToggle={(e) => setTimelineOpen(e.currentTarget.open)}
      >
        <summary>时间线</summary>
        <ol class="timeline">
          {tl.length ? (
            tl.map((it, i) => (
              <li key={i} class="timeline-item">
                <span class="src">{it.src}</span>
                <span>
                  {it.title}
                  {it.line ? ` · ${it.line}` : ""}
                </span>
              </li>
            ))
          ) : (
            <li class="timeline-item hint">暂无事件</li>
          )}
        </ol>
      </details>

      <p class="hint progress-log-hint">
        worker / supervisor 完整 tail 与历史 →
        <button
          type="button"
          class="linkish"
          onClick={onGotoLogs}
        >
          日志
        </button>
      </p>
    </div>
  );
}

function fmtN(v) {
  return v == null || v === "" ? "—" : String(v);
}
