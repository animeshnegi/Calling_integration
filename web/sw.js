const CACHE="eip-phone-v1";
const ASSETS=["/phone","/phone.css","/phone.js","/manifest.json","https://engineerip.com/static/img/logo.png","https://engineerip.com/static/img/preloder1.png"];
self.addEventListener("install",e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(ASSETS.filter(u=>u.startsWith("/")))).then(()=>self.skipWaiting())));
self.addEventListener("activate",e=>e.waitUntil(self.clients.claim()));
self.addEventListener("fetch",e=>{if(e.request.method!=="GET")return;e.respondWith(caches.match(e.request).then(c=>c||fetch(e.request).then(r=>{if(new URL(e.request.url).origin===location.origin){const copy=r.clone();caches.open(CACHE).then(x=>x.put(e.request,copy))}return r}).catch(()=>caches.match("/phone"))))});
