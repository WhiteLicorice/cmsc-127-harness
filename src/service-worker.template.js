// The harness page uses this worker to keep an offline snapshot. The build
// fills in the build ID placeholder. Each build installs one immutable
// snapshot, and a failed install keeps the active snapshot. A half-copied page
// never reaches a reader.

const OFFLINE_BUILD_ID = "__OFFLINE_BUILD_ID__";
const SNAPSHOT_PREFIX = "cmsc127-harness-";
const MANIFEST_PATH = "offline-manifest.json";
const PAGE_PATH = "index.html";
const SCHEMA_VERSION = 1;
const NAVIGATION_TIMEOUT = 3000;

function snapshotCacheName(buildId = OFFLINE_BUILD_ID) {
  return `${SNAPSHOT_PREFIX}${buildId}`;
}

function scoped(path) {
  return new URL(path, self.registration.scope);
}

function requestFor(url, cache = "default") {
  return new Request(url.href || url, { method: "GET", cache });
}

// The HTTP cache may hold the previous build. A file fetch must bypass it, or a
// stale body fails the hash check and the next build never installs.
function freshRequestFor(url) {
  return requestFor(url, "no-store");
}

function hex(bytes) {
  return Array.from(new Uint8Array(bytes), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function sha256(buffer) {
  return hex(await crypto.subtle.digest("SHA-256", buffer));
}

function invalidManifest(message) {
  return new Error(`Offline manifest is not usable: ${message}`);
}

async function readManifest() {
  const url = scoped(MANIFEST_PATH);
  const response = await fetch(freshRequestFor(url));
  if (!response.ok) throw invalidManifest(`the fetch returned ${response.status}`);
  let manifest;
  try {
    manifest = await response.json();
  } catch {
    throw invalidManifest("the file is not JSON");
  }
  if (manifest.schemaVersion !== SCHEMA_VERSION) {
    throw invalidManifest(`schema ${manifest.schemaVersion} is not supported`);
  }
  if (manifest.buildId !== OFFLINE_BUILD_ID) {
    throw invalidManifest(`build ${manifest.buildId} does not match the worker`);
  }
  if (!Array.isArray(manifest.files) || manifest.files.length === 0) {
    throw invalidManifest("the file list is empty");
  }
  for (const entry of manifest.files) {
    if (typeof entry?.path !== "string" || !/^[0-9a-f]{64}$/.test(entry.hash)) {
      throw invalidManifest("a file entry has no path or no valid hash");
    }
  }
  return manifest;
}

async function snapshotIsComplete(cache, manifest) {
  if (!(await cache.match(requestFor(scoped(MANIFEST_PATH))))) return false;
  for (const entry of manifest.files) {
    if (!(await cache.match(requestFor(scoped(entry.path))))) return false;
  }
  return true;
}

async function installSnapshot() {
  const cacheName = snapshotCacheName();
  const existed = (await caches.keys()).includes(cacheName);
  const cache = await caches.open(cacheName);
  try {
    const manifest = await readManifest();
    if (await snapshotIsComplete(cache, manifest)) {
      await self.skipWaiting();
      return;
    }

    // Check every file before the first write. The fetch bypasses the HTTP
    // cache, so a stale body cannot fail a good build.
    const fetched = [];
    for (const entry of manifest.files) {
      const url = scoped(entry.path);
      const response = await fetch(freshRequestFor(url));
      if (!response.ok) throw new Error(`Offline file returned ${response.status}: ${entry.path}`);
      const body = await response.clone().arrayBuffer();
      if (await sha256(body) !== entry.hash) {
        throw new Error(`Offline file hash does not match: ${entry.path}`);
      }
      fetched.push({ url, response });
    }

    for (const file of fetched) {
      await cache.put(requestFor(file.url), file.response);
    }
    await cache.put(requestFor(scoped(MANIFEST_PATH)), new Response(JSON.stringify(manifest), {
      headers: { "Content-Type": "application/json" },
    }));
    await self.skipWaiting();
  } catch (error) {
    // Keep the active snapshot. Remove only a snapshot this install created.
    if (!existed) await caches.delete(cacheName).catch(() => undefined);
    throw error;
  }
}

async function deleteOlderSnapshots() {
  const current = snapshotCacheName();
  const names = await caches.keys();
  await Promise.all(names
    .filter((name) => name.startsWith(SNAPSHOT_PREFIX) && name !== current)
    .map((name) => caches.delete(name)));
}

function offlineResponse(status, text) {
  return new Response(text, {
    status,
    statusText: text,
    headers: { "Content-Type": "text/plain; charset=utf-8" },
  });
}

// Read the snapshot without creating it. Opening a missing cache would plant an
// empty one, which a later failed install would then keep.
async function snapshotCache() {
  const name = snapshotCacheName();
  if (!(await caches.keys()).includes(name)) return null;
  return caches.open(name);
}

async function cachedPage() {
  try {
    const cache = await snapshotCache();
    return cache === null ? undefined : await cache.match(requestFor(scoped(PAGE_PATH)));
  } catch {
    return undefined;
  }
}

// A page keeps its HTML and its scripts from one build. A failed install keeps
// the active snapshot, so the network can answer with the next build while the
// scripts still come from the snapshot. The page then fails on a changed
// element. Compare the network page with the active page, and serve the active
// page when the two bodies belong to different builds.
async function pageForActiveBuild(response) {
  const type = response.headers.get("Content-Type") ?? "";
  if (!type.includes("text/html")) return response;
  try {
    const cache = await snapshotCache();
    if (cache === null) return response;
    const active = await cache.match(requestFor(scoped(PAGE_PATH)));
    if (!active) return response;
    const [networkHash, activeHash] = await Promise.all([
      sha256(await response.clone().arrayBuffer()),
      sha256(await active.clone().arrayBuffer()),
    ]);
    return networkHash === activeHash ? response : active;
  } catch {
    // A cache read can fail. The network page still answers, as it did before.
    return response;
  }
}

async function handleNavigation(request) {
  const fallback = cachedPage();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), NAVIGATION_TIMEOUT);
  try {
    const response = await fetch(request, { signal: controller.signal });
    if (response.ok) return pageForActiveBuild(response);
  } catch {
    // The network is slow or absent. The snapshot answers below.
  } finally {
    clearTimeout(timer);
  }
  return (await fallback) ?? offlineResponse(503, "The page is not available offline.");
}

function answersRequest(request) {
  if (request.method !== "GET") return false;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return false;
  const scope = self.registration.scope;
  if (!url.href.startsWith(scope)) return false;
  const path = url.href.slice(scope.length);
  // The panels read live database data. A cached answer would show tables that
  // are not there.
  return !path.startsWith("api/");
}

async function handleAsset(request) {
  try {
    const cache = await snapshotCache();
    const cached = cache === null ? undefined : await cache.match(request);
    if (cached) return cached;
    return await fetch(request);
  } catch {
    return offlineResponse(503, "This file is not available offline.");
  }
}

self.addEventListener("install", (event) => {
  event.waitUntil(installSnapshot());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    await deleteOlderSnapshots();
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.mode === "navigate" && new URL(request.url).href.startsWith(self.registration.scope)) {
    event.respondWith(handleNavigation(request));
    return;
  }
  if (answersRequest(request)) event.respondWith(handleAsset(request));
});
