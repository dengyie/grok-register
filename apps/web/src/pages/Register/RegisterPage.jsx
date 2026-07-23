// src/pages/Register/RegisterPage.jsx
// Register / control home: left form + right progress. Full parity with legacy
// register UX (apps/web/legacy/assets/app.js register + renderRunStatus pipeline).
//
// Key behaviors:
//  - 4s poll of api.currentRun() + api.overview(); never wipes form while
//    regFormDirty.value === true; form loaded from config only once.
//  - startRun body keys mirror legacy exactly (kind/product/mode/target/threads/
//    tag/extra_env{SUPERVISOR_CHUNK,CPA_BATCH_END_INJECT,CPA_BATCH_IMPORT_*,
//    CPA_PROBE_CHAT=false,SKIP_CLASH_PREFLIGHT,NODE_SCORE,EMAIL_PROVIDER,
//    DEFAULT_DOMAINS}).
//  - stopRun: window.confirm Chinese warning first.
//  - putConfig wrapped as { config: partial } (backend ConfigPutIn schema).
//  - 401 → session.authenticated=false (gate shows).
//  - 接口/运维: 保存 / 自检(link #/settings) / 测代理. No duplicate cleanup/selfcheck
//    buttons here (spec IA — selfcheck lives on settings).
import { useEffect, useState, useCallback } from "preact/hooks";
import * as api from "../../api/client.js";
import { session } from "../../store/session.js";
import {
  showOpsFeedback,
  stickyBanner,
  opsLog,
  clearStickyBanner,
} from "../../store/feedback.js";
import {
  currentRunState,
  overviewState,
  regFormDirty,
  regFormLoaded,
  lastProductOk,
} from "../../store/run.js";
import { RegForm } from "./RegForm.jsx";
import { RunProgress } from "./RunProgress.jsx";
import "../../styles/run.css";

// Initial form state (also used before config loads). Mirrors legacy defaults.
const initialForm = {
  email_provider: "cloudflare",
  mailKey: "",
  savedSecret: "",
  defaultDomains: "",
  target: 100,
  threads: 1,
  mode: "ordinary",
  tag: "batch_web",
  chunk: 3,
  turnstile: 150,
  ssoOnly: true,
  batchEndInject: false,
  importEvery: 100,
  importSize: "",
  importPause: "",
  proxyMode: "clash",
  proxy: "",
  proxyList: "",
  kind: "grok_supervisor",
  product: "grok",
  skipPreflight: false,
  nodeScore: "",
  syncMailEnv: true,
};

function providerKeyField(provider) {
  const p = (provider || "").toLowerCase();
  if (p === "cloudflare") return "cloudflare_api_key";
  if (p === "duckmail") return "duckmail_api_key";
  if (p === "yyds") return "yyds_api_key";
  if (p === "cloudmail") return "cloudmail_password";
  return null;
}

function formatApiError(e) {
  if (!e) return "未知错误";
  const msg = String(e.message || e);
  if (e.status === 409) {
    return `已有任务在跑（${msg}）。首页会显示当前 progress；如需停请用「停止」（会杀外部 supervisor）。`;
  }
  if (e.status === 401) return "未登录或会话过期，请重新登录。";
  if (e.status === 422) return `参数校验失败: ${msg}`;
  if (e.status) return `HTTP ${e.status}: ${msg}`;
  return msg;
}

function snapshotFormFromConfig(c, prev) {
  // No prev → first load: apply defaults, then overlay config protocol bits
  // (email provider/domains/proxy/turnstile — the only fields legacy hydrates)
  // leaving target/threads/mode/tag/kind/product at their HTML Defaults.
  // prev → forced refresh: re-hydrate protocol bits but PRESERVE user edits to
  // run params (matches legacy refreshFn only refilling email/proxy/turnstile).
  const f = { ...initialForm, ...(prev || {}) };
  if (c.email_provider) f.email_provider = c.email_provider;
  if (c.defaultDomains != null) f.defaultDomains = String(c.defaultDomains);
  if (c.proxy != null) f.proxy = String(c.proxy);
  if (c.proxy_rotate_mode) f.proxyMode = c.proxy_rotate_mode;
  if (c.proxy_list != null) {
    const pl = c.proxy_list;
    f.proxyList = Array.isArray(pl) ? pl.join("\n") : String(pl);
  }
  if (c.turnstile_stuck_timeout != null) {
    f.turnstile = Number(c.turnstile_stuck_timeout);
  }
  f.probeChat = false; // supervisor hard-forced off
  const keyField = providerKeyField(c.email_provider);
  if (keyField && c[keyField] != null) {
    f.savedSecret = String(c[keyField]);
  }
  return f;
}

