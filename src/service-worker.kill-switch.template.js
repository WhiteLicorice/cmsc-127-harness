// The kill switch. Deploy it as service-worker.js to remove the offline
// snapshot from every open page. It clears only harness caches and then
// unregisters itself. A reader gets the network page at the next load.

const SNAPSHOT_PREFIX = "cmsc127-harness-";

self.addEventListener("install", (event) => {
  event.waitUntil(self.skipWaiting());
});

self.addEventListener("activate", (event) => {
  event.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names
      .filter((name) => name.startsWith(SNAPSHOT_PREFIX))
      .map((name) => caches.delete(name)));
    await self.clients.claim();
    await self.registration.unregister();
  })());
});
