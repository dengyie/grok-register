// NodesTab — Clash leaves + catalog subtabs + subscription import-url
import { useCallback, useEffect, useMemo, useState } from "preact/hooks";
import * as api from "../../api/client.js";
import { session } from "../../store/session.js";
import { showOpsFeedback } from "../../store/feedback.js";
import { Button, Select, Tabs, Chip } from "../../ui/index.js";
import { formatApiError, healthBadge } from "../../lib/format.js";

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

function filterClashLeaves(leaves, mode, q) {
  let out = leaves || [];
  if (mode === "pool") out = out.filter((n) => n.in_register_pool);
  else if (mode === "ok") out = out.filter((n) => n.health === "ok");
  else if (mode === "fail") out = out.filter((n) => n.health === "fail");
  const qq = (q || "").trim().toLowerCase();
  if (qq) out = out.filter((n) => (n.name || "").toLowerCase().includes(qq));
  return out.slice().sort((a, b) => {
    const pa = a.in_register_pool ? 0 : 1;
    const pb = b.in_register_pool ? 0 : 1;
    if (pa !== pb) return pa - pb;
    const rank = { ok: 0, unknown: 1, fail: 2 };
    const ha = rank[a.health || "unknown"] ?? 1;
    const hb = rank[b.health || "unknown"] ?? 1;
    if (ha !== hb) return ha - hb;
    const da = a.last_delay_ms != null ? a.last_delay_ms : 1e9;
    const db = b.last_delay_ms != null ? b.last_delay_ms : 1e9;
    return da - db;
  });
}

const CLASH_FILTER = [
  { value: "pool", label: "仅注册池" },
  { value: "all", label: "全部叶子" },
  { value: "ok", label: "仅可用" },
  { value: "fail", label: "仅失败" },
];

const HEALTH_OPTS = [
  { value: "", label: "全部" },
  { value: "ok", label: "可用" },
  { value: "fail", label: "失败" },
  { value: "unknown", label: "未测" },
];

const TIER_OPTS = [
  { value: "", label: "全部" },
  { value: "0", label: "机房 0" },
  { value: "1", label: "住宅 1" },
];

const PAGE_SIZES = [
  { value: "25", label: "25" },
  { value: "50", label: "50" },
  { value: "100", label: "100" },
];

