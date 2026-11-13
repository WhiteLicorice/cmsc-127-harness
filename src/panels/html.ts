const HTML_ESCAPES: Record<string, string> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

export function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (character) => HTML_ESCAPES[character] ?? character);
}

export function displayValue(value: unknown): string {
  if (value === null) {
    return "NULL";
  }
  if (typeof value === "string") {
    return value;
  }
  if (typeof value === "object") {
    return JSON.stringify(value) ?? "";
  }
  return String(value);
}

export function codeBlock(value: unknown): string {
  return `<pre>${escapeHtml(JSON.stringify(value, null, 2) ?? "null")}</pre>`;
}
