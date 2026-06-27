const CACHE_NAME = 'secretary-ai-v78';
const SHARE_CACHE = 'share-target-cache';
const ASSETS = [
  '/',
  '/static/css/app_v12.css',
  '/static/js/app_v12.js',
  '/static/manifest.json',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Web Share Target (POST) — PayPay 等から共有された画像/テキストを受け取り、
// SW のキャッシュに退避してからアプリ本体へリダイレクトする。
async function handleShareTarget(request) {
  try {
    const form = await request.formData();
    const text = form.get('text') || '';
    const title = form.get('title') || '';
    const sharedUrl = form.get('url') || '';
    // パラメータ名に依存せず、最初の画像ファイルを拾う
    let imageFile = null;
    for (const value of form.values()) {
      if (value instanceof File && (value.type || '').startsWith('image/')) {
        imageFile = value;
        break;
      }
    }
    const cache = await caches.open(SHARE_CACHE);
    await cache.put(
      '/__share_payload__',
      new Response(
        JSON.stringify({
          text: String(text),
          title: String(title),
          url: String(sharedUrl),
          hasImage: !!imageFile,
        }),
        { headers: { 'Content-Type': 'application/json' } }
      )
    );
    if (imageFile) {
      await cache.put(
        '/__share_image__',
        new Response(imageFile, {
          headers: { 'Content-Type': imageFile.type || 'image/jpeg' },
        })
      );
    } else {
      await cache.delete('/__share_image__');
    }
  } catch (e) {
    /* ignore — 失敗してもアプリは開く */
  }
  return Response.redirect('/?share-target=1', 303);
}

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // 共有ターゲットの受け口 (POST /share-target)
  if (event.request.method === 'POST' && url.pathname === '/share-target') {
    event.respondWith(handleShareTarget(event.request));
    return;
  }
  // 退避した共有ペイロード/画像をアプリ本体へ返す
  if (url.pathname === '/__share_payload__' || url.pathname === '/__share_image__') {
    event.respondWith(
      caches.open(SHARE_CACHE)
        .then((c) => c.match(url.pathname))
        .then((r) => r || new Response('', { status: 404 }))
    );
    return;
  }

  // API calls should always go to network
  if (event.request.url.includes('/api/')) {
    event.respondWith(fetch(event.request));
    return;
  }
  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});

// SKIP_WAITING — app_v12.js から新しい SW を即時有効化する
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'SKIP_WAITING') self.skipWaiting();
});

// Push notification handler
self.addEventListener('push', (event) => {
  let data = { title: 'マネージャー', body: '新しいメッセージがあります', url: '/' };
  if (event.data) {
    try { data = { ...data, ...event.data.json() }; }
    catch (e) { data.body = event.data.text() || data.body; }
  }
  const opts = {
    body: data.body,
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/icon-192.png',
    tag: 'manager-msg',
    renotify: true,
    vibrate: [200, 100, 200],
    data: { url: data.url || '/', qid: data.qid, answers: data.answers || {} },
  };
  // ログ質問の通知アクション（機能E）: ボタンから1タップで回答
  if (Array.isArray(data.actions) && data.actions.length) {
    opts.actions = data.actions.slice(0, 2);
  }
  event.waitUntil(self.registration.showNotification(data.title, opts));
});

// Notification click handler — focus existing tab or open new one
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const d = event.notification.data || {};
  let target = d.url || '/';
  // アクションボタンが押された場合（機能E）: アプリを deep-link で開き、自動で回答を記録
  if (event.action && d.qid && d.answers && d.answers[event.action]) {
    target = `/?logq=${encodeURIComponent(d.qid)}&ans=${encodeURIComponent(d.answers[event.action])}`;
  }
  event.waitUntil((async () => {
    const allClients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const client of allClients) {
      if ('focus' in client) {
        await client.focus();
        if ('navigate' in client) {
          try { await client.navigate(target); } catch (e) { /* ignore */ }
        }
        return;
      }
    }
    if (self.clients.openWindow) {
      await self.clients.openWindow(target);
    }
  })());
});
