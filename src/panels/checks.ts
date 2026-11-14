import { codeBlock, escapeHtml } from "./html.js";

export type CheckResult = {
  id: string;
  kind?: string;
  stage?: number;
  status: string;
  expected?: unknown;
  actual?: unknown;
};

export type RunnerResult = {
  results: CheckResult[];
  passed?: number;
  failed?: number;
  skipped?: number;
  stage?: number;
  message?: string;
};

// The server sends this message when .cmsc127/last-run.json is absent.
const MISSING_RESULT = "No check results found. Run check.sh from the project root.";

export type CheckView = {
  id: string;
  kind: string;
  stage: number | null;
  status: string;
  expected?: unknown;
  actual?: unknown;
};

export function describeChecks(result: RunnerResult): CheckView[] {
  return result.results.map((entry) => ({
    id: entry.id,
    kind: entry.kind ?? "check",
    stage: entry.stage ?? null,
    status: entry.status,
    ...(entry.status === "fail"
      ? { expected: entry.expected ?? null, actual: entry.actual ?? null }
      : {}),
  }));
}

export function renderChecks(result: RunnerResult): string {
  const entries = describeChecks(result);
  if (entries.length === 0) {
    if (result.message === undefined || result.message === MISSING_RESULT) {
      return "<p>No check result exists. Run check.sh from the project root.</p>";
    }
    return `<p>${escapeHtml(result.message)} This page cannot read the file. Run check.sh from the project root to write a new result.</p>`;
  }
  const rows = entries.map((entry) => {
    const expected = entry.status === "fail" ? codeBlock(entry.expected) : "";
    const actual = entry.status === "fail" ? codeBlock(entry.actual) : "";
    return `<tr><td>${escapeHtml(entry.id)}</td><td>${escapeHtml(entry.kind)}</td><td>${entry.stage ?? ""}</td><td>${escapeHtml(entry.status)}</td><td>${expected}</td><td>${actual}</td></tr>`;
  }).join("");
  const summary = `${result.passed ?? 0} passed, ${result.failed ?? 0} failed, ${result.skipped ?? 0} skipped`;
  return `<p>${escapeHtml(summary)}</p><div class="table-scroll" tabindex="0"><table><thead><tr><th>Case</th><th>Kind</th><th>Stage</th><th>Result</th><th>Expected</th><th>Actual</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}
