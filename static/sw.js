// static/sw.js
// Service Worker для Web Push уведомлений.
// Браузер регистрирует его через navigator.serviceWorker.register('/static/sw.js').
// Живёт отдельно от остального JS — браузер кэширует и запускает его в фоне.

self.addEventListener('push', function (event) {
  if (!event.data) return;

  let data;
  try {
    data = event.data.json();
  } catch {
    data = { title: 'PulseHR', body: event.data.text() };
  }

  const options = {
    body:    data.body  || 'Новое событие в PulseHR',
    icon:    '/static/icon.png',
    badge:   '/static/icon.png',
    data:    { survey_id: data.survey_id, url: data.url || '/' },
    actions: [
      { action: 'open',    title: 'Пройти опрос' },
      { action: 'dismiss', title: 'Закрыть'       },
    ],
  };

  event.waitUntil(
    self.registration.showNotification(data.title || 'PulseHR', options)
  );
});

self.addEventListener('notificationclick', function (event) {
  event.notification.close();

  if (event.action === 'dismiss') return;

  const url = event.notification.data?.url || '/';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      for (const client of list) {
        if (client.url.includes(self.location.origin) && 'focus' in client) {
          client.navigate(url);
          return client.focus();
        }
      }
      return clients.openWindow(url);
    })
  );
});
