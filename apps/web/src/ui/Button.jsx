export function Button({
  variant = "ghost",
  size = "md",
  busy = false,
  type = "button",
  class: cls = "",
  children,
  disabled,
  ...rest
}) {
  return (
    <button
      type={type}
      class={`btn btn-${variant} btn-${size} ${busy ? "busy" : ""} ${cls}`}
      disabled={busy || !!disabled}
      aria-busy={busy || undefined}
      {...rest}
    >
      {children}
    </button>
  );
}
