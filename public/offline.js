import { isHostedPage } from "./hosted.js";
// The hosted page keeps a copy of itself in the browser, so a bootcamp session
// needs no network after the first load. The local page has its own server and
// registers no worker.
export const NEW_BUILD_NOTICE = "A new version of this page is ready. It loads the next time you open the page.";
const WORKER_PATH = "service-worker.js";
function showNotice() {
    if (document.querySelector(".offline-notice") !== null) {
        return;
    }
    const notice = document.createElement("p");
    notice.className = "offline-notice poll-summary";
    notice.setAttribute("role", "status");
    notice.textContent = NEW_BUILD_NOTICE;
    document.body.append(notice);
}
export function registerOfflineWorker() {
    if (!isHostedPage() || !("serviceWorker" in navigator)) {
        return;
    }
    const scope = new URL("./", document.baseURI);
    // A page that a worker already controls meets a new build at the next load.
    // The first install adds a controller too, and that is not a new build.
    const hadController = navigator.serviceWorker.controller !== null;
    navigator.serviceWorker.addEventListener("controllerchange", () => {
        if (hadController) {
            showNotice();
        }
    });
    navigator.serviceWorker
        .register(new URL(WORKER_PATH, scope).href, { scope: scope.href })
        .catch(() => undefined);
}
