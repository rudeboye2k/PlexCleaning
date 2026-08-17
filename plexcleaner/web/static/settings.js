/* Schema-driven settings form.
 *
 * The server describes every field once (plexcleaner/schema.py) and this
 * renders it, for both the first-run wizard and the Settings page. Adding a
 * setting means touching the Python schema, not this file.
 */

const SettingsForm = (() => {
  // Sentinel the server understands as "keep the secret you already have".
  const UNCHANGED = '__unchanged__';

  let schema = null;
  let config = null;
  let provenance = {};
  let meta = {};
  let showAdvanced = false;

  const PROV_LABEL = {
    saved: ['saved', 'Set here in the web UI'],
    env: ['env', 'Comes from an environment variable'],
    file: ['file', 'Comes from config.yaml'],
    default: ['', ''],
  };

  async function load() {
    const [s, c] = await Promise.all([api('/api/schema'), api('/api/settings')]);
    if (!s || !c) return false;
    schema = s;
    config = c.config;
    provenance = c.provenance || {};
    meta = c;
    return true;
  }

  // -- config path helpers ------------------------------------------------
  function getPath(path) {
    return path.split('.').reduce((node, key) => (node == null ? undefined : node[key]), config);
  }

  function setPath(path, value) {
    const parts = path.split('.');
    let node = config;
    for (const key of parts.slice(0, -1)) {
      if (node[key] == null || typeof node[key] !== 'object') node[key] = {};
      node = node[key];
    }
    node[parts.slice(-1)[0]] = value;
  }

  function fullPath(section, field, index) {
    if (section.repeatable) return `${section.list_path}.${index}.${field.path}`;
    return `${section.key}.${field.path}`;
  }

  // Repeatable sections live in arrays, so the dotted path needs index-aware access.
  function readValue(section, field, index) {
    if (section.repeatable) {
      const list = getPath(section.list_path) || [];
      return list[index] ? list[index][field.path] : undefined;
    }
    return getPath(`${section.key}.${field.path}`);
  }

  function writeValue(section, field, index, value) {
    if (section.repeatable) {
      const list = getPath(section.list_path) || [];
      while (list.length <= index) list.push({});
      list[index][field.path] = value;
      setPath(section.list_path, list);
    } else {
      setPath(`${section.key}.${field.path}`, value);
    }
  }

  // -- rendering ----------------------------------------------------------
  function fieldHtml(section, field, index) {
    const id = 'f_' + fullPath(section, field, index).replace(/[^a-zA-Z0-9]/g, '_');
    const value = readValue(section, field, index);
    const provKey = section.repeatable ? section.list_path : `${section.key}.${field.path}`;
    const [provText, provTitle] = PROV_LABEL[provenance[provKey]] || PROV_LABEL.default;
    const badge = provText ? `<span class="prov prov-${provenance[provKey]}" title="${esc(provTitle)}">${provText}</span>` : '';
    const unit = field.unit ? `<span class="unit">${esc(field.unit)}</span>` : '';
    const help = field.help ? `<p class="field-help">${esc(field.help)}</p>` : '';
    const cls = 'field' + (field.advanced ? ' field-advanced' : '');

    let input;
    switch (field.type) {
      case 'bool':
        // The badge goes inside the label so it stays an inline chip rather
        // than stretching across the column flexbox.
        return `<div class="${cls} field-bool">
          <label class="switch">
            <input type="checkbox" id="${id}" data-section="${esc(section.key)}"
                   data-field="${esc(field.path)}" data-index="${index}" ${value ? 'checked' : ''}>
            <span>${esc(field.label)}${badge}</span>
          </label>${help}</div>`;

      case 'select':
        input = `<select id="${id}" data-section="${esc(section.key)}" data-field="${esc(field.path)}" data-index="${index}">
          ${(field.options || []).map(o =>
            `<option value="${esc(o)}" ${String(value) === String(o) ? 'selected' : ''}>${esc(o)}</option>`).join('')}
        </select>`;
        break;

      case 'list':
        input = `<input type="text" id="${id}" data-section="${esc(section.key)}"
                  data-field="${esc(field.path)}" data-index="${index}" data-list="1"
                  value="${esc((value || []).join(', '))}"
                  placeholder="${esc(field.placeholder || 'comma, separated, values')}">`;
        break;

      case 'number':
        input = `<input type="number" id="${id}" data-section="${esc(section.key)}"
                  data-field="${esc(field.path)}" data-index="${index}"
                  value="${value ?? ''}"
                  ${field.min != null ? `min="${field.min}"` : ''} ${field.max != null ? `max="${field.max}"` : ''}>`;
        break;

      case 'password':
        input = `<input type="password" id="${id}" data-section="${esc(section.key)}"
                  data-field="${esc(field.path)}" data-index="${index}"
                  value="${value === UNCHANGED ? '' : esc(value || '')}"
                  autocomplete="new-password"
                  placeholder="${value === UNCHANGED ? '•••••••• (leave blank to keep)' : esc(field.placeholder || '')}">`;
        break;

      default:
        input = `<input type="${field.type === 'url' ? 'text' : 'text'}" id="${id}"
                  data-section="${esc(section.key)}" data-field="${esc(field.path)}" data-index="${index}"
                  value="${esc(value ?? '')}" placeholder="${esc(field.placeholder || '')}">`;
    }

    return `<div class="${cls}">
      <label for="${id}">${esc(field.label)}${badge}</label>
      <div class="input-row">${input}${unit}</div>${help}</div>`;
  }

  function instanceHtml(section, index) {
    const list = getPath(section.list_path) || [];
    const name = (list[index] && list[index].name) || `${section.key}-${index + 1}`;
    return `<div class="instance" data-index="${index}">
      <div class="instance-head">
        <strong>${esc(name)}</strong>
        <div>
          <button class="btn btn-small test-btn" data-kind="${esc(section.key)}" data-index="${index}">Test</button>
          <button class="btn btn-small remove-instance" data-list="${esc(section.list_path)}" data-index="${index}">Remove</button>
        </div>
      </div>
      <div class="test-result" data-kind="${esc(section.key)}" data-index="${index}"></div>
      <div class="field-grid">${section.fields.map(f => fieldHtml(section, f, index)).join('')}</div>
    </div>`;
  }

  function sectionHtml(section) {
    const body = section.repeatable
      ? `${((getPath(section.list_path) || []).map((_, i) => instanceHtml(section, i)).join('')) ||
           '<p class="muted">No instances configured.</p>'}
         <button class="btn btn-small add-instance" data-list="${esc(section.list_path)}">+ Add ${esc(section.title)} instance</button>`
      : `${section.service ? `<div class="test-row">
            <button class="btn btn-small test-btn" data-kind="${esc(section.service)}" data-index="0">Test connection</button>
            ${section.service === 'plex' ? '<button class="btn btn-small" id="discover-libs">Discover libraries</button>' : ''}
            <span class="test-result" data-kind="${esc(section.service)}" data-index="0"></span>
          </div>` : ''}
         <div class="field-grid">${section.fields.map(f => fieldHtml(section, f, 0)).join('')}</div>`;

    return `<section class="card settings-section" data-section="${esc(section.key)}">
      <div class="card-head">
        <h2>${section.icon ? section.icon + ' ' : ''}${esc(section.title)}</h2>
      </div>
      ${section.description ? `<p class="muted small">${esc(section.description)}</p>` : ''}
      ${body}
    </section>`;
  }

  function render(container, sectionKeys) {
    const keys = sectionKeys && sectionKeys.length
      ? sectionKeys
      : schema.sections.map(s => s.key);
    container.innerHTML = keys
      .map(k => schema.sections.find(s => s.key === k))
      .filter(Boolean)
      .map(sectionHtml)
      .join('');
    bind(container);
    applyAdvanced(container);
  }

  function applyAdvanced(container) {
    container.querySelectorAll('.field-advanced').forEach(el => {
      el.style.display = showAdvanced ? '' : 'none';
    });
  }

  function setAdvanced(value, container) {
    showAdvanced = value;
    applyAdvanced(container);
  }

  // -- input binding ------------------------------------------------------
  function bind(container) {
    container.querySelectorAll('[data-field]').forEach(el => {
      const handler = () => {
        const section = schema.sections.find(s => s.key === el.dataset.section) ||
                        schema.sections.find(s => s.service === el.dataset.section);
        if (!section) return;
        const field = section.fields.find(f => f.path === el.dataset.field);
        const index = Number(el.dataset.index || 0);
        let value;
        if (el.type === 'checkbox') value = el.checked;
        else if (el.dataset.list) value = el.value.split(',').map(s => s.trim()).filter(Boolean);
        else if (el.type === 'number') value = el.value === '' ? null : Number(el.value);
        else value = el.value;

        // A blank password means "keep what is stored", not "clear it".
        // To remove a service entirely, switch its Enabled toggle off.
        if (field && field.type === 'password' && value === '') value = UNCHANGED;
        writeValue(section, field, index, value);
      };
      el.addEventListener('change', handler);
      el.addEventListener('input', handler);
    });

    container.querySelectorAll('.add-instance').forEach(btn => btn.onclick = () => {
      const list = getPath(btn.dataset.list) || [];
      const kind = btn.dataset.list;
      list.push({
        name: `${kind}-${list.length + 1}`, enabled: false, url: '', api_key: '',
        delete_files: true, add_import_exclusion: true, verify_ssl: false, timeout: 60,
      });
      setPath(btn.dataset.list, list);
      rerender(container);
    });

    container.querySelectorAll('.remove-instance').forEach(btn => btn.onclick = () => {
      const list = getPath(btn.dataset.list) || [];
      list.splice(Number(btn.dataset.index), 1);
      setPath(btn.dataset.list, list);
      rerender(container);
    });

    container.querySelectorAll('.test-btn').forEach(btn => btn.onclick = () => testService(btn, container));

    const discover = container.querySelector('#discover-libs');
    if (discover) discover.onclick = () => discoverLibraries(container);
  }

  let lastKeys = null;
  function rerender(container) {
    render(container, lastKeys);
  }

  // -- service testing / discovery ---------------------------------------
  async function testService(btn, container) {
    const kind = btn.dataset.kind;
    const index = Number(btn.dataset.index || 0);
    const section = schema.sections.find(s => s.key === kind || s.service === kind);
    const box = container.querySelector(`.test-result[data-kind="${kind}"][data-index="${index}"]`);
    const payload = {kind: kind};

    if (section.repeatable) {
      Object.assign(payload, (getPath(section.list_path) || [])[index] || {});
    } else {
      Object.assign(payload, getPath(section.key) || {});
    }
    btn.disabled = true;
    if (box) box.innerHTML = '<span class="muted small">Testing…</span>';
    const res = await api('/api/test-service', {method: 'POST', body: JSON.stringify(payload)});
    btn.disabled = false;
    if (!res || !box) return;
    box.innerHTML = res.ok
      ? `<span class="ok small">✓ Connected${res.version ? ' — v' + esc(res.version) : ''}</span>`
      : `<span class="bad small">✗ ${esc(res.detail || 'Failed')}</span>`;
  }

  async function discoverLibraries(container) {
    const plex = getPath('plex') || {};
    const res = await api('/api/discover/plex-libraries', {
      method: 'POST',
      body: JSON.stringify({url: plex.url, token: plex.token, verify_ssl: plex.verify_ssl}),
    });
    if (!res) return;
    const box = container.querySelector('.test-result[data-kind="plex"][data-index="0"]');
    const names = res.libraries.map(l => l.title);
    if (box) {
      box.innerHTML = `<span class="ok small">Found ${names.length} movie/show librar${names.length === 1 ? 'y' : 'ies'}:
        ${names.map(n => `<button class="tag tag-click exclude-lib" data-name="${esc(n)}" title="Click to exclude from scanning">${esc(n)}</button>`).join(' ')}
        </span>`;
      box.querySelectorAll('.exclude-lib').forEach(b => b.onclick = () => {
        const current = getPath('plex.exclude_libraries') || [];
        const name = b.dataset.name;
        const next = current.includes(name) ? current.filter(x => x !== name) : [...current, name];
        setPath('plex.exclude_libraries', next);
        toast(current.includes(name) ? `${name} will be scanned` : `${name} will be skipped`);
        rerender(container);
      });
    }
  }

  // -- saving -------------------------------------------------------------
  async function save(extra = {}) {
    const res = await api('/api/settings', {
      method: 'POST',
      body: JSON.stringify({config: config, ...extra}),
    });
    if (!res) return null;
    config = res.config;
    return res;
  }

  return {
    load,
    render: (container, keys) => { lastKeys = keys; render(container, keys); },
    save,
    setAdvanced,
    get config() { return config; },
    get meta() { return meta; },
    getPath,
    setPath,
  };
})();
