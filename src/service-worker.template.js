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
const BUILDING_PATH = "__building__";
const MANIFEST_PATH = "offline-manifest.json";
const PAGE_PATH = "index.html";
const SCHEMA_VERSION = 1;
const NAVIGATION_TIMEOUT = 3000;
// A navigation writes its record before the browser lists the new document.
// Until that document appears in the client list, the record holds its snapshot
// for this long. The grace also covers a replaced document that has not closed.
const CLIENT_GRACE = 10_000;
// An install marks its own snapshot while it fills it. The mark carries the
// time of the last write. A cleanup in another worker keeps a snapshot with a
// fresh mark, so a reload cannot remove a download in progress. The mark stays
// until the new worker activates, which covers the takeover wait. A mark older
// than this limit belongs to an install that stopped, so its snapshot can go.
const BUILDING_LIMIT = 600_000;

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

// The lock of one snapshot. An install holds it while it fills the snapshot.
// A cleanup holds it while it deletes. The two steps then never meet on one
// cache.
function snapshotLock(name) {
  return `${name}-lock`;
}

// Run a step while this worker holds the lock of one snapshot. A browser
// without the lock API runs the step without a lock.
async function holdSnapshot(name, step) {
  const locks = self.navigator?.locks;
  if (!locks) return step();
  return locks.request(snapshotLock(name), step);
}

// Run a step only when no other worker holds the lock of one snapshot. The
// answer is false when the lock is busy, so a cleanup never waits.
async function holdSnapshotIfFree(name, step) {
  const locks = self.navigator?.locks;
  if (!locks) {
    await step();
    return true;
  }
  let ran = false;
  await locks.request(snapshotLock(name), { ifAvailable: true }, async (lock) => {
    if (lock === null) return;
    ran = true;
    await step();
  });
  return ran;
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
  // Hold the lock of this snapshot while it fills. A cleanup in another worker
  // takes the same lock before it deletes, so it never removes this snapshot.
  await holdSnapshot(cacheName, () => fillSnapshot(cacheName));
}

async function fillSnapshot(cacheName) {
  const cache = await caches.open(cacheName);
  try {
    // An active worker can run its cleanup while this install works. The
    // cleanup must then keep this snapshot. Write the record first, so the
    // cleanup can see that an install still fills the snapshot. The record
    // stays until this worker activates, which covers the wait before the
    // takeover.
    await cache.put(requestFor(scoped(BUILDING_PATH)), new Response(String(Date.now())));
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
      // Keep the record fresh for a long download. A cleanup in another worker
      // then still sees an install at work.
      await cache.put(requestFor(scoped(BUILDING_PATH)), new Response(String(Date.now())));
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
    // This install holds no snapshot now. Clear its record, then remove a
    // snapshot that no document can read. The active snapshot stays.
    await cache.delete(requestFor(scoped(BUILDING_PATH))).catch(() => undefined);
    if (!(await holdsManifest(cache))) {
      await caches.delete(cacheName).catch(() => undefined);
    }
    throw error;
  }
}

// True when the snapshot carries its manifest. A snapshot without one never
// answered a document, so its removal costs no open page.
async function holdsManifest(cache) {
  try {
    return (await cache.match(requestFor(scoped(MANIFEST_PATH)))) !== undefined;
  } catch {
    return false;
  }
}

// Open a snapshot only when it exists. An open of a missing cache plants an
// empty one, which a later cleanup keeps. The caller holds the lock of the
// snapshot, so no delete can remove the cache between the check and the open.
async function openSnapshot(name) {
  if (!(await caches.has(name))) return null;
  return caches.open(name);
}

// Open a snapshot only when it exists, under the lock of the snapshot. A
// cleanup in another worker deletes under the same lock, so the check and the
// open never meet a delete. A busy lock means an install or a delete owns the
// snapshot, so this read skips it.
async function existingSnapshot(buildId) {
  const name = snapshotCacheName(buildId);
  let cache = null;
  await holdSnapshotIfFree(name, async () => {
    cache = await openSnapshot(name);
  });
  return cache;
}

