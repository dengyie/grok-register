export function Chip({ kind = "default", children, class: cls = "", title, ...rest }) {
  return (
    <span class={`chip chip-${kind} ${cls}`} title={title} {...rest}>
      {children}
    </span>
  );
}
