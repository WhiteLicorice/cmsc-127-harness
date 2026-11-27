import { diffSnapshot } from "./panels/changes.js";
import { registerOfflineWorker, watchOfflineSave } from "./offline.js";
import { readPollSettings } from "./polling.js";
import { escapeHtml } from "./panels/html.js";
import { CONNECT_IN_STATUS_PROMPT, DEFAULT_SERVER_ADDRESS, isHostedPage, loopbackInit, serverAddress, apiUrl, TOO_OLD_MESSAGE, unreachableMessage, versionSupported, } from "./hosted.js";
import { panelRegistry } from "./panelRegistry.js";
import { selectionMatches } from "./panelRequest.js";
import { renderStatus } from "./panels/status.js";
function requireElement(selector) {
    const element = document.querySelector(selector);
    if (element === null) {
        throw new Error("The harness page is missing a required element.");
    }
    return element;
}
const panelButtons = requireElement("#panels");
const panelContent = requireElement("#panel-content");
const schemaSelect = requireElement("#schema-select");
const connectionMessage = requireElement("#connection-message");
const pollSummary = requireElement("#poll-summary");
// The hosted page talks to a server on this computer, so it waits for the
// Connect click on its Status panel. The local page was served by its server
// and needs no click.
const HOSTED = isHostedPage();
const server = serverAddress();
let connected = !HOSTED;
let connecting = false;
let tooOld = false;
// True only after a complete snapshot is in the browser cache. The Status panel
// shows the offline line from this value alone.
let saved = false;
let activePanel = HOSTED ? "status" : "tables";
let selectedSchema = "public";
let currentTables = null;
let previousTables = null;
let feed = [];
let panelData = null;
let statusData = null;
let pollInterval = 1000;
let pollingTables = false;
let pollingPanel = false;
let renderedMarkup = "";
const requestMessages = { tables: "", panel: "" };
function showMessage(message, source = "panel") {
    requestMessages[source] = message;
    const visibleMessage = requestMessages.panel || requestMessages.tables;
    connectionMessage.textContent = visibleMessage;
    connectionMessage.hidden = visibleMessage.length === 0;
}
const SERVER_UNREACHABLE = "The page cannot reach its server. Run bash start.sh from the b1 bundle folder.";
function describeRequestError(error) {
    if (error instanceof TypeError) {
        return HOSTED ? unreachableMessage(server ?? DEFAULT_SERVER_ADDRESS) : SERVER_UNREACHABLE;
    }
    return error instanceof Error ? error.message : SERVER_UNREACHABLE;
}
// A body that is not JSON means some other program answered on the port. The
// page reports that as a server it cannot reach, so a reader never meets a
// parser message.
async function readJson(response) {
    try {
        return await response.json();
    }
    catch {
        throw new TypeError("The response is not JSON.");
    }
}
async function requestJson(url) {
    const response = await fetch(apiUrl(url, server), loopbackInit(server));
    const data = await readJson(response);
    if (!response.ok) {
        const body = data;
        throw new Error(body.message ?? "The harness request failed.");
    }
    return data;
}
// The version check reads the body of a failed status request too, because a
// server with its database down still states its version.
async function fetchStatus() {
    const response = await fetch(apiUrl("/api/status", server), loopbackInit(server));
    return await readJson(response);
}
function updateSchemas(response) {
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
function configureMermaid() {
    if (!window.mermaid) {
        return;
    }
    const styles = getComputedStyle(document.documentElement);
    const token = (name) => styles.getPropertyValue(name).trim();
    const dark = styles.colorScheme === "dark";
    window.mermaid.initialize({
        startOnLoad: false,
        theme: "base",
        themeVariables: {
            darkMode: dark,
            background: token("--surface"),
            primaryColor: token("--surface-2"),
            primaryTextColor: token("--text"),
            primaryBorderColor: token("--border"),
            secondaryColor: token("--surface"),
            tertiaryColor: token("--code-bg"),
            mainBkg: token("--surface-2"),
            nodeBorder: token("--border"),
            // The ER renderer reads rowOdd and rowEven for the attribute rows. It
            // ignores attributeBackgroundColorOdd and attributeBackgroundColorEven.
            rowOdd: token("--surface"),
            rowEven: token("--surface-2"),
            lineColor: token("--border"),
            textColor: token("--text"),
            titleColor: token("--text"),
            edgeLabelBackground: token("--surface"),
            fontFamily: styles.fontFamily,
        },
    });
}
async function drawMermaid() {
    if (!window.mermaid) {
        return;
    }
    configureMermaid();
    try {
        await window.mermaid.run({ querySelector: ".mermaid" });
    }
    catch {
        showMessage("The schema diagram could not be drawn. Check the ERD block in the README.md of the folder you started the page in.");
    }
}
function statusView() {
    const view = { hosted: HOSTED, connected, saved };
    if (connecting) {
        view.connecting = true;
    }
    if (tooOld) {
        view.tooOld = true;
    }
    return view;
}
function renderCurrentPanel() {
    const panel = panelRegistry.find((candidate) => candidate.id === activePanel);
    if (!panel) {
        showMessage("The selected panel is not available.");
        return;
    }
    let markup;
    if (HOSTED && tooOld) {
        // One plain line in the Status panel. The other panels draw nothing, so
        // the page never shows seven empty panels against an old server.
        markup = activePanel === "status"
            ? `<p class="error-message">${escapeHtml(TOO_OLD_MESSAGE)}</p>`
            : "";
    }
    else if (HOSTED && !connected) {
        markup = activePanel === "status"
            ? renderStatus({ server: "unknown", databaseStatus: "unknown" }, statusView())
            : `<p>${escapeHtml(CONNECT_IN_STATUS_PROMPT)}</p>`;
    }
    else if (activePanel === "tables" && currentTables) {
        markup = panel.render(currentTables, previousTables);
    }
    else if (activePanel === "changes") {
        markup = panel.render(feed);
    }
    else if (activePanel === "status" && statusData !== null) {
        markup = panel.render(statusData, statusView());
    }
    else if (panelData !== null) {
        markup = panel.render(panelData);
    }
    else {
        markup = "<p>Loading…</p>";
    }
    if (markup === renderedMarkup && panelContent.dataset["panel"] === activePanel) {
        return;
    }
    const samePanel = panelContent.dataset["panel"] === activePanel;
    const scrollState = samePanel
        ? [...panelContent.querySelectorAll(".table-scroll")].map((node) => ({
            left: node.scrollLeft, top: node.scrollTop, focused: node === document.activeElement,
        }))
        : [];
    renderedMarkup = markup;
    panelContent.dataset["panel"] = activePanel;
    panelContent.innerHTML = markup;
    panelContent.querySelectorAll(".table-scroll").forEach((node, index) => {
        const saved = scrollState[index];
        if (saved) {
            node.scrollLeft = saved.left;
            node.scrollTop = saved.top;
            if (saved.focused)
                node.focus({ preventScroll: true });
        }
    });
    void drawMermaid();
}
function buildPanelButtons() {
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
async function pollTables() {
    if (pollingTables) {
        return;
    }
    pollingTables = true;
    const requestedSchema = selectedSchema;
    try {
        const response = await requestJson(`/api/tables?schema=${encodeURIComponent(requestedSchema)}`);
        if (requestedSchema !== selectedSchema) {
            return;
        }
        updateSchemas(response);
        previousTables = currentTables;
        currentTables = response;
        const now = new Date();
        if (previousTables) {
            const diff = diffSnapshot({ schema: previousTables.schema, tables: previousTables.tables }, { schema: response.schema, tables: response.tables }, now.toISOString());
            feed = [...feed, ...diff.events].slice(-100);
        }
        pollSummary.textContent = `Last table poll: ${now.toLocaleTimeString()}.`;
        showMessage(response.message ?? "", "tables");
        if (activePanel === "tables" || activePanel === "changes") {
            renderCurrentPanel();
        }
    }
    catch (error) {
        if (requestedSchema === selectedSchema) {
            showMessage(describeRequestError(error), "tables");
        }
    }
    finally {
        pollingTables = false;
    }
}
async function refreshActivePanel() {
    if (!connected) {
        return;
    }
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
            statusData = panelData;
        }
        showMessage("");
        renderCurrentPanel();
    }
    catch (error) {
        if (selectionMatches(requestedPanel, activePanel, requestedSchema, selectedSchema)) {
            panelData = null;
            showMessage(describeRequestError(error));
        }
    }
    finally {
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
    if (!connected) {
        return;
    }
    void pollTables();
    void refreshActivePanel();
});
panelContent.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement) || target.dataset["action"] !== "connect") {
        return;
    }
    if (!HOSTED || connected) {
        return;
    }
    connectHosted();
});
document.addEventListener("cmsc127:theme", () => {
    renderedMarkup = "";
    renderCurrentPanel();
});
function startPolling() {
    void readPollSettings(() => requestJson("/api/changes")).then((settings) => {
        pollInterval = settings.intervalMs;
        if (settings.error !== null) {
            showMessage(describeRequestError(settings.error));
        }
        void pollTables();
        void refreshActivePanel();
        window.setInterval(() => void pollTables(), pollInterval);
        window.setInterval(() => void refreshActivePanel(), pollInterval);
    });
}
// The hosted page checks the server release before it polls, so an old server
// draws one plain line instead of seven empty panels.
function connectHosted() {
    if (connecting) {
        return;
    }
    connecting = true;
    renderCurrentPanel();
    void fetchStatus()
        .then((data) => {
        if (versionSupported(data)) {
            connected = true;
            statusData = null;
            panelData = null;
            startPolling();
        }
        else {
            tooOld = true;
        }
    })
        .catch((error) => showMessage(describeRequestError(error)))
        .finally(() => {
        connecting = false;
        renderCurrentPanel();
    });
}
registerOfflineWorker();
watchOfflineSave(() => {
    saved = true;
    renderedMarkup = "";
    renderCurrentPanel();
});
buildPanelButtons();
if (HOSTED) {
    renderCurrentPanel();
}
else {
    startPolling();
}
