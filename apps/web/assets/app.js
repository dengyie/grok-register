(() => {
  const $ = (sel) => document.querySelector(sel);
  const tokenKey = "controlToken";

  function token() {
    return sessionStorage.getItem(tokenKey) || $("#token").value.trim();
  }

  function headers(json = false) {
    const h = {};
    if (json) h["Content-Type"] = "application/json";
    const t = token();
    if (t) h["Authorization"] = `Bearer ${t}`;
    return h;
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, opts);
    const text = await res.text();
    let body;
    try {
      body = text ? JSON.parse(text) : {};
    } catch {
      body = { detail: text };
    }
    if (!res.ok) {
      const detail = body.detail || res.statusText;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return body;
  }

  function showPage(name) {
    document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
    document.querySelectorAll("nav button").forEach((b) => b.classList.remove("active"));
    $(`#page-${name}`).classList.add("active");
    const btn = document.querySelector(`nav button[data-page="${name}"]`);
    if (btn) btn.classList.add("active");
    if (name === "overview") refreshOverview();
    if (name === "config") loadConfig();
    if (name === "runs") refreshRuns();
  }

  async function refreshOverview() {
    const el = $("#overview-status");
    const pre = $("#overview-json");
    try {
      const data = await api("/api/overview", { headers: headers() });
      const run = data.run;
      const alive = run && run.alive;
      el.textContent = `product_ok=${data.product_ok} · run=${alive ? "ALIVE" : "idle"} · complete=${run && run.complete != null ? run.complete : "—"} · zero=${run && run.consecutive_zero != null ? run.consecutive_zero : "—"}`;
      el.className = "card " + (alive ? "ok" : "muted");
      pre.textContent = JSON.stringify(data, null, 2);
    } catch (e) {
      el.textContent = String(e.message || e);
      el.className = "card";
      pre.textContent = "";
    }
  }

  async function loadConfig() {
    const pre = $("#config-result");
    try {
      const data = await api("/api/config", { headers: headers() });
      const c = data.config || {};
      const form = $("#config-form");
      for (const el of form.elements) {
        if (!el.name) continue;
        if (el.tagName === "SELECT") {
          const v = c[el.name];
          if (v === true || v === false) el.value = String(v);
          else if (v != null && v !== "") el.value = String(v);
          else el.value = "";
        } else if (c[el.name] != null) {
          el.value = String(c[el.name]);
        }
      }
      pre.textContent = JSON.stringify(data, null, 2);
    } catch (e) {
      pre.textContent = String(e.message || e);
    }
  }

  async function saveConfig(ev) {
    ev.preventDefault();
    const form = $("#config-form");
    const config = {};
    for (const el of form.elements) {
      if (!el.name || el.type === "submit" || el.type === "button") continue;
      let v = el.value;
      if (v === "") continue;
      if (el.name === "turnstile_stuck_timeout") v = Number(v);
      if (el.name === "cpa_probe_chat" || el.name === "cpa_remote_inject") {
        if (v === "true") v = true;
        else if (v === "false") v = false;
      }
      config[el.name] = v;
    }
    const pre = $("#config-result");
    try {
      const data = await api("/api/config", {
        method: "PUT",
        headers: headers(true),
        body: JSON.stringify({ config }),
      });
      pre.textContent = JSON.stringify(data, null, 2);
    } catch (e) {
      pre.textContent = String(e.message || e);
    }
  }

  function showImport(result) {
    $("#import-result").textContent = JSON.stringify(result, null, 2);
  }

  async function postMultipart(url, formData) {
    const res = await fetch(url, { method: "POST", headers: headers(), body: formData });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || res.statusText);
    return body;
  }

  async function importNodes() {
    const f = $("#nodes-file").files[0];
    if (!f) return showImport({ error: "pick a file" });
    const fd = new FormData();
    fd.append("file", f);
    fd.append("dry_run", $("#nodes-dry").checked ? "true" : "false");
    fd.append("replace", $("#nodes-replace").checked ? "true" : "false");
    try {
      showImport(await postMultipart("/api/import/nodes", fd));
    } catch (e) {
      showImport({ error: String(e.message || e) });
    }
  }

  async function importMail() {
    const fd = new FormData();
    fd.append("content", $("#mail-text").value);
    fd.append("mode", $("#mail-mode").value);
    try {
      showImport(await postMultipart("/api/import/mail", fd));
    } catch (e) {
      showImport({ error: String(e.message || e) });
    }
  }

  async function importAuths() {
    const f = $("#auths-file").files[0];
    if (!f) return showImport({ error: "pick a file" });
    const fd = new FormData();
    fd.append("file", f);
    fd.append("no_remote", $("#auths-remote").checked ? "false" : "true");
    try {
      showImport(await postMultipart("/api/import/auths", fd));
    } catch (e) {
      showImport({ error: String(e.message || e) });
    }
  }

  async function importPack() {
    const f = $("#pack-file").files[0];
    if (!f) return showImport({ error: "pick a file" });
    const fd = new FormData();
    fd.append("file", f);
    fd.append("apply", $("#pack-apply").checked ? "true" : "false");
    try {
      showImport(await postMultipart("/api/import/pack", fd));
    } catch (e) {
      showImport({ error: String(e.message || e) });
    }
  }

  async function refreshRuns() {
    try {
      const cur = await api("/api/runs/current", { headers: headers() });
      $("#run-status").textContent = JSON.stringify(cur, null, 2);
      const logs = await api("/api/runs/current/logs?tail=200", { headers: headers() });
      $("#run-log").textContent = (logs.path ? `# ${logs.path}\n` : "") + (logs.text || "");
    } catch (e) {
      $("#run-status").textContent = String(e.message || e);
    }
  }

  async function startRun(ev) {
    ev.preventDefault();
    const form = $("#run-form");
    const fd = new FormData(form);
    const body = {
      kind: fd.get("kind"),
      product: fd.get("product"),
      mode: fd.get("mode"),
      target: Number(fd.get("target") || 100),
      threads: Number(fd.get("threads") || 1),
      tag: fd.get("tag") || "batch_web",
      extra_env: {},
    };
    const skip = (fd.get("SKIP_CLASH_PREFLIGHT") || "").toString().trim();
    if (skip !== "") body.extra_env.SKIP_CLASH_PREFLIGHT = skip;
    try {
      const data = await api("/api/runs/start", {
        method: "POST",
        headers: headers(true),
        body: JSON.stringify(body),
      });
      $("#run-status").textContent = JSON.stringify(data, null, 2);
      await refreshRuns();
    } catch (e) {
      $("#run-status").textContent = String(e.message || e);
    }
  }

  async function stopRun() {
    try {
      const data = await api("/api/runs/stop", { method: "POST", headers: headers() });
      $("#run-status").textContent = JSON.stringify(data, null, 2);
      await refreshRuns();
    } catch (e) {
      $("#run-status").textContent = String(e.message || e);
    }
  }

  // wire
  $("#token").value = sessionStorage.getItem(tokenKey) || "";
  $("#saveToken").addEventListener("click", () => {
    sessionStorage.setItem(tokenKey, $("#token").value.trim());
    refreshOverview();
  });
  document.querySelectorAll("nav button").forEach((b) => {
    b.addEventListener("click", () => showPage(b.dataset.page));
  });
  $("#config-reload").addEventListener("click", loadConfig);
  $("#config-form").addEventListener("submit", saveConfig);
  $("#nodes-go").addEventListener("click", importNodes);
  $("#mail-go").addEventListener("click", importMail);
  $("#auths-go").addEventListener("click", importAuths);
  $("#pack-go").addEventListener("click", importPack);
  $("#run-form").addEventListener("submit", startRun);
  $("#run-stop").addEventListener("click", stopRun);
  $("#run-refresh").addEventListener("click", refreshRuns);

  setInterval(() => {
    if ($("#page-overview").classList.contains("active")) refreshOverview();
    if ($("#page-runs").classList.contains("active")) refreshRuns();
  }, 5000);

  refreshOverview();
})();
