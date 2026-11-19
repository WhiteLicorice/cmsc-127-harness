import { isHostedPage } from "./hosted.js";

// The hosted page keeps a copy of itself in the browser, so a bootcamp session
// needs no network after the first load. The local page has its own server and
// registers no worker.

export const NEW_BUILD_NOTICE =
  "A new version of this page is ready. It loads the next time you open the page.";

// The Status panel shows this line only after a complete snapshot is verified
// and stored. The worker writes the manifest last, so a manifest with every
// file beside it marks a finished copy.
export const OFFLINE_SAVED = "Saved for offline use.";

const WORKER_PATH = "service-worker.js";
const SNAPSHOT_PREFIX = "cmsc127-harness-";
const MANIFEST_PATH = "offline-manifest.json";

function showNotice(): void {
  if (document.querySelector(".offline-notice") !== null) {
    return;
  }
  const notice = document.createElement("p");
  notice.className = "offline-notice poll-summary";
  notice.setAttribute("role", "status");
  notice.textContent = NEW_BUILD_NOTICE;
  document.body.append(notice);
}

type Manifest = { files?: { path?: unknown }[] };

async function cacheIsComplete(name: string): Promise<boolean> {
  const cache = await caches.open(name);
  const record = await cache.match(new URL(MANIFEST_PATH, document.baseURI).href);
  if (!record) {
    return false;
  }
  let manifest: Manifest;
  try {
    manifest = await record.json() as Manifest;
  } catch {
    return false;
  }
  if (!Array.isArray(manifest.files) || manifest.files.length === 0) {
    return false;
  }
  for (const entry of manifest.files) {
    if (typeof entry?.path !== "string") {
      return false;
    }
    if (!(await cache.match(new URL(entry.path, document.baseURI).href))) {
      return false;
    }
  }
  return true;
}

// True when a finished snapshot sits in the browser cache. The page reads the
// same storage the worker writes, so the answer needs no message round trip.
export async function snapshotIsSaved(): Promise<boolean> {
  if (!("caches" in globalThis)) {
    return false;
  }
  try {
    for (const name of await caches.keys()) {
      if (!name.startsWith(SNAPSHOT_PREFIX)) continue;
      if (!/^[0-9a-f]{64}$/.test(name.slice(SNAPSHOT_PREFIX.length))) continue;
      if (await cacheIsComplete(name)) return true;
    }
  } catch {
    return false;
  }
  return false;
}

// Watch the stored snapshot while the page stays open. The worker writes a
// complete snapshot within a few seconds of the first load, so a short interval
// finds it. A takeover can also finish an install, so that event checks again.
// The watch stops at the first true answer or after one minute.
export function watchOfflineSave(onSaved: () => void): void {
  if (!isHostedPage()) {
    return;
  }
  let attempts = 0;
  let done = false;
  const check = async (): Promise<void> => {
    if (done) {
      return;
    }
    if (await snapshotIsSaved()) {
      done = true;
      onSaved();
      return;
    }
    attempts += 1;
    if (attempts < 120) {
      window.setTimeout(() => void check(), 500);
    }
  };
  navigator.serviceWorker?.addEventListener("controllerchange", () => void check());
  void check();
}

export function registerOfflineWorker(): void {
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
