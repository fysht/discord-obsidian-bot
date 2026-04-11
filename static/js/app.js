/* ==========================================
   Manager AI — Application Logic v3
   ========================================== */

const API_BASE = '';
let apiKey = localStorage.getItem('secretary_api_key') || '';

// ========== Utilities ==========
const $ = sel => document.querySelector(sel);
const $$ = sel => document.querySelectorAll(sel);

function showScreen(id) {
    $$('.screen').forEach(s => s.classList.remove('active'));
    document.getElementById(id).classList.add('active');
}

function formatTime(isoStr) {
    if (!isoStr) return '';
    const d = new Date(isoStr);
    return d.toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit' });
}

function escapeHtml(text) {
    if (!text) return '';
    const el = document.createElement('div');
    el.textContent = text;
    return el.innerHTML;
}

// ========== API Core ==========
async function apiFetch(path, options = {}) {
    const headers = {
        'Content-Type': 'application/json',
        'X-Api-Key': apiKey,
        ...(options.headers || {}),
    };
    const res = await fetch(`${API_BASE}${path}`, { ...options, headers });
    if (res.status === 401) {
        localStorage.removeItem('secretary_api_key');
        apiKey = '';
        showScreen('login-screen');
        throw new Error('認証が切れました。再ログインしてください。');
    }
    if (!res.ok) throw new Error(`APIエラー: ${res.status}`);
    return res.json();
}

// ========== Login ==========
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
            if (!r.ok) throw new Error('パスワードが正しくありません。');
            return r.json();
        });

        apiKey = data.api_key;
        localStorage.setItem('secretary_api_key', apiKey);
        $('#login-error').textContent = '';
        showScreen('main-screen');
        loadHistory();
    } catch (err) {
        $('#login-error').textContent = err.message;
    }
});

// ========== Navigation (Main Tabs & Sub Tabs) ==========
$$('.nav-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        $$('.nav-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        
        $$('.tab-content').forEach(t => t.classList.remove('active'));
        const targetTab = btn.dataset.tab;
        $(`#${targetTab}-tab`).classList.add('active');

        // タブに応じたデータ読み込み
        if (targetTab === 'dashboard' || targetTab === 'wellness' || targetTab === 'logs') {
            loadDashboard();
        }
    });
});

$$('.sub-nav-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        $$('.sub-nav-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        
        $$('.sub-tab-content').forEach(t => t.classList.remove('active'));
        $(`#sub-${btn.dataset.sub}`).classList.add('active');
    });
});

// ========== Chat System ==========
const chatMessages = $('#chat-messages');
const messageInput = $('#message-input');
const sendBtn = $('#send-btn');

// Input resizing
messageInput.addEventListener('input', () => {
    messageInput.style.height = 'auto';
    messageInput.style.height = Math.min(messageInput.scrollHeight, 120) + 'px';
});

// PC: Enter to Send
messageInput.addEventListener('keydown', (e) => {
    if (e.isComposing) return;
    if (window.innerWidth >= 768 && e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        $('#chat-form').dispatchEvent(new Event('submit'));
    }
});

function addMessage(role, content, timestamp) {
    const welcome = chatMessages.querySelector('.chat-welcome');
    if (welcome) welcome.remove();

    const div = document.createElement('div');
    div.className = `message ${role}`;
    const timeStr = timestamp ? formatTime(timestamp) : formatTime(new Date().toISOString());

    div.innerHTML = `
        <div class="msg-bubble">${formatMessageText(content)}</div>
        <div class="msg-time">${timeStr}</div>
    `;

    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function formatMessageText(text) {
    let html = escapeHtml(text).replace(/\n/g, '<br>');
    // Inline buttons for quick actions
    html = html.replace(/\[BUTTON:add_task:(.+?)\]/g, (_, taskName) => {
        return `<button class="inline-action-btn" onclick="directAddTask('${escapeHtml(taskName)}')">＋ ${escapeHtml(taskName)}をタスクへ</button>`;
    });
    return html;
}

window.directAddTask = async function(taskName) {
    try {
        await apiFetch('/api/task_action', {
            method: 'POST',
            body: JSON.stringify({ action: 'create', new_text: taskName }),
        });
        showToast(`「${taskName}」を追加しました`);
    } catch {
        showToast('追加に失敗しました', true);
    }
};

$('#chat-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const msg = messageInput.value.trim();
    if (!msg || sendBtn.disabled) return;

    addMessage('user', msg);
    messageInput.value = '';
    messageInput.style.height = 'auto';
    sendBtn.disabled = true;
    showTyping();

    try {
        const data = await apiFetch('/api/chat', {
            method: 'POST',
            body: JSON.stringify({ message: msg }),
        });
        hideTyping();
        addMessage('assistant', data.reply);
    } catch (err) {
        hideTyping();
        addMessage('assistant', 'ごめん、ちょっと調子が悪いみたいだ。もう一回送ってくれる？');
    } finally {
        sendBtn.disabled = false;
        messageInput.focus();
    }
});

