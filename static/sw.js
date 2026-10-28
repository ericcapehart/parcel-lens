// Minimal service worker so Android offers "Install app".
// It caches nothing: every request goes to the network, so a deploy is live on the next open.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
