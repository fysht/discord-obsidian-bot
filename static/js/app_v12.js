const API_BASE = '';
let apiKey = localStorage.getItem('secretary_api_key') || '';
let lastMsgDate = null;

// 各カテゴリーごとのソート状態を保持するオブジェクト
let linkSorts = {
    web: 'newest',
    youtube: 'newest',
    recipe: 'newest',
    map: 'newest',
    book: 'newest'
};

const $ = sel => document.querySelector(sel);
const $$ = sel => document.querySelectorAll(sel);

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
        } catch (err) { $('#login-error').textContent = 'パスワードが違います'; }
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

const resetChatBtn = $('#reset-chat-btn');
if (resetChatBtn) {
    resetChatBtn.addEventListener('click', async () => {
        if (!confirm('チャット履歴を完全に消去しますか？（AI側の短期記憶もクリアされます）')) return;
        try {
            await apiFetch('/api/reset_history', { method: 'POST' });
            if (chatMessages) {
                chatMessages.innerHTML = '<div class="chat-welcome"><h2>リセットしました。</h2><p>また新しくお話ししましょう！</p></div>';
                lastMsgDate = null;
            }
            showToast('チャットをリセットしました');
        } catch (err) {
            showToast('リセットに失敗しました', true);
        }
    });
}

