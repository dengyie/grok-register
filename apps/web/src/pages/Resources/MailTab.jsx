// MailTab — email provider config + hotmail cred import (sole mail-cred surface)
import { useCallback, useEffect, useState } from "preact/hooks";
import * as api from "../../api/client.js";
import { session } from "../../store/session.js";
import { showOpsFeedback } from "../../store/feedback.js";
import { Button, Select } from "../../ui/index.js";
import { formatApiError } from "../../lib/format.js";

const PROVIDERS = [
  "cloudflare",
  "cloudmail",
  "duckmail",
  "yyds",
  "gmail",
  "hotmail",
  "outlookmail",
];

const STRATEGIES = ["round_robin", "random", "failover"];

const SECRET_KEYS = [
  "cloudflare_api_key",
  "duckmail_api_key",
  "yyds_api_key",
  "cloudmail_password",
];

const EMPTY = {
  email_provider: "cloudflare",
  email_provider_strategy: "round_robin",
  defaultDomains: "",
  cloudflare_api_base: "",
  cloudflare_api_key: "",
  duckmail_api_key: "",
  yyds_api_key: "",
  cloudmail_url: "",
  cloudmail_admin_email: "",
  cloudmail_password: "",
  gmail_imap_user: "",
  hotmail_accounts_file: "",
  hotmail_allow_plus_alias: false,
};

function pretty(v) {
  try {
    return typeof v === "string" ? v : JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

export function MailTab() {
  const [form, setForm] = useState(EMPTY);
  const [result, setResult] = useState("");
  const [credText, setCredText] = useState("");
  const [credMode, setCredMode] = useState("append");
  const [busy, setBusy] = useState("");

  function auth(e) {
    if (e && e.status === 401) {
      session.value = { ...session.value, authenticated: false };
      return true;
    }
    return false;
  }

  const load = useCallback(async () => {
    setBusy("load");
    try {
      const data = await api.getConfig();
      const c = data.config || {};
      const next = { ...EMPTY };
      for (const k of Object.keys(EMPTY)) {
        if (c[k] == null) continue;
        if (k === "hotmail_allow_plus_alias") {
          next[k] = c[k] === true || c[k] === "true" || c[k] === 1 || c[k] === "1";
        } else if (SECRET_KEYS.includes(k)) {
          // show masked placeholder; empty submit keeps old
          next[k] = "";
        } else {
          next[k] = String(c[k]);
        }
      }
      setForm(next);
      setResult(
        pretty({
          loaded: true,
          provider: c.email_provider,
          domains: c.defaultDomains,
        }),
      );
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

  async function save() {
    setBusy("save");
    try {
      const partial = { ...form };
      for (const k of SECRET_KEYS) {
        const v = String(partial[k] || "");
        if (v === "" || v.startsWith("***")) delete partial[k];
      }
      const data = await api.putConfig({ config: partial });
      setResult(pretty(data));
      showOpsFeedback("邮箱配置已保存", "ok");
      // clear secret inputs after save
      setForm((p) => {
        const n = { ...p };
        for (const k of SECRET_KEYS) n[k] = "";
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

  async function importCreds() {
    setBusy("cred");
    try {
      const fd = new FormData();
      fd.append("content", credText || "");
      fd.append("mode", credMode || "append");
      const body = await api.importMailText(fd);
      setResult(pretty(body));
      showOpsFeedback("凭证已导入", "ok");
    } catch (e) {
      if (auth(e)) return;
      setResult(pretty({ error: formatApiError(e) }));
      showOpsFeedback(`导入失败: ${formatApiError(e)}`, "err");
    } finally {
      setBusy("");
    }
  }

  return (
    <div class="resources-tab mail-tab">
      <div class="toolbar wrap mail-toolbar">
        <Button variant="ghost" busy={busy === "load"} onClick={load}>
          重载
        </Button>
        <Button
          variant="primary"
          busy={busy === "save"}
          onClick={save}
        >
          保存邮箱配置
        </Button>
      </div>

      <form class="card grid mail-form" onSubmit={(e) => e.preventDefault()}>
        <label>
          email_provider
          <Select
            value={form.email_provider}
            options={PROVIDERS}
            onChange={(v) => set({ email_provider: v })}
          />
        </label>
        <label>
          email_provider_strategy
          <Select
            value={form.email_provider_strategy}
            options={STRATEGIES}
            onChange={(v) => set({ email_provider_strategy: v })}
          />
        </label>
        <label class="span2">
          defaultDomains
          <input
            value={form.defaultDomains}
            placeholder="a.com,b.com"
            onInput={(e) => set({ defaultDomains: e.currentTarget.value })}
          />
        </label>
        <label>
          cloudflare_api_base
          <input
            value={form.cloudflare_api_base}
            onInput={(e) => set({ cloudflare_api_base: e.currentTarget.value })}
          />
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
        <label>
          cloudmail_url
          <input
            value={form.cloudmail_url}
            onInput={(e) => set({ cloudmail_url: e.currentTarget.value })}
          />
        </label>
        <label>
          cloudmail_admin_email
          <input
            value={form.cloudmail_admin_email}
            onInput={(e) =>
              set({ cloudmail_admin_email: e.currentTarget.value })
            }
          />
        </label>
        <label>
          cloudmail_password
          <input
            type="password"
            value={form.cloudmail_password}
            placeholder="leave empty to keep"
            onInput={(e) => set({ cloudmail_password: e.currentTarget.value })}
          />
        </label>
        <label>
          gmail_imap_user
          <input
            value={form.gmail_imap_user}
            onInput={(e) => set({ gmail_imap_user: e.currentTarget.value })}
          />
        </label>
        <label>
          hotmail_accounts_file
          <input
            value={form.hotmail_accounts_file}
            placeholder="mail_credentials.txt"
            onInput={(e) =>
              set({ hotmail_accounts_file: e.currentTarget.value })
            }
          />
        </label>
        <label class="check span2">
          <input
            type="checkbox"
            checked={!!form.hotmail_allow_plus_alias}
            onChange={(e) =>
              set({ hotmail_allow_plus_alias: e.currentTarget.checked })
            }
          />{" "}
          hotmail_allow_plus_alias（生产勿开）
        </label>
      </form>

      <div class="card">
        <h2>Hotmail / Outlook 凭证导入</h2>
        <p class="hint">每行: email----password----clientId----refresh_token</p>
        <textarea
          rows={6}
          value={credText}
          placeholder="email----password----clientId----refresh_token"
          onInput={(e) => setCredText(e.currentTarget.value)}
        />
        <label class="inline">
          mode{" "}
          <select
            value={credMode}
            onChange={(e) => setCredMode(e.currentTarget.value)}
          >
            <option value="append">append</option>
            <option value="replace">replace</option>
          </select>
        </label>
        <Button variant="ghost" busy={busy === "cred"} onClick={importCreds}>
          导入凭证
        </Button>
      </div>

      {result ? <pre class="log">{result}</pre> : null}
    </div>
  );
}
