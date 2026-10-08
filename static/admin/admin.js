/* ============================================================================
   CallNotifier Admin — вся клиентская логика
   ============================================================================ */

'use strict';

// ============================================================================
// Утилиты
// ============================================================================

/** Простой fetch с JSON-ответом. Бросает исключение при не-2xx. */
async function apiFetch(url, options = {}) {
    const resp = await fetch(url, {
        credentials: 'same-origin',
        ...options,
        headers: {
            'Accept': 'application/json',
            ...(options.body ? { 'Content-Type': 'application/json' } : {}),
            ...(options.headers || {}),
        },
    });
    if (!resp.ok) {
        let detail = `HTTP ${resp.status}`;
        try {
            const err = await resp.json();
            if (err && err.error) detail = err.error;
        } catch (_) { /* ignore */ }
        throw new Error(detail);
    }
    const text = await resp.text();
    return text ? JSON.parse(text) : null;
}

/** Форматирует длительность (сек) в "1ч 23м 45с" или "12с". */
function fmtDuration(seconds) {
    if (seconds == null) return '—';
    seconds = Math.max(0, Math.floor(seconds));
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (h > 0) return `${h}ч ${m}м ${s}с`;
    if (m > 0) return `${m}м ${s}с`;
    return `${s}с`;
}

/** Форматирует timestamp (сек) в HH:MM:SS. */
function fmtTime(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString('ru-RU', { hour12: false });
}

/** Форматирует timestamp (сек) в DD.MM HH:MM:SS. */
function fmtDateTime(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    const dd = String(d.getDate()).padStart(2, '0');
    const mm = String(d.getMonth() + 1).padStart(2, '0');
    return `${dd}.${mm} ${d.toLocaleTimeString('ru-RU', { hour12: false })}`;
}

