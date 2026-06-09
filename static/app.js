// static/app.js
// Подключается в base.html на всех страницах.
// Содержит:
//   - post() / get() — удобные обёртки над fetch с CSRF
//   - toast()        — всплывающие уведомления
//   - initPush()     — запрос разрешения и регистрация Web Push
//   - флеш-сообщения из Flask

// ── Toast ────────────────────────────────────────────────────────────────────

function toast(msg, type = 'info') {
  let wrap = document.getElementById('toast-container');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.id = 'toast-container';
    document.body.appendChild(wrap);
  }
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

// ── Fetch helpers ─────────────────────────────────────────────────────────────

async function post(url, data = {}) {
  const form = new FormData();
  Object.entries(data).forEach(([k, v]) => form.append(k, v));
  const res = await fetch(url, { method: 'POST', body: form });
  return res;
}

async function postJSON(url, data = {}) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  return res;
}

async function getJSON(url) {
  const res = await fetch(url);
  return res.json();
}

// ── Web Push ──────────────────────────────────────────────────────────────────

function urlBase64ToUint8Array(base64String) {
  const padding = '='.repeat((4 - base64String.length % 4) % 4);
  const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
  const rawData = window.atob(base64);
  return Uint8Array.from([...rawData].map(c => c.charCodeAt(0)));
}

async function initPush() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return;

  // Показываем мягкий pre-prompt только один раз
  if (localStorage.getItem('push_asked')) return;

  const banner = document.getElementById('push-banner');
  if (banner) banner.style.display = 'flex';
}

async function enablePush() {
  document.getElementById('push-banner')?.remove();
  localStorage.setItem('push_asked', '1');

  try {
    const reg = await navigator.serviceWorker.register('/static/sw.js');
    const { public_key } = await getJSON('/api/push/vapid-key');
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(public_key),
    });
    const subData = sub.toJSON();
    await postJSON('/api/push/subscribe', subData);
    toast('🔔 Push-уведомления включены', 'success');
  } catch (e) {
    console.warn('Push subscribe failed:', e);
  }
}

function dismissPush() {
  document.getElementById('push-banner')?.remove();
  localStorage.setItem('push_asked', '1');
}

// ── Init on DOM ready ─────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  // Автозакрытие flash-сообщений Flask через 4 секунды
  document.querySelectorAll('.flash-msg').forEach(el => {
    setTimeout(() => el.remove(), 4000);
  });

  // Предлагаем Push при первом визите
  initPush();

  // Подсветка активного пункта в навигации
  const path = location.pathname;
  document.querySelectorAll('.nav a').forEach(a => {
    if (a.getAttribute('href') === path ||
        (a.getAttribute('href') !== '/' && path.startsWith(a.getAttribute('href')))) {
      a.classList.add('active');
    }
  });
});
