export function Chip({ kind = "default", children, class: cls = "", title, ...rest }) {
  // Accept "mini" as alias for CSS class chip-mini
  const extra = String(cls || "")
    .split(/\s+/)
    .filter(Boolean)
    .map((c) => (c === "mini" ? "chip-mini" : c))
    .join(" ");
  return (
    <span class={`chip chip-${kind} ${extra}`} title={title} {...rest}>
      {children}
    </span>
  );
}
