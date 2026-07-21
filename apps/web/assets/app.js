(() => {
  const $ = (sel) => document.querySelector(sel);
  const tokenKey = "controlToken";
  let authState = {
    authenticated: false,
    username: null,
    auth_required: true,
    password_login_enabled: true,
    users_configured: false,
  };
  let nodesTab = "clash";
  let catalogPage = 1;
  let catalogCache = null;
  let clashCache = null;
  let lastProductOk = null;
  let accountsPage = 1;
  let accountsCache = null;
  let cfgCache = null;
  let logTimer = null;
  // Form load guard: polling must not wipe in-progress edits.
  let regFormLoaded = false;
  let regFormDirty = false;

  function token() {
    return sessionStorage.getItem(tokenKey) || ($("#token") && $("#token").value.trim()) || "";
  }

  function headers(json = false) {
    const h = {};
    if (json) h["Content-Type"] = "application/json";
    const t = token();
    if (t) h["Authorization"] = `Bearer ${t}`;
    return h;
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, { credentials: "same-origin", ...opts });
    const text = await res.text();
    let body;
    try {
      body = text ? JSON.parse(text) : {};
    } catch {
      body = { detail: text };
    }
    if (!res.ok) {
      const detail = body.detail || res.statusText;
      const err = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      err.status = res.status;
      throw err;
    }
    return body;
  }

  function showGate(on) {
    $("#login-gate")?.classList.toggle("hidden", !on);
    $("#app-shell")?.classList.toggle("hidden", on);
  }

  function escapeHtml(s) {
    return String(s || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function dash(v) {
    return v == null || v === "" ? "—" : String(v);
  }

  function setResult(el, data) {
    if (!el) return;
    el.textContent = typeof data === "string" ? data : JSON.stringify(data, null, 2);
  }

  function healthBadge(h) {
    const cls = h === "ok" ? "ok" : h === "fail" ? "fail" : "unknown";
    const label = h === "ok" ? "可用" : h === "fail" ? "失败" : "未测";
    return `<span class="badge ${cls}">${label}</span>`;
  }

  async function refreshMe() {
    const me = await api("/api/auth/me", { headers: headers() });
    authState = me;
    const who = $("#whoami");
    if (who) {
      if (me.authenticated) who.textContent = me.username ? `已登录: ${me.username}` : "已认证";
      else if (token()) who.textContent = "Bearer token";
      else who.textContent = me.auth_required ? "未登录" : "开放模式";
    }
    return me;
  }

  async function ensureAuthed() {
    try {
      const me = await refreshMe();
      const hasBearer = Boolean(token());
      if (me.authenticated || hasBearer || !me.auth_required) {
        showGate(false);
        return true;
      }
      if (me.password_login_enabled) {
        showGate(true);
        return false;
      }
      showGate(false);
      return true;
    } catch (e) {
      showGate(true);
      if ($("#login-error")) $("#login-error").textContent = String(e.message || e);
      return false;
    }
  }

  async function doLogin(ev) {
    ev.preventDefault();
    if ($("#login-error")) $("#login-error").textContent = "";
    const username = $("#login-user").value.trim();
    const password = $("#login-pass").value;
    try {
      await api("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      $("#login-pass").value = "";
      showGate(false);
      await refreshMe();
      showPage("register");
    } catch (e) {
      if ($("#login-error")) $("#login-error").textContent = String(e.message || e);
    }
  }

  async function doLogout() {
    try {
      await api("/api/auth/logout", { method: "POST", headers: headers() });
    } catch {
      /* ignore */
    }
    sessionStorage.removeItem(tokenKey);
    if ($("#token")) $("#token").value = "";
    showGate(true);
    if ($("#login-error")) $("#login-error").textContent = "";
  }

  function showPage(name) {
    document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
    document.querySelectorAll("#nav button, .side-nav button").forEach((b) => b.classList.remove("active"));
    const page = $(`#page-${name}`);
    if (page) page.classList.add("active");
    const btn = document.querySelector(`#nav button[data-page="${name}"], .side-nav button[data-page="${name}"]`);
    if (btn) btn.classList.add("active");

    if (name === "register") {
      // Enter page: load form once (unless dirty), always refresh status/logs.
      loadRegisterForm({ force: false });
      refreshRegister({ reloadForm: false });
    }
    if (name === "accounts") refreshAccounts();
    if (name === "mail") loadMailForm();
    if (name === "nodes") refreshNodesPage();
    if (name === "settings") loadSettings();
  }

  // ── Config helpers ──────────────────────────────────────────────────────
  function providerKeyField(provider) {
    const p = (provider || "").toLowerCase();
    if (p === "cloudflare") return "cloudflare_api_key";
    if (p === "duckmail") return "duckmail_api_key";
    if (p === "yyds") return "yyds_api_key";
    if (p === "cloudmail") return "cloudmail_password";
    return null;
  }

  async function fetchConfig() {
    const data = await api("/api/config", { headers: headers() });
    cfgCache = data.config || {};
    return cfgCache;
  }

  function fillFormFromConfig(form, c) {
    if (!form || !c) return;
    for (const el of form.elements) {
      if (!el.name) continue;
      if (el.type === "checkbox") {
        const v = c[el.name];
        el.checked = v === true || v === "true" || v === 1 || v === "1";
        continue;
      }
      if (el.tagName === "SELECT") {
        const v = c[el.name];
        if (v === true || v === false) el.value = String(v);
        else if (v != null && v !== "") el.value = String(v);
        else if ([...el.options].some((o) => o.value === "")) el.value = "";
        continue;
      }
      if (c[el.name] != null) el.value = String(c[el.name]);
    }
  }

  function collectForm(form, { skipEmptySecrets = true } = {}) {
    const config = {};
    for (const el of form.elements) {
      if (!el.name || el.type === "submit" || el.type === "button") continue;
      if (el.type === "checkbox") {
        config[el.name] = el.checked;
        continue;
      }
      let v = el.value;
      if (skipEmptySecrets && (el.type === "password" || /api_key|password|token|secret/i.test(el.name))) {
        if (v === "" || String(v).startsWith("***")) continue;
      }
      if (v === "" && el.tagName === "SELECT" && el.options[0] && el.options[0].value === "") continue;
      if (el.type === "number" && v !== "") v = Number(v);
      if (el.name === "cpa_probe_chat" || el.name === "cpa_remote_inject" || el.name === "hotmail_allow_plus_alias") {
        if (v === "true") v = true;
        else if (v === "false") v = false;
      }
      if (v === "" && el.tagName !== "TEXTAREA") continue;
      config[el.name] = v;
    }
    return config;
  }

  async function putConfig(partial) {
    return api("/api/config", {
      method: "PUT",
      headers: headers(true),
      body: JSON.stringify({ config: partial }),
    });
  }

  // ── Register page ───────────────────────────────────────────────────────
  function markRegFormDirty() {
    regFormDirty = true;
  }

  function wireRegFormDirtyOnce() {
    const form = $("#reg-form");
    if (!form || form.dataset.dirtyWired === "1") return;
    form.dataset.dirtyWired = "1";
    form.addEventListener("input", markRegFormDirty);
    form.addEventListener("change", markRegFormDirty);
  }

  /**
   * Load protocol fields from config.
   * @param {{force?: boolean}} opts force=true bypasses dirty guard (explicit 刷新).
   * Polling MUST call with force=false and skip when dirty/already loaded.
   */
  async function loadRegisterForm(opts = {}) {
    const force = !!(opts && opts.force);
    wireRegFormDirtyOnce();
    if (!force && regFormLoaded && regFormDirty) return;
    if (!force && regFormLoaded) return;
    try {
      const c = await fetchConfig();
      const form = $("#reg-form");
      if (!form) return;
      if (c.email_provider) $("#reg-email-provider").value = c.email_provider;
      if (c.defaultDomains != null) $("#reg-domains").value = String(c.defaultDomains);
      if (c.proxy != null) $("#reg-proxy").value = String(c.proxy);
      if (c.proxy_rotate_mode) $("#reg-proxy-mode").value = c.proxy_rotate_mode;
      if (c.proxy_list != null) {
        const pl = c.proxy_list;
        $("#reg-proxy-list").value = Array.isArray(pl) ? pl.join("\n") : String(pl);
      }
      if (c.turnstile_stuck_timeout != null) $("#reg-turnstile").value = String(c.turnstile_stuck_timeout);
      // Supervisor hard-forces CPA_PROBE_CHAT=false; keep UI honest (disabled).
      const probeEl = $("#reg-probe-chat");
      if (probeEl) {
        probeEl.checked = false;
        probeEl.disabled = true;
        probeEl.title = "Supervisor 硬编码 CPA_PROBE_CHAT=false；中途 chat 探针对 bulk 无意义";
      }
      // sso-only default true only on first load; never force on every refresh
      if (!regFormLoaded) {
        const sso = $("#reg-sso-only");
        if (sso) sso.checked = true;
      }
      // batch-end inject is a run-time env intent, NOT config cpa_remote_inject.
      // Do not mirror cpa_remote_inject into this checkbox (that conflated mid-mint inject).
      // Leave user's checkbox as-is after first load.
      const keyField = providerKeyField(c.email_provider);
      if (keyField && c[keyField] != null) {
        // redacted value is fine as placeholder signal
        $("#reg-mail-key").placeholder = String(c[keyField]);
      }
      regFormLoaded = true;
      if (force) regFormDirty = false;
    } catch (e) {
      if (e.status === 401) return showGate(true);
      setResult($("#reg-action-result"), String(e.message || e));
    }
  }

  async function saveRegisterCfg() {
    const pre = $("#reg-action-result");
    try {
      const provider = $("#reg-email-provider").value;
      const partial = {
        email_provider: provider,
        defaultDomains: $("#reg-domains").value.trim(),
        proxy: $("#reg-proxy").value.trim(),
        proxy_rotate_mode: $("#reg-proxy-mode").value,
        proxy_list: $("#reg-proxy-list").value,
        turnstile_stuck_timeout: Number($("#reg-turnstile").value || 150),
        // Always persist disk-first mid-mint inject off for supervisor safety.
        // Batch-end inject is CPA_BATCH_END_INJECT (extra_env on start), not this key.
        cpa_remote_inject: false,
        // Probe is forced false on supervisor path; store false so config stays honest.
        cpa_probe_chat: false,
      };
      // sso-only is informational UI; mid-mint inject always false (disk-first).
      // Unchecking does NOT enable mid-mint inject — that would break product contract.
      void $("#reg-sso-only")?.checked;
      const key = $("#reg-mail-key").value.trim();
      const keyField = providerKeyField(provider);
      if (key && keyField) partial[keyField] = key;
      const data = await putConfig(partial);
      setResult(pre, data);
      cfgCache = data.config || cfgCache;
      regFormDirty = false;
      regFormLoaded = true;
    } catch (e) {
      setResult(pre, String(e.message || e));
    }
  }

  // ── Run progress render pipeline (v1 merge, console5) ──────────────

  function pct(a, b) {
    if (b == null || a == null || Number(b) <= 0) return null;
    return Math.max(0, Math.min(100, (Number(a) / Number(b)) * 100));
  }

  function fmtNum(v) {
    return v == null || v === "" ? "—" : String(v);
  }

  function kpiCard(label, value, sub, cls) {
    const c = cls ? ` ${cls}` : "";
    const s = sub ? `<div class="sub">${escapeHtml(sub)}</div>` : "";
    return `<div class="kpi${c}"><div class="label">${escapeHtml(label)}</div><div class="value">${escapeHtml(value)}</div>${s}</div>`;
  }

  function renderRunHeader(run) {
    const el = $("#run-header");
    if (!el) return;
    const alive = !!(run && run.alive);
    const stuck = !!(run && run.stuck);
    el.dataset.state = stuck ? "stuck" : alive ? "alive" : "idle";
    const word = el.querySelector(".status-word");
    if (word) word.textContent = alive ? "ALIVE" : run ? "idle" : "无任务";
    const chips = [];
    const meta = (run && run.meta) || {};
    const tag = (run && run.tag) || meta.tag;
    if (tag) chips.push(`<span class="chip mini">tag=${escapeHtml(tag)}</span>`);
    if (run && run.pid != null) chips.push(`<span class="chip mini">pid=${escapeHtml(run.pid)}</span>`);
    if (run && run.mode) chips.push(`<span class="chip mini">${escapeHtml(run.mode)}</span>`);
    if (run && run.kind) chips.push(`<span class="chip mini">${escapeHtml(run.kind)}</span>`);
    if (stuck) {
      const reason = (run && (run.stuck_reason || (run.summary && run.summary.fatal_reason))) || "";
      chips.push(`<span class="chip mini danger" title="${escapeHtml(reason)}">stuck</span>`);
    }
    const chipsEl = $("#run-header-chips");
    if (chipsEl) chipsEl.innerHTML = chips.join("");
  }

  function renderKpiGrid(run, overview) {
    const el = $("#run-kpi");
    if (!el) return;
    const complete = run && run.complete != null ? run.complete : null;
    const goal = run && run.goal_complete != null ? run.goal_complete : null;
    const remain = run && run.remain != null ? run.remain : null;
    const gained = run && run.batch_gained != null ? run.batch_gained : null;
    const target = run && (run.target != null ? run.target : run.target_new);
    const batchRemain = run && run.batch_remain != null ? run.batch_remain : null;
    const sub = run && run.sub != null ? run.sub : null;
    const zero = run && run.consecutive_zero != null ? run.consecutive_zero : null;
    const disk = overview && overview.product_ok != null ? overview.product_ok : lastProductOk;
    const nodes = overview && overview.nodes ? overview.nodes : null;
    const alive = !!(run && run.alive);

    const zeroClass = run && run.stuck ? "danger" : zero != null && Number(zero) >= 4 ? "warn" : "";
    const completeClass = alive && remain === 0 ? "ok" : "";

    el.innerHTML = [
      kpiCard(
        "complete / goal",
        `${fmtNum(complete)}${goal != null ? " / " + goal : ""}`,
        remain != null ? `剩余 ${remain}` : "",
        completeClass,
      ),
      kpiCard(
        "本批 gained",
        `${fmtNum(gained)}${target != null ? " / " + target : ""}`,
        batchRemain != null ? `剩余 ${batchRemain}` : "",
        "",
      ),
      kpiCard("disk product_ok", fmtNum(disk), "", disk != null && Number(disk) > 0 ? "ok" : ""),
      kpiCard(
        "sub · zero",
        `${fmtNum(sub)} · ${fmtNum(zero)}`,
        run && run.chunk != null ? `chunk ${run.chunk}` : "",
        zeroClass,
      ),
      kpiCard("mode", fmtNum(run && run.mode), run && run.kind ? String(run.kind) : "", ""),
      kpiCard(
        "nodes healthy",
        nodes ? `${fmtNum(nodes.healthy)} / ${fmtNum(nodes.total)}` : "—",
        nodes && nodes.enabled != null ? `enabled ${nodes.enabled}` : "",
        "",
      ),
    ].join("");
  }

  function barRow(label, a, b, pctVal, cls) {
    const fillStyle = pctVal == null ? "width:0" : `width:${pctVal.toFixed(1)}%`;
    const cap = pctVal == null ? "—" : `${fmtNum(a)} / ${fmtNum(b)} (${pctVal.toFixed(0)}%)`;
    const c = cls ? ` ${cls}` : "";
    return `<div class="bar-row">
      <span class="bar-label">${escapeHtml(label)}</span>
      <div class="bar-track"><div class="bar-fill${c}" style="${fillStyle}"></div></div>
      <span class="bar-caption">${escapeHtml(cap)}</span>
    </div>`;
  }

  function renderBars(run) {
    const el = $("#run-bars");
    if (!el) return;
    const complete = run && run.complete != null ? run.complete : null;
    const goal = run && run.goal_complete != null ? run.goal_complete : null;
    const gained = run && run.batch_gained != null ? run.batch_gained : null;
    const target = run && (run.target != null ? run.target : run.target_new);
    const stuck = !!(run && run.stuck);
    const gp = pct(complete, goal);
    const bp = pct(gained, target);
    const gClass = stuck ? "danger" : "";
    const bClass = stuck ? "warn" : "";
    el.innerHTML = [
      barRow("全局", complete, goal, gp, gClass),
      barRow("本批", gained, target, bp, bClass),
    ].join("");
  }

  function renderStepRail(steps) {
    const el = $("#run-steps");
    if (!el) return;
    if (!Array.isArray(steps) || !steps.length) {
      el.innerHTML = "";
      return;
    }
    el.innerHTML = steps
      .map((s) => {
        const state = s && s.state ? String(s.state) : "pending";
        const title = s && (s.title || s.id) ? String(s.title || s.id) : "";
        const desc = s && s.desc ? String(s.desc) : "";
        return `<span class="step ${escapeHtml(state)}" title="${escapeHtml(desc)}">${escapeHtml(title)}</span>`;
      })
      .join("");
  }

  function renderStatusCard(run) {
    const title = $("#run-status-title");
    const body = $("#run-status-body");
    if (!title || !body) return;
    const alive = !!(run && run.alive);
    const phase = (run && run.phase_title) || (run && run.phase) || "—";
    title.textContent = `任务状态: ${alive ? "运行中" : run ? "空闲" : "—"} · ${phase}`;
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
    body.textContent = lines.join("\n");
  }

  function renderRecentWrites(writes) {
    const el = $("#run-writes");
    if (!el) return;
    if (!Array.isArray(writes) || !writes.length) {
      el.classList.add("hidden");
      el.innerHTML = "";
      return;
    }
    el.classList.remove("hidden");
    el.innerHTML = writes
      .slice(-5)
      .map((p) => {
        const raw = String(p);
        const name = raw.split(/[\\/]/).pop() || raw;
        return `<span class="write-chip" title="${escapeHtml(raw)}">${escapeHtml(name)}</span>`;
      })
      .join("");
  }

  function renderTimeline(items) {
    const el = $("#run-timeline");
    if (!el) return;
    const cap = 6;
    const rows = Array.isArray(items) ? items.slice(-cap) : [];
    if (!rows.length) {
      el.innerHTML = `<li class="timeline-item hint">暂无事件</li>`;
      return;
    }
    el.innerHTML = rows
      .map((it) => {
        const src = escapeHtml(String((it && (it.source || it.phase)) || "log"));
        const title = escapeHtml(String((it && it.title) || ""));
        const line = escapeHtml(String((it && it.line) || "").slice(0, 300));
        return `<li class="timeline-item"><span class="src">${src}</span><span>${title}${line ? ` · ${line}` : ""}</span></li>`;
      })
      .join("");
  }

  function renderRunStatus(run, productOk, overview) {
    const ov = overview || { product_ok: productOk };
    renderRunHeader(run);
    renderKpiGrid(run, ov);
    renderBars(run);
    renderStepRail(run && run.steps);
    renderStatusCard(run);
    renderRecentWrites(run && run.recent_writes);
    renderTimeline(run && run.timeline);
  }

  async function refreshLogsOnly() {
    const which = $("#log-which")?.value || "auto";
    const tail = $("#log-tail")?.value || "200";
    try {
      const logs = await api(`/api/runs/current/logs?tail=${tail}&which=${which}`, {
        headers: headers(),
      });
      if ($("#log-path")) $("#log-path").textContent = logs.path ? `path: ${logs.path}` : "";
      if ($("#run-log")) {
        $("#run-log").textContent = logs.text || "";
        $("#run-log").scrollTop = $("#run-log").scrollHeight;
      }
    } catch (e) {
      if (e.status === 401) showGate(true);
      else if ($("#run-log")) $("#run-log").textContent = String(e.message || e);
    }
  }

  /**
   * Refresh run status + logs. Never reloads form on poll.
   * @param {{reloadForm?: boolean}} opts reloadForm only for explicit 刷新 button.
   */
  async function refreshRegister(opts = {}) {
    const reloadForm = !!(opts && opts.reloadForm);
    try {
      if (reloadForm) await loadRegisterForm({ force: true });
      const cur = await api("/api/runs/current", { headers: headers() });
      const run = cur.run || null;
      try {
        const ov = await api("/api/overview", { headers: headers() });
        lastProductOk = ov.product_ok;
        renderRunStatus(run, ov.product_ok, ov);
      } catch {
        renderRunStatus(run, lastProductOk, null);
      }
      await refreshLogsOnly();
    } catch (e) {
      if (e.status === 401) return showGate(true);
      setResult($("#reg-action-result"), String(e.message || e));
    }
  }

  function applyKindVisibility() {
    const kind = $("#reg-kind")?.value || "grok_supervisor";
    const isSupervisor = kind === "grok_supervisor";
    const productSel = $("#reg-product");
    if (productSel) {
      productSel.disabled = isSupervisor;
      if (isSupervisor) productSel.value = "grok";
    }
    // Supervisor-only knobs: hide when switching to register_sh single-shot.
    const supervisorOnly = [
      "#reg-chunk",
      "#reg-batch-end-inject",
      "#reg-import-every",
      "#reg-import-size",
      "#reg-import-pause",
    ];
    supervisorOnly.forEach((sel) => {
      const el = $(sel);
      if (!el) return;
      const wrap = el.closest("label");
      if (wrap) wrap.style.display = isSupervisor ? "" : "none";
    });
  }

  async function startRun() {
    const pre = $("#reg-action-result");
    // save protocol bits first (best-effort)
    try {
      await saveRegisterCfg();
    } catch {
      /* continue start */
    }

    const kind = $("#reg-kind")?.value || "grok_supervisor";
    const product =
      kind === "grok_supervisor" ? "grok" : $("#reg-product")?.value || "grok";
    const extra_env = {};

    // Supervisor-only knobs
    if (kind === "grok_supervisor") {
      const chunk = ($("#reg-chunk")?.value || "").trim();
      if (chunk) extra_env.SUPERVISOR_CHUNK = chunk;
      extra_env.CPA_BATCH_END_INJECT = $("#reg-batch-end-inject")?.checked
        ? "true"
        : "false";
      const every = ($("#reg-import-every")?.value || "").trim();
      if (every) extra_env.CPA_BATCH_IMPORT_EVERY = every;
      const size = ($("#reg-import-size")?.value || "").trim();
      if (size) extra_env.CPA_BATCH_IMPORT_SIZE = size;
      const pause = ($("#reg-import-pause")?.value || "").trim();
      if (pause !== "") extra_env.CPA_BATCH_IMPORT_PAUSE = pause;
    }

    // Universal: probe-off (supervisor hard-forces false again; register_sh honors)
    extra_env.CPA_PROBE_CHAT = "false";

    // Advanced knobs
    if ($("#reg-skip-preflight")?.checked) extra_env.SKIP_CLASH_PREFLIGHT = "1";
    const nodeScore = ($("#reg-node-score")?.value || "").trim();
    if (nodeScore !== "") extra_env.NODE_SCORE = nodeScore;

    // Sync mail env → extra_env (avoid register_sh not reading config)
    if ($("#reg-sync-mail-env")?.checked) {
      const prov = ($("#reg-email-provider")?.value || "").trim();
      const dom = ($("#reg-domains")?.value || "").trim();
      if (prov) extra_env.EMAIL_PROVIDER = prov;
      if (dom) extra_env.DEFAULT_DOMAINS = dom;
    }

    const body = {
      kind,
      product,
      mode: $("#reg-mode")?.value || "ordinary",
      target: Number($("#reg-target")?.value || 100),
      threads: Number($("#reg-threads")?.value || 1),
      tag: ($("#reg-tag")?.value || "batch_web").trim() || "batch_web",
      extra_env,
    };
    try {
      const data = await api("/api/runs/start", {
        method: "POST",
        headers: headers(true),
        body: JSON.stringify(body),
      });
      setResult(pre, data);
      await refreshRegister({ reloadForm: false });
    } catch (e) {
      setResult(pre, String(e.message || e));
    }
  }

  async function stopRun() {
    const pre = $("#reg-action-result");
    try {
      const data = await api("/api/runs/stop", { method: "POST", headers: headers() });
      setResult(pre, data);
      await refreshRegister({ reloadForm: false });
    } catch (e) {
      setResult(pre, String(e.message || e));
    }
  }

  async function runSelfcheck(targetPre) {
    const pre = targetPre || $("#reg-action-result");
    try {
      setResult(pre, "selfcheck…");
      const data = await api("/api/ops/selfcheck", { headers: headers() });
      setResult(pre, data);
    } catch (e) {
      setResult(pre, String(e.message || e));
    }
  }

  async function runCleanup(targetPre) {
    const pre = targetPre || $("#reg-action-result");
    try {
      setResult(pre, "cleanup…");
      const data = await api("/api/ops/cleanup-orphans?dry_run=false", {
        method: "POST",
        headers: headers(),
      });
      setResult(pre, data);
    } catch (e) {
      setResult(pre, String(e.message || e));
    }
  }

  async function testProxy() {
    const pre = $("#reg-action-result");
    try {
      setResult(pre, "测代理…");
      // Prefer Clash pool test if available; fall back to catalog sample.
      let data;
      try {
        data = await api("/api/nodes/clash/test", {
          method: "POST",
          headers: headers(true),
          body: JSON.stringify({ limit: 8 }),
        });
      } catch {
        data = await api("/api/nodes/test", {
          method: "POST",
          headers: headers(true),
          body: JSON.stringify({ limit: 8 }),
        });
      }
      setResult(pre, data);
    } catch (e) {
      setResult(pre, String(e.message || e));
    }
  }

  // ── Accounts ────────────────────────────────────────────────────────────
  function accountsQuery() {
    const params = new URLSearchParams();
    const q = ($("#acc-q")?.value || "").trim();
    const complete = $("#acc-complete")?.value || "";
    const page_size = $("#acc-page-size")?.value || "50";
    if (q) params.set("q", q);
    if (complete !== "") params.set("complete", complete);
    params.set("page", String(accountsPage));
    params.set("page_size", page_size);
    return params.toString();
  }

  async function refreshAccounts() {
    const sum = $("#acc-summary");
    const tbody = $("#acc-table tbody");
    const pageInfo = $("#acc-page-info");
    if (!tbody) return;
    try {
      const data = await api(`/api/accounts?${accountsQuery()}`, { headers: headers() });
      accountsCache = data;
      if (sum) {
        sum.textContent =
          `path=${data.path} · 本页筛选 total=${data.total} complete=${data.complete}` +
          (data.disk_complete != null ? ` · disk_complete=${data.disk_complete}` : "") +
          ` · 第 ${data.page}/${data.pages || 1} 页`;
        sum.className = "card " + (data.complete > 0 ? "ok" : "muted");
      }
      if (pageInfo) pageInfo.textContent = `page ${data.page}/${data.pages || 1}`;
      const rows = data.items || [];
      if (!rows.length) {
        tbody.innerHTML = `<tr><td colspan="6" class="hint">无账号</td></tr>`;
      } else {
        tbody.innerHTML = rows
          .map((it) => {
            const st = it.complete
              ? '<span class="badge ok">complete</span>'
              : '<span class="badge fail">incomplete</span>';
            const flags = [
              it.disabled ? '<span class="badge fail">disabled</span>' : "",
              it.expired ? '<span class="badge fail">expired</span>' : "",
            ]
              .filter(Boolean)
              .join(" ");
            return `<tr>
              <td>${st} ${flags}</td>
              <td><div>${escapeHtml(it.email || "")}</div><div class="hint">${escapeHtml(it.name || "")}</div></td>
              <td>${escapeHtml(it.mtime_iso || "")}</td>
              <td>${it.priority != null ? escapeHtml(it.priority) : "—"}</td>
              <td>${escapeHtml(it.auth_kind || "—")}</td>
              <td class="ops">
                <button type="button" class="small danger" data-acc-del="${escapeHtml(it.name)}">删除</button>
              </td>
            </tr>`;
          })
          .join("");
        tbody.querySelectorAll("button[data-acc-del]").forEach((btn) => {
          btn.addEventListener("click", async () => {
            const name = btn.dataset.accDel;
            if (!confirm(`删除 ${name}？仅删本地 cpa_auths，不影响 tebi。`)) return;
            try {
              setResult(
                $("#acc-result"),
                await api(`/api/accounts/${encodeURIComponent(name)}`, {
                  method: "DELETE",
                  headers: headers(),
                })
              );
              await refreshAccounts();
            } catch (e) {
              setResult($("#acc-result"), String(e.message || e));
            }
          });
        });
      }
    } catch (e) {
      if (e.status === 401) return showGate(true);
      if (sum) sum.textContent = String(e.message || e);
      tbody.innerHTML = "";
    }
  }

  // ── Mail page ───────────────────────────────────────────────────────────
  async function loadMailForm() {
    const pre = $("#mail-result");
    try {
      const c = await fetchConfig();
      fillFormFromConfig($("#mail-form"), c);
      if ($("#mail-plus-alias")) {
        $("#mail-plus-alias").checked = !!(c.hotmail_allow_plus_alias === true || c.hotmail_allow_plus_alias === "true");
      }
      setResult(pre, { loaded: true, provider: c.email_provider, domains: c.defaultDomains });
    } catch (e) {
      if (e.status === 401) return showGate(true);
      setResult(pre, String(e.message || e));
    }
  }

  async function saveMailForm() {
    const pre = $("#mail-result");
    try {
      const form = $("#mail-form");
      const partial = collectForm(form);
      if ($("#mail-plus-alias")) partial.hotmail_allow_plus_alias = $("#mail-plus-alias").checked;
      const data = await putConfig(partial);
      setResult(pre, data);
      cfgCache = data.config || cfgCache;
    } catch (e) {
      setResult(pre, String(e.message || e));
    }
  }

  async function importMailCreds(fromMailPage) {
    const textEl = fromMailPage ? $("#mail-cred-text") : $("#mail-text");
    const modeEl = fromMailPage ? $("#mail-cred-mode") : $("#mail-mode");
    const pre = fromMailPage ? $("#mail-result") : $("#import-result");
    const fd = new FormData();
    fd.append("content", textEl?.value || "");
    fd.append("mode", modeEl?.value || "append");
    try {
      const res = await fetch("/api/import/mail", {
        method: "POST",
        headers: headers(),
        body: fd,
        credentials: "same-origin",
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || res.statusText);
      setResult(pre, body);
    } catch (e) {
      setResult(pre, { error: String(e.message || e) });
    }
  }

  // ── Settings ────────────────────────────────────────────────────────────
  async function loadSettings() {
    const pre = $("#config-result");
    try {
      const data = await api("/api/config", { headers: headers() });
      cfgCache = data.config || {};
      fillFormFromConfig($("#config-form"), cfgCache);
      setResult(pre, data);
    } catch (e) {
      if (e.status === 401) return showGate(true);
      setResult(pre, String(e.message || e));
    }
  }

  async function saveSettings(ev) {
    if (ev) ev.preventDefault();
    const pre = $("#config-result");
    try {
      const partial = collectForm($("#config-form"));
      const data = await putConfig(partial);
      setResult(pre, data);
      cfgCache = data.config || cfgCache;
    } catch (e) {
      setResult(pre, String(e.message || e));
    }
  }

  // ── Import ──────────────────────────────────────────────────────────────
  function showImport(result) {
    setResult($("#import-result"), result);
  }

  async function postMultipart(url, formData) {
    const res = await fetch(url, {
      method: "POST",
      headers: headers(),
      body: formData,
      credentials: "same-origin",
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || res.statusText);
    return body;
  }

  async function importNodes() {
    const f = $("#nodes-file")?.files?.[0];
    if (!f) return showImport({ error: "pick a file" });
    const fd = new FormData();
    fd.append("file", f);
    fd.append("dry_run", $("#nodes-dry")?.checked ? "true" : "false");
    fd.append("replace", $("#nodes-replace")?.checked ? "true" : "false");
    try {
      showImport(await postMultipart("/api/import/nodes", fd));
    } catch (e) {
      showImport({ error: String(e.message || e) });
    }
  }

  async function importAuths() {
    const f = $("#auths-file")?.files?.[0];
    if (!f) return showImport({ error: "pick a file" });
    const fd = new FormData();
    fd.append("file", f);
    fd.append("no_remote", $("#auths-remote")?.checked ? "false" : "true");
    try {
      showImport(await postMultipart("/api/import/auths", fd));
    } catch (e) {
      showImport({ error: String(e.message || e) });
    }
  }

  async function importPack() {
    const f = $("#pack-file")?.files?.[0];
    if (!f) return showImport({ error: "pick a file" });
    const fd = new FormData();
    fd.append("file", f);
    fd.append("apply", $("#pack-apply")?.checked ? "true" : "false");
    try {
      showImport(await postMultipart("/api/import/pack", fd));
    } catch (e) {
      showImport({ error: String(e.message || e) });
    }
  }

  // ── Nodes ───────────────────────────────────────────────────────────────
  function setNodesTab(tab) {
    nodesTab = tab;
    document.querySelectorAll("[data-nodes-tab]").forEach((b) => {
      b.classList.toggle("active", b.dataset.nodesTab === tab);
    });
    $("#nodes-catalog")?.classList.toggle("hidden", tab !== "catalog");
    $("#nodes-clash")?.classList.toggle("hidden", tab !== "clash");
    if (tab === "catalog") refreshCatalog();
    if (tab === "clash") refreshClash();
  }

  function refreshNodesPage() {
    setNodesTab(nodesTab || "clash");
  }

  function catalogQuery() {
    const q = ($("#cat-q")?.value || "").trim();
    const health = $("#cat-health")?.value || "";
    const tier = $("#cat-tier")?.value || "";
    const page_size = $("#cat-page-size")?.value || "50";
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (health) params.set("health", health);
    if (tier !== "") params.set("tier", tier);
    params.set("page", String(catalogPage));
    params.set("page_size", page_size);
    params.set("sort", "priority");
    return params.toString();
  }

  async function refreshCatalog() {
    const sum = $("#nodes-catalog-summary");
    const tbody = $("#nodes-catalog-table tbody");
    const pageInfo = $("#cat-page-info");
    if (!tbody) return;
    try {
      const data = await api(`/api/nodes?${catalogQuery()}`, { headers: headers() });
      catalogCache = data;
      if (sum) {
        sum.textContent =
          `path=${data.path} · total=${data.total} enabled=${data.enabled} healthy=${data.healthy}` +
          (data.fail != null ? ` fail=${data.fail}` : "") +
          (data.unknown != null ? ` unknown=${data.unknown}` : "") +
          ` · 筛选 ${data.filtered}/${data.total} · 第 ${data.page}/${data.pages || 1} 页`;
        sum.className = "card " + (data.healthy > 0 ? "ok" : "muted");
      }
      if (pageInfo) {
        pageInfo.textContent = `page ${data.page}/${data.pages || 1} · showing ${(data.nodes || []).length}`;
      }
      const rows = data.nodes || [];
      if (!rows.length) {
        tbody.innerHTML = `<tr><td colspan="8" class="hint">无匹配节点</td></tr>`;
      } else {
        tbody.innerHTML = rows
          .map((n) => {
            const health =
              n.health || (n.last_ok === true ? "ok" : n.last_ok === false ? "fail" : "unknown");
            const cool = n.cooling ? ` · cool:${escapeHtml(n.cooldown_reason || "")}` : "";
            return `<tr>
            <td>${healthBadge(health)}${cool}${n.enabled === false ? ' <span class="badge fail">禁用</span>' : ""}</td>
            <td><div>${escapeHtml(n.label || n.id)}</div><div class="hint">${escapeHtml(n.id)}</div></td>
            <td>${n.tier === 1 ? "住宅" : "机房"}</td>
            <td>${n.last_ms != null ? n.last_ms + "ms" : "—"}</td>
            <td>${escapeHtml(n.last_ip || "—")}</td>
            <td>${
              n.priority_score != null
                ? n.priority_score
                : n.quality_score != null
                  ? n.quality_score
                  : "—"
            }</td>
            <td>${n.fail_count || 0}${
              n.last_error ? `<div class="hint">${escapeHtml(n.last_error)}</div>` : ""
            }</td>
            <td class="ops">
              <button type="button" class="small" data-act="test" data-id="${escapeHtml(n.id)}">测活</button>
              <button type="button" class="small" data-act="toggle" data-id="${escapeHtml(
                n.id
              )}" data-en="${n.enabled ? "0" : "1"}">${n.enabled ? "禁用" : "启用"}</button>
              <button type="button" class="small danger" data-act="del" data-id="${escapeHtml(
                n.id
              )}">删除</button>
            </td>
          </tr>`;
          })
          .join("");
      }
      tbody.querySelectorAll("button[data-act]").forEach((btn) => {
        btn.addEventListener("click", () =>
          onCatalogAction(btn.dataset.act, btn.dataset.id, btn.dataset.en)
        );
      });
    } catch (e) {
      if (e.status === 401) return showGate(true);
      if (sum) sum.textContent = String(e.message || e);
      tbody.innerHTML = "";
    }
  }

  async function onCatalogAction(act, id, en) {
    const pre = $("#nodes-catalog-result");
    try {
      if (act === "del") {
        if (!confirm(`删除节点 ${id}?`)) return;
        setResult(
          pre,
          await api(`/api/nodes/${encodeURIComponent(id)}`, {
            method: "DELETE",
            headers: headers(),
          })
        );
      } else if (act === "toggle") {
        setResult(
          pre,
          await api(`/api/nodes/${encodeURIComponent(id)}`, {
            method: "PATCH",
            headers: headers(true),
            body: JSON.stringify({ enabled: en === "1" }),
          })
        );
      } else if (act === "test") {
        setResult(
          pre,
          await api("/api/nodes/test", {
            method: "POST",
            headers: headers(true),
            body: JSON.stringify({ ids: [id] }),
          })
        );
      }
      await refreshCatalog();
    } catch (e) {
      setResult(pre, String(e.message || e));
    }
  }

  async function addNode() {
    const pre = $("#nodes-catalog-result");
    const url = $("#node-url").value.trim();
    const label = $("#node-label").value.trim();
    const tags = ($("#node-tags").value || "")
      .split(/[,;]/)
      .map((s) => s.trim())
      .filter(Boolean);
    const tier = Number($("#node-tier").value || 0);
    try {
      const data = await api("/api/nodes", {
        method: "POST",
        headers: headers(true),
        body: JSON.stringify({ url, label, tags, tier, enabled: true }),
      });
      setResult(pre, data);
      if (data.ok) {
        $("#node-url").value = "";
        $("#node-label").value = "";
      }
      await refreshCatalog();
    } catch (e) {
      setResult(pre, String(e.message || e));
    }
  }

  async function testAllCatalog() {
    const pre = $("#nodes-catalog-result");
    setResult(pre, "testing…");
    try {
      const data = await api("/api/nodes/test", {
        method: "POST",
        headers: headers(true),
        body: JSON.stringify({ limit: 50 }),
      });
      setResult(pre, data);
      await refreshCatalog();
    } catch (e) {
      setResult(pre, String(e.message || e));
    }
  }

  function filterClashLeaves(leaves) {
    const mode = $("#clash-filter")?.value || "pool";
    const q = ($("#clash-q")?.value || "").trim().toLowerCase();
    let out = leaves || [];
    if (mode === "pool") out = out.filter((n) => n.in_register_pool);
    else if (mode === "ok") out = out.filter((n) => n.health === "ok");
    else if (mode === "fail") out = out.filter((n) => n.health === "fail");
    if (q) out = out.filter((n) => (n.name || "").toLowerCase().includes(q));
    out = out.slice().sort((a, b) => {
      const pa = a.in_register_pool ? 0 : 1;
      const pb = b.in_register_pool ? 0 : 1;
      if (pa !== pb) return pa - pb;
      const ha = { ok: 0, unknown: 1, fail: 2 }[a.health || "unknown"] ?? 1;
      const hb = { ok: 0, unknown: 1, fail: 2 }[b.health || "unknown"] ?? 1;
      if (ha !== hb) return ha - hb;
      const da = a.last_delay_ms != null ? a.last_delay_ms : 1e9;
      const db = b.last_delay_ms != null ? b.last_delay_ms : 1e9;
      return da - db;
    });
    return out;
  }

  function renderClashTable(data) {
    const sum = $("#clash-summary");
    const tbody = $("#clash-leaves-table tbody");
    const groupsEl = $("#clash-groups");
    if (!tbody) return;
    if (!data || !data.ok) {
      if (sum)
        sum.textContent = `Clash 不可用: ${(data && data.error) || "unknown"} (api=${
          (data && data.api) || ""
        })`;
      tbody.innerHTML = "";
      if (groupsEl) groupsEl.innerHTML = "";
      return;
    }
    const leaves = data.leaves || [];
    const poolN = leaves.filter((x) => x.in_register_pool).length;
    const okN = leaves.filter((x) => x.health === "ok").length;
    const shown = filterClashLeaves(leaves);
    if (sum) {
      sum.textContent =
        `api=${data.api} · leaves=${data.leaf_count} · 注册池 ${poolN} · 可用 ${okN}` +
        ` · 显示 ${shown.length} · groups=${data.group_count}` +
        ` · secret=${data.secret_configured ? "yes" : "no"}`;
    }
    if (groupsEl) {
      groupsEl.innerHTML = (data.groups || [])
        .map((g) => {
          const hot = g.register_relevant ? " hot" : "";
          return `<span class="chip${hot}" title="${escapeHtml(g.type)} now=${escapeHtml(
            g.now || ""
          )}">${escapeHtml(g.name)} · ${g.count}${
            g.now ? " → " + escapeHtml(g.now) : ""
          }</span>`;
        })
        .join("");
    }
    if (!shown.length) {
      tbody.innerHTML = `<tr><td colspan="8" class="hint">当前筛选下无节点</td></tr>`;
      return;
    }
    tbody.innerHTML = shown
      .map((n) => {
        const health = n.health || "unknown";
        const groups = (n.groups || []).slice(0, 4).join(", ");
        return `<tr>
            <td>${healthBadge(health)}</td>
            <td>${escapeHtml(n.name)}</td>
            <td>${escapeHtml(n.type || "")}</td>
            <td>${n.last_delay_ms != null ? n.last_delay_ms + "ms" : "—"}</td>
            <td>${n.priority_score != null ? n.priority_score : "—"}</td>
            <td>${n.in_register_pool ? '<span class="badge pool">注册池</span>' : "—"}</td>
            <td class="hint">${escapeHtml(groups)}</td>
            <td class="ops"><button type="button" class="small" data-clash-test="${escapeHtml(
              n.name
            )}">测活</button></td>
          </tr>`;
      })
      .join("");
    const pre = $("#nodes-clash-result");
    tbody.querySelectorAll("button[data-clash-test]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        setResult(pre, "testing…");
        try {
          const r = await api("/api/nodes/clash/test", {
            method: "POST",
            headers: headers(true),
            body: JSON.stringify({ names: [btn.dataset.clashTest] }),
          });
          setResult(pre, r);
          await refreshClash();
        } catch (e) {
          setResult(pre, String(e.message || e));
        }
      });
    });
  }

  async function refreshClash() {
    const sum = $("#clash-summary");
    const pre = $("#nodes-clash-result");
    try {
      const data = await api("/api/nodes/clash", { headers: headers() });
      clashCache = data;
      renderClashTable(data);
      if (pre && data && !data.ok) setResult(pre, data);
      else if (pre) pre.textContent = "";
    } catch (e) {
      if (e.status === 401) return showGate(true);
      if (sum) sum.textContent = String(e.message || e);
      const tbody = $("#clash-leaves-table tbody");
      if (tbody) tbody.innerHTML = "";
    }
  }

  async function clashTest(limit, poolOnly) {
    const pre = $("#nodes-clash-result");
    setResult(pre, "testing…");
    try {
      let names = null;
      if (poolOnly) {
        const listing = clashCache || (await api("/api/nodes/clash", { headers: headers() }));
        names = (listing.leaves || []).filter((x) => x.in_register_pool).map((x) => x.name);
      }
      const body = names ? { names, limit: names.length || 40 } : { limit: limit || 40 };
      const data = await api("/api/nodes/clash/test", {
        method: "POST",
        headers: headers(true),
        body: JSON.stringify(body),
      });
      setResult(pre, data);
      await refreshClash();
    } catch (e) {
      setResult(pre, String(e.message || e));
    }
  }

  // ── Wire ────────────────────────────────────────────────────────────────
  if ($("#token")) {
    $("#token").value = sessionStorage.getItem(tokenKey) || "";
  }
  $("#saveToken")?.addEventListener("click", async () => {
    sessionStorage.setItem(tokenKey, $("#token").value.trim());
    await ensureAuthed();
    showPage("register");
  });
  $("#login-form")?.addEventListener("submit", doLogin);
  $("#logoutBtn")?.addEventListener("click", doLogout);
  document.querySelectorAll("#nav button, .side-nav button").forEach((b) => {
    b.addEventListener("click", () => showPage(b.dataset.page));
  });

  // register toolbar
  $("#btn-save-cfg")?.addEventListener("click", saveRegisterCfg);
  $("#btn-selfcheck")?.addEventListener("click", () => runSelfcheck($("#reg-action-result")));
  $("#btn-start-turnstile")?.addEventListener("click", () => {
    setResult($("#reg-action-result"), {
      ok: false,
      detail: "WIP：过盾浏览器池尚未接入 control plane；mint 仍由 register_cli/tab_pool 自管。",
    });
  });
  $("#btn-cleanup")?.addEventListener("click", () => runCleanup($("#reg-action-result")));
  $("#btn-test-proxy")?.addEventListener("click", testProxy);
  $("#btn-start")?.addEventListener("click", startRun);
  $("#btn-stop")?.addEventListener("click", stopRun);
  // Explicit 刷新: force form reload + status/logs
  $("#btn-refresh")?.addEventListener("click", () => refreshRegister({ reloadForm: true }));
  $("#log-which")?.addEventListener("change", refreshLogsOnly);
  $("#log-tail")?.addEventListener("change", refreshLogsOnly);
  wireRegFormDirtyOnce();
  // Advanced start: toggle supervisor vs register_sh field visibility
  $("#reg-kind")?.addEventListener("change", applyKindVisibility);
  applyKindVisibility();

  // accounts
  $("#acc-refresh")?.addEventListener("click", refreshAccounts);
  let accQTimer = null;
  const resetAccAndRefresh = () => {
    accountsPage = 1;
    refreshAccounts();
  };
  $("#acc-q")?.addEventListener("input", () => {
    clearTimeout(accQTimer);
    accQTimer = setTimeout(resetAccAndRefresh, 250);
  });
  $("#acc-complete")?.addEventListener("change", resetAccAndRefresh);
  $("#acc-page-size")?.addEventListener("change", resetAccAndRefresh);
  $("#acc-prev")?.addEventListener("click", () => {
    if (accountsPage > 1) {
      accountsPage -= 1;
      refreshAccounts();
    }
  });
  $("#acc-next")?.addEventListener("click", () => {
    const pages = accountsCache && accountsCache.pages ? accountsCache.pages : 1;
    if (accountsPage < pages) {
      accountsPage += 1;
      refreshAccounts();
    }
  });

  // mail
  $("#mail-reload")?.addEventListener("click", loadMailForm);
  $("#mail-save")?.addEventListener("click", saveMailForm);
  $("#mail-cred-go")?.addEventListener("click", () => importMailCreds(true));

  // import
  $("#nodes-go")?.addEventListener("click", importNodes);
  $("#mail-go")?.addEventListener("click", () => importMailCreds(false));
  $("#auths-go")?.addEventListener("click", importAuths);
  $("#pack-go")?.addEventListener("click", importPack);

  // settings
  $("#config-reload")?.addEventListener("click", loadSettings);
  $("#config-save")?.addEventListener("click", saveSettings);
  $("#config-form")?.addEventListener("submit", saveSettings);
  $("#settings-selfcheck")?.addEventListener("click", () => runSelfcheck($("#config-result")));
  $("#settings-cleanup")?.addEventListener("click", () => runCleanup($("#config-result")));

  // nodes
  document.querySelectorAll("[data-nodes-tab]").forEach((b) => {
    b.addEventListener("click", () => setNodesTab(b.dataset.nodesTab));
  });
  $("#node-add")?.addEventListener("click", addNode);
  $("#node-test-all")?.addEventListener("click", testAllCatalog);
  $("#node-refresh")?.addEventListener("click", refreshCatalog);
  $("#clash-refresh")?.addEventListener("click", refreshClash);
  $("#clash-test-pool")?.addEventListener("click", () => clashTest(80, true));
  $("#clash-test-all")?.addEventListener("click", () => clashTest(40, false));
  $("#clash-filter")?.addEventListener("change", () => {
    if (clashCache) renderClashTable(clashCache);
    else refreshClash();
  });
  let clashQTimer = null;
  $("#clash-q")?.addEventListener("input", () => {
    clearTimeout(clashQTimer);
    clashQTimer = setTimeout(() => {
      if (clashCache) renderClashTable(clashCache);
    }, 150);
  });

  let catQTimer = null;
  const resetCatPageAndRefresh = () => {
    catalogPage = 1;
    refreshCatalog();
  };
  $("#cat-q")?.addEventListener("input", () => {
    clearTimeout(catQTimer);
    catQTimer = setTimeout(resetCatPageAndRefresh, 250);
  });
  $("#cat-health")?.addEventListener("change", resetCatPageAndRefresh);
  $("#cat-tier")?.addEventListener("change", resetCatPageAndRefresh);
  $("#cat-page-size")?.addEventListener("change", resetCatPageAndRefresh);
  $("#cat-prev")?.addEventListener("click", () => {
    if (catalogPage > 1) {
      catalogPage -= 1;
      refreshCatalog();
    }
  });
  $("#cat-next")?.addEventListener("click", () => {
    const pages = catalogCache && catalogCache.pages ? catalogCache.pages : 1;
    if (catalogPage < pages) {
      catalogPage += 1;
      refreshCatalog();
    }
  });

  // auto-refresh register status/logs only — never re-fill form (avoids wipe)
  logTimer = setInterval(() => {
    if ($("#app-shell")?.classList.contains("hidden")) return;
    if ($("#page-register")?.classList.contains("active")) {
      if ($("#log-follow")?.checked !== false) refreshRegister({ reloadForm: false });
    }
  }, 4000);

  ensureAuthed().then((ok) => {
    if (ok) showPage("register");
  });
})();
