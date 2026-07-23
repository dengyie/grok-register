import { useEffect, useState } from "preact/hooks";
import { session } from "./store/session.js";
import * as api from "./api/client.js";
import { ToastHost } from "./ui/ToastHost.jsx";
import { OpsFeedbackBar } from "./ui/OpsFeedbackBar.jsx";
import { LoginGate } from "./pages/LoginGate.jsx";
import { RegisterPage } from "./pages/Register/RegisterPage.jsx";
import { LogsPage } from "./pages/RunLogs/LogsPage.jsx";
import { AccountsPage } from "./pages/Accounts/AccountsPage.jsx";
import { ResourcesPage } from "./pages/Resources/ResourcesPage.jsx";
import { SettingsPage } from "./pages/Settings/SettingsPage.jsx";

const NAV = [
  { id: "register", label: "总览 / 注册", hash: "#/register" },
  { id: "logs", label: "运行日志", hash: "#/logs" },
  { id: "accounts", label: "账号池", hash: "#/accounts" },
  { id: "resources", label: "资源", hash: "#/resources" },
  { id: "settings", label: "设置", hash: "#/settings" },
];

function pageFromHash() {
  const h = (location.hash || "#/register").replace(/^#\/?/, "");
  const id = h.split("?")[0] || "register";
  return NAV.some((n) => n.id === id) ? id : "register";
}

export function App() {
  const [page, setPage] = useState(pageFromHash);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  useEffect(() => {
    const onHash = () => setPage(pageFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    (async () => {
      try {
        const m = await api.me();
        session.value = { ...session.value, ...m, checked: true };
      } catch {
        session.value = { ...session.value, authenticated: false, checked: true };
      }
    })();
  }, []);

  const authed = session.value.authenticated || !session.value.auth_required;

  if (!session.value.checked) return <div class="login-gate">加载中…</div>;
  if (!authed) return <LoginGate />;

  const Page = {
    register: RegisterPage,
    logs: LogsPage,
    accounts: AccountsPage,
    resources: ResourcesPage,
    settings: SettingsPage,
  }[page];

  return (
    <>
      <ToastHost />
      <div class={`app-shell ${sidebarOpen ? "nav-open" : ""}`}>
        <button
          type="button"
          class="nav-toggle btn btn-ghost btn-sm"
          onClick={() => setSidebarOpen((v) => !v)}
        >
          菜单
        </button>
        {sidebarOpen ? (
          <button
            type="button"
            class="nav-backdrop"
            aria-label="关闭菜单"
            onClick={() => setSidebarOpen(false)}
          />
        ) : null}
        <aside class="sidebar">
          <div class="side-brand">
            <div class="logo-dot" aria-hidden="true" />
            <div>
              <div class="side-title">AI 注册机</div>
              <a
                class="side-link"
                href="https://github.com/dengyie/ai-register-machine"
                target="_blank"
                rel="noopener noreferrer"
              >
                grok / mimo / chatgpt
              </a>
            </div>
          </div>
          <nav class="side-nav">
            {NAV.map((n) => (
              <a
                key={n.id}
                href={n.hash}
                class={`nav-item ${page === n.id ? "active" : ""}`}
                onClick={() => setSidebarOpen(false)}
              >
                <span class="nav-dot" />
                {n.label}
              </a>
            ))}
          </nav>
          <div class="side-foot">
            <div class="hint">{session.value.username || "operator"}</div>
            <button
              type="button"
              class="btn btn-danger btn-sm"
              onClick={async () => {
                try {
                  await api.logout();
                } finally {
                  api.clearToken();
                  session.value = {
                    ...session.value,
                    authenticated: false,
                    username: null,
                  };
                }
              }}
            >
              Logout
            </button>
          </div>
        </aside>
        <div class="main-wrap">
          <OpsFeedbackBar />
          <Page />
        </div>
      </div>
    </>
  );
}
