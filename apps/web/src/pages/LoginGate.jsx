import { useState } from "preact/hooks";
import * as api from "../api/client.js";
import { session } from "../store/session.js";
import { Button } from "../ui/Button.jsx";

export function LoginGate() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function onSubmit(e) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await api.login(username.trim(), password);
      const m = await api.me();
      session.value = {
        ...session.value,
        ...m,
        authenticated: true,
        username: (m && m.username) || username.trim(),
        checked: true,
      };
    } catch (err) {
      setError((err && err.message) || "登录失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div class="login-gate">
      <form class="login-card" autocomplete="on" onSubmit={onSubmit}>
        <h1>登录 Control Plane</h1>
        <p class="hint">需要操作员账密。脚本仍可用 Bearer token。</p>
        <p class="hint brand-line">
          开源项目{" "}
          <a
            href="https://github.com/dengyie/ai-register-machine"
            target="_blank"
            rel="noopener noreferrer"
          >
            ai-register-machine
          </a>
        </p>
        <label>
          用户名{" "}
          <input
            name="username"
            autocomplete="username"
            placeholder="admin"
            required
            value={username}
            onInput={(e) => setUsername(e.currentTarget.value)}
          />
        </label>
        <label>
          密码{" "}
          <input
            name="password"
            type="password"
            autocomplete="current-password"
            placeholder="密码"
            required
            value={password}
            onInput={(e) => setPassword(e.currentTarget.value)}
          />
        </label>
        <div class="actions">
          <Button variant="primary" type="submit" busy={busy}>
            登录
          </Button>
        </div>
        <p class="error" role="alert">
          {error}
        </p>
      </form>
    </div>
  );
}