// Read one file from one snapshot without opening the cache. Opening a missing
// cache would plant an empty one.
async function snapshotMatch(buildId, request) {
  return caches.match(request, { cacheName: snapshotCacheName(buildId) });
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
      // A cleanup can remove the snapshot after the list. The check and the
      // open run under the lock of the snapshot, so the open never plants a
      // cache that the cleanup then keeps. A busy lock leaves the record to
      // age out at the next pass.
      await holdSnapshotIfFree(name, async () => {
        const cache = await openSnapshot(name);
        await cache?.delete(requestFor(clientKey(clientId)));
      });
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
      if (await snapshotMatch(buildOf(name), requestFor(clientKey(clientId)))) return buildOf(name);
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

// Delete an older snapshot only when no open document names it, and only when
// the snapshot belongs to a retired build. A record for a client that is gone
// stays for the grace period, because the navigation that wrote it may still be
// in flight. A snapshot with a fresh building record is still being filled, and
// it stays too.
//
// One scan decides what to delete. A snapshot that an install opens after that
// scan is not in the list, so the cleanup never meets it. The delete then runs
// under the lock of the snapshot, which keeps it apart from an install that
// fills the same cache.
//
// The open of each snapshot also runs under its lock. A cleanup in another
// worker deletes under the same lock, so this open never plants a cache that
// the other cleanup then keeps.
async function deleteUnusedSnapshots() {
  const current = snapshotCacheName();
  const unused = [];
  try {
    const live = await liveClientIds();
    const now = Date.now();
    for (const name of await caches.keys()) {
      if (buildOf(name) === null) continue;
      // A busy lock means an install or a delete owns the snapshot, so this
      // pass leaves it alone.
      let cache = null;
      const opened = await holdSnapshotIfFree(name, async () => {
        cache = await openSnapshot(name);
      });
      if (!opened || cache === null) continue;
      if (name === current) {
        // The current snapshot answers files. Its records still age out.
        await pruneClientRecords(cache, live, now);
        continue;
      }
      if (await snapshotIsUnused(cache, live, now)) unused.push(name);
    }
  } catch {
    // A client list can fail. Keep the older snapshots rather than risk a
    // half-loaded page, and try again at the next pass.
    return;
  }
  await Promise.all(unused.map((name) => deleteSnapshot(name)));
}

// True when no document reads this snapshot and no install fills it. The empty
// check comes first because an install opens its snapshot before it writes the
// record. A snapshot with files and no manifest is an install that stopped. No
// document can read it.
async function snapshotIsUnused(cache, live, now) {
  if (await isEmpty(cache)) return false;
  if (await buildingNow(cache, now)) return false;
  if (!(await holdsManifest(cache))) return true;
  return !(await pruneClientRecords(cache, live, now));
}

// Delete one snapshot that the scan found unused. The lock keeps this delete
// apart from an install that fills the same cache. The test runs again under
// the lock because an install can start between the scan and this step.
async function deleteSnapshot(name) {
  await holdSnapshotIfFree(name, async () => {
    try {
      const cache = await openSnapshot(name);
      if (cache === null) return;
      if (!(await snapshotIsUnused(cache, await liveClientIds(), Date.now()))) return;
      await caches.delete(name);
    } catch {
      // A failed check keeps the snapshot. The next pass tries again.
    }
  });
}

async function isEmpty(cache) {
  try {
    return (await cache.keys()).length === 0;
  } catch {
    return false;
  }
}

// True when an install wrote its building record less than BUILDING_LIMIT ago.
async function buildingNow(cache, now) {
  let record;
  try {
    record = await cache.match(requestFor(scoped(BUILDING_PATH)));
  } catch {
    return true;
  }
  if (!record) return false;
  const stamp = Number(await record.text());
  // A record that cannot be read still marks an install. Keep the snapshot
  // rather than risk the snapshot of a running install.
  if (!Number.isFinite(stamp)) return true;
  return now - stamp < BUILDING_LIMIT;
}

// Drop the records of documents that are gone. True when one record still
// belongs to a live document or sits inside its grace period.
async function pruneClientRecords(cache, live, now) {
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
  return held;
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
    return await snapshotMatch(buildId, requestFor(scoped(PAGE_PATH)));
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
    const active = await snapshotMatch(OFFLINE_BUILD_ID, requestFor(scoped(PAGE_PATH)));
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
    const buildId = (await buildFor(event.clientId)) ?? OFFLINE_BUILD_ID;
    const cached = await snapshotMatch(buildId, request);
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
    // Record the open documents first. The building record then goes, and a
    // cleanup in the replaced worker keeps the snapshot through those records.
    await adoptClients();
    await clearBuildingRecord();
    await deleteUnusedSnapshots();
    await self.clients.claim();
  })());
});

// This worker owns the snapshot now, so the record that marked it as filling
// has no further use. A cleanup after this point never waits on it.
async function clearBuildingRecord() {
  try {
    const cache = await existingSnapshot();
    await cache?.delete(requestFor(scoped(BUILDING_PATH)));
  } catch {
    // A failed delete leaves a record that ages out at BUILDING_LIMIT.
  }
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.mode === "navigate" && new URL(request.url).href.startsWith(self.registration.scope)) {
    event.respondWith(handleNavigation(event));
    return;
  }
  if (answersRequest(request)) event.respondWith(handleAsset(event));
});
