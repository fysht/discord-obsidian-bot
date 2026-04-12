/* ==========================================
   Manager AI — Application Logic v4.0
   ========================================== */

const API_BASE = '';
let apiKey = localStorage.getItem('secretary_api_key') || '';
let currentLanguage = 'ja'; // 'ja' or 'en' (Concept)

const $ = sel => document.querySelector(sel);
const $$ = sel => document.querySelectorAll(sel);

// ========== UI UTILS ==========
function showScreen(id) {
    $$('.screen').forEach(s => s.classList.remove('active'));
    $(`#${id}`).classList.add('active');
}

function showToast(msg, isError = false) {
    const container = $('#toast-container');
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
$('#login-form').addEventListener('submit', async (e) => {
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

// ========== NAVIGATION ==========
$$('.nav-item').forEach(item => {
    item.addEventListener('click', () => {
        const tab = item.dataset.tab;
        switchTab(tab);
    });
});

function switchTab(tab) {
    $$('.nav-item').forEach(i => i.classList.remove('active'));
    $(`.nav-item[data-tab="${tab}"]`).classList.add('active');
    
    $$('.tab-pane').forEach(p => p.classList.remove('active'));
    $(`#tab-${tab}`).classList.add('active');
    
    // Update Header Title
    const titles = { chat: 'チャット', info: '情報', log: 'デイリーログ', schedule: '予定' };
    $('#current-tab-title').textContent = titles[tab] || 'Manager AI';

    if (tab !== 'chat') loadDashboard();
}

// ========== SETTINGS (CONCEPT) ==========
$('#settings-btn').addEventListener('click', () => {
    currentLanguage = currentLanguage === 'ja' ? 'en' : 'ja';
    showToast(`Language switched to ${currentLanguage === 'ja' ? '日本語' : 'English'} (Concept)`);
});

// ========== CHAT SYSTEM ==========
const chatMessages = $('#chat-messages');
const messageInput = $('#message-input');
const sendBtn = $('#send-btn');

$('#chat-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const msg = messageInput.value.trim();
    if (!msg || sendBtn.disabled) return;

    appendMsg('user', msg);
    messageInput.value = '';
    messageInput.style.height = 'auto';
    sendBtn.disabled = true;

    try {
        const data = await apiFetch('/api/chat', { method: 'POST', body: JSON.stringify({ message: msg }) });
        appendMsg('assistant', data.reply);
    } catch (err) {
        appendMsg('assistant', 'すみません、エラーが発生しました。');
    } finally {
        sendBtn.disabled = false;
    }
});

function appendMsg(role, content) {
    const welcome = chatMessages.querySelector('.chat-welcome');
    if (welcome) welcome.remove();

    const div = document.createElement('div');
    div.className = `message ${role}`;
    div.innerHTML = `<div class="msg-bubble">${escapeHtml(content).replace(/\n/g, '<br>')}</div>`;
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

window.sendActionCommand = (cmd) => {
    messageInput.value = cmd;
    $('#chat-form').dispatchEvent(new Event('submit'));
};

// ========== INTELLIGENT TASK MODAL ==========
let currentTaskType = 'start';

window.openIntelligentTaskModal = async (type) => {
    currentTaskType = type;
    const modal = $('#task-modal');
    $('#modal-title').textContent = type === 'start' ? 'タスク開始（履歴）' : 'タスク終了（実行中）';
    $('#modal-list').innerHTML = '<div class="loading-placeholder">候補を読み込み中...</div>';
    $('#custom-task-input').value = '';
    
    modal.classList.remove('hidden');
    
    try {
        const data = await apiFetch('/api/task_candidates');
        const list = type === 'start' ? data.start : data.end;
        
        if (list && list.length > 0) {
            $('#modal-list').innerHTML = list.map(item => `
                <div class="modal-item" onclick="selectTaskCandidate('${escapeHtml(item)}')">${escapeHtml(item)}</div>
            `).join('');
        } else {
            $('#modal-list').innerHTML = '<div class="loading-placeholder">候補がありません</div>';
        }
    } catch (err) {
        $('#modal-list').innerHTML = '<div class="loading-placeholder">取得に失敗しました</div>';
    }
};

window.closeTaskModal = () => $('#task-modal').classList.add('hidden');

window.selectTaskCandidate = (name) => {
    $('#custom-task-input').value = name;
};

$('#task-confirm-btn').addEventListener('click', () => {
    const val = $('#custom-task-input').value.trim();
    if (!val) return;
    const cmd = currentTaskType === 'start' ? `今から「${val}」を開始するよ` : `「${val}」が終わったよ`;
    sendActionCommand(cmd);
    closeTaskModal();
});

// ========== DASHBOARD & EDIT MODAL ==========
let editTarget = null; // { type: 'calendar'|'google_task'|'obsidian', id: string, text: string }

async function loadDashboard() {
    try {
        const data = await apiFetch('/api/dashboard');
        
        // Date
        if ($('#dash-date-label')) $('#dash-date-label').textContent = data.date || '---';

        // 1. Google Calendar
        const calEl = $('#dash-google-calendar');
        if (data.g_calendar && data.g_calendar.length > 0) {
            calEl.innerHTML = data.g_calendar.map(ev => `
                <div class="list-item">
                    <div class="li-time">${ev.time}</div>
                    <div class="li-text">${escapeHtml(ev.summary)}</div>
                    <button class="edit-btn" onclick="openEditModal('calendar', '${ev.id}', '${escapeHtml(ev.summary)}')">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
                    </button>
                </div>
            `).join('');
        } else {
            calEl.innerHTML = '<div class="loading-placeholder">予定はありません</div>';
        }

        // 2. Google Tasks (タスク)
        const gTaskEl = $('#dash-google-tasks');
        if (data.google_tasks && data.google_tasks.length > 0) {
            gTaskEl.innerHTML = data.google_tasks.map(t => `
                <div class="list-item">
                    <div class="li-text">✅ ${escapeHtml(t.title)}</div>
                    <button class="edit-btn" onclick="openEditModal('google_task', '${t.id}', '${escapeHtml(t.title)}')">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
                    </button>
                </div>
            `).join('');
        } else {
            gTaskEl.innerHTML = '<div class="loading-placeholder">タスクはありません</div>';
        }

        // 3. Obsidian Tasks
        const obTaskEl = $('#dash-tasks');
        if (data.tasks && data.tasks.length > 0) {
            obTaskEl.innerHTML = data.tasks.map(t => `
                <div class="list-item">
                    <div class="li-text" style="text-decoration: ${t.done ? 'line-through' : 'none'}">${escapeHtml(t.text)}</div>
                    <button class="edit-btn" onclick="openEditModal('obsidian', '', '${escapeHtml(t.text)}', ${t.done})">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
                    </button>
                </div>
            `).join('');
        }

        // 4. Weather & News
        if ($('#dash-weather')) $('#dash-weather').textContent = data.weather || '---';
        if ($('#dash-news')) {
            $('#dash-news').innerHTML = (data.news || []).map(n => `<div class="news-item">${escapeHtml(n.split('\n')[0])}</div>`).join('');
        }

        // 5. Sleep & Diary
        if ($('#dash-sleep')) {
            $('#dash-sleep').innerHTML = data.sleep?.score ? `スコア: ${data.sleep.score} / 時間: ${data.sleep.duration}` : 'データなし';
        }
        if ($('#dash-alter-log')) $('#dash-alter-log').innerHTML = escapeHtml(data.alter_log || '日記はまだ生成されていません。').replace(/\n/g, '<br>');

    } catch (err) {
        console.error(err);
    }
}

window.openEditModal = (type, id, text, isDone = false) => {
    editTarget = { type, id, text, isDone };
    const modal = $('#edit-modal');
    $('#edit-modal-title').textContent = type === 'calendar' ? '予定を編集' : 'タスクを編集';
    $('#edit-input').value = text;
    modal.classList.remove('hidden');
};

window.closeEditModal = () => $('#edit-modal').classList.add('hidden');

$('#edit-save-btn').addEventListener('click', async () => {
    const newVal = $('#edit-input').value.trim();
    if (!newVal) return;
    
    try {
        if (editTarget.type === 'calendar') {
            await apiFetch('/api/calendar_action', { method: 'POST', body: JSON.stringify({ action: 'update', event_id: editTarget.id, summary: newVal }) });
        } else if (editTarget.type === 'google_task') {
            await apiFetch('/api/google_tasks_action', { method: 'POST', body: JSON.stringify({ action: 'update', task_id: editTarget.id, title: newVal }) });
        } else if (editTarget.type === 'obsidian') {
            await apiFetch('/api/task_action', { method: 'POST', body: JSON.stringify({ action: 'update', old_text: editTarget.text, new_text: newVal }) });
        }
        showToast('保存しました');
        loadDashboard();
        closeEditModal();
    } catch { showToast('保存失敗', true); }
});

$('#edit-delete-btn').addEventListener('click', async () => {
    if (!confirm('本当に削除しますか？')) return;
    try {
        if (editTarget.type === 'calendar') {
            await apiFetch('/api/calendar_action', { method: 'POST', body: JSON.stringify({ action: 'delete', event_id: editTarget.id }) });
        } else if (editTarget.type === 'google_task') {
            await apiFetch('/api/google_tasks_action', { method: 'POST', body: JSON.stringify({ action: 'delete', task_id: editTarget.id }) });
        } else if (editTarget.type === 'obsidian') {
            await apiFetch('/api/task_action', { method: 'POST', body: JSON.stringify({ action: 'delete', old_text: editTarget.text }) });
        }
        showToast('削除しました');
        loadDashboard();
        closeEditModal();
    } catch { showToast('削除失敗', true); }
});

// ========== INIT ==========
function initMain() {
    loadHistory();
    loadDashboard();
}

async function loadHistory() {
    try {
        const data = await apiFetch('/api/history?limit=20');
        chatMessages.innerHTML = '';
        data.messages.forEach(m => appendMsg(m.role, m.content));
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
