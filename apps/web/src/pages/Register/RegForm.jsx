// src/pages/Register/RegForm.jsx
// Left panel: start parameters. Mirrors legacy register form + advanced knobs.
// State is lifted to the parent (RegisterPage) so the 4s poll never wipes edits:
// RegForm only receives initial values once via `initial` (snapshot from config);
// subsequent props updates are ignored while regFormDirty is true (parent guards).
import { useEffect } from "preact/hooks";
import { Field, Select } from "../../ui/index.js";
import { regFormDirty } from "../../store/run.js";

const PROVIDERS = [
  "cloudflare",
  "cloudmail",
  "duckmail",
  "yyds",
  "gmail",
  "hotmail",
  "outlookmail",
];

function providerKeyField(provider) {
  const p = (provider || "").toLowerCase();
  if (p === "cloudflare") return "cloudflare_api_key";
  if (p === "duckmail") return "duckmail_api_key";
  if (p === "yyds") return "yyds_api_key";
  if (p === "cloudmail") return "cloudmail_password";
  return null;
}

const DEFAULTS = {
  email_provider: "cloudflare",
  mailKey: "",
  mailKeyPlaceholder: "",
  defaultDomains: "",
  target: 100,
  threads: 1,
  mode: "ordinary",
  tag: "batch_web",
  chunk: 3,
  turnstile: 150,
  probeChat: false,
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

export function RegForm({ value, onChange, advancedOpen, onToggleAdvanced }) {
  // Whenever any field changes mark the form dirty so poll won't reload.
  useEffect(() => {
    // no-op; dirty handling is done in field onChange below
  }, []);

  const v = { ...DEFAULTS, ...value };
  const isSupervisor = (v.kind || "grok_supervisor") === "grok_supervisor";

  function set(partial) {
    regFormDirty.value = true;
    onChange({ ...v, ...partial });
  }

  // Show provider key placeholder as the redacted saved value, like legacy.
  const keyField = providerKeyField(v.email_provider);
  const mailKeyPlaceholder =
    (keyField && v[`saved_${keyField}`]) ||
    (keyField && v.savedSecret) ||
    "按邮箱服务写入对应 key";

  return (
    <form
      class="stack-form"
      onSubmit={(e) => e.preventDefault()}
    >
      <div class="field-grid">
        <Field label="邮箱服务">
          <Select
            options={PROVIDERS}
            value={v.email_provider}
            onChange={(val) => set({ email_provider: val })}
          />
        </Field>
        <Field label="API KEY / 凭证">
          <input
            type="password"
            placeholder={mailKeyPlaceholder}
            autocomplete="off"
            value={v.mailKey}
            onInput={(e) => set({ mailKey: e.currentTarget.value })}
          />
        </Field>
        <Field label="域名" span2>
          <input
            placeholder="多域名用英文逗号，如 a.com,b.com"
            value={v.defaultDomains}
            onInput={(e) => set({ defaultDomains: e.currentTarget.value })}
          />
        </Field>
        <Field label="数量 target">
          <input
            type="number"
            min="1"
            value={v.target}
            onInput={(e) =>
              set({ target: e.currentTarget.value === "" ? "" : Number(e.currentTarget.value) })
            }
          />
        </Field>
        <Field label="线程 threads">
          <input
            type="number"
            min="1"
            max="32"
            value={v.threads}
            onInput={(e) =>
              set({ threads: e.currentTarget.value === "" ? "" : Number(e.currentTarget.value) })
            }
          />
        </Field>
        <Field label="模式">
          <Select
            options={[
              { value: "ordinary", label: "ordinary · Clash 机房" },
              { value: "residential", label: "residential · 家宽" },
            ]}
            value={v.mode}
            onChange={(val) => set({ mode: val })}
          />
        </Field>
        <Field label="tag">
          <input
            value={v.tag}
            onInput={(e) => set({ tag: e.currentTarget.value })}
          />
        </Field>
      </div>

      <div class="info-box">
        默认 disk-first：中途不 tebi 注入。完整日志在「日志」页。
      </div>

      {/* Batch + proxy + advanced merged into ONE advanced drawer (spec IA). */}
      <details class="advanced" open={advancedOpen} onToggle={onToggleAdvanced}>
        <summary>高级启动 · 批参数 / 代理 / kind / product / SKIP_CLASH / NODE_SCORE</summary>
        <div class="field-grid">
          <Field label="SUPERVISOR_CHUNK" class={isSupervisor ? "" : "hidden"}>
            <input
              type="number"
              min="1"
              max="20"
              value={v.chunk}
              disabled={!isSupervisor}
              onInput={(e) =>
                set({ chunk: e.currentTarget.value === "" ? "" : Number(e.currentTarget.value) })
              }
            />
          </Field>
          <Field label="Turnstile 卡住超时 (秒)">
            <input
              type="number"
              min="5"
              value={v.turnstile}
              onInput={(e) =>
                set({ turnstile: e.currentTarget.value === "" ? "" : Number(e.currentTarget.value) })
              }
            />
          </Field>
          <label
            class="check muted"
            title="Supervisor 硬编码 CPA_PROBE_CHAT=false"
          >
            <input type="checkbox" checked={false} disabled /> 注册后 chat 探针（强制关）
          </label>
          <label
            class="check"
            title="信息项：中途 mint 永不 tebi 注入（disk-first 产品契约）；取消勾选不会开启 mid-mint inject"
          >
            <input
              type="checkbox"
              checked={v.ssoOnly}
              onChange={(e) => set({ ssoOnly: e.currentTarget.checked })}
            />{" "}
            只产落盘 auth（默认 · 信息项）
          </label>
          <label class="check" title="仅批边界 CPA_BATCH_END_INJECT">
            <input
              type="checkbox"
              checked={v.batchEndInject}
              disabled={!isSupervisor}
              onChange={(e) => set({ batchEndInject: e.currentTarget.checked })}
            /> 批边界自动导入 CPA
          </label>
          <Field
            label="CPA_BATCH_IMPORT_EVERY"
            class={isSupervisor ? "" : "hidden"}
          >
            <input
              type="number"
              min="1"
              value={v.importEvery}
              disabled={!isSupervisor}
              onInput={(e) =>
                set({ importEvery: e.currentTarget.value === "" ? "" : Number(e.currentTarget.value) })
              }
            />
          </Field>

          <Field label="代理策略 proxy_rotate_mode">
            <Select
              options={[
                { value: "clash", label: "clash · 内置节点池轮换" },
                { value: "list", label: "list · PROXY_LIST / proxy_list" },
                { value: "off", label: "off · 固定出口" },
              ]}
              value={v.proxyMode}
              onChange={(val) => set({ proxyMode: val })}
            />
          </Field>
          <Field label="固定 proxy URL">
            <input
              placeholder="http://127.0.0.1:7897"
              value={v.proxy}
              onInput={(e) => set({ proxy: e.currentTarget.value })}
            />
          </Field>
          <Field label="代理池 proxy_list / PROXY_LIST" span2>
            <textarea
              rows="4"
              placeholder="每行一个代理；可留空走 Clash"
              value={v.proxyList}
              onInput={(e) => set({ proxyList: e.currentTarget.value })}
            />
          </Field>

          <Field label="kind">
            <Select
              options={[
                { value: "grok_supervisor", label: "grok_supervisor · 批量监督" },
                { value: "register_sh", label: "register_sh · 单次外壳" },
              ]}
              value={v.kind}
              onChange={(val) =>
                set({ kind: val, product: val === "grok_supervisor" ? "grok" : v.product })
              }
            />
          </Field>
          <Field label="product">
            <Select
              options={["grok", "mimo", "chatgpt"]}
              value={isSupervisor ? "grok" : v.product}
              disabled={isSupervisor}
              onChange={(val) => set({ product: val })}
            />
          </Field>
          <label class="check" title="SKIP_CLASH_PREFLIGHT=1 会跳过 Clash 批前测活；默认关">
            <input
              type="checkbox"
              checked={v.skipPreflight}
              onChange={(e) => set({ skipPreflight: e.currentTarget.checked })}
            /> 跳过 Clash 批前测活
          </label>
          <Field label="NODE_SCORE">
            <Select
              options={[
                { value: "", label: "(不传，吃环境)" },
                { value: "1", label: "1 · 打分排序" },
                { value: "0", label: "0 · 关闭打分" },
              ]}
              value={v.nodeScore}
              onChange={(val) => set({ nodeScore: val })}
            />
          </Field>
          <Field
            label="CPA_BATCH_IMPORT_SIZE"
            class={isSupervisor ? "" : "hidden"}
          >
            <input
              type="number"
              min="1"
              placeholder="100"
              value={v.importSize}
              disabled={!isSupervisor}
              onInput={(e) => set({ importSize: e.currentTarget.value })}
            />
          </Field>
          <Field
            label="CPA_BATCH_IMPORT_PAUSE"
            class={isSupervisor ? "" : "hidden"}
          >
            <input
              type="number"
              min="0"
              placeholder="3"
              value={v.importPause}
              disabled={!isSupervisor}
              onInput={(e) => set({ importPause: e.currentTarget.value })}
            />
          </Field>
          <label class="check span2" title="启动时把当前邮箱表单同步进 extra_env">
            <input
              type="checkbox"
              checked={v.syncMailEnv}
              onChange={(e) => set({ syncMailEnv: e.currentTarget.checked })}
            /> 同步 EMAIL_PROVIDER / DEFAULT_DOMAINS 到 extra_env
          </label>
        </div>
      </details>
    </form>
  );
}
