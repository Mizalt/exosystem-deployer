/* --- panel_ai.js --- ИИ-помощник + техподдержка ПАНЕЛИ деплоера (ADR-103/123/125).
 *
 * Виджет рендерится ТОЛЬКО когда:
 *   1. панель открыта ВНУТРИ ЛК (embedded — body.embedded, ADR-092), И
 *   2. нода вернула GET /api/panel/ai-availability → {available:true, mode:"cloud",
 *      ai_origin}. Standalone-панель → available:false → виджета нет вовсе
 *      (fail-safe: браузер там не носит ЛК-сессию, cross-origin к ЛК не пройдёт).
 *
 * Транспорт — postMessage к РОДИТЕЛЮ-ЛК (окно, загрузившее iframe панели): нода в
 * тракте ИИ/поддержки не участвует, CORS ЛК не открываем. Панель шлёт запрос
 * родителю (window.parent, targetOrigin = ai_origin), страница-хост ЛК дёргает свои
 * same-origin эндпоинты (/api/panel-ai/* и /api/support/* под ЛК-сессией браузера)
 * и возвращает ответ тем же postMessage. Ключ DeepSeek и тикеты не покидают ЛК.
 *
 * ADR-123 (техпод ВНУТРИ панели): раньше «Написать в поддержку» слало сигнал
 * panel-support, и ЛК уничтожал embed (closePanelEmbed), выкидывая пользователя из
 * панели. Теперь поддержка (список тикетов / переписка / ответ / новое обращение)
 * работает ВНУТРИ этого виджета — экраны list/ticket/new рендерятся здесь, а
 * операции проксируются тем же мостом под ЛК-сессией. Пользователь НЕ покидает панель.
 *
 * ADR-125 (навигация-проводник, nav-only): маркеры [[nav:ключ|Подпись]] в ответе ИИ
 * ведут ТОЛЬКО к экрану. Ключи панели (dashboard/ssl/…) — hash-навигация SPA панели;
 * ключи ЛК (servers/billing/…) — сигнал panel-nav родителю, ЛК сам делает goToPage
 * по своему whitelist. Никаких произвольных URL/действий: ассистент лишь ПОДВОДИТ.
 *
 * Безопасность рендера (инвариант ADR-091): ответ модели НЕДОВЕРЕННЫЙ — сначала
 * escape, разметка (жирный/код/списки/nav) строится ПОВЕРХ уже экранированного
 * текста, поэтому сырой innerHTML модели в DOM не попадает. Вопрос пользователя —
 * через textContent. Маркеры [[nav:ключ|Подпись]] валидируются зеркальным
 * whitelist (панель + ЛК); незнакомый ключ рендерится текстом.
 */
