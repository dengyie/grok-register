// ImportTab — 2×2 cards: nodes file, mail text, auths file, pack zip
import { useRef, useState } from "preact/hooks";
import * as api from "../../api/client.js";
import { session } from "../../store/session.js";
import { showOpsFeedback } from "../../store/feedback.js";
import { Button } from "../../ui/index.js";
import { formatApiError } from "../../lib/format.js";

function pretty(v) {
  try {
    return typeof v === "string" ? v : JSON.stringify(v, null, 2);
  } catch {
    return String(v);
  }
}

export function ImportTab() {
  const [result, setResult] = useState("");
  const [busy, setBusy] = useState("");
  const nodesFile = useRef(null);
  const authsFile = useRef(null);
  const packFile = useRef(null);
  const [nodesDry, setNodesDry] = useState(true);
  const [nodesReplace, setNodesReplace] = useState(false);
  const [mailText, setMailText] = useState("");
  const [mailMode, setMailMode] = useState("append");
  const [authsRemote, setAuthsRemote] = useState(false);
  const [packApply, setPackApply] = useState(false);

  function auth(e) {
    if (e && e.status === 401) {
      session.value = { ...session.value, authenticated: false };
      return true;
    }
    return false;
  }

  async function run(key, fn) {
    setBusy(key);
    try {
      const body = await fn();
      setResult(pretty(body));
      showOpsFeedback(`导入完成 · ${key}`, "ok", { toast: true, sticky: false });
    } catch (e) {
      if (auth(e)) return;
      setResult(pretty({ error: formatApiError(e) }));
      showOpsFeedback(`导入失败: ${formatApiError(e)}`, "err");
    } finally {
      setBusy("");
    }
  }

  return (
    <div class="resources-tab import-tab">
      <div class="import-grid">
        <div class="card">
          <h2>Nodes / proxies 文件</h2>
          <input type="file" ref={nodesFile} />
          <label class="check">
            <input
              type="checkbox"
              checked={nodesDry}
              onChange={(e) => setNodesDry(e.currentTarget.checked)}
            />{" "}
            dry-run
          </label>
          <label class="check">
            <input
              type="checkbox"
              checked={nodesReplace}
              onChange={(e) => setNodesReplace(e.currentTarget.checked)}
            />{" "}
            replace
          </label>
          <Button
            variant="ghost"
            busy={busy === "nodes"}
            onClick={() =>
              run("nodes", async () => {
                const f = nodesFile.current?.files?.[0];
                if (!f) throw new Error("pick a file");
                const fd = new FormData();
                fd.append("file", f);
                fd.append("dry_run", nodesDry ? "true" : "false");
                fd.append("replace", nodesReplace ? "true" : "false");
                return api.importNodesFile(fd);
              })
            }
          >
            Import nodes
          </Button>
        </div>

        <div class="card">
          <h2>Mail credentials</h2>
          <p class="hint">与「资源 → 邮箱」凭证导入同源 API；完整 hotmail 表单见邮箱 Tab。</p>
          <textarea
            rows={5}
            value={mailText}
            placeholder="email----password----clientId----refresh_token"
            onInput={(e) => setMailText(e.currentTarget.value)}
          />
          <label class="inline">
            mode{" "}
            <select
              value={mailMode}
              onChange={(e) => setMailMode(e.currentTarget.value)}
            >
              <option value="append">append</option>
              <option value="replace">replace</option>
            </select>
          </label>
          <Button
            variant="ghost"
            busy={busy === "mail"}
            onClick={() =>
              run("mail", async () => {
                const fd = new FormData();
                fd.append("content", mailText || "");
                fd.append("mode", mailMode || "append");
                return api.importMailText(fd);
              })
            }
          >
            Import mail
          </Button>
        </div>

        <div class="card">
          <h2>Account / token dumps</h2>
          <input type="file" ref={authsFile} />
          <label class="check">
            <input
              type="checkbox"
              checked={authsRemote}
              onChange={(e) => setAuthsRemote(e.currentTarget.checked)}
            />{" "}
            allow remote CPA inject
          </label>
          <Button
            variant="ghost"
            busy={busy === "auths"}
            onClick={() =>
              run("auths", async () => {
                const f = authsFile.current?.files?.[0];
                if (!f) throw new Error("pick a file");
                const fd = new FormData();
                fd.append("file", f);
                fd.append("no_remote", authsRemote ? "false" : "true");
                return api.importAuthsFile(fd);
              })
            }
          >
            Import auths
          </Button>
        </div>

        <div class="card">
          <h2>Config pack (zip)</h2>
          <input type="file" ref={packFile} />
          <label class="check">
            <input
              type="checkbox"
              checked={packApply}
              onChange={(e) => setPackApply(e.currentTarget.checked)}
            />{" "}
            apply
          </label>
          <Button
            variant="ghost"
            busy={busy === "pack"}
            onClick={() =>
              run("pack", async () => {
                const f = packFile.current?.files?.[0];
                if (!f) throw new Error("pick a file");
                const fd = new FormData();
                fd.append("file", f);
                fd.append("apply", packApply ? "true" : "false");
                return api.importPackFile(fd);
              })
            }
          >
            Import pack
          </Button>
        </div>
      </div>
      {result ? <pre class="log">{result}</pre> : null}
    </div>
  );
}
