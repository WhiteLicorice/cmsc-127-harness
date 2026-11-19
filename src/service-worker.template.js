// The harness page uses this worker to keep an offline snapshot. The build
// fills in the build ID placeholder. Each build installs one immutable
// snapshot, and a failed install keeps the active snapshot. A half-copied page
// never reaches a reader.
//
// One document reads its HTML and its scripts from one build. A new build can
// take over while an older document still boots. The worker therefore records
// which build answered each document, inside that build's snapshot. A file
// request follows the record, and an older snapshot stays until no open
// document names it.

const OFFLINE_BUILD_ID = "__OFFLINE_BUILD_ID__";
const SNAPSHOT_PREFIX = "cmsc127-harness-";
const CLIENT_PATH = "__client__/";
const MANIFEST_PATH = "offline-manifest.json";
const PAGE_PATH = "index.html";
const SCHEMA_VERSION = 1;
const NAVIGATION_TIMEOUT = 3000;
// A navigation writes its record before the browser lists the new document.
// Until that document appears in the client list, the record holds its snapshot
// for this long. The grace also covers a replaced document that has not closed.
const CLIENT_GRACE = 10_000;

function snapshotCacheName(buildId = OFFLINE_BUILD_ID) {
  return `${SNAPSHOT_PREFIX}${buildId}`;
}

