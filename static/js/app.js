/* ==========================================
   Manager AI — Application Logic v4.5
   ========================================== */

const API_BASE = '';
let apiKey = localStorage.getItem('secretary_api_key') || '';
let lastMsgDate = null;

const $ = sel => document.querySelector(sel);
const $$ = sel => document.querySelectorAll(sel);

// ========== UI UTILS ==========
function showScreen(id) {
    $$('.screen').forEach(s => s.classList.remove('active'));
    $(`#${id}`).classList.add('active');
}

function showToast(msg, isError = false) {
    const container = $('#toast-container');
    if (!container) return;
    const toast = document.createElement('div');
    toast.className = `toast ${isError ? 'error' : ''}`;
    toast.textContent = msg;
    container.appendChild(toast);
    setTimeout(() => {
        toast.style.opacity = '0';
        setTimeout(() => toast.remove(), 500);
    }, 3000);
}

const escapeHtml = t => {
    const d = document.createElement('div');
    d.textContent = t;
    return d.innerHTML;
};

// ========== API CORE ==========
async function apiFetch(path, options = {}) {
    const headers = { 'Content-Type': 'application/json', 'X-Api-Key': apiKey, ...(options.headers || {}) };
    const res = await fetch(`${API_BASE}${path}`, { ...options, headers });
    if (res.status === 401) {
        localStorage.removeItem('secretary_api_key');
        apiKey = '';
        showScreen('login-screen');
        throw new Error('Unauthorized');
    }
    if (!res.ok) throw new Error(`API Error: ${res.status}`);
    return res.json();
}

// ========== LOGIN ==========
const loginForm = $('#login-form');
if (loginForm) {
    loginForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const pw = $('#password-input').value.trim();
        if (!pw) return;

        try {
            const data = await fetch(`${API_BASE}/api/auth`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ password: pw }),
            }).then(r => {
                if (!r.ok) throw new Error('Password mismatch');
                return r.json();
            });

            apiKey = data.api_key;
            localStorage.setItem('secretary_api_key', apiKey);
            showScreen('main-screen');
            initMain();
        } catch (err) {
            $('#login-error').textContent = 'パスワードが違います';
        }
    });
}

// ========== NAVIGATION ==========
$$('.nav-item').forEach(item => {
    item.addEventListener('click', () => {
        const tab = item.dataset.tab;
        switchTab(tab);
    });
});

function switchTab(tab) {
    $$('.nav-item').forEach(i => i.classList.remove('active'));
    document.querySelector(`.nav-item[data-tab="${tab}"]`)?.classList.add('active');
    
    $$('.tab-pane').forEach(p => p.classList.remove('active'));
    $(`#tab-${tab}`)?.classList.add('active');
    
    const titles = { chat: 'チャット', info: '情報', log: 'ライフログ', schedule: '予定' };
    const titleEl = $('#current-tab-title');
    if (titleEl) titleEl.textContent = titles[tab] || 'Manager AI';

    if (tab !== 'chat') loadDashboard();
}

// ========== CHAT SYSTEM ==========
const chatMessages = $('#chat-messages');
const messageInput = $('#message-input');
const sendBtn = $('#send-btn');
const chatForm = $('#chat-form');

if (chatForm) {
    chatForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const msg = messageInput.value.trim();
        if (!msg) return;

        appendMsg('user', msg);
        messageInput.value = '';
        messageInput.style.height = '40px';
        sendBtn.classList.remove('active');

        try {
            const data = await apiFetch('/api/chat', { method: 'POST', body: JSON.stringify({ message: msg }) });
            appendMsg('assistant', data.reply);
        } catch (err) {
            appendMsg('assistant', 'すみません、エラーが発生しました。');
        }
    });
}

if (messageInput) {
    messageInput.addEventListener('input', () => {
        messageInput.style.height = '40px';
        messageInput.style.height = Math.min(messageInput.scrollHeight, 120) + 'px';
        sendBtn.classList.toggle('active', messageInput.value.trim() !== '');
    });
}