export function RegisterPage() {
  const [form, setForm] = useState(initialForm);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [busyKey, setBusyKey] = useState(null); // 'start' | 'stop' | 'save' | 'proxy' | 'refresh'
  const [actionResult, setActionResult] = useState("");
  const [opsLogOpen, setOpsLogOpen] = useState(false);
  const [loadedOnce, setLoadedOnce] = useState(false);

  // Load form from /api/config exactly once (and on explicit refresh).
  const loadForm = useCallback(async ({ force = false } = {}) => {
    if (regFormLoaded.value && !force) return;
    if (!force && regFormDirty.value && regFormLoaded.value) return;
    try {
      const data = await api.getConfig();
      const c = data.config || {};
      setForm((prev) =>
        snapshotFormFromConfig(c, force ? prev : undefined),
      );
      regFormLoaded.value = true;
      if (force) regFormDirty.value = false;
    } catch (e) {
      if (e.status === 401) {
        session.value = { ...session.value, authenticated: false };
        return;
      }
      if (force) showOpsFeedback(`加载配置失败: ${formatApiError(e)}`, "err");
    }
  }, []);

  // 4s poll: run + overview only. Never destroys form edits.
  useEffect(() => {
    let cancelled = false;
    async function tick() {
      try {
        const [cur, ov] = await Promise.all([api.currentRun(), api.overview()]);
        if (cancelled) return;
        const run = cur.run ?? cur ?? null;
        currentRunState.value = run;
        if (ov) {
          overviewState.value = ov;
          if (ov.product_ok != null) lastProductOk.value = ov.product_ok;
        }
      } catch (e) {
        if (e.status === 401) {
          session.value = { ...session.value, authenticated: false };
        }
        // poll failures stay silent (no toast spam)
      }
    }
    loadForm({ force: false });
    tick();
    const id = setInterval(tick, 4000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function gotoLogs() {
    location.hash = "#/logs";
  }

  // Build the start body exactly mirroring legacy startRun().
  function buildStartBody() {
    const v = form;
    const kind = v.kind || "grok_supervisor";
    const product =
      kind === "grok_supervisor" ? "grok" : v.product || "grok";
    const extra_env = {};

    if (kind === "grok_supervisor") {
      const chunk = String(v.chunk ?? "").trim();
      if (chunk) extra_env.SUPERVISOR_CHUNK = chunk;
      extra_env.CPA_BATCH_END_INJECT = v.batchEndInject ? "true" : "false";
      const every = String(v.importEvery ?? "").trim();
      if (every) extra_env.CPA_BATCH_IMPORT_EVERY = every;
      const size = String(v.importSize ?? "").trim();
      if (size) extra_env.CPA_BATCH_IMPORT_SIZE = size;
      const pause = String(v.importPause ?? "").trim();
      if (pause !== "") extra_env.CPA_BATCH_IMPORT_PAUSE = pause;
    }

    // Universal: probe-off
    extra_env.CPA_PROBE_CHAT = "false";

    if (v.skipPreflight) extra_env.SKIP_CLASH_PREFLIGHT = "1";
    const nodeScore = String(v.nodeScore ?? "").trim();
    if (nodeScore !== "") extra_env.NODE_SCORE = nodeScore;

    if (v.syncMailEnv) {
      const prov = (v.email_provider || "").trim();
      const dom = (v.defaultDomains || "").trim();
      if (prov) extra_env.EMAIL_PROVIDER = prov;
      if (dom) extra_env.DEFAULT_DOMAINS = dom;
    }

    return {
      kind,
      product,
      mode: v.mode || "ordinary",
      target: Number(v.target == null || v.target === "" ? 100 : v.target),
      threads: Number(v.threads == null || v.threads === "" ? 1 : v.threads),
      tag: String(v.tag || "batch_web").trim() || "batch_web",
      extra_env,
    };
  }

  // Build the config partial from the form (save path). Mirrors legacy saveRegisterCfg.
  function buildConfigPartial() {
    const v = form;
    const provider = v.email_provider;
    const partial = {
      email_provider: provider,
      defaultDomains: (v.defaultDomains || "").trim(),
      proxy: (v.proxy || "").trim(),
      proxy_rotate_mode: v.proxyMode,
      proxy_list: v.proxyList || "",
      turnstile_stuck_timeout: Number(v.turnstile || 150),
      // disk-first mid-mint inject always off; batch-end inject is CPA_BATCH_END_INJECT (extra_env).
      cpa_remote_inject: false,
      cpa_probe_chat: false,
    };
    const key = (v.mailKey || "").trim();
    const keyField = providerKeyField(provider);
    if (key && keyField) partial[keyField] = key;
    return { partial, provider };
  }

  async function saveConfig({ silent = false } = {}) {
    if (!silent) setBusyKey("save");
    try {
      const { partial, provider } = buildConfigPartial();
      // Wrap in { config: partial } — backend ConfigPutIn schema.
      const data = await api.putConfig({ config: partial });
      regFormDirty.value = false;
      regFormLoaded.value = true;
      // Keep saved secret placeholder fresh + clear entered key.
      const keyField = providerKeyField(provider);
      if (keyField && data.config && data.config[keyField] != null) {
        setForm((p) => ({ ...p, mailKey: "", savedSecret: String(data.config[keyField]) }));
      } else {
        setForm((p) => ({ ...p, mailKey: "" }));
      }
      if (!silent) {
        const prov = provider || "—";
        const dom = partial.defaultDomains || "—";
        showOpsFeedback(`配置已保存 · provider=${prov} · domains=${dom}`, "ok");
      }
      return data;
    } catch (e) {
      if (e.status === 401) {
        session.value = { ...session.value, authenticated: false };
        throw e;
      }
      if (!silent) showOpsFeedback(`保存失败: ${formatApiError(e)}`, "err");
      throw e;
    } finally {
      if (!silent) setBusyKey(null);
    }
  }

  async function refresh() {
    setBusyKey("refresh");
    try {
      await loadForm({ force: true });
      // One immediate run refresh too.
      const cur = await api.currentRun();
      const run = cur.run ?? cur ?? null;
      currentRunState.value = run;
      try {
        const ov = await api.overview();
        overviewState.value = ov;
        if (ov && ov.product_ok != null) lastProductOk.value = ov.product_ok;
      } catch {
        /* render with last known */
      }
      const phase = (run && (run.phase_title || run.phase)) || "—";
      const alive = !!(run && run.alive);
      const sub = run && run.sub != null ? ` sub=${run.sub}` : "";
      const complete = run && run.complete != null ? ` complete=${run.complete}` : "";
      const worker =
        run && run.worker_log
          ? ` · worker=${String(run.worker_log).split(/[\\/]/).pop()}`
          : "";
      showOpsFeedback(
        `已刷新 · ${alive ? "运行中" : "空闲"} · ${phase}${sub}${complete}${worker}`,
        "ok",
        { toast: true, sticky: true },
      );
    } catch (e) {
      if (e.status === 401) {
        session.value = { ...session.value, authenticated: false };
        return;
      }
      showOpsFeedback(formatApiError(e), "err");
    } finally {
      setBusyKey(null);
    }
  }

  async function start() {
    setBusyKey("start");
    showOpsFeedback("正在保存配置并启动…", "info", { toast: false, sticky: true });
    try {
      await saveConfig({ silent: true });
    } catch (e) {
      showOpsFeedback(
        `配置保存失败（仍尝试启动）: ${formatApiError(e)}`,
        "warn",
        { toast: true, sticky: true },
      );
    }
    const body = buildStartBody();
    try {
      const data = await api.startRun(body);
      const pid = data && data.run && data.run.pid;
      const detail = (data && data.detail) || "started";
      showOpsFeedback(
        `已启动 · ${detail}${pid != null ? ` pid=${pid}` : ""} · tag=${body.tag}`,
        "ok",
      );
      // immediate progress refresh
      try {
        const cur = await api.currentRun();
        currentRunState.value = cur.run ?? cur ?? null;
        const ov = await api.overview();
        overviewState.value = ov;
        if (ov && ov.product_ok != null) lastProductOk.value = ov.product_ok;
      } catch {
        /* fine */
      }
    } catch (e) {
      if (e.status === 401) {
        session.value = { ...session.value, authenticated: false };
        return;
      }
      showOpsFeedback(formatApiError(e), "err");
      if (e.status === 409) {
        try {
          const cur = await api.currentRun();
          currentRunState.value = cur.run ?? cur ?? null;
        } catch {
          /* ignore */
        }
      }
    } finally {
      setBusyKey(null);
    }
  }

  async function stop() {
    const ok = window.confirm(
      "确认停止当前任务？\n\n会结束 control 启动的进程组，也会尝试停止外部 supervisor（flock pid）。\n生产 batch 若在跑会被杀掉。",
    );
    if (!ok) {
      showOpsFeedback("已取消停止", "info", { toast: true, sticky: false });
      return;
    }
    setBusyKey("stop");
    showOpsFeedback("正在停止…", "warn", { toast: false, sticky: true });
    try {
      const data = await api.stopRun();
      const detail =
        (data && data.detail) || (data && data.ok ? "stopped" : "no active run");
      const pid = data && data.pid;
      showOpsFeedback(
        `${data && data.ok ? "已停止" : "停止未生效"} · ${detail}${pid != null ? ` pid=${pid}` : ""}`,
        data && data.ok ? "ok" : "warn",
      );
      const cur = await api.currentRun();
      currentRunState.value = cur.run ?? cur ?? null;
    } catch (e) {
      if (e.status === 401) {
        session.value = { ...session.value, authenticated: false };
        return;
      }
      showOpsFeedback(formatApiError(e), "err");
    } finally {
      setBusyKey(null);
    }
  }

  async function testProxy() {
    setBusyKey("proxy");
    showOpsFeedback("正在测代理…", "info", { toast: false, sticky: true });
    try {
      let data;
      let via = "clash";
      try {
        data = await api.testClash({ limit: 8 });
      } catch {
        via = "catalog";
        data = await api.testCatalog({ limit: 8 });
      }
      const healthy =
        data && (data.healthy != null
          ? data.healthy
          : data.ok_count != null
            ? data.ok_count
            : null);
      const total =
        data && (data.total != null
          ? data.total
          : data.tested != null
            ? data.tested
            : null);
      const summary =
        healthy != null && total != null
          ? `healthy=${healthy}/${total}`
          : (data && data.detail) || "done";
      showOpsFeedback(`代理测试完成 (${via}) · ${summary}`, "ok");
      setActionResult(typeof data === "string" ? data : JSON.stringify(data, null, 2));
    } catch (e) {
      if (e.status === 401) {
        session.value = { ...session.value, authenticated: false };
        return;
      }
      showOpsFeedback(`测代理失败: ${formatApiError(e)}`, "err");
    } finally {
      setBusyKey(null);
    }
  }

  const banner = stickyBanner.value;
  const logItems = opsLog.value;

  return (
    <section class="page">
      <header class="page-head">
        <div>
          <h1>协议注册</h1>
          <p class="hint">左启动参数 · 右实时进度。完整 worker/supervisor 日志 → 日志页。</p>
        </div>
        <div class="toolbar">
          <button
            type="button"
            class="btn btn-primary btn-md"
            disabled={busyKey === "start"}
            onClick={start}
          >
            {busyKey === "start" ? "启动中…" : "开始"}
          </button>
          <button
            type="button"
            class="btn btn-danger btn-md"
            disabled={busyKey === "stop"}
            onClick={stop}
          >
            {busyKey === "stop" ? "停止中…" : "停止"}
          </button>
          <button
            type="button"
            class="btn btn-ghost btn-md"
            disabled={busyKey === "refresh"}
            onClick={refresh}
          >
            {busyKey === "refresh" ? "刷新中…" : "刷新"}
          </button>
          <button
            type="button"
            class="btn btn-ghost btn-md"
            onClick={gotoLogs}
          >
            日志 →
          </button>
        </div>
      </header>

      {banner ? (
        <div
          class={`ops-feedback ${banner.kind || "info"}`}
          role="status"
          onClick={clearStickyBanner}
          title="点击清除横幅"
        >
          {banner.message}
        </div>
      ) : null}
      <details
        class="ops-log-wrap"
        open={opsLogOpen}
        onToggle={(e) => setOpsLogOpen(e.currentTarget.open)}
      >
        <summary>
          操作日志 <span class="hint">{logItems.length}</span>
        </summary>
        <ol class="ops-log" aria-live="polite">
          {logItems.map((it, i) => (
            <li key={i}>
              <span class="t">{it.t}</span>
              <span class={`k ${it.kind}`}>{it.kind}</span>
              <span class="m">{it.m}</span>
            </li>
          ))}
        </ol>
      </details>

      <div class="split">
        <div class="panel form-panel">
          <RegForm
            value={form}
            onChange={(next) => setForm(next)}
            advancedOpen={advancedOpen}
            onToggleAdvanced={(e) => setAdvancedOpen(e.currentTarget.open)}
          />
          {/* Footer toolbar: 保存 / 自检(link #/settings) / 测代理.
              No duplicate cleanup/selfcheck buttons (settings only). */}
          <div class="form-foot-toolbar">
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              disabled={busyKey === "save"}
              onClick={() => saveConfig().catch(() => {})}
            >
              {busyKey === "save" ? "保存中…" : "保存"}
            </button>
            <a class="btn btn-ghost btn-sm" href="#/settings" title="自检 / 清理在设置页">
              自检 →
            </a>
            <button
              type="button"
              class="btn btn-ghost btn-sm"
              disabled={busyKey === "proxy"}
              onClick={testProxy}
            >
              {busyKey === "proxy" ? "测代理…" : "测代理"}
            </button>
          </div>
          {actionResult ? (
            <pre class={`log compact ${actionResult ? "ok" : ""}`}>{actionResult}</pre>
          ) : null}
        </div>

        <div class="panel progress-panel">
          <RunProgress onGotoLogs={gotoLogs} />
        </div>
      </div>
    </section>
  );
}
