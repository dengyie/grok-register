// src/pages/RunLogs/LogsPage.jsx
// Logs page: current-run status strip + path summary + which/tail/follow tail
// + supervisor history. Path is RunLogs/ because root .gitignore has logs/.
// Hash route remains #/logs.
import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import * as api from "../../api/client.js";
import { session } from "../../store/session.js";
import {
  currentRunState,
  overviewState,
  lastProductOk,
} from "../../store/run.js";
import { showOpsFeedback } from "../../store/feedback.js";
import { StatusDot, Chip, Button, Select } from "../../ui/index.js";
import { runHeader } from "../Register/progressRender.jsx";
import { formatApiError } from "../../lib/format.js";
import "../../styles/run.css";
import "../../styles/logs.css";

const WHICH_OPTS = [
  { value: "auto", label: "auto" },
  { value: "worker", label: "worker" },
  { value: "supervisor", label: "supervisor" },
  { value: "both", label: "both" },
];

const TAIL_OPTS = [
  { value: "100", label: "100" },
  { value: "200", label: "200" },
  { value: "500", label: "500" },
];

function pathSummary(run) {
  const parts = [];
  if (run && run.worker_log) parts.push(`worker: ${run.worker_log}`);
  if (run && run.supervisor_log) parts.push(`supervisor: ${run.supervisor_log}`);
  return parts.length ? parts.join("  ·  ") : "path: —";
}

function historyItems(data) {
  const runs = Array.isArray(data?.runs) ? data.runs : [];
  return runs.slice(0, 12).map((r) => {
    const d = new Date((r.mtime || 0) * 1000);
    const iso = Number.isNaN(d.getTime())
      ? "—"
      : d.toISOString().replace("T", " ").slice(0, 19);
    return { iso, name: r.name || "" };
  });
}

