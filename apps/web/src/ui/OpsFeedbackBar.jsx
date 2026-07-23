// Shell-level sticky banner + ops log (legacy #ops-feedback / #ops-log).
// Mounted once in App.main-wrap so every page sees top actions.
import { useState } from "preact/hooks";
import {
  stickyBanner,
  opsLog,
  clearStickyBanner,
} from "../store/feedback.js";

export function OpsFeedbackBar() {
  const [logOpen, setLogOpen] = useState(false);
  const banner = stickyBanner.value;
  const logItems = opsLog.value;

  return (
    <>
      {banner ? (
        <div
          class={`ops-feedback ${banner.kind || "info"}`}
          role="status"
          onClick={clearStickyBanner}
          title="点击清除横幅"
        >
          {banner.message}
        </div>
      ) : null}
      <details
        class="ops-log-wrap"
        open={logOpen}
        onToggle={(e) => setLogOpen(e.currentTarget.open)}
      >
        <summary>
          操作日志 <span class="hint">{logItems.length}</span>
        </summary>
        <ol class="ops-log" aria-live="polite">
          {logItems.map((it, i) => (
            <li key={i}>
              <span class="t">{it.t}</span>
              <span class={`k ${it.kind}`}>{it.kind}</span>
              <span class="m">{it.m}</span>
            </li>
          ))}
        </ol>
      </details>
    </>
  );
}
