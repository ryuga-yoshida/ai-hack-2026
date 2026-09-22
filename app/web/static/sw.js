// Followup: インストール可能にするための最小の Service Worker（キャッシュはしない。常にネットワーク）
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