$$('.nav-item').forEach(item => {
    item.addEventListener('click', () => { switchTab(item.dataset.tab); });
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

        try {
            const data = await apiFetch('/api/chat', { method: 'POST', body: JSON.stringify({ message: msg }) });
            appendMsg('assistant', data.reply);
            
            // AI応答後にダッシュボードをリロードして反映させる
            if (typeof loadDashboard === 'function') loadDashboard();
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
    if (role === 'assistant') html += `<img src="/static/icons/avatar.png" class="msg-avatar">`;
    let processedContent = escapeHtml(content).replace(/\n/g, '<br>');
    
    html += `
        <div class="msg-content">
            <div class="msg-bubble" style="word-break: break-all;">${processedContent}</div>
        </div>
        <div class="msg-time">${tStr}</div>
    `;
    div.innerHTML = html;
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

async function loadDashboard() {
    if (!apiKey) return;
    try {
        const data = await apiFetch('/api/dashboard');
        
        const dateLabel = $('#dash-date-label');
        if (dateLabel) dateLabel.textContent = data.date || '---';

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
                    html += `<div class="weather-slots">`;
                    let lastDay = '';
                    w.slots.forEach(s => {
                        if (s.day !== lastDay) {
                            html += `<div class="weather-day-label">${escapeHtml(s.day)}</div>`;
                            lastDay = s.day;
                        }
                        html += `
                            <div class="weather-slot">
                                <div class="ws-time">${escapeHtml(s.time)}</div>
                                <div class="ws-icon">${s.icon}</div>
                                <div class="ws-weather">${escapeHtml(s.weather)}</div>
                                <div class="ws-pop">${escapeHtml(s.pop)}</div>
                                <div class="ws-temp">${escapeHtml(s.temp)}℃</div>
                            </div>
                        `;
                    });
                    html += `</div>`;
                }
                weatherEl.innerHTML = html;
            } else weatherEl.innerHTML = `<div class="loading-placeholder">気象データを取得できませんでした</div>`;
        }

        const newsEl = $('#dash-news');
        if (newsEl) {
            if (data.news && data.news.length > 0) {
                newsEl.innerHTML = data.news.map(n => `
                    <div class="news-item">
                        <span class="news-dot"></span>
                        <a href="${n.link}" target="_blank" class="news-text">${escapeHtml(n.title)}</a>
                    </div>
                `).join('');
            } else newsEl.innerHTML = '<div class="loading-placeholder">現在、ニュースはありません</div>';
        }

        renderTaskGroup($('#dash-google-tasks-work'), data.google_tasks_work, '仕事');
        renderTaskGroup($('#dash-google-tasks-private'), data.google_tasks_private, 'プライベート');

        const calEl = $('#dash-google-calendar');
        if (calEl && data.g_calendar) {
            calEl.innerHTML = data.g_calendar.length ? data.g_calendar.map(ev => {
                return `
                    <div class="list-item">
                        <div class="li-time">${ev.time || '終日'}</div>
                        <div class="li-text">${escapeHtml(ev.summary)}</div>
                    </div>
                `;
            }).join('') : '<div class="loading-placeholder">予定はありません</div>';
        }

        const obTaskEl = $('#dash-tasks');
        if (obTaskEl && data.tasks) {
            obTaskEl.innerHTML = data.tasks.length ? data.tasks.map(t => {
                const isLog = t.is_log || false;
                const isRunning = isLog && t.text.includes('▶');
                
                if (isLog) {
                    return `
                        <div class="list-item" style="border-left: 3px solid ${isRunning ? 'var(--primary)' : 'rgba(255,255,255,0.1)'}; padding-left: 12px; margin-bottom: 6px; align-items: flex-start; flex-direction: column;">
                            <div class="li-text" style="font-family:'Courier New', Courier, monospace; font-size:0.85rem; line-height:1.5;">${escapeHtml(t.text)}</div>
                        </div>
                    `;
                } else {
                    return `
                        <div class="list-item">
                            <div class="li-text" style="text-decoration: ${t.done ? 'line-through' : 'none'}">${escapeHtml(t.text)}</div>
                        </div>
                    `;
                }
            }).join('') : '<div class="loading-placeholder">ログはありません</div>';
        }

        renderTaskGroup($('#dash-habits'), data.habits, '習慣');
        const habitProgressWrap = $('#habit-progress-wrap');
        if (habitProgressWrap) habitProgressWrap.style.display = 'none';

        const sleepEl = $('#dash-sleep');
        if (sleepEl) {
            if (data.sleep && data.sleep.score !== "N/A") {
                sleepEl.innerHTML = `
                    <div class="sleep-stats">
                        <div class="sleep-score">
                            <span class="ss-value">${data.sleep.score}</span>
                            <span class="ss-label">点</span>
                        </div>
                        <div class="sleep-duration">${data.sleep.duration}</div>
                    </div>
                `;
            } else {
                sleepEl.innerHTML = '<div class="loading-placeholder">昨夜のデータがありません</div>';
            }
            loadSleepTrend();
        }
        
        const diaryEl = $('#dash-alter-log');
        if (diaryEl) diaryEl.innerHTML = (data.alter_log || '日記は順次生成されます。').replace(/\n/g, '<br>');
        
        // 「書籍＆ナレッジ」のテキストを「書籍」に置換
        document.querySelectorAll('.section-title').forEach(el => {
            if(el.textContent.includes('書籍＆ナレッジ')) {
                el.textContent = el.textContent.replace('書籍＆ナレッジ', '書籍');
            }
        });

        loadStockedLinks();

    } catch (err) { console.error(err); }
}

let _currentWorkTasks = [];
let _currentPrivateTasks = [];
let _currentHabitTasks = [];

function renderTaskGroup(container, tasks, listName) {
    if (!container) return;
    if (listName === '仕事') _currentWorkTasks = tasks || [];
    if (listName === 'プライベート') _currentPrivateTasks = tasks || [];
    if (listName === '習慣') _currentHabitTasks = tasks || [];

    container.innerHTML = tasks && tasks.length ? tasks.map((t, idx) => `
        <div class="list-item" style="gap:6px;">
            <div class="checkbox-custom" onclick="toggleGoogleTask('${t.id}', '${listName}')"></div>
            <div class="li-text" style="flex:1;">${escapeHtml(t.title)}</div>
            <div style="display:flex; flex-direction:column; gap:1px;">
                <button class="icon-btn" style="padding:2px; font-size:0.7rem; opacity:${idx === 0 ? '0.3' : '1'};" ${idx === 0 ? 'disabled' : ''} onclick="moveGTask('${t.id}', 'up', '${listName}')">▲</button>
                <button class="icon-btn" style="padding:2px; font-size:0.7rem; opacity:${idx === tasks.length - 1 ? '0.3' : '1'};" ${idx === tasks.length - 1 ? 'disabled' : ''} onclick="moveGTask('${t.id}', 'down', '${listName}')">▼</button>
            </div>
        </div>
    `).join('') : '<div class="loading-placeholder">未完了のタスクはありません</div>';
}

