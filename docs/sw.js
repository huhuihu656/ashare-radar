/* 势能雷达 Service Worker —— 离线/弱网兜底。
 *
 * 策略：
 *  - 静态资源（html/css/js/图标）预缓存，cache-first：后续访问即使
 *    GitHub Pages 抖动也能秒开；
 *  - 数据文件（latest.json/klines.json/mainline.json）network-first，
 *    失败时回退缓存（展示旧数据并标注），保证任何网络条件下页面可用。
 */
const CACHE = "ashare-radar-v1";
const STATIC = [
  "./",
  "./index.html",
  "./styles.css",
  "./app.js",
];
const DATA = [
  "./data/latest.json",
  "./data/mainline.json",
  "./data/klines.json",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(STATIC))
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET") return;
  const isData = DATA.some((d) => url.pathname.endsWith(d));
  if (isData) {
    // 数据：网络优先，失败回退缓存（旧数据也优于白屏）
    event.respondWith(
      fetch(event.request)
        .then((response) => {
          const copy = response.clone();
          caches.open(CACHE).then((cache) => cache.put(event.request, copy));
          return response;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }
  if (STATIC.some((s) => url.pathname.endsWith(s)) || url.pathname.endsWith("/ashare-radar/")) {
    // 静态：缓存优先，后台更新
    event.respondWith(
      caches.match(event.request).then((cached) => {
        const refresh = fetch(event.request)
          .then((response) => {
            if (response.ok) {
              const copy = response.clone();
              caches.open(CACHE).then((cache) => cache.put(event.request, copy));
            }
            return response;
          })
          .catch(() => cached);
        return cached || refresh;
      })
    );
  }
});
