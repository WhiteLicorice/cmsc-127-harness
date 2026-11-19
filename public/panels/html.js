const HTML_ESCAPES = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
};
export function escapeHtml(value) {
    return value.replace(/[&<>"']/g, (character) => HTML_ESCAPES[character] ?? character);
}
export function displayValue(value) {
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
export function codeBlock(value) {
    return `<pre>${escapeHtml(JSON.stringify(value, null, 2) ?? "null")}</pre>`;
}