window.toggleGoogleTask = async (taskId, listName) => {
    try {
        await apiFetch('/api/google_tasks_action', {
            method: 'POST',
            body: JSON.stringify({ action: 'toggle', task_id: taskId, completed: true, list_name: listName })
        });
        showToast('完了にしました！');
        loadDashboard();
    } catch (e) {
        showToast('更新に失敗しました', true);
    }
};

window.moveGTask = async (taskId, direction, listName) => {
    const tasks = listName === '仕事' ? _currentWorkTasks : listName === '習慣' ? _currentHabitTasks : _currentPrivateTasks;
    const idx = tasks.findIndex(t => t.id === taskId);
    if (idx === -1) return;

    let previousId = null;
    if (direction === 'up') {
        if (idx <= 0) return;
        previousId = idx >= 2 ? tasks[idx - 2].id : null;
    } else {
        if (idx >= tasks.length - 1) return;
        previousId = tasks[idx + 1].id;
    }

    try {
        await apiFetch('/api/google_tasks_move', {
            method: 'POST',
            body: JSON.stringify({ task_id: taskId, previous_task_id: previousId, list_name: listName })
        });
        loadDashboard();
    } catch (e) {
        showToast('移動に失敗しました', true);
    }
};

// ========== MISSING UTILITY FUNCTIONS ==========
window.forceReload = () => window.location.reload(true);

window.closeBreakdownModal = () => {
    $('#breakdown-modal')?.classList.add('hidden');
};

window.closeEditModal = () => {
    $('#edit-modal')?.classList.add('hidden');
};

// ========== ADD TASK MODAL ==========
window.openAddTaskModal = (listName) => {
    const modal = $('#add-task-modal');
    if (!modal) return;
    const title = { '仕事': '仕事タスクを追加', 'プライベート': 'プライベートタスクを追加', '習慣': '習慣を追加' };
    $('#add-task-modal-title').textContent = title[listName] || 'タスクを追加';
    $('#add-task-list-name').value = listName;
    $('#add-task-title-input').value = '';
    modal.classList.remove('hidden');
    setTimeout(() => $('#add-task-title-input').focus(), 100);
};

window.closeAddTaskModal = () => {
    $('#add-task-modal')?.classList.add('hidden');
};

window.saveNewTask = async () => {
    const title = $('#add-task-title-input').value.trim();
    const listName = $('#add-task-list-name').value;
    if (!title) { showToast('タスクを入力してください', true); return; }

    const btn = $('#add-task-save-btn');
    btn.textContent = '追加中...';
    btn.disabled = true;

    try {
        await apiFetch('/api/google_tasks_action', {
            method: 'POST',
            body: JSON.stringify({ action: 'add', title, list_name: listName })
        });
        showToast(`「${title}」を追加しました`);
        closeAddTaskModal();
        loadDashboard();
    } catch (e) {
        showToast('追加に失敗しました', true);
    } finally {
        btn.textContent = '追加';
        btn.disabled = false;
    }
};

// Enterキーで送信
document.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !$('#add-task-modal')?.classList.contains('hidden')) {
        const focused = document.activeElement;
        if (focused && focused.id === 'add-task-title-input') saveNewTask();
    }
});

