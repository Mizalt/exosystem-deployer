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
    let CACHE = { groups: null, blueprints: null, services: null, applications: null, certs: null };
    const templates = {
        dashboard: `<header><h1>Дашборд</h1></header><div class="page-content"><p style="color:var(--text-secondary)">В разработке.</p></div>`,
        blueprints: `
            <header><h1>Библиотека приложений</h1><button class="btn-primary" id="newBlueprintBtn"><span class="material-symbols-outlined">add</span>Создать приложение</button></header>
            <div class="page-content"><div id="blueprintsList" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(400px, 1fr)); gap: 16px;"><p>Загрузка...</p></div></div>`,
        services: `
            <header><h1>Сервисы (Запущенные контейнеры)</h1><button class="btn-primary" id="newServiceBtn"><span class="material-symbols-outlined">rocket_launch</span>Запустить сервис</button></header>
            <div class="content-area">
                <div class="services-list" id="servicesContainer"><div class="section-title">Загрузка...</div></div>
                <div class="details-panel" id="detailsPanel">
                    <div class="details-content">
                        <div style="display: flex; justify-content: space-between; align-items: center;"><h2 id="detName">Детали</h2><button class="icon-btn" id="closeDetailsBtn"><span class="material-symbols-outlined">close</span></button></div>
                        <div id="detStatus" style="font-size: 14px; margin-bottom: 8px;"></div>
                        <div id="detArtifactInfo" style="font-size: 14px; color: var(--text-secondary);"></div>
                        <div class="stat-grid"><div class="stat-card"><div class="stat-card-label">CPU</div><div class="stat-card-value" id="statCpu">--</div></div><div class="stat-card"><div class="stat-card-label">Memory</div><div class="stat-card-value" id="statMemory">--</div></div></div>
                        <div class="section-title">Консоль</div><div class="log-window" id="logWindow">...</div>
                    </div>
                </div>
            </div>`,
        applications: `
            <header><h1>Приложения (Публичные домены)</h1><button class="btn-primary" id="newAppBtn"><span class="material-symbols-outlined">public</span>Опубликовать сервис</button></header>
            <div class="page-content"><div class="settings-card"><table class="styled-table" id="appsTable"><thead><tr><th>Имя приложения</th><th>Домен</th><th>Указывает на сервис</th><th>SSL</th><th>Действия</th></tr></thead><tbody><tr><td colspan="5" style="text-align:center; color: var(--text-secondary);">Загрузка...</td></tr></tbody></table></div></div>`,
        ssl: `
            <header><h1 style="font-weight: 400; font-size: 28px;">Управление SSL сертификатами</h1></header>
            <div class="page-content page-grid">
                <div>
                    <div class="settings-card"><h3>Существующие сертификаты</h3><table class="styled-table" id="certsTable"><thead><tr><th>Имя (каталог)</th><th>Домен (CN)</th><th>Действителен до</th><th>Действия</th></tr></thead><tbody><tr><td colspan="4" style="text-align: center; color: var(--text-secondary);">Загрузка...</td></tr></tbody></table></div>
                    <div class="settings-card"><h3>Выпустить сертификат Let's Encrypt</h3><p style="color: var(--text-secondary); font-size: 14px; margin-bottom: 16px;">Убедитесь, что A-запись домена указывает на IP сервера. Используйте DNS-чекер для проверки.</p><form id="issueSslForm" class="settings-form"><input type="text" name="domain" placeholder="example.com" required><button type="submit" class="btn btn-primary deploy-action">Выпустить</button></form><div class="log-window" id="sslLogWindow" style="height: 250px; display: none; margin-top: 16px;"></div></div>
                </div>
                <div>
                     <div class="settings-card"><h3>Проверить DNS</h3><form id="checkDnsForm" class="settings-form"><input type="text" name="domain" placeholder="example.com" required><button type="submit" class="btn btn-primary">Проверить</button></form><div id="dnsResult"></div></div>
                    <div class="settings-card"><h3>Загрузить свой сертификат</h3><form id="uploadCertForm"><div class="form-group"><label>Имя для хранения</label><input type="text" name="name" placeholder="my-custom-cert" required pattern="^[a-zA-Z0-9._\\-]+$"></div><div class="form-group"><label>Файл сертификата (fullchain.pem)</label><input type="file" name="cert_file" required accept=".pem,.crt"></div><div class="form-group"><label>Приватный ключ (privkey.pem)</label><input type="file" name="key_file" required accept=".pem,.key"></div><button type="submit" class="btn btn-primary" style="margin-top: 8px;">Загрузить</button></form></div>
                </div>
            </div>`,
        settings: `
            <header><h1>Настройки</h1></header>
            <div class="page-content">
                <div class="settings-card">
                    <h3>Домен панели (Deployer)</h3>
                    <p style="color: var(--text-secondary); font-size: 14px; margin-bottom: 16px;">
                        Настройте основной адрес, по которому вы заходите в эту панель.
                    </p>
                    <form id="panelSettingsForm">
                        <div class="form-group">
                            <label>Домен панели</label>
                            <input type="text" name="domain" placeholder="panel.example.com">
                        </div>
                        <div class="form-group">
                            <label>SSL Сертификат</label>
                            <select name="ssl_cert_name">
                                <option value="">Без SSL (HTTP)</option>
                            </select>
                        </div>
                        <button type="submit" class="btn btn-primary">Применить настройки домена</button>
                    </form>
                </div>
                <div class="page-grid">
                    <div>
                        <div class="settings-card">
                            <h3>Группы портов</h3><p style="color: var(--text-secondary); font-size: 14px; margin-bottom: 16px;">Группы определяют диапазоны портов для новых сервисов.</p><table class="styled-table" id="groupsTable"><thead><tr><th>Имя</th><th>Диапазон портов</th><th>Действия</th></tr></thead><tbody><tr><td colspan="3" style="text-align:center; color:var(--text-secondary)">Загрузка...</td></tr></tbody></table>
                        </div>
                    </div>
                    <div>
                        <div class="settings-card">
                            <h3>Создать новую группу</h3><form id="createGroupForm"><div class="form-group"><label>Имя группы</label><input type="text" name="name" placeholder="backend-services" required></div><div class="form-group"><label>Начальный порт</label><input type="number" name="start_port" placeholder="9001" required></div><div class="form-group"><label>Конечный порт</label><input type="number" name="end_port" placeholder="9010" required></div><button type="submit" class="btn btn-primary" style="margin-top: 8px;">Создать</button></form>
                        </div>
                    </div>
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
        const initialPage = window.location.hash.substring(1) || 'services';
        if (!window.location.hash) window.location.replace('#' + initialPage);
        navigate(initialPage);
    };

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
        clearToken();
        invalidateCache('groups', 'blueprints', 'services', 'applications', 'certs');
        showLoginScreen();
    });

    // --- Роутер ---
    const navigate = (page) => {
        mainContent.innerHTML = templates[page] || `<p>Страница не найдена</p>`;
        document.querySelectorAll('.nav-item').forEach(link => link.classList.toggle('active', link.dataset.page === page));
        const initFunctions = { blueprints: initBlueprintsPage, services: initServicesPage, applications: initApplicationsPage, ssl: initSslPage, settings: initSettingsPage };
        if (initFunctions[page]) initFunctions[page]();
    };

    // --- Инициализация Страниц ---
    const initBlueprintsPage = () => { document.getElementById('newBlueprintBtn').onclick = handleNewBlueprint; document.getElementById('uploadArtifactForm').addEventListener('submit', handleUploadArtifact); loadAndDisplayBlueprints(); };
    const initServicesPage = () => { document.getElementById('newServiceBtn').onclick = () => showModal('serviceModal', prepareServiceModal); document.getElementById('serviceForm').addEventListener('submit', handleCreateService); document.getElementById('redeployForm').addEventListener('submit', handleRedeployService); document.getElementById('closeDetailsBtn').onclick = closeDetails; loadAndDisplayServices(); };
    const initApplicationsPage = () => { document.getElementById('newAppBtn').onclick = () => showModal('applicationModal', prepareApplicationModal); document.getElementById('applicationForm').addEventListener('submit', handleCreateApplication); loadAndDisplayApplications(); };
    const initSslPage = () => { document.getElementById('checkDnsForm').addEventListener('submit', handleDnsCheck); document.getElementById('uploadCertForm').addEventListener('submit', handleCertUpload); document.getElementById('issueSslForm').addEventListener('submit', handleSslIssue); loadAndDisplayCerts(); };
    const initSettingsPage = async () => { document.getElementById('createGroupForm').addEventListener('submit', handleCreateGroup); const panelForm = document.getElementById('panelSettingsForm'); const certSelect = panelForm.querySelector('select[name="ssl_cert_name"]'); const settings = await fetchData('panelSettings', '/api/panel/settings'); panelForm.elements.domain.value = settings.domain || ''; const certs = await fetchData('certs', '/api/ssl/certificates'); populateSelect(certSelect, certs, c => c.name, c => c.name, true); certSelect.value = settings.ssl_cert_name || ''; panelForm.onsubmit = async (e) => { e.preventDefault(); const data = formToJSON(panelForm); data.ssl_cert_name = data.ssl_cert_name || null; data.domain = data.domain || null; await postJSON(panelForm, '/api/panel/settings', data, "Настройки панели сохранены! Nginx перезагружается...", () => { if (data.domain && data.domain !== window.location.hostname) { alert(`Внимание! Панель теперь будет доступна по адресу: http${data.ssl_cert_name ? 's':''}://${data.domain}`); } }); }; loadAndDisplayGroups(); };

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
            if (successMsg) alert(successMsg);
            if (callback) callback();

            return json_body;
        } catch (err) {
            alert(`Ошибка: ${err.message}`);
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
    const populateSelect = (select, items, textFn, valueFn, keepFirst = false) => { select.innerHTML = keepFirst ? select.firstElementChild.outerHTML : '<option value="" disabled selected>Выберите...</option>'; items.forEach(item => { const opt = document.createElement('option'); opt.textContent = textFn(item); opt.value = valueFn(item); select.appendChild(opt); }); };
    const formToJSON = form => Object.fromEntries(new FormData(form).entries());
    const postJSON = (form, url, data, successMsg, callback) =>
        handleApiRequest(form, url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) }, successMsg, callback);    const postForm = (form, url, formData, successMsg, callback) => handleApiRequest(form, url, { method: 'POST', body: formData }, successMsg, callback);
    const deleteJSON = (url, successMsg, callback) => handleApiRequest(null, url, { method: 'DELETE' }, successMsg, callback);

    // --- Функции для каждой страницы (без изменений в логике, т.к. handleApiRequest всё делает) ---
    async function loadAndDisplayBlueprints() { const container = document.getElementById('blueprintsList'); try { const blueprints = await fetchData('blueprints', '/api/blueprints'); if (blueprints.length === 0) { container.innerHTML = `<p style="color:var(--text-secondary)">Библиотека пуста. Нажмите "Создать приложение", чтобы добавить первое.</p>`; return; } container.innerHTML = blueprints.map(bp => `<div class="settings-card"><div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px;"><h3 style="margin:0;">${escapeHTML(bp.name)}</h3><button class="btn btn-secondary upload-artifact-btn" data-id="${bp.id}" data-name="${escapeHTML(bp.name)}"><span class="material-symbols-outlined" style="font-size:18px">upload</span> Загрузить</button></div><p style="font-size:14px; color:var(--text-secondary); min-height:2em;">${escapeHTML(bp.description) || ''}</p><h5 style="margin-top:16px; margin-bottom:8px; font-size:13px; color:var(--text-secondary)">ВЕРСИИ (${bp.artifacts.length})</h5>${bp.artifacts.length > 0 ? `<ul style="list-style-type:none; font-size:14px;">${[...bp.artifacts].reverse().slice(0, 5).map(art => `<li style="padding:4px 0; border-bottom:1px solid var(--border)"><span class="mono">${escapeHTML(art.version_tag)}</span> <span style="float:right; color:var(--text-secondary)">${new Date(art.created_at).toLocaleDateString()}</span></li>`).join('')}</ul>` : '<p style="font-size:14px; color:var(--text-secondary)">Нет загруженных версий.</p>'}</div>`).join(''); document.querySelectorAll('.upload-artifact-btn').forEach(btn => btn.onclick = () => { const form = document.getElementById('uploadArtifactForm'); form.elements.blueprint_id.value = btn.dataset.id; document.getElementById('uploadBlueprintName').value = btn.dataset.name; showModal('uploadArtifactModal'); }); } catch (error) { if (getToken()) container.innerHTML = `<p style="color:var(--danger)">Ошибка загрузки библиотеки.</p>`; } }
    async function handleNewBlueprint() { const name = prompt("Имя приложения (a-z, 0-9, -):"); if (!name || !/^[a-z0-9-]+$/.test(name)) { if (name) alert("Неверный формат имени."); return; } const description = prompt("Краткое описание (необязательно):"); await postJSON(null, '/api/blueprints', { name, description }, "Приложение создано!", () => { invalidateCache('blueprints'); loadAndDisplayBlueprints(); }); }
    async function handleUploadArtifact(e) { e.preventDefault(); const form = e.target; const blueprintId = form.elements.blueprint_id.value; await postForm(form, `/api/blueprints/${blueprintId}/artifacts`, new FormData(form), "Версия успешно загружена!", () => { hideModal('uploadArtifactModal'); invalidateCache('blueprints'); loadAndDisplayBlueprints(); }); }
    async function loadAndDisplayServices() { const container = document.getElementById('servicesContainer'); try { const services = await fetchData('services', '/api/services'); container.innerHTML = `<div class="section-title">Все сервисы (${services.length})</div>`; if (services.length === 0) { container.innerHTML += '<p style="color:var(--text-secondary); padding: 16px 0;">Нет запущенных сервисов.</p>'; return; } services.forEach(srv => { const isOnline = srv.status === 'online'; const card = document.createElement('div'); card.className = 'service-card'; card.dataset.serviceData = JSON.stringify(srv); card.innerHTML = ` <div class="service-info"> <div class="status-dot ${srv.status}"></div> <div> <div class="service-name">${escapeHTML(srv.name)}</div> <div class="service-meta">Port: ${srv.assigned_port} &bull; v.${escapeHTML(srv.artifact.version_tag)}</div> </div> </div> <div class="service-actions"> <div class="dropdown"> <button class="icon-btn" data-toggle="dropdown"><span class="material-symbols-outlined">more_vert</span></button> <div class="dropdown-menu"> <div data-action="start" ${isOnline ? 'class="dropdown-item disabled"' : 'class="dropdown-item"'}><span class="material-symbols-outlined">play_arrow</span>Запустить</div> <div data-action="stop" ${!isOnline ? 'class="dropdown-item disabled"' : 'class="dropdown-item"'}><span class="material-symbols-outlined">stop</span>Остановить</div> <div data-action="restart" ${!isOnline ? 'class="dropdown-item disabled"' : 'class="dropdown-item"'}><span class="material-symbols-outlined">refresh</span>Перезапустить</div> <div class="dropdown-item" data-action="redeploy"><span class="material-symbols-outlined">cached</span>Обновить / Откатить</div> <hr style="border-color: var(--border); margin: 4px 8px;"> <div class="dropdown-item danger" data-action="delete"><span class="material-symbols-outlined">delete</span>Удалить сервис</div> </div> </div> </div>`; container.appendChild(card); card.addEventListener('click', e => { if (!e.target.closest('.service-actions')) openDetails(card, srv); }); const dropdownToggle = card.querySelector('[data-toggle="dropdown"]'); const dropdownMenu = card.querySelector('.dropdown-menu'); dropdownToggle.addEventListener('click', e => { e.stopPropagation(); document.querySelectorAll('.dropdown-menu.show').forEach(m => m !== dropdownMenu && m.classList.remove('show')); dropdownMenu.classList.toggle('show'); }); dropdownMenu.addEventListener('click', e => { e.stopPropagation(); const item = e.target.closest('.dropdown-item'); if (item && !item.classList.contains('disabled')) { handleServiceAction(item.dataset.action, srv, card); dropdownMenu.classList.remove('show'); } }); }); } catch (error) { if (getToken()) container.innerHTML = `<p style="color:var(--danger)">Ошибка загрузки сервисов.</p>`; } }
    async function handleServiceAction(action, service, card) { switch (action) { case 'start': case 'stop': case 'restart': card.querySelector('.status-dot').className = 'status-dot restarting'; await postJSON(null, `/api/services/${service.id}/${action}`, null, `Действие '${action}' выполнено.`, () => { invalidateCache('services'); loadAndDisplayServices(); if (document.getElementById('detailsPanel').classList.contains('open')) closeDetails(); }); break; case 'redeploy': showModal('redeployModal', () => prepareRedeployModal(service)); break; case 'delete': if (confirm(`Вы уверены, что хотите ПОЛНОСТЬЮ удалить сервис "${service.name}"?\n\nЭто действие необратимо и приведет к удалению контейнера.`)) { await deleteJSON(`/api/services/${service.id}`, "Сервис успешно удален.", () => { invalidateCache('services'); loadAndDisplayServices(); if (document.getElementById('detailsPanel').classList.contains('open')) closeDetails(); }); } break; } }
    async function handleCreateService(e) { e.preventDefault(); const form = e.target; const data = formToJSON(form); data.artifact_id = parseInt(data.artifact_id); await postJSON(form, '/api/services', data, "Сервис успешно запущен!", () => { hideModal('serviceModal'); invalidateCache('services'); loadAndDisplayServices(); }); }
    async function handleRedeployService(e) { e.preventDefault(); const form = e.target; const serviceId = form.elements.service_id.value; const data = { artifact_id: parseInt(form.elements.artifact_id.value) }; await postJSON(form, `/api/services/${serviceId}/redeploy`, data, "Сервис успешно обновлен!", () => { hideModal('redeployModal'); invalidateCache('services'); loadAndDisplayServices(); if (document.getElementById('detailsPanel').classList.contains('open')) closeDetails(); }); }
    async function loadAndDisplayApplications() { const tableBody = document.querySelector('#appsTable tbody'); try { const apps = await fetchData('applications', '/api/applications'); if (apps.length === 0) { tableBody.innerHTML = `<tr><td colspan="5" style="text-align:center; color: var(--text-secondary);">Нет опубликованных приложений.</td></tr>`; return; } tableBody.innerHTML = apps.map(app => `<tr><td>${escapeHTML(app.name)}</td><td><a href="https://${app.domain}" target="_blank">${app.domain}</a></td><td><span class="mono">${escapeHTML(app.service.name)}</span></td><td>${app.ssl_cert_name ? `✅ ${app.ssl_cert_name}` : '❌ HTTP'}</td><td><button class="icon-btn danger" data-app-id="${app.id}" data-app-name="${escapeHTML(app.name)}" title="Удалить (снять с публикации)"><span class="material-symbols-outlined">delete</span></button></td></tr>`).join(''); tableBody.querySelectorAll('[data-app-id]').forEach(btn => { btn.onclick = () => handleApplicationDelete(btn.dataset.appId, btn.dataset.appName); }); } catch (error) { if(getToken()) tableBody.innerHTML = `<tr><td colspan="5" style="color:var(--danger)">Ошибка загрузки приложений.</td></tr>`; } }
    async function handleApplicationDelete(appId, appName) { if (!confirm(`Вы уверены, что хотите удалить приложение (снять с публикации) "${appName}"?\n\nЭто действие НЕ остановит работающий сервис, а только уберет публичный доступ к нему.`)) return; await deleteJSON(`/api/applications/${appId}`, "Приложение успешно удалено.", () => { invalidateCache('applications'); loadAndDisplayApplications(); }); }
    async function handleCreateApplication(e) { e.preventDefault(); const form = e.target; const data = formToJSON(form); data.service_id = parseInt(data.service_id); data.ssl_cert_name = data.ssl_cert_name || null; await postJSON(form, '/api/applications', data, "Приложение успешно опубликовано!", () => { hideModal('applicationModal'); invalidateCache('applications'); loadAndDisplayApplications(); }); }
    async function loadAndDisplayCerts() { const tableBody = document.querySelector('#certsTable tbody'); try { const certs = await fetchData('certs', '/api/ssl/certificates'); if (certs.length === 0) { tableBody.innerHTML = `<tr><td colspan="4" style="text-align:center; color:var(--text-secondary);">Сертификаты не найдены.</td></tr>`; return; } tableBody.innerHTML = certs.map(cert => `<tr><td><span class="mono">${escapeHTML(cert.name)}</span></td><td>${escapeHTML(cert.subject)}</td><td>${new Date(cert.not_after).toLocaleDateString()}</td><td><button class="icon-btn danger" data-cert-name="${escapeHTML(cert.name)}"><span class="material-symbols-outlined">delete</span></button></td></tr>`).join(''); tableBody.querySelectorAll('[data-cert-name]').forEach(btn => { btn.onclick = () => handleCertDelete(btn.dataset.certName); }); } catch (error) { if (getToken()) tableBody.innerHTML = `<tr><td colspan="4" style="color:var(--danger)">Ошибка загрузки.</td></tr>`; } }
    async function handleDnsCheck(e) { e.preventDefault(); const form = e.target; const domain = form.elements.domain.value; const resultDiv = document.getElementById('dnsResult'); resultDiv.innerHTML = 'Проверка...'; try { const response = await fetch(`/api/ssl/check-dns?domain=${encodeURIComponent(domain)}`, { headers: { 'Authorization': `Bearer ${getToken()}` } }); if(response.status === 401) { clearToken(); showLoginScreen(); return; } const data = await response.json(); const isGood = data.matches; resultDiv.innerHTML = `<div class="dns-result ${isGood ? 'dns-result-good':'dns-result-bad'}"><p>${isGood ? '<b>Отлично!</b> A-запись домена указывает на IP сервера.' : (data.error || '<b>Внимание!</b> A-запись домена не указывает на IP сервера.')}</p><p style="font-size:13px; color:var(--text-secondary); margin-top:8px;">IP сервера: ${data.server_ip || 'N/A'} | IP домена: ${data.domain_ip || 'N/A'}</p></div>`; } catch(err) { resultDiv.innerHTML = `<p style="color:var(--danger)">Ошибка проверки DNS.</p>`} }
    async function handleCertUpload(e) { e.preventDefault(); await postForm(e.target, '/api/ssl/certificates', new FormData(e.target), "Сертификат загружен!", () => { e.target.reset(); invalidateCache('certs'); loadAndDisplayCerts(); }); }
    async function handleCertDelete(certName) { if (!confirm(`Удалить сертификат "${certName}"?`)) return; await deleteJSON(`/api/ssl/certificates/${certName}`, "Сертификат удален.", () => { invalidateCache('certs'); loadAndDisplayCerts(); }); }
    async function handleSslIssue(e) { e.preventDefault(); const form = e.target; const btn = form.querySelector('button[type="submit"]'); const domain = form.elements.domain.value; const logWindow = document.getElementById('sslLogWindow'); btn.disabled = true; logWindow.style.display = 'block'; logWindow.innerHTML = '<span class="log-info">Запуск процесса для ' + escapeHTML(domain) + '...</span>'; try { const response = await postJSON(form, '/api/ssl/issue', { domain: domain }); const { task_id } = response; const ws = new WebSocket(`${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/api/ssl/ws/issue-ssl/${task_id}`); ws.onopen = () => logWindow.innerHTML += '\n<span class="log-success">Соединение с лог-сервером установлено.</span>'; ws.onmessage = (event) => { if (event.data === "CLOSE_CONNECTION") ws.close(); else { logWindow.innerHTML += `\n${escapeHTML(event.data)}`; logWindow.scrollTop = logWindow.scrollHeight; } }; ws.onclose = () => { logWindow.innerHTML += '\n<span class="log-success">Процесс завершен. Обновление списка...</span>'; btn.disabled = false; invalidateCache('certs'); loadAndDisplayCerts(); }; ws.onerror = (err) => { logWindow.innerHTML += '\n<span class="log-error">Ошибка WebSocket.</span>'; console.error("WebSocket Error:", err); btn.disabled = false; }; } catch (err) { logWindow.innerHTML += `\n<span class="log-error">Ошибка: ${err.message}</span>`; btn.disabled = false; } }
    async function loadAndDisplayGroups() { const tableBody = document.querySelector('#groupsTable tbody'); try { const groups = await fetchData('groups', '/api/groups'); if (groups.length === 0) { tableBody.innerHTML = `<tr><td colspan="3" style="text-align:center; color:var(--text-secondary);">Группы не созданы.</td></tr>`; return; } tableBody.innerHTML = groups.map(g => `<tr><td>${escapeHTML(g.name)}</td><td><span class="mono">${g.start_port} - ${g.end_port}</span></td><td><button class="icon-btn danger" data-group-id="${g.id}" data-group-name="${g.name}"><span class="material-symbols-outlined">delete</span></button></td></tr>`).join(''); tableBody.querySelectorAll('[data-group-id]').forEach(btn => { btn.onclick = () => handleGroupDelete(btn.dataset.groupId, btn.dataset.groupName); }); } catch (error) { if (getToken()) tableBody.innerHTML = `<tr><td colspan="3" style="color:var(--danger)">Ошибка загрузки.</td></tr>`; } }
    async function handleCreateGroup(e) { e.preventDefault(); const form = e.target; const data = formToJSON(form); if (parseInt(data.start_port) >= parseInt(data.end_port)) { alert("Начальный порт должен быть меньше конечного."); return; } await postJSON(form, '/api/groups', data, "Группа создана!", () => { invalidateCache('groups'); loadAndDisplayGroups(); form.reset(); }); }
    async function handleGroupDelete(groupId, groupName) { if (!confirm(`Удалить группу "${groupName}"?`)) return; await deleteJSON(`/api/groups/${groupId}`, "Группа удалена.", () => { invalidateCache('groups'); loadAndDisplayGroups(); }); }
    async function prepareServiceModal() { const form=document.getElementById('serviceForm'),bpSelect=form.querySelector('#select_blueprint'),artSelect=form.querySelector('select[name="artifact_id"]'),groupSelect=form.querySelector('select[name="group_name"]');const blueprints=await fetchData('blueprints','/api/blueprints');populateSelect(bpSelect,blueprints,bp=>bp.name,bp=>bp.id);bpSelect.onchange=()=>{const selectedBp=blueprints.find(bp=>bp.id==bpSelect.value);populateSelect(artSelect,selectedBp?selectedBp.artifacts.slice().reverse():[],art=>`${art.version_tag} (${new Date(art.created_at).toLocaleDateString()})`,art=>art.id);artSelect.dispatchEvent(new Event('change'))};const groups=await fetchData('groups','/api/groups');populateSelect(groupSelect,groups,g=>`${g.name} (${g.start_port}-${g.end_port})`,g=>g.name); }
    async function prepareApplicationModal() { const form=document.getElementById('applicationModal'),serviceSelect=form.querySelector('select[name="service_id"]'),sslSelect=form.querySelector('select[name="ssl_cert_name"]');const services=await fetchData('services','/api/services');const onlineServices=services.filter(s=>s.status==='online');populateSelect(serviceSelect,onlineServices,srv=>`${srv.name} (Port: ${srv.assigned_port})`,srv=>srv.id);const certs=await fetchData('certs','/api/ssl/certificates');populateSelect(sslSelect,certs,cert=>cert.name,cert=>cert.name,true); }
    async function prepareRedeployModal(service) { const form=document.getElementById('redeployForm');form.reset();form.elements.service_id.value=service.id;document.getElementById('redeployServiceName').value=service.name;document.getElementById('redeployCurrentVersion').value=service.artifact.version_tag;const artifactSelect=form.querySelector('select[name="artifact_id"]');artifactSelect.innerHTML='<option disabled selected>Загрузка версий...</option>';const blueprints=await fetchData('blueprints','/api/blueprints');const serviceBlueprint=blueprints.find(bp=>bp.id===service.artifact.blueprint_id);if(serviceBlueprint){const availableArtifacts=serviceBlueprint.artifacts.filter(art=>art.id!==service.artifact.id);populateSelect(artifactSelect,availableArtifacts.slice().reverse(),art=>`${art.version_tag} (${new Date(art.created_at).toLocaleDateString()})`,art=>art.id);}else{artifactSelect.innerHTML='<option disabled selected>Не удалось найти версии</option>';} }
    function openDetails(card, service) { document.querySelectorAll('.service-card').forEach(c=>c.classList.remove('selected')); card.classList.add('selected'); const panel = document.getElementById('detailsPanel'); panel.dataset.serviceData = JSON.stringify(service); panel.classList.add('open'); panel.querySelector('#detName').textContent = service.name; panel.querySelector('#detStatus').textContent = `● ${service.status}`; panel.querySelector('#detArtifactInfo').textContent = `v.${escapeHTML(service.artifact.version_tag)}`; ['#statCpu','#statMemory','#logWindow'].forEach(sel=>panel.querySelector(sel).textContent='Загрузка...'); if(service.status==='online'){fetchData('stats',`/api/services/${service.id}/stats`).then(stats=>{panel.querySelector('#statCpu').textContent=`${stats.cpu_percent}%`;panel.querySelector('#statMemory').textContent=`${stats.memory_usage_mb} MB`;}).catch(()=>panel.querySelector('#statCpu').textContent='Ошибка');fetchData('logs', `/api/services/${service.id}/logs`).then(data=>{panel.querySelector('#logWindow').textContent=data.logs||'Логи пусты.';}).catch(()=>panel.querySelector('#logWindow').textContent='Не удалось загрузить.');} else{['#statCpu','#statMemory'].forEach(sel=>panel.querySelector(sel).textContent='N/A');panel.querySelector('#logWindow').textContent='Сервис остановлен.';} }
    const closeDetails = () => { document.getElementById('detailsPanel').classList.remove('open'); document.querySelectorAll('.service-card').forEach(c=>c.classList.remove('selected')); };
    const showModal = (id, prep) => { if (prep) prep().catch(err => console.error(`Modal prep failed for ${id}`, err)); document.getElementById(id).classList.add('show'); };
    const hideModal = id => document.getElementById(id).classList.remove('show');

    // --- Глобальные Обработчики и Запуск ---
    document.addEventListener('click', e => { if (e.target.closest('[data-close-modal]')) hideModal(e.target.closest('.modal-backdrop').id); if (!e.target.closest('.dropdown')) { document.querySelectorAll('.dropdown-menu.show').forEach(menu => menu.classList.remove('show')); } });
    document.querySelectorAll('.nav-item').forEach(link => link.onclick = e => { if (e.currentTarget.id === 'logoutBtn') return; e.preventDefault(); const page = e.currentTarget.dataset.page; if (window.location.hash !== `#${page}`) window.location.hash = page; });
    window.addEventListener('hashchange', () => navigate(window.location.hash.substring(1) || 'services'));

    // --- Инициализация Приложения ---
    if (getToken()) {
        showApp();
    } else {
        showLoginScreen();
    }
});