(function () {
  'use strict';

  // Зеркальный whitelist ключей панельного SPA (паритет с NAV_TARGETS_PANEL на бэке).
  // Ключ = hash-навигация панели (dashboard/pipeline/ssl/terminal/settings).
  var NAV_PANEL = {
    dashboard: 'Дашборд',
    pipeline: 'Конвейер',
    ssl: 'SSL',
    terminal: 'Терминал',
    settings: 'Настройки',
  };
  // Зеркальный whitelist разделов ЛК (паритет с NAV_TARGETS_LK на бэке). Ведут в ЛК
  // через мост panel-nav → goToPage (кросс-хост, ADR-125). support — экран поддержки
  // ВНУТРИ панели (ADR-123), НЕ уходит в ЛК.
  var NAV_LK = {
    servers: 'Серверы',
    integrations: 'Интеграции',
    mcp: 'Агенты (MCP)',
    billing: 'Тариф',
    support: 'Поддержка',
    account: 'Профиль',
  };
  // Объединённый whitelist — для валидации маркеров (незнакомый ключ → текст).
  function navLabel(key) { return NAV_PANEL[key] || NAV_LK[key] || null; }

  var MODE_KEY = 'panelAiMode';        // closed|corner|full — переживает перезагрузку вкладки
  var DRAFT_KEY = 'panelAiDraft';      // лёгкий client-side буфер черновика на время вкладки

  var aiOrigin = null;                 // origin ЛК (родитель iframe) — куда шлём postMessage
  var mode = 'closed';
  var loaded = false;                  // история ИИ уже дочитана
  var sending = false;
  var msgSeq = 0;
  var pending = {};                    // id → {resolve, reject, timer}

  // Экраны виджета (ADR-123): ai (ИИ-диалог) | list (чаты) | ticket (переписка) | new.
  var screen = 'ai';
  var ticketId = null;                 // id открытого тикета (экран ticket)
  var ticketStatus = null;
  var ticketPollTimer = null;          // лёгкий поллинг открытого диалога (~12 c)
  var ticketLastSig = '';
  var ticketSending = false;
  var lastQuestion = '';               // последний вопрос ИИ — черновик темы тикета

  var STATUS_RU = { open: 'открыт', in_progress: 'в работе', closed: 'закрыт' };

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

  // Кросс-хост навигация-проводник (ADR-125): ключ раздела ЛК → сигнал родителю,
  // ЛК валидирует по СВОЕМУ whitelist и делает goToPage. Шлём только КЛЮЧ (не URL),
  // на известный origin ЛК (не '*'). Никаких действий/данных — чистый переход.
  function navToLk(key) {
    if (!aiOrigin || window.parent === window) return;
    try {
      window.parent.postMessage({ type: 'panel-nav', key: key }, aiOrigin);
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
      return navLabel(key)
        ? '<button type="button" class="pai-nav-btn" data-pai-nav="' + esc(key) + '">'
          + '<span class="material-symbols-outlined">arrow_forward</span>' + label + '</button>'
        : label;
    });
    return s;
  }
  // GFM pipe-таблица ПОВЕРХ экранированного текста: заголовок + строка-разделитель
  // |---|:---:| … Без строки-разделителя обычный текст с «|» таблицей НЕ считается.
  function tableSplitRow(line) {
    // «|» внутри [[nav:ключ|Подпись]] — часть маркера, а НЕ граница колонки:
    // прячем под сентинел до разбиения и возвращаем в ячейках (текст уже
    // экранирован esc(), сырой «<» в нём невозможен — коллизий нет).
    var s = line.trim().replace(/\[\[nav:([a-z_]+)\|/g, '[[nav:$1<np>');
    if (s.charAt(0) === '|') s = s.slice(1);
    if (s.charAt(s.length - 1) === '|') s = s.slice(0, -1);
    return s.split('|').map(function (c) { return c.trim().replace(/<np>/g, '|'); });
  }
  function tableAligns(line) {
    // null — не разделитель; иначе массив выравниваний колонок: '' | 'c' | 'r'.
    var s = (line || '').trim();
    if (s.indexOf('|') === -1 || s.indexOf('-') === -1) return null;
    var cells = tableSplitRow(s);
    var al = [];
    for (var i = 0; i < cells.length; i++) {
      if (!/^:?-+:?$/.test(cells[i])) return null;
      var l = cells[i].charAt(0) === ':';
      var r = cells[i].charAt(cells[i].length - 1) === ':';
      al.push(l && r ? 'c' : (r ? 'r' : ''));
    }
    return al.length ? al : null;
  }
  // Выравнивание — ТОЛЬКО фиксированные классы из закрытого набора (никаких
  // style-атрибутов со значениями из текста ответа).
  function tableAlignClass(a) {
    return a === 'c' ? ' class="md-al-c"' : (a === 'r' ? ' class="md-al-r"' : '');
  }
  function tableHtml(header, aligns, rows) {
    var html = '<div class="md-table-wrap"><table class="md-table"><thead><tr>';
    for (var j = 0; j < header.length; j++) {
      html += '<th' + tableAlignClass(aligns[j] || '') + '>' + inline(header[j]) + '</th>';
    }
    html += '</tr></thead><tbody>';
    for (var r = 0; r < rows.length; r++) {
      html += '<tr>';
      // Лишние ячейки строки обрезаются по заголовку, недостающие — пустые.
      for (var c = 0; c < header.length; c++) {
        html += '<td' + tableAlignClass(aligns[c] || '') + '>' + inline(rows[r][c] || '') + '</td>';
      }
      html += '</tr>';
    }
    return html + '</tbody></table></div>';
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
      var al = null;
      if (line.indexOf('|') !== -1 && i + 1 < lines.length
          && (al = tableAligns(lines[i + 1]))) {
        flush();
        var rows = [];
        i += 1; // строка-разделитель съедена
        while (i + 1 < lines.length && lines[i + 1].trim()
               && lines[i + 1].indexOf('|') !== -1) {
          rows.push(tableSplitRow(lines[i + 1]));
          i += 1;
        }
        out.push(tableHtml(tableSplitRow(line), al, rows));
      } else if ((m = line.match(/^[-*•]\s+(.+)$/))) {
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
      '<button id="paiFab" class="pai-fab" type="button" title="Помощник и поддержка">'
      + '<span class="material-symbols-outlined">forum</span></button>'
      + '<div id="paiPanel" class="pai-panel" hidden>'
      + '  <div class="pai-head">'
      + '    <button class="icon-btn" id="paiBackBtn" type="button" title="К чатам" hidden>'
      + '      <span class="material-symbols-outlined">arrow_back</span></button>'
      + '    <span class="material-symbols-outlined pai-head-icon" id="paiHeadIcon">forum</span>'
      + '    <div class="pai-head-title"><strong id="paiHeadTitle">Помощник</strong>'
      + '      <span class="pai-head-sub" id="paiHeadSub">подскажет по этой панели</span></div>'
      + '    <button class="icon-btn" id="paiResolveBtn" type="button" title="Вопрос решён — закрыть обращение" hidden>'
      + '      <span class="material-symbols-outlined">task_alt</span></button>'
      + '    <button class="icon-btn" id="paiClearBtn" type="button" title="Очистить диалог" hidden>'
      + '      <span class="material-symbols-outlined">delete_sweep</span></button>'
      + '    <button class="icon-btn" id="paiExpandBtn" type="button" title="На весь экран">'
      + '      <span class="material-symbols-outlined">open_in_full</span></button>'
      + '    <button class="icon-btn" id="paiCloseBtn" type="button" title="Свернуть">'
      + '      <span class="material-symbols-outlined">close</span></button>'
      + '  </div>'
      // Экран: список чатов (закреплённый ИИ + тикеты).
      + '  <div class="pai-screen" id="paiScreenList" hidden>'
      + '    <div class="pai-body pai-list" id="paiList"></div>'
      + '    <div class="pai-foot-btn">'
      + '      <button type="button" class="pai-block-btn" id="paiNewBtn">'
      + '        <span class="material-symbols-outlined">edit_square</span>Новое обращение</button>'
      + '    </div>'
      + '  </div>'
      // Экран: ИИ-диалог.
      + '  <div class="pai-screen" id="paiScreenAi" hidden>'
      + '    <div class="pai-body" id="paiBody"></div>'
      + '    <form class="pai-input" id="paiForm">'
      + '      <textarea id="paiText" rows="1" maxlength="2000" '
      + '        placeholder="Например: как задеплоить приложение?"></textarea>'
      + '      <button type="submit" class="pai-send" id="paiSendBtn" title="Отправить">'
      + '        <span class="material-symbols-outlined">send</span></button>'
      + '    </form>'
      + '    <div class="pai-foot"><span>Отвечает ИИ — может ошибаться.</span>'
      + '      <span id="paiUsage"></span></div>'
      + '  </div>'
      // Экран: переписка тикета.
      + '  <div class="pai-screen" id="paiScreenTicket" hidden>'
      + '    <div class="pai-body" id="paiTicketBody"></div>'
      + '    <form class="pai-input" id="paiTicketForm">'
      + '      <textarea id="paiTicketText" rows="1" maxlength="5000" '
      + '        placeholder="Сообщение в поддержку…"></textarea>'
      + '      <button type="submit" class="pai-send" id="paiTicketSendBtn" title="Отправить">'
      + '        <span class="material-symbols-outlined">send</span></button>'
      + '    </form>'
      + '    <div class="pai-closed-row" id="paiTicketClosedRow" hidden>'
      + '      <span class="material-symbols-outlined">lock</span><span>Обращение закрыто.</span>'
      + '      <button type="button" class="pai-nav-btn" id="paiTicketNewFromClosed">'
      + '        <span class="material-symbols-outlined">edit_square</span>Новое обращение</button>'
      + '    </div>'
      + '  </div>'
      // Экран: новое обращение.
      + '  <div class="pai-screen" id="paiScreenNew" hidden>'
      + '    <div class="pai-body pai-new-form">'
      + '      <p class="pai-note" style="text-align:left;">Опишите проблему — ответ '
      + '        оператора появится здесь же, в переписке.</p>'
      + '      <label class="pai-lbl">Тема</label>'
      + '      <input type="text" id="paiNewSubject" class="pai-inp" maxlength="200" '
      + '        placeholder="Кратко о проблеме">'
      + '      <label class="pai-lbl">Сообщение</label>'
      + '      <textarea id="paiNewText" class="pai-inp" rows="5" maxlength="5000" '
      + '        placeholder="Подробное описание, шаги воспроизведения…"></textarea>'
      + '      <div class="pai-note err" id="paiNewErr"></div>'
      + '      <div class="pai-new-actions">'
      + '        <button type="button" class="pai-block-btn" id="paiNewSendBtn">'
      + '          <span class="material-symbols-outlined">send</span>Отправить</button>'
      + '        <button type="button" class="pai-ghost-btn" id="paiNewCancelBtn">Отмена</button>'
      + '      </div>'
      + '    </div>'
      + '  </div>'
      + '</div>';
    document.body.appendChild(root);

    $('paiFab').addEventListener('click', function () { setMode('corner'); });
    $('paiCloseBtn').addEventListener('click', function () { setMode('closed'); });
    $('paiExpandBtn').addEventListener('click', function () {
      setMode(mode === 'full' ? 'corner' : 'full');
    });
    $('paiClearBtn').addEventListener('click', clearDialog);
    $('paiBackBtn').addEventListener('click', function () { setScreen('list'); });
    $('paiResolveBtn').addEventListener('click', resolveTicket);
    $('paiNewBtn').addEventListener('click', function () { setScreen('new'); });
    $('paiTicketNewFromClosed').addEventListener('click', function () { setScreen('new'); });
    $('paiNewSendBtn').addEventListener('click', createTicket);
    $('paiNewCancelBtn').addEventListener('click', function () { setScreen('list'); });
    $('paiForm').addEventListener('submit', function (e) { e.preventDefault(); send(); });
    $('paiTicketForm').addEventListener('submit', function (e) { e.preventDefault(); sendTicketReply(); });

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
    // Enter — отправить; авто-рост тикет-инпута.
    var tta = $('paiTicketText');
    tta.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendTicketReply(); }
    });
    tta.addEventListener('input', function () {
      tta.style.height = 'auto';
      tta.style.height = Math.min(tta.scrollHeight, 120) + 'px';
    });
    $('paiText').addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
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
    if (m !== 'closed') {
      if (screen === 'ai' && !loaded) refresh();
      else applyScreen();
    }
    if (m === 'closed') stopTicketPoll();
  }

  // --- Экранная машина виджета (ai | list | ticket | new) -------------------- //
  function setHead(icon, title, sub) {
    $('paiHeadIcon').textContent = icon;
    $('paiHeadTitle').textContent = title;
    $('paiHeadSub').textContent = sub;
  }

  function setScreen(s, tid) {
    screen = s;
    if (s === 'ticket') ticketId = tid || ticketId;
    applyScreen();
  }

  function applyScreen() {
    var map = { ai: 'paiScreenAi', list: 'paiScreenList', ticket: 'paiScreenTicket', new: 'paiScreenNew' };
    ['paiScreenAi', 'paiScreenList', 'paiScreenTicket', 'paiScreenNew'].forEach(function (id) {
      var el = $(id);
      if (el) el.hidden = id !== map[screen];
    });
    $('paiBackBtn').hidden = screen === 'ai';
    $('paiClearBtn').hidden = screen !== 'ai';
    var isTicket = screen === 'ticket';
    $('paiResolveBtn').hidden = !(isTicket && ticketStatus && ticketStatus !== 'closed');
    if (!isTicket) stopTicketPoll();
    if (screen === 'ai') {
      setHead('smart_toy', 'ИИ-помощник', 'подскажет и покажет, куда перейти');
      if (!loaded) refresh(); else scrollDown();
      var ta = $('paiText');
      if (ta && !ta.disabled) setTimeout(function () { ta.focus(); }, 50);
    } else if (screen === 'list') {
      setHead('forum', 'Сообщения', 'ИИ-помощник и поддержка');
      loadList();
    } else if (screen === 'ticket') {
      setHead('support_agent', 'Обращение', 'поддержка');
      openTicket(ticketId);
    } else if (screen === 'new') {
      setHead('edit_square', 'Новое обращение', 'поддержка ответит в этом чате');
      var subj = $('paiNewSubject');
      $('paiNewErr').textContent = '';
      if (subj && !subj.value && lastQuestion) subj.value = lastQuestion.slice(0, 200);
      setTimeout(function () { (subj.value ? $('paiNewText') : subj).focus(); }, 50);
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
  function noteInto(host, text) {
    if (!host) return;
    var el = document.createElement('div');
    el.className = 'pai-note err';
    el.textContent = text;
    host.appendChild(el);
    host.scrollTop = host.scrollHeight;
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
      + 'и покажу, куда перейти. Нужна помощь человека — «В поддержку» ниже.</p>'
      + '<div class="pai-suggest">'
      + qs.map(function (q) {
        return '<button type="button" class="pai-chip" data-pai-ask="' + esc(q) + '">'
          + esc(q) + '</button>';
      }).join('') + '</div>';
    body.appendChild(el);
  }

  // Мостик из ИИ-чата: под свежим ответом — «Не помогло? В поддержку».
  function showBridge() {
    var body = $('paiBody');
    if (!body) return;
    var old = body.querySelector('.pai-bridge');
    if (old) old.remove();
    var el = document.createElement('div');
    el.className = 'pai-bridge';
    el.innerHTML = '<span>Не помогло?</span>'
      + '<button type="button" class="pai-nav-btn" data-pai-support>'
      + '<span class="material-symbols-outlined">support_agent</span>В поддержку</button>';
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
    lastQuestion = text; // черновик темы для «Не помогло? → в поддержку»
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
    showBridge();
    scrollDown();
    setUsage(resp.body.used_today, resp.body.daily_limit);
    if (ta && !ta.disabled) ta.focus();
  }

  async function clearDialog() {
    // Кастомный confirm панели (sandbox без allow-modals глушит нативный confirm).
    var ask = window.panelConfirm || function (m) { return Promise.resolve(window.confirm(m)); };
    if (!await ask('Очистить историю диалога с помощником?')) return;
    try {
      var resp = await callLk('clear', {});
      if (resp.status !== 200) throw new Error('clear-failed');
    } catch (e) { appendNote('Не удалось очистить.'); return; }
    loaded = false;
    refresh();
  }

  // --- ТЕХПОДДЕРЖКА ВНУТРИ ПАНЕЛИ (ADR-123) ---------------------------------- //
  function ago(ts) {
    var d = new Date(ts);
    if (isNaN(d)) return '';
    var now = new Date();
    var same = d.toDateString() === now.toDateString();
    return same ? d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })
      : d.toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  }

  function chatRow(o) {
    var chip = o.status
      ? '<span class="pai-chat-status' + (o.status === 'closed' ? ' closed' : '') + '">'
        + esc(STATUS_RU[o.status] || o.status) + '</span>' : '';
    var side = o.badge
      ? '<span class="pai-badge">' + (o.badge > 99 ? '99+' : o.badge) + '</span>'
      : '<span class="material-symbols-outlined pai-chevron">chevron_right</span>';
    return '<div class="pai-chat' + (o.pinned ? ' pinned' : '') + '" ' + o.action + '>'
      + '<span class="pai-chat-icon"><span class="material-symbols-outlined">' + o.icon + '</span></span>'
      + '<div class="pai-chat-main">'
      + '<div class="pai-chat-title"><span class="pai-chat-name">' + esc(o.title) + '</span>' + chip + '</div>'
      + '<div class="pai-chat-prev">' + esc(o.preview) + '</div></div>'
      + '<div class="pai-chat-side">'
      + (o.time ? '<span class="pai-chat-time">' + esc(o.time) + '</span>' : '') + side + '</div></div>';
  }

  function ticketRow(t) {
    var last = t.last_message;
    var preview = last
      ? (last.author === 'operator' ? 'Поддержка: ' : 'Вы: ') + last.body
      : t.message;
    return chatRow({
      icon: 'support_agent', title: t.subject, preview: preview,
      time: ago(last ? last.created_at : t.created_at),
      badge: t.unread_count, status: t.status,
      action: 'data-pai-ticket="' + t.id + '"',
    });
  }

  async function loadList() {
    var host = $('paiList');
    if (!host) return;
    host.innerHTML = '<div class="pai-note"><span class="material-symbols-outlined pai-spin">'
      + 'progress_activity</span>Загрузка…</div>';
    var resp = null;
    try { resp = await callLk('support.list', {}); } catch (_) { /* ниже */ }
    if (screen !== 'list' || !document.body.contains(host)) return;
    var ai = chatRow({
      icon: 'smart_toy', title: 'ИИ-помощник',
      preview: 'мгновенные ответы: как задеплоить, привязать домен, выпустить SSL…',
      time: '', badge: 0, status: null, action: 'data-pai-ai', pinned: true,
    });
    if (!resp || resp.status !== 200 || !resp.body) {
      host.innerHTML = ai + '<div class="pai-note err">Не удалось загрузить обращения — попробуйте позже.</div>';
      return;
    }
    var tickets = resp.body.items || [];
    var open = tickets.filter(function (t) { return t.status !== 'closed'; });
    var closed = tickets.filter(function (t) { return t.status === 'closed'; });
    var archive = closed.length
      ? '<details class="pai-archive"><summary><span class="material-symbols-outlined">inventory_2</span>'
        + 'Архив — закрытые (' + closed.length + ')</summary>'
        + closed.map(ticketRow).join('') + '</details>' : '';
    var empty = tickets.length ? ''
      : '<div class="pai-list-empty">Обращений пока нет. Вопрос по платформе — спросите '
        + 'ИИ-помощника; нужна помощь человека — «Новое обращение».</div>';
    host.innerHTML = ai + open.map(ticketRow).join('') + empty + archive;
  }

  function stopTicketPoll() {
    if (ticketPollTimer) { clearTimeout(ticketPollTimer); ticketPollTimer = null; }
  }

  function threadSig(thread) {
    var items = thread.items || [];
    var last = items[items.length - 1];
    return (thread.ticket || {}).status + '|' + items.length + '|' + (last ? last.id : 0);
  }

  function renderThread(thread) {
    var body = $('paiTicketBody');
    if (!body) return;
    var t = thread.ticket || {};
    ticketStatus = t.status;
    setHead('support_agent', t.subject || 'Обращение', 'поддержка · ' + (STATUS_RU[t.status] || t.status));
    $('paiResolveBtn').hidden = !(ticketStatus && ticketStatus !== 'closed');
    $('paiTicketClosedRow').hidden = t.status !== 'closed';
    $('paiTicketForm').hidden = t.status === 'closed';
    body.innerHTML = (thread.items || []).map(function (m) {
      return '<div class="pai-msg pai-bubble ' + (m.author === 'user' ? 'user' : 'assistant') + '">'
        + (m.author === 'operator'
          ? '<div class="pai-op-label"><span class="material-symbols-outlined">support_agent</span>Поддержка</div>' : '')
        + '<div class="pai-bubble-text">' + esc(m.body) + '</div>'
        + '<div class="pai-time">' + esc(ago(m.created_at)) + '</div></div>';
    }).join('');
    body.scrollTop = body.scrollHeight;
  }

  async function openTicket(id) {
    if (!id) { setScreen('list'); return; }
    stopTicketPoll();
    var body = $('paiTicketBody');
    body.innerHTML = '<div class="pai-note"><span class="material-symbols-outlined pai-spin">'
      + 'progress_activity</span>Загрузка переписки…</div>';
    var resp = null;
    try { resp = await callLk('support.open', { ticket_id: id }); } catch (_) { /* сеть */ }
    if (screen !== 'ticket' || ticketId !== id) return; // ушли с экрана
    if (resp && resp.status === 404) { setScreen('list'); return; }
    if (!resp || resp.status !== 200 || !resp.body) {
      body.innerHTML = '<div class="pai-note err">Не удалось загрузить переписку — попробуйте ещё раз.</div>';
      return;
    }
    ticketLastSig = threadSig(resp.body);
    renderThread(resp.body);
    var ta = $('paiTicketText');
    if (ta && !$('paiTicketForm').hidden) setTimeout(function () { ta.focus(); }, 50);
    scheduleTicketPoll();
  }

  // Лёгкий поллинг ОТКРЫТОГО диалога (~12 c): свежие ответы оператора появляются сами.
  function scheduleTicketPoll() {
    stopTicketPoll();
    if (screen !== 'ticket' || mode === 'closed') return;
    ticketPollTimer = setTimeout(async function () {
      if (screen !== 'ticket' || mode === 'closed') return;
      if (!document.hidden && !ticketSending) {
        try {
          var resp = await callLk('support.open', { ticket_id: ticketId });
          if (resp && resp.status === 200 && resp.body && screen === 'ticket'
              && threadSig(resp.body) !== ticketLastSig) {
            ticketLastSig = threadSig(resp.body);
            renderThread(resp.body);
          }
        } catch (_) { /* следующий тик */ }
      }
      scheduleTicketPoll();
    }, 12000);
  }

  async function sendTicketReply() {
    if (ticketSending || !ticketId) return;
    var ta = $('paiTicketText');
    var text = (ta ? ta.value : '').trim();
    if (!text) return;
    ticketSending = true;
    $('paiTicketSendBtn').disabled = true;
    var resp = null;
    try {
      resp = await callLk('support.reply', { ticket_id: ticketId, message: text });
    } catch (_) { /* сеть */ }
    ticketSending = false;
    $('paiTicketSendBtn').disabled = false;
    var body = $('paiTicketBody');
    if (!resp) { noteInto(body, 'Сеть недоступна — попробуйте ещё раз.'); return; }
    // Реплика в тикет — 201 Created (см. POST /api/support/{id}/messages).
    if (resp.status !== 201 || !resp.body) {
      var d = resp.body || {};
      noteInto(body, d.detail || 'Не удалось отправить сообщение.');
      if (resp.status === 409) openTicket(ticketId); // тикет закрыли — честный вид
      return;
    }
    ta.value = ''; ta.style.height = 'auto';
    var el = document.createElement('div');
    el.className = 'pai-msg pai-bubble user';
    el.innerHTML = '<div class="pai-bubble-text">' + esc(resp.body.body || text) + '</div>'
      + '<div class="pai-time">' + esc(ago(resp.body.created_at || new Date().toISOString())) + '</div>';
    body.appendChild(el);
    body.scrollTop = body.scrollHeight;
    ticketLastSig = ''; // следующий поллинг заберёт каноничный тред
    ta.focus();
  }

  async function resolveTicket() {
    var ask = window.panelConfirm || function (m) { return Promise.resolve(window.confirm(m)); };
    if (!ticketId || !await ask('Закрыть обращение? Продолжить переписку в нём будет нельзя.')) return;
    try {
      var resp = await callLk('support.close', { ticket_id: ticketId });
      if (resp.status !== 200) throw new Error('close-failed');
    } catch (e) { noteInto($('paiTicketBody'), 'Не удалось закрыть.'); return; }
    openTicket(ticketId);
  }

  async function createTicket() {
    var subj = $('paiNewSubject'), text = $('paiNewText'), err = $('paiNewErr');
    err.textContent = '';
    var btn = $('paiNewSendBtn');
    btn.disabled = true;
    try {
      var resp = await callLk('support.create',
        { subject: (subj.value || '').trim(), message: (text.value || '').trim() });
      if (!resp || resp.status !== 201 || !resp.body) {
        throw new Error((resp && resp.body && resp.body.detail) || 'Не удалось отправить обращение.');
      }
      subj.value = ''; text.value = ''; lastQuestion = '';
      setScreen('ticket', resp.body.id); // сразу в диалог — ответ придёт сюда же
    } catch (e) { err.textContent = e.message; }
    finally { btn.disabled = false; }
  }

  // Делегированные клики виджета: nav-переходы, быстрые вопросы, чаты, мостик.
  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;
    var nav = e.target.closest('[data-pai-nav]');
    if (nav) {
      var key = nav.dataset.paiNav;
      if (!navLabel(key)) return;                 // незнакомый ключ — игнор (nav-only)
      if (NAV_PANEL[key]) {
        // Раздел ПАНЕЛИ: из полноэкрана в уголок, затем hash-навигация SPA.
        if (mode === 'full') setMode('corner');
        window.location.hash = key;
      } else if (key === 'support') {
        setScreen('list');                        // поддержка ВНУТРИ панели (ADR-123)
      } else {
        navToLk(key);                             // раздел ЛК: кросс-хост проводник (ADR-125)
      }
      return;
    }
    if (e.target.closest('[data-pai-support]')) { setScreen('new'); return; }
    if (e.target.closest('[data-pai-ai]')) { setScreen('ai'); return; }
    var ticket = e.target.closest('[data-pai-ticket]');
    if (ticket) { setScreen('ticket', parseInt(ticket.dataset.paiTicket, 10)); return; }
    var chip = e.target.closest('[data-pai-ask]');
    if (chip) { setScreen('ai'); send(chip.dataset.paiAsk); }
  });

  function setVisible(visible) {
    var root = $('pai-root');
    if (!visible) {
      stopTicketPoll();
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
    screen = 'ai';
    ticketId = null;
    ticketStatus = null;
    ticketLastSig = '';
    lastQuestion = '';
  }

  // Экспорт для панельного app.js (монтаж после входа, демонтаж при выходе).
  window.PanelAI = { init: init, teardown: teardown };
})();