/** HTML-escape для безопасной вставки текста. */
function esc(s) {
    if (s == null) return '';
    return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// Служебные типы, которые не отображаются в UI админки.
// `warehouse` — генерируется только WarehouseManager'ом, пользовательских
// вызовов по нему не бывает. Если появятся ещё служебные — допишем сюда.
const INTERNAL_TYPES = ['warehouse'];

// ============================================================================
// Тосты
// ============================================================================

const toastContainer = document.getElementById('toast-container');

function toast(title, body = '', type = 'success', timeout = 3000) {
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.innerHTML = `
        <div class="toast-title">${esc(title)}</div>
        ${body ? `<div class="toast-body">${esc(body)}</div>` : ''}
    `;
    toastContainer.appendChild(el);
    setTimeout(() => {
        el.style.transition = 'opacity 0.3s, transform 0.3s';
        el.style.opacity = '0';
        el.style.transform = 'translateX(30px)';
        setTimeout(() => el.remove(), 300);
    }, timeout);
}

// ============================================================================
// Управление вкладками
// ============================================================================

const tabs = document.querySelectorAll('.tab');
const panels = document.querySelectorAll('.tab-panel');

let activeTab = 'dashboard';

tabs.forEach(tab => {
    tab.addEventListener('click', () => switchTab(tab.dataset.tab));
});

function switchTab(name) {
    activeTab = name;
    tabs.forEach(t => t.classList.toggle('active', t.dataset.tab === name));
    panels.forEach(p => p.classList.toggle('active', p.id === `panel-${name}`));

    // При переключении — сразу тянем данные для выбранного таба
    if (name === 'dashboard')   refreshDashboard();
    if (name === 'create')      refreshCreateForm();
    if (name === 'types')       refreshTypes();
    if (name === 'warehouses')  refreshWarehouses();
    if (name === 'logs')        refreshLogs();
}

// ============================================================================
// Хедер: часы и индикатор соединения
// ============================================================================

const headerTime = document.getElementById('header-time');
const connDot    = document.getElementById('conn-dot');
const headerHost = document.getElementById('header-host');

function tickClock() {
    const d = new Date();
    headerTime.textContent = d.toLocaleTimeString('ru-RU', { hour12: false });
}
setInterval(tickClock, 1000);
tickClock();

function setConnected(ok) {
    connDot.classList.toggle('disconnected', !ok);
    connDot.title = ok ? 'Соединение с сервером установлено' : 'Нет соединения с сервером';
}

// ============================================================================
// ТАБ 1: ДАШБОРД
// ============================================================================

const cardServer = document.getElementById('card-server');
const cardTts    = document.getElementById('card-tts');
const cardWh     = document.getElementById('card-wh');
const cardDb     = document.getElementById('card-db');

async function refreshDashboard() {
    try {
        const [status, calls] = await Promise.all([
            apiFetch('/admin/api/status'),
            apiFetch('/admin/api/calls'),
        ]);
        setConnected(true);
        renderStatusCards(status);
        renderCalls(calls);
    } catch (e) {
        setConnected(false);
        console.error('refreshDashboard:', e);
    }
}

function renderStatusCards(s) {
    // --- Сервер ---
    headerHost.textContent = `${s.server.host}:${s.server.port}`;
    cardServer.className = 'status-card ok';
    cardServer.querySelector('.status-card-value').textContent = 'Online';
    cardServer.querySelector('.status-card-sub').textContent = `Uptime: ${fmtDuration(s.server.uptime_seconds)}`;

    // --- TTS ---
    const tts = s.tts || {};
    if (!tts.model_loaded) {
        cardTts.className = 'status-card warn';
        cardTts.querySelector('.status-card-value').textContent = 'Загрузка…';
        cardTts.querySelector('.status-card-sub').textContent = 'Модель Silero TTS ещё не готова';
    } else if (tts.busy) {
        cardTts.className = 'status-card ok';
        cardTts.querySelector('.status-card-value').textContent = `Генерация (${tts.active_tasks})`;
        cardTts.querySelector('.status-card-sub').textContent = tts.last_generated_filename
            ? `Последний: ${tts.last_generated_filename.slice(0, 12)}…`
            : `Голос: ${tts.speaker}`;
    } else {
        cardTts.className = 'status-card ok';
        cardTts.querySelector('.status-card-value').textContent = 'Готов';
        cardTts.querySelector('.status-card-sub').textContent = tts.last_generated_time
            ? `Последний файл: ${fmtTime(tts.last_generated_time)}`
            : `Голос: ${tts.speaker}, ${tts.sample_rate} Hz`;
    }

    // --- Менеджер складов ---
    const wh = s.warehouse_manager || {};
    if (!wh.running) {
        cardWh.className = 'status-card warn';
        cardWh.querySelector('.status-card-value').textContent = 'Остановлен';
        cardWh.querySelector('.status-card-sub').textContent = '—';
    } else if (wh.exceeding_count > 0) {
        cardWh.className = 'status-card error';
        cardWh.querySelector('.status-card-value').textContent = `⚠ ${wh.exceeding_count} превыш.`;
        cardWh.querySelector('.status-card-sub').textContent = `Всего складов: ${wh.warehouses_count}`;
    } else {
        cardWh.className = 'status-card ok';
        cardWh.querySelector('.status-card-value').textContent = 'Норма';
        cardWh.querySelector('.status-card-sub').textContent = `Складов: ${wh.warehouses_count}`;
    }

    // --- БД ---
    const db = s.db || {};
    cardDb.className = 'status-card ok';
    cardDb.querySelector('.status-card-value').textContent = `${db.active_calls} вызовов`;
    cardDb.querySelector('.status-card-sub').textContent = `Типов: ${db.types_count}`;
}

function renderCalls(calls) {
    document.getElementById('calls-count').textContent = calls.length;
    const tbody = document.getElementById('calls-tbody');

    if (!calls.length) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty">Нет активных вызовов</td></tr>';
        return;
    }

    tbody.innerHTML = calls.map(c => {
        const elapsed = c.elapsed_min || 0;
        let cls = 'row-green';
        if (elapsed >= 15) cls = 'row-red';
        else if (elapsed >= 5) cls = 'row-orange';

        return `
            <tr class="${cls}">
                <td>${c.id}</td>
                <td>${esc(c.external_id)}</td>
                <td>${esc(c.type)}</td>
                <td>${esc(c.name)}</td>
                <td>${esc(c.equipment)}</td>
                <td>${elapsed} мин</td>
                <td>
                    <button class="btn btn-danger btn-mini" data-close-id="${c.id}">
                        Закрыть
                    </button>
                </td>
            </tr>
        `;
    }).join('');

    // Навешиваем обработчики на кнопки "Закрыть"
    tbody.querySelectorAll('[data-close-id]').forEach(btn => {
        btn.addEventListener('click', async () => {
            const id = btn.dataset.closeId;
            btn.disabled = true;
            try {
                await apiFetch(`/admin/api/calls/${id}/close`, { method: 'POST' });
                toast('Вызов закрыт', `ID=${id}`, 'success', 2000);
                refreshDashboard();
            } catch (e) {
                toast('Ошибка', e.message, 'error', 4000);
                btn.disabled = false;
            }
        });
    });
}

// ============================================================================
// ТАБ 2: СОЗДАТЬ ВЫЗОВ
// ============================================================================

