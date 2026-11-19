import { codeBlock, escapeHtml } from "./html.js";
// The server sends this message when .cmsc127/last-run.json is absent.
const MISSING_RESULT = "No check results yet. The lab runner writes .cmsc127/last-run.json when it runs.";
export function describeChecks(result) {
    return result.results.map((entry) => ({
        id: entry.id,
        kind: entry.kind ?? "check",
        stage: entry.stage ?? null,
        status: entry.status,
        ...(entry.status === "fail"
            ? { expected: entry.expected ?? null, actual: entry.actual ?? null }
            : {}),
        differences: entry.differences ?? [],
        warnings: entry.warnings ?? [],
    }));
}
// The status word carries the meaning. The class adds colour and weight.
function resultClass(status) {
    if (status === "pass") {
        return "result result-pass";
    }
    if (status === "fail") {
        return "result result-fail";
    }
    return "result result-skip";
}
function counted(value, singular, plural) {
    const total = value ?? 0;
    return `${total} ${total === 1 ? singular : plural}`;
}
export function renderChecks(result) {
    const entries = describeChecks(result);
    if (entries.length === 0) {
        if (result.message === undefined || result.message === MISSING_RESULT) {
            return `<p>${escapeHtml(MISSING_RESULT)}</p>`;
        }
        return `<p>${escapeHtml(result.message)}</p>`;
    }
    const rows = entries.map((entry) => {
        const expected = entry.status === "fail" ? codeBlock(entry.expected) : "";
        const differences = entry.status === "fail" && entry.differences.length > 0
            ? `<pre class="difference-note">${escapeHtml(entry.differences.join("\n"))}</pre>`
            : "";
        const actual = entry.status === "fail"
            ? `${differences}${codeBlock(entry.actual)}`
            : entry.warnings.length > 0
                ? `<pre class="warning-note">${escapeHtml(entry.warnings.join("\n"))}</pre>`
                : "";
        return `<tr><td>${escapeHtml(entry.id)}</td><td>${escapeHtml(entry.kind)}</td><td>${entry.stage ?? ""}</td><td class="${resultClass(entry.status)}">${escapeHtml(entry.status)}</td><td>${expected}</td><td>${actual}</td></tr>`;
    }).join("");
    const summary = [
        counted(result.passed, "passed", "passed"),
        counted(result.failed, "failed", "failed"),
        counted(result.skipped, "skipped", "skipped"),
        counted(result.warnings, "warning", "warnings"),
    ].join(", ");
    return `<p>${escapeHtml(summary)}</p><div class="table-scroll" tabindex="0"><table><thead><tr><th>Case</th><th>Kind</th><th>Stage</th><th>Result</th><th>Expected</th><th>Actual</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}
