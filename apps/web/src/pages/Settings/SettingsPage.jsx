// Settings — global proxy/turnstile/cpa knobs + secrets + bearer + ops only.
// Full email_provider UI lives on Resources/MailTab (no duplicate hotmail form).
import { useCallback, useEffect, useState } from "preact/hooks";
import * as api from "../../api/client.js";
import { getToken, setToken } from "../../api/client.js";
import { session } from "../../store/session.js";
import { showOpsFeedback } from "../../store/feedback.js";
import { Button } from "../../ui/index.js";
import { formatApiError } from "../../lib/format.js";
import "../../styles/settings.css";

const SECRET_KEYS = [
  "cloudflare_api_key",
  "duckmail_api_key",
  "yyds_api_key",
];

const EMPTY = {
  email_provider: "",
  defaultDomains: "",
  proxy: "",
  proxy_rotate_mode: "",
  proxy_list: "",
  turnstile_stuck_timeout: "",
  cpa_probe_chat: "",
  cpa_remote_inject: "",
  cloudflare_api_key: "",
  duckmail_api_key: "",
  yyds_api_key: "",
};

function pretty(v) {
  try {
    return typeof v === "string" ? v : JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

function auth(e) {
  if (e && e.status === 401) {
    session.value = { ...session.value, authenticated: false };
    return true;
  }
  return false;
}

export function SettingsPage() {
  const [form, setForm] = useState(EMPTY);
  const [token, setTokenLocal] = useState(getToken());
  const [result, setResult] = useState("");
  const [busy, setBusy] = useState("");

  const load = useCallback(async () => {
    setBusy("load");
    try {
      const data = await api.getConfig();
      const c = data.config || {};
      const next = { ...EMPTY };
      for (const k of Object.keys(EMPTY)) {
        if (SECRET_KEYS.includes(k)) {
          next[k] = "";
          continue;
        }
        if (c[k] == null) continue;
        if (k === "cpa_probe_chat" || k === "cpa_remote_inject") {
          if (c[k] === true) next[k] = "true";
          else if (c[k] === false) next[k] = "false";
          else next[k] = String(c[k]);
        } else if (k === "proxy_list") {
          next[k] = Array.isArray(c[k]) ? c[k].join("\n") : String(c[k]);
        } else {
          next[k] = String(c[k]);
        }
      }
      setForm(next);
      setResult(pretty(data));
    } catch (e) {
      if (auth(e)) return;
      setResult(String(e.message || e));
    } finally {
      setBusy("");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  function set(partial) {
    setForm((p) => ({ ...p, ...partial }));
  }

  async function save(ev) {
    if (ev) ev.preventDefault();
    setBusy("save");
    try {
      const partial = {};
      for (const [k, raw] of Object.entries(form)) {
        let v = raw;
        if (SECRET_KEYS.includes(k)) {
          if (v === "" || String(v).startsWith("***")) continue;
          partial[k] = v;
          continue;
        }
        if (k === "cpa_probe_chat" || k === "cpa_remote_inject") {
          if (v === "") continue;
          if (v === "true") v = true;
          else if (v === "false") v = false;
          partial[k] = v;
          continue;
        }
        if (k === "turnstile_stuck_timeout") {
          if (v === "") continue;
          partial[k] = Number(v);
          continue;
        }
        if (v === "" && k !== "proxy_list") continue;
        partial[k] = v;
      }
      const data = await api.putConfig({ config: partial });
      setResult(pretty(data));
      showOpsFeedback("设置已保存", "ok");
      setForm((p) => {
        const n = { ...p };
        for (const sk of SECRET_KEYS) n[sk] = "";
        return n;
      });
    } catch (e) {
      if (auth(e)) return;
      setResult(String(e.message || e));
      showOpsFeedback(`保存失败: ${formatApiError(e)}`, "err");
    } finally {
      setBusy("");
    }
  }

  function saveBearer() {
    setToken(token.trim());
    showOpsFeedback(
      token.trim() ? "Bearer 已写入 sessionStorage" : "Bearer 已清除",
      "ok",
      { toast: true, sticky: false },
    );
  }

  async function runSelfcheck() {
    setBusy("selfcheck");
    try {
      const data = await api.selfcheck();
      setResult(pretty(data));
      showOpsFeedback("自检完成", "ok");
    } catch (e) {
      if (auth(e)) return;
      setResult(String(e.message || e));
      showOpsFeedback(`自检失败: ${formatApiError(e)}`, "err");
    } finally {
      setBusy("");
    }
  }

  async function runCleanup() {
    if (
      !window.confirm(
        "确认清理过盾残留？\n\n会对本机 orphan chromium / 残留目录做清理（dry_run=false）。",
      )
    ) {
      showOpsFeedback("已取消清理", "info", { toast: true, sticky: false });
      return;
    }
    setBusy("cleanup");
    try {
      const data = await api.cleanupOrphans();
      setResult(pretty(data));
      showOpsFeedback("清理完成", "ok");
    } catch (e) {
      if (auth(e)) return;
      setResult(String(e.message || e));
      showOpsFeedback(`清理失败: ${formatApiError(e)}`, "err");
    } finally {
      setBusy("");
    }
  }

  return (
    <section class="page page-settings">
      <header class="page-head">
        <div>
          <h1>设置</h1>
          <p class="hint">
            Secrets 显示为掩码；空或 *** 保存时保留旧值。Bearer 仅脚本用。邮箱通道完整表单在
            「资源 → 邮箱」。
          </p>
        </div>
        <div class="toolbar">
          <Button variant="ghost" busy={busy === "load"} onClick={load}>
            Reload
          </Button>
          <Button variant="primary" busy={busy === "save"} onClick={save}>
            Save
          </Button>
        </div>
      </header>

      <form class="card grid settings-form" onSubmit={save}>
        <label>
          email_provider
          <input
            value={form.email_provider}
            onInput={(e) => set({ email_provider: e.currentTarget.value })}
          />
        </label>
        <label>
          defaultDomains
          <input
            value={form.defaultDomains}
            onInput={(e) => set({ defaultDomains: e.currentTarget.value })}
          />
        </label>
        <label>
          proxy
          <input
            value={form.proxy}
            onInput={(e) => set({ proxy: e.currentTarget.value })}
          />
        </label>
        <label>
          proxy_rotate_mode
          <input
            value={form.proxy_rotate_mode}
            onInput={(e) => set({ proxy_rotate_mode: e.currentTarget.value })}
          />
        </label>
        <label class="span2">
          proxy_list
          <textarea
            rows={3}
            value={form.proxy_list}
            onInput={(e) => set({ proxy_list: e.currentTarget.value })}
          />
        </label>
        <label>
          turnstile_stuck_timeout
          <input
            type="number"
            step="1"
            value={form.turnstile_stuck_timeout}
            onInput={(e) =>
              set({ turnstile_stuck_timeout: e.currentTarget.value })
            }
          />
        </label>
        <label>
          cpa_probe_chat
          <select
            value={form.cpa_probe_chat}
            onChange={(e) => set({ cpa_probe_chat: e.currentTarget.value })}
          >
            <option value="">(unchanged)</option>
            <option value="false">false</option>
            <option value="true">true</option>
          </select>
        </label>
        <label>
          cpa_remote_inject (intent)
          <select
            value={form.cpa_remote_inject}
            onChange={(e) => set({ cpa_remote_inject: e.currentTarget.value })}
          >
            <option value="">(unchanged)</option>
            <option value="false">false</option>
            <option value="true">true</option>
          </select>
        </label>
        <label>
          cloudflare_api_key
          <input
            type="password"
            value={form.cloudflare_api_key}
            placeholder="leave empty to keep"
            onInput={(e) => set({ cloudflare_api_key: e.currentTarget.value })}
          />
        </label>
        <label>
          duckmail_api_key
          <input
            type="password"
            value={form.duckmail_api_key}
            placeholder="leave empty to keep"
            onInput={(e) => set({ duckmail_api_key: e.currentTarget.value })}
          />
        </label>
        <label>
          yyds_api_key
          <input
            type="password"
            value={form.yyds_api_key}
            placeholder="leave empty to keep"
            onInput={(e) => set({ yyds_api_key: e.currentTarget.value })}
          />
        </label>
      </form>

      <div class="card">
        <h2>脚本 Bearer token（可选）</h2>
        <label class="inline">
          API Token{" "}
          <input
            type="password"
            value={token}
            placeholder="optional bearer"
            autocomplete="off"
            onInput={(e) => setTokenLocal(e.currentTarget.value)}
          />
        </label>
        <Button variant="ghost" onClick={saveBearer}>
          Save token
        </Button>
      </div>

      <div class="card">
        <h2>运维自检</h2>
        <div class="toolbar wrap">
          <Button
            variant="ghost"
            busy={busy === "selfcheck"}
            onClick={runSelfcheck}
          >
            运行自检
          </Button>
          <Button
            variant="ghost"
            busy={busy === "cleanup"}
            onClick={runCleanup}
          >
            清理过盾残留
          </Button>
        </div>
        {result ? <pre class="log">{result}</pre> : null}
      </div>
    </section>
  );
}
