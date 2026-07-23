export function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function dash(v) {
  return v == null || v === "" ? "—" : String(v);
}

export function fmtNum(v) {
  return v == null || v === "" ? "—" : String(v);
}

export function pct(a, b) {
  if (b == null || a == null || Number(b) <= 0) return null;
  return Math.max(0, Math.min(100, (Number(a) / Number(b)) * 100));
}

export function formatApiError(e) {
  if (!e) return "unknown error";
  if (typeof e === "string") return e;
  if (e.message) return e.status ? `${e.status}: ${e.message}` : e.message;
  return String(e);
}

export function healthBadge(h) {
  if (h === "ok" || h === true) return { label: "ok", cls: "ok" };
  if (h === "fail" || h === false) return { label: "fail", cls: "danger" };
  return { label: "?", cls: "muted" };
}