function showTyping() {
    const div = document.createElement('div');
    div.className = 'message assistant';
    div.id = 'typing-msg';
    div.innerHTML = `<div class="msg-bubble typing"><span class="dot"></span><span class="dot"></span><span class="dot"></span></div>`;
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function hideTyping() {
    const el = $('#typing-msg');
    if (el) el.remove();
}

async function loadHistory() {
    try {
        const data = await apiFetch('/api/history?limit=30');
        chatMessages.innerHTML = '';
        if (data.messages && data.messages.length > 0) {
            data.messages.forEach(m => addMessage(m.role, m.content, m.timestamp));
        }
    } catch (err) {
        console.error('History load failed:', err);
    }
}

window.sendActionCommand = function(cmd) {
    messageInput.value = cmd;
    $('#chat-form').dispatchEvent(new Event('submit'));
};

// ========== Dashboard & Data Rendering ==========
async function loadDashboard() {
    try {
        const data = await apiFetch('/api/dashboard');
        
        // Date Label
        if ($('#dash-date-label')) {
            const now = new Date();
            const days = ['日', '月', '火', '水', '木', '金', '土'];
            $('#dash-date-label').textContent = `${now.getMonth()+1}月${now.getDate()}日 (${days[now.getDay()]})`;
        }

        // 1. Google Calendar
        const gcEl = $('#dash-google-calendar');
        if (data.g_calendar && data.g_calendar.length > 0) {
            gcEl.innerHTML = data.g_calendar.map(l => `<div class="cal-item">${escapeHtml(l.replace(/^- /, ''))}</div>`).join('');
        } else {
            gcEl.innerHTML = '<p class="dash-empty">予定はありません</p>';
        }

        // 2. Google Tasks
        const gtEl = $('#dash-google-tasks');
        if (data.g_tasks && data.g_tasks.length > 0) {
            gtEl.innerHTML = data.g_tasks.map(l => `<div class="gtask-item">⭕ ${escapeHtml(l.replace(/^- /, ''))}</div>`).join('');
        } else {
            gtEl.innerHTML = '<p class="dash-empty">タスクはありません</p>';
        }

        // 3. Obsidian Tasks
        const otEl = $('#dash-tasks');
        if (data.tasks && data.tasks.length > 0) {
            otEl.innerHTML = data.tasks.map(t => `
                <div class="task-item ${t.done ? 'done' : ''}">
                    <span class="task-check" onclick="toggleTask('${escapeHtml(t.text)}')">${t.done ? '✅' : '⬜'}</span>
                    <span class="task-text">${escapeHtml(t.text)}</span>
                    <div class="task-actions">
                        <button class="small-btn" onclick="deleteTask('${escapeHtml(t.text)}')">🗑️</button>
                    </div>
                </div>
            `).join('');
        } else {
            otEl.innerHTML = '<p class="dash-empty">タスクはありません</p>';
        }

        // 4. Weather
        if ($('#dash-weather')) {
            $('#dash-weather').innerHTML = `<p>${escapeHtml(data.weather || '取得失敗')}</p>`;
        }

        // 5. Sleep (Health)
        const sleepEl = $('#dash-sleep');
        if (sleepEl) {
            if (data.sleep && data.sleep.score !== 'N/A') {
                sleepEl.innerHTML = `
                    <div class="health-grid">
                        <div class="health-stat"><small>スコア</small><strong>${data.sleep.score}</strong></div>
                        <div class="health-stat"><small>睡眠時間</small><strong>${data.sleep.duration}</strong></div>
                        <div class="health-stat"><small>歩数</small><strong>${data.sleep.steps}歩</strong></div>
                    </div>`;
            } else {
                sleepEl.innerHTML = '<p class="dash-empty">データがまだ同期されていません</p>';
            }
        }

        // 6. News
        const newsEl = $('#dash-news');
        if (newsEl) {
            if (data.news && data.news.length > 0) {
                newsEl.innerHTML = data.news.map(n => {
                    const lines = n.split('\n');
                    return `<a href="${lines[1]}" target="_blank" class="news-item">
                                <span class="news-title">${escapeHtml(lines[0])}</span>
                            </a>`;
                }).join('');
            } else {
                newsEl.innerHTML = '<p class="dash-empty">ニュースが取得できませんでした</p>';
            }
        }

        // 7. Alter Log
        if ($('#dash-alter-log')) {
            $('#dash-alter-log').innerHTML = data.alter_log ? escapeHtml(data.alter_log).replace(/\n/g, '<br>') : '未生成です';
        }

    } catch (err) {
        showToast('データの更新に失敗しました', true);
    }
}

async function toggleTask(text) {
    try {
        await apiFetch('/api/task_action', { method: 'POST', body: JSON.stringify({ action: 'toggle', old_text: text }) });
        loadDashboard();
    } catch { showToast('同期待ち...', true); }
}

async function deleteTask(text) {
    if (!confirm('削除しますか？')) return;
    try {
        await apiFetch('/api/task_action', { method: 'POST', body: JSON.stringify({ action: 'delete', old_text: text }) });
        loadDashboard();
    } catch { showToast('削除失敗', true); }
}

// ========== Modal Logic ==========
let taskActionType = 'start';

window.openTaskEntry = function(type) {
    taskActionType = type;
    const modal = $('#task-modal');
    const title = $('#modal-title');
    title.textContent = type === 'start' ? 'タスク開始' : 'タスク終了';
    $('#task-name-input').value = '';
    modal.classList.remove('hidden');
    $('#task-name-input').focus();
};

window.closeTaskEntry = function() {
    $('#task-modal').classList.add('hidden');
};

$('#task-submit-btn').addEventListener('click', () => {
    const val = $('#task-name-input').value.trim();
    if (!val) return;
    
    const command = taskActionType === 'start' ? `今から「${val}」を開始するよ` : `「${val}」が終わったよ`;
    sendActionCommand(command);
    closeTaskEntry();
});

// Toast notification
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

// ========== Initialize ==========
window.addEventListener('DOMContentLoaded', () => {
    if (apiKey) {
        showScreen('main-screen');
        loadHistory();
        loadDashboard();
    } else {
        showScreen('login-screen');
    }

    if ('serviceWorker' in navigator) {
        navigator.serviceWorker.register('/static/js/sw.js').catch(err => console.error(err));
    }
});