function appendMsg(role, content) {
    if (!chatMessages) return;
    const now = new Date();
    const dStr = `${now.getFullYear()}年${now.getMonth()+1}月${now.getDate()}日(${['日','月','火','水','木','金','土'][now.getDay()]})`;
    const tStr = now.getHours().toString().padStart(2, '0') + ':' + now.getMinutes().toString().padStart(2, '0');

    if (lastMsgDate !== dStr) {
        const sep = document.createElement('div');
        sep.className = 'chat-date-separator';
        sep.textContent = dStr;
        chatMessages.appendChild(sep);
        lastMsgDate = dStr;
    }

    const div = document.createElement('div');
    div.className = `message ${role}`;
    
    let html = '';
    if (role === 'assistant') {
        html += `<img src="/static/icons/avatar.png" class="msg-avatar">`;
    }
    
    html += `
        <div class="msg-content">
            <div class="msg-bubble">${escapeHtml(content).replace(/\n/g, '<br>')}</div>
        </div>
        <div class="msg-time">${tStr}</div>
    `;
    
    div.innerHTML = html;
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

window.sendActionCommand = (cmd) => {
    if (!messageInput) return;
    messageInput.value = cmd;
    const event = new Event('submit', { cancelable: true });
    chatForm.dispatchEvent(event);
};

// ========== DASHBOARD ==========
async function loadDashboard() {
    try {
        const data = await apiFetch('/api/dashboard');
        
        const dateLabel = $('#dash-date-label');
        if (dateLabel) dateLabel.textContent = data.date || '---';

        // Weather
        const weatherEl = $('#dash-weather');
        if (weatherEl) {
            weatherEl.style.whiteSpace = 'pre-wrap';
            weatherEl.textContent = data.weather || '取得中...';
        }

        // News
        const newsEl = $('#dash-news');
        if (newsEl && data.news) {
            newsEl.innerHTML = data.news.map(n => `
                <div class="news-item">
                    <span class="news-dot"></span>
                    <a href="${n.link}" target="_blank" class="news-text">${escapeHtml(n.title)}</a>
                </div>
            `).join('');
        }

        // Google Tasks
        const gTaskEl = $('#dash-google-tasks');
        if (gTaskEl && data.google_tasks) {
            gTaskEl.innerHTML = data.google_tasks.map(t => `
                <div class="list-item">
                    <div class="checkbox-custom ${t.status === 'completed' ? 'checked' : ''}" 
                         onclick="toggleGoogleTask('${t.id}', '${t.status}')"></div>
                    <div class="li-text">${escapeHtml(t.title)}</div>
                    <button class="icon-btn" onclick="openEditModal('google_task', '${t.id}', '${escapeHtml(t.title)}')">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
                    </button>
                </div>
            `).join('');
        }

        // Google Calendar
        const calEl = $('#dash-google-calendar');
        if (calEl && data.g_calendar) {
            calEl.innerHTML = data.g_calendar.map(ev => {
                const startTime = ev.start ? (ev.start.includes('T') ? ev.start.split('T')[1].slice(0,5) : '終日') : '終日';
                return `
                    <div class="list-item">
                        <div class="li-time">${startTime}</div>
                        <div class="li-text">${escapeHtml(ev.summary)}</div>
                        <button class="icon-btn" onclick="openEditModal('calendar', '${ev.id}', '${escapeHtml(ev.summary)}')">
                            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
                        </button>
                    </div>
                `;
            }).join('');
        }

        // Obsidian Tasks (Lifelog)
        const obTaskEl = $('#dash-tasks');
        if (obTaskEl && data.tasks) {
            obTaskEl.innerHTML = data.tasks.map(t => `
                <div class="list-item">
                    <div class="li-text" style="text-decoration: ${t.done ? 'line-through' : 'none'}">${escapeHtml(t.text)}</div>
                    <button class="icon-btn" onclick="openEditModal('obsidian', '', '${escapeHtml(t.text)}', ${t.done})">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
                    </button>
                </div>
            `).join('');
        }

        // Sleep & Diary
        const sleepEl = $('#dash-sleep');
        if (sleepEl) {
            sleepEl.innerHTML = data.sleep?.score ? `スコア: ${data.sleep.score} / 時間: ${data.sleep.duration}` : '<div class="loading-placeholder">データなし</div>';
        }
        const diaryEl = $('#dash-alter-log');
        if (diaryEl) {
            diaryEl.innerHTML = (data.alter_log || '日記は順次生成されます。').replace(/\n/g, '<br>');
        }

    } catch (err) {
        console.error(err);
    }
}

// ========== ACTIONS ==========
window.toggleGoogleTask = async (taskId, currentStatus) => {
    const isCompleted = currentStatus === 'completed';
    try {
        await apiFetch('/api/google_tasks_action', {
            method: 'POST',
            body: JSON.stringify({ action: 'toggle', task_id: taskId, completed: !isCompleted })
        });
        loadDashboard();
    } catch (err) { showToast('更新に失敗しました', true); }
};

window.openAddEventModal = () => {
    const modal = $('#event-modal');
    if (modal) {
        modal.classList.remove('hidden');
        const now = new Date();
        const jstOffset = 9 * 60;
        const localTime = new Date(now.getTime() + (jstOffset + now.getTimezoneOffset()) * 60000);
        const st = new Date(localTime.getTime() + 60*60*1000).toISOString().slice(0,16);
        $('#event-start').value = st;
        $('#event-end').value = st;
    }
};

window.closeEventModal = () => $('#event-modal')?.classList.add('hidden');

window.saveEvent = async () => {
    const summary = $('#event-summary').value.trim();
    const start = $('#event-start').value;
    const end = $('#event-end').value;
    const desc = $('#event-desc').value.trim();

    if (!summary || !start) {
        showToast('件名と開始時間は必須です', true);
        return;
    }

    const fmt = s => s.replace('T', ' ') + ':00';
    try {
        await apiFetch('/api/calendar_action', {
            method: 'POST',
            body: JSON.stringify({
                action: 'add',
                summary,
                start_time: fmt(start),
                end_time: end ? fmt(end) : fmt(start),
                description: desc
            })
        });
        showToast('予定を追加しました');
        closeEventModal();
        loadDashboard();
    } catch { showToast('追加に失敗しました', true); }
};

window.openEditModal = (type, id, text, isDone = false) => {
    window.currentEditTarget = { type, id, text, isDone };
    const input = $('#edit-input');
    if (input) input.value = text;
    $('#edit-modal')?.classList.remove('hidden');
};

window.closeEditModal = () => $('#edit-modal')?.classList.add('hidden');

const editSaveBtn = $('#edit-save-btn');
if (editSaveBtn) {
    editSaveBtn.addEventListener('click', async () => {
        const t = window.currentEditTarget;
        const val = $('#edit-input').value.trim();
        if (!val) return;
        try {
            if (t.type === 'calendar') {
                await apiFetch('/api/calendar_action', { method: 'POST', body: JSON.stringify({ action: 'update', event_id: t.id, summary: val }) });
            } else if (t.type === 'google_task') {
                await apiFetch('/api/google_tasks_action', { method: 'POST', body: JSON.stringify({ action: 'update', task_id: t.id, title: val }) });
            } else if (t.type === 'obsidian') {
                await apiFetch('/api/task_action', { method: 'POST', body: JSON.stringify({ action: 'update', old_text: t.text, new_text: val }) });
            }
            showToast('保存しました');
            loadDashboard();
            closeEditModal();
        } catch { showToast('保存に失敗しました', true); }
    });
}

const editDeleteBtn = $('#edit-delete-btn');
if (editDeleteBtn) {
    editDeleteBtn.addEventListener('click', async () => {
        const t = window.currentEditTarget;
        if (!confirm('削除しますか？')) return;
        try {
            if (t.type === 'calendar') {
                await apiFetch('/api/calendar_action', { method: 'POST', body: JSON.stringify({ action: 'delete', event_id: t.id }) });
            } else if (t.type === 'google_task') {
                await apiFetch('/api/google_tasks_action', { method: 'POST', body: JSON.stringify({ action: 'delete', task_id: t.id }) });
            } else if (t.type === 'obsidian') {
                await apiFetch('/api/task_action', { method: 'POST', body: JSON.stringify({ action: 'delete', old_text: t.text }) });
            }
            showToast('削除しました');
            loadDashboard();
            closeEditModal();
        } catch { showToast('削除に失敗しました', true); }
    });
}

// ========== INIT ==========
function initMain() {
    loadHistory();
    loadDashboard();
}

async function loadHistory() {
    try {
        const data = await apiFetch('/api/history?limit=20');
        if (chatMessages) {
            chatMessages.innerHTML = '';
            lastMsgDate = null;
            data.messages.forEach(m => appendMsg(m.role, m.content));
        }
    } catch {}
}

window.addEventListener('DOMContentLoaded', () => {
    if (apiKey) {
        showScreen('main-screen');
        initMain();
    } else {
        showScreen('login-screen');
    }
});