// ========== CALENDAR EVENT MODAL ==========
window.openAddEventModal = () => {
    const now = new Date();
    const toLocal = d => {
        const off = d.getTimezoneOffset() * 60000;
        return new Date(d - off).toISOString().slice(0, 16);
    };
    const start = new Date(now);
    start.setMinutes(0, 0, 0);
    start.setHours(start.getHours() + 1);
    const end = new Date(start);
    end.setHours(end.getHours() + 1);

    $('#event-summary').value = '';
    $('#event-start').value = toLocal(start);
    $('#event-end').value = toLocal(end);
    $('#event-desc').value = '';
    $('#event-modal').classList.remove('hidden');
    setTimeout(() => $('#event-summary').focus(), 100);
};

window.closeEventModal = () => {
    $('#event-modal')?.classList.add('hidden');
};

window.saveEvent = async () => {
    const summary = $('#event-summary').value.trim();
    if (!summary) { showToast('タイトルを入力してください', true); return; }

    const start = $('#event-start').value;
    const end = $('#event-end').value;
    const desc = $('#event-desc').value;

    const btn = document.querySelector('#event-modal .modal-btn.submit');
    if (btn) { btn.textContent = '追加中...'; btn.disabled = true; }

    try {
        await apiFetch('/api/calendar_action', {
            method: 'POST',
            body: JSON.stringify({
                action: 'add',
                summary,
                start_time: start.replace('T', ' ') + ':00',
                end_time: end.replace('T', ' ') + ':00',
                description: desc
            })
        });
        showToast('予定を追加しました');
        closeEventModal();
        loadDashboard();
    } catch (e) {
        showToast('追加に失敗しました', true);
    } finally {
        if (btn) { btn.textContent = '追加'; btn.disabled = false; }
    }
};

// ========== ACTIONS ==========
window.sendActionCommand = (cmd) => {
    if (!messageInput) return;
    messageInput.value = cmd;
    const event = new Event('submit', { cancelable: true });
    $('#chat-form').dispatchEvent(event);
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
    sendActionCommand(`${name}${modeStr}`);
    closeTaskModal();
};

window.closeTaskModal = () => {
    $('#task-modal').classList.add('hidden');
    $('#custom-task-input').value = '';
};

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

window.openTaskBreakdownModal = () => {
    $('#breakdown-task-input').value = '';
    $('#breakdown-result').style.display = 'none';
    const genBtn = $('#breakdown-generate-btn');
    if(genBtn) {
        genBtn.style.display = '';
        genBtn.textContent = 'AIで分解';
        genBtn.disabled = false;
    }
    $('#breakdown-apply-btn').style.display = 'none';
    $('#breakdown-list').innerHTML = '';
    currentBreakdownSubtasks = [];
    $('#breakdown-modal').classList.remove('hidden');
};

let currentBreakdownSubtasks = [];
window.generateBreakdown = async () => {
    const task = $('#breakdown-task-input').value.trim();
    if (!task) { showToast('タスクを入力してください', true); return; }

    const btn = $('#breakdown-generate-btn');
    btn.textContent = '分析中...';
    btn.disabled = true;

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
        btn.textContent = 'AIで分解';
        btn.disabled = false;
    }
};

window.applyBreakdown = async () => {
    if (currentBreakdownSubtasks.length === 0) return;
    const listName = $('#breakdown-list-name').value;
    const btn = $('#breakdown-apply-btn');
    btn.textContent = '追加中...';
    btn.disabled = true;

    try {
        const data = await apiFetch('/api/task_breakdown/apply', {
            method: 'POST',
            body: JSON.stringify({ list_name: listName, subtasks: currentBreakdownSubtasks })
        });
        showToast(data.message || '追加しました');
        appendMsg('assistant', data.message);
        $('#breakdown-modal').classList.add('hidden');
        loadDashboard();
    } catch (e) {
        console.error(e);
        showToast('タスク追加に失敗しました', true);
    } finally {
        btn.textContent = 'Tasksに追加';
        btn.disabled = false;
    }
};

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

window.triggerDailyReport = async () => {
    if (!confirm('今日の日次整理を実行しますか？\n会話ログを元にDaily Journal、Events & Actions等を生成し、Obsidianに保存します。')) return;
    showToast('日次整理を実行中...');
    try {
        const data = await apiFetch('/api/daily_report', { method: 'POST' });
        showToast(data.message || '日次整理が完了しました');
        loadDashboard();
    } catch { showToast('日次整理に失敗しました', true); }
};

