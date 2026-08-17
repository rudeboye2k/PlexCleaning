function getCookie(name) {
  const m = document.cookie.match('(^|;)\\s*' + name + '\\s*=\\s*([^;]+)');
  return m ? decodeURIComponent(m.pop()) : '';
}

async function api(url, options = {}) {
  const opts = {
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': getCookie('plexcleaner_csrf')},
    ...options,
  };
  try {
    const res = await fetch(url, opts);
    if (res.status === 401) { location.href = '/login'; return null; }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { toast(data.error || `Request failed (${res.status})`, true); return null; }
    return data;
  } catch (err) {
    toast('Network error: ' + err.message, true);
    return null;
  }
}

function toast(message, isError = false) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = message;
  el.className = 'toast' + (isError ? ' toast-error' : '');
  setTimeout(() => el.classList.add('hidden'), 4000);
}

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, c =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

function val(id) {
  const el = document.getElementById(id);
  return el ? el.value : '';
}

function fmtDate(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return esc(iso);
  const days = Math.floor((Date.now() - d.getTime()) / 86400000);
  return `${d.toISOString().slice(0, 10)} <span class="muted small">(${days}d)</span>`;
}

function debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

// Highlight the current nav item.
document.querySelectorAll('.topbar nav a').forEach(a => {
  if (a.getAttribute('href') === location.pathname) a.classList.add('active');
});