const cfExternalId = document.getElementById('cf-external-id');
const cfType       = document.getElementById('cf-type');
const cfName       = document.getElementById('cf-name');
const cfEquipment  = document.getElementById('cf-equipment');
const cfInfo       = document.getElementById('cf-info');
const cfForced     = document.getElementById('cf-forced');
const createStatus = document.getElementById('create-status');

let typesCache = [];

function newManualId() {
    return `manual-${Math.floor(Date.now() / 1000)}`;
}

async function refreshCreateForm() {
    if (!typesCache.length) {
        try {
            const all = await apiFetch('/admin/api/types');
            // Тот же фильтр, что и в refreshTypes — единый источник правды
            typesCache = all.filter(t => !INTERNAL_TYPES.includes(t.type));
        } catch (e) {
            console.error('refreshCreateForm:', e);
            return;
        }
    }
    // Заполняем select
    const cur = cfType.value;
    cfType.innerHTML = typesCache.map(t =>
        `<option value="${esc(t.type)}">${esc(t.description)} (${esc(t.type)})</option>`
    ).join('');
    if (cur && typesCache.some(t => t.type === cur)) cfType.value = cur;

    // Если external_id пуст — сгенерим
    if (!cfExternalId.value) cfExternalId.value = newManualId();
}

document.getElementById('btn-regen-id').addEventListener('click', () => {
    cfExternalId.value = newManualId();
});

document.getElementById('btn-create-submit').addEventListener('click', async () => {
    const external_id = cfExternalId.value.trim();
    const type        = cfType.value;
    const name        = cfName.value.trim();
    const equipment   = cfEquipment.value.trim();
    const info        = cfInfo.value.trim();
    const forced      = parseInt(cfForced.value, 10) || 0;

    if (!external_id || !type) {
        createStatus.textContent = '⚠ Заполните External ID и тип';
        createStatus.className = 'form-status error';
        return;
    }

    const payload = { external_id, type, name, equipment, info };
    if (forced > 0) payload.forced_close_seconds = forced;

    const btn = document.getElementById('btn-create-submit');
    btn.disabled = true;
    try {
        await apiFetch('/admin/api/calls', {
            method: 'POST',
            body: JSON.stringify(payload),
        });

        createStatus.textContent = `✅ Создан ID=${external_id}`;
        createStatus.className = 'form-status success';
        toast('Вызов создан', `${type} → ${equipment || '—'}`, 'success', 2500);

        // Сбрасываем только external_id — остальное оставляем для пакетной отправки
        cfExternalId.value = newManualId();
        cfInfo.value = '';
        cfForced.value = 0;
        // Фокус обратно на поле имени для удобства
        cfName.focus();
    } catch (e) {
        createStatus.textContent = `❌ ${e.message}`;
        createStatus.className = 'form-status error';
        toast('Ошибка создания', e.message, 'error', 4000);
    } finally {
        btn.disabled = false;
    }
});

// ============================================================================
// ТАБ 3: ТИПЫ
// ============================================================================

