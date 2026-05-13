/**
 * Formatting helpers shared between SSR pages and client islands.
 * Mirrors the inline `fmtMoney` / `escapeHtml` helpers from the legacy
 * site/_*_template.html files.
 */

export function fmtMoney(n: unknown): string {
  if (n === null || n === undefined) return "—";
  const num = Number(n);
  if (!Number.isFinite(num)) return "—";
  const maxFractionDigits = num < 100 ? 2 : 0;
  return "$" + num.toLocaleString("en-US", { maximumFractionDigits: maxFractionDigits });
}

const HTML_ESCAPES: Record<string, string> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

export function escapeHtml(s: unknown): string {
  return String(s ?? "").replace(/[&<>"']/g, (c) => HTML_ESCAPES[c]!);
}

export function slugify(s: string): string {
  return s
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
}

export function titleCase(s: string): string {
  if (!s) return s;
  return s.charAt(0).toUpperCase() + s.slice(1).toLowerCase();
}

export function compactNumber(n: number): string {
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString("en-US");
}
