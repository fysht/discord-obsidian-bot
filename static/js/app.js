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

const pwToggleBtn = $('#pw-toggle-btn');
if (pwToggleBtn) {
    pwToggleBtn.addEventListener('click', () => {
        const input = $('#password-input');
        if (input.type === 'password') {
            input.type = 'text';
            pwToggleBtn.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"></path><line x1="1" y1="1" x2="23" y2="23"></line></svg>';
        } else {
            input.type = 'password';
            pwToggleBtn.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
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

        // 英会話モードがONなら指示を付加
        const isEnglish = $('#english-mode-checkbox')?.checked;
        const finalMsg = isEnglish ? `${msg}\n(Important: Please reply ONLY in English for this response.)` : msg;

        try {
            const data = await apiFetch('/api/chat', { method: 'POST', body: JSON.stringify({ message: finalMsg }) });
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
    
    let processedContent = escapeHtml(content).replace(/\n/g, '<br>');
    
    // NotebookLM URLへのリンクが含まれている場合はボタンとして表示
    if (content.includes('notebooklm.google.com')) {
        const urlMatch = content.match(/https:\/\/notebooklm\.google\.com\/notebook\/[a-zA-Z0-9-]+/);
        if (urlMatch) {
            processedContent += `<br><a href="${urlMatch[0]}" target="_blank" class="mini-link" style="display:inline-block; margin-top:8px;">📚 NotebookLMで詳しく調べる</a>`;
        }
    }

    // AI提案ボタンのパース [ACTION:tool_name:arg1=val1|arg2=val2]
    const actionMatch = content.match(/\[ACTION:(.*?):(.*?)\]/);
    if (actionMatch) {
        const toolName = actionMatch[1];
        const argsRaw = actionMatch[2].split('|');
        const args = {};
        argsRaw.forEach(pair => {
            const [k, v] = pair.split('=');
            if (k) args[k] = v;
        });

        const btnLabel = toolName === 'calendar_add' ? 'カレンダーに登録する' : (toolName === 'task_add' ? 'タスクに追加する' : '実行する');
        processedContent = processedContent.replace(/\[ACTION:.*?\]/, '');
        processedContent += `
            <div style="margin-top:12px; display:flex; gap:8px;">
                <button class="mini-link" style="background:var(--accent); color:#fff; border:none;" 
                        onclick='executeAiAction("${toolName}", ${JSON.stringify(args)})'>${btnLabel}</button>
                <button class="mini-link" style="background:rgba(255,255,255,0.05); color:var(--text-muted); border:none;" 
                        onclick="this.parentElement.remove()">キャンセル</button>
            </div>
        `;
    }

    html += `
        <div class="msg-content">
            <div class="msg-bubble">${processedContent}</div>
        </div>
        <div class="msg-time">${tStr}</div>
    `;
    
    div.innerHTML = html;
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

window.executeAiAction = async (toolName, args) => {
    try {
        await apiFetch('/api/execute_tool', { method: 'POST', body: JSON.stringify({ tool_name: toolName, args }) });
        showToast('実行しました');
        loadDashboard();
    } catch { showToast('実行に失敗しました', true); }
};

window.openIntelligentTaskModal = async (mode) => {
    const modal = $('#task-modal');
    const list = $('#modal-list');
    const title = $('#modal-title');
    if (!modal || !list || !title) return;

    title.textContent = mode === 'start' ? 'タスク開始' : 'タスク終了';
    list.innerHTML = '<div class="loading-placeholder">候補を取得中...</div>';
    modal.classList.remove('hidden');

    try {
        const data = await apiFetch('/api/task_candidates');
        const candidates = mode === 'start' ? data.start : data.end;
        
        if (!candidates || candidates.length === 0) {
            list.innerHTML = '<div class="loading-placeholder">候補がありません</div>';
        } else {
            list.innerHTML = candidates.map(c => `
                <div class="modal-item" onclick="selectTaskCandidate('${c}')">${escapeHtml(c)}</div>
            `).join('');
        }
    } catch (err) {
        console.error(err);
        list.innerHTML = '<div class="loading-placeholder">データ取得に失敗しました</div>';
    }

    const confirmBtn = $('#task-confirm-btn');
    if (confirmBtn) {
        confirmBtn.onclick = () => {
            const val = $('#custom-task-input').value.trim();
            if (val) selectTaskCandidate(val);
        };
    }
};

window.selectTaskCandidate = (name) => {
    const isStart = $('#modal-title').textContent === 'タスク開始';
    const modeStr = isStart ? '開始' : '終了';
    sendActionCommand(`${name}を${modeStr}`);
    closeTaskModal();
};

window.closeTaskModal = () => {
    $('#task-modal').classList.add('hidden');
    $('#custom-task-input').value = '';
};

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

        // Weather (Detailed Slots)
        const weatherEl = $('#dash-weather');
        if (weatherEl && data.weather && data.weather.slots) {
            weatherEl.innerHTML = `
                <div style="margin-bottom:12px; font-weight:600;">${data.weather.summary}</div>
                <div style="display:flex; flex-direction:column; gap:8px;">
                    ${data.weather.slots.map(s => `
                        <div style="display:flex; align-items:center; justify-content:space-between; font-size:0.85rem; padding:4px 0; border-bottom:1px solid rgba(255,255,255,0.05);">
                            <div style="width:50px;">${s.time}</div>
                            <div style="font-size:1.2rem; width:30px; text-align:center;">${s.icon}</div>
                            <div style="width:60px; color:var(--accent); text-align:right;">降水 ${s.pop}</div>
                            <div style="width:50px; text-align:right;">${s.temp}℃</div>
                        </div>
                    `).join('')}
                </div>
            `;
        } else if (weatherEl) {
            weatherEl.textContent = data.weather?.summary || '取得失敗';
        }

        // News
        const newsEl = $('#dash-news');
        if (newsEl && data.news) {
            newsEl.innerHTML = data.news.length ? data.news.map(n => `
                <div class="news-item">
                    <span class="news-dot"></span>
                    <a href="${n.link}" target="_blank" class="news-text">${escapeHtml(n.title)}</a>
                </div>
            `).join('') : '<div class="loading-placeholder">ニュースはありません</div>';
        }

        // Google Tasks (Separate lists)
        renderTaskGroup($('#dash-google-tasks-work'), data.google_tasks_work, '仕事');
        renderTaskGroup($('#dash-google-tasks-private'), data.google_tasks_private, 'プライベート');

        // Google Calendar
        const calEl = $('#dash-google-calendar');
        if (calEl && data.g_calendar) {
            calEl.innerHTML = data.g_calendar.length ? data.g_calendar.map(ev => {
                const startTime = ev.start ? (ev.start.includes('T') ? ev.start.split('T')[1].slice(0,5) : '終日') : '終日';
                return `
                    <div class="list-item">
                        <div class="li-time">${startTime}</div>
                        <div class="li-text">${escapeHtml(ev.summary)}</div>
                        <button class="icon-btn" onclick='openEditModal(${JSON.stringify({ type: "calendar", id: ev.id, title: ev.summary })})'>
                            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
                        </button>
                    </div>
                `;
            }).join('') : '<div class="loading-placeholder">予定はありません</div>';
        }

        // Obsidian Tasks (Lifelog)
        const obTaskEl = $('#dash-tasks');
        if (obTaskEl && data.tasks) {
            obTaskEl.innerHTML = data.tasks.length ? data.tasks.map(t => `
                <div class="list-item">
                    <div class="li-text" style="text-decoration: ${t.done ? 'line-through' : 'none'}">${escapeHtml(t.text)}</div>
                    <button class="icon-btn" onclick='openEditModal(${JSON.stringify({ type: "obsidian", text: t.text, done: t.done })})'>
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
                    </button>
                </div>
            `).join('') : '<div class="loading-placeholder">ログはありません</div>';
        }

        // 習慣
        const habitsContainer = $('#dash-habits');
        if (habitsContainer && data.habits) {
            habitsContainer.innerHTML = data.habits.length ? data.habits.map(t => `
                <div class="list-item">
                    <div class="li-text">${escapeHtml(t.title)}</div>
                    <button class="icon-btn" onclick='openEditModal(${JSON.stringify({ type: "habit", id: t.id, title: t.title })})'>
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
                    </button>
                </div>
            `).join('') : '<div class="loading-placeholder">登録された習慣はありません。</div>';
        }

        const sleepEl = $('#dash-sleep');
        if (sleepEl) {
            sleepEl.innerHTML = data.sleep?.score ? `スコア: ${data.sleep.score} / 時間: ${data.sleep.duration}` : '<div class="loading-placeholder">データなし</div>';
        }
        const diaryEl = $('#dash-alter-log');
        if (diaryEl) {
            diaryEl.innerHTML = (data.alter_log || '日記は順次生成されます。').replace(/\n/g, '<br>');
        }
        loadNotebooks();

    } catch (err) { console.error(err); }
}

function renderTaskGroup(container, tasks, listName) {
    if (!container) return;
    container.innerHTML = tasks && tasks.length ? tasks.map(t => `
        <div class="list-item">
            <div class="checkbox-custom" onclick="toggleGoogleTask('${t.id}', false, '${listName}')"></div>
            <div class="li-text">${escapeHtml(t.title)}</div>
            <button class="icon-btn" onclick='openEditModal(${JSON.stringify({ type: "google_task", id: t.id, title: t.title, listName })})'>
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
            </button>
        </div>
    `).join('') : '<div class="loading-placeholder">未完了のタスクはありません</div>';
}

// ========== ACTIONS ==========
window.openAddTaskModal = (listName) => {
    const title = prompt(`「${listName}」リストにタスクを追加:`);
    if (!title) return;
    addGoogleTask(title, listName);
};

async function addGoogleTask(title, listName) {
    try {
        await apiFetch('/api/google_tasks_action', {
            method: 'POST',
            body: JSON.stringify({ action: 'add', title, list_name: listName })
        });
        showToast('追加しました');
        loadDashboard();
    } catch { showToast('追加に失敗しました', true); }
}

window.toggleGoogleTask = async (taskId, currentStatus, listName) => {
    try {
        await apiFetch('/api/google_tasks_action', {
            method: 'POST',
            body: JSON.stringify({ action: 'toggle', task_id: taskId, completed: true, list_name: listName })
        });
        showToast('完了にしました');
        loadDashboard();
    } catch { showToast('更新に失敗しました', true); }
};

window.openAddEventModal = () => {
    const modal = $('#event-modal');
    if (!modal) return;
    modal.classList.remove('hidden');
    const now = new Date();
    const st = new Date(now.getTime() + 60*60*1000).toISOString().slice(0,16);
    $('#event-start').value = st;
    $('#event-end').value = st;
};

window.closeEventModal = () => $('#event-modal')?.classList.add('hidden');

window.saveEvent = async () => {
    const summary = $('#event-summary').value.trim();
    const start = $('#event-start').value;
    const end = $('#event-end').value;
    const desc = $('#event-desc').value.trim();
    if (!summary || !start) return showToast('必須項目が未入力です', true);

    const fmt = s => s.replace('T', ' ') + ':00';
    try {
        await apiFetch('/api/calendar_action', {
            method: 'POST',
            body: JSON.stringify({ action: 'add', summary, start_time: fmt(start), end_time: end ? fmt(end) : fmt(start), description: desc })
        });
        showToast('追加しました');
        closeEventModal();
        loadDashboard();
    } catch { showToast('追加に失敗しました', true); }
};

window.openEditModal = (item) => {
    window.currentEditTarget = item;
    const input = $('#edit-input');
    const toggleBtn = $('#edit-toggle-btn');
    if (input) input.value = item.text || item.title || '';
    if (toggleBtn) {
        if (item.type === 'obsidian') toggleBtn.classList.remove('hidden');
        else toggleBtn.classList.add('hidden');
    }
    $('#edit-modal')?.classList.remove('hidden');
};

window.closeEditModal = () => $('#edit-modal')?.classList.add('hidden');

$('#edit-save-btn')?.addEventListener('click', async () => {
    const t = window.currentEditTarget;
    const val = $('#edit-input').value.trim();
    if (!val) return;
    try {
        if (t.type === 'calendar') await apiFetch('/api/calendar_action', { method: 'POST', body: JSON.stringify({ action: 'update', event_id: t.id, summary: val }) });
        else if (t.type === 'google_task' || t.type === 'habit') await apiFetch('/api/google_tasks_action', { method: 'POST', body: JSON.stringify({ action: 'update', task_id: t.id, title: val, list_name: t.listName || (t.type === 'habit' ? '習慣' : null) }) });
        else if (t.type === 'obsidian') await apiFetch('/api/task_action', { method: 'POST', body: JSON.stringify({ action: 'update', old_text: t.text, new_text: val }) });
        showToast('保存しました');
        loadDashboard();
        closeEditModal();
    } catch { showToast('保存に失敗しました', true); }
});

$('#edit-delete-btn')?.addEventListener('click', async () => {
    const t = window.currentEditTarget;
    if (!confirm('削除しますか？')) return;
    try {
        if (t.type === 'calendar') await apiFetch('/api/calendar_action', { method: 'POST', body: JSON.stringify({ action: 'delete', event_id: t.id }) });
        else if (t.type === 'google_task' || t.type === 'habit') await apiFetch('/api/google_tasks_action', { method: 'POST', body: JSON.stringify({ action: 'delete', task_id: t.id, list_name: t.listName || (t.type === 'habit' ? '習慣' : null) }) });
        else if (t.type === 'obsidian') await apiFetch('/api/task_action', { method: 'POST', body: JSON.stringify({ action: 'delete', old_text: t.text }) });
        showToast('削除しました');
        loadDashboard();
        closeEditModal();
    } catch { showToast('削除に失敗しました', true); }
});

// ========== NOTEBOOK LM ==========
function loadNotebooks() {
    const notebooks = JSON.parse(localStorage.getItem('mng_notebook_links') || '[]');
    const container = $('#dash-notebooks');
    if (!container) return;
    if (notebooks.length === 0) {
        container.innerHTML = '<div class="p-20 text-center text-secondary">登録されたノートはありません</div>';
        return;
    }
    container.innerHTML = notebooks.map((nb, idx) => `
        <div class="list-item">
            <div class="li-text" style="cursor:pointer;" onclick="window.open('${nb.url}', '_blank')">
                <div style="font-weight:600;">${escapeHtml(nb.title)}</div>
                <div class="text-secondary" style="font-size:0.7rem;">最後に追加: ${nb.updated}</div>
            </div>
            <button class="icon-btn" onclick="deleteNotebook(${idx})" style="color:#ff4444;"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/></svg></button>
        </div>
    `).join('');
}

window.registerNotebook = () => {
    const title = prompt('ノートのタイトル:');
    if (!title) return;
    const url = prompt('NotebookLMのURL:');
    if (!url || !url.includes('notebooklm.google.com')) return alert('無効なURLです');
    const notebooks = JSON.parse(localStorage.getItem('mng_notebook_links') || '[]');
    notebooks.push({ title, url, updated: new Date().toLocaleDateString() });
    localStorage.setItem('mng_notebook_links', JSON.stringify(notebooks));
    loadNotebooks();
};

window.deleteNotebook = (idx) => {
    if (confirm('削除しますか？')) {
        const notebooks = JSON.parse(localStorage.getItem('mng_notebook_links') || '[]');
        notebooks.splice(idx, 1);
        localStorage.setItem('mng_notebook_links', JSON.stringify(notebooks));
        loadNotebooks();
    }
};

// ========== INIT ==========
function initMain() { loadHistory(); loadDashboard(); }

async function loadHistory() {
    try {
        const data = await apiFetch('/api/history?limit=20');
        if (chatMessages) {
            chatMessages.innerHTML = '<div class="chat-welcome"><h2>こんにちは。</h2><p>今日はどんなお手伝いをしましょうか？</p></div>';
            lastMsgDate = null;
            data.messages.forEach(m => appendMsg(m.role, m.content));
        }
    } catch {}
}

window.addEventListener('DOMContentLoaded', () => { if (apiKey) { showScreen('main-screen'); initMain(); } else { showScreen('login-screen'); } });
