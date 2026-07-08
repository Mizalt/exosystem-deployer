/* --- app.js --- */
/* Интегрирована система аутентификации JWT */

document.addEventListener('DOMContentLoaded', () => {
    // --- DOM Элементы ---
    const mainContent = document.getElementById('main-content');
    const loginScreen = document.getElementById('login-screen');
    const appContainer = document.getElementById('app-container');
    const loginForm = document.getElementById('loginForm');
    const logoutBtn = document.getElementById('logoutBtn');

    // --- Кэш и Шаблоны ---
    let CACHE = { groups: null, blueprints: null, services: null, applications: null, certs: null, systemMetrics: null, githubStatus: null, githubRepos: null };
    const templates = {
        dashboard: `<div class="page-content"><p style="color:var(--text-secondary)">Загрузка дашборда…</p></div>`,
        pipeline: `<div id="pipelineRailHost"></div><div id="stageContent"></div>`,
        blueprints: `
            <div class="stage-toolbar">
                <div class="stage-toolbar-text"><h2>Библиотека приложений</h2><span>Загруженные приложения и их версии (код)</span></div>
                <button class="btn btn-primary" id="newBlueprintBtn"><span class="material-symbols-outlined">add</span>Создать приложение</button>
            </div>
            <div class="page-content"><div id="blueprintsList" class="library-grid"><p>Загрузка...</p></div></div>`,
        services: `
            <div class="stage-toolbar">
                <div class="stage-toolbar-text"><h2>Сервисы</h2><span>Запущенные контейнеры (внутренний порт)</span></div>
                <button class="btn btn-primary" id="newServiceBtn"><span class="material-symbols-outlined">bolt</span>Запустить сервис</button>
            </div>
            <div class="content-area">
                <div class="services-list" id="servicesContainer"><div class="section-title">Загрузка...</div></div>
            </div>`,
        applications: `
            <div class="stage-toolbar">
                <div class="stage-toolbar-text"><h2>Приложения</h2><span>Публичные домены (точки входа)</span></div>
                <button class="btn btn-primary" id="newAppBtn"><span class="material-symbols-outlined">public</span>Опубликовать сервис</button>
            </div>
            <div class="page-content"><div class="settings-card"><table class="styled-table" id="appsTable"><thead><tr><th>Имя приложения</th><th>Домен</th><th>Указывает на сервис</th><th>SSL</th><th style="text-align:right;">Действия</th></tr></thead><tbody><tr><td colspan="5" style="text-align:center; color: var(--text-secondary);">Загрузка...</td></tr></tbody></table></div></div>`,
        settings: `
            <header><h1>Настройки</h1></header>
            <div class="page-content">
                <div class="tab-bar">
                    <button class="tab active" data-tab="panel"><span class="material-symbols-outlined">tune</span>Панель</button>
                    <button class="tab" data-tab="ports"><span class="material-symbols-outlined">lan</span>Группы портов</button>
                    <button class="tab" data-tab="ssl"><span class="material-symbols-outlined">shield_lock</span>SSL</button>
                    <button class="tab" data-tab="integrations"><span class="material-symbols-outlined">hub</span>Интеграции</button>
                </div>
                <div class="tab-panel active" data-panel="panel">
                    <div id="firstAccessBanner"></div>
                    <div class="settings-card" style="max-width:560px;">
                        <h3>Домен панели (Deployer)</h3>
                        <p class="card-hint">Основной адрес, по которому вы заходите в эту панель.</p>
                        <form id="panelSettingsForm">
                            <div class="form-group"><label>Домен панели</label><input type="text" name="domain" placeholder="panel.example.com" autocomplete="off"><div class="dns-inline" data-dns-for="panelDomain"></div></div>
                            <div class="form-group">
                                <label>SSL / HTTPS</label>
                                <select name="ssl_mode" id="panelSslMode">
                                    <option value="none">Без SSL (HTTP)</option>
                                    <option value="issue">Выпустить сертификат сейчас (Let's Encrypt)</option>
                                    <option value="existing">Выбрать существующий сертификат</option>
                                </select>
                            </div>
                            <div class="form-group" id="panelSslExistingGroup" style="display:none;">
                                <label>Сертификат</label>
                                <select name="ssl_cert_name"><option value="">—</option></select>
                            </div>
                            <div id="panelSslIssueHint" style="display:none; margin-bottom:16px;">
                                <div id="panelDnsStatus" style="font-size:13px; color:var(--text-secondary);">Введите домен — проверим DNS перед выпуском.</div>
                                <div class="log-window" id="panelSslLogWindow" style="height:160px; display:none; margin-top:12px;"></div>
                            </div>
                            <button type="submit" class="btn btn-primary">Применить</button>
                        </form>
                    </div>
                </div>
                <div class="tab-panel" data-panel="ports">
                    <div class="page-grid">
                        <div class="settings-card">
                            <h3>Группы портов</h3><p class="card-hint">Группы определяют диапазоны портов для новых сервисов.</p><table class="styled-table" id="groupsTable"><thead><tr><th>Имя</th><th>Диапазон портов</th><th style="text-align:right;">Действия</th></tr></thead><tbody><tr><td colspan="3" style="text-align:center; color:var(--text-secondary)">Загрузка...</td></tr></tbody></table>
                        </div>
                        <div class="settings-card">
                            <h3>Создать новую группу</h3><form id="createGroupForm"><div class="form-group"><label>Имя группы</label><input type="text" name="name" placeholder="backend-services" required></div><div class="form-group"><label>Начальный порт</label><input type="number" name="start_port" placeholder="9001" required></div><div class="form-group"><label>Конечный порт</label><input type="number" name="end_port" placeholder="9010" required></div><button type="submit" class="btn btn-primary" style="margin-top: 8px;">Создать</button></form>
                        </div>
                    </div>
                </div>
                <div class="tab-panel" data-panel="ssl">
                    <p class="card-hint" style="max-width:640px;">SSL-сертификаты настраиваются редко (обычно один раз при подключении домена), поэтому раздел живёт здесь, в «Настройках».</p>
                    <div class="tab-bar">
                        <button class="tab active" data-tab="certs"><span class="material-symbols-outlined">verified</span>Сертификаты</button>
                        <button class="tab" data-tab="issue"><span class="material-symbols-outlined">add_moderator</span>Выпустить</button>
                        <button class="tab" data-tab="upload"><span class="material-symbols-outlined">upload</span>Загрузить свой</button>
                    </div>
                    <div class="tab-panel active" data-panel="certs">
                        <div class="settings-card">
                            <p class="card-hint">Let's Encrypt-сертификаты продлеваются автоматически за ~30 дней до истечения. Если продлить не удаётся, за 14 дней появится алерт в центре задач.</p>
                            <table class="styled-table" id="certsTable"><thead><tr><th>Имя (каталог)</th><th>Домен (CN)</th><th>Действителен до</th><th style="text-align:right;">Действия</th></tr></thead><tbody><tr><td colspan="4" style="text-align: center; color: var(--text-secondary);">Загрузка...</td></tr></tbody></table>
                        </div>
                    </div>
                    <div class="tab-panel" data-panel="issue">
                        <div class="settings-card" style="max-width:560px;">
                            <h3>Выпустить Let's Encrypt</h3>
                            <p class="card-hint">A-запись домена должна указывать на этот сервер. Проверка идёт автоматически при вводе.</p>
                            <form id="issueSslForm">
                                <div class="form-group"><label>Домен</label><input type="text" name="domain" placeholder="example.com" required autocomplete="off"><div class="dns-inline" data-dns-for="sslDomain"></div></div>
                                <button type="submit" class="btn btn-primary deploy-action"><span class="material-symbols-outlined">add_moderator</span>Выпустить</button>
                            </form>
                            <div class="log-window" id="sslLogWindow" style="height: 250px; display: none; margin-top: 16px;"></div>
                        </div>
                    </div>
                    <div class="tab-panel" data-panel="upload">
                        <div class="settings-card" style="max-width:560px;">
                            <h3>Загрузить свой сертификат</h3>
                            <form id="uploadCertForm">
                                <div class="form-group"><label>Имя для хранения</label><input type="text" name="name" placeholder="my-custom-cert" required pattern="^[a-zA-Z0-9._\\-]+$"></div>
                                <div class="form-group"><label>Файл сертификата (fullchain.pem)</label><input type="file" name="cert_file" required accept=".pem,.crt"></div>
                                <div class="form-group"><label>Приватный ключ (privkey.pem)</label><input type="file" name="key_file" required accept=".pem,.key"></div>
                                <button type="submit" class="btn btn-primary"><span class="material-symbols-outlined">upload</span>Загрузить</button>
                            </form>
                        </div>
                    </div>
                </div>
                <div class="tab-panel" data-panel="integrations">
                    <div class="settings-card" style="max-width:560px;">
                        <h3><span class="material-symbols-outlined" style="vertical-align:middle;">code</span> GitHub</h3>
                        <p class="card-hint">Подключите аккаунт, чтобы импортировать версии из приватных
                            репозиториев и выбирать репозиторий из списка при добавлении версии.</p>
                        <div id="githubStatus" style="margin-bottom:14px; color:var(--text-secondary)">Загрузка…</div>
                        <form id="githubConnectForm">
                            <div class="form-group"><label>Personal Access Token</label>
                                <input type="password" name="token" placeholder="ghp_…" autocomplete="off"></div>
                            <button type="submit" class="btn btn-primary"><span class="material-symbols-outlined">link</span>Подключить</button>
                            <button type="button" class="btn btn-danger" id="githubDisconnectBtn" style="display:none;"><span class="material-symbols-outlined">link_off</span>Отключить</button>
                        </form>
                        <p class="card-hint">Токен — fine-grained PAT с доступом на нужные репозитории
                            (Contents: Read). Хранится зашифрованным, никогда не показывается полностью.</p>
                    </div>
                </div>
            </div>`,
        terminal: `
            <header><h1>Терминал</h1></header>
            <div class="page-content">
                <div class="settings-card" style="max-width:920px;">
                    <div class="terminal-warn">
                        <span class="material-symbols-outlined">warning</span>
                        <div>
                            <strong>Терминал для знатоков.</strong> Команды выполняются прямо
                            на этом сервере с правами панели. Одна команда — один вывод (не
                            интерактивная сессия): <code>df -h</code>, <code>docker ps</code>,
                            <code>free -m</code>. Есть таймаут и лимит вывода; каждая команда
                            пишется в журнал. Неверная команда может навредить серверу.
                        </div>
                    </div>
                    <label class="terminal-ack">
                        <input type="checkbox" id="terminalAck">
                        Понимаю риск — включить ввод команд
                    </label>
                    <div id="terminalWindow" class="log-window" style="height:380px; display:none;"></div>
                    <form id="terminalForm" style="display:none; margin-top:12px; gap:10px; align-items:stretch;">
                        <span class="terminal-prompt">$</span>
                        <input type="text" id="terminalInput" class="terminal-input" autocomplete="off"
                               spellcheck="false" placeholder="команда, напр. df -h  (↑/↓ — история)" disabled>
                        <button type="submit" class="btn btn-primary" id="terminalRun" disabled>
                            <span class="material-symbols-outlined">play_arrow</span>Выполнить</button>
                    </form>
                </div>
            </div>`,
    };

    // --- Логика Аутентификации ---
    const getToken = () => localStorage.getItem('accessToken');
    const setToken = (token) => localStorage.setItem('accessToken', token);
    const clearToken = () => localStorage.removeItem('accessToken');

    const showLoginScreen = () => {
        appContainer.style.display = 'none';
        loginScreen.style.display = 'flex';
    };

    const showApp = () => {
        loginScreen.style.display = 'none';
        appContainer.style.display = 'flex';
        wireModalForms();
        const initialPage = window.location.hash.substring(1) || 'services';
        if (!window.location.hash) window.location.replace('#' + initialPage);
        navigate(initialPage);
        startPolling();  // live-обновление статусов/логов
        initTaskCenter();  // центр фоновых задач (Ночь 10, ADR-069)
        // ИИ-помощник панели (ADR-103): монтируется САМ только в embedded-режиме
        // (панель внутри ЛК) И когда нода отдала ai-availability=true; иначе no-op.
        if (window.PanelAI && window.PanelAI.init) window.PanelAI.init();
    };

    // Обработчики submit модалок раньше навешивались только в init-функциях стадий;
    // боковая панель дашборда открывает те же модалки откуда угодно, поэтому
    // навешиваем их один раз глобально (идемпотентно через .onsubmit).
    function wireModalForms() {
        const set = (id, fn) => { const el = document.getElementById(id); if (el) el.onsubmit = fn; };
        set('serviceForm', handleCreateService);
        set('redeployForm', handleRedeployService);
        set('configForm', handleServiceConfig);
        set('applicationForm', handleCreateApplication);
        set('editApplicationForm', handleEditApplication);
        set('blueprintForm', handleBlueprintSubmit);
        const up = document.getElementById('uploadArtifactForm');
        if (up) { up.onsubmit = handleUploadArtifact; up.elements.zip_file.onchange = handleArtifactFileSelected; wireUploadSourceTabs(); }
    }

    loginForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const btn = loginForm.querySelector('button[type="submit"]');
        const errorDiv = document.getElementById('loginError');
        errorDiv.textContent = '';
        btn.disabled = true;
        btn.textContent = 'Вход...';

        const formData = new FormData();
        formData.append('username', loginForm.elements.username.value);
        formData.append('password', loginForm.elements.password.value);

        try {
            const response = await fetch('/api/auth/token', {
                method: 'POST',
                body: new URLSearchParams(formData)
            });
            if (!response.ok) {
                const err = await response.json();
                throw new Error(err.detail || 'Ошибка входа');
            }
            const data = await response.json();
            setToken(data.access_token);
            showApp();
        } catch (err) {
            errorDiv.textContent = err.message;
        } finally {
            btn.disabled = false;
            btn.textContent = 'Войти';
        }
    });

    logoutBtn.addEventListener('click', () => {
        stopPolling();
        clearToken();
        invalidateCache('groups', 'blueprints', 'services', 'applications', 'certs');
        teardownTaskCenter();
        if (window.PanelAI && window.PanelAI.teardown) window.PanelAI.teardown();
        showLoginScreen();
    });

    // --- Конвейер (визуальный степпер над контентом стадий) ---
    const PIPELINE_STAGES = [
        { page: 'blueprints', num: 1, label: 'Библиотека', cacheKey: 'blueprints' },
        { page: 'services', num: 2, label: 'Сервисы', cacheKey: 'services' },
        { page: 'applications', num: 3, label: 'Приложения', cacheKey: 'applications' },
    ];
    const stageCount = key => (Array.isArray(CACHE[key]) ? CACHE[key].length : '·');
    const railHTML = (activePage) => {
        const stages = PIPELINE_STAGES.map(s => `
            <div class="pipeline-stage ${s.page === activePage ? 'active' : ''}" data-stage="${s.page}">
                <span class="stage-label">${s.label}</span>
                <span class="stage-count" data-count-for="${s.cacheKey}">${stageCount(s.cacheKey)}</span>
            </div>`).join('<span class="pipeline-connector"><span class="material-symbols-outlined">arrow_forward</span></span>');
        return `<div class="pipeline-rail">${stages}</div>`;
    };
    // Обновляет счётчик стадии в шапке конвейера (вызывается после загрузки данных).
    const updateRailCount = (cacheKey) => {
        const el = document.querySelector(`[data-count-for="${cacheKey}"]`);
        if (el) el.textContent = stageCount(cacheKey);
    };
    // Заполняет ВСЕ счётчики конвейера сразу при открытии (не только активной стадии),
    // иначе соседние показывают «·» пока в них не перейдёшь. Данные кэшируются — дёшево.
    const refreshAllRailCounts = () => {
        PIPELINE_STAGES.forEach(async s => {
            try { await fetchData(s.cacheKey, `/api/${s.cacheKey}`); updateRailCount(s.cacheKey); } catch (_) { /* тихо */ }
        });
    };

    let currentStage = 'blueprints';
    // Лениво: функции-инициализаторы объявлены ниже (const), резолвим в момент вызова,
    // а не при инициализации (иначе TDZ — обращение до объявления).
    const stageInit = (stage) => ({ blueprints: initBlueprintsPage, services: initServicesPage, applications: initApplicationsPage }[stage]);

    // Рендер единой страницы «Конвейер»: степпер сверху + контент текущей стадии.
    // pickDefault=true (открытие «Конвейера» из меню, а не переход на конкретную стадию):
    // если есть опубликованные приложения — открываем сразу «Приложения» (обычно
    // пользователь идёт смотреть/управлять именно ими), иначе — начало «Библиотека».
    const renderPipeline = async (pickDefault) => {
        mainContent.innerHTML = templates.pipeline;
        if (pickDefault) {
            try {
                const apps = await fetchData('applications', '/api/applications');
                currentStage = (Array.isArray(apps) && apps.length) ? 'applications' : 'blueprints';
            } catch (_) { /* тихо — оставляем текущую стадию */ }
        }
        document.getElementById('pipelineRailHost').innerHTML = railHTML(currentStage);
        mainContent.querySelectorAll('.pipeline-stage').forEach(stage => stage.onclick = () => switchStage(stage.dataset.stage));
        switchStage(currentStage);
        refreshAllRailCounts();  // заполнить счётчики всех стадий сразу, а не только активной
    };
    const switchStage = (stage) => {
        currentStage = stage;
        mainContent.querySelectorAll('.pipeline-stage').forEach(s => s.classList.toggle('active', s.dataset.stage === stage));
        document.getElementById('stageContent').innerHTML = templates[stage] || '';
        const init = stageInit(stage);
        if (init) init();
    };
    // Переход на стадию конвейера с опциональным действием после рендера (напр. открыть модалку).
    const goToStage = (stage, cb) => {
        if (document.getElementById('stageContent')) switchStage(stage);
        else { currentStage = stage; renderPipeline(); }
        if (cb) setTimeout(cb, 50);
    };

    // --- Роутер ---
    const navigate = (page) => {
        // Обратная совместимость: SSL переехал из отдельного раздела во вкладку
        // «Настройки» (меню воронкой — SSL настраивается редко). Старый deep-link
        // #ssl открывает Настройки и активирует вкладку SSL.
        let openSslTab = false;
        if (page === 'ssl') { page = 'settings'; openSslTab = true; }
        // Прямые хэши стадий (#services и т.п.) открывают конвейер на нужной стадии
        // (обратная совместимость + переходы «Запустить»/«Опубликовать»).
        let freshPipeline = false;
        if (PIPELINE_STAGES.some(s => s.page === page)) { currentStage = page; page = 'pipeline'; }
        else if (page === 'pipeline') { freshPipeline = true; }  // клик по «Конвейер» в меню → умный дефолт стадии
        if (page === 'pipeline') {
            renderPipeline(freshPipeline);
        } else {
            mainContent.innerHTML = templates[page] || `<p>Страница не найдена</p>`;
            enhancePasswordInputs(mainContent);  // «глаз» у паролей страницы (GitHub PAT и др.)
            const initFunctions = { dashboard: initDashboardPage, settings: initSettingsPage, terminal: initTerminalPage };
            if (initFunctions[page]) initFunctions[page]();
            if (openSslTab) activateSettingsTab('ssl');  // deep-link #ssl → вкладка SSL в Настройках
        }
        document.querySelectorAll('.nav-item').forEach(link => link.classList.toggle('active', link.dataset.page === page));
    };

    // --- Дашборд (настраиваемые пресеты + системные метрики) ---
    // Реестр видов: добавление нового пресета = добавление ключа с render-функцией.
    const DASHBOARD_VIEWS = {
        graph:   { label: 'Граф',    icon: 'account_tree', render: renderGraphView },
        columns: { label: 'Колонки', icon: 'view_column',  render: renderColumnsView },
    };
    const getDashboardView = () => { const v = localStorage.getItem('dashboardView'); return DASHBOARD_VIEWS[v] ? v : 'graph'; };

    function initDashboardPage() { renderDashboard(); }

    async function renderDashboard() {
        const view = getDashboardView();
        mainContent.innerHTML = `
            <header>
                <h1>Дашборд</h1>
                <div class="action-row">
                    <div class="view-switcher" id="dashViewSwitcher">
                        ${Object.entries(DASHBOARD_VIEWS).map(([k, v]) => `<button class="view-option ${k === view ? 'active' : ''}" data-view="${k}" title="${v.label}"><span class="material-symbols-outlined">${v.icon}</span>${v.label}</button>`).join('')}
                    </div>
                    <button class="btn-icon-label" id="dashRefreshBtn"><span class="material-symbols-outlined">refresh</span>Обновить</button>
                </div>
            </header>
            <div class="page-content dashboard-content">
                <div class="dash-section-label"><span class="material-symbols-outlined">monitor_heart</span>Сервер <span class="dash-section-hint">динамика за 24 часа</span></div>
                <div id="hostHealthWrap">${hostHealthSkeletonHTML()}</div>
                <div class="dash-section-label"><span class="material-symbols-outlined">deployed_code</span>Сервисы и Docker</div>
                <div class="metrics-strip" id="metricsStrip">${metricsSkeletonHTML()}</div>
                <div class="dashboard-view-host" id="dashViewHost"><p style="color:var(--text-secondary)">Загрузка…</p></div>
            </div>`;
        document.getElementById('dashViewSwitcher').querySelectorAll('.view-option').forEach(btn => btn.onclick = () => { localStorage.setItem('dashboardView', btn.dataset.view); renderDashboard(); });
        document.getElementById('dashRefreshBtn').onclick = () => { invalidateCache('blueprints', 'services', 'applications', 'systemMetrics', 'hostHealth', 'metricsHistory'); renderDashboard(); };
        loadDashboardMetrics();
        loadHostHealth();
        const host = document.getElementById('dashViewHost');
        try { await DASHBOARD_VIEWS[view].render(host); }
        catch (e) { if (getToken()) host.innerHTML = `<p style="color:var(--danger)">Ошибка загрузки дашборда.</p>`; }
    }

    // Полоса метрик: карточки присутствуют ВСЕГДА (фикс. сетка). Скелетон показывается
    // до прихода данных, затем значения подставляются в уже отрисованные блоки — поэтому
    // верстка не «прыгает» (не появляется/не меняет размер при загрузке).
    // Ночь 19: карточка «Хост» убрана — хост-метрики живут в секции «Сервер» (графики),
    // дублирование делало дашборд наляпистым.
    const METRIC_LABELS = ['Сервисы', 'CPU сервисов', 'RAM сервисов', 'Сеть сервисов', 'Диск Docker'];
    const metricCardHTML = (label, value, sub, accent) =>
        `<div class="metric-card${accent ? ' ' + accent : ''}"><div class="metric-label">${label}</div><div class="metric-value">${value}</div><div class="metric-sub">${sub}</div></div>`;
    function metricsSkeletonHTML() {
        return METRIC_LABELS.map(label =>
            `<div class="metric-card loading"><div class="metric-label">${label}</div><div class="metric-value"><span class="skel skel-val"></span></div><div class="metric-sub"><span class="skel skel-sub"></span></div></div>`
        ).join('');
    }

    async function loadDashboardMetrics() {
        const strip = document.getElementById('metricsStrip');
        if (!strip) return;
        try {
            // Метрики подаём с точки зрения объектов деплоера (сервисов), а не хоста:
            // host-счётчики Docker (все контейнеры/образы машины) путают пользователя.
            const [m, services] = await Promise.all([
                fetchData('systemMetrics', '/api/system/metrics'),
                fetchData('services', '/api/services'),
            ]);
            const host = m.host || {}, disk = m.disk || {}, load = m.load || {};
            const online = services.filter(s => s.status === 'online').length;
            const failed = services.filter(s => s.status === 'failed').length;
            const fmtMb = v => v == null ? '—' : (v >= 1024 ? `${(v / 1024).toFixed(1)} GB` : `${v} MB`);
            strip.innerHTML =
                metricCardHTML('Сервисы', `${online} online`, `${failed} failed · ${services.length} всего`, failed ? 'warn' : '') +
                metricCardHTML('CPU сервисов', `${load.cpu_percent ?? 0}%`, `${load.managed_running ?? 0} запущено`) +
                metricCardHTML('RAM сервисов', fmtMb(load.memory_usage_mb), `из ${fmtMb(host.mem_total_mb)} хоста`) +
                metricCardHTML('Сеть сервисов', `↓ ${fmtMb(load.net_rx_mb)}`, `↑ ${fmtMb(load.net_tx_mb)}`) +
                metricCardHTML('Диск Docker', fmtMb(disk.images_mb), `образы · тома ${fmtMb(disk.volumes_mb)} · Docker ${host.server_version ?? '?'}`);
        } catch (e) {
            // Даже при сбое Docker карточки остаются на месте (значение «—», подпись «недоступно»),
            // чтобы полоса метрик не схлопывалась и верстка не прыгала.
            if (getToken()) strip.innerHTML = METRIC_LABELS.map(l => metricCardHTML(l, '—', 'недоступно')).join('');
        }
    }

    // --- Виджет «Здоровье сервера» (Ночь 13, 21_HOST_OPS волна 1): ЦП/память/диск/
    // swap ХОСТА с порогами (жёлтый >80%, красный >92%) + warnings от сервера.
    // Ночь 19: значения стали ГРАФИКАМИ с динамикой за 24 ч (как в ЛК) — история
    // из GET /api/system/metrics/history (минутный сэмплер metrics_history.py).
    // На хосте без /proc (dev-Windows) сервер отдаёт null — карточка честно «—».
    const HH_LABELS = ['ЦП сервера', 'Память сервера', 'Диск сервера', 'Swap'];
    let lastHostHealthSig = '';
    function hostHealthSkeletonHTML() {
        lastHostHealthSig = '';
        return `<div class="metrics-strip host-health-strip">` + HH_LABELS.map(label =>
            `<div class="metric-card loading"><div class="metric-label">${label}</div><div class="metric-value"><span class="skel skel-val"></span></div><div class="metric-sub"><span class="skel skel-sub"></span></div></div>`
        ).join('') + `</div>`;
    }
    const hhPct = v => (v == null ? '—' : `${Math.round(v)}%`);
    // Порог карточки считает СЕРВЕР (status/warnings) — здесь только раскраска значения.
    const hhLevel = pct => (pct == null ? '' : (pct >= 92 ? 'crit' : (pct >= 80 ? 'warn' : 'ok')));
    // Спарклайн динамики (как в ЛК): полилиния + заливка, без библиотек.
    function sparklineSVG(vals, lvl, height = 34) {
        const nums = vals.filter(v => v != null);
        if (nums.length < 2) return '';
        const w = 100, h = height, pad = 2, n = vals.length;
        const pts = [];
        vals.forEach((v, i) => {
            if (v == null) return;
            const x = n > 1 ? (i / (n - 1)) * w : w;
            const y = h - pad - (Math.min(100, Math.max(0, v)) / 100) * (h - pad * 2);
            pts.push(`${x.toFixed(1)},${y.toFixed(1)}`);
        });
        const first = pts[0].split(',')[0], last = pts[pts.length - 1].split(',')[0];
        return `<svg class="spark ${lvl || ''}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true"><polygon points="${first},${h} ${pts.join(' ')} ${last},${h}"></polygon><polyline points="${pts.join(' ')}"></polyline></svg>`;
    }
    function hhCardHTML(label, pct, sub, series) {
        const lvl = hhLevel(pct);
        const spark = series ? sparklineSVG(series, lvl) : '';
        const graph = spark || (pct == null ? '' :
            `<div class="hh-bar"><div class="hh-bar-fill ${lvl}" style="width:${Math.max(2, Math.min(100, pct))}%"></div></div>`);
        return `<div class="metric-card hh-${lvl || 'na'}"><div class="metric-label">${label}</div><div class="metric-value">${hhPct(pct)}</div>${graph}<div class="metric-sub">${sub}</div></div>`;
    }
    function fmtUptime(sec) {
        if (sec == null) return '';
        const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600);
        return d > 0 ? `аптайм ${d} д ${h} ч` : `аптайм ${h} ч ${Math.floor((sec % 3600) / 60)} мин`;
    }
    // Колонка истории метрик (1=ЦП, 2=Память, 3=Диск) + живой снимок хвостом.
    // История длиной в сутки прореживается до ~90 точек — спарклайну больше не нужно.
    function metricSeries(hist, col, currentPct) {
        const pts = (hist && Array.isArray(hist.points)) ? hist.points : [];
        let vals = pts.map(p => (Array.isArray(p) && p[col] != null ? p[col] : null));
        const step = Math.ceil(vals.length / 90);
        if (step > 1) {
            const thin = [];
            for (let i = 0; i < vals.length; i += step) {
                const chunk = vals.slice(i, i + step).filter(v => v != null);
                thin.push(chunk.length ? Math.max(...chunk) : null);
            }
            vals = thin;
        }
        if (currentPct != null) vals.push(currentPct);
        return vals;
    }
    async function loadHostHealth() {
        const wrap = document.getElementById('hostHealthWrap');
        if (!wrap) return;
        let h;
        try { h = await fetchData('hostHealth', '/api/host/health'); }
        catch (e) {
            if (getToken()) wrap.innerHTML = `<div class="metrics-strip host-health-strip">${HH_LABELS.map(l => `<div class="metric-card"><div class="metric-label">${l}</div><div class="metric-value">—</div><div class="metric-sub">недоступно</div></div>`).join('')}</div>`;
            return;
        }
        let hist = null;
        try { hist = await fetchData('metricsHistory', '/api/system/metrics/history'); }
        catch (e) { /* истории может не быть (первый запуск) — карточки живут барами */ }
        // Точечное обновление: перерисовываем только при изменении данных (без миганий).
        const sig = JSON.stringify(h) + '|' + (hist && hist.points ? hist.points.length : 0);
        if (sig === lastHostHealthSig) return;
        lastHostHealthSig = sig;
        const disk = h.disk || {}, mem = h.memory || {}, swap = h.swap || {};
        const load = h.load || null, cpu = h.cpu_count;
        const cpuPct = (load && cpu) ? Math.min(100, Math.round((load[0] / cpu) * 100)) : null;
        const noSwap = swap.total_mb === 0;
        const swapCard = noSwap
            ? `<div class="metric-card hh-warn"><div class="metric-label">Swap</div><div class="metric-value">нет</div><div class="metric-sub">не настроен — риск OOM при сборке</div></div>`
            : hhCardHTML('Swap', swap.used_pct, swap.total_mb == null ? 'нет данных' : `${(swap.total_mb / 1024).toFixed(1)} GB выделено`);
        const warns = (h.warnings || []);
        const warnHTML = warns.length
            ? `<div class="host-health-warnings ${h.status === 'crit' ? 'crit' : ''}"><span class="material-symbols-outlined">warning</span>${warns.map(escapeHTML).join(' · ')}</div>`
            : '';
        wrap.innerHTML = `<div class="metrics-strip host-health-strip">`
            + hhCardHTML('ЦП сервера', cpuPct,
                load ? `load ${load[0].toFixed(2)} · ${cpu ?? '?'} CPU${h.uptime_sec != null ? ' · ' + fmtUptime(h.uptime_sec) : ''}` : 'нет данных',
                metricSeries(hist, 1, cpuPct))
            + hhCardHTML('Память сервера', mem.used_pct,
                mem.available_mb == null ? 'нет данных' : `доступно ${(mem.available_mb / 1024).toFixed(1)} GB из ${(mem.total_mb / 1024).toFixed(1)} GB`,
                metricSeries(hist, 2, mem.used_pct))
            + hhCardHTML('Диск сервера', disk.used_pct,
                disk.free_gb == null ? 'нет данных' : `свободно ${disk.free_gb} GB из ${disk.total_gb} GB`,
                metricSeries(hist, 3, disk.used_pct))
            + swapCard
            + `</div>${warnHTML}`;
    }

    // Авто-обновление дашборда после мутирующего запроса (если открыт он и нет модалки).
    function maybeRefreshDashboard(method) {
        if (!method || method === 'GET') return;
        if (window.location.hash.replace('#', '') !== 'dashboard') return;
        if (document.querySelector('.modal-backdrop.show')) return;
        invalidateCache('systemMetrics');
        clearTimeout(maybeRefreshDashboard._t);
        maybeRefreshDashboard._t = setTimeout(() => { if (window.location.hash.replace('#', '') === 'dashboard') renderDashboard(); }, 120);
    }

    // Готовит узлы и рёбра графа из уже кэшируемых данных трёх уровней.
    // Узлы — описатели (kind/id/inner/col), рёбра обогащены метаданными связи
    // (имена/виды концов + текст отношения) для попапа по клику на линию.
    const REL_LABELS = { 'bp>svc': 'Сервис развёрнут из этой сборки', 'svc>app': 'Приложение опубликовано на этом сервисе' };

    // Упорядочивает узлы ВНУТРИ колонок так, чтобы связанные узлы стояли «друг напротив
    // друга» — это убирает пересечение стрелок («икс» 1-2/2-1 вместо 1-1/2-2) и в графе,
    // и в колонках. Барицентр-эвристика (как в Sugiyama-раскладке): колонка сортируется
    // по средней позиции своих родителей в предыдущей колонке. Один прямой проход слева
    // направо достаточно для нашего дерева из 3 уровней (Библиотека→Сервисы→Приложения).
    // Мутирует массив `nodes` на месте (его порядок и определяет раскладку обоих видов).
    function orderNodesByConnection(nodes, edges) {
        const NO_PARENT = 1e9;  // узлы без родителя уходят вниз колонки, сохраняя относительный порядок
        const parents = {};
        edges.forEach(e => { (parents[e.to] = parents[e.to] || []).push(e.from); });
        const byCol = [[], [], []];
        nodes.forEach(n => { if (byCol[n.col]) byCol[n.col].push(n); });
        const pos = {};  // key → позиция узла в своей колонке (заполняется по мере прохода)
        byCol[0].forEach((n, i) => { pos[n.key] = i; });  // колонка 0 сохраняет исходный порядок
        const bary = n => {
            const ps = (parents[n.key] || []).map(k => pos[k]).filter(v => v != null);
            return ps.length ? ps.reduce((s, v) => s + v, 0) / ps.length : NO_PARENT;
        };
        for (let c = 1; c < 3; c++) {
            byCol[c].forEach((n, i) => { n._ord = i; });  // исходный индекс — стабильный tiebreak
            byCol[c].sort((a, b) => (bary(a) - bary(b)) || (a._ord - b._ord));
            byCol[c].forEach((n, i) => { pos[n.key] = i; });
        }
        byCol.forEach(col => col.forEach(n => { delete n._ord; }));
        nodes.length = 0;  // перезаписываем массив новым порядком (col0, col1, col2)
        byCol.forEach(col => col.forEach(n => nodes.push(n)));
    }

    async function buildDashboardData() {
        const [blueprints, services, applications] = await Promise.all([
            fetchData('blueprints', '/api/blueprints'),
            fetchData('services', '/api/services'),
            fetchData('applications', '/api/applications'),
        ]);
        const emptyNote = t => `<div class="graph-empty">${t}</div>`;
        const nodes = [];
        blueprints.forEach(bp => nodes.push({ key: `bp-${bp.id}`, kind: 'bp', cls: 'bp', id: bp.id, col: 0, name: bp.name,
            title: `${bp.name} · версий: ${bp.artifacts.length}`,
            inner: `<span class="material-symbols-outlined">inventory_2</span><span class="gn-label">${escapeHTML(bp.name)}</span><span class="gn-badge">v${bp.artifacts.length}</span>` }));
        services.forEach(s => nodes.push({ key: `svc-${s.id}`, kind: 'svc', cls: 'svc', id: s.id, col: 1, name: s.name,
            title: `${s.name} · ${s.status} · порт ${s.assigned_port} · ${s.artifact.version_tag}`,
            inner: `<span class="status-dot ${s.status}"></span><span class="gn-label">${escapeHTML(s.name)}</span><span class="gn-port">:${s.assigned_port}</span>` }));
        applications.forEach(a => nodes.push({ key: `app-${a.id}`, kind: 'app', cls: 'app', id: a.id, col: 2, name: a.domain,
            title: `${a.domain}${a.ssl_cert_name ? ' · SSL' : ' · HTTP'}`,
            inner: `<span class="material-symbols-outlined">${a.ssl_cert_name ? 'lock' : 'public'}</span><span class="gn-label">${escapeHTML(a.domain)}</span>` }));
        const nodeByKey = {}; nodes.forEach(n => nodeByKey[n.key] = n);

        const edges = [];
        const addEdge = (from, to) => {
            const a = nodeByKey[from], b = nodeByKey[to];
            if (!a || !b) return;
            edges.push({ from, to, fromKind: a.kind, toKind: b.kind, fromId: a.id, toId: b.id, fromName: a.name, toName: b.name, relLabel: REL_LABELS[`${a.kind}>${b.kind}`] || 'Связь' });
        };
        services.forEach(s => { if (s.artifact && s.artifact.blueprint_id != null) addEdge(`bp-${s.artifact.blueprint_id}`, `svc-${s.id}`); });
        applications.forEach(a => { if (a.service && a.service.id != null) addEdge(`svc-${a.service.id}`, `app-${a.id}`); });

        // Выстраиваем порядок узлов в колонках по связям — рёбра перестают пересекаться.
        orderNodesByConnection(nodes, edges);

        // Готовые HTML-колонки для статичного вида «Колонки».
        const nodeHTML = n => `<div class="graph-node ${n.cls}" data-node="${n.key}" data-kind="${n.kind}" data-id="${n.id}" title="${escapeHTML(n.title)}">${n.inner}</div>`;
        const colHTML = c => { const ns = nodes.filter(n => n.col === c); return ns.length ? ns.map(nodeHTML).join('') : emptyNote(['нет приложений', 'нет сервисов', 'нет публикаций'][c]); };
        return { blueprints, services, applications, nodes, edges, nodeByKey, bpCol: colHTML(0), svcCol: colHTML(1), appCol: colHTML(2) };
    }

    const GRAPH_EMPTY = `<div class="settings-card"><p style="color:var(--text-secondary)">Пока нет объектов. Начните с Библиотеки в разделе «Конвейер».</p></div>`;

    // Граф-вид: динамический холст (детерминированная раскладка + pan/zoom).
    async function renderGraphView(host) {
        const d = await buildDashboardData();
        if (!d.blueprints.length && !d.services.length && !d.applications.length) { host.innerHTML = GRAPH_EMPTY; return; }
        host.innerHTML = `
            <div class="graph-viewport" id="graphViewport">
                <div class="graph-world" id="graphWorld">
                    <svg class="graph-edges" id="graphEdges" xmlns="http://www.w3.org/2000/svg"></svg>
                </div>
                <div class="graph-controls" id="graphControls">
                    <button class="graph-ctl" data-z="in" title="Приблизить"><span class="material-symbols-outlined">add</span></button>
                    <button class="graph-ctl" data-z="out" title="Отдалить"><span class="material-symbols-outlined">remove</span></button>
                    <button class="graph-ctl" data-z="fit" title="Вместить"><span class="material-symbols-outlined">fit_screen</span></button>
                </div>
                <div class="graph-hint">Колесо — масштаб · перетаскивание — панорама · клик по связи — детали</div>
            </div>`;
        setupGraphCanvas(host, d);
    }

    // Статичный вид «Колонки»: карточки уровней, без рёбер.
    async function renderColumnsView(host) {
        const d = await buildDashboardData();
        if (!d.blueprints.length && !d.services.length && !d.applications.length) { host.innerHTML = GRAPH_EMPTY; return; }
        host.innerHTML = `
            <div class="columns-canvas" id="graphCanvas">
                <div class="settings-card graph-col"><div class="graph-col-title">Библиотека <span>${d.blueprints.length}</span></div><div class="graph-col-nodes">${d.bpCol}</div></div>
                <div class="settings-card graph-col"><div class="graph-col-title">Сервисы <span>${d.services.length}</span></div><div class="graph-col-nodes">${d.svcCol}</div></div>
                <div class="settings-card graph-col"><div class="graph-col-title">Приложения <span>${d.applications.length}</span></div><div class="graph-col-nodes">${d.appCol}</div></div>
            </div>`;
        setupColumnsInteractions(host, d.edges);
    }

    // --- Колонки: hover-подсветка цепочки + клик по узлу (без рёбер). ---
    function setupColumnsInteractions(host, edges) {
        const canvas = host.querySelector('#graphCanvas');
        const fwd = {}, bwd = {};
        edges.forEach(e => { (fwd[e.from] = fwd[e.from] || []).push(e.to); (bwd[e.to] = bwd[e.to] || []).push(e.from); });
        const reach = (start, adj) => { const seen = new Set(), q = [start]; while (q.length) { const n = q.shift(); (adj[n] || []).forEach(t => { if (!seen.has(t)) { seen.add(t); q.push(t); } }); } return seen; };
        const highlight = (key) => {
            const visited = new Set([key, ...reach(key, fwd), ...reach(key, bwd)]);
            canvas.classList.add('dim');
            host.querySelectorAll('.graph-node').forEach(n => n.classList.toggle('highlight', visited.has(n.dataset.node)));
        };
        const clear = () => { canvas.classList.remove('dim'); host.querySelectorAll('.highlight').forEach(el => el.classList.remove('highlight')); };
        host.querySelectorAll('.graph-node').forEach(node => {
            node.onmouseenter = () => highlight(node.dataset.node);
            node.onmouseleave = clear;
            node.onclick = () => openDetailDrawer(node.dataset.kind, parseInt(node.dataset.id));
        });
    }

    // Хранит активный resize-обработчик холста, чтобы снять его при ре-рендере.
    let graphResizeHandler = null;
    // --- Граф-холст: детерминированная раскладка, узлы+рёбра в одном transform-мире. ---
    // Координаты рёбер берутся из раскладки (а не из getBoundingClientRect), поэтому
    // стрелки совпадают с узлами при любом масштабе/панораме — корень прежних промахов.
    function setupGraphCanvas(host, d) {
        const viewport = host.querySelector('#graphViewport');
        const world = host.querySelector('#graphWorld');
        const svg = host.querySelector('#graphEdges');

        const NODE_W = 240, NODE_H = 46, COL_GAP = 150, ROW_GAP = 18, PAD = 28, TITLE_H = 34;
        const COL_LABELS = ['Библиотека', 'Сервисы', 'Приложения'];
        const COL_EMPTY = ['нет приложений', 'нет сервисов', 'нет публикаций'];
        const colCounts = [d.blueprints.length, d.services.length, d.applications.length];

        // Раскладка: 3 колонки на фикс. X, узлы стопкой, колонки центрируются по вертикали.
        const byCol = [[], [], []];
        d.nodes.forEach(n => byCol[n.col].push(n));
        const colH = byCol.map(ns => ns.length ? ns.length * NODE_H + (ns.length - 1) * ROW_GAP : NODE_H);
        const contentH = Math.max(...colH);
        const worldW = PAD * 2 + 3 * NODE_W + 2 * COL_GAP;
        const worldH = PAD * 2 + TITLE_H + contentH;

        const pos = {};
        let nodesHTML = '';
        for (let c = 0; c < 3; c++) {
            const x = PAD + c * (NODE_W + COL_GAP);
            nodesHTML += `<div class="graph-col-title canvas-title" style="left:${x}px;top:${PAD}px;width:${NODE_W}px">${COL_LABELS[c]} <span>${colCounts[c]}</span></div>`;
            const ns = byCol[c];
            const y0 = PAD + TITLE_H + (contentH - colH[c]) / 2;
            if (!ns.length) { nodesHTML += `<div class="graph-empty canvas-empty" style="left:${x}px;top:${y0}px;width:${NODE_W}px">${COL_EMPTY[c]}</div>`; continue; }
            ns.forEach((n, i) => {
                const y = y0 + i * (NODE_H + ROW_GAP);
                pos[n.key] = { x, y };
                nodesHTML += `<div class="graph-node canvas-node ${n.cls}" data-node="${n.key}" data-kind="${n.kind}" data-id="${n.id}" title="${escapeHTML(n.title)}" style="left:${x}px;top:${y}px">${n.inner}</div>`;
            });
        }
        world.style.width = worldW + 'px'; world.style.height = worldH + 'px';
        svg.setAttribute('width', worldW); svg.setAttribute('height', worldH);
        svg.setAttribute('viewBox', `0 0 ${worldW} ${worldH}`);
        world.insertAdjacentHTML('beforeend', nodesHTML);

        // Рёбра: видимая кривая + широкая прозрачная «hit»-кривая для клика/наведения.
        const defs = `<defs><marker id="arrowHead" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="var(--text-secondary)"/></marker></defs>`;
        const edgeByKey = {};
        svg.innerHTML = defs + d.edges.map(e => {
            const a = pos[e.from], b = pos[e.to];
            if (!a || !b) return '';
            const key = `${e.from}__${e.to}`; edgeByKey[key] = e;
            const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2, x2 = b.x, y2 = b.y + NODE_H / 2, mx = (x1 + x2) / 2;
            const path = `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`;
            return `<path class="graph-edge" data-edge="${key}" marker-end="url(#arrowHead)" d="${path}" />` +
                   `<path class="graph-edge-hit" data-edge="${key}" d="${path}" />`;
        }).join('');

        // Подсветка цепочки (предки+потомки) и отдельной связи.
        const fwd = {}, bwd = {};
        d.edges.forEach(e => { (fwd[e.from] = fwd[e.from] || []).push(e.to); (bwd[e.to] = bwd[e.to] || []).push(e.from); });
        const reach = (start, adj) => { const seen = new Set(), q = [start]; while (q.length) { const n = q.shift(); (adj[n] || []).forEach(t => { if (!seen.has(t)) { seen.add(t); q.push(t); } }); } return seen; };
        const highlightNode = (key) => {
            const visited = new Set([key, ...reach(key, fwd), ...reach(key, bwd)]);
            world.classList.add('dim');
            world.querySelectorAll('.graph-node').forEach(n => n.classList.toggle('highlight', visited.has(n.dataset.node)));
            world.querySelectorAll('.graph-edge').forEach(p => { const [f, t] = p.dataset.edge.split('__'); p.classList.toggle('highlight', visited.has(f) && visited.has(t)); });
        };
        const highlightEdge = (key) => {
            const e = edgeByKey[key]; if (!e) return;
            world.classList.add('dim');
            world.querySelectorAll('.graph-node').forEach(n => n.classList.toggle('highlight', n.dataset.node === e.from || n.dataset.node === e.to));
            world.querySelectorAll('.graph-edge').forEach(p => p.classList.toggle('highlight', p.dataset.edge === key));
        };
        const clearHighlight = () => { world.classList.remove('dim'); world.querySelectorAll('.highlight').forEach(el => el.classList.remove('highlight')); };

        // Попап о связи: концы как кнопки (открывают drawer) + текст отношения.
        const KIND_LABEL = { bp: 'Библиотека', svc: 'Сервис', app: 'Приложение' };
        const closeEdgePopover = () => { const p = document.getElementById('graphEdgePopover'); if (p) p.remove(); };
        const showEdgePopover = (e, evt) => {
            closeEdgePopover();
            const pop = document.createElement('div');
            pop.className = 'graph-edge-popover'; pop.id = 'graphEdgePopover';
            pop.innerHTML = `
                <div class="gep-title"><span class="material-symbols-outlined">share</span>Связь</div>
                <div class="gep-flow">
                    <button class="gep-chip ${e.fromKind}" data-kind="${e.fromKind}" data-id="${e.fromId}"><span class="gep-kind">${KIND_LABEL[e.fromKind]}</span>${escapeHTML(e.fromName)}</button>
                    <span class="material-symbols-outlined gep-arrow">arrow_forward</span>
                    <button class="gep-chip ${e.toKind}" data-kind="${e.toKind}" data-id="${e.toId}"><span class="gep-kind">${KIND_LABEL[e.toKind]}</span>${escapeHTML(e.toName)}</button>
                </div>
                <div class="gep-rel">${escapeHTML(e.relLabel)}</div>`;
            viewport.appendChild(pop);
            const r = viewport.getBoundingClientRect();
            let x = evt.clientX - r.left + 12, y = evt.clientY - r.top + 12;
            x = Math.min(x, r.width - pop.offsetWidth - 8);
            y = Math.min(y, r.height - pop.offsetHeight - 8);
            pop.style.left = Math.max(8, x) + 'px'; pop.style.top = Math.max(8, y) + 'px';
            pop.querySelectorAll('.gep-chip').forEach(c => c.onclick = ev => { ev.stopPropagation(); closeEdgePopover(); openDetailDrawer(c.dataset.kind, parseInt(c.dataset.id)); });
            highlightEdge(`${e.from}__${e.to}`);
        };

        // Pan/zoom: единый CSS-transform на мире (узлы и рёбра двигаются вместе).
        let zoom = 1, panX = 0, panY = 0;
        const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
        const apply = () => { world.style.transform = `translate(${panX}px, ${panY}px) scale(${zoom})`; };
        const fit = () => {
            const vw = viewport.clientWidth, vh = viewport.clientHeight;
            if (!vw || !vh) return;
            zoom = clamp(Math.min(vw / worldW, vh / worldH) * 0.92, 0.2, 1.2);
            panX = (vw - worldW * zoom) / 2; panY = (vh - worldH * zoom) / 2;
            apply();
        };
        const zoomAt = (mx, my, factor) => {
            const nz = clamp(zoom * factor, 0.2, 2.5);
            panX = mx - ((mx - panX) / zoom) * nz;
            panY = my - ((my - panY) / zoom) * nz;
            zoom = nz; apply();
        };

        viewport.addEventListener('wheel', e => {
            e.preventDefault();
            const r = viewport.getBoundingClientRect();
            zoomAt(e.clientX - r.left, e.clientY - r.top, e.deltaY < 0 ? 1.12 : 1 / 1.12);
        }, { passive: false });

        host.querySelector('#graphControls').addEventListener('click', e => {
            const z = e.target.closest('button')?.dataset.z; if (!z) return;
            if (z === 'fit') return fit();
            const r = viewport.getBoundingClientRect();
            zoomAt(r.width / 2, r.height / 2, z === 'in' ? 1.2 : 1 / 1.2);
        });

        // Панорама перетаскиванием фона; флаг moved отличает панораму от клика.
        let dragging = false, moved = false, sx = 0, sy = 0, spx = 0, spy = 0;
        const onBackground = t => !t.closest('.graph-node') && !t.closest('.graph-edge-hit') && !t.closest('.graph-controls') && !t.closest('.graph-edge-popover');
        viewport.addEventListener('pointerdown', e => {
            if (e.button !== 0) return;
            moved = false;  // сброс на ЛЮБОМ нажатии — иначе прошлая панорама «съедает» следующий клик по узлу/связи
            if (!onBackground(e.target)) return;  // панорама — только перетаскиванием фона
            dragging = true; sx = e.clientX; sy = e.clientY; spx = panX; spy = panY;
            viewport.classList.add('panning'); viewport.setPointerCapture(e.pointerId);
        });
        viewport.addEventListener('pointermove', e => {
            if (!dragging) return;
            const dx = e.clientX - sx, dy = e.clientY - sy;
            if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
            panX = spx + dx; panY = spy + dy; apply();
        });
        const endDrag = () => { if (dragging) { dragging = false; viewport.classList.remove('panning'); } };
        viewport.addEventListener('pointerup', endDrag);
        viewport.addEventListener('pointercancel', endDrag);

        // Клик по фону — снять подсветку и закрыть попап.
        viewport.addEventListener('click', e => {
            if (moved || !onBackground(e.target)) return;
            closeEdgePopover(); clearHighlight();
        });

        world.querySelectorAll('.graph-node').forEach(node => {
            node.addEventListener('mouseenter', () => { if (!dragging) highlightNode(node.dataset.node); });
            node.addEventListener('mouseleave', () => { if (!document.getElementById('graphEdgePopover')) clearHighlight(); });
            node.addEventListener('click', e => { if (moved) return; e.stopPropagation(); closeEdgePopover(); openDetailDrawer(node.dataset.kind, parseInt(node.dataset.id)); });
        });
        world.querySelectorAll('.graph-edge-hit').forEach(hit => {
            const key = hit.dataset.edge;
            hit.addEventListener('mouseenter', () => { if (!dragging) highlightEdge(key); });
            hit.addEventListener('mouseleave', () => { if (!document.getElementById('graphEdgePopover')) clearHighlight(); });
            hit.addEventListener('click', e => { if (moved) return; e.stopPropagation(); showEdgePopover(edgeByKey[key], e); });
        });

        requestAnimationFrame(fit);
        if (graphResizeHandler) window.removeEventListener('resize', graphResizeHandler);
        graphResizeHandler = () => fit();
        window.addEventListener('resize', graphResizeHandler);
    }

    // --- Боковая панель деталей (popup) для любого элемента дашборда ---
    // Вся доступная информация + полное управление + кнопка перехода на «уровень».
    function closeDrawer() { const d = document.getElementById('detailDrawer'); d.classList.remove('show'); delete d.dataset.kind; }
    const goToLevel = (stage) => { closeDrawer(); window.location.hash = stage; };
    const drawerRow = (k, v) => `<div class="detail-row"><span class="detail-k">${k}</span><span class="detail-v">${v}</span></div>`;
    const authGet = (url) => fetch(url, { headers: { 'Authorization': `Bearer ${getToken()}` } }).then(r => r.json());
    // Перерисовать список «Сервисов», если мы на этой стадии (drawer открыт поверх него).
    const refreshServiceListIfOpen = () => { if (currentStage === 'services' && document.getElementById('servicesContainer')) loadAndDisplayServices(); };

    // Живое обновление ОТКРЫТОГО drawer сервиса (логи + CPU/RAM) без перерисовки —
    // сохраняет позицию скролла логов. Полную перерисовку делает поллинг при смене статуса.
    function updateServiceDrawerLive(svc) {
        if (svc.status === 'online') {
            authGet(`/api/services/${svc.id}/stats`).then(s => {
                const c = document.getElementById('drwCpu'), m = document.getElementById('drwMem');
                if (c) c.textContent = `${s.cpu_percent}%`;
                if (m) m.textContent = `${s.memory_usage_mb} MB`;
            }).catch(() => {});
        }
        authGet(`/api/services/${svc.id}/logs`).then(d => {
            const log = document.getElementById('drwLogs');
            if (log) { const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40; log.textContent = d.logs || 'Логи пусты.'; if (atBottom) log.scrollTop = log.scrollHeight; }
        }).catch(() => {});
    }
    // Поллинг открытого drawer сервиса: смена статуса → полная перерисовка (кнопки/индикаторы),
    // иначе — живое обновление логов/статов на месте.
    function pollServiceDrawer(services) {
        const drawer = document.getElementById('detailDrawer');
        if (!drawer.classList.contains('show') || drawer.dataset.kind !== 'svc') return;
        const id = parseInt(drawer.dataset.id, 10);
        const svc = services.find(s => s.id === id);
        if (!svc) { closeDrawer(); return; }
        if (String(svc.status) !== drawer.dataset.status) { openDetailDrawer('svc', id); return; }
        updateServiceDrawerLive(svc);
    }

    async function openDetailDrawer(kind, id) {
        const title = document.getElementById('drawerTitle');
        const body = document.getElementById('drawerBody');
        const drawer = document.getElementById('detailDrawer');
        // Запоминаем, что показано — поллинг живёт-обновляет открытый drawer сервиса.
        drawer.dataset.kind = kind; drawer.dataset.id = id;
        body.innerHTML = `<p style="color:var(--text-secondary)">Загрузка…</p>`;
        drawer.classList.add('show');
        try {
            if (kind === 'bp') await renderBlueprintDrawer(id, title, body);
            else if (kind === 'svc') await renderServiceDrawer(id, title, body);
            else if (kind === 'app') await renderAppDrawer(id, title, body);
        } catch (e) { body.innerHTML = `<p style="color:var(--danger)">Не удалось загрузить детали.</p>`; }
    }

    async function renderBlueprintDrawer(id, title, body) {
        const blueprints = await fetchData('blueprints', '/api/blueprints');
        const bp = blueprints.find(b => b.id === id);
        if (!bp) { body.innerHTML = 'Не найдено.'; return; }
        title.innerHTML = `<span class="material-symbols-outlined">inventory_2</span>${escapeHTML(bp.name)}`;
        const versions = [...bp.artifacts].reverse().map(a => `<li class="drawer-version"><span class="mono">${escapeHTML(a.version_tag)}</span><span>${new Date(a.created_at).toLocaleDateString()}</span></li>`).join('') || '<li class="drawer-version" style="color:var(--text-secondary)">Нет версий</li>';
        body.innerHTML = `
            <div class="detail-chain">
                ${drawerRow('Тип', 'Приложение в библиотеке (blueprint)')}
                ${drawerRow('Описание', escapeHTML(bp.description) || '—')}
                ${drawerRow('Версий', bp.artifacts.length)}
            </div>
            <div class="section-title">Версии</div>
            <ul class="drawer-versions">${versions}</ul>
            <div class="action-row" style="margin-top:16px;">
                <button class="btn-icon-label" id="drwUpload"><span class="material-symbols-outlined">upload</span>Загрузить версию</button>
                <button class="btn-icon-label" id="drwGoto"><span class="material-symbols-outlined">arrow_forward</span>Перейти в Библиотеку</button>
            </div>`;
        document.getElementById('drwGoto').onclick = () => goToLevel('blueprints');
        document.getElementById('drwUpload').onclick = () => { closeDrawer(); openUploadModal(bp.id, bp.name); };
    }

    async function renderServiceDrawer(id, title, body) {
        const services = await fetchData('services', '/api/services');
        const svc = services.find(s => s.id === id);
        if (!svc) { body.innerHTML = 'Не найдено.'; return; }
        const online = svc.status === 'online';
        document.getElementById('detailDrawer').dataset.status = svc.status;  // поллинг сверяет смену статуса
        title.innerHTML = `<span class="status-dot ${svc.status}"></span>${escapeHTML(svc.name)}`;
        body.innerHTML = `
            <div class="detail-chain">
                ${drawerRow('Тип', 'Сервис (контейнер)')}
                ${drawerRow('Статус', `<span class="status-dot ${svc.status}"></span> ${svc.status}`)}
                ${drawerRow('Внутренний порт', svc.assigned_port || '—')}
                ${drawerRow('Версия', escapeHTML(svc.artifact.version_tag))}
                ${drawerRow('Из приложения', escapeHTML(svc.blueprint_name || '—'))}
            </div>
            <div class="stat-grid"><div class="stat-card"><div class="stat-card-label">CPU</div><div class="stat-card-value" id="drwCpu">${online ? '…' : 'N/A'}</div></div><div class="stat-card"><div class="stat-card-label">Memory</div><div class="stat-card-value" id="drwMem">${online ? '…' : 'N/A'}</div></div></div>
            <div class="section-title">Управление</div>
            <div class="drawer-toolbar">
                <button class="tool-btn" id="drwOpen" title="Открыть в браузере"><span class="material-symbols-outlined">open_in_new</span></button>
                <button class="tool-btn" id="drwStart" ${online ? 'disabled' : ''} title="Запустить"><span class="material-symbols-outlined">play_arrow</span></button>
                <button class="tool-btn" id="drwStop" ${online ? '' : 'disabled'} title="Остановить"><span class="material-symbols-outlined">stop</span></button>
                <button class="tool-btn" id="drwRestart" ${online ? '' : 'disabled'} title="Перезапустить"><span class="material-symbols-outlined">refresh</span></button>
                <button class="tool-btn" id="drwRedeploy" title="Обновить / Откатить версию"><span class="material-symbols-outlined">cached</span></button>
                <button class="tool-btn" id="drwConfig" title="Настройки сборки и рантайма"><span class="material-symbols-outlined">tune</span></button>
                <button class="tool-btn" id="drwPublish" ${online ? '' : 'disabled'} title="Опубликовать (публичный домен)"><span class="material-symbols-outlined">public</span></button>
                <span class="drawer-toolbar-sep"></span>
                <button class="tool-btn danger" id="drwDelete" title="Удалить сервис"><span class="material-symbols-outlined">delete</span></button>
            </div>
            <div class="section-title">Масштаб (реплики)</div>
            <div class="scale-row">
                <span class="mono scale-status" title="online / желаемое">${svc.online_count ?? 0}/${svc.target_replicas ?? 1} online</span>
                <input type="number" class="field-input" id="drwReplicas" min="0" max="20" value="${svc.target_replicas ?? 1}">
                <button class="btn-icon-label" id="drwScale"><span class="material-symbols-outlined">done</span>Применить</button>
            </div>
            <div id="drwDiag"></div>
            <div class="section-title">Консоль (последние строки)</div>
            <div class="log-window" id="drwLogs" style="height:200px;">Загрузка логов…</div>
            <div class="action-row" style="margin-top:16px;"><button class="btn-icon-label" id="drwGoto"><span class="material-symbols-outlined">arrow_forward</span>Перейти в Сервисы</button></div>`;
        document.getElementById('drwGoto').onclick = () => goToLevel('services');
        wireServiceOpenButton(document.getElementById('drwOpen'), svc);
        // Действие НЕ закрывает drawer (как было) — обновляем список под ним и перерисовываем
        // сам drawer свежими данными (видно смену статуса/кнопок + живые логи продолжаются).
        const afterAction = () => { invalidateCache('services', 'systemMetrics'); refreshServiceListIfOpen(); openDetailDrawer('svc', id); };
        const afterDelete = () => { invalidateCache('services', 'systemMetrics'); refreshServiceListIfOpen(); closeDrawer(); };
        const act = (action, msg) => postJSON(null, `/api/services/${id}/${action}`, null, msg, afterAction);
        document.getElementById('drwStart').onclick = () => act('start', 'Запуск…');
        document.getElementById('drwStop').onclick = () => act('stop', 'Остановка…');
        document.getElementById('drwRestart').onclick = () => act('restart', 'Перезапуск…');
        document.getElementById('drwRedeploy').onclick = () => { closeDrawer(); showModal('redeployModal', () => prepareRedeployModal(svc)); };
        document.getElementById('drwConfig').onclick = () => { closeDrawer(); showModal('configModal', () => prepareConfigModal(svc)); };
        document.getElementById('drwPublish').onclick = () => { closeDrawer(); showModal('applicationModal', () => prepareApplicationModal({ serviceId: svc.id })); };
        document.getElementById('drwDelete').onclick = async () => { closeDrawer(); if (await panelConfirm(`Удалить сервис «${svc.name}»? Контейнер будет удалён.`, { danger: true })) deleteJSON(`/api/services/${id}`, 'Сервис удалён.', afterDelete); };
        document.getElementById('drwScale').onclick = () => { const n = parseInt(document.getElementById('drwReplicas').value, 10); if (Number.isNaN(n)) return; postJSON(null, `/api/services/${id}/scale`, { target_replicas: n }, `Масштаб → ${n} реплик(и)`, afterAction); };
        if (online) authGet(`/api/services/${id}/stats`).then(s => { const c = document.getElementById('drwCpu'); if (c) { c.textContent = `${s.cpu_percent}%`; document.getElementById('drwMem').textContent = `${s.memory_usage_mb} MB`; } }).catch(() => {});
        // Логи и диагностику тянем всегда — в т.ч. для failed/offline (почему умер/не запускается).
        authGet(`/api/services/${id}/logs`).then(d => {
            const log = document.getElementById('drwLogs'); if (log) log.textContent = d.logs || 'Логи пусты.';
            const diag = document.getElementById('drwDiag');
            if (diag && svc.status !== 'online') {
                const bits = [];
                // Человекочитаемый диагноз с бэкенда — что именно случилось и где чинить.
                if (d.diagnosis) bits.push(`<b>${escapeHTML(d.diagnosis)}</b>`);
                else if (d.status === 'build_failed') bits.push('<span class="material-symbols-outlined inline-ico">warning</span> Образ не собрался — причина в логе ниже.');
                else if (d.exit_code != null) bits.push(`Контейнер завершился с кодом <span class="mono">${d.exit_code}</span>.`);
                if (d.oom_killed) bits.push('Признак: <span class="mono">OOMKilled</span> (нехватка памяти).');
                if (d.logs_readable === false) bits.push('<span class="material-symbols-outlined inline-ico">warning</span> Логи недоступны: logging-драйвер Docker на хосте не поддерживает чтение (нужен json-file/local) — это настройка сервера, не приложения.');
                if (d.restart_count) bits.push(`Попыток перезапуска до отказа: ${d.restart_count}.`);
                if (bits.length) diag.innerHTML = `<div class="diag-box">${bits.join('<br>')}</div>`;
            }
        }).catch(() => { const log = document.getElementById('drwLogs'); if (log) log.textContent = 'Не удалось загрузить логи.'; });
    }

    async function renderAppDrawer(id, title, body) {
        const [apps, services] = await Promise.all([fetchData('applications', '/api/applications'), fetchData('services', '/api/services')]);
        const app = apps.find(a => a.id === id);
        if (!app) { body.innerHTML = 'Не найдено.'; return; }
        const svc = services.find(s => s.id === app.service.id);
        const proto = app.ssl_cert_name ? 'https' : 'http';
        title.innerHTML = `<span class="material-symbols-outlined">${app.ssl_cert_name ? 'lock' : 'public'}</span>${escapeHTML(app.name)}`;
        body.innerHTML = `
            <div class="detail-chain">
                ${drawerRow('Тип', 'Приложение (публичный домен)')}
                ${drawerRow('Домен', `<a href="${proto}://${app.domain}" target="_blank">${escapeHTML(app.domain)}</a>${copyBtnHTML(`${proto}://${app.domain}`, 'Скопировать адрес')}`)}
                ${drawerRow('SSL', app.ssl_cert_name
                    ? `<span class="material-symbols-outlined inline-ico" style="color:var(--success)">lock</span> ${escapeHTML(app.ssl_cert_name)}`
                    : '<span class="material-symbols-outlined inline-ico" style="color:var(--text-secondary)">no_encryption</span> HTTP')}
                ${drawerRow('Сервис', `<span class="status-dot ${svc ? svc.status : 'offline'}"></span> <span class="mono">${escapeHTML(app.service.name)}</span>${svc ? ` · :${svc.assigned_port}` : ''}`)}
                ${drawerRow('Версия', svc ? escapeHTML(svc.artifact.version_tag) : '—')}
                ${drawerRow('Пользователи (protected)', (app.users || []).length)}
            </div>
            <div class="section-title">Управление</div>
            <div class="drawer-toolbar">
                <a class="tool-btn" href="${proto}://${app.domain}" target="_blank" title="Открыть домен"><span class="material-symbols-outlined">open_in_new</span></a>
                <button class="tool-btn" id="drwEdit" title="Редактировать (домен / SSL)"><span class="material-symbols-outlined">edit</span></button>
                ${app.ssl_cert_name ? '' : `<button class="tool-btn" id="drwSsl" title="Выпустить Let's Encrypt SSL"><span class="material-symbols-outlined">shield_lock</span></button>`}
                <span class="drawer-toolbar-sep"></span>
                <button class="tool-btn danger" id="drwDelete" title="Удалить (снять с публикации)"><span class="material-symbols-outlined">delete</span></button>
            </div>
            <div class="action-row" style="margin-top:16px;"><button class="btn-icon-label" id="drwGoto"><span class="material-symbols-outlined">arrow_forward</span>Перейти в Приложения</button></div>`;
        document.getElementById('drwGoto').onclick = () => goToLevel('applications');
        document.getElementById('drwEdit').onclick = () => { closeDrawer(); openEditApplicationModal(app); };
        const sslBtn = document.getElementById('drwSsl');
        if (sslBtn) sslBtn.onclick = () => { closeDrawer(); handleIssueAppSsl(app.id, app.domain); };
        document.getElementById('drwDelete').onclick = async () => { closeDrawer(); if (await panelConfirm(`Удалить приложение (снять с публикации) «${app.name}»?`, { danger: true })) deleteJSON(`/api/applications/${app.id}`, 'Приложение удалено.', () => { invalidateCache('applications', 'systemMetrics'); closeDrawer(); }); };
    }

    // --- Инициализация Страниц ---
    const initBlueprintsPage = () => { document.getElementById('newBlueprintBtn').onclick = () => openBlueprintModal(); document.getElementById('blueprintForm').onsubmit = handleBlueprintSubmit; const upForm = document.getElementById('uploadArtifactForm'); upForm.onsubmit = handleUploadArtifact; upForm.elements.zip_file.onchange = handleArtifactFileSelected; loadAndDisplayBlueprints(); };
    const initServicesPage = () => { document.getElementById('newServiceBtn').onclick = () => showModal('serviceModal', () => prepareServiceModal()); document.getElementById('serviceForm').onsubmit = handleCreateService; document.getElementById('redeployForm').onsubmit = handleRedeployService; loadAndDisplayServices(); };
    const initApplicationsPage = () => { document.getElementById('newAppBtn').onclick = () => showModal('applicationModal', () => prepareApplicationModal()); document.getElementById('applicationForm').onsubmit = handleCreateApplication; document.getElementById('editApplicationForm').onsubmit = handleEditApplication; loadAndDisplayApplications(); };
    // SSL-раздел живёт вкладкой внутри «Настройки» (меню воронкой). Навешивает
    // обработчики форм сертификатов и грузит список. Зовётся из initSettingsPage.
    const initSslTab = () => { document.getElementById('uploadCertForm').onsubmit = handleCertUpload; const issueForm = document.getElementById('issueSslForm'); issueForm.onsubmit = handleSslIssue; attachDnsCheck(issueForm.elements.domain); loadAndDisplayCerts(); };
    // Программно активировать вкладку «Настройки» по имени (для deep-link #ssl).
    const activateSettingsTab = (name) => {
        const bar = document.querySelector('#main-content .page-content > .tab-bar');
        const tab = bar && bar.querySelector(`.tab[data-tab="${name}"]`);
        if (tab) tab.click();
    };
    // Баннер первичного доступа (ADR-014): пока домен не задан, панель открыта по
    // IP:7999 — подсказываем задать домен и закрыть доступ после онбординга.
    const renderFirstAccessBanner = (domain) => {
        const el = document.getElementById('firstAccessBanner');
        if (!el) return;
        if (domain) { el.innerHTML = ''; return; }
        el.innerHTML = `
            <div class="banner-warn">
                <span class="material-symbols-outlined">lock_open</span>
                <div>
                    <strong>Первичный доступ открыт по IP</strong>
                    <p>Панель сейчас доступна по голому IP без HTTPS. Задайте домен и SSL ниже, затем закройте первичный доступ на сервере:</p>
                    <code>sh /opt/exosystem-deployer/close-initial-access.sh</code>${copyBtnHTML('sh /opt/exosystem-deployer/close-initial-access.sh', 'Скопировать команду')}
                </div>
            </div>`;
    };

    const initSettingsPage = async () => {
        document.getElementById('createGroupForm').onsubmit = handleCreateGroup;
        const panelForm = document.getElementById('panelSettingsForm');
        const certSelect = panelForm.querySelector('select[name="ssl_cert_name"]');
        const modeSelect = document.getElementById('panelSslMode');
        const existingGroup = document.getElementById('panelSslExistingGroup');
        const issueHint = document.getElementById('panelSslIssueHint');
        attachDnsCheck(panelForm.elements.domain);

        const settings = await fetchData('panelSettings', '/api/panel/settings');
        panelForm.elements.domain.value = settings.domain || '';
        renderFirstAccessBanner(settings.domain);
        const certs = await fetchData('certs', '/api/ssl/certificates');
        populateSelect(certSelect, certs, c => c.name, c => c.name, true);
        certSelect.value = settings.ssl_cert_name || '';

        // Режим SSL: none / issue (выпустить сейчас) / existing. Начальный — по текущим настройкам.
        modeSelect.value = settings.ssl_cert_name ? 'existing' : 'none';
        const syncSslMode = () => {
            existingGroup.style.display = modeSelect.value === 'existing' ? 'block' : 'none';
            issueHint.style.display = modeSelect.value === 'issue' ? 'block' : 'none';
            certSelect.required = modeSelect.value === 'existing';
        };
        modeSelect.onchange = syncSslMode;
        syncSslMode();

        panelForm.onsubmit = (e) => handlePanelSettingsSubmit(e, panelForm);
        loadAndDisplayGroups();

        document.getElementById('githubConnectForm').onsubmit = handleGithubConnect;
        document.getElementById('githubDisconnectBtn').onclick = handleGithubDisconnect;
        loadGithubIntegration();

        initSslTab();  // SSL — вкладка внутри Настроек (меню воронкой)
    };

    // --- Терминал «для знатоков» (ADR-090): одна команда → вывод ---
    function initTerminalPage() {
        const ack = document.getElementById('terminalAck');
        const form = document.getElementById('terminalForm');
        const input = document.getElementById('terminalInput');
        const runBtn = document.getElementById('terminalRun');
        const win = document.getElementById('terminalWindow');
        if (!ack || !form || !input || !win) return;
        const history = [];
        let histPos = 0;

        const appendLine = (text, cls) => {
            win.style.display = 'block';
            const span = cls ? `<span class="log-${cls}">${escapeHTML(text)}</span>` : escapeHTML(text);
            win.innerHTML += (win.innerHTML ? '\n' : '') + span;
            win.scrollTop = win.scrollHeight;
        };

        // Чекбокс-акцепт риска гейтит ввод (расширенный режим для знатоков).
        ack.onchange = () => {
            const on = ack.checked;
            form.style.display = on ? 'flex' : 'none';
            input.disabled = !on;
            runBtn.disabled = !on;
            if (on) input.focus();
        };

        // История команд стрелками (как в реальном шелле).
        input.addEventListener('keydown', (e) => {
            if (e.key === 'ArrowUp') {
                if (histPos > 0) { histPos--; input.value = history[histPos] || ''; }
                e.preventDefault();
            } else if (e.key === 'ArrowDown') {
                if (histPos < history.length) { histPos++; input.value = history[histPos] || ''; }
                e.preventDefault();
            }
        });

        form.onsubmit = async (e) => {
            e.preventDefault();
            const command = input.value.trim();
            if (!command) return;
            if (history[history.length - 1] !== command) history.push(command);
            histPos = history.length;
            input.value = '';
            appendLine('$ ' + command, 'info');
            input.disabled = true; runBtn.disabled = true;
            const token = getToken();
            try {
                const resp = await fetch('/api/admin/exec', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
                    body: JSON.stringify({ command }),
                });
                if (resp.status === 401) { clearToken(); showLoginScreen(); return; }
                let data = {};
                try { data = await resp.json(); } catch (_) { data = {}; }
                if (!resp.ok) {
                    appendLine(data.detail || `Ошибка ${resp.status}.`, 'error');
                } else {
                    if (data.output) appendLine(data.output);
                    const codeCls = (data.exit_code === 0) ? 'success' : 'error';
                    const codeText = data.timed_out ? 'прервано по таймауту'
                        : (data.exit_code === null || data.exit_code === undefined)
                            ? 'не запустилось' : `код выхода: ${data.exit_code}`;
                    appendLine(`[${codeText}]`, codeCls);
                }
            } catch (_) {
                appendLine('Сеть недоступна — команда не выполнена.', 'error');
            } finally {
                input.disabled = false; runBtn.disabled = false; input.focus();
            }
        };
    }

    // --- Интеграция GitHub (ADR-033): подключение PAT + статус ---
    async function loadGithubIntegration() {
        const statusEl = document.getElementById('githubStatus');
        const disconnectBtn = document.getElementById('githubDisconnectBtn');
        if (!statusEl) return;
        invalidateCache('githubStatus');
        try {
            const status = await fetchData('githubStatus', '/api/integrations/github');
            if (status.connected) {
                statusEl.innerHTML = `<span style="color:var(--success)"><span class="material-symbols-outlined inline-ico">check_circle</span> Подключено: <strong>${escapeHTML(status.login || '')}</strong> <span class="mono">(${escapeHTML(status.masked_token || '')})</span></span>`;
                disconnectBtn.style.display = 'inline-flex';
            } else {
                statusEl.innerHTML = `<span>Не подключено.</span>`;
                disconnectBtn.style.display = 'none';
            }
        } catch (_) { statusEl.textContent = 'Ошибка загрузки статуса.'; }
    }
    async function handleGithubConnect(e) {
        e.preventDefault();
        const form = e.target;
        const token = form.elements.token.value.trim();
        if (!token) { panelAlert('Введите токен.'); return; }
        await postJSON(form, '/api/integrations/github', { token }, "GitHub подключён!", () => {
            form.reset();
            invalidateCache('githubStatus', 'githubRepos');
            loadGithubIntegration();
        });
    }
    async function handleGithubDisconnect() {
        if (!await panelConfirm('Отключить GitHub-аккаунт? Импорт приватных репозиториев станет недоступен.', { danger: true })) return;
        deleteJSON('/api/integrations/github', 'GitHub отключён.', () => {
            invalidateCache('githubStatus', 'githubRepos');
            loadGithubIntegration();
        });
    }

    // Бесшовно: домен + (опц.) сертификат одним действием. Для «issue»: DNS-чек →
    // сохранить домен по HTTP (чтобы nginx отвечал на :80 для ACME-челленджа) →
    // выпустить сертификат (WS-лог) → сохранить с HTTPS.
    async function handlePanelSettingsSubmit(e, panelForm) {
        e.preventDefault();
        const domain = (panelForm.elements.domain.value || '').trim() || null;
        const mode = document.getElementById('panelSslMode').value;
        const btn = panelForm.querySelector('button[type="submit"]');
        if (!domain && mode !== 'none') { panelAlert('Укажите домен панели для выпуска/привязки SSL.'); return; }

        let sslCertName = null;
        if (mode === 'existing') {
            sslCertName = panelForm.elements.ssl_cert_name.value || null;
        } else if (mode === 'issue') {
            // Выпуск SSL для домена панели — в ФОН (Ночь 10): задача сохранит домен по HTTP,
            // дождётся распространения DNS, выпустит сертификат и привяжет его к панели.
            const dnsStatus = document.getElementById('panelDnsStatus');
            btn.disabled = true;
            try {
                await postJSON(null, '/api/pending-actions/panel-ssl', { domain });
            } catch (err) { btn.disabled = false; return; }  // ошибка уже показана
            btn.disabled = false;
            dnsStatus.innerHTML = '<span style="color:var(--success)">Задача создана — выпуск идёт в фоне.</span>';
            invalidateCache('panelSettings', 'certs'); renderFirstAccessBanner(domain);
            openTaskCenter();
            tcToast('Выпуск SSL для панели запущен в фоне. Домен и сертификат применятся автоматически.');
            return;
        }

        await postJSON(panelForm, '/api/panel/settings', { domain, ssl_cert_name: sslCertName }, "Настройки панели применены! Nginx перезагружается...", () => {
            invalidateCache('panelSettings', 'certs');
            renderFirstAccessBanner(domain);
            if (domain && domain !== window.location.hostname) {
                const url = `${sslCertName ? 'https' : 'http'}://${domain}`;
                panelAlert(`Панель теперь доступна по адресу: ${url}` + (sslCertName ? `\n\nГотово! Не забудьте закрыть первичный доступ (порт 7999) на сервере:\nsh /opt/exosystem-deployer/close-initial-access.sh` : ''));
            }
        });
    }

    // --- Универсальные функции ---
    async function handleApiRequest(form, url, options, successMsg, callback) {
        const token = getToken();
        if (!token && url !== '/api/auth/token') {
            showLoginScreen();
            return;
        }

        options.headers = { ...options.headers, 'Authorization': `Bearer ${token}` };

        const btn = form ? form.querySelector('button[type="submit"]') : null;
        if (btn) { btn.disabled = true; btn.dataset.originalText = btn.textContent; btn.textContent = 'Выполнение...'; }

        try {
            const response = await fetch(url, options);
            if (response.status === 401) {
                clearToken();
                showLoginScreen();
                throw new Error("Сессия истекла. Пожалуйста, войдите снова.");
            }
            if (!response.ok) {
                const err = await response.json();
                throw new Error(err.detail || 'Произошла неизвестная ошибка');
            }
            let json_body = null;
            if (response.status !== 204 && response.headers.get("content-type")?.includes("application/json")) {
                json_body = await response.json();
            }
            if (successMsg) panelAlert(successMsg);
            if (callback) callback();
            maybeRefreshDashboard(options.method);

            return json_body;
        } catch (err) {
            panelAlert(`Ошибка: ${err.message}`);
            throw err;
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = btn.dataset.originalText || 'Submit'; }
        }
    }

    const fetchData = async (key, url) => {
        if (CACHE[key]) return CACHE[key];
        const token = getToken();
        if (!token) { showLoginScreen(); throw new Error("Not authenticated"); }
        try {
            const response = await fetch(url, { headers: { 'Authorization': `Bearer ${token}` } });
            if (response.status === 401) { clearToken(); showLoginScreen(); throw new Error("Session expired"); }
            if (!response.ok) throw new Error(`Network error for ${url}`);
            CACHE[key] = await response.json();
            return CACHE[key];
        } catch (error) { console.error(`Fetch failed for ${key}:`, error); throw error; }
    };

    const invalidateCache = (...keys) => { keys.forEach(key => CACHE[key] = null); };
    const escapeHTML = str => str?.toString().replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') || '';
    // Для значений в HTML-атрибутах (data-copy="…"): + кавычки.
    const escAttr = str => escapeHTML(str).replace(/"/g, '&quot;');

    // ===== Копирование в буфер (Ночь 20): единый паттерн с ЛК =====
    // Иконка content_copy → clipboard.writeText → краткий фидбек «Скопировано».
    // Панель часто живёт по IP без HTTPS (не-secure-context) → фолбэк textarea+execCommand.
    async function copyText(text) {
        try {
            if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(text);
                return true;
            }
        } catch (_) { /* ниже — фолбэк */ }
        try {
            const ta = document.createElement('textarea');
            ta.value = text;
            ta.setAttribute('readonly', '');
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            const ok = document.execCommand('copy');
            ta.remove();
            return ok;
        } catch (_) { return false; }
    }
    function copyFeedback(btn, ok) {
        const icon = btn.querySelector('.material-symbols-outlined');
        if (icon) icon.textContent = ok ? 'check' : 'error';
        btn.classList.add(ok ? 'copied' : 'copy-fail');
        let tip = btn.querySelector('.copy-tip');
        if (!tip) { tip = document.createElement('span'); tip.className = 'copy-tip'; btn.appendChild(tip); }
        tip.textContent = ok ? 'Скопировано' : 'Не удалось';
        clearTimeout(btn._copyTimer);
        btn._copyTimer = setTimeout(() => {
            if (icon) icon.textContent = 'content_copy';
            btn.classList.remove('copied', 'copy-fail');
            tip.remove();
        }, 1400);
    }
    const copyBtnHTML = (value, title = 'Копировать') =>
        `<button type="button" class="copy-btn" data-copy="${escAttr(value)}" title="${escAttr(title)}"><span class="material-symbols-outlined">content_copy</span></button>`;
    // Делегированный клик: кнопки рождаются в перерисовываемых таблицах/drawer'ах.
    document.addEventListener('click', async e => {
        const b = e.target.closest('.copy-btn');
        if (!b) return;
        copyFeedback(b, await copyText(b.dataset.copy || ''));
    });

    // ===== «Глаз» у пароля (Ночь 20): показать/скрыть вводимое (как в ЛК) =====
    function enhancePasswordInputs(root) {
        (root || document).querySelectorAll('input[type="password"]').forEach(input => {
            if (input.dataset.pwEyed) return;
            input.dataset.pwEyed = '1';
            const wrap = document.createElement('span');
            wrap.className = 'pw-field';
            input.parentNode.insertBefore(wrap, input);
            wrap.appendChild(input);
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'pw-toggle';
            btn.title = 'Показать пароль';
            btn.setAttribute('aria-label', 'Показать пароль');
            btn.innerHTML = '<span class="material-symbols-outlined">visibility</span>';
            btn.addEventListener('click', () => {
                const show = input.type === 'password';
                input.type = show ? 'text' : 'password';
                btn.querySelector('.material-symbols-outlined').textContent = show ? 'visibility_off' : 'visibility';
                btn.title = show ? 'Скрыть пароль' : 'Показать пароль';
                btn.setAttribute('aria-label', btn.title);
                input.focus();
            });
            wrap.appendChild(btn);
        });
    }
    // Русское склонение по числу: forms = [1, 2-4, 5+] (напр. ['версия','версии','версий']).
    const pluralRu = (n, forms) => { const a = Math.abs(n) % 100, b = a % 10; if (a > 10 && a < 20) return forms[2]; if (b > 1 && b < 5) return forms[1]; if (b === 1) return forms[0]; return forms[2]; };
    // Навешивает toggle на kebab-кнопку дропдауна (закрывает остальные открытые меню).
    // Переиспользует существующий паттерн `.dropdown`/`.dropdown-menu`/`data-toggle="dropdown"`.
    function wireDropdown(dd) {
        const toggle = dd.querySelector('[data-toggle="dropdown"]');
        const menu = dd.querySelector('.dropdown-menu');
        if (!toggle || !menu) return;
        toggle.addEventListener('click', e => {
            e.stopPropagation();
            const open = menu.classList.contains('show');
            document.querySelectorAll('.dropdown-menu.show').forEach(m => m.classList.remove('show'));
            if (!open) menu.classList.add('show');
        });
    }
    // --- Точечный keyed-рендер списка (Ночь 12, ADR-080, инвариант №9 «UI не врёт»).
    // Вместо `innerHTML = всё заново` (мигание, сброс открытых меню/фокуса) пересоздаём
    // ТОЛЬКО элементы с изменившейся сигнатурой данных; порядок — как в items;
    // исчезнувшие удаляем. Элемент, где пользователь что-то раскрыл (дропдаун/details)
    // или пишет (фокус), не трогаем до следующего тика.
    function syncKeyedList(host, items, keyOf, sigOf, buildEl) {
        const byKey = {};
        Array.from(host.children).forEach(el => { if (el.dataset && el.dataset.key) byKey[el.dataset.key] = el; });
        const seen = new Set();
        items.forEach((item, i) => {
            const key = String(keyOf(item));
            const sig = sigOf(item);
            seen.add(key);
            let node = byKey[key];
            if (!node) {
                node = buildEl(item);
                node.dataset.key = key;
                node.dataset.sig = sig;
            } else if (node.dataset.sig !== sig && !node.querySelector('.dropdown-menu.show, details[open], :focus')) {
                const fresh = buildEl(item);
                fresh.dataset.key = key;
                fresh.dataset.sig = sig;
                node.replaceWith(fresh);
                byKey[key] = fresh;
                node = fresh;
            }
            const ref = host.children[i] || null;
            if (ref !== node) host.insertBefore(node, ref);
        });
        Array.from(host.children).forEach(el => {
            if (!el.dataset || !el.dataset.key || !seen.has(el.dataset.key)) el.remove();
        });
    }
    const populateSelect = (select, items, textFn, valueFn, keepFirst = false) => { select.innerHTML = keepFirst ? select.firstElementChild.outerHTML : '<option value="" disabled selected>Выберите...</option>'; items.forEach(item => { const opt = document.createElement('option'); opt.textContent = textFn(item); opt.value = valueFn(item); select.appendChild(opt); }); };
    const formToJSON = form => Object.fromEntries(new FormData(form).entries());
    const postJSON = (form, url, data, successMsg, callback, method = 'POST') =>
        handleApiRequest(form, url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }, successMsg, callback);
    const postForm = (form, url, formData, successMsg, callback) => handleApiRequest(form, url, { method: 'POST', body: formData }, successMsg, callback);
    const deleteJSON = (url, successMsg, callback) => handleApiRequest(null, url, { method: 'DELETE' }, successMsg, callback);

    // --- Функции для каждой страницы (без изменений в логике, т.к. handleApiRequest всё делает) ---
    async function loadAndDisplayBlueprints() {
        const container = document.getElementById('blueprintsList');
        if (!container) return;  // вызвано вне стадии «Библиотека» (напр. из дашборда)
        try {
            const blueprints = await fetchData('blueprints', '/api/blueprints');
            updateRailCount('blueprints');
            if (blueprints.length === 0) { container.innerHTML = `<p style="color:var(--text-secondary)">Библиотека пуста. Нажмите "Создать приложение", чтобы добавить первое.</p>`; return; }

            // Действия версии. Для последней версии — «Запустить» + kebab (скачать/удалить);
            // в истории (compact) — компактные иконки (история живёт в overlay со скроллом,
            // вложенный дропдаун там обрезался бы overflow'ом, поэтому иконки).
            const versionActions = (art, bp, compact) => compact
                ? `<button class="icon-btn run-service-btn" data-art-id="${art.id}" data-bp-id="${bp.id}" title="Запустить сервис из этой версии"><span class="material-symbols-outlined">bolt</span></button>
                   <button class="icon-btn dl-artifact-btn" data-art-id="${art.id}" data-bp-id="${bp.id}" data-name="${escapeHTML(bp.name)}" data-tag="${escapeHTML(art.version_tag)}" title="Скачать ZIP"><span class="material-symbols-outlined">download</span></button>
                   <button class="icon-btn danger del-artifact-btn" data-art-id="${art.id}" data-bp-id="${bp.id}" data-tag="${escapeHTML(art.version_tag)}" title="Удалить версию"><span class="material-symbols-outlined">delete</span></button>`
                : `<button class="btn-icon-label run-service-btn" data-art-id="${art.id}" data-bp-id="${bp.id}" title="Запустить сервис из этой версии"><span class="material-symbols-outlined">bolt</span>Запустить</button>
                   <div class="dropdown">
                       <button class="icon-btn" data-toggle="dropdown" title="Ещё"><span class="material-symbols-outlined">more_vert</span></button>
                       <div class="dropdown-menu">
                           <div class="dropdown-item dl-artifact-btn" data-art-id="${art.id}" data-bp-id="${bp.id}" data-name="${escapeHTML(bp.name)}" data-tag="${escapeHTML(art.version_tag)}"><span class="material-symbols-outlined">download</span>Скачать ZIP</div>
                           <div class="dropdown-item danger del-artifact-btn" data-art-id="${art.id}" data-bp-id="${bp.id}" data-tag="${escapeHTML(art.version_tag)}"><span class="material-symbols-outlined">delete</span>Удалить версию</div>
                       </div>
                   </div>`;
            // Строка версии: тег/дата слева (усекается при нехватке места), действия справа.
            const versionRow = (art, bp, { latest = false, compact = false } = {}) => `
                <li class="lib-version${latest ? ' latest' : ''}"${art.description ? ` title="${escapeHTML(art.description)}"` : ''}>
                    <div class="lib-version-info">
                        <span class="mono">${escapeHTML(art.version_tag)}</span>
                        ${latest ? '<span class="lib-badge-latest">последняя</span>' : ''}
                        <span class="lib-version-date">${new Date(art.created_at).toLocaleDateString()}</span>
                        ${art.description ? '<span class="material-symbols-outlined lib-version-note" title="' + escapeHTML(art.description) + '">notes</span>' : ''}
                    </div>
                    <div class="lib-version-actions">${versionActions(art, bp, compact)}</div>
                </li>`;

            container.innerHTML = blueprints.map(bp => {
                const arts = [...bp.artifacts].reverse();  // новые сверху
                let versionsBlock;
                if (arts.length === 0) {
                    versionsBlock = '<p class="lib-empty">Нет загруженных версий. Нажмите «Загрузить версию».</p>';
                } else {
                    // Показываем только последнюю версию; историю — во ВСПЛЫВАЮЩЕМ overlay
                    // (position:absolute), чтобы раскрытие не меняло размер карточки и не
                    // дёргало интерфейс.
                    const [latest, ...rest] = arts;
                    const restBlock = rest.length ? `
                        <div class="lib-history">
                            <button type="button" class="lib-versions-toggle" data-toggle-versions><span class="material-symbols-outlined">expand_more</span><span class="lvt-text">Ещё версий: ${rest.length}</span></button>
                            <ul class="lib-versions-rest" hidden>${rest.map(a => versionRow(a, bp, { compact: true })).join('')}</ul>
                        </div>` : '';
                    versionsBlock = `<ul class="lib-versions">${versionRow(latest, bp, { latest: true })}</ul>${restBlock}`;
                }
                return `<div class="settings-card lib-card">
                    <div class="lib-card-head">
                        <div class="lib-card-title">
                            <h3>${escapeHTML(bp.name)}</h3>
                            <span class="lib-versions-count">${bp.artifacts.length} ${pluralRu(bp.artifacts.length, ['версия', 'версии', 'версий'])}</span>
                        </div>
                        <div class="lib-card-actions">
                            <button class="btn-icon-label upload-artifact-btn" data-id="${bp.id}" data-name="${escapeHTML(bp.name)}"><span class="material-symbols-outlined">upload</span>Загрузить версию</button>
                            <div class="dropdown">
                                <button class="icon-btn" data-toggle="dropdown" title="Действия с приложением"><span class="material-symbols-outlined">more_vert</span></button>
                                <div class="dropdown-menu">
                                    <div class="dropdown-item edit-bp-btn" data-id="${bp.id}" data-name="${escapeHTML(bp.name)}" data-desc="${escapeHTML(bp.description || '')}"><span class="material-symbols-outlined">edit</span>Редактировать</div>
                                    <div class="dropdown-item danger del-bp-btn" data-id="${bp.id}" data-name="${escapeHTML(bp.name)}"><span class="material-symbols-outlined">delete</span>Удалить приложение</div>
                                </div>
                            </div>
                        </div>
                    </div>
                    ${bp.description ? `<p class="lib-card-desc">${escapeHTML(bp.description)}</p>` : ''}
                    ${versionsBlock}
                </div>`;
            }).join('');

            // Дропдауны (kebab) закрываются по клику на пункт; раскрытие истории версий.
            container.querySelectorAll('.dropdown').forEach(wireDropdown);
            container.querySelectorAll('.dropdown-menu').forEach(menu => menu.addEventListener('click', () => menu.classList.remove('show')));
            const closeHistory = (list) => {
                if (!list || list.hasAttribute('hidden')) return;
                list.setAttribute('hidden', '');
                const t = list.previousElementSibling;
                if (t) { t.classList.remove('open'); const tx = t.querySelector('.lvt-text'); if (tx) tx.textContent = `Ещё версий: ${list.children.length}`; }
            };
            container.querySelectorAll('[data-toggle-versions]').forEach(btn => btn.onclick = () => {
                const list = btn.nextElementSibling;
                const willOpen = list.hasAttribute('hidden');
                container.querySelectorAll('.lib-versions-rest').forEach(l => { if (l !== list) closeHistory(l); });  // одна история за раз
                if (willOpen) { list.removeAttribute('hidden'); btn.classList.add('open'); btn.querySelector('.lvt-text').textContent = 'Скрыть историю'; }
                else closeHistory(list);
            });

            container.querySelectorAll('.upload-artifact-btn').forEach(btn => btn.onclick = () => openUploadModal(btn.dataset.id, btn.dataset.name));
            container.querySelectorAll('.edit-bp-btn').forEach(btn => btn.onclick = () => openBlueprintModal({ id: btn.dataset.id, name: btn.dataset.name, description: btn.dataset.desc }));
            container.querySelectorAll('.del-bp-btn').forEach(btn => btn.onclick = () => handleBlueprintDelete(btn.dataset.id, btn.dataset.name));
            container.querySelectorAll('.dl-artifact-btn').forEach(btn => btn.onclick = () => handleArtifactDownload(btn.dataset.bpId, btn.dataset.artId, `${btn.dataset.name}-${btn.dataset.tag}.zip`));
            container.querySelectorAll('.del-artifact-btn').forEach(btn => btn.onclick = () => handleArtifactDelete(btn.dataset.bpId, btn.dataset.artId, btn.dataset.tag));
            container.querySelectorAll('.run-service-btn').forEach(btn => btn.onclick = () => goToStage('services', () => showModal('serviceModal', () => prepareServiceModal({ blueprintId: btn.dataset.bpId, artifactId: btn.dataset.artId }))));
        } catch (error) { if (getToken()) container.innerHTML = `<p style="color:var(--danger)">Ошибка загрузки библиотеки.</p>`; }
    }
    function openBlueprintModal(bp = null) {
        const form = document.getElementById('blueprintForm');
        form.reset();
        document.getElementById('blueprintModalTitle').textContent = bp ? 'Редактировать приложение' : 'Создать приложение';
        form.elements.blueprint_id.value = bp ? bp.id : '';
        form.elements.name.value = bp ? bp.name : '';
        form.elements.description.value = bp ? (bp.description || '') : '';
        // При редактировании имя менять можно, но предупредим: оно используется в сервисах.
        showModal('blueprintModal');
    }
    async function handleBlueprintSubmit(e) {
        e.preventDefault();
        const form = e.target;
        const id = form.elements.blueprint_id.value;
        const data = { name: form.elements.name.value, description: form.elements.description.value || null };
        if (id) {
            await postJSON(form, `/api/blueprints/${id}`, data, "Изменения сохранены!", () => { hideModal('blueprintModal'); invalidateCache('blueprints'); loadAndDisplayBlueprints(); }, 'PATCH');
        } else {
            await postJSON(form, '/api/blueprints', data, "Приложение создано!", () => { hideModal('blueprintModal'); invalidateCache('blueprints'); loadAndDisplayBlueprints(); });
        }
    }
    async function handleBlueprintDelete(id, name) {
        if (!await panelConfirm(`Удалить приложение "${name}" из библиотеки вместе со всеми версиями?\n\nНельзя удалить, если на него есть запущенные сервисы.`, { danger: true })) return;
        await deleteJSON(`/api/blueprints/${id}`, "Приложение удалено.", () => { invalidateCache('blueprints'); loadAndDisplayBlueprints(); });
    }
    async function handleArtifactDelete(bpId, artId, tag) {
        if (!await panelConfirm(`Удалить версию "${tag}"?\n\nНельзя удалить, если версия используется запущенным сервисом.`, { danger: true })) return;
        await deleteJSON(`/api/blueprints/${bpId}/artifacts/${artId}`, "Версия удалена.", () => { invalidateCache('blueprints'); loadAndDisplayBlueprints(); });
    }
    // Генерирует уникальное имя на основе base, избегая занятых (existing — массив строк).
    function uniqueName(base, existing) {
        const taken = new Set((existing || []).filter(Boolean));
        if (!taken.has(base)) return base;
        let i = 2;
        while (taken.has(`${base}-${i}`)) i++;
        return `${base}-${i}`;
    }
    // При выборе ZIP — спросить сервер о предлагаемой версии и описании (из VERSION/CHANGELOG).
    async function handleArtifactFileSelected(e) {
        const fileInput = e.target;
        const form = fileInput.form;
        const file = fileInput.files[0];
        if (!file) return;
        const bpId = form.elements.blueprint_id.value;
        const versionInput = form.elements.version_tag;
        const descInput = form.elements.description;
        const oldPh = versionInput.placeholder;
        versionInput.placeholder = 'Анализ архива…';
        try {
            const fd = new FormData(); fd.append('zip_file', file);
            const res = await fetch(`/api/blueprints/${bpId}/artifacts/inspect`, { method: 'POST', headers: { 'Authorization': `Bearer ${getToken()}` }, body: fd });
            if (!res.ok) throw new Error();
            const data = await res.json();
            if (data.suggested_version && !versionInput.value) versionInput.value = data.suggested_version;
            if (data.description && !descInput.value) descInput.value = data.description;
        } catch (_) { /* тихо: подсказки необязательны */ }
        finally { versionInput.placeholder = oldPh; }
    }
    // Открыть модалку добавления версии (общий путь: страница Библиотеки и drawer).
    function openUploadModal(bpId, bpName) {
        const form = document.getElementById('uploadArtifactForm');
        form.reset();
        form.elements.blueprint_id.value = bpId;
        document.getElementById('uploadBlueprintName').value = bpName || '';
        setUploadSource('zip');
        showModal('uploadArtifactModal');
    }
    // Переключатель источника версии: ZIP-загрузка / импорт из GitHub.
    function setUploadSource(src) {
        const form = document.getElementById('uploadArtifactForm');
        const tabs = document.getElementById('uploadSrcTabs');
        if (!form || !tabs) return;
        form.dataset.src = src;
        tabs.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.src === src));
        form.querySelectorAll('[data-src-panel]').forEach(p => p.classList.toggle('active', p.dataset.srcPanel === src));
        if (src === 'github') loadGithubRepoPicker();
    }
    function wireUploadSourceTabs() {
        const tabs = document.getElementById('uploadSrcTabs');
        if (!tabs) return;
        tabs.querySelectorAll('.tab').forEach(t => t.onclick = () => setUploadSource(t.dataset.src));
        const select = document.getElementById('githubRepoSelect');
        if (select) select.onchange = () => {
            if (select.value) document.querySelector('#uploadArtifactForm [name="repo_url"]').value = `https://github.com/${select.value}`;
        };
    }
    // Подставляет список репозиториев подключённого GitHub-аккаунта вместо ручного
    // ввода URL (ADR-033). Без подключения — селект скрыт, поле URL работает как раньше.
    async function loadGithubRepoPicker() {
        const group = document.getElementById('githubRepoPickerGroup');
        const select = document.getElementById('githubRepoSelect');
        const hint = document.getElementById('githubSourceHint');
        if (!group || !select) return;
        try {
            const status = await fetchData('githubStatus', '/api/integrations/github');
            if (!status.connected) { group.style.display = 'none'; return; }
            const repos = await fetchData('githubRepos', '/api/integrations/github/repos');
            populateSelect(select, repos, r => r.full_name + (r.private ? ' (приватный)' : ''), r => r.full_name, false);
            group.style.display = 'block';
            if (hint) hint.innerHTML = `GitHub подключён (<strong>${escapeHTML(status.login || '')}</strong>) — доступны и приватные репозитории. Если в репозитории есть свой <span class="mono">Dockerfile</span> — соберём по нему.`;
        } catch (_) { group.style.display = 'none'; }
    }

    async function handleUploadArtifact(e) {
        e.preventDefault();
        const form = e.target;
        const blueprintId = form.elements.blueprint_id.value;
        const done = () => { hideModal('uploadArtifactModal'); invalidateCache('blueprints'); loadAndDisplayBlueprints(); };
        if ((form.dataset.src || 'zip') === 'github') {
            const repoUrl = form.elements.repo_url.value.trim();
            if (!repoUrl) { panelAlert('Укажите URL публичного GitHub-репозитория.'); return; }
            const data = {
                repo_url: repoUrl,
                ref: form.elements.ref.value.trim() || null,
                version_tag: form.elements.version_tag.value.trim() || null,
                description: form.elements.description.value.trim() || null,
            };
            await postJSON(form, `/api/blueprints/${blueprintId}/artifacts/from-github`, data, "Версия импортирована из GitHub!", done);
        } else {
            if (!form.elements.zip_file.files.length) { panelAlert('Выберите ZIP-архив с кодом.'); return; }
            await postForm(form, `/api/blueprints/${blueprintId}/artifacts`, new FormData(form), "Версия успешно загружена!", done);
        }
    }

    // Скачивание загруженной версии: authenticated fetch -> blob (JWT в заголовке,
    // прямая ссылка <a> его не передаёт).
    async function handleArtifactDownload(bpId, artId, filename) {
        try {
            const resp = await fetch(`/api/blueprints/${bpId}/artifacts/${artId}/download`, { headers: { 'Authorization': `Bearer ${getToken()}` } });
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const blob = await resp.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url; a.download = filename;
            document.body.appendChild(a); a.click(); a.remove();
            URL.revokeObjectURL(url);
        } catch (err) { panelAlert(`Ошибка скачивания: ${err.message}`); }
    }
    // «~40 с» / «~3 мин» / «~1 ч» — оценка оставшегося времени (Ночь 14).
    function fmtEta(sec) {
        if (sec == null) return '';
        if (sec < 90) return `~${Math.max(Math.round(sec / 10) * 10, 10)} с`;
        const m = Math.round(sec / 60);
        return m < 90 ? `~${m} мин` : `~${Math.round(m / 60)} ч`;
    }
    // Полоса живого прогресса сборки/пулла (Ночь 14, ADR-082): стадия + процент +
    // ETA по средним прошлых сборок. percent=null → неопределённая анимация.
    function buildStripHTML(build) {
        if (!build) return '';
        const pct = build.percent;
        const bar = pct == null
            ? `<div class="svc-build-bar indeterminate"><div class="svc-build-fill"></div></div>`
            : `<div class="svc-build-bar"><div class="svc-build-fill" style="width:${Math.max(3, Math.min(100, pct))}%"></div></div>`;
        const eta = build.eta_seconds != null ? ` · осталось ${fmtEta(build.eta_seconds)}` : '';
        return `<div class="svc-build">${bar}<div class="svc-build-text">${escapeHTML(build.detail || 'Сборка…')}${eta}</div></div>`;
    }
    // Сигнатура карточки сервиса: только рендерящиеся поля (инвариант Ночи 12);
    // ETA сборки огрубляем до 15 с — иначе карточка пересоздавалась бы каждый тик
    // (менялись бы только секунды) и закрывала открытое меню.
    function serviceSig(s) {
        const b = s.build ? {
            stage: s.build.stage, percent: s.build.percent, detail: s.build.detail,
            eta: s.build.eta_seconds != null ? Math.round(s.build.eta_seconds / 15) : null,
        } : null;
        return JSON.stringify({ ...s, build: b });
    }
    // Карточка сервиса: DOM-узел + обработчики ЕЁ кнопок (scoped — при точечной
    // перерисовке одной карточки остальные не переподписываются). Ночь 12, ADR-080.
    function buildServiceCard(srv) {
        const isOnline = srv.status === 'online';
        const card = document.createElement('div');
        card.className = 'service-card';
        card.dataset.serviceData = JSON.stringify(srv);
        card.innerHTML = ` <div class="service-info"> <div class="status-dot ${srv.status}"></div> <div> <div class="service-name">${escapeHTML(srv.name)}</div> <div class="service-meta">Port: ${srv.assigned_port} &bull; ${escapeHTML(srv.artifact.version_tag)}</div>${buildStripHTML(srv.build)} </div> </div> <div class="service-actions"> <div class="dropdown"> <button class="icon-btn" data-toggle="dropdown"><span class="material-symbols-outlined">more_vert</span></button> <div class="dropdown-menu"> <div data-action="start" ${isOnline ? 'class="dropdown-item disabled"' : 'class="dropdown-item"'}><span class="material-symbols-outlined">play_arrow</span>Запустить</div> <div data-action="stop" ${!isOnline ? 'class="dropdown-item disabled"' : 'class="dropdown-item"'}><span class="material-symbols-outlined">stop</span>Остановить</div> <div data-action="restart" ${!isOnline ? 'class="dropdown-item disabled"' : 'class="dropdown-item"'}><span class="material-symbols-outlined">refresh</span>Перезапустить</div> <div class="dropdown-item" data-action="redeploy"><span class="material-symbols-outlined">cached</span>Обновить / Откатить</div> <div class="dropdown-item" data-action="config"><span class="material-symbols-outlined">tune</span>Настройки сборки</div> <div data-action="publish" ${isOnline ? 'class="dropdown-item"' : 'class="dropdown-item disabled"'}><span class="material-symbols-outlined">public</span>Опубликовать</div> <hr style="border-color: var(--border); margin: 4px 8px;"> <div class="dropdown-item danger" data-action="delete"><span class="material-symbols-outlined">delete</span>Удалить сервис</div> </div> </div> </div>`;
        card.addEventListener('click', e => { if (!e.target.closest('.service-actions')) openDetailDrawer('svc', srv.id); });
        const dropdownToggle = card.querySelector('[data-toggle="dropdown"]');
        const dropdownMenu = card.querySelector('.dropdown-menu');
        dropdownToggle.addEventListener('click', e => { e.stopPropagation(); document.querySelectorAll('.dropdown-menu.show').forEach(m => m !== dropdownMenu && m.classList.remove('show')); dropdownMenu.classList.toggle('show'); });
        dropdownMenu.addEventListener('click', e => { e.stopPropagation(); const item = e.target.closest('.dropdown-item'); if (item && !item.classList.contains('disabled')) { handleServiceAction(item.dataset.action, srv, card); dropdownMenu.classList.remove('show'); } });
        return card;
    }
    async function loadAndDisplayServices() {
        const container = document.getElementById('servicesContainer');
        if (!container) return;
        try {
            const services = await fetchData('services', '/api/services');
            updateRailCount('services');
            // Каркас (заголовок + host карточек) создаётся один раз; дальше — только дифф.
            let title = container.querySelector(':scope > .section-title');
            let host = container.querySelector(':scope > [data-services-host]');
            if (!title || !host) {
                container.innerHTML = `<div class="section-title"></div><div data-services-host></div>`;
                title = container.querySelector(':scope > .section-title');
                host = container.querySelector(':scope > [data-services-host]');
            }
            title.textContent = `Все сервисы (${services.length})`;
            if (services.length === 0) {
                if (host.dataset.empty !== '1') { host.dataset.empty = '1'; host.innerHTML = '<p style="color:var(--text-secondary); padding: 16px 0;">Нет запущенных сервисов.</p>'; }
                return;
            }
            delete host.dataset.empty;
            // Точечный рендер: пересоздаются только карточки с изменившимися данными —
            // индикаторы догоняют правду каждый тик, список не мигает. Сигнатура
            // без волатильных секунд ETA (см. serviceSig, Ночь 14).
            syncKeyedList(host, services, s => s.id, serviceSig, buildServiceCard);
        } catch (error) { if (getToken()) container.innerHTML = `<p style="color:var(--danger)">Ошибка загрузки сервисов.</p>`; }
    }
    async function handleServiceAction(action, service, card) { switch (action) { case 'start': case 'stop': case 'restart': card.querySelector('.status-dot').className = 'status-dot restarting'; card.dataset.sig = 'action-pending'; /* руками менял DOM → следующий дифф обязан пересоздать карточку */ await postJSON(null, `/api/services/${service.id}/${action}`, null, `Действие '${action}' выполнено.`, () => { invalidateCache('services'); loadAndDisplayServices(); }); break; case 'redeploy': showModal('redeployModal', () => prepareRedeployModal(service)); break; case 'config': showModal('configModal', () => prepareConfigModal(service)); break; case 'publish': goToStage('applications', () => showModal('applicationModal', () => prepareApplicationModal({ serviceId: service.id }))); break; case 'delete': if (await panelConfirm(`Вы уверены, что хотите ПОЛНОСТЬЮ удалить сервис "${service.name}"?\n\nЭто действие необратимо и приведет к удалению контейнера.`, { danger: true })) { await deleteJSON(`/api/services/${service.id}`, "Сервис успешно удален.", () => { invalidateCache('services'); loadAndDisplayServices(); }); } break; } }
    async function handleCreateService(e) { e.preventDefault(); const form = e.target; const data = formToJSON(form); data.artifact_id = parseInt(data.artifact_id); await postJSON(form, '/api/services', data, "Сервис успешно запущен!", () => { hideModal('serviceModal'); invalidateCache('services'); loadAndDisplayServices(); }); }
    // Потоковый редеплой с ЖИВЫМ WS-логом сборки (ADR-023). Зеркалит issueSslForDomain.
    function streamRedeploy(serviceId, artifactId, logWindow) {
        const append = (text) => { logWindow.style.display = 'block'; logWindow.innerHTML += `\n${escapeHTML(text)}`; logWindow.scrollTop = logWindow.scrollHeight; };
        logWindow.style.display = 'block'; logWindow.innerHTML = '<span class="log-info">Запуск сборки новой версии…</span>';
        return new Promise((resolve, reject) => {
            postJSON(null, `/api/services/${serviceId}/redeploy-stream`, { artifact_id: artifactId })
                .then(response => {
                    const ws = new WebSocket(`${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/api/services/ws/redeploy/${response.task_id}`);
                    ws.onmessage = (event) => { if (event.data === "CLOSE_CONNECTION") ws.close(); else append(event.data); };
                    ws.onerror = () => { append('Ошибка WebSocket.'); reject(new Error('WS error')); };
                    ws.onclose = () => resolve();
                })
                .catch(reject);
        });
    }
    async function handleRedeployService(e) {
        e.preventDefault();
        const form = e.target;
        const serviceId = form.elements.service_id.value;
        const artifactId = parseInt(form.elements.artifact_id.value, 10);
        const btn = form.querySelector('button[type="submit"]');
        const logWindow = document.getElementById('redeployLogWindow');
        btn.disabled = true;
        try {
            await streamRedeploy(serviceId, artifactId, logWindow);  // живой лог сборки; модалку не закрываем, чтобы прочитать итог
        } catch (err) { /* лог уже показан */ }
        finally {
            btn.disabled = false;
            invalidateCache('services'); loadAndDisplayServices();
        }
    }
    // Строка приложения: DOM-узел + обработчики (scoped) — для точечного рендера.
    function buildAppRow(app) {
        const proto = app.ssl_cert_name ? 'https' : 'http';
        const sslCell = app.ssl_cert_name
            ? `<span class="material-symbols-outlined inline-ico" style="color:var(--success)">lock</span> ${escapeHTML(app.ssl_cert_name)}`
            : '<span class="material-symbols-outlined inline-ico" style="color:var(--text-secondary)">no_encryption</span> HTTP';
        const issueBtn = app.ssl_cert_name ? '' : `<button class="btn-icon-label issue-app-ssl-btn" title="Выпустить Let's Encrypt SSL"><span class="material-symbols-outlined">shield_lock</span>Выпустить SSL</button>`;
        const tpl = document.createElement('template');
        tpl.innerHTML = `<tr class="app-row clickable-row" data-app-id="${app.id}" title="Открыть детали приложения">
            <td>${escapeHTML(app.name)}</td>
            <td><a href="${proto}://${app.domain}" target="_blank">${escapeHTML(app.domain)}</a>${copyBtnHTML(`${proto}://${app.domain}`, 'Скопировать адрес')}</td>
            <td><span class="mono">${escapeHTML(app.service.name)}</span></td>
            <td>${sslCell}</td>
            <td><span class="action-row end">
                ${issueBtn}
                <button class="icon-btn edit-app-btn" title="Редактировать (домен/SSL)"><span class="material-symbols-outlined">edit</span></button>
                <button class="icon-btn danger del-app-btn" title="Удалить (снять с публикации)"><span class="material-symbols-outlined">delete</span></button>
            </span></td>
        </tr>`;
        const tr = tpl.content.firstElementChild;
        // Клик по строке (не по кнопке/ссылке) открывает тот же боковой drawer, что и на
        // дашборде — детальный просмотр опубликованного приложения (запрос пользователя).
        tr.addEventListener('click', (e) => {
            if (e.target.closest('button, a')) return;
            openDetailDrawer('app', app.id);
        });
        const del = tr.querySelector('.del-app-btn');
        if (del) del.onclick = () => handleApplicationDelete(app.id, app.name);
        const edit = tr.querySelector('.edit-app-btn');
        if (edit) edit.onclick = () => openEditApplicationModal(app);
        const issue = tr.querySelector('.issue-app-ssl-btn');
        if (issue) issue.onclick = () => handleIssueAppSsl(app.id, app.domain);
        return tr;
    }
    async function loadAndDisplayApplications() {
        const tableBody = document.querySelector('#appsTable tbody');
        if (!tableBody) return;
        try {
            const apps = await fetchData('applications', '/api/applications');
            updateRailCount('applications');
            if (apps.length === 0) {
                if (tableBody.dataset.empty !== '1') { tableBody.dataset.empty = '1'; tableBody.innerHTML = `<tr><td colspan="5" style="text-align:center; color: var(--text-secondary);">Нет опубликованных приложений.</td></tr>`; }
                return;
            }
            delete tableBody.dataset.empty;
            // Точечный рендер (Ночь 12): страница живая — фоновый выпуск SSL сам
            // «зазеленит» строку без F5, при этом таблица не мигает на поллинге.
            syncKeyedList(tableBody, apps, a => a.id, a => JSON.stringify(a), buildAppRow);
        } catch (error) { if (getToken()) { delete tableBody.dataset.empty; tableBody.innerHTML = `<tr><td colspan="5" style="color:var(--danger)">Ошибка загрузки приложений.</td></tr>`; } }
    }
    async function handleApplicationDelete(appId, appName) { if (!await panelConfirm(`Вы уверены, что хотите удалить приложение (снять с публикации) "${appName}"?\n\nЭто действие НЕ остановит работающий сервис, а только уберет публичный доступ к нему.`, { danger: true })) return; await deleteJSON(`/api/applications/${appId}`, "Приложение успешно удалено.", () => { invalidateCache('applications'); loadAndDisplayApplications(); }); }
    async function openEditApplicationModal(app) {
        const form = document.getElementById('editApplicationForm');
        form.reset();
        form.elements.app_id.value = app.id;
        document.getElementById('editAppName').value = app.name;
        form.elements.domain.value = app.domain;
        attachDnsCheck(form.elements.domain);
        const sslSelect = form.querySelector('select[name="ssl_cert_name"]');
        const certs = await fetchData('certs', '/api/ssl/certificates');
        populateSelect(sslSelect, certs, c => c.name, c => c.name, true);
        sslSelect.value = app.ssl_cert_name || '';
        showModal('editApplicationModal');
    }
    async function handleEditApplication(e) {
        e.preventDefault();
        const form = e.target;
        const appId = form.elements.app_id.value;
        const data = { domain: form.elements.domain.value, ssl_cert_name: form.elements.ssl_cert_name.value || null };
        await postJSON(form, `/api/applications/${appId}`, data, "Приложение обновлено! Nginx перезагружается...", () => { hideModal('editApplicationModal'); invalidateCache('applications'); loadAndDisplayApplications(); }, 'PATCH');
    }
    // Выпуск SSL для уже опубликованного приложения — в ФОН (Ночь 10): задача сама
    // дождётся DNS, выпустит сертификат и привяжет его. Страницу можно закрыть.
    async function handleIssueAppSsl(appId, domain) {
        if (!await panelConfirm(`Выпустить Let's Encrypt сертификат для "${domain}" в фоне?\n\nЗадача сама дождётся распространения DNS и выпустит сертификат — можно закрыть страницу и следить в центре задач.`)) return;
        try {
            await postJSON(null, '/api/pending-actions/issue-ssl', { domain, app_id: appId });
            openTaskCenter();
            tcToast('Выпуск SSL запущен в фоне. Следите за ходом в центре задач.');
        } catch (err) { /* сообщение уже показано */ }
    }
    async function handleCreateApplication(e) {
        e.preventDefault();
        const form = e.target;
        const raw = formToJSON(form);
        const mode = raw.ssl_mode;
        const domainMode = document.getElementById('appDomainGroup').dataset.mode || 'manual';
        const domain = currentAppDomain();
        if (!domain) { panelAlert('Укажите домен (или субдомен и зону).'); return; }
        const serviceId = parseInt(raw.service_id);
        const name = (raw.name || '').trim();

        // Долгий путь (ждать распространения DNS до суток / выпускать SSL) уводим в ФОН —
        // модалку не держим (Ночь 10, инвариант №7). Сервер сам доведёт до HTTPS.
        const needsAsync = (domainMode === 'picker') || (mode === 'issue');
        if (needsAsync) {
            const body = {
                service_id: serviceId, domain, name: name || null, ssl_mode: mode,
                existing_cert: mode === 'existing' ? (raw.ssl_cert_name || null) : null,
            };
            if (domainMode === 'picker') {
                body.zone = document.getElementById('appZoneSelect').value;
                body.subdomain = document.getElementById('appSubdomainInput').value.trim();
            }
            const submitBtn = form.querySelector('button[type="submit"]');
            submitBtn.disabled = true;
            try {
                await postJSON(null, '/api/pending-actions/publish', body);
            } catch (err) { submitBtn.disabled = false; return; }  // ошибка уже показана
            submitBtn.disabled = false;
            hideModal('applicationModal');
            invalidateCache('applications'); loadAndDisplayApplications();
            openTaskCenter();
            tcToast('Публикация запущена в фоне. Следите за ходом в центре задач (кнопка справа внизу) — можно закрыть страницу.');
            return;
        }

        // Быстрый путь (без SSL или готовый сертификат на введённом домене) — как раньше,
        // синхронно: тут ждать нечего.
        let appName = name;
        if (!appName) {
            const srv = (CACHE.services || []).find(s => s.id === serviceId);
            appName = uniqueName((srv && srv.name) || 'app', (CACHE.applications || []).map(a => a.name));
        }
        const data = { name: appName, domain, service_id: serviceId, ssl_cert_name: mode === 'existing' ? (raw.ssl_cert_name || null) : null };
        await postJSON(form, '/api/applications', data, `Приложение опубликовано! Доступно: ${data.ssl_cert_name ? 'https' : 'http'}://${domain}`, () => { hideModal('applicationModal'); invalidateCache('applications'); loadAndDisplayApplications(); });
    }
    // Срок серта в UI (Ночь 16, ADR-085): дата + «через N дн.» с подсветкой
    // (≤14 — красный алерт, ≤30 — жёлтый «продлевается»), ручные серты — пометка.
    // Просроченный (days_left < 0) — не «через -104 дн.», а «истёк N дн. назад».
    function certValidityCell(cert) {
        const date = new Date(cert.not_after).toLocaleDateString();
        const d = cert.days_left;
        if (d === null || d === undefined) return date;
        let color = 'var(--text-secondary)', note = `через ${d} дн.`;
        if (d < 0) { color = 'var(--danger)'; note = `истёк ${Math.abs(d)} дн. назад!`; }
        else if (d <= 14) { color = 'var(--danger)'; note = `через ${d} дн. — истекает!`; }
        else if (d <= 30) { color = 'var(--warning, #fbbc04)'; note = cert.auto_renew ? `через ${d} дн. — продлевается` : `через ${d} дн.`; }
        const manual = cert.auto_renew ? '' : ` <span style="color:var(--text-secondary)">· ручной</span>`;
        return `${date} <span style="color:${color}">(${note})</span>${manual}`;
    }
    async function loadAndDisplayCerts() { const tableBody = document.querySelector('#certsTable tbody'); try { const certs = await fetchData('certs', '/api/ssl/certificates'); if (certs.length === 0) { tableBody.innerHTML = `<tr><td colspan="4" style="text-align:center; color:var(--text-secondary);">Сертификаты не найдены.</td></tr>`; return; } tableBody.innerHTML = certs.map(cert => `<tr><td><span class="mono">${escapeHTML(cert.name)}</span></td><td>${escapeHTML(cert.subject)}</td><td>${certValidityCell(cert)}</td><td><button class="icon-btn danger" data-cert-name="${escapeHTML(cert.name)}"><span class="material-symbols-outlined">delete</span></button></td></tr>`).join(''); tableBody.querySelectorAll('[data-cert-name]').forEach(btn => { btn.onclick = () => handleCertDelete(btn.dataset.certName); }); } catch (error) { if (getToken()) tableBody.innerHTML = `<tr><td colspan="4" style="color:var(--danger)">Ошибка загрузки.</td></tr>`; } }
    async function checkDns(domain) {
        const response = await fetch(`/api/ssl/check-dns?domain=${encodeURIComponent(domain)}`, { headers: { 'Authorization': `Bearer ${getToken()}` } });
        if (response.status === 401) { clearToken(); showLoginScreen(); throw new Error('Session expired'); }
        if (!response.ok) throw new Error('Ошибка проверки DNS');
        return response.json();
    }
    // Живой DNS-индикатор прямо в поле домена (debounce). status = .dns-inline рядом с input.
    function attachDnsCheck(input) {
        if (!input) return;
        const status = input.parentElement.querySelector('.dns-inline');
        if (!status) return;
        let timer = null;
        input.oninput = () => {
            clearTimeout(timer);
            const domain = input.value.trim();
            if (!domain || !domain.includes('.')) { status.className = 'dns-inline'; status.textContent = ''; return; }
            const ico = name => `<span class="material-symbols-outlined inline-ico">${name}</span>`;
            status.className = 'dns-inline checking'; status.innerHTML = `${ico('hourglass_empty')} Проверка DNS…`;
            timer = setTimeout(async () => {
                try {
                    const d = await checkDns(domain);
                    if (d.matches && d.warning) { status.className = 'dns-inline warn'; status.innerHTML = `${ico('warning')} Указывает сюда (${escapeHTML(d.server_ip || '')}), но есть лишние A-записи: ${escapeHTML((d.domain_ips||[]).filter(ip=>ip!==d.server_ip).join(', '))} — удали их, иначе SSL не выпустится`; }
                    else if (d.matches) { status.className = 'dns-inline good'; status.innerHTML = `${ico('check_circle')} Домен указывает на этот сервер (${escapeHTML(d.server_ip || '')})`; }
                    else { status.className = 'dns-inline bad'; status.innerHTML = `${ico('error')} Не указывает на сервер · домен: ${escapeHTML(d.domain_ip || 'нет A-записи')} · сервер: ${escapeHTML(d.server_ip || '?')}`; }
                } catch (e) { status.className = 'dns-inline'; status.textContent = ''; }
            }, 600);
        };
    }
    async function handleCertUpload(e) { e.preventDefault(); await postForm(e.target, '/api/ssl/certificates', new FormData(e.target), "Сертификат загружен!", () => { e.target.reset(); invalidateCache('certs'); loadAndDisplayCerts(); }); }
    async function handleCertDelete(certName) { if (!await panelConfirm(`Удалить сертификат "${certName}"?`, { danger: true })) return; await deleteJSON(`/api/ssl/certificates/${certName}`, "Сертификат удален.", () => { invalidateCache('certs'); loadAndDisplayCerts(); }); }
    // Общий поток выпуска Let's Encrypt: POST /issue → WebSocket-лог → проверка появления
    // сертификата. Резолвится при успехе, реджектится если сертификат не появился.
    // logWindow — опциональный DOM-элемент для трансляции логов.
    function issueSslForDomain(domain, logWindow = null) {
        const append = (text, cls) => { if (!logWindow) return; logWindow.style.display = 'block'; logWindow.innerHTML += `\n${cls ? `<span class="log-${cls}">${escapeHTML(text)}</span>` : escapeHTML(text)}`; logWindow.scrollTop = logWindow.scrollHeight; };
        if (logWindow) { logWindow.style.display = 'block'; logWindow.innerHTML = `<span class="log-info">Запуск выпуска для ${escapeHTML(domain)}...</span>`; }
        return new Promise((resolve, reject) => {
            postJSON(null, '/api/ssl/issue', { domain })
                .then(response => {
                    const { task_id } = response;
                    const ws = new WebSocket(`${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/api/ssl/ws/issue-ssl/${task_id}`);
                    ws.onopen = () => append('Соединение с лог-сервером установлено.', 'success');
                    ws.onmessage = (event) => { if (event.data === "CLOSE_CONNECTION") ws.close(); else append(event.data); };
                    ws.onerror = (err) => { console.error("WebSocket Error:", err); append('Ошибка WebSocket.', 'error'); reject(new Error('Ошибка WebSocket при выпуске SSL.')); };
                    ws.onclose = async () => {
                        // Проверяем, что сертификат реально появился (certbot мог завершиться ошибкой).
                        invalidateCache('certs');
                        try {
                            const certs = await fetchData('certs', '/api/ssl/certificates');
                            if (certs.find(c => c.name === domain)) { append('Сертификат успешно выпущен.', 'success'); resolve(); }
                            else { append('Сертификат не был выпущен (см. лог выше).', 'error'); reject(new Error('Сертификат не выпущен. Проверьте DNS и логи.')); }
                        } catch (e) { reject(e); }
                    };
                })
                .catch(reject);
        });
    }
    async function handleSslIssue(e) {
        e.preventDefault();
        const form = e.target;
        const btn = form.querySelector('button[type="submit"]');
        const domain = form.elements.domain.value;
        const logWindow = document.getElementById('sslLogWindow');
        btn.disabled = true;
        try { await issueSslForDomain(domain, logWindow); }
        catch (err) { /* лог уже показан */ }
        finally { btn.disabled = false; invalidateCache('certs'); loadAndDisplayCerts(); }
    }
    async function loadAndDisplayGroups() { const tableBody = document.querySelector('#groupsTable tbody'); try { const groups = await fetchData('groups', '/api/groups'); if (groups.length === 0) { tableBody.innerHTML = `<tr><td colspan="3" style="text-align:center; color:var(--text-secondary);">Группы не созданы.</td></tr>`; return; } tableBody.innerHTML = groups.map(g => `<tr><td>${escapeHTML(g.name)}</td><td><span class="mono">${g.start_port} - ${g.end_port}</span></td><td><button class="icon-btn danger" data-group-id="${g.id}" data-group-name="${g.name}"><span class="material-symbols-outlined">delete</span></button></td></tr>`).join(''); tableBody.querySelectorAll('[data-group-id]').forEach(btn => { btn.onclick = () => handleGroupDelete(btn.dataset.groupId, btn.dataset.groupName); }); } catch (error) { if (getToken()) tableBody.innerHTML = `<tr><td colspan="3" style="color:var(--danger)">Ошибка загрузки.</td></tr>`; } }
    async function handleCreateGroup(e) { e.preventDefault(); const form = e.target; const data = formToJSON(form); if (parseInt(data.start_port) >= parseInt(data.end_port)) { panelAlert("Начальный порт должен быть меньше конечного."); return; } await postJSON(form, '/api/groups', data, "Группа создана!", () => { invalidateCache('groups'); loadAndDisplayGroups(); form.reset(); }); }
    async function handleGroupDelete(groupId, groupName) { if (!await panelConfirm(`Удалить группу "${groupName}"?`, { danger: true })) return; await deleteJSON(`/api/groups/${groupId}`, "Группа удалена.", () => { invalidateCache('groups'); loadAndDisplayGroups(); }); }
    async function prepareServiceModal(prefill = null) {
        const form = document.getElementById('serviceForm');
        form.reset();
        const bpSelect = form.querySelector('#select_blueprint'), artSelect = form.querySelector('select[name="artifact_id"]'), groupSelect = form.querySelector('select[name="group_name"]'), nameInput = form.elements.name;
        delete nameInput.dataset.touched;
        nameInput.oninput = () => { nameInput.dataset.touched = '1'; };
        const blueprints = await fetchData('blueprints', '/api/blueprints');
        const services = await fetchData('services', '/api/services');
        populateSelect(bpSelect, blueprints, bp => bp.name, bp => bp.id);
        const fillArtifacts = () => { const selectedBp = blueprints.find(bp => bp.id == bpSelect.value); populateSelect(artSelect, selectedBp ? selectedBp.artifacts.slice().reverse() : [], art => `${art.version_tag} (${new Date(art.created_at).toLocaleDateString()})`, art => art.id); };
        // Автоген имени сервиса из имени приложения (если пользователь не редактировал поле).
        const suggestName = () => { const bp = blueprints.find(b => b.id == bpSelect.value); if (bp && !nameInput.dataset.touched) nameInput.value = uniqueName(bp.name, (services || []).map(s => s.name)); };
        bpSelect.onchange = () => { fillArtifacts(); suggestName(); };
        const groups = await fetchData('groups', '/api/groups');
        populateSelect(groupSelect, groups, g => `${g.name} (${g.start_port}-${g.end_port})`, g => g.name);
        // Проброс контекста из Библиотеки: предвыбрать приложение и версию.
        if (prefill && prefill.blueprintId) {
            bpSelect.value = prefill.blueprintId;
            fillArtifacts();
            if (prefill.artifactId) artSelect.value = prefill.artifactId;
        }
        suggestName();
        // Каждое открытие — расширенная сборка свёрнута (узкое окно), активен шаблон Python.
        const modalEl = document.getElementById('serviceModal').querySelector('.modal');
        modalEl.classList.remove('has-advanced');
        const at = modalEl.querySelector('[data-toggle-advanced]'); if (at) at.setAttribute('aria-expanded', 'false');
        modalEl.querySelectorAll('.preset-chip').forEach((c, i) => c.classList.toggle('active', i === 0));
    }
    // Режим ввода домена в модалке публикации (ADR-057): пикер «из готового» ↔ ручной.
    function setAppDomainMode(mode) {
        const group = document.getElementById('appDomainGroup');
        const picker = document.getElementById('appDomainPicker');
        const manual = document.getElementById('appDomainManual');
        const form = document.getElementById('applicationForm');
        group.dataset.mode = mode;
        picker.style.display = mode === 'picker' ? 'block' : 'none';
        manual.style.display = mode === 'manual' ? 'block' : 'none';
        form.elements.domain.required = (mode === 'manual');
        // Субдомен в пикере необязателен: пусто = публикация на сам домен (apex, Задача 2).
        document.getElementById('appSubdomainInput').required = false;
    }
    // Текущий выбранный домен модалки публикации (независимо от режима).
    // В пикере пустой субдомен = сам домен зоны (публикация на ПОЛНЫЙ домен).
    function currentAppDomain() {
        const mode = document.getElementById('appDomainGroup').dataset.mode || 'manual';
        if (mode === 'picker') {
            const sub = document.getElementById('appSubdomainInput').value.trim();
            const zone = document.getElementById('appZoneSelect').value;
            if (!zone) return '';
            return sub ? `${sub}.${zone}` : zone;
        }
        return document.getElementById('applicationForm').elements.domain.value.trim();
    }
    async function prepareApplicationModal(prefill = null) {
        const form = document.getElementById('applicationForm');
        form.reset();
        const serviceSelect = form.querySelector('select[name="service_id"]');
        const sslSelect = form.querySelector('select[name="ssl_cert_name"]');
        const nameInput = form.elements.name;
        const modeSelect = document.getElementById('appSslMode');
        const existingGroup = document.getElementById('appSslExistingGroup');
        const issueHint = document.getElementById('appSslIssueHint');
        const logWindow = document.getElementById('appSslLogWindow');

        delete nameInput.dataset.touched;
        nameInput.oninput = () => { nameInput.dataset.touched = '1'; };
        attachDnsCheck(form.elements.domain);

        // Доменный пикер «из готового» (ADR-057): зоны пушит контрол-плейн.
        // Есть зоны → дефолт пикер (максимум готового); нет → только ручной ввод.
        const dnsReqStatus = document.getElementById('appDnsRequestStatus');
        dnsReqStatus.textContent = 'A-запись создадим автоматически при публикации.';
        let dnsZones = [];
        try {
            invalidateCache('dnsIntegration'); // зоны мог только что запушить ЛК
            const dns = await fetchData('dnsIntegration', '/api/integrations/dns');
            dnsZones = dns.zones || [];
        } catch (e) { dnsZones = []; }
        const zoneSelect = document.getElementById('appZoneSelect');
        populateSelect(zoneSelect, dnsZones.map(z => ({ z })), o => o.z, o => o.z);
        if (dnsZones.length) zoneSelect.value = dnsZones[0];
        document.getElementById('appDomainManualBtn').onclick = () => setAppDomainMode('manual');
        document.getElementById('appDomainPickerBtn').onclick = () => setAppDomainMode('picker');
        document.getElementById('appDomainPickerBtn').style.display = dnsZones.length ? 'inline' : 'none';
        setAppDomainMode(dnsZones.length ? 'picker' : 'manual');
        // Автоподстановка субдомена из имени приложения (пока пользователь не трогал поле).
        const subInput = document.getElementById('appSubdomainInput');
        delete subInput.dataset.touched;
        subInput.oninput = () => { subInput.dataset.touched = '1'; };

        const services = await fetchData('services', '/api/services');
        const applications = await fetchData('applications', '/api/applications');
        const onlineServices = services.filter(s => s.status === 'online');
        populateSelect(serviceSelect, onlineServices, srv => `${srv.name} (Port: ${srv.assigned_port})`, srv => srv.id);
        // Автоген имени приложения из имени выбранного сервиса (+ субдомен пикера из имени).
        const suggestName = () => {
            const srv = onlineServices.find(s => s.id == serviceSelect.value);
            if (srv && !nameInput.dataset.touched) nameInput.value = uniqueName(srv.name, (applications || []).map(a => a.name));
            if (nameInput.value && !subInput.dataset.touched) subInput.value = nameInput.value;
        };
        serviceSelect.onchange = suggestName;
        if (prefill && prefill.serviceId) serviceSelect.value = prefill.serviceId;
        suggestName();

        const certs = await fetchData('certs', '/api/ssl/certificates');
        populateSelect(sslSelect, certs, cert => cert.name, cert => cert.name, true);

        // Переключение режима SSL: none / existing / issue.
        modeSelect.value = 'none';
        existingGroup.style.display = 'none';
        issueHint.style.display = 'none';
        if (logWindow) { logWindow.style.display = 'none'; logWindow.innerHTML = ''; }
        modeSelect.onchange = () => {
            existingGroup.style.display = modeSelect.value === 'existing' ? 'block' : 'none';
            issueHint.style.display = modeSelect.value === 'issue' ? 'block' : 'none';
            sslSelect.required = modeSelect.value === 'existing';
        };
    }
    async function prepareRedeployModal(service) { const form=document.getElementById('redeployForm');form.reset();form.elements.service_id.value=service.id;document.getElementById('redeployServiceName').value=service.name;document.getElementById('redeployCurrentVersion').value=service.artifact.version_tag;const artifactSelect=form.querySelector('select[name="artifact_id"]');artifactSelect.innerHTML='<option disabled selected>Загрузка версий...</option>';const blueprints=await fetchData('blueprints','/api/blueprints');const serviceBlueprint=blueprints.find(bp=>bp.id===service.artifact.blueprint_id);if(serviceBlueprint){const currentId=service.artifact.id;const availableArtifacts=serviceBlueprint.artifacts.filter(art=>art.id!==currentId);
        // Размечаем направление относительно текущей версии: новее (id больше) =
        // обновление ↑, старее = откат ↓. id монотонен (артефакты создаются по порядку),
        // поэтому надёжнее даты. Делает откат на старую версию явным (DoD «откатываться»).
        const label=art=>`${art.version_tag} (${new Date(art.created_at).toLocaleDateString()}) — ${art.id>currentId?'↑ обновление':'↓ откат'}`;populateSelect(artifactSelect,availableArtifacts.slice().reverse(),label,art=>art.id);}else{artifactSelect.innerHTML='<option disabled selected>Не удалось найти версии</option>';} }
    // Расширенный режим (Идея 2а): редактор конфига сборки/рантайма сервиса.
    function prepareConfigModal(service) {
        const form = document.getElementById('configForm');
        form.reset();
        form.elements.service_id.value = service.id;
        form.elements.base_image.value = service.base_image || '';
        form.elements.run_command.value = service.run_command || '';
        form.elements.internal_port.value = (service.internal_port ?? 80);
        form.elements.env_vars.value = Object.entries(service.env_vars || {}).map(([k, v]) => `${k}=${v}`).join('\n');
    }
    async function handleServiceConfig(e) {
        e.preventDefault();
        const form = e.target;
        const id = form.elements.service_id.value;
        const data = {
            base_image: form.elements.base_image.value,
            run_command: form.elements.run_command.value,
            internal_port: form.elements.internal_port.value === '' ? null : parseInt(form.elements.internal_port.value, 10),
            env_vars: form.elements.env_vars.value,  // backend парсит KEY=VALUE построчно
        };
        await postJSON(form, `/api/services/${id}/config`, data, 'Конфиг применён, сервис пересоздаётся.', () => {
            hideModal('configModal'); invalidateCache('services'); loadAndDisplayServices();
        }, 'PATCH');
    }
    // URL для «Открыть в браузере»: прокси-роут деплоера на первую публикацию сервиса.
    // Работает и локально, и на сервере (тот же хост панели), без зависимости от DNS.
    function serviceOpenUrl(service) {
        const app = (service.applications || [])[0];
        return app ? `${window.location.origin}/api/proxy/${encodeURIComponent(app.name)}/` : null;
    }
    function wireServiceOpenButton(btn, service) {
        if (!btn) return;
        const url = serviceOpenUrl(service);
        if (url) {
            btn.disabled = false;
            btn.title = 'Открыть сервис через прокси деплоера';
            btn.onclick = () => window.open(url, '_blank', 'noopener');
        } else {
            // Инвариант №9 (Ночь 12): невозможное действие — честный disabled с
            // причиной, а не активная кнопка с alert-сюрпризом.
            btn.disabled = true;
            btn.title = 'Сервис ещё не опубликован — нажмите «Опубликовать», кнопка включится сама';
            btn.onclick = null;
        }
    }

    // Детали сервиса показываются в едином drawer (как на дашборде) — openDetailDrawer('svc', id).
    // Прежняя встроенная панель удалена; живое обновление drawer — в pollServiceDrawer.

    // --- Live-обновление UI (поллинг): статусы/индикаторы и логи без переклика. ---
    // Один постоянный таймер; на каждом тике сам решает по текущей странице, что
    // обновлять. Не мешает модалкам/дропдаунам/скрытой вкладке.
    let pollTimer = null, lastServicesSig = '', lastCertsSig = '';
    const POLL_MS = 4000;
    const startPolling = () => { stopPolling(); pollTimer = setInterval(pollTick, POLL_MS); };
    const stopPolling = () => { if (pollTimer) clearInterval(pollTimer); pollTimer = null; };
    // Перерисовать то, что сейчас на экране, из свежего кэша (после invalidate).
    // Зовётся при завершении фоновой задачи и после закрытия модалок (Ночь 12).
    function refreshCurrentPage() {
        const page = window.location.hash.replace('#', '') || 'services';
        if (currentStage === 'services' && document.getElementById('servicesContainer')) loadAndDisplayServices();
        if (currentStage === 'applications' && document.querySelector('#appsTable tbody')) loadAndDisplayApplications();
        if (document.querySelector('#certsTable tbody')) { lastCertsSig = ''; loadAndDisplayCerts(); }  // SSL-вкладка Настроек
        if (page === 'dashboard') { invalidateCache('systemMetrics'); renderDashboard(); }
    }

    // --- Центр фоновых задач (Ночь 10, ADR-069) ---
    // Долгие операции (публикация с DNS-ожиданием, выпуск SSL) идут в фоне на сервере
    // (модель PendingAction), а UI лишь показывает их статус и уведомляет о результате.
    let taskCenterOpen = false;
    let taskCenterTab = 'tasks';   // 'tasks' | 'notifs' — активная вкладка панели
    let taskCenterKnown = {};   // id -> последний виденный статус (для тостов-уведомлений)
    const TASK_STATUS_RU = { pending: 'в очереди', running: 'выполняется', done: 'готово', error: 'ошибка' };
    const NOTIF_MAX = 100;      // сколько последних системных уведомлений храним в истории
    let notifHistory = loadNotifHistory();

    function loadNotifHistory() {
        try { return JSON.parse(localStorage.getItem('tcNotifHistory') || '[]'); } catch (_) { return []; }
    }
    function saveNotifHistory() {
        try { localStorage.setItem('tcNotifHistory', JSON.stringify(notifHistory.slice(0, NOTIF_MAX))); } catch (_) { /* переполнение — не критично */ }
    }

    function initTaskCenter() {
        const tc = document.getElementById('taskCenter');
        if (!tc) return;
        tc.hidden = false;
        taskCenterKnown = {};  // на новом входе не тостим исторические задачи (заполнится молча)
        document.getElementById('taskCenterToggle').onclick = () => {
            taskCenterOpen = !taskCenterOpen;
            document.getElementById('taskCenterPanel').hidden = !taskCenterOpen;
            if (taskCenterOpen) { refreshTaskCenter(); renderNotifs(); }
        };
        document.getElementById('taskCenterClose').onclick = () => {
            taskCenterOpen = false;
            document.getElementById('taskCenterPanel').hidden = true;
        };
        tc.querySelectorAll('.tc-tab').forEach(t => t.onclick = () => switchTaskCenterTab(t.dataset.tcTab));
        const clearBtn = document.getElementById('tcNotifClear');
        if (clearBtn) clearBtn.onclick = () => { notifHistory = []; saveNotifHistory(); renderNotifs(); updateNotifBadge(); };
        switchTaskCenterTab('tasks');
        updateNotifBadge();
        refreshTaskCenter();
    }
    function switchTaskCenterTab(tab) {
        taskCenterTab = tab;
        document.querySelectorAll('#taskCenter .tc-tab').forEach(t => t.classList.toggle('active', t.dataset.tcTab === tab));
        const list = document.getElementById('taskCenterList');
        const notifs = document.getElementById('taskCenterNotifs');
        if (list) list.hidden = tab !== 'tasks';
        if (notifs) notifs.hidden = tab !== 'notifs';
        if (tab === 'notifs') renderNotifs();
    }
    function teardownTaskCenter() {
        const tc = document.getElementById('taskCenter');
        if (tc) tc.hidden = true;
        taskCenterOpen = false;
        taskCenterKnown = {};
        const panel = document.getElementById('taskCenterPanel');
        if (panel) panel.hidden = true;
    }
    function openTaskCenter() {
        const panel = document.getElementById('taskCenterPanel');
        if (!panel) return;
        taskCenterOpen = true;
        panel.hidden = false;
        refreshTaskCenter();
    }
    async function refreshTaskCenter() {
        if (!getToken()) return;
        let actions;
        try {
            const r = await fetch('/api/pending-actions', { headers: { 'Authorization': `Bearer ${getToken()}` } });
            if (!r.ok) return;
            actions = await r.json();
        } catch (_) { return; }
        renderTaskCenter(actions);
        notifyTaskChanges(actions);
    }
    const taskIsActive = a => a.status === 'pending' || a.status === 'running';
    function renderTaskCenter(actions) {
        const badge = document.getElementById('taskCenterBadge');
        const toggle = document.getElementById('taskCenterToggle');
        const active = actions.filter(taskIsActive).length;
        if (badge) { badge.hidden = actions.length === 0; badge.textContent = String(active || actions.length); }
        if (toggle) toggle.classList.toggle('busy', active > 0);
        if (!taskCenterOpen) return;  // тело перерисовываем только когда панель открыта
        const list = document.getElementById('taskCenterList');
        if (!list) return;
        if (actions.length === 0) {
            list.innerHTML = `<div class="task-center-empty">Фоновых задач нет.<br>Долгие операции (публикация, выпуск SSL) появятся здесь и доведутся до конца сами.</div>`;
            return;
        }
        // Сохраняем, какие журналы были раскрыты, чтобы перерисовка их не схлопнула.
        const openLogs = new Set([...list.querySelectorAll('details[data-log-id]')].filter(d => d.open).map(d => d.dataset.logId));
        list.innerHTML = actions.map(a => taskItemHTML(a, openLogs.has(String(a.id)))).join('');
        list.querySelectorAll('[data-retry]').forEach(b => b.onclick = () => retryTask(b.dataset.retry));
        list.querySelectorAll('[data-dismiss]').forEach(b => b.onclick = () => dismissTask(b.dataset.dismiss));
    }
    function taskResultHTML(a) {
        if (!a.result) return '';
        const body = (a.status === 'done' && /^https?:\/\//.test(a.result))
            ? `<a href="${escapeHTML(a.result)}" target="_blank" rel="noopener">${escapeHTML(a.result)}</a>${copyBtnHTML(a.result, 'Скопировать адрес')}`
            : escapeHTML(a.result);
        return `<div class="task-item-result">${body}</div>`;
    }
    function taskItemHTML(a, logOpen) {
        const icon = taskIsActive(a)
            ? '<span class="task-spinner"></span>'
            : `<span class="material-symbols-outlined" style="color:var(--${a.status === 'done' ? 'success' : 'danger'})">${a.status === 'done' ? 'check_circle' : 'error'}</span>`;
        const btns = [];
        if (a.status === 'error') btns.push(`<button class="btn-icon-label" data-retry="${a.id}"><span class="material-symbols-outlined">refresh</span>Повторить</button>`);
        if (!taskIsActive(a)) btns.push(`<button class="btn-icon-label" data-dismiss="${a.id}"><span class="material-symbols-outlined">close</span>Убрать</button>`);
        const log = a.log ? `<details class="task-item-log" data-log-id="${a.id}"${logOpen ? ' open' : ''}><summary>Журнал</summary><pre>${escapeHTML(a.log)}</pre></details>` : '';
        // Стадия активной задачи + честный ETA (Ночь 14): для DNS вместо числа —
        // вилка «от минут до суток» (фаза вне нашего контроля, ADR-066/082).
        let stage = '';
        if (taskIsActive(a) && a.stage_label) {
            const eta = a.unpredictable
                ? escapeHTML(a.hint || 'от минут до суток')
                : (a.eta_seconds != null ? `осталось ${fmtEta(a.eta_seconds)}` : '');
            stage = `<div class="task-item-stage"><span class="task-stage-chip">${escapeHTML(a.stage_label)}</span>${eta ? `<span class="task-stage-eta">${eta}</span>` : ''}</div>`;
        }
        return `<div class="task-item">
            <div class="task-item-head">${icon}<span class="task-item-title" title="${escapeHTML(a.title || '')}">${escapeHTML(a.title || a.type)}</span><span class="task-item-status ${a.status}">${TASK_STATUS_RU[a.status] || a.status}</span></div>
            ${stage}
            ${taskResultHTML(a)}
            ${log}
            ${btns.length ? `<div class="task-item-actions">${btns.join('')}</div>` : ''}
        </div>`;
    }
    async function retryTask(id) {
        try { await fetch(`/api/pending-actions/${id}/retry`, { method: 'POST', headers: { 'Authorization': `Bearer ${getToken()}` } }); } catch (_) { /* тихо */ }
        refreshTaskCenter();
    }
    async function dismissTask(id) {
        try { await fetch(`/api/pending-actions/${id}`, { method: 'DELETE', headers: { 'Authorization': `Bearer ${getToken()}` } }); } catch (_) { /* тихо */ }
        refreshTaskCenter();
    }
    // Тост при смене статуса задачи на завершённый — «уведомление о результате» (DoD Ночи 10).
    function notifyTaskChanges(actions) {
        const firstLoad = Object.keys(taskCenterKnown).length === 0;
        if (!firstLoad) {
            actions.forEach(a => {
                const prev = taskCenterKnown[a.id];
                if (prev && prev !== a.status && (a.status === 'done' || a.status === 'error')) {
                    const ok = a.status === 'done';
                    tcToast(`${a.title || 'Задача'}: ${a.result || (ok ? 'готово' : 'ошибка')}`, ok ? 'success' : 'error');
                    // Результат фоновой задачи меняет мир (публикация/SSL/привязка) —
                    // текущая страница обязана узнать САМА, без F5 (инвариант №9).
                    invalidateCache('applications', 'certs', 'services');
                    refreshCurrentPage();
                }
            });
        }
        taskCenterKnown = Object.fromEntries(actions.map(a => [a.id, a.status]));
    }
    function tcToast(msg, kind) {
        pushNotif(msg, kind);  // любое уведомление попадает в историю (последние 100)
        let wrap = document.getElementById('tcToastWrap');
        if (!wrap) { wrap = document.createElement('div'); wrap.id = 'tcToastWrap'; wrap.className = 'tc-toast-wrap'; document.body.appendChild(wrap); }
        const el = document.createElement('div');
        el.className = `tc-toast ${kind || ''}`;
        el.innerHTML = `<span class="tc-toast-msg"></span><button type="button" class="tc-toast-close" aria-label="Закрыть"><span class="material-symbols-outlined">close</span></button>`;
        el.querySelector('.tc-toast-msg').textContent = msg;
        const dismiss = () => { el.style.transition = 'opacity .3s'; el.style.opacity = '0'; setTimeout(() => el.remove(), 300); };
        el.querySelector('.tc-toast-close').onclick = dismiss;  // × — быстро скипнуть уведомление
        wrap.appendChild(el);
        setTimeout(dismiss, 6000);
    }
    // --- История системных уведомлений (запрос пользователя): последние 100, вкладка «Уведомления» ---
    function pushNotif(msg, kind) {
        notifHistory.unshift({ t: Date.now(), msg: String(msg), kind: kind || '' });
        if (notifHistory.length > NOTIF_MAX) notifHistory.length = NOTIF_MAX;
        saveNotifHistory();
        updateNotifBadge();
        if (taskCenterOpen && taskCenterTab === 'notifs') renderNotifs();
    }
    function updateNotifBadge() {
        const c = document.getElementById('tcNotifCount');
        if (!c) return;
        c.hidden = notifHistory.length === 0;
        c.textContent = String(notifHistory.length);
    }
    function renderNotifs() {
        const list = document.getElementById('tcNotifList');
        if (!list) return;
        if (!notifHistory.length) {
            list.innerHTML = `<div class="tc-notifs-empty">Уведомлений пока нет.<br>Здесь копятся системные события (публикация, SSL, ошибки).</div>`;
            return;
        }
        list.innerHTML = notifHistory.map(n => `
            <div class="tc-notif ${n.kind || ''}">
                <span class="tc-notif-dot"></span>
                <div class="tc-notif-body">
                    <div class="tc-notif-msg">${escapeHTML(n.msg)}</div>
                    <div class="tc-notif-time">${new Date(n.t).toLocaleString()}</div>
                </div>
            </div>`).join('');
    }

    async function pollTick() {
        if (document.hidden || !getToken()) return;
        refreshTaskCenter();  // фоновые задачи обновляем на любой странице (бейдж/уведомления)
        if (document.querySelector('.modal-backdrop.show')) return;  // не дёргаем во время модалок
        const page = window.location.hash.replace('#', '') || 'services';
        const onServices = currentStage === 'services' && document.getElementById('servicesContainer');
        const onApplications = currentStage === 'applications' && document.querySelector('#appsTable tbody');
        const onDashboard = page === 'dashboard';
        const onSsl = !!document.querySelector('#certsTable tbody');  // SSL-вкладка Настроек
        try {
            if (onServices || onDashboard) {
                invalidateCache('services');
                const services = await fetchData('services', '/api/services');
                const sig = services.map(s => `${s.id}:${s.status}:${s.assigned_port}:${(s.applications || []).length}`).sort().join('|');
                const changed = sig !== lastServicesSig;
                lastServicesSig = sig;
                const drawerOpen = document.getElementById('detailDrawer').classList.contains('show');
                if (onServices) {
                    // Keyed-дифф (Ночь 12): зовём каждый тик — перерисуются только карточки
                    // с изменившимися данными; открытые дропдауны/drawer не мешают правде.
                    loadAndDisplayServices();
                    pollServiceDrawer(services);  // живые логи/статы открытого drawer сервиса
                } else if (onDashboard) {
                    // Drawer открыт → живёт-обновляем его (а дашборд под ним не трогаем, чтобы не сбить zoom).
                    if (drawerOpen) pollServiceDrawer(services);
                    else {
                        if (changed) { invalidateCache('systemMetrics', 'metricsHistory'); renderDashboard(); }
                        // Здоровье хоста живёт своим диффом (Ночь 13): диск/память
                        // ползут независимо от сервисов — обновляем каждый тик
                        // (+ история для графиков, Ночь 19 — новая точка раз в минуту).
                        else { invalidateCache('hostHealth', 'metricsHistory'); loadHostHealth(); }
                    }
                }
            } else if (onApplications) {
                // Страница «Приложения» тоже живая (жалоба: SSL выпущен в фоне, а страница
                // «не знала» до F5). Дифф-рендер — обновление без миганий.
                invalidateCache('applications');
                await fetchData('applications', '/api/applications');
                loadAndDisplayApplications();
            } else if (onSsl) {
                // Список сертификатов: фоновый выпуск добавляет строки — тоже без F5.
                invalidateCache('certs');
                const certs = await fetchData('certs', '/api/ssl/certificates');
                const csig = JSON.stringify(certs);
                if (csig !== lastCertsSig) { lastCertsSig = csig; loadAndDisplayCerts(); }
            }
        } catch (_) { /* тихо: следующий тик повторит */ }
    }
    // Вкладка снова видима → немедленно догоняем правду (пропущенные тики).
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden && getToken()) pollTick();
    });
    // prep может быть как async (возвращает Promise), так и СИНХРОННЫМ (prepareConfigModal
    // возвращает undefined). Раньше безусловный prep().catch() падал на undefined.catch
    // → модалка «Настройки сборки» вообще не открывалась. Теперь терпимо к обоим случаям.
    const showModal = (id, prep) => {
        if (prep) {
            try {
                const r = prep();
                if (r && typeof r.then === 'function') r.catch(err => console.error(`Modal prep failed for ${id}`, err));
            } catch (err) { console.error(`Modal prep failed for ${id}`, err); }
        }
        document.getElementById(id).classList.add('show');
    };
    const hideModal = id => {
        document.getElementById(id).classList.remove('show');
        // Модалка блокировала тики (см. pollTick) — закрылась → сразу догоняем правду.
        setTimeout(() => { try { pollTick(); } catch (_) { /* тихо */ } }, 250);
    };

    // --- Кастомные диалоги подтверждения/уведомления (ADR-092) ---------------- //
    // Панель встроена в iframe ЛК с sandbox БЕЗ allow-modals → нативные
    // confirm()/alert()/prompt() браузер молча игнорирует («Ignored call to
    // 'confirm()'. The document is sandboxed…») и подтверждение не срабатывает.
    // Свой промис-based диалог рисуется в DOM панели и работает внутри sandbox
    // без ослабления его keyword-ов. Реюзаем .modal-backdrop/.modal (style.css).
    // Текст сообщения — ТОЛЬКО через textContent (никакого innerHTML): любой
    // недоверенный ввод (имена сервисов/приложений/доменов) не может пробить XSS.
    let panelDialogEl = null;
    function panelDialog({ message, confirmText, cancelText, danger }) {
        return new Promise(resolve => {
            if (!panelDialogEl) {
                panelDialogEl = document.createElement('div');
                panelDialogEl.className = 'modal-backdrop';
                panelDialogEl.id = 'panelDialog';
                panelDialogEl.innerHTML =
                    '<div class="modal" style="max-width:440px">'
                    + '<div class="modal-header"><h3 class="modal-title" id="panelDialogTitle">Подтверждение</h3></div>'
                    + '<p id="panelDialogMsg" style="white-space:pre-wrap; margin:4px 0 4px; color:var(--text-secondary)"></p>'
                    + '<div class="modal-footer">'
                    + '<button type="button" class="btn btn-secondary" id="panelDialogCancel">Отмена</button>'
                    + '<button type="button" class="btn btn-primary" id="panelDialogOk">OK</button>'
                    + '</div></div>';
                document.body.appendChild(panelDialogEl);
            }
            const okBtn = panelDialogEl.querySelector('#panelDialogOk');
            const cancelBtn = panelDialogEl.querySelector('#panelDialogCancel');
            // Текст — строго textContent (без innerHTML): анти-XSS для имён/доменов.
            panelDialogEl.querySelector('#panelDialogMsg').textContent = String(message == null ? '' : message);
            okBtn.textContent = confirmText || 'OK';
            okBtn.className = 'btn ' + (danger ? 'btn-danger' : 'btn-primary');
            cancelBtn.textContent = cancelText || 'Отмена';
            cancelBtn.style.display = cancelText === null ? 'none' : '';  // null → режим alert (одна кнопка)

            let done = false;
            const close = result => {
                if (done) return;
                done = true;
                panelDialogEl.classList.remove('show');
                document.removeEventListener('keydown', onKey, true);
                okBtn.onclick = cancelBtn.onclick = panelDialogEl.onclick = null;
                resolve(result);
            };
            const onKey = e => {
                if (e.key === 'Escape') { e.preventDefault(); close(false); }
                else if (e.key === 'Enter') { e.preventDefault(); close(true); }
            };
            okBtn.onclick = () => close(true);
            cancelBtn.onclick = () => close(false);
            // Клик по затемнению (вне .modal) = отмена, как у нативного confirm.
            panelDialogEl.onclick = e => { if (e.target === panelDialogEl) close(false); };
            document.addEventListener('keydown', onKey, true);
            panelDialogEl.classList.add('show');
            okBtn.focus();
        });
    }
    // panelConfirm(text) → Promise<bool>: замена нативного confirm() в sandbox.
    const panelConfirm = (message, opts = {}) => panelDialog({ message, confirmText: opts.confirmText || 'Да', cancelText: opts.cancelText || 'Отмена', danger: opts.danger });
    // panelAlert(text) → Promise<void>: замена нативного alert() (одна кнопка).
    const panelAlert = message => panelDialog({ message, confirmText: 'OK', cancelText: null });
    // Экспорт для panel_ai.js (отдельный IIFE-виджет ИИ-помощника).
    window.panelConfirm = panelConfirm;
    window.panelAlert = panelAlert;

    // --- Глобальные Обработчики и Запуск ---
    // Пресет-чипы расширенной сборки: заполняют базовый образ / команду / порт в форме.
    // data-image/cmd/port пусты для «Python (FastAPI)» → возврат к автогену по умолчанию.
    function applyBuildPreset(chip) {
        const form = chip.closest('form');
        const presets = chip.closest('.build-presets');
        if (!form) return;
        if (form.elements.base_image) form.elements.base_image.value = chip.dataset.image || '';
        if (form.elements.run_command) form.elements.run_command.value = chip.dataset.cmd || '';
        if (form.elements.internal_port) form.elements.internal_port.value = chip.dataset.port || '';
        if (presets) presets.querySelectorAll('.preset-chip').forEach(c => c.classList.toggle('active', c === chip));
    }

    document.addEventListener('click', e => {
        const presetChip = e.target.closest('.preset-chip[data-preset]');
        if (presetChip) { applyBuildPreset(presetChip); return; }
        // Раскрытие расширенной сборки ВБОК: модалка становится шире (второй столбец),
        // а не растёт вниз со скроллом (по фидбэку). Класс на .modal управляет шириной/гридом.
        const advToggle = e.target.closest('[data-toggle-advanced]');
        if (advToggle) {
            const modal = advToggle.closest('.modal');
            const open = modal.classList.toggle('has-advanced');
            advToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
            return;
        }
        if (e.target.closest('[data-close-modal]')) hideModal(e.target.closest('.modal-backdrop').id);
        if (e.target.closest('[data-close-drawer]') || e.target.id === 'detailDrawer') closeDrawer();
        if (!e.target.closest('.dropdown')) { document.querySelectorAll('.dropdown-menu.show').forEach(menu => menu.classList.remove('show')); }
        // Закрыть открытую историю версий (overlay) по клику вне неё.
        if (!e.target.closest('.lib-history')) {
            document.querySelectorAll('.lib-versions-rest:not([hidden])').forEach(list => {
                list.setAttribute('hidden', '');
                const t = list.previousElementSibling;
                if (t) { t.classList.remove('open'); const tx = t.querySelector('.lvt-text'); if (tx) tx.textContent = `Ещё версий: ${list.children.length}`; }
            });
        }
        const tab = e.target.closest('.tab');
        if (tab && tab.closest('.tab-bar')) {
            const bar = tab.closest('.tab-bar');
            const name = tab.dataset.tab;
            bar.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t === tab));
            bar.parentElement.querySelectorAll(':scope > .tab-panel').forEach(p => p.classList.toggle('active', p.dataset.panel === name));
        }
    });
    document.querySelectorAll('.nav-item').forEach(link => link.onclick = e => { if (e.currentTarget.id === 'logoutBtn') return; e.preventDefault(); const page = e.currentTarget.dataset.page; if (window.location.hash !== `#${page}`) window.location.hash = page; });
    window.addEventListener('hashchange', () => navigate(window.location.hash.substring(1) || 'services'));

    // SSO из ЛК (ADR-034 Phase 1): токен приходит во fragment `#sso_token=…` — фрагмент
    // НЕ уходит на сервер (нет в логах прокси). Подхватываем, чистим адрес, дальше — как
    // обычный вход. Активно только когда фрагмент присутствует (иначе no-op).
    (() => {
        const m = window.location.hash.match(/[#&]sso_token=([^&]+)/);
        if (m) {
            setToken(decodeURIComponent(m[1]));
            history.replaceState(null, '', window.location.pathname + window.location.search);
        }
    })();

    // Embedded-режим (ADR-092): панель открыта в iframe ЛК (`?embedded=1`) — прячем
    // дублирующее обрамление (логотип и «Выйти»: бренд и сессия там — у ЛК), чтобы не
    // было «сайта в сайте». Навигация панели остаётся. Флаг запоминается в
    // sessionStorage (переживает hash-переходы/перезагрузку внутри iframe и живёт
    // только в этой вкладке) — standalone-режим по прямому домену не затрагивается.
    (() => {
        const inQuery = /[?&]embedded=1(&|$)/.test(window.location.search);
        let embedded = inQuery;
        try {
            if (inQuery) sessionStorage.setItem('panelEmbedded', '1');
            embedded = embedded || sessionStorage.getItem('panelEmbedded') === '1';
        } catch (_) { /* приватный режим/запрет стораджа — хватит и query */ }
        if (embedded) document.body.classList.add('embedded');
    })();

    // --- Инициализация Приложения ---
    enhancePasswordInputs(document);  // «глаз» у пароля формы входа
    if (getToken()) {
        showApp();
    } else {
        showLoginScreen();
    }
});