async function refreshTypes() {
    const tbody = document.getElementById('types-tbody');
    try {
        const all = await apiFetch('/admin/api/types');
        // Фильтруем ОДИН раз — при записи в кеш.
        // Это гарантирует, что и таб «Типы», и выпадающий список «Создать вызов»
        // работают с одним и тем же отфильтрованным набором.
        typesCache = all.filter(t => !INTERNAL_TYPES.includes(t.type));
        renderTypes(typesCache);
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="7" class="empty">Ошибка: ${esc(e.message)}</td></tr>`;
    }
}


function renderTypes(types) {
    const tbody = document.getElementById('types-tbody');
    if (!types.length) {
        tbody.innerHTML = '<tr><td colspan="7" class="empty">Нет типов</td></tr>';
        return;
    }
    tbody.innerHTML = types.map(t => `
        <tr data-type="${esc(t.type)}">
            <td><code>${esc(t.type)}</code></td>
            <td><input class="cell-input" data-field="description" value="${esc(t.description)}" style="width: 100%;"></td>
            <td><input class="cell-input" data-field="live_time_min" type="number" value="${t.live_time_min}" style="width: 80px;"></td>
            <td><input class="cell-input" data-field="repeat_interval_min" type="number" value="${t.repeat_interval_min}" style="width: 80px;"></td>
            <td>
                <input type="checkbox" data-field="should_speak" ${t.should_speak ? 'checked' : ''}>
            </td>
            <td>${t.active_calls}</td>
            <td>
                <button class="btn btn-primary btn-mini" data-save-type="${esc(t.type)}">💾</button>
                <button class="btn btn-danger btn-mini" data-del-type="${esc(t.type)}" ${t.active_calls ? 'disabled title="Есть активные вызовы"' : ''}>🗑</button>
            </td>
        </tr>
    `).join('');

    // Кнопки "Сохранить"
    tbody.querySelectorAll('[data-save-type]').forEach(btn => {
        btn.addEventListener('click', async () => {
            const typeKey = btn.dataset.saveType;
            const row = tbody.querySelector(`tr[data-type="${CSS.escape(typeKey)}"]`);
            const payload = {
                type: typeKey,
                description:        row.querySelector('[data-field="description"]').value,
                live_time_min:      parseInt(row.querySelector('[data-field="live_time_min"]').value, 10) || 0,
                repeat_interval_min:parseInt(row.querySelector('[data-field="repeat_interval_min"]').value, 10) || 0,
                should_speak:       row.querySelector('[data-field="should_speak"]').checked ? 1 : 0,
            };
            try {
                await apiFetch('/admin/api/types', { method: 'POST', body: JSON.stringify(payload) });
                toast('Тип сохранён', typeKey, 'success', 2000);
                refreshTypes();
            } catch (e) {
                toast('Ошибка', e.message, 'error', 4000);
            }
        });
    });

    // Кнопки "Удалить"
    tbody.querySelectorAll('[data-del-type]').forEach(btn => {
        btn.addEventListener('click', async () => {
            const typeKey = btn.dataset.delType;
            if (!confirm(`Удалить тип "${typeKey}"?`)) return;
            try {
                await apiFetch(`/admin/api/types/${encodeURIComponent(typeKey)}`, { method: 'DELETE' });
                toast('Тип удалён', typeKey, 'success', 2000);
                refreshTypes();
            } catch (e) {
                toast('Ошибка', e.message, 'error', 4000);
            }
        });
    });
}

document.getElementById('btn-add-type').addEventListener('click', async () => {
    const typeKey = prompt('Ключ нового типа (латиница, без пробелов):');
    if (!typeKey) return;
    const description = prompt('Описание:', typeKey) || typeKey;
    try {
        await apiFetch('/admin/api/types', {
            method: 'POST',
            body: JSON.stringify({
                type: typeKey.trim(),
                description,
                live_time_min: 0,
                repeat_interval_min: 5,
                should_speak: 1,
            }),
        });
        toast('Тип добавлен', typeKey, 'success', 2000);
        refreshTypes();
    } catch (e) {
        toast('Ошибка', e.message, 'error', 4000);
    }
});

// ============================================================================
// ТАБ 4: СКЛАДЫ
// ============================================================================

async function refreshWarehouses() {
    const tbody = document.getElementById('wh-tbody');
    try {
        const data = await apiFetch('/admin/api/warehouses');
        renderWarehouses(data);
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="9" class="empty">Ошибка: ${esc(e.message)}</td></tr>`;
    }
}

