// Resources page: Nodes | Mail | Import (IA merge of legacy three pages)
import { useEffect, useState } from "preact/hooks";
import { Tabs } from "../../ui/index.js";
import { NodesTab } from "./NodesTab.jsx";
import { MailTab } from "./MailTab.jsx";
import { ImportTab } from "./ImportTab.jsx";
import "../../styles/resources.css";
import "../../styles/accounts.css"; // table.data / badge / filter-bar shared

const TABS = [
  { id: "nodes", label: "节点" },
  { id: "mail", label: "邮箱" },
  { id: "import", label: "导入" },
];

function tabFromHash() {
  try {
    const h = location.hash || "";
    const m = h.match(/[?&]tab=([a-z]+)/i);
    const t = m && m[1];
    if (t && TABS.some((x) => x.id === t)) return t;
  } catch {
    /* ignore */
  }
  return "nodes";
}

export function ResourcesPage() {
  const [tab, setTab] = useState(tabFromHash);

  useEffect(() => {
    const base = "#/resources";
    const next = tab === "nodes" ? base : `${base}?tab=${tab}`;
    if (location.hash !== next && (location.hash.startsWith("#/resources") || !location.hash)) {
      location.hash = next;
    }
  }, [tab]);

  return (
    <section class="page page-resources">
      <header class="page-head">
        <div>
          <h1>资源</h1>
          <p class="hint">
            节点池（Clash + catalog）、邮箱接码与导入。Hotmail 凭证仅在此「邮箱」tab。
          </p>
        </div>
      </header>

      <div class="card">
        <Tabs items={TABS} value={tab} onChange={setTab} />
      </div>

      {tab === "nodes" ? <NodesTab /> : null}
      {tab === "mail" ? <MailTab /> : null}
      {tab === "import" ? <ImportTab /> : null}
    </section>
  );
}
