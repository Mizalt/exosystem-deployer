/* --- panel_ai.js --- ИИ-помощник ПАНЕЛИ деплоера (ADR-103).
 *
 * Виджет рендерится ТОЛЬКО когда:
 *   1. панель открыта ВНУТРИ ЛК (embedded — body.embedded, ADR-092), И
 *   2. нода вернула GET /api/panel/ai-availability → {available:true, mode:"cloud",
 *      ai_origin}. Standalone-панель → available:false → виджета нет вовсе
 *      (fail-safe: браузер там не носит ЛК-сессию, cross-origin к ЛК не пройдёт).
 *
 * Транспорт — postMessage к РОДИТЕЛЮ-ЛК (окно, загрузившее iframe панели): нода в
 * тракте ИИ не участвует, CORS ЛК не открываем. Панель шлёт вопрос родителю
 * (window.parent, targetOrigin = ai_origin), страница-хост ЛК дёргает свои
 * same-origin эндпоинты /api/panel-ai/* под ЛК-сессией браузера и возвращает
 * ответ тем же postMessage. Ключ DeepSeek не покидает ЛК.
 *
 * Безопасность рендера (инвариант ADR-091): ответ модели НЕДОВЕРЕННЫЙ — сначала
 * escape, разметка (жирный/код/списки/nav) строится ПОВЕРХ уже экранированного
 * текста, поэтому сырой innerHTML модели в DOM не попадает. Вопрос пользователя —
 * через textContent. Маркеры [[nav:ключ|Подпись]] валидируются зеркальным
 * whitelist страниц панели; незнакомый ключ рендерится текстом.
 */