// A snapshot name ends with the 64-character build ID. No other name carries
// that form, so this test keeps a snapshot apart from any other cache.
function buildOf(name) {
  if (!name.startsWith(SNAPSHOT_PREFIX)) return null;
  const buildId = name.slice(SNAPSHOT_PREFIX.length);
  return /^[0-9a-f]{64}$/.test(buildId) ? buildId : null;
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
    // The manifest goes in last, so a snapshot with a manifest is complete.
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

// Read a snapshot without creating it. Opening a missing cache would plant an
// empty one, which a later failed install would then keep.
async function existingSnapshot(buildId) {
  const name = snapshotCacheName(buildId);
  if (!(await caches.keys()).includes(name)) return null;
  return caches.open(name);
}

function clientKey(clientId) {
  return scoped(CLIENT_PATH + encodeURIComponent(clientId));
}

function clientIdOf(url) {
  const prefix = scoped(CLIENT_PATH).href;
  return url.startsWith(prefix) ? decodeURIComponent(url.slice(prefix.length)) : "";
}

// The record sits in the snapshot that answers the document, so a later worker
// reads it with one lookup. The record goes away with its snapshot. It holds
// the time it was written, because a navigation has no client object yet.
async function rememberBuild(clientId, buildId) {
  if (!clientId) return;
  try {
    const cache = await existingSnapshot(buildId);
    if (cache === null) return;
    await cache.put(requestFor(clientKey(clientId)), new Response(`${buildId} ${Date.now()}`));
  } catch {
    // The active snapshot still answers when the record is missing.
  }
}

async function forgetBuild(clientId) {
  if (!clientId) return;
  try {
    for (const name of await caches.keys()) {
      if (buildOf(name) === null) continue;
      const cache = await caches.open(name);
      await cache.delete(requestFor(clientKey(clientId)));
    }
  } catch {
    // A cache delete can fail. The record then ages out at the next pass.
  }
}

async function buildFor(clientId) {
  if (!clientId) return null;
  try {
    for (const name of await caches.keys()) {
      if (buildOf(name) === null) continue;
      const cache = await caches.open(name);
      if (await cache.match(requestFor(clientKey(clientId)))) return buildOf(name);
    }
  } catch {
    return null;
  }
  return null;
}

async function liveClientIds() {
  const clients = await self.clients.matchAll({ includeUncontrolled: true, type: "all" });
  return new Set(clients.map((client) => client.id).filter((id) => Boolean(id)));
}

// A page that loaded before this worker controlled it has no record. The worker
// that claims that page is the build that installed just after the load, so it
// records its own build. A page with a record keeps it, and a takeover leaves
// the older page on its older build.
async function adoptClients() {
  let clients;
  try {
    clients = await self.clients.matchAll({ includeUncontrolled: true, type: "all" });
  } catch {
    return;
  }
  for (const client of clients) {
    if (!client.id) continue;
    if (await buildFor(client.id) === null) {
      await rememberBuild(client.id, OFFLINE_BUILD_ID);
    }
  }
}

// Delete an older snapshot only when no open document names it. A record for a
// client that is gone stays for the grace period, because the navigation that
// wrote it may still be in flight.
async function deleteUnusedSnapshots() {
  const current = snapshotCacheName();
  const keep = new Set([current]);
  try {
    const live = await liveClientIds();
    const now = Date.now();
    for (const name of await caches.keys()) {
      if (buildOf(name) === null) continue;
      const cache = await caches.open(name);
      let held = false;
      for (const request of await cache.keys()) {
        const clientId = clientIdOf(request.url);
        if (clientId === "") continue;
        const record = await cache.match(request);
        const stamp = Number((record ? await record.text() : "").trim().split(" ")[1] ?? 0);
        if (live.has(clientId) || now - stamp < CLIENT_GRACE) {
          held = true;
          continue;
        }
        await cache.delete(request);
      }
      if (held) keep.add(name);
    }
  } catch {
    // A client list can fail. Keep the older snapshots rather than risk a
    // half-loaded page, and try again at the next pass.
    return;
  }
  await Promise.all((await caches.keys())
    .filter((name) => buildOf(name) !== null && !keep.has(name))
    .map((name) => caches.delete(name)));
}

function offlineResponse(status, text) {
  return new Response(text, {
    status,
    statusText: text,
    headers: { "Content-Type": "text/plain; charset=utf-8" },
  });
}

async function cachedPage(buildId = OFFLINE_BUILD_ID) {
  try {
    const cache = await existingSnapshot(buildId);
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
//
// The compare reads the whole network body. The caller keeps the navigation
// deadline alive for that read, so a slow body reaches the cached page.
async function pageForActiveBuild(response) {
  const type = response.headers.get("Content-Type") ?? "";
  if (!type.includes("text/html")) return response;
  // Read the body first. A stalled or failed body throws here, and the caller
  // answers with the cached page.
  const networkBody = await response.clone().arrayBuffer();
  try {
    const cache = await existingSnapshot(OFFLINE_BUILD_ID);
    if (cache === null) return response;
    const active = await cache.match(requestFor(scoped(PAGE_PATH)));
    if (!active) return response;
    const [networkHash, activeHash] = await Promise.all([
      sha256(networkBody),
      sha256(await active.clone().arrayBuffer()),
    ]);
    return networkHash === activeHash ? response : active;
  } catch {
    // A cache read can fail. The network page still answers, as it did before.
    return response;
  }
}

// The deadline covers the headers and the whole body. A timeout, an abort, or a
// failed body read answers with the cached page.
//
// The worker never aborts the navigation fetch. An abort fails the navigation
// before the cached page can answer. The deadline races the body read instead,
// and the network fetch runs on unseen.
async function handleNavigation(event) {
  const { request } = event;
  const documentId = event.resultingClientId || event.clientId;
  const fallback = cachedPage();
  let timer = 0;
  let expired = false;
  const deadline = new Promise((resolve) => {
    timer = setTimeout(() => {
      expired = true;
      resolve(null);
    }, NAVIGATION_TIMEOUT);
  });
  // Record the build before the fetch. A takeover can then find the record
  // while the navigation is still in flight. The previous document in this tab
  // is replaced, so its record goes now.
  await rememberBuild(documentId, OFFLINE_BUILD_ID);
  if (event.clientId && event.clientId !== documentId) {
    await forgetBuild(event.clientId);
    event.waitUntil(deleteUnusedSnapshots());
  }
  try {
    const page = (async () => {
      const response = await fetch(request);
      if (!response.ok) return null;
      return await pageForActiveBuild(response);
    })().catch(() => null);
    const answer = await Promise.race([page, deadline]);
    if (!expired && answer !== null && answer !== undefined) return answer;
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

// A file request follows the build of its document. Without a record, the
// active snapshot answers, as it did before.
async function handleAsset(event) {
  const { request } = event;
  try {
    const cache = await existingSnapshot((await buildFor(event.clientId)) ?? OFFLINE_BUILD_ID);
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
    await adoptClients();
    await deleteUnusedSnapshots();
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.mode === "navigate" && new URL(request.url).href.startsWith(self.registration.scope)) {
    event.respondWith(handleNavigation(event));
    return;
  }
  if (answersRequest(request)) event.respondWith(handleAsset(event));
});
