/* The softphone's offline shell.
 *
 * It precaches the phone itself and, for those URLs only, answers from the
 * network first. Everything else - the console, the API, the brand logo - is
 * left alone: a service worker in scope "/" that cached every same-origin GET
 * could otherwise hand the console a stale page or a stale API answer, which
 * is not this worker's business. The vendored SIP stack (/jssip.min.js) is
 * part of the shell: it is served from this origin and the phone cannot sign
 * in without it.
 *
 * The shell is fetched network-first and cached only as the offline fallback.
 * It used to answer cache-first behind a manually bumped version, and the
 * discipline failed: phone.js was fixed three times after the last bump, and
 * every browser that had cached the shell kept running the old build - one
 * that dialled the SIP domain's WebSocket and never saw the fix. A cache that
 * can shadow a deployed build is worse than no cache, so the network answer
 * wins every time it exists and the cache answers only when offline. The name
 * still bumps on layout changes so activation drops older caches.
 */
const CACHE = "eip-phone-v11";
const VERSION = "11";
/* The page requests versioned CSS/JS, so a browser with last release's
   public-cached /phone.js cannot answer the new page from its old URL. */
const SHELL = ["/phone", `/phone.css?v=${VERSION}`, `/phone.js?v=${VERSION}`, "/jssip.min.js", "/manifest.json"];
const SHELL_PATHS = SHELL.map(path => new URL(path, location.origin).pathname);

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
  if (url.origin !== location.origin || !SHELL_PATHS.includes(url.pathname)) return;
  /* Network first. The cache is the offline answer, not a second origin. */
  e.respondWith(fetch(e.request, {cache: "no-cache"}).then(async response => {
    if (response.ok) {
      try { await (await caches.open(CACHE)).put(e.request, response.clone()); }
      catch { /* Storage full must not break an online phone. */ }
    }
    return response;
  }).catch(error => caches.open(CACHE).then(cache => cache.match(e.request).then(cached =>
    /* Offline: a navigation still opens the phone from the cached shell; a
       missing asset is reported as the network failure it is. */
    cached || (e.request.mode === "navigate" ? cache.match("/phone") : Promise.reject(error))))));
});
