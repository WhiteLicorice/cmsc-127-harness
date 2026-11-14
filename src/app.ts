import { buildFeed, type ChangeFeedEvent } from "./panels/changes.js";
import { panelRegistry, type PanelId } from "./panelRegistry.js";
import { selectionMatches } from "./panelRequest.js";
import { type TablesResponse } from "./panels/tables.js";
import { type StatusData } from "./panels/status.js";

declare global {
  interface Window {
    mermaid?: {
      run: (options?: { querySelector?: string }) => Promise<void>;
    };
  }
}

function requireElement<ElementType extends Element>(selector: string): ElementType {
  const element = document.querySelector<ElementType>(selector);
  if (element === null) {
    throw new Error("The harness page is missing a required element.");
  }
  return element;
}

const panelButtons = requireElement<HTMLElement>("#panels");
const panelContent = requireElement<HTMLElement>("#panel-content");
const schemaSelect = requireElement<HTMLSelectElement>("#schema-select");
const connectionMessage = requireElement<HTMLElement>("#connection-message");
const pollSummary = requireElement<HTMLElement>("#poll-summary");

let activePanel: PanelId = "tables";
let selectedSchema = "public";
let currentTables: TablesResponse | null = null;
let previousTables: TablesResponse | null = null;
let feed: ChangeFeedEvent[] = [];
let panelData: unknown = null;
let statusData: StatusData | null = null;
let pollInterval = 1000;
let pollingTables = false;
let pollingPanel = false;
let renderedMarkup = "";
const requestMessages = { tables: "", panel: "" };

function showMessage(message: string, source: "tables" | "panel" = "panel"): void {
  requestMessages[source] = message;
  const visibleMessage = requestMessages.panel || requestMessages.tables;
  connectionMessage.textContent = visibleMessage;
  connectionMessage.hidden = visibleMessage.length === 0;
}

const SERVER_UNREACHABLE = "The harness server cannot be reached. Run ./start.sh from the project directory in Git Bash.";

function describeRequestError(error: unknown): string {
  if (error instanceof TypeError) {
    return SERVER_UNREACHABLE;
  }
  return error instanceof Error ? error.message : SERVER_UNREACHABLE;
}

async function requestJson(url: string): Promise<unknown> {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  const data: unknown = await response.json();
  if (!response.ok) {
    const body = data as { message?: string };
    throw new Error(body.message ?? "The harness request failed.");
  }
  return data;
}

function updateSchemas(response: TablesResponse): void {
  if (response.schemas.length === 0 || response.schema === null) {
    schemaSelect.replaceChildren();
    schemaSelect.disabled = true;
    return;
  }
  schemaSelect.disabled = false;
  selectedSchema = response.schema;
  const options = response.schemas.map((schema) => {
    const option = document.createElement("option");
    option.value = schema;
    option.textContent = schema;
    option.selected = schema === selectedSchema;
    return option;
  });
  schemaSelect.replaceChildren(...options);
}

async function drawMermaid(): Promise<void> {
  if (!window.mermaid) {
    return;
  }
  try {
    await window.mermaid.run({ querySelector: ".mermaid" });
  } catch {
    showMessage("The schema diagram could not be drawn. Check the ERD text in README.md.");
  }
}

function renderCurrentPanel(): void {
  const panel = panelRegistry.find((candidate) => candidate.id === activePanel);
  if (!panel) {
    showMessage("The selected panel is not available.");
    return;
  }
  let markup: string;
  if (activePanel === "tables" && currentTables) {
    markup = panel.render(currentTables, previousTables);
  } else if (activePanel === "changes") {
    markup = panel.render(feed);
  } else if (activePanel === "status" && statusData) {
    markup = panel.render(statusData);
  } else if (panelData !== null) {
    markup = panel.render(panelData);
  } else {
    markup = "<p>Loading…</p>";
  }
  if (markup === renderedMarkup && panelContent.dataset["panel"] === activePanel) {
    return;
  }
  const samePanel = panelContent.dataset["panel"] === activePanel;
  const scrollState = samePanel
    ? [...panelContent.querySelectorAll<HTMLElement>(".table-scroll")].map((node) => ({
      left: node.scrollLeft, top: node.scrollTop, focused: node === document.activeElement,
    }))
    : [];
  renderedMarkup = markup;
  panelContent.dataset["panel"] = activePanel;
  panelContent.innerHTML = markup;
  panelContent.querySelectorAll<HTMLElement>(".table-scroll").forEach((node, index) => {
    const saved = scrollState[index];
    if (saved) {
      node.scrollLeft = saved.left;
      node.scrollTop = saved.top;
      if (saved.focused) node.focus({ preventScroll: true });
    }
  });
  void drawMermaid();
}

