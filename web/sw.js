/* The softphone's offline shell.
 *
 * It precaches the phone itself and, for those URLs only, answers from the
 * cache first. Everything else - the console, the API, the brand logo - is
 * left alone: a service worker in scope "/" that cached every same-origin GET
 * could otherwise hand the console a stale page or a stale API answer, which
 * is not this worker's business. The vendored SIP stack (/jssip.min.js) is
 * part of the shell: it is served from this origin and the phone cannot sign
 * in without it.
 *
 * Bump CACHE on a release that changes the shell: install then replaces the
 * cached copies instead of serving the previous version forever. Activation
 * deletes every older eip-phone-* cache and the fetch handler only ever reads
 * the current one, so a shell cached by a previous release can never shadow
 * the new files (it used to, which kept broken pages alive after a fix).
 */
const CACHE = "eip-phone-v7";
const SHELL = ["/phone", "/phone.css", "/phone.js", "/jssip.min.js", "/manifest.json"];

self.addEventListener("install", e => e.waitUntil(
  caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())
));
self.addEventListener("activate", e => e.waitUntil(
  caches.keys()
    .then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key))))
    .then(() => self.clients.claim())
));

self.addEventListener("fetch", e => {
  if (e.request.method !== "GET") return;
  const url = new URL(e.request.url);
  if (url.origin !== location.origin || !SHELL.includes(url.pathname)) return;
  e.respondWith(caches.open(CACHE).then(cache => cache.match(e.request).then(cached => cached || fetch(e.request).then(response => {
    const copy = response.clone();
    cache.put(e.request, copy);
    return response;
  })
    /* Offline: a navigation still opens the phone from the cached shell; a
       missing asset is reported as the network failure it is. */
    .catch(error => (e.request.mode === "navigate" ? cache.match("/phone") : Promise.reject(error))))));
});
