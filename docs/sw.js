/* 势能雷达 Service Worker —— 离线/弱网兜底（v2）。

策略（v2 修复 v1 的"老用户长期吃旧前端"问题）：
  - index.html：network-first，网络失败才回退缓存 —— 保证每次打开都是
    最新页面结构（页面用 ?v=N 引用资源，天然绕过旧缓存）；
  - 静态资源（css/js）：stale-while-revalidate —— 先用缓存立即渲染，
    后台静默更新；
  - 数据文件：network-first，失败回退缓存（旧数据优于白屏）。
  - 缓存名带版本，activate 时清掉所有旧版本缓存。
*/
const CACHE = "ashare-radar-v2";
const STATIC_ASSETS = ["./styles.css", "./app.js", "./sw.js"];
const HTML_PATHS = ["./", "./index.html"];
const DATA = [
  "./data/latest.json",
  "./data/mainline.json",
  "./data/klines.json",
  "./data/tracked.json",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(["./index.html", "./styles.css", "./app.js"]))
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
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  const pathname = url.pathname;

  const isHtml = HTML_PATHS.some((p) => pathname.endsWith(p) || pathname.endsWith("/ashare-radar/"));
  if (isHtml) {
    // 页面结构：网络优先，离线兜底
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req).then((cached) => cached || caches.match("./index.html")))
    );
    return;
  }

  const isData = DATA.some((d) => pathname.endsWith(d));
  if (isData) {
    // 数据：网络优先，失败回退缓存
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
          return res;
        })
        .catch(() => caches.match(req))
    );
    return;
  }

  if (STATIC_ASSETS.some((s) => pathname.endsWith(s))) {
    // 静态资源：stale-while-revalidate
    event.respondWith(
      caches.match(req).then((cached) => {
        const refresh = fetch(req)
          .then((res) => {
            if (res.ok) {
              const copy = res.clone();
              caches.open(CACHE).then((c) => c.put(req, copy));
            }
            return res;
          })
          .catch(() => cached);
        return cached || refresh;
      })
    );
  }
});