function buildPanelButtons(): void {
  const buttons = panelRegistry.map((panel) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = panel.title;
    button.dataset["panel"] = panel.id;
    button.setAttribute("aria-pressed", String(panel.id === activePanel));
    button.addEventListener("click", () => {
      activePanel = panel.id;
      panelData = null;
      showMessage("");
      panelButtons.querySelectorAll("button").forEach((item) => {
        item.setAttribute("aria-pressed", String(item.dataset["panel"] === activePanel));
      });
      renderCurrentPanel();
      void refreshActivePanel();
    });
    return button;
  });
  panelButtons.replaceChildren(...buttons);
}

async function pollTables(): Promise<void> {
  if (pollingTables) {
    return;
  }
  pollingTables = true;
  const requestedSchema = selectedSchema;
  try {
    const response = await requestJson(`/api/tables?schema=${encodeURIComponent(requestedSchema)}`) as TablesResponse;
    if (requestedSchema !== selectedSchema) {
      return;
    }
    updateSchemas(response);
    previousTables = currentTables;
    currentTables = response;
    const now = new Date().toISOString();
    if (previousTables) {
      feed = [...feed, ...buildFeed(previousTables.tables, currentTables.tables, now)].slice(-100);
    }
    pollSummary.textContent = `Last table poll: ${now}. Interval: ${pollInterval} ms.`;
    showMessage(response.message ?? "", "tables");
    if (activePanel === "tables" || activePanel === "changes") {
      renderCurrentPanel();
    }
  } catch (error) {
    if (requestedSchema === selectedSchema) {
      showMessage(describeRequestError(error), "tables");
    }
  } finally {
    pollingTables = false;
  }
}

async function refreshActivePanel(): Promise<void> {
  if (activePanel === "tables" || activePanel === "changes" || pollingPanel) {
    return;
  }
  const panel = panelRegistry.find((candidate) => candidate.id === activePanel);
  if (!panel) {
    return;
  }
  const requestedPanel = activePanel;
  const requestedSchema = selectedSchema;
  pollingPanel = true;
  try {
    const response = await requestJson(panel.endpoint(requestedSchema));
    if (!selectionMatches(requestedPanel, activePanel, requestedSchema, selectedSchema)) {
      return;
    }
    panelData = response;
    if (requestedPanel === "status") {
      statusData = panelData as StatusData;
    }
    showMessage("");
    renderCurrentPanel();
  } catch (error) {
    if (selectionMatches(requestedPanel, activePanel, requestedSchema, selectedSchema)) {
      panelData = null;
      showMessage(describeRequestError(error));
    }
  } finally {
    pollingPanel = false;
  }
}

schemaSelect.addEventListener("change", () => {
  selectedSchema = schemaSelect.value;
  previousTables = null;
  currentTables = null;
  feed = [];
  panelData = null;
  renderCurrentPanel();
  void pollTables();
  void refreshActivePanel();
});

buildPanelButtons();
void requestJson("/api/changes")
  .then((data) => {
    const settings = data as { pollIntervalMs?: number };
    pollInterval = settings.pollIntervalMs ?? 1000;
    void pollTables();
    void refreshActivePanel();
    window.setInterval(() => void pollTables(), pollInterval);
    window.setInterval(() => void refreshActivePanel(), pollInterval);
  })
  .catch(() => showMessage("The harness server is not running. Start it again from Git Bash."));
