// src/pages/Accounts/AccountsPage.jsx
// Account pool: filter + paginate cpa_auths (complete = access+refresh on disk).
// Delete is local-only (DELETE /api/accounts/{name}); does not touch tebi.
import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import * as api from "../../api/client.js";
import { session } from "../../store/session.js";
import { showOpsFeedback } from "../../store/feedback.js";
import { Button, Select, Kpi } from "../../ui/index.js";
import { formatApiError } from "../../lib/format.js";
import "../../styles/accounts.css";

const COMPLETE_OPTS = [
  { value: "", label: "全部" },
  { value: "1", label: "仅 complete" },
  { value: "0", label: "仅 incomplete" },
];

const PAGE_SIZE_OPTS = [
  { value: "25", label: "25" },
  { value: "50", label: "50" },
  { value: "100", label: "100" },
];

function buildAccountsQuery({ q, complete, page, pageSize }) {
  const params = new URLSearchParams();
  const qq = (q || "").trim();
  if (qq) params.set("q", qq);
  if (complete !== "" && complete != null) params.set("complete", complete);
  params.set("page", String(page || 1));
  params.set("page_size", String(pageSize || 50));
  return params.toString();
}

export function AccountsPage() {
  const [q, setQ] = useState("");
  const [complete, setComplete] = useState("");
  const [pageSize, setPageSize] = useState("50");
  const [page, setPage] = useState(1);
  const [data, setData] = useState(null);
  const [err, setErr] = useState("");
  const [result, setResult] = useState("");
  const [busy, setBusy] = useState(false);
  const qTimer = useRef(null);
  const qFirst = useRef(true);
  // Keep latest filters for debounced load without stale closures.
  const filtersRef = useRef({ q: "", complete: "", pageSize: "50", page: 1 });
  filtersRef.current = { q, complete, pageSize, page };

  const handleAuth = (e) => {
    if (e && e.status === 401) {
      session.value = { ...session.value, authenticated: false };
      return true;
    }
    return false;
  };

  const load = useCallback(async (override = {}) => {
    const f = { ...filtersRef.current, ...override };
    const pg = f.page != null ? f.page : 1;
    setBusy(true);
    setErr("");
    try {
      const qs = buildAccountsQuery({
        q: f.q,
        complete: f.complete,
        page: pg,
        pageSize: f.pageSize,
      });
      const res = await api.listAccounts(qs);
      setData(res);
      if (override.page != null) setPage(override.page);
      else if (res && res.page != null) setPage(res.page);
    } catch (e) {
      if (handleAuth(e)) return;
      setData(null);
      setErr(formatApiError(e));
    } finally {
      setBusy(false);
    }
  }, []);

  // Initial load
  useEffect(() => {
    load({ page: 1 });
  }, [load]);

  // complete / pageSize change → reset page
  useEffect(() => {
    // skip first paint double-fetch: only when filters change after mount
    // handled by explicit handlers below for complete/pageSize
  }, []);

  // Debounced search (skip mount — initial load handles first paint)
  useEffect(() => {
    if (qFirst.current) {
      qFirst.current = false;
      return undefined;
    }
    if (qTimer.current) clearTimeout(qTimer.current);
    qTimer.current = setTimeout(() => {
      setPage(1);
      load({ page: 1, q });
    }, 300);
    return () => {
      if (qTimer.current) clearTimeout(qTimer.current);
    };
  }, [q, load]);

  function onCompleteChange(v) {
    setComplete(v);
    setPage(1);
    load({ page: 1, complete: v });
  }

  function onPageSizeChange(v) {
    setPageSize(v);
    setPage(1);
    load({ page: 1, pageSize: v });
  }

  async function onDelete(name) {
    if (!name) return;
    if (!window.confirm(`删除 ${name}？仅删本地 cpa_auths，不影响 tebi。`)) {
      return;
    }
    try {
      const r = await api.deleteAccount(name);
      setResult(typeof r === "string" ? r : JSON.stringify(r, null, 2));
      showOpsFeedback(`已删除 ${name}`, "ok", { toast: true, sticky: false });
      await load();
    } catch (e) {
      if (handleAuth(e)) return;
      setResult(String(e.message || e));
      showOpsFeedback(`删除失败: ${formatApiError(e)}`, "err");
    }
  }

  const items = (data && data.items) || [];
  const total = data ? data.total || 0 : 0;
  const c = data ? data.complete || 0 : 0;
  const inc = Math.max(0, total - c);
  const dc = data && data.disk_complete != null ? data.disk_complete : null;
  const pages = data ? data.pages || 1 : 1;
  const curPage = data ? data.page || page : page;

  return (
    <section class="page page-accounts">
      <header class="page-head">
        <div>
          <h1>账号池</h1>
          <p class="hint">
            cpa_auths 落盘 complete（access+refresh）。注册主链路止于磁盘；导入 tebi 另走
            Import。
          </p>
        </div>
        <div class="toolbar">
          <Button variant="ghost" busy={busy} onClick={() => load()}>
            刷新
          </Button>
        </div>
      </header>

      <div class="card filter-bar">
        <label class="inline">
          搜索{" "}
          <input
            type="search"
            value={q}
            placeholder="email / 文件名"
            onInput={(e) => setQ(e.currentTarget.value)}
          />
        </label>
        <label class="inline">
          完整度{" "}
          <Select
            value={complete}
            options={COMPLETE_OPTS}
            onChange={onCompleteChange}
          />
        </label>
        <label class="inline">
          每页{" "}
          <Select
            value={pageSize}
            options={PAGE_SIZE_OPTS}
            onChange={onPageSizeChange}
          />
        </label>
        <Button
          variant="ghost"
          size="sm"
          disabled={curPage <= 1 || busy}
          onClick={() => load({ page: Math.max(1, curPage - 1) })}
        >
          上一页
        </Button>
        <Button
          variant="ghost"
          size="sm"
          disabled={curPage >= pages || busy}
          onClick={() => load({ page: curPage + 1 })}
        >
          下一页
        </Button>
        <span class="hint">
          page {curPage}/{pages}
        </span>
      </div>

      {err ? (
        <p class="hint err-text">{err}</p>
      ) : (
        <div class="kpi-grid compact acc-summary">
          <Kpi
            label="total (本页)"
            value={String(total)}
            hint={data?.path ? `path=${data.path}` : ""}
          />
          <Kpi
            label="complete"
            value={String(c)}
            hint={`第 ${curPage}/${pages} 页`}
            class={c > 0 ? "ok" : ""}
          />
          <Kpi
            label="incomplete"
            value={String(inc)}
            class={inc > 0 ? "warn" : ""}
          />
          {dc != null ? (
            <Kpi label="disk complete" value={String(dc)} hint="全盘" class="ok" />
          ) : null}
        </div>
      )}

      <div class="card table-wrap">
        <table class="data">
          <thead>
            <tr>
              <th>状态</th>
              <th>email</th>
              <th>mtime</th>
              <th>priority</th>
              <th>kind</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            {!items.length ? (
              <tr>
                <td colspan="6" class="hint">
                  无账号
                </td>
              </tr>
            ) : (
              items.map((it) => (
                <tr key={it.name || it.email}>
                  <td>
                    <span class={`badge ${it.complete ? "ok" : "fail"}`}>
                      {it.complete ? "complete" : "incomplete"}
                    </span>{" "}
                    {it.disabled ? (
                      <span class="badge fail">disabled</span>
                    ) : null}{" "}
                    {it.expired ? <span class="badge fail">expired</span> : null}
                  </td>
                  <td>
                    <div>{it.email || ""}</div>
                    <div class="hint">{it.name || ""}</div>
                  </td>
                  <td>{it.mtime_iso || ""}</td>
                  <td>{it.priority != null ? String(it.priority) : "—"}</td>
                  <td>{it.auth_kind || "—"}</td>
                  <td class="ops">
                    <Button
                      variant="danger"
                      size="sm"
                      onClick={() => onDelete(it.name)}
                    >
                      删除
                    </Button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {result ? <pre class="log compact">{result}</pre> : null}
    </section>
  );
}