export function NodesTab() {
  const [sub, setSub] = useState("clash"); // clash | catalog
  const [clash, setClash] = useState(null);
  const [clashFilter, setClashFilter] = useState("pool");
  const [clashQ, setClashQ] = useState("");
  const [clashResult, setClashResult] = useState("");
  const [busy, setBusy] = useState("");
  const [importUrl, setImportUrl] = useState("");
  const [importDry, setImportDry] = useState(true);

  // catalog state
  const [cat, setCat] = useState(null);
  const [catQ, setCatQ] = useState("");
  const [catHealth, setCatHealth] = useState("");
  const [catTier, setCatTier] = useState("");
  const [catPageSize, setCatPageSize] = useState("50");
  const [catPage, setCatPage] = useState(1);
  const [catResult, setCatResult] = useState("");
  const [addUrl, setAddUrl] = useState("");
  const [addLabel, setAddLabel] = useState("");
  const [addTags, setAddTags] = useState("");
  const [addTier, setAddTier] = useState("0");

  const refreshClash = useCallback(async () => {
    setBusy("clash");
    try {
      const data = await api.listClash();
      setClash(data);
      if (data && !data.ok) setClashResult(pretty(data));
      else setClashResult("");
    } catch (e) {
      if (auth(e)) return;
      setClash(null);
      setClashResult(String(e.message || e));
    } finally {
      setBusy("");
    }
  }, []);

  const refreshCatalog = useCallback(async (override = {}) => {
    setBusy("catalog");
    try {
      const page = override.page != null ? override.page : catPage;
      const q = override.q != null ? override.q : catQ;
      const health = override.health != null ? override.health : catHealth;
      const tier = override.tier != null ? override.tier : catTier;
      const pageSize =
        override.pageSize != null ? override.pageSize : catPageSize;
      const params = new URLSearchParams();
      const qq = String(q || "").trim();
      if (qq) params.set("q", qq);
      if (health) params.set("health", health);
      if (tier !== "") params.set("tier", tier);
      params.set("page", String(page));
      params.set("page_size", pageSize);
      params.set("sort", "priority");
      const data = await api.listCatalog(params.toString());
      setCat(data);
      if (override.page != null) setCatPage(override.page);
      else if (data.page != null) setCatPage(data.page);
    } catch (e) {
      if (auth(e)) return;
      setCat(null);
      setCatResult(String(e.message || e));
    } finally {
      setBusy("");
    }
  }, [catQ, catHealth, catTier, catPage, catPageSize]);

  useEffect(() => {
    if (sub === "clash") refreshClash();
    else refreshCatalog({ page: 1 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sub]);

  const shownLeaves = useMemo(() => {
    if (!clash || !clash.ok) return [];
    return filterClashLeaves(clash.leaves || [], clashFilter, clashQ);
  }, [clash, clashFilter, clashQ]);

  async function clashTestOne(name) {
    setClashResult("testing…");
    try {
      const r = await api.testClash({ names: [name] });
      setClashResult(pretty(r));
      await refreshClash();
    } catch (e) {
      if (auth(e)) return;
      setClashResult(String(e.message || e));
    }
  }

  async function clashTest(limit, poolOnly) {
    setClashResult("testing…");
    setBusy("test");
    try {
      let names = null;
      if (poolOnly) {
        const listing = clash || (await api.listClash());
        names = (listing.leaves || [])
          .filter((x) => x.in_register_pool)
          .map((x) => x.name);
      }
      const body = names
        ? { names, limit: names.length || 40 }
        : { limit: limit || 40 };
      const data = await api.testClash(body);
      setClashResult(pretty(data));
      await refreshClash();
    } catch (e) {
      if (auth(e)) return;
      setClashResult(String(e.message || e));
    } finally {
      setBusy("");
    }
  }

  async function doImportUrl() {
    const url = importUrl.trim();
    if (!url) {
      showOpsFeedback("请填写订阅 URL", "warn");
      return;
    }
    setBusy("import");
    try {
      const body = { url, dry_run: importDry };
      const data = await api.importClashUrl(body);
      setClashResult(pretty(data));
      const ms =
        data.total_ms != null
          ? data.total_ms
          : data.timings && data.timings.total_ms;
      const msTxt = ms != null ? ` · ${Math.round(ms)}ms` : "";
      showOpsFeedback(
        `Clash 导入${importDry ? " dry-run" : ""}完成${msTxt}`,
        "ok",
      );
      if (!importDry) await refreshClash();
    } catch (e) {
      if (auth(e)) return;
      setClashResult(String(e.message || e));
      showOpsFeedback(`导入失败: ${formatApiError(e)}`, "err");
    } finally {
      setBusy("");
    }
  }

  async function onCatalogAction(act, id, enabled) {
    try {
      if (act === "del") {
        if (!window.confirm(`删除节点 ${id}?`)) return;
        setCatResult(pretty(await api.deleteCatalogNode(id)));
      } else if (act === "toggle") {
        setCatResult(
          pretty(await api.patchCatalogNode(id, { enabled: !enabled })),
        );
      } else if (act === "test") {
        setCatResult(pretty(await api.testCatalog({ ids: [id] })));
      }
      await refreshCatalog();
    } catch (e) {
      if (auth(e)) return;
      setCatResult(String(e.message || e));
    }
  }

  async function addNode() {
    try {
      const tags = (addTags || "")
        .split(/[,;]/)
        .map((s) => s.trim())
        .filter(Boolean);
      const data = await api.addCatalogNode({
        url: addUrl.trim(),
        label: addLabel.trim(),
        tags,
        tier: Number(addTier || 0),
        enabled: true,
      });
      setCatResult(pretty(data));
      if (data.ok) {
        setAddUrl("");
        setAddLabel("");
      }
      await refreshCatalog();
    } catch (e) {
      if (auth(e)) return;
      setCatResult(String(e.message || e));
    }
  }

  async function testAllCatalog() {
    setCatResult("testing…");
    try {
      const data = await api.testCatalog({ limit: 50 });
      setCatResult(pretty(data));
      await refreshCatalog();
    } catch (e) {
      if (auth(e)) return;
      setCatResult(String(e.message || e));
    }
  }

  const leaves = clash?.leaves || [];
  const poolN = leaves.filter((x) => x.in_register_pool).length;
  const okN = leaves.filter((x) => x.health === "ok").length;
  const catPages = cat?.pages || 1;
  const catCur = cat?.page || catPage;

  return (
    <div class="resources-tab nodes-tab">
      <div class="card">
        <Tabs
          items={[
            { id: "clash", label: "内置 Clash / mihomo" },
            { id: "catalog", label: "项目 catalog" },
          ]}
          value={sub}
          onChange={setSub}
        />
      </div>

      {sub === "clash" ? (
        <>
          <div class="card actions-bar">
            <Button
              variant="ghost"
              busy={busy === "clash"}
              onClick={refreshClash}
            >
              刷新 Clash
            </Button>
            <Button
              variant="ghost"
              busy={busy === "test"}
              onClick={() => clashTest(40, true)}
            >
              测活注册池
            </Button>
            <Button
              variant="ghost"
              busy={busy === "test"}
              onClick={() => clashTest(40, false)}
            >
              测活（前 40）
            </Button>
            <label class="inline">
              筛选{" "}
              <Select
                value={clashFilter}
                options={CLASH_FILTER}
                onChange={setClashFilter}
              />
            </label>
            <label class="inline">
              搜索{" "}
              <input
                value={clashQ}
                placeholder="名称关键字"
                onInput={(e) => setClashQ(e.currentTarget.value)}
              />
            </label>
            <span class="hint">
              {clash && clash.ok
                ? `api=${clash.api} · leaves=${clash.leaf_count} · 注册池 ${poolN} · 可用 ${okN} · 显示 ${shownLeaves.length} · groups=${clash.group_count} · secret=${clash.secret_configured ? "yes" : "no"}`
                : clash
                  ? `Clash 不可用: ${clash.error || "unknown"} (api=${clash.api || ""})`
                  : "—"}
            </span>
          </div>

          <div class="card">
            <h2>订阅导入</h2>
            <div class="actions-bar wrap">
              <input
                class="grow"
                value={importUrl}
                placeholder="https://… subscription URL"
                onInput={(e) => setImportUrl(e.currentTarget.value)}
              />
              <label class="check">
                <input
                  type="checkbox"
                  checked={importDry}
                  onChange={(e) => setImportDry(e.currentTarget.checked)}
                />{" "}
                dry-run
              </label>
              <Button
                variant="ghost"
                busy={busy === "import"}
                onClick={doImportUrl}
              >
                导入 URL
              </Button>
            </div>
          </div>

          <div class="card">
            <h2>策略组</h2>
            <div class="chip-row">
              {(clash?.groups || []).map((g, i) => (
                <Chip
                  key={i}
                  class={g.register_relevant ? "hot" : ""}
                  title={`${g.type || ""} now=${g.now || ""}`}
                >
                  {g.name} · {g.count}
                  {g.now ? ` → ${g.now}` : ""}
                </Chip>
              ))}
            </div>
          </div>

          <div class="card table-wrap">
            <table class="data">
              <thead>
                <tr>
                  <th>健康</th>
                  <th>名称</th>
                  <th>类型</th>
                  <th>延迟</th>
                  <th>优先级分</th>
                  <th>注册池</th>
                  <th>所属组</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {!shownLeaves.length ? (
                  <tr>
                    <td colspan="8" class="hint">
                      当前筛选下无节点
                    </td>
                  </tr>
                ) : (
                  shownLeaves.map((n) => {
                    const hb = healthBadge(n.health || "unknown");
                    const groups = (n.groups || []).slice(0, 4).join(", ");
                    return (
                      <tr key={n.name}>
                        <td>
                          <span class={`badge ${hb.cls === "danger" ? "fail" : hb.cls === "ok" ? "ok" : "unknown"}`}>
                            {hb.label}
                          </span>
                        </td>
                        <td>{n.name}</td>
                        <td>{n.type || ""}</td>
                        <td>
                          {n.last_delay_ms != null ? `${n.last_delay_ms}ms` : "—"}
                        </td>
                        <td>
                          {n.priority_score != null ? n.priority_score : "—"}
                        </td>
                        <td>
                          {n.in_register_pool ? (
                            <span class="badge pool">注册池</span>
                          ) : (
                            "—"
                          )}
                        </td>
                        <td class="hint">{groups}</td>
                        <td class="ops">
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => clashTestOne(n.name)}
                          >
                            测活
                          </Button>
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
          {clashResult ? <pre class="log compact">{clashResult}</pre> : null}
        </>
      ) : (
        <>
          <div class="card grid">
            <label>
              proxy URL{" "}
              <input
                value={addUrl}
                placeholder="http://user:pass@host:port"
                onInput={(e) => setAddUrl(e.currentTarget.value)}
              />
            </label>
            <label>
              label{" "}
              <input
                value={addLabel}
                onInput={(e) => setAddLabel(e.currentTarget.value)}
              />
            </label>
            <label>
              tags{" "}
              <input
                value={addTags}
                placeholder="residential,us"
                onInput={(e) => setAddTags(e.currentTarget.value)}
              />
            </label>
            <label>
              tier{" "}
              <select
                value={addTier}
                onChange={(e) => setAddTier(e.currentTarget.value)}
              >
                <option value="0">0 datacenter</option>
                <option value="1">1 residential</option>
              </select>
            </label>
            <div class="actions">
              <Button variant="ghost" onClick={addNode}>
                添加节点
              </Button>
              <Button variant="ghost" onClick={testAllCatalog}>
                测活（enabled≤50）
              </Button>
              <Button
                variant="ghost"
                busy={busy === "catalog"}
                onClick={() => refreshCatalog()}
              >
                刷新
              </Button>
            </div>
          </div>

          <div class="card filter-bar">
            <label class="inline">
              搜索{" "}
              <input
                value={catQ}
                onInput={(e) => setCatQ(e.currentTarget.value)}
              />
            </label>
            <label class="inline">
              健康{" "}
              <Select
                value={catHealth}
                options={HEALTH_OPTS}
                onChange={(v) => {
                  setCatHealth(v);
                  refreshCatalog({ page: 1, health: v });
                }}
              />
            </label>
            <label class="inline">
              tier{" "}
              <Select
                value={catTier}
                options={TIER_OPTS}
                onChange={(v) => {
                  setCatTier(v);
                  refreshCatalog({ page: 1, tier: v });
                }}
              />
            </label>
            <label class="inline">
              每页{" "}
              <Select
                value={catPageSize}
                options={PAGE_SIZES}
                onChange={(v) => {
                  setCatPageSize(v);
                  refreshCatalog({ page: 1, pageSize: v });
                }}
              />
            </label>
            <Button
              variant="ghost"
              size="sm"
              disabled={catCur <= 1}
              onClick={() => refreshCatalog({ page: Math.max(1, catCur - 1) })}
            >
              上一页
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={catCur >= catPages}
              onClick={() => refreshCatalog({ page: catCur + 1 })}
            >
              下一页
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => refreshCatalog({ page: 1, q: catQ })}
            >
              应用筛选
            </Button>
            <span class="hint">
              page {catCur}/{catPages} · showing {(cat?.nodes || []).length}
            </span>
          </div>

          <div class={`card ${cat && cat.healthy > 0 ? "ok" : "muted"}`}>
            {cat
              ? `path=${cat.path} · total=${cat.total} enabled=${cat.enabled} healthy=${cat.healthy}` +
                (cat.fail != null ? ` fail=${cat.fail}` : "") +
                (cat.unknown != null ? ` unknown=${cat.unknown}` : "") +
                ` · 筛选 ${cat.filtered}/${cat.total} · 第 ${cat.page}/${cat.pages || 1} 页`
              : "—"}
          </div>

          <div class="card table-wrap">
            <table class="data">
              <thead>
                <tr>
                  <th>状态</th>
                  <th>label</th>
                  <th>tier</th>
                  <th>延迟</th>
                  <th>IP</th>
                  <th>优先级</th>
                  <th>失败</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {!(cat?.nodes || []).length ? (
                  <tr>
                    <td colspan="8" class="hint">
                      无匹配节点
                    </td>
                  </tr>
                ) : (
                  (cat.nodes || []).map((n) => {
                    const health =
                      n.health ||
                      (n.last_ok === true
                        ? "ok"
                        : n.last_ok === false
                          ? "fail"
                          : "unknown");
                    const hb = healthBadge(health);
                    return (
                      <tr key={n.id}>
                        <td>
                          <span
                            class={`badge ${hb.cls === "danger" ? "fail" : hb.cls === "ok" ? "ok" : "unknown"}`}
                          >
                            {hb.label}
                          </span>
                          {n.cooling
                            ? ` · cool:${n.cooldown_reason || ""}`
                            : ""}
                          {n.enabled === false ? (
                            <span class="badge fail">禁用</span>
                          ) : null}
                        </td>
                        <td>
                          <div>{n.label || n.id}</div>
                          <div class="hint">{n.id}</div>
                        </td>
                        <td>{n.tier === 1 ? "住宅" : "机房"}</td>
                        <td>{n.last_ms != null ? `${n.last_ms}ms` : "—"}</td>
                        <td>{n.last_ip || "—"}</td>
                        <td>
                          {n.priority_score != null
                            ? n.priority_score
                            : n.quality_score != null
                              ? n.quality_score
                              : "—"}
                        </td>
                        <td>
                          {n.fail_count || 0}
                          {n.last_error ? (
                            <div class="hint">{n.last_error}</div>
                          ) : null}
                        </td>
                        <td class="ops">
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => onCatalogAction("test", n.id)}
                          >
                            测活
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() =>
                              onCatalogAction("toggle", n.id, n.enabled !== false)
                            }
                          >
                            {n.enabled === false ? "启用" : "禁用"}
                          </Button>
                          <Button
                            variant="danger"
                            size="sm"
                            onClick={() => onCatalogAction("del", n.id)}
                          >
                            删除
                          </Button>
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
          {catResult ? <pre class="log compact">{catResult}</pre> : null}
        </>
      )}
    </div>
  );
}