function renderWarehouses(data) {
    const tbody = document.getElementById('wh-tbody');
    const list = data.warehouses || [];
    if (!list.length) {
        tbody.innerHTML = '<tr><td colspan="11" class="empty">Нет складов</td></tr>';
        return;
    }

    tbody.innerHTML = list.map(w => {
        let status, cls;
        if (w.is_exceeding)      { status = '⚠ Превышение'; cls = 'row-red'; }
        else if (w.current_value >= w.orange_threshold) { status = 'Внимание'; cls = 'row-orange'; }
        else                     { status = 'Норма'; cls = 'row-green'; }

        const lastAlert = w.last_alert_time ? fmtDateTime(w.last_alert_time) : '—';

        return `
            <tr class="${cls}" data-key="${esc(w.key)}">
                <td><code>${esc(w.key)}</code></td>
                <td><input class="cell-input" data-field="name" value="${esc(w.name)}" style="width: 180px;"></td>
                <td><input class="cell-input" data-field="current_value" type="number" value="${w.current_value}" style="width: 80px;"></td>
                <td><input class="cell-input" data-field="capacity" type="number" value="${w.capacity}" style="width: 80px;"></td>
                <td><input class="cell-input" data-field="orange_threshold" type="number" value="${w.orange_threshold}" style="width: 80px;"></td>
                <td><input class="cell-input" data-field="red_threshold" type="number" value="${w.red_threshold}" style="width: 80px;"></td>
                <td><input class="cell-input" data-field="repeat_interval_min" type="number" value="${w.repeat_interval_min}" style="width: 80px;"></td>
                <td>${status}</td>
                <td>${lastAlert}</td>
                <td>
                    <button class="btn btn-primary btn-mini" data-save-wh="${esc(w.key)}">💾</button>
                </td>
            </tr>
        `;
    }).join('');

    // Сохранение параметров и текущего значения склада (Объединенная кнопка 💾)
    tbody.querySelectorAll('[data-save-wh]').forEach(btn => {
        btn.addEventListener('click', async () => {
            const key = btn.dataset.saveWh;
            const row = tbody.querySelector(`tr[data-key="${CSS.escape(key)}"]`);

            // 1. Собираем параметры склада
            const payload = {
                name:               row.querySelector('[data-field="name"]').value,
                capacity:           parseInt(row.querySelector('[data-field="capacity"]').value, 10) || 0,
                orange_threshold:   parseInt(row.querySelector('[data-field="orange_threshold"]').value, 10) || 0,
                red_threshold:      parseInt(row.querySelector('[data-field="red_threshold"]').value, 10) || 0,
                repeat_interval_min:parseInt(row.querySelector('[data-field="repeat_interval_min"]').value, 10) || 0,
            };

            // 2. Собираем текущее значение паллет
            const currentValue = parseInt(row.querySelector('[data-field="current_value"]').value, 10) || 0;

            btn.disabled = true; // Блокируем кнопку на время отправки
            try {
                // ШАГ 1: Сохраняем настройки склада (название, пороги, интервал)
                await apiFetch(`/admin/api/warehouses/${encodeURIComponent(key)}/params`, {
                    method: 'POST',
                    body: JSON.stringify(payload),
                });

                // ШАГ 2: Сохраняем текущее значение паллет
                await apiFetch(`/admin/api/warehouses/${encodeURIComponent(key)}/value`, {
                    method: 'POST',
                    body: JSON.stringify({ value: currentValue }),
                });

                toast('Данные склада успешно обновлены', key, 'success', 2000);
                refreshWarehouses(); // Перерисовываем таблицу актуальными данными
            } catch (e) {
                toast('Ошибка сохранения', e.message, 'error', 4000);
            } finally {
                btn.disabled = false; // Возвращаем кнопку в активное состояние
            }
        });
    });

    // Отдельная кнопка сохранения только current_value
    // (можно оставить или убрать — решим). Пока — упрощаем: одна кнопка «💾»
    // сохраняет и параметры, и значение. Если нужно разделить — сделаем.
}

document.getElementById('btn-force-check').addEventListener('click', async () => {
    try {
        await apiFetch('/admin/api/warehouses/force_check', { method: 'POST' });
        toast('Проверка запущена', 'Менеджер складов выполнит _check_all()', 'success', 2000);
        setTimeout(refreshWarehouses, 500);
    } catch (e) {
        toast('Ошибка', e.message, 'error', 4000);
    }
});

// ============================================================================
// ТАБ 5: ЛОГИ
// ============================================================================

const logView       = document.getElementById('log-view');
const logPath       = document.getElementById('log-path');
const logLastUpdate = document.getElementById('log-last-update');
const logLive       = document.getElementById('log-live');

let logLiveTimer = null;

async function refreshLogs() {
    try {
        const data = await apiFetch('/admin/api/logs?tail=200');
        logView.textContent = (data.lines || []).join('');
        logPath.textContent = `Файл: ${data.log_file || '—'}`;
        logLastUpdate.textContent = `Обновлено: ${new Date().toLocaleTimeString('ru-RU', { hour12: false })}`;
        // Прокрутить вниз
        logView.scrollTop = logView.scrollHeight;
    } catch (e) {
        logView.textContent = `Ошибка загрузки логов: ${e.message}`;
    }
}

document.getElementById('btn-log-refresh').addEventListener('click', refreshLogs);

logLive.addEventListener('change', () => {
    if (logLive.checked) {
        refreshLogs();
        logLiveTimer = setInterval(refreshLogs, 2000);
    } else {
        if (logLiveTimer) {
            clearInterval(logLiveTimer);
            logLiveTimer = null;
        }
    }
});

// ============================================================================
// Кнопка "Обновить" в хедере — обновляет активный таб
// ============================================================================

document.getElementById('btn-refresh').addEventListener('click', () => {
    switchTab(activeTab);
    toast('Обновлено', '', 'success', 1000);
});

// ============================================================================
// Автообновление дашборда раз в 5 секунд (только когда активен этот таб)
// ============================================================================

setInterval(() => {
    if (activeTab === 'dashboard') refreshDashboard();
}, 5000);

// ============================================================================
// Инициализация при загрузке
// ============================================================================

(function init() {
    refreshDashboard();
    // Загружаем типы заранее, чтобы форма создания была готова
    refreshCreateForm();
    // Типы, склады, логи — подтянутся при первом переключении на таб
})();