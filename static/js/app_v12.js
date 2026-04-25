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
let isChatSending = false;
const chatForm = $('#chat-form');

if (chatForm) {
    chatForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        if (isChatSending) return;

        const msg = messageInput.value.trim();
        if (!msg) return;

        isChatSending = true;
        sendBtn.style.opacity = '0.5';
        sendBtn.disabled = true;

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
        } finally {
            isChatSending = false;
            sendBtn.style.opacity = '1';
            sendBtn.disabled = false;
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

function appendMsg(role, content, isoTimestamp = null) {
    if (!chatMessages) return;
    const now = isoTimestamp ? new Date(isoTimestamp) : new Date();
    const dStr = `${now.getFullYear()}年${now.getMonth()+1}月${now.getDate()}日(${['日','月','火','水','木','金','土'][now.getDay()]})`;
    const tStr = now.getHours().toString().padStart(2, '0') + ':' + now.getMinutes().toString().padStart(2, '0');

    if (lastMsgDate !== dStr) {
        const sep = document.createElement('div');
        sep.className = 'msg-date-separator';
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

        const btnLabel = toolName === 'calendar_add' ? 'カレンダーの予定に追加' : 
                         (toolName === 'task_add' ? 'タスクに追加する' : 
                         (toolName === 'task_delete' ? 'このタスクを削除' : '実行する'));
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

window.forceReload = async () => {
    if ('serviceWorker' in navigator) {
        const regs = await navigator.serviceWorker.getRegistrations();
        for (let reg of regs) await reg.unregister();
    }
    window.location.reload(true);
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
    if (!apiKey) {
        console.warn("No API Key - loadDashboard aborted");
        return;
    }
    try {
        const data = await apiFetch('/api/dashboard');
        
        const dateLabel = $('#dash-date-label');
        if (dateLabel) dateLabel.textContent = data.date || '---';

        // Weather
        const weatherEl = $('#dash-weather');
        if (weatherEl) {
            if (data.weather && data.weather.summary !== "取得失敗") {
                const w = data.weather;
                let html = `<div class="weather-summary">${escapeHtml(w.summary)}</div>`;
                if (w.max_temp || w.min_temp) {
                    html += `<div class="weather-temps">
                        <span class="temp-max">↑${w.max_temp}℃</span>
                        <span class="temp-min">↓${w.min_temp}℃</span>
                    </div>`;
                }
                if (w.slots && w.slots.length > 0) {
                    // 日付ラベルでグループ化
                    let currentDay = '';
                    html += `<div class="weather-slots">`;
                    w.slots.forEach(s => {
                        if (s.day !== currentDay) {
                            currentDay = s.day;
                            html += `<div class="weather-day-label">${escapeHtml(s.day)}</div>`;
                        }
                        html += `<div class="weather-slot">
                            <div class="ws-time">${s.time}</div>
                            <div class="ws-icon">${s.icon}</div>
                            <div class="ws-weather">${escapeHtml(s.weather || '')}</div>
                            <div class="ws-pop">☂${s.pop}</div>
                        </div>`;
                    });
                    html += `</div>`;
                }
                weatherEl.innerHTML = html;
            } else {
                weatherEl.innerHTML = `<div class="loading-placeholder">気象データを取得できませんでした</div>`;
            }
        }

        // News
        const newsEl = $('#dash-news');
        if (newsEl) {
            if (data.news && data.news.length > 0) {
                newsEl.innerHTML = data.news.map(n => `
                    <div class="news-item">
                        <span class="news-dot"></span>
                        <a href="${n.link}" target="_blank" class="news-text">${escapeHtml(n.title)}</a>
                    </div>
                `).join('');
            } else {
                newsEl.innerHTML = '<div class="loading-placeholder">現在、ニュースはありません</div>';
            }
        }

        // Google Tasks (Separate lists)
        renderTaskGroup($('#dash-google-tasks-work'), data.google_tasks_work, '仕事');
        renderTaskGroup($('#dash-google-tasks-private'), data.google_tasks_private, 'プライベート');

        // Google Calendar
        const calEl = $('#dash-google-calendar');
        if (calEl && data.g_calendar) {
            calEl.innerHTML = data.g_calendar.length ? data.g_calendar.map(ev => {
                return `
                    <div class="list-item">
                        <div class="li-time">${ev.time || '終日'}</div>
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

        // 習慣 - 簡易表示（詳細はloadHabitsで上書き）
        const habitsContainer = $('#dash-habits');
        if (habitsContainer && (!data.habits || data.habits.length === 0)) {
            habitsContainer.innerHTML = '<div class="loading-placeholder">登録された習慣はありません</div>';
        }
        loadHabits(); // 詳細データを非同期取得

        const sleepEl = $('#dash-sleep');
        if (sleepEl) {
            sleepEl.innerHTML = data.sleep?.score ? `スコア: ${data.sleep.score} / 時間: ${data.sleep.duration}` : '<div class="loading-placeholder">データなし</div>';
        }
        const diaryEl = $('#dash-alter-log');
        if (diaryEl) {
            diaryEl.innerHTML = (data.alter_log || '日記は順次生成されます。').replace(/\n/g, '<br>');
        }
        loadBookshelf();
        loadStockedLinks(); // 積読・ストックリンク読み込み

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
        else if (t.type === 'habit') await apiFetch('/api/habits/update', { method: 'POST', body: JSON.stringify({ habit_id: t.id, old_title: t.title, title: val }) });
        else if (t.type === 'google_task') await apiFetch('/api/google_tasks_action', { method: 'POST', body: JSON.stringify({ action: 'update', task_id: t.id, title: val, list_name: t.listName }) });
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
        else if (t.type === 'habit') await apiFetch('/api/habits/delete', { method: 'POST', body: JSON.stringify({ habit_id: t.id, old_title: t.title }) });
        else if (t.type === 'google_task') await apiFetch('/api/google_tasks_action', { method: 'POST', body: JSON.stringify({ action: 'delete', task_id: t.id, list_name: t.listName }) });
        else if (t.type === 'obsidian') await apiFetch('/api/task_action', { method: 'POST', body: JSON.stringify({ action: 'delete', old_text: t.text }) });
        showToast('削除しました');
        loadDashboard();
        closeEditModal();
    } catch { showToast('削除に失敗しました', true); }
});

// ========== HABIT TRACKER ==========
async function loadHabits() {
    try {
        const data = await apiFetch('/api/habits');
        const container = $('#dash-habits');
        const wrap = $('#habit-progress-wrap');
        const fill = $('#habit-progress-fill');
        const label = $('#habit-progress-label');
        if (!container) return;

        if (!data.habits || data.habits.length === 0) {
            container.innerHTML = '<div class="p-20 text-center text-secondary">登録された習慣はありません</div>';
            if (wrap) wrap.style.display = 'none';
            return;
        }

        const total = data.habits.length;
        const doneCount = data.habits.filter(h => data.today_done.includes(h.id)).length;
        const percent = Math.round((doneCount / total) * 100);

        if (wrap) {
            wrap.style.display = 'flex';
            fill.style.width = `${percent}%`;
            label.textContent = `${doneCount}/${total}`;
        }

        container.innerHTML = data.habits.map(h => {
            const isDone = data.today_done.includes(h.id);
            const streakText = data.streaks[h.id] || '';
            const streakBadge = streakText.includes('連続') ? `<span class="habit-streak">🔥${streakText.replace(/[^0-9]/g, '')}</span>` : `<span class="habit-streak" style="color:var(--text-muted)">${streakText}</span>`;
            return `
                <div class="habit-item ${isDone ? 'done' : ''}" id="habit-item-${h.id}">
                    <button class="habit-check-btn" onclick="completeHabit('${h.name}', '${h.id}')" ${isDone ? 'disabled' : ''}>✔</button>
                    <div class="habit-name">${escapeHtml(h.name)}</div>
                    ${streakBadge}
                    <button class="icon-btn" onclick='openEditModal(${JSON.stringify({ type: "habit", id: h.id, title: h.name })})'>
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
                    </button>
                </div>
            `;
        }).join('');
    } catch (e) {
        console.error("Habit fetch error", e);
    }
}

window.completeHabit = async (habitName, hId) => {
    try {
        const item = $(`#habit-item-${hId}`);
        if(item) item.classList.add('done');
        showToast(`「${habitName}」を完了しました！🎉`);
        await apiFetch('/api/habits/complete', {
            method: 'POST',
            body: JSON.stringify({ habit_name: habitName })
        });
        loadHabits(); // リロードしてストリークとバーを更新
    } catch { showToast('失敗しました', true); loadHabits(); }
};

// ========== BOOKSHELF (NOTEBOOK LM) ==========
function loadBookshelf() {
    const books = JSON.parse(localStorage.getItem('mng_books') || '[]');
    const container = $('#dash-bookshelf');
    if (!container) return;
    if (books.length === 0) {
        container.innerHTML = '<div class="p-20 text-center text-secondary">登録された書籍はありません</div>';
        return;
    }
    container.innerHTML = books.map((b, idx) => {
        const action = b.url ? "window.open('" + b.url + "', '_blank')" : "alert('NotebookLMのURLが登録されていません')";
        const openBtn = b.url ? `<button class="book-btn nlm" onclick="window.open('${b.url}', '_blank')">📚 開く</button>` : '';
        return `
        <div class="book-item">
            <div class="book-title" onclick="${action}">
                ${escapeHtml(b.title)}
            </div>
            <div class="book-actions">
                <button class="book-btn" onclick="copyBookNotes('${b.title}')">📋 メモコピー</button>
                ${openBtn}
                <button class="book-btn delete" onclick="deleteBook(${idx})">🗑️</button>
            </div>
        </div>
        `;
    }).join('');
}

window.openBookModal = () => {
    $('#book-title-input').value = '';
    $('#book-nlm-url-input').value = '';
    $('#book-modal').classList.remove('hidden');
};

window.closeBookModal = () => {
    $('#book-modal').classList.add('hidden');
};

window.saveBook = () => {
    const title = $('#book-title-input').value.trim();
    const url = $('#book-nlm-url-input').value.trim();
    if (!title) { alert('書籍タイトルを入力してください'); return; }
    
    const books = JSON.parse(localStorage.getItem('mng_books') || '[]');
    books.push({ title, url, added: new Date().toLocaleDateString() });
    localStorage.setItem('mng_books', JSON.stringify(books));
    closeBookModal();
    loadBookshelf();
    showToast('書籍を登録しました');
};

window.deleteBook = (idx) => {
    if (confirm('この書籍の紐付けを削除しますか？\n(Driveのメモデータ自体は削除されません)')) {
        const books = JSON.parse(localStorage.getItem('mng_books') || '[]');
        books.splice(idx, 1);
        localStorage.setItem('mng_books', JSON.stringify(books));
        loadBookshelf();
    }
};

window.copyBookNotes = async (title) => {
    showToast(`${title}のメモを取得中...`);
    try {
        const data = await apiFetch(`/api/book_notes?title=${encodeURIComponent(title)}`);
        if (!data.content) throw new Error("メモが空です");
        await navigator.clipboard.writeText(data.content);
        showToast('コピーしました！NotebookLMの「ソースを追加」に貼り付けてください');
    } catch (e) {
        console.error(e);
        showToast('コピーに失敗しました。メモが存在しない可能性があります。', true);
    }
};

window.loadStockedLinks = async () => {
    try {
        const data = await apiFetch('/api/links');
        const webEl = $('#dash-stocked-web');
        const ytEl = $('#dash-stocked-youtube');
        const recipeEl = $('#dash-stocked-recipe');
        const mapEl = $('#dash-stocked-map');
        const bookEl = $('#dash-stocked-book');

        const links = data.links || [];
        const webLinks = links.filter(l => l.type === 'web');
        const ytLinks = links.filter(l => l.type === 'youtube');
        const recipeLinks = links.filter(l => l.type === 'recipe');
        const mapLinks = links.filter(l => l.type === 'map');
        const bookLinks = links.filter(l => l.type === 'book');

        const renderGroup = (container, items, type) => {
            if (!container) return;
            if (items.length === 0) {
                container.innerHTML = '<div style="padding:10px 18px; color:var(--text-muted); font-size:0.85rem;">登録なし</div>';
                return;
            }
            container.innerHTML = items.map(lk => {
                const dateStr = new Date(lk.added_at).toLocaleString('ja-JP', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
                const actionBtn = `<button class="modal-btn" style="padding:3px 8px; font-size:0.7rem; background:rgba(0,186,152,0.1); color:var(--accent);" onclick='openLinkDetailsModal(${JSON.stringify(lk).replace(/'/g, "&#39;")})'>📝 詳細編集</button>`;
                
                let extraInfo = '';
                if(lk.purpose) extraInfo += `<span style="font-size:0.75rem; color:var(--accent); margin-right:8px;">🎯 ${escapeHtml(lk.purpose)}</span>`;
                if(lk.target_date) extraInfo += `<span style="font-size:0.75rem; color:var(--text-secondary); margin-right:8px;">📅 ${escapeHtml(lk.target_date)}</span>`;

                return `
                    <div class="list-item" id="stocked-link-${lk.id}" style="flex-direction:column; align-items:stretch; gap:4px;">
                        <div style="display:flex; align-items:center; gap:6px;">
                            <a href="${lk.url}" target="_blank" style="flex:1; color:var(--text); text-decoration:none; font-weight:500; font-size:0.85rem; line-height:1.3; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${escapeHtml(lk.title !== 'Untitled' ? lk.title : lk.url)}</a>
                        </div>
                        ${extraInfo ? `<div style="margin-top:2px;">${extraInfo}</div>` : ''}
                        <div style="display:flex; justify-content:space-between; align-items:center; margin-top:4px;">
                            <span style="font-size:0.65rem; color:var(--text-muted);">${dateStr}</span>
                            <div style="display:flex; gap:5px;">
                                ${actionBtn}
                                <button class="modal-btn" style="padding:3px 8px; font-size:0.7rem; background:rgba(255,80,80,0.15); color:#ff5050;" onclick="deleteStockedLink(${lk.id})">削除</button>
                            </div>
                        </div>
                    </div>
                `;
            }).join('');
        };

        renderGroup(webEl, webLinks, 'web');
        renderGroup(ytEl, ytLinks, 'youtube');
        renderGroup(recipeEl, recipeLinks, 'recipe');
        renderGroup(mapEl, mapLinks, 'map');
        renderGroup(bookEl, bookLinks, 'book');
    } catch (e) {
        console.error("StockedLinks fetch error", e);
    }
};

let currentEditLinkId = null;
let currentEditLinkType = null;

window.openLinkDetailsModal = (lk) => {
    currentEditLinkId = lk.id;
    currentEditLinkType = lk.type;
    
    $('#link-modal-title').textContent = "詳細編集: " + (lk.title.length > 15 ? lk.title.substring(0,15)+"..." : lk.title);
    
    // Type badge & field toggling
    const tr = { 'web': '🌐 ウェブ', 'youtube': '📺 YouTube', 'recipe': '🍳 レシピ', 'map': '🗺️ マップ', 'book': '📚 書籍' };
    $('#link-modal-type-badge').textContent = tr[lk.type] || lk.type;

    $('#field-purpose').style.display = 'flex';
    $('#field-summary').style.display = 'flex';
    $('#field-memo').style.display = 'flex';
    $('#field-date').style.display = ['recipe', 'map'].includes(lk.type) ? 'flex' : 'none';
    $('#field-url').style.display = lk.type === 'book' ? 'flex' : 'none';
    $('#link-extra-actions').style.display = lk.type === 'youtube' ? 'block' : 'none';
    
    $('#link-purpose-input').value = lk.purpose || '';
    $('#link-date-input').value = lk.target_date || '';
    $('#link-note-url-input').value = lk.linked_note_url || '';
    $('#link-summary-input').value = lk.summary || '';
    $('#link-memo-input').value = lk.memo || '';
    $('#link-calendar-check').checked = true; // default

    $('#link-details-modal').classList.remove('hidden');
};

window.closeLinkDetailsModal = () => {
    $('#link-details-modal').classList.add('hidden');
    currentEditLinkId = null;
};

// 共通保存処理
$('#link-save-btn')?.addEventListener('click', async () => {
    if(!currentEditLinkId) return;
    const btn = $('#link-save-btn');
    btn.textContent = '保存中...';
    btn.disabled = true;
    
    const reqData = {
        purpose: $('#link-purpose-input').value,
        summary: $('#link-summary-input').value,
        memo: $('#link-memo-input').value,
        target_date: $('#link-date-input').value,
        linked_note_url: $('#link-note-url-input').value,
        type: currentEditLinkType,
        add_to_calendar: $('#link-calendar-check').checked
    };

    try {
        await apiFetch(`/api/links/${currentEditLinkId}`, {
            method: 'PUT',
            body: JSON.stringify(reqData)
        });
        showToast('保存しました');
        closeLinkDetailsModal();
        loadStockedLinks();
    } catch (e) {
        showToast('保存に失敗しました', true);
    } finally {
        btn.textContent = '保存';
        btn.disabled = false;
    }
});

// YouTube -> レシピ移動
$('#link-move-recipe-btn')?.addEventListener('click', async () => {
    if(!currentEditLinkId || !confirm('このリンクをレシピに変更しますか？')) return;
    
    const reqData = { type: 'recipe' };
    try {
        await apiFetch(`/api/links/${currentEditLinkId}`, { method: 'PUT', body: JSON.stringify(reqData) });
        showToast('レシピに移動しました');
        closeLinkDetailsModal();
        loadStockedLinks();
    } catch (e) {
        showToast('移動に失敗しました', true);
    }
});

// YouTube / レシピ用の要約貼り付けモーダル
let pasteTargetLinkId = null;
window.openPasteSummaryModal = (linkId, title) => {
    pasteTargetLinkId = linkId;
    $('#paste-summary-title').textContent = title || 'リンク';
    $('#paste-summary-text').value = '';
    $('#paste-summary-modal').classList.remove('hidden');
};
window.closePasteSummaryModal = () => {
    $('#paste-summary-modal').classList.add('hidden');
    pasteTargetLinkId = null;
};
window.submitPasteSummary = async () => {
    const text = $('#paste-summary-text').value.trim();
    if (!text) { showToast('要約テキストを貼り付けてください', true); return; }
    if (!pasteTargetLinkId) return;

    $('#paste-submit-btn').textContent = '保存中...';
    $('#paste-submit-btn').disabled = true;

    try {
        const data = await apiFetch(`/api/links/${pasteTargetLinkId}/summarize_manual`, {
            method: 'POST',
            body: JSON.stringify({ summary: text })
        });
        showToast(data.message || '保存しました');
        closePasteSummaryModal();
        loadStockedLinks();
    } catch (e) {
        console.error(e);
        showToast('保存に失敗しました', true);
    } finally {
        $('#paste-submit-btn').textContent = '保存';
        $('#paste-submit-btn').disabled = false;
    }
};

window.deleteStockedLink = async (linkId) => {
    if (!confirm('このリンクを削除しますか？')) return;
    try {
        await apiFetch(`/api/links/${linkId}`, { method: 'DELETE' });
        showToast('リンクを削除しました');
        loadStockedLinks();
    } catch (e) {
        console.error(e);
        showToast('削除に失敗しました', true);
    }
};

window.copyDailySummary = async () => {
    const events = $('#dash-tasks')?.innerText || "";
    const insights = $('#dash-alter-log')?.innerText || "";
    const content = `本日の記録:\n\n${events}\n\n${insights}`;
    try {
        await navigator.clipboard.writeText(content);
        showToast('今日のサマリーをコピーしました！NotebookLMに貼り付けてください');
    } catch {
        showToast('コピーに失敗しました', true);
    }
};

// ========== INIT ==========
function initMain() {
    loadHistory();
    loadDashboard();

    // 共有ボタンからアプリが起動された場合の処理
    const params = new URLSearchParams(window.location.search);
    const sharedUrl = params.get('url') || '';
    const sharedText = params.get('text') || '';
    const sharedTitle = params.get('title') || '';

    // URLが共有されたらチャットに送信してストック
    const urlToStock = sharedUrl || extractUrl(sharedText);
    if (urlToStock && apiKey) {
        // URLパラメータを消す(履歴を綺麗にする)
        window.history.replaceState({}, '', '/');
        // チャットタブに切り替え
        switchTab('chat');
        // 少し待ってから送信（UIの初期化完了を待つ）
        setTimeout(async () => {
            const msg = sharedTitle ? `${sharedTitle}\n${urlToStock}` : urlToStock;
            appendMsg('user', msg);
            try {
                const data = await apiFetch('/api/chat', {
                    method: 'POST',
                    body: JSON.stringify({ message: msg })
                });
                appendMsg('assistant', data.reply);
            } catch (e) {
                appendMsg('assistant', 'リンクのストックに失敗しました。');
            }
        }, 500);
    }
}

function extractUrl(text) {
    if (!text) return '';
    const match = text.match(/https?:\/\/[^\s]+/);
    return match ? match[0] : '';
}

// Chat reset button
const resetBtn = $('#reset-chat-btn');
if (resetBtn) {
    resetBtn.addEventListener('click', async () => {
        if (!confirm('チャット履歴をすべて削除しますか？')) return;
        try {
            await apiFetch('/api/reset_history', { method: 'POST' });
            if (chatMessages) {
                chatMessages.innerHTML = '<div class="chat-welcome"><h2>こんにちは。</h2><p>今日はどんなお手伝いをしましょうか？</p></div>';
                lastMsgDate = null;
            }
            showToast('履歴をリセットしました');
        } catch { showToast('リセットに失敗しました', true); }
    });
}

// Daily report trigger
window.triggerDailyReport = async () => {
    if (!confirm('今日の日次整理を実行しますか？\n会話ログを元にDaily Journal、Events & Actions、Insights & Thoughts、Next Actionsを生成し、Obsidianに保存します。')) return;
    showToast('日次整理を実行中...');
    try {
        const data = await apiFetch('/api/daily_report', { method: 'POST' });
        showToast(data.message || '日次整理が完了しました');
        loadDashboard();
    } catch { showToast('日次整理に失敗しました', true); }
};

// ========== 機能1: ブリーフィング ==========
window.runBriefing = async () => {
    showToast('ブリーフィングを生成中...');
    try {
        const data = await apiFetch('/api/briefing', { method: 'POST' });
        appendMsg('assistant', data.reply);
        showToast(data.type === 'morning' ? '朝のブリーフィングです' : '夜のレビューです');
    } catch (e) {
        console.error(e);
        showToast('ブリーフィングの生成に失敗しました', true);
    }
};

// ========== 機能X: タスク整理 (トリアージ) ==========
window.runTaskTriage = async (listName) => {
    showToast(`「${listName}」のタスクを整理中...`);
    try {
        const data = await apiFetch('/api/task_triage', { 
            method: 'POST',
            body: JSON.stringify({ list_name: listName })
        });
        appendMsg('assistant', data.reply);
        showToast('整理提案が完了しました');
    } catch (e) {
        console.error(e);
        showToast('タスク整理の提案生成に失敗しました', true);
    }
};

// ========== 機能7: タスクブレイクダウン ==========
let currentBreakdownSubtasks = [];

window.openTaskBreakdownModal = () => {
    $('#breakdown-task-input').value = '';
    $('#breakdown-result').style.display = 'none';
    $('#breakdown-generate-btn').style.display = '';
    $('#breakdown-apply-btn').style.display = 'none';
    $('#breakdown-list').innerHTML = '';
    currentBreakdownSubtasks = [];
    $('#breakdown-modal').classList.remove('hidden');
};

window.closeBreakdownModal = () => {
    $('#breakdown-modal').classList.add('hidden');
};

window.generateBreakdown = async () => {
    const task = $('#breakdown-task-input').value.trim();
    if (!task) { showToast('タスクを入力してください', true); return; }

    $('#breakdown-generate-btn').textContent = '分析中...';
    $('#breakdown-generate-btn').disabled = true;

    try {
        const data = await apiFetch('/api/task_breakdown', {
            method: 'POST',
            body: JSON.stringify({ message: task })
        });

        currentBreakdownSubtasks = data.subtasks;
        const listEl = $('#breakdown-list');
        listEl.innerHTML = data.subtasks.map((st, i) => `
            <div class="modal-item" style="display:flex; justify-content:space-between; align-items:center; cursor:default;">
                <span>${escapeHtml(st.title)}</span>
                <span style="font-size:0.75rem; color:var(--text-muted);">${escapeHtml(st.estimate || '')}</span>
            </div>
        `).join('');

        $('#breakdown-result').style.display = '';
        $('#breakdown-generate-btn').style.display = 'none';
        $('#breakdown-apply-btn').style.display = '';
    } catch (e) {
        console.error(e);
        showToast('タスク分解に失敗しました', true);
    } finally {
        $('#breakdown-generate-btn').textContent = 'AIで分解';
        $('#breakdown-generate-btn').disabled = false;
    }
};

window.applyBreakdown = async () => {
    if (currentBreakdownSubtasks.length === 0) return;

    const listName = $('#breakdown-list-name').value;
    $('#breakdown-apply-btn').textContent = '追加中...';
    $('#breakdown-apply-btn').disabled = true;

    try {
        const data = await apiFetch('/api/task_breakdown/apply', {
            method: 'POST',
            body: JSON.stringify({
                list_name: listName,
                subtasks: currentBreakdownSubtasks
            })
        });
        showToast(data.message || '追加しました');
        appendMsg('assistant', data.message);
        closeBreakdownModal();
        loadDashboard();
    } catch (e) {
        console.error(e);
        showToast('タスク追加に失敗しました', true);
    } finally {
        $('#breakdown-apply-btn').textContent = 'Tasksに追加';
        $('#breakdown-apply-btn').disabled = false;
    }
};

// ========== 機能14: 健康と気分の相関分析 ==========
window.runHealthCorrelation = async () => {
    showToast('1週間のデータを分析中... (少し時間がかかります)');
    try {
        const data = await apiFetch('/api/health_correlation', { method: 'POST' });
        appendMsg('assistant', data.analysis);
    } catch (e) {
        console.error(e);
        showToast('健康分析に失敗しました', true);
    }
};

async function loadHistory() {
    try {
        const data = await apiFetch('/api/history?limit=20');
        if (chatMessages) {
            chatMessages.innerHTML = '<div class="chat-welcome"><h2>こんにちは。</h2><p>今日はどんなお手伝いをしましょうか？</p></div>';
            lastMsgDate = null;
            data.messages.forEach(m => appendMsg(m.role, m.content, m.timestamp));
        }
    } catch {}
}

window.addEventListener('DOMContentLoaded', () => { if (apiKey) { showScreen('main-screen'); initMain(); } else { showScreen('login-screen'); } });
