// The harness page offline worker. The build fills in the build ID placeholder
// with the build ID. Each build installs one immutable snapshot. A failed
// install keeps the active snapshot, so a half-copied page never reaches a
// reader.

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
  const response = await fetch(requestFor(url, "no-store"));
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

    // Check every file before the first write, so a mismatch leaves no
    // half-installed page behind.
    const fetched = [];
    for (const entry of manifest.files) {
      const url = scoped(entry.path);
      const response = await fetch(requestFor(url));
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

async function tellClients(message) {
  const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  clients.forEach((client) => client.postMessage(message));
}

function offlineResponse(status, text) {
  return new Response(text, {
    status,
    statusText: text,
    headers: { "Content-Type": "text/plain; charset=utf-8" },
  });
}

async function cachedPage() {
  try {
    const cache = await caches.open(snapshotCacheName());
    return await cache.match(requestFor(scoped(PAGE_PATH)));
  } catch {
    return undefined;
  }
}

async function handleNavigation(request) {
  const fallback = cachedPage();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), NAVIGATION_TIMEOUT);
  try {
    const response = await fetch(request, { signal: controller.signal });
    if (response.ok) return response;
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
  // Live database data. A cached answer would show tables that are not there.
  return !path.startsWith("api/");
}

async function handleAsset(request) {
  try {
    const cache = await caches.open(snapshotCacheName());
    const cached = await cache.match(request);
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
    await tellClients({ type: "offline-ready", buildId: OFFLINE_BUILD_ID });
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