export function LogsPage() {
  const [which, setWhich] = useState("auto");
  const [tail, setTail] = useState("200");
  const [follow, setFollow] = useState(true);
  const [logText, setLogText] = useState("");
  const [logPath, setLogPath] = useState("");
  const [history, setHistory] = useState(null); // null=loading | [] | items
  const [historyOpen, setHistoryOpen] = useState(true);
  const [historyErr, setHistoryErr] = useState("");
  const [busy, setBusy] = useState(false);
  const preRef = useRef(null);
  const whichRef = useRef(which);
  const tailRef = useRef(tail);
  whichRef.current = which;
  tailRef.current = tail;

  const handleAuth = (e) => {
    if (e && e.status === 401) {
      session.value = { ...session.value, authenticated: false };
      return true;
    }
    return false;
  };

  const refreshStatus = useCallback(async () => {
    try {
      const [cur, ov] = await Promise.all([api.currentRun(), api.overview()]);
      const run = cur?.run ?? cur ?? null;
      currentRunState.value = run;
      if (ov) {
        overviewState.value = ov;
        if (ov.product_ok != null) lastProductOk.value = ov.product_ok;
      }
      return run;
    } catch (e) {
      if (handleAuth(e)) return null;
      throw e;
    }
  }, []);

  const refreshLogs = useCallback(async () => {
    try {
      const logs = await api.runLogs(
        Number(tailRef.current) || 200,
        whichRef.current || "auto",
      );
      setLogPath(logs.path ? `path: ${logs.path}` : "");
      setLogText(logs.text || "");
      // stick to bottom like legacy
      requestAnimationFrame(() => {
        const el = preRef.current;
        if (el) el.scrollTop = el.scrollHeight;
      });
    } catch (e) {
      if (handleAuth(e)) return;
      setLogText(String(e.message || e));
    }
  }, []);

  const refreshHistory = useCallback(async () => {
    setHistoryErr("");
    try {
      const data = await api.listRuns();
      setHistory(historyItems(data));
    } catch (e) {
      if (handleAuth(e)) return;
      setHistory([]);
      setHistoryErr(String(e.message || e));
    }
  }, []);

  const refreshAll = useCallback(
    async ({ explicit = false } = {}) => {
      if (explicit) setBusy(true);
      try {
        const run = await refreshStatus();
        await refreshLogs();
        if (historyOpen) await refreshHistory();
        if (explicit) {
          const phase = (run && (run.phase_title || run.phase)) || "—";
          const alive = !!(run && run.alive);
          const sub = run && run.sub != null ? ` sub=${run.sub}` : "";
          const complete =
            run && run.complete != null ? ` complete=${run.complete}` : "";
          const worker =
            run && run.worker_log
              ? ` · worker=${String(run.worker_log).split(/[\\/]/).pop()}`
              : "";
          showOpsFeedback(
            `已刷新 · ${alive ? "运行中" : "空闲"} · ${phase}${sub}${complete}${worker}`,
            "ok",
            { toast: true, sticky: true },
          );
        }
      } catch (e) {
        if (handleAuth(e)) return;
        if (explicit) showOpsFeedback(formatApiError(e), "err");
      } finally {
        if (explicit) setBusy(false);
      }
    },
    [refreshStatus, refreshLogs, refreshHistory, historyOpen],
  );

  // Mount: load status + logs + history; follow poll every 4s while checked.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (cancelled) return;
      await refreshAll({ explicit: false });
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!follow) return undefined;
    const id = setInterval(() => {
      refreshStatus().catch(() => {});
      refreshLogs().catch(() => {});
    }, 4000);
    return () => clearInterval(id);
  }, [follow, refreshStatus, refreshLogs]);

  // which/tail change → immediate re-fetch
  useEffect(() => {
    refreshLogs();
  }, [which, tail, refreshLogs]);

  // history opens → load
  useEffect(() => {
    if (historyOpen) refreshHistory();
  }, [historyOpen, refreshHistory]);

  const run = currentRunState.value;
  const head = runHeader(run);
  // Logs summary chips lean: pid / tag / mode / stuck (legacy)
  const meta = (run && run.meta) || {};
  const tag = (run && run.tag) || meta.tag;
  const logChips = [];
  if (run && run.pid != null) logChips.push({ label: `pid=${run.pid}` });
  if (tag) logChips.push({ label: `tag=${tag}` });
  if (run && run.mode) logChips.push({ label: String(run.mode) });
  if (run && run.stuck) logChips.push({ label: "stuck", danger: true });
  const chips = logChips.length ? logChips : head.chips;

  return (
    <section class="page page-logs">
      <header class="page-head">
        <div>
          <h1>日志</h1>
          <p class="hint">
            当前 run 的 worker / supervisor tail、路径摘要与历史 supervisor。启动与进度在「注册」。
          </p>
        </div>
        <div class="toolbar wrap">
          <Button
            variant="ghost"
            busy={busy}
            onClick={() => refreshAll({ explicit: true })}
          >
            刷新
          </Button>
          <Button
            variant="ghost"
            onClick={() => {
              location.hash = "#/register";
            }}
          >
            ← 注册
          </Button>
          <label class="inline check">
            <input
              type="checkbox"
              checked={follow}
              onChange={(e) => setFollow(!!e.currentTarget.checked)}
            />{" "}
            自动刷新
          </label>
        </div>
      </header>

      <div
        class={`run-header run-header-${head.state}`}
        data-state={head.state}
        id="logs-summary"
      >
        <StatusDot kind={head.dotKind} />
        <span class="status-word">{head.word}</span>
        <span class="meta-chips">
          {chips.map((c, i) => (
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

      <div class="logs-paths hint mono">{pathSummary(run)}</div>

      <div class="panel logs-log-panel">
        <div class="log-toolbar">
          <h2>当前 tail</h2>
          <label class="inline">
            来源{" "}
            <Select
              value={which}
              options={WHICH_OPTS}
              onChange={(v) => setWhich(v)}
            />
          </label>
          <label class="inline">
            行数{" "}
            <Select
              value={tail}
              options={TAIL_OPTS}
              onChange={(v) => setTail(v)}
            />
          </label>
          <span class="hint mono log-path-inline">{logPath}</span>
        </div>
        <pre ref={preRef} class="log logs-log mono">
          {logText}
        </pre>
      </div>

      <details
        class="card run-history-wrap"
        open={historyOpen}
        onToggle={(e) => setHistoryOpen(e.currentTarget.open)}
      >
        <summary>最近 supervisor 历史</summary>
        <div class="run-history">
          {historyErr ? (
            <span class="hint">加载失败: {historyErr}</span>
          ) : history == null ? (
            <span class="hint">加载中…</span>
          ) : history.length === 0 ? (
            <span class="hint">暂无历史 supervisor 日志。</span>
          ) : (
            <ul class="run-history-list">
              {history.map((h, i) => (
                <li key={i}>
                  <span class="mono hint">{h.iso}</span>
                  {" · "}
                  <span class="mono">{h.name}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </details>
    </section>
  );
}