window.copyDailySummary = async () => {
    const events = $('#dash-tasks')?.innerText || "";
    const insights = $('#dash-alter-log')?.innerText || "";
    const content = `本日の記録:\n\n${events}\n\n${insights}`;
    try {
        await navigator.clipboard.writeText(content);
        showToast('今日のサマリーをコピーしました！');
    } catch { showToast('コピーに失敗しました', true); }
};

window.openManualAddModal = (type) => {
    const title = prompt("タイトルを入力してください:");
    if (!title) return;
    const url = prompt("URLがあれば入力してください（任意）:", "") || "";
    
    apiFetch('/api/links', {
        method: 'POST',
        body: JSON.stringify({ title, type: type, url })
    }).then(() => {
        showToast('手動で追加しました');
        loadStockedLinks();
    }).catch(() => showToast('追加に失敗しました', true));
};

window.changeLinkSort = (type, val) => {
    linkSorts[type] = val;
    loadStockedLinks();
};

window.loadStockedLinks = async () => {
    try {
        const data = await apiFetch('/api/links');
        const webEl = $('#dash-stocked-web');
        const ytEl = $('#dash-stocked-youtube');
        const recipeEl = $('#dash-stocked-recipe');
        const mapEl = $('#dash-stocked-map');
        const bookEl = $('#dash-stocked-book');

        let links = data.links || [];
        
        const getSortFn = (type) => {
            return (a, b) => {
                const sortType = linkSorts[type];
                if (sortType === 'newest') return new Date(b.added_at) - new Date(a.added_at);
                if (sortType === 'oldest') return new Date(a.added_at) - new Date(b.added_at);
                if (sortType === 'title') return a.title.localeCompare(b.title);
                return 0;
            };
        };

        const webLinks = links.filter(l => l.type === 'web').sort(getSortFn('web'));
        const ytLinks = links.filter(l => l.type === 'youtube').sort(getSortFn('youtube'));
        const recipeLinks = links.filter(l => l.type === 'recipe').sort(getSortFn('recipe'));
        const mapLinks = links.filter(l => l.type === 'map').sort(getSortFn('map'));
        const bookLinks = links.filter(l => l.type === 'book').sort(getSortFn('book'));

        const setupHeader = (id, type) => {
            const container = $(`#${id}`);
            if(!container) return;
            const prev = container.previousElementSibling;
            
            if (prev && !prev.querySelector('.header-controls')) {
                const ctrl = document.createElement('div');
                ctrl.className = 'header-controls';
                ctrl.style.display = 'inline-flex';
                ctrl.style.gap = '8px';
                ctrl.style.marginLeft = 'auto';
                ctrl.style.alignItems = 'center';
                
                // 個別のソートプルダウン
                const sortSelect = document.createElement('select');
                sortSelect.style.cssText = "background:var(--bg-elevated); color:var(--text); border:1px solid var(--border-glass); padding:2px 4px; border-radius:4px; font-size:0.75rem; cursor:pointer;";
                sortSelect.innerHTML = `
                    <option value="newest" ${linkSorts[type]==='newest'?'selected':''}>新しい順</option>
                    <option value="oldest" ${linkSorts[type]==='oldest'?'selected':''}>古い順</option>
                    <option value="title" ${linkSorts[type]==='title'?'selected':''}>タイトル順</option>
                `;
                sortSelect.onchange = (e) => changeLinkSort(type, e.target.value);
                
                const addBtn = document.createElement('button');
                addBtn.className = 'modal-btn';
                addBtn.style.cssText = "padding:2px 8px; font-size:0.7rem;";
                addBtn.textContent = '＋ 追加';
                addBtn.onclick = () => openManualAddModal(type);
                
                ctrl.appendChild(sortSelect);
                ctrl.appendChild(addBtn);
                
                prev.style.display = 'flex';
                prev.style.justifyContent = 'space-between';
                prev.style.alignItems = 'center';
                prev.appendChild(ctrl);
            } else if (prev) {
                // 既に存在する場合は選択状態を更新
                const sel = prev.querySelector('select');
                if (sel) sel.value = linkSorts[type];
            }
        };

        setupHeader('dash-stocked-web', 'web');
        setupHeader('dash-stocked-youtube', 'youtube');
        setupHeader('dash-stocked-recipe', 'recipe');
        setupHeader('dash-stocked-map', 'map');
        setupHeader('dash-stocked-book', 'book');

        // 古い全体のソートプルダウンがあれば削除
        const oldWrap = $('#link-sort-wrapper');
        if (oldWrap) oldWrap.remove();

        const renderGroup = (container, items) => {
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
                    <div class="list-item" id="stocked-link-${lk.id}" style="flex-direction:column; align-items:stretch; gap:4px; min-width: 0;">
                        <div style="display:flex; align-items:flex-start; gap:6px; min-width: 0;">
                            ${lk.url ? `<a href="${lk.url}" target="_blank" style="flex:1; color:var(--text); text-decoration:none; font-weight:500; font-size:0.85rem; line-height:1.4; word-break: break-all; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;">${escapeHtml(lk.title !== 'Untitled' ? lk.title : lk.url)}</a>` 
                                     : `<span style="flex:1; color:var(--text); font-weight:500; font-size:0.85rem; line-height:1.4; display:block;">${escapeHtml(lk.title)}</span>`}
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

        renderGroup(webEl, webLinks);
        renderGroup(ytEl, ytLinks);
        renderGroup(recipeEl, recipeLinks);
        renderGroup(mapEl, mapLinks);
        renderGroup(bookEl, bookLinks);
        
        loadNotebooks();

    } catch (e) { console.error("StockedLinks fetch error", e); }
};

// ========== NOTEBOOK LM ==========
window.loadNotebooks = () => {
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
};

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

let currentEditLinkId = null;
window.openLinkDetailsModal = (lk) => {
    currentEditLinkId = lk.id;
    
    $('#link-modal-title').innerHTML = `<input type="text" id="link-title-input" value="${escapeHtml(lk.title)}" style="width:100%; background:rgba(0,0,0,0.2); border:1px solid var(--border-glass); color:#fff; padding:8px; border-radius:4px; font-size:0.9rem;" placeholder="タイトル">`;
    
    $('#link-modal-type-badge').innerHTML = `
        <select id="link-type-select" style="background:var(--bg-elevated); color:inherit; border:1px solid var(--border-glass); border-radius:4px; outline:none; font-size:0.75rem; font-weight:bold; cursor:pointer; padding:2px 4px;">
            <option value="web" ${lk.type==='web'?'selected':''}>🌐 ウェブ</option>
            <option value="youtube" ${lk.type==='youtube'?'selected':''}>📺 YouTube</option>
            <option value="recipe" ${lk.type==='recipe'?'selected':''}>🍳 レシピ</option>
            <option value="map" ${lk.type==='map'?'selected':''}>🗺️ マップ</option>
            <option value="book" ${lk.type==='book'?'selected':''}>📚 書籍</option>
        </select>
    `;

    $('#field-purpose').style.display = 'flex';
    $('#field-summary').style.display = 'flex';
    $('#field-memo').style.display = 'flex';
    $('#field-date').style.display = ['recipe', 'map', 'book'].includes(lk.type) ? 'flex' : 'none';
    
    if (!$('#purpose-options')) {
        const dl = document.createElement('datalist');
        dl.id = 'purpose-options';
        dl.innerHTML = `<option value="後で読む/見る"><option value="今週中に実行"><option value="鑑賞済み/完了"><option value="参考資料"><option value="作ってみたい">`;
        document.body.appendChild(dl);
    }
    const pInput = $('#link-purpose-input');
    if (pInput) pInput.setAttribute('list', 'purpose-options');

    $('#link-purpose-input').value = lk.purpose || '';
    $('#link-date-input').value = lk.target_date || '';
    $('#link-note-url-input').value = lk.linked_note_url || '';
    $('#link-summary-input').value = lk.summary || '';
    $('#link-memo-input').value = lk.memo || '';
    $('#link-calendar-check').checked = true;

    $('#link-details-modal').classList.remove('hidden');
};

window.closeLinkDetailsModal = () => {
    $('#link-details-modal').classList.add('hidden');
    currentEditLinkId = null;
};

$('#link-save-btn')?.addEventListener('click', async () => {
    if(!currentEditLinkId) return;
    const btn = $('#link-save-btn');
    btn.textContent = '保存中...';
    btn.disabled = true;
    
    const reqData = {
        title: $('#link-title-input').value,
        type: $('#link-type-select').value,
        purpose: $('#link-purpose-input').value,
        summary: $('#link-summary-input').value,
        memo: $('#link-memo-input').value,
        target_date: $('#link-date-input').value,
        linked_note_url: $('#link-note-url-input').value,
        add_to_calendar: $('#link-calendar-check').checked
    };

    try {
        await apiFetch(`/api/links/${currentEditLinkId}`, {
            method: 'PUT',
            body: JSON.stringify(reqData)
        });
        showToast('保存・同期しました');
        closeLinkDetailsModal();
        loadStockedLinks();
    } catch (e) {
        showToast('保存に失敗しました', true);
    } finally {
        btn.textContent = '保存';
        btn.disabled = false;
    }
});

window.deleteStockedLink = async (linkId) => {
    if (!confirm('このデータを削除しますか？')) return;
    try {
        await apiFetch(`/api/links/${linkId}`, { method: 'DELETE' });
        showToast('削除しました');
        loadStockedLinks();
    } catch (e) { showToast('削除に失敗しました', true); }
};

function initMain() {
    loadHistory();
    loadDashboard();

    const params = new URLSearchParams(window.location.search);
    const sharedUrl = params.get('url') || '';
    const sharedText = params.get('text') || '';
    const sharedTitle = params.get('title') || '';

    const urlToStock = sharedUrl || (sharedText ? (sharedText.match(/https?:\/\/[^\s]+/) || [''])[0] : '');
    if (urlToStock && apiKey) {
        window.history.replaceState({}, '', '/');
        switchTab('chat');
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

async function loadHistory() {
    try {
        const data = await apiFetch('/api/history?limit=100');
        if (chatMessages) {
            chatMessages.innerHTML = '<div class="chat-welcome"><h2>こんにちは。</h2><p>今日はどんなお手伝いをしましょうか？</p></div>';
            lastMsgDate = null;
            data.messages.forEach(m => appendMsg(m.role, m.content, m.timestamp));
        }
    } catch {}
}

window.addEventListener('DOMContentLoaded', () => { if (apiKey) { showScreen('main-screen'); initMain(); } else { showScreen('login-screen'); } });

async function loadHabits() {
    try {
        const data = await apiFetch('/api/habits');
        const container = $('#dash-habits');
        if (!container) return;
        if (!data.habits || data.habits.length === 0) {
            container.innerHTML = '<div class="p-20 text-center text-secondary">登録された習慣はありません</div>';
            const wrap = $('#habit-progress-wrap');
            if (wrap) wrap.style.display = 'none';
            return;
        }

        const doneCount = data.habits.filter(h => data.today_done.includes(h.id)).length;
        const total = data.habits.length;
        const progressPct = total > 0 ? Math.round((doneCount / total) * 100) : 0;

        const wrap = $('#habit-progress-wrap');
        if (wrap) {
            wrap.style.display = '';
            const fill = $('#habit-progress-fill');
            if (fill) fill.style.width = `${progressPct}%`;
            const label = $('#habit-progress-label');
            if (label) label.textContent = `${doneCount}/${total}`;
        }

        container.innerHTML = data.habits.map(h => {
            const isDone = data.today_done.includes(h.id);
            const streakText = (data.streaks && data.streaks[h.id]) || '';
            const streakMatch = streakText.match(/(\d+)/);
            const streakNum = streakMatch ? parseInt(streakMatch[1]) : 0;

            let streakBadge = '';
            if (streakNum > 0) {
                const color = streakNum >= 30 ? '#ff6600' : streakNum >= 14 ? '#ff9900' : streakNum >= 7 ? '#ffcc00' : streakNum >= 3 ? 'var(--primary)' : 'var(--text-muted)';
                const icon = streakNum >= 30 ? '🔥' : streakNum >= 7 ? '⚡' : '✨';
                streakBadge = `<span style="font-size:0.72rem; color:${color}; font-weight:700; white-space:nowrap; min-width:30px; text-align:right;">${icon}${streakNum}</span>`;
            }

            return `
                <div class="habit-item ${isDone ? 'done' : ''}" id="habit-item-${h.id}">
                    <button class="habit-check-btn" onclick="completeHabit('${h.name}', '${h.id}')" ${isDone ? 'disabled' : ''}>✔</button>
                    <div class="habit-name" style="flex:1;">${escapeHtml(h.name)}</div>
                    ${streakBadge}
                </div>
            `;
        }).join('');
    } catch (e) { console.error('loadHabits error', e); }
}

window.completeHabit = async (habitName, hId) => {
    try {
        const item = $(`#habit-item-${hId}`);
        if (item) item.classList.add('done');
        showToast(`「${habitName}」を完了しました！🎉`);
        await apiFetch('/api/habits/complete', { method: 'POST', body: JSON.stringify({ habit_name: habitName }) });
        loadHabits();
    } catch { showToast('失敗しました', true); }
};

async function loadSleepTrend() {
    const container = $('#dash-sleep');
    if (!container) return;
    try {
        const data = await apiFetch('/api/sleep_trend');
        if (!data.trend || data.trend.every(d => !d.score)) return;

        const validScores = data.trend.map(d => d.score || 0);
        const maxScore = Math.max(...validScores, 1);

        const barsHtml = data.trend.map(d => {
            const score = d.score || 0;
            const barHeight = score > 0 ? Math.max(4, Math.round((score / 100) * 56)) : 4;
            const color = score >= 80 ? 'var(--primary)' : score >= 60 ? '#ffaa00' : score > 0 ? '#ff5555' : 'rgba(255,255,255,0.1)';
            const durationMin = d.duration || 0;
            const dh = Math.floor(durationMin / 60);
            const dm = durationMin % 60;
            const durationStr = dh > 0 ? `${dh}h${dm}m` : durationMin > 0 ? `${durationMin}m` : '-';
            return `
                <div style="display:flex; flex-direction:column; align-items:center; gap:3px; flex:1; min-width:0;">
                    <div style="font-size:0.62rem; color:var(--text-secondary); font-weight:600;">${score || '-'}</div>
                    <div style="display:flex; flex-direction:column; justify-content:flex-end; height:56px;">
                        <div style="height:${barHeight}px; background:${color}; width:100%; border-radius:3px; transition:height 0.4s; min-width:20px;"></div>
                    </div>
                    <div style="font-size:0.6rem; color:var(--text-muted); white-space:nowrap;">${d.date}</div>
                    <div style="font-size:0.58rem; color:var(--text-muted); white-space:nowrap;">${durationStr}</div>
                </div>
            `;
        }).join('');

        const trendDiv = document.createElement('div');
        trendDiv.style.cssText = 'margin-top:12px; border-top:1px solid var(--border-glass); padding-top:10px;';
        trendDiv.innerHTML = `
            <div style="font-size:0.72rem; color:var(--text-secondary); margin-bottom:8px; font-weight:500;">📈 1週間の推移</div>
            <div style="display:flex; align-items:flex-end; gap:4px; padding:0 2px;">
                ${barsHtml}
            </div>
        `;
        container.appendChild(trendDiv);
    } catch (e) {
        console.error('Sleep trend error', e);
    }
}