(function () {
  'use strict';

  // Зеркальный whitelist страниц панельного SPA (паритет с panel_knowledge.NAV_TARGETS
  // на бэке — тест паритета). Ключ = hash-навигация панели (dashboard/pipeline/ssl/
  // terminal/settings). Незнакомый nav-маркер станет обычным текстом.
  var NAV_TARGETS = {
    dashboard: 'Дашборд',
    pipeline: 'Конвейер',
    ssl: 'SSL',
    terminal: 'Терминал',
    settings: 'Настройки',
  };

  var MODE_KEY = 'panelAiMode';        // closed|corner|full — переживает перезагрузку вкладки
  var DRAFT_KEY = 'panelAiDraft';      // лёгкий client-side буфер черновика на время вкладки

  var aiOrigin = null;                 // origin ЛК (родитель iframe) — куда шлём postMessage
  var mode = 'closed';
  var loaded = false;                  // история уже дочитана
  var sending = false;
  var msgSeq = 0;
  var pending = {};                    // id → {resolve, reject, timer}

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function $(id) { return document.getElementById(id); }

  function isEmbedded() { return document.body.classList.contains('embedded'); }

  // --- Транспорт: запрос к странице-хосту ЛК через postMessage к родителю ----- //
  function callLk(action, payload) {
    return new Promise(function (resolve, reject) {
      if (!aiOrigin || window.parent === window) { reject(new Error('no-parent')); return; }
      var id = 'pai-' + (++msgSeq) + '-' + Date.now();
      var timer = setTimeout(function () {
        delete pending[id];
        reject(new Error('timeout'));
      }, 30000);
      pending[id] = { resolve: resolve, reject: reject, timer: timer };
      try {
        window.parent.postMessage(
          { type: 'panel-ai-request', id: id, action: action, payload: payload || {} },
          aiOrigin);
      } catch (e) {
        clearTimeout(timer);
        delete pending[id];
        reject(e);
      }
    });
  }

  // «Написать в поддержку»: поддержка/тикеты живут в мессенджере ЛК, а он в embed
  // спрятан. Шлём родителю-ЛК односторонний сигнал (ответа не ждём) на ТОТ ЖЕ
  // известный origin, что и запросы ИИ (не '*') — ЛК уйдёт с embed-вьюхи и откроет
  // мессенджер на экране поддержки. Никаких URL/данных не передаём: чистый триггер.
  function openSupport() {
    if (!aiOrigin || window.parent === window) return;
    try {
      window.parent.postMessage({ type: 'panel-support' }, aiOrigin);
    } catch (_) { /* родитель ушёл — молча */ }
  }

  window.addEventListener('message', function (event) {
    // Принимаем ТОЛЬКО ответы от известного origin ЛК-родителя (анти-спуфинг).
    if (!aiOrigin || event.origin !== aiOrigin) return;
    var d = event.data;
    if (!d || d.type !== 'panel-ai-response' || !d.id) return;
    var p = pending[d.id];
    if (!p) return;
    clearTimeout(p.timer);
    delete pending[d.id];
    // Форма ответа: {status:<http>, body:<json|null>} — как у fetch, но через ЛК-хост.
    p.resolve({ status: d.status, body: d.body });
  });

  // --- Рендер ответа: markdown-лайт ПОВЕРХ экранированного текста ------------- //
  function inline(s) {
    s = s.replace(/`([^`]+)`/g, function (m, c) { return '<code>' + c + '</code>'; });
    s = s.replace(/\*\*([^*]+)\*\*/g, function (m, c) { return '<strong>' + c + '</strong>'; });
    // [[nav:ключ|Подпись]] → кнопка перехода ТОЛЬКО по whitelist, иначе текст-подпись.
    s = s.replace(/\[\[nav:([a-z_]+)\|([^\]|]{1,80})\]\]/g, function (m, key, label) {
      return NAV_TARGETS[key]
        ? '<button type="button" class="pai-nav-btn" data-pai-nav="' + esc(key) + '">'
          + '<span class="material-symbols-outlined">arrow_forward</span>' + label + '</button>'
        : label;
    });
    return s;
  }
  function mdLite(text) {
    var lines = esc(text).split(/\r?\n/);
    var out = [];
    var list = null; // {type, items}
    function flush() {
      if (list) {
        out.push('<' + list.type + '>'
          + list.items.map(function (i) { return '<li>' + i + '</li>'; }).join('')
          + '</' + list.type + '>');
        list = null;
      }
    }
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].trim();
      var m;
      if ((m = line.match(/^[-*•]\s+(.+)$/))) {
        if (!list || list.type !== 'ul') { flush(); list = { type: 'ul', items: [] }; }
        list.items.push(inline(m[1]));
      } else if ((m = line.match(/^\d+[.)]\s+(.+)$/))) {
        if (!list || list.type !== 'ol') { flush(); list = { type: 'ol', items: [] }; }
        list.items.push(inline(m[1]));
      } else if (!line) {
        flush();
      } else if ((m = line.match(/^#{1,4}\s+(.+)$/))) {
        flush(); out.push('<p><strong>' + inline(m[1]) + '</strong></p>');
      } else {
        flush(); out.push('<p>' + inline(line) + '</p>');
      }
    }
    flush();
    return out.join('');
  }

  function build() {
    if ($('pai-root')) return;
    var root = document.createElement('div');
    root.id = 'pai-root';
    root.innerHTML =
      '<button id="paiFab" class="pai-fab" type="button" title="ИИ-помощник панели">'
      + '<span class="material-symbols-outlined">smart_toy</span></button>'
      + '<div id="paiPanel" class="pai-panel" hidden>'
      + '  <div class="pai-head">'
      + '    <span class="material-symbols-outlined pai-head-icon">smart_toy</span>'
      + '    <div class="pai-head-title"><strong>ИИ-помощник</strong>'
      + '      <span class="pai-head-sub">подскажет по этой панели</span></div>'
      + '    <button class="icon-btn" id="paiClearBtn" type="button" title="Очистить диалог" hidden>'
      + '      <span class="material-symbols-outlined">delete_sweep</span></button>'
      + '    <button class="icon-btn" id="paiSupportBtn" type="button" title="Написать в поддержку">'
      + '      <span class="material-symbols-outlined">support_agent</span></button>'
      + '    <button class="icon-btn" id="paiExpandBtn" type="button" title="На весь экран">'
      + '      <span class="material-symbols-outlined">open_in_full</span></button>'
      + '    <button class="icon-btn" id="paiCloseBtn" type="button" title="Свернуть">'
      + '      <span class="material-symbols-outlined">close</span></button>'
      + '  </div>'
      + '  <div class="pai-body" id="paiBody"></div>'
      + '  <form class="pai-input" id="paiForm">'
      + '    <textarea id="paiText" rows="1" maxlength="2000" '
      + '      placeholder="Например: как задеплоить приложение?"></textarea>'
      + '    <button type="submit" class="pai-send" id="paiSendBtn" title="Отправить">'
      + '      <span class="material-symbols-outlined">send</span></button>'
      + '  </form>'
      + '  <div class="pai-foot"><span>Отвечает ИИ — может ошибаться.</span>'
      + '    <span id="paiUsage"></span></div>'
      + '</div>';
    document.body.appendChild(root);

    $('paiFab').addEventListener('click', function () { setMode('corner'); });
    $('paiCloseBtn').addEventListener('click', function () { setMode('closed'); });
    $('paiExpandBtn').addEventListener('click', function () {
      setMode(mode === 'full' ? 'corner' : 'full');
    });
    $('paiClearBtn').addEventListener('click', clearDialog);
    $('paiSupportBtn').addEventListener('click', openSupport);
    $('paiForm').addEventListener('submit', function (e) { e.preventDefault(); send(); });

    // Резервное закрытие: Escape сворачивает открытый чат (страховка к крестику).
    $('paiPanel').addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && mode !== 'closed') { e.stopPropagation(); setMode('closed'); }
    });

    var ta = $('paiText');
    try { ta.value = sessionStorage.getItem(DRAFT_KEY) || ''; } catch (_) { /* ignore */ }
    ta.addEventListener('input', function () {
      ta.style.height = 'auto';
      ta.style.height = Math.min(ta.scrollHeight, 120) + 'px';
      try { sessionStorage.setItem(DRAFT_KEY, ta.value); } catch (_) { /* ignore */ }
    });
  }

  function setMode(m) {
    mode = m;
    try { localStorage.setItem(MODE_KEY, m); } catch (_) { /* ignore */ }
    var fab = $('paiFab'), panel = $('paiPanel');
    if (!fab || !panel) return;
    fab.hidden = m !== 'closed';
    panel.hidden = m === 'closed';
    panel.classList.toggle('full', m === 'full');
    var expIcon = $('paiExpandBtn').querySelector('.material-symbols-outlined');
    expIcon.textContent = m === 'full' ? 'close_fullscreen' : 'open_in_full';
    if (m !== 'closed' && !loaded) refresh();
    if (m !== 'closed') {
      scrollDown();
      var ta = $('paiText');
      if (ta && !ta.disabled) setTimeout(function () { ta.focus(); }, 50);
    }
  }

  function scrollDown() { var b = $('paiBody'); if (b) b.scrollTop = b.scrollHeight; }
  function setUsage(used, limit) {
    var el = $('paiUsage');
    if (el && limit) el.textContent = used + '/' + limit + ' за сегодня';
  }
  function setInputEnabled(on) {
    var ta = $('paiText'), btn = $('paiSendBtn');
    if (ta) ta.disabled = !on;
    if (btn) btn.disabled = !on;
  }

  function appendMsg(role, content, scroll) {
    var body = $('paiBody');
    if (!body) return null;
    var el = document.createElement('div');
    el.className = 'pai-msg ' + (role === 'user' ? 'user' : 'assistant');
    if (role === 'user') el.textContent = content;   // вопрос — как есть, без разметки
    else el.innerHTML = mdLite(content);             // ответ — markdown-лайт по escape-тексту
    body.appendChild(el);
    if (scroll !== false) scrollDown();
    return el;
  }
  function appendNote(text, isError) {
    var body = $('paiBody');
    if (!body) return;
    var el = document.createElement('div');
    el.className = 'pai-note' + (isError === false ? '' : ' err');
    el.textContent = text;
    body.appendChild(el);
    scrollDown();
  }
  function typingRow() {
    var body = $('paiBody');
    var el = document.createElement('div');
    el.className = 'pai-msg assistant pai-typing';
    el.innerHTML = '<span class="material-symbols-outlined pai-spin">progress_activity</span>'
      + 'Помощник печатает…';
    if (body) { body.appendChild(el); scrollDown(); }
    return el;
  }
  function hello() {
    var body = $('paiBody');
    if (!body) return;
    var qs = ['Как задеплоить приложение?', 'Как опубликовать сервис на домене?',
              'Как выпустить SSL-сертификат?', 'Где смотреть прогресс задач?'];
    var el = document.createElement('div');
    el.className = 'pai-msg assistant';
    el.innerHTML = '<p>Привет! Я помогу разобраться с этой панелью: как загрузить код, '
      + 'запустить сервис, опубликовать его на домене с HTTPS и следить за задачами — '
      + 'и покажу, куда перейти.</p><div class="pai-suggest">'
      + qs.map(function (q) {
        return '<button type="button" class="pai-chip" data-pai-ask="' + esc(q) + '">'
          + esc(q) + '</button>';
      }).join('') + '</div>';
    body.appendChild(el);
  }

  async function refresh() {
    var body = $('paiBody');
    if (!body) return;
    setInputEnabled(false);
    body.innerHTML = '<div class="pai-note"><span class="material-symbols-outlined pai-spin">'
      + 'progress_activity</span>Загрузка…</div>';
    var resp;
    try {
      resp = await callLk('history', {});
    } catch (_) {
      body.innerHTML = '';
      appendNote('Помощник недоступен — не удалось связаться с личным кабинетом.');
      return;
    }
    body.innerHTML = '';
    // 503 (ИИ выключен) — тихо прячем виджет: fail-safe, без пустых модалок.
    if (resp.status === 503) { setVisible(false); return; }
    // 403 (нет консента) — показываем экран согласия (тот же поток, что в ЛК).
    if (resp.status === 403) { consentGate(resp.body); return; }
    if (resp.status !== 200 || !resp.body) {
      appendNote('Не удалось загрузить историю — но спросить можно.', false);
    } else {
      var items = resp.body.items || [];
      if (!items.length) hello();
      items.forEach(function (m) { appendMsg(m.role, m.content, false); });
      setUsage(resp.body.used_today, resp.body.daily_limit);
    }
    loaded = true;
    setInputEnabled(true);
    scrollDown();
  }

  // Экран консента: сам акцепт живёт в ЛК (браузер с ЛК-сессией), поэтому кнопка
  // ведёт пользователя дать согласие в личном кабинете (там же, где ИИ-разбор/FAQ).
  function consentGate(body) {
    var host = $('paiBody');
    if (!host) return;
    var prov = (body && body.provider) || {};
    var name = prov.name || prov.key || 'сторонний ИИ-провайдер';
    var providerHtml = prov.url
      ? '<a href="' + esc(prov.url) + '" target="_blank" rel="noopener">' + esc(name) + '</a>'
      : esc(name);
    var el = document.createElement('div');
    el.className = 'pai-gate';
    el.innerHTML = '<span class="material-symbols-outlined">psychology</span>'
      + '<p style="margin:0;">Помощник отвечает с помощью сторонней ИИ-модели ' + providerHtml
      + ': ваши вопросы отправляются на серверы провайдера. Советы ИИ могут быть неточными — '
      + 'решения принимаете вы.</p>'
      + '<p style="margin:0;">Чтобы включить помощника, примите условия работы с ИИ в личном '
      + 'кабинете (раздел ИИ-помощника/разбора ошибок) — то же согласие действует и здесь.</p>';
    host.innerHTML = '';
    host.appendChild(el);
  }

  async function send(preset) {
    if (sending) return;
    var ta = $('paiText');
    var text = (preset != null ? preset : (ta ? ta.value : '')).trim();
    if (!text) return;
    sending = true;
    if (ta && preset == null) {
      ta.value = ''; ta.style.height = 'auto';
      try { sessionStorage.removeItem(DRAFT_KEY); } catch (_) { /* ignore */ }
    }
    $('paiSendBtn').disabled = true;
    appendMsg('user', text);
    var typing = typingRow();
    var resp = null;
    try {
      resp = await callLk('ask', { message: text });
    } catch (_) { /* ниже — честная заглушка */ }
    typing.remove();
    sending = false;
    $('paiSendBtn').disabled = false;
    if (!resp) { appendNote('Не удалось связаться с личным кабинетом — попробуйте ещё раз.'); return; }
    if (resp.status === 503) { appendNote('ИИ-помощник сейчас недоступен на платформе.'); return; }
    if (resp.status === 403) { consentGate(resp.body); return; }
    if (resp.status !== 200 || !resp.body) {
      var detail = resp.body && resp.body.detail;
      appendNote(detail || 'Не удалось получить ответ — попробуйте позже.');
      return;
    }
    appendMsg('assistant', (resp.body.reply && resp.body.reply.content) || '');
    setUsage(resp.body.used_today, resp.body.daily_limit);
    if (ta && !ta.disabled) ta.focus();
  }

  async function clearDialog() {
    if (!window.confirm('Очистить историю диалога с помощником?')) return;
    try {
      var resp = await callLk('clear', {});
      if (resp.status !== 200) throw new Error('clear-failed');
    } catch (e) { appendNote('Не удалось очистить.'); return; }
    loaded = false;
    refresh();
  }

  // Делегированные клики виджета: nav-переходы и быстрые вопросы.
  document.addEventListener('click', function (e) {
    var nav = e.target.closest ? e.target.closest('[data-pai-nav]') : null;
    if (nav && NAV_TARGETS[nav.dataset.paiNav]) {
      // Из полноэкрана — в уголок, чтобы страница была видна; чат не сбрасывается.
      if (mode === 'full') setMode('corner');
      // Hash-навигация панельного SPA (app.js слушает hashchange).
      window.location.hash = nav.dataset.paiNav;
      return;
    }
    var chip = e.target.closest ? e.target.closest('[data-pai-ask]') : null;
    if (chip) { send(chip.dataset.paiAsk); }
  });

  function setVisible(visible) {
    var root = $('pai-root');
    if (!visible) {
      if (root) root.remove();
      return;
    }
    build();
    var saved = null;
    try { saved = localStorage.getItem(MODE_KEY); } catch (_) { /* ignore */ }
    setMode(saved === 'corner' || saved === 'full' ? saved : 'closed');
  }

  // --- Гейт показа: embedded + ЛК-сессия + ai-availability ------------------- //
  // Вызывается из app.js ПОСЛЕ входа (showApp) — тогда есть токен панели, а флаг
  // embedded уже проставлен. Идемпотентно: повторный вызов не плодит виджет.
  async function init() {
    if ($('pai-root')) return;                       // уже смонтирован
    // Standalone-панель (не в iframe ЛК) — виджета нет вовсе (MVP, ADR-103).
    if (!isEmbedded() || window.parent === window) return;
    var avail = null;
    try {
      var token = localStorage.getItem('accessToken');
      var r = await fetch('/api/panel/ai-availability', {
        headers: token ? { Authorization: 'Bearer ' + token } : {},
      });
      if (r.ok) avail = await r.json();
    } catch (_) { /* нода недоступна → без виджета */ }
    if (!avail || !avail.available || avail.mode !== 'cloud' || !avail.ai_origin) return;
    aiOrigin = avail.ai_origin;
    setVisible(true);
  }

  // Выход/смена аккаунта: убираем виджет и сбрасываем состояние (приватность —
  // отрисованная история/черновик уходят; при следующем входе дочитаются с ЛК).
  function teardown() {
    setVisible(false);
    aiOrigin = null;
    loaded = false;
    sending = false;
  }

  // Экспорт для панельного app.js (монтаж после входа, демонтаж при выходе).
  window.PanelAI = { init: init, teardown: teardown };
})();
