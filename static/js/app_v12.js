const API_BASE = '';
let apiKey = localStorage.getItem('secretary_api_key') || '';
let lastMsgDate = null;

// iOS Safari ではキーボード開閉時に dvh が更新されないため、
// visualViewport の高さを --app-height に反映してレイアウトを制御する
(function initViewportHeightFix() {
    function updateAppHeight() {
        const h = window.visualViewport ? window.visualViewport.height : window.innerHeight;
        document.documentElement.style.setProperty('--app-height', h + 'px');
    }
    if (window.visualViewport) {
        window.visualViewport.addEventListener('resize', updateAppHeight);
    }
    window.addEventListener('resize', updateAppHeight);
    updateAppHeight();
})();

// 各カテゴリーごとのソート状態を保持するオブジェクト
let linkSorts = {
    web: 'newest',
    youtube: 'newest',
    recipe: 'newest',
    map: 'newest',
    book: 'newest'
};

// 各カテゴリーごとの目的フィルタ ('' = すべて表示)
let linkPurposeFilters = {
    web: '',
    youtube: '',
    recipe: '',
    map: '',
    book: ''
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

const settingsBtn = $('#settings-btn');
if (settingsBtn) {
    settingsBtn.addEventListener('click', () => openSettingsMenu());
}

window.openSettingsMenu = () => {
    const choice = prompt(
        '設定メニュー（番号を入力）:\n\n1. 通知ステータスを確認\n2. 通知テスト送信\n3. 通知の購読を再登録\n\nキャンセルで閉じる',
        '1'
    );
    if (choice === '1') return checkNotificationStatus();
    if (choice === '2') return testPushNotification();
    if (choice === '3') return resubscribePush();
};

window.resubscribePush = async () => {
    try {
        const reg = 'serviceWorker' in navigator ? await navigator.serviceWorker.getRegistration('/') : null;
        if (reg) {
            const sub = await reg.pushManager.getSubscription();
            if (sub) {
                await sub.unsubscribe();
                try { await apiFetch('/api/push/unsubscribe', { method: 'POST', body: JSON.stringify({ endpoint: sub.endpoint }) }); } catch {}
            }
        }
        const result = await subscribePush();
        showToast(result.ok ? '購読を再登録しました' : ('再登録NG: ' + result.reason), !result.ok);
    } catch (e) {
        showToast('再登録に失敗しました: ' + (e.message || e), true);
    }
};

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
let _pendingReplyToId = null;
let _pendingReplyContent = null;

if (chatForm) {
    chatForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        if (isChatSending) return;
        const msg = messageInput.value.trim();
        if (!msg) return;

        isChatSending = true;
        sendBtn.style.opacity = '0.5';
        sendBtn.disabled = true;

        const replyTo = _pendingReplyToId;
        const replyContent = _pendingReplyContent;
        const userEl = appendMsg('user', msg, null, { replyContent: replyContent });
        clearReplyContext();
        messageInput.value = '';
        messageInput.style.height = '40px';
        sendBtn.classList.remove('active');

        try {
            const data = await apiFetch('/api/chat', {
                method: 'POST',
                body: JSON.stringify({ message: msg, reply_to_id: replyTo }),
            });
            if (userEl && data.user_message_id) userEl.dataset.msgId = String(data.user_message_id);
            appendMsg('assistant', data.reply, null, { id: data.assistant_message_id });

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

function clearReplyContext() {
    _pendingReplyToId = null;
    _pendingReplyContent = null;
    if (messageInput) messageInput.placeholder = 'メッセージを入力...';
}

if (messageInput) {
    messageInput.addEventListener('input', () => {
        messageInput.style.height = '40px';
        messageInput.style.height = Math.min(messageInput.scrollHeight, 120) + 'px';
        sendBtn.classList.toggle('active', messageInput.value.trim() !== '');
    });
}

function appendMsg(role, content, isoTimestamp = null, opts = {}) {
    if (!chatMessages) return null;
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
    div.className = `message ${role}` + (opts.starred ? ' starred' : '');
    if (opts.id) div.dataset.msgId = String(opts.id);
    if (opts.starred) div.dataset.starred = '1';

    let html = '';
    if (role === 'assistant') html += `<img src="/static/icons/avatar.png" class="msg-avatar">`;

    // [ACTION:...] タグを抽出してボタン描画用に分離
    const actions = [];
    const visibleText = String(content || '').replace(/\[ACTION:([^\]]+)\]/g, (_, payload) => {
        actions.push(payload);
        return '';
    }).trim();
    const processedContent = escapeHtml(visibleText).replace(/\n/g, '<br>');

    // 引用 (返信) 表示
    let quoteHtml = '';
    if (opts.replyContent) {
        quoteHtml = `<div class="msg-quote">${escapeHtml(opts.replyContent.slice(0, 120))}</div>`;
    }

    let actionHtml = '';
    if (actions.length > 0 && role === 'assistant') {
        actionHtml = '<div class="msg-actions">' + actions.map((p, i) => {
            const label = describeAction(p);
            const safe = encodeURIComponent(p);
            return `<button class="msg-action-btn" onclick="executeAction('${safe}', this)">${escapeHtml(label)}</button>`;
        }).join('') + '</div>';
    }

    html += `
        <div class="msg-content">
            <div class="msg-bubble" style="word-break: break-all;" data-raw="${escapeHtml(content)}">${quoteHtml}${processedContent}${actionHtml}</div>
            <div class="msg-time">${tStr}</div>
        </div>
    `;
    div.innerHTML = html;
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
    if (role === 'assistant') notifyManager(content);
    return div;
}

function _parseActionPayload(payload) {
    // 例: "calendar_add:summary=会議|start=2026-04-26T14:00:00|end=2026-04-26T15:00:00"
    const colonIdx = payload.indexOf(':');
    const action = colonIdx === -1 ? payload : payload.slice(0, colonIdx);
    const argStr = colonIdx === -1 ? '' : payload.slice(colonIdx + 1);
    const args = {};
    if (argStr) {
        argStr.split('|').forEach(pair => {
            const eqIdx = pair.indexOf('=');
            if (eqIdx === -1) return;
            args[pair.slice(0, eqIdx)] = pair.slice(eqIdx + 1);
        });
    }
    return { action, args };
}

function describeAction(payload) {
    const { action, args } = _parseActionPayload(payload);
    switch (action) {
        case 'calendar_add': return `📅 カレンダーに追加: ${args.summary || ''}`;
        case 'task_add':     return `✅ タスクに追加: ${args.title || ''}`;
        case 'task_delete':  return `🗑 タスクを削除: ${args.keyword || ''}`;
        case 'mit_set':      return `🎯 今日のMITを登録 (${(args.items || '').split(',').filter(Boolean).length}件)`;
        case 'mit_rollover': return `📤 未達MITを翌日へ繰越`;
        default:             return `▶ ${action} を実行`;
    }
}

window.executeAction = async function(encodedPayload, btn) {
    const payload = decodeURIComponent(encodedPayload);
    const { action, args } = _parseActionPayload(payload);
    if (btn) { btn.disabled = true; btn.textContent = '実行中...'; }
    try {
        if (action === 'calendar_add') {
            await apiFetch('/api/calendar_action', {
                method: 'POST',
                body: JSON.stringify({ action: 'add', summary: args.summary, start_time: args.start, end_time: args.end })
            });
            showToast('カレンダーに登録しました');
        } else if (action === 'task_add') {
            await apiFetch('/api/google_tasks_action', {
                method: 'POST',
                body: JSON.stringify({ action: 'add', title: args.title, list_name: args.list_name || null })
            });
            showToast('タスクに追加しました');
        } else if (action === 'task_delete') {
            // keyword ベースの削除はサーバ側が未実装。ガイダンス表示。
            showToast('削除はタスク一覧から行ってください', true);
        } else if (action === 'mit_set') {
            const items = (args.items || '').split(',').map(s => s.trim()).filter(Boolean);
            await apiFetch('/api/mit_set', { method: 'POST', body: JSON.stringify({ items }) });
            showToast('今日のMITを登録しました');
        } else if (action === 'mit_rollover') {
            await apiFetch('/api/mit_rollover', { method: 'POST' });
            showToast('未達MITを翌日に繰り越しました');
        } else {
            showToast('未対応のアクションです', true);
        }
        if (btn) { btn.textContent = '完了 ✓'; btn.classList.add('done'); }
    } catch (e) {
        console.error(e);
        showToast('実行に失敗しました', true);
        if (btn) { btn.disabled = false; btn.textContent = describeAction(payload); }
    }
};

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

        loadHabits();

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

    const activeTasks = (tasks || []).filter(t => !t.completed);
    const doneTasks = (tasks || []).filter(t => t.completed);

    if (!tasks || tasks.length === 0) {
        container.innerHTML = '<div class="loading-placeholder">未完了のタスクはありません</div>';
        return;
    }

    container.innerHTML = [
        ...activeTasks.map(t => {
            const dueLabel = t.due ? _formatDueLabel(t.due) : '';
            const dueAttr = t.due ? t.due.slice(0, 10) : '';
            return `
            <div class="list-item" style="gap:6px;" id="gtask-item-${t.id}">
                <div class="checkbox-custom" onclick="toggleGoogleTask('${t.id}', '${listName}')" style="cursor:pointer;"></div>
                <div class="li-text" style="flex:1;">${escapeHtml(t.title)}${dueLabel ? `<span class="task-due-chip">${escapeHtml(dueLabel)}</span>` : ''}</div>
                <button class="task-due-btn" onclick="openTaskDueEditor('${t.id}', '${listName}', '${escapeHtml(t.title)}', '${dueAttr}')" title="締切を編集">📅</button>
            </div>`;
        }),
        ...doneTasks.map(t => `
            <div class="list-item" style="gap:6px; opacity:0.5;" id="gtask-item-${t.id}">
                <div class="checkbox-custom" style="background:var(--primary); border-color:var(--primary); pointer-events:none; display:flex; align-items:center; justify-content:center; color:#fff; font-size:0.7rem;">✓</div>
                <div class="li-text" style="flex:1; text-decoration:line-through; color:var(--text-muted);">${escapeHtml(t.title)}</div>
            </div>
        `)
    ].join('');
}

function _formatDueLabel(due) {
    try {
        const d = new Date(due);
        if (isNaN(d.getTime())) return '';
        const now = new Date();
        const diff = Math.round((d - now) / (1000 * 60 * 60 * 24));
        if (diff < 0) return `🔴期限切れ`;
        if (diff === 0) return `🟠今日`;
        if (diff === 1) return `🟡明日`;
        return `📅${d.getMonth()+1}/${d.getDate()}`;
    } catch (e) { return ''; }
}

window.openTaskDueEditor = (taskId, listName, title, currentDue) => {
    const newDue = prompt(`「${title}」の締切日を入力 (YYYY-MM-DD、空欄でクリア):`, currentDue || '');
    if (newDue === null) return; // cancel
    const trimmed = newDue.trim();
    if (trimmed && !/^\d{4}-\d{2}-\d{2}$/.test(trimmed)) {
        showToast('YYYY-MM-DD 形式で入力してください', true);
        return;
    }
    apiFetch('/api/google_tasks_action', {
        method: 'POST',
        body: JSON.stringify({ action: 'update', task_id: taskId, list_name: listName, due: trimmed })
    }).then(() => {
        showToast(trimmed ? `締切を ${trimmed} に設定しました` : '締切をクリアしました');
        loadDashboard();
    }).catch(() => showToast('締切の更新に失敗しました', true));
};

window.toggleGoogleTask = async (taskId, listName) => {
    const item = $(`#gtask-item-${taskId}`);
    if (item) {
        item.classList.add('task-syncing');
        item.style.opacity = '0.5';
        const cb = item.querySelector('.checkbox-custom');
        if (cb) {
            cb.style.background = 'var(--primary)';
            cb.style.borderColor = 'var(--primary)';
            cb.style.color = '#fff';
            cb.style.fontSize = '0.7rem';
            cb.textContent = '✓';
            cb.onclick = null;
        }
        const text = item.querySelector('.li-text');
        if (text) text.style.textDecoration = 'line-through';
    }
    try {
        await apiFetch('/api/google_tasks_action', {
            method: 'POST',
            body: JSON.stringify({ action: 'toggle', task_id: taskId, completed: true, list_name: listName })
        });
        showToast('完了にしました！');
        loadDashboard();
    } catch (e) {
        showToast('更新に失敗しました', true);
        loadDashboard();
    } finally {
        if (item) item.classList.remove('task-syncing');
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
    // ソース選択リセット
    const manualRadio = document.querySelector('input[name="breakdown-source"][value="manual"]');
    if (manualRadio) manualRadio.checked = true;
    onBreakdownSourceChange();
    $('#breakdown-modal').classList.remove('hidden');
};

window.onBreakdownSourceChange = async () => {
    const source = document.querySelector('input[name="breakdown-source"]:checked')?.value || 'manual';
    if (source === 'manual') {
        $('#breakdown-manual-section').style.display = '';
        $('#breakdown-existing-section').style.display = 'none';
    } else {
        $('#breakdown-manual-section').style.display = 'none';
        $('#breakdown-existing-section').style.display = '';
        // 既存タスク一覧を取得
        const sel = $('#breakdown-existing-select');
        sel.innerHTML = '<option value="">読み込み中...</option>';
        try {
            const data = await apiFetch('/api/tasks_for_breakdown');
            sel.innerHTML = '';
            if (!data.tasks || data.tasks.length === 0) {
                sel.innerHTML = '<option value="">未完了タスクなし</option>';
                return;
            }
            data.tasks.forEach(t => {
                const opt = document.createElement('option');
                opt.value = JSON.stringify({title: t.title, list_name: t.list_name});
                opt.textContent = `[${t.list_name}] ${t.title}`;
                sel.appendChild(opt);
            });
            // デフォルトで先頭の list_name を追加先にも反映
            try {
                const first = JSON.parse(sel.options[0].value);
                if (first.list_name) {
                    const listSelect = $('#breakdown-list-name');
                    if (listSelect) listSelect.value = first.list_name;
                }
            } catch(e) {}
        } catch (e) {
            sel.innerHTML = '<option value="">取得失敗</option>';
        }
    }
};

let currentBreakdownSubtasks = [];
let currentBreakdownParent = '';
window.generateBreakdown = async () => {
    const source = document.querySelector('input[name="breakdown-source"]:checked')?.value || 'manual';
    let task = '';
    if (source === 'manual') {
        task = $('#breakdown-task-input').value.trim();
    } else {
        const val = $('#breakdown-existing-select').value;
        if (val) {
            try {
                const parsed = JSON.parse(val);
                task = parsed.title;
                // 追加先リストを既存タスクのリストに合わせる
                const listSelect = $('#breakdown-list-name');
                if (listSelect && parsed.list_name) listSelect.value = parsed.list_name;
            } catch(e) { task = val; }
        }
    }
    if (!task) { showToast('タスクを選択/入力してください', true); return; }
    currentBreakdownParent = task;

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
            body: JSON.stringify({
                list_name: listName,
                subtasks: currentBreakdownSubtasks,
                parent_title: currentBreakdownParent || ''
            })
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

// ============================================================
// ゼロ秒思考機能 (Zero Second Thinking)
// ============================================================
let ztState = {
    theme: '',
    sessionId: null,
    timerInterval: null,
    remainingSec: 60,
};

window.openZeroSecModal = async () => {
    ztResetSteps();
    $('#zerosec-modal').classList.remove('hidden');
    // テーマ候補ロード
    const listEl = $('#zt-theme-list');
    listEl.innerHTML = '<div style="padding:20px; text-align:center; color:var(--text-muted);">テーマ生成中...</div>';
    $('#zt-custom-theme').value = '';
    try {
        const data = await apiFetch('/api/zerosec/themes', {
            method: 'POST',
            body: JSON.stringify({ context: '' })
        });
        renderZtThemes(data.themes || []);
    } catch (e) {
        listEl.innerHTML = '<div style="padding:14px; color:var(--text-muted); font-size:0.85rem;">テーマ取得に失敗。自分で入力してね。</div>';
    }
};

function renderZtThemes(themes) {
    const listEl = $('#zt-theme-list');
    if (!themes.length) {
        listEl.innerHTML = '<div style="padding:14px; color:var(--text-muted); font-size:0.85rem;">候補なし。自分で入力してね。</div>';
        return;
    }
    listEl.innerHTML = themes.map((t, i) => `
        <div class="modal-item zt-theme-item" data-theme="${escapeHtml(t)}" onclick="selectZtTheme(this)">
            <span>${escapeHtml(t)}</span>
        </div>
    `).join('');
}

window.selectZtTheme = (el) => {
    document.querySelectorAll('.zt-theme-item').forEach(e => e.classList.remove('selected'));
    el.classList.add('selected');
    $('#zt-custom-theme').value = el.dataset.theme;
};

function ztResetSteps() {
    $('#zt-step-theme').style.display = '';
    $('#zt-step-timer').style.display = 'none';
    $('#zt-step-review').style.display = 'none';
    if (ztState.timerInterval) {
        clearInterval(ztState.timerInterval);
        ztState.timerInterval = null;
    }
}

window.closeZeroSecModal = () => {
    if (ztState.timerInterval) {
        clearInterval(ztState.timerInterval);
        ztState.timerInterval = null;
    }
    $('#zerosec-modal').classList.add('hidden');
    ztState = { theme: '', sessionId: null, timerInterval: null, remainingSec: 60 };
};

window.startZeroSec = async () => {
    const theme = $('#zt-custom-theme').value.trim();
    if (!theme) { showToast('テーマを選ぶか入力してね', true); return; }
    ztState.theme = theme;
    $('#zt-current-theme').textContent = theme;
    $('#zt-memo-input').value = '';
    $('#zt-step-theme').style.display = 'none';
    $('#zt-step-timer').style.display = '';

    // ライフログ開始（バックグラウンド）
    apiFetch('/api/zerosec/log_start', {
        method: 'POST',
        body: JSON.stringify({ context: theme })
    }).catch(() => {});

    // 1分タイマー
    ztState.remainingSec = 60;
    updateZtTimer();
    ztState.timerInterval = setInterval(() => {
        ztState.remainingSec -= 1;
        updateZtTimer();
        if (ztState.remainingSec <= 0) {
            clearInterval(ztState.timerInterval);
            ztState.timerInterval = null;
            // タイマー終了後にレビュー画面へ自動遷移
            ztGoToReview();
        }
    }, 1000);
};

function updateZtTimer() {
    const m = Math.floor(ztState.remainingSec / 60).toString().padStart(2, '0');
    const s = (ztState.remainingSec % 60).toString().padStart(2, '0');
    const display = $('#zt-timer');
    if (display) {
        display.textContent = `${m}:${s}`;
        if (ztState.remainingSec <= 10) display.classList.add('countdown-warning');
        else display.classList.remove('countdown-warning');
    }
}

function ztGoToReview() {
    $('#zt-review-theme').value = ztState.theme;
    $('#zt-review-memo').value = $('#zt-memo-input').value;
    $('#zt-step-timer').style.display = 'none';
    $('#zt-step-review').style.display = '';
}

window.finishZeroSec = () => {
    if (ztState.timerInterval) {
        clearInterval(ztState.timerInterval);
        ztState.timerInterval = null;
    }
    ztGoToReview();
};

window.abortZeroSec = () => {
    if (!confirm('中断する？メモは破棄されるよ')) return;
    closeZeroSecModal();
};

window.ztAttachImage = () => {
    $('#zt-image-input').click();
};

const ztImageInput = document.getElementById('zt-image-input');
if (ztImageInput) {
    ztImageInput.addEventListener('change', async (e) => {
        const files = Array.from(e.target.files || []);
        if (!files.length) return;
        showToast('手書きを読み取り中...');
        try {
            const images = await Promise.all(files.map(f => fileToBase64(f)));
            const data = await apiFetch('/api/note_from_images', {
                method: 'POST',
                body: JSON.stringify({
                    images: images.map(img => ({ image_base64: img.base64, mime_type: img.mime })),
                    hint: `ゼロ秒思考: ${ztState.theme}`
                })
            });
            const ocrText = data.structured_content || data.transcription || '';
            const cur = $('#zt-review-memo').value;
            $('#zt-review-memo').value = cur ? `${cur}\n\n${ocrText}` : ocrText;
            showToast('取り込み完了');
        } catch (err) {
            console.error(err);
            showToast('画像の読み取りに失敗', true);
        }
        e.target.value = '';
    });
}

async function ztSaveCommon() {
    const theme = $('#zt-review-theme').value.trim() || ztState.theme;
    const memo = $('#zt-review-memo').value.trim();
    if (!memo) { showToast('メモが空です', true); return null; }
    const payload = { theme, memo };
    if (ztState.sessionId) payload.session_id = ztState.sessionId;
    try {
        const data = await apiFetch('/api/zerosec/save', {
            method: 'POST',
            body: JSON.stringify(payload)
        });
        if (data.session_id) ztState.sessionId = data.session_id;
        showToast(data.message || '保存しました');
        return { theme, memo };
    } catch (e) {
        console.error(e);
        showToast('保存に失敗', true);
        return null;
    }
}

window.ztSaveAndClose = async () => {
    const res = await ztSaveCommon();
    if (res) closeZeroSecModal();
};

window.ztSaveAndDeepDive = async () => {
    const res = await ztSaveCommon();
    if (!res) return;
    // 深掘り用テーマを取得
    const listEl = $('#zt-theme-list');
    listEl.innerHTML = '<div style="padding:20px; text-align:center; color:var(--text-muted);">深掘りテーマを生成中...</div>';
    // Step1（テーマ選択）画面に戻る
    ztResetSteps();
    $('#zt-custom-theme').value = '';
    try {
        const data = await apiFetch('/api/zerosec/deep_dive', {
            method: 'POST',
            body: JSON.stringify({
                original_theme: res.theme,
                user_memo: res.memo
            })
        });
        renderZtThemes(data.themes || []);
    } catch (e) {
        listEl.innerHTML = '<div style="padding:14px; color:var(--text-muted); font-size:0.85rem;">取得失敗。自分で入力してね。</div>';
    }
};

// ファイル -> base64 変換ヘルパー
function fileToBase64(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => {
            const result = reader.result || '';
            const base64 = result.toString().split(',')[1] || '';
            resolve({ base64, mime: file.type || 'image/jpeg' });
        };
        reader.onerror = reject;
        reader.readAsDataURL(file);
    });
}

// ============================================================
// 読書機能 (Reading)
// ============================================================
let readingState = {
    bookTitle: '',
    startedAt: null,
    timerInterval: null,
    promptInterval: null,
    sentPrompts: [],
    enablePrompt: false,
};

window.openReadingModal = async () => {
    readingResetSteps();
    $('#reading-modal').classList.remove('hidden');
    $('#reading-custom-title').value = '';
    $('#reading-prompt-toggle').checked = false;
    const listEl = $('#reading-book-list');
    listEl.innerHTML = '<div style="padding:20px; text-align:center; color:var(--text-muted);">候補を読み込み中...</div>';
    try {
        const data = await apiFetch('/api/reading/books');
        renderBookCandidates(data.books || []);
    } catch (e) {
        listEl.innerHTML = '<div style="padding:14px; color:var(--text-muted); font-size:0.85rem;">候補取得に失敗。手動入力してね。</div>';
    }
};

function renderBookCandidates(books) {
    const listEl = $('#reading-book-list');
    if (!books.length) {
        listEl.innerHTML = '<div style="padding:14px; color:var(--text-muted); font-size:0.85rem;">候補なし。手動入力してね。</div>';
        return;
    }
    listEl.innerHTML = books.map(b => {
        const badge = b.source === 'stock'
            ? '<span style="font-size:0.7rem; color:var(--accent); margin-left:6px;">📚 ストック</span>'
            : '<span style="font-size:0.7rem; color:var(--text-muted); margin-left:6px;">📓 履歴</span>';
        return `
            <div class="modal-item book-cand-item" data-title="${escapeHtml(b.title)}" onclick="selectBookCand(this)">
                <span>${escapeHtml(b.title)}${badge}</span>
            </div>
        `;
    }).join('');
}

window.selectBookCand = (el) => {
    document.querySelectorAll('.book-cand-item').forEach(e => e.classList.remove('selected'));
    el.classList.add('selected');
    $('#reading-custom-title').value = el.dataset.title;
};

function readingResetSteps() {
    $('#reading-step-select').style.display = '';
    $('#reading-step-active').style.display = 'none';
    if (readingState.timerInterval) clearInterval(readingState.timerInterval);
    if (readingState.promptInterval) clearInterval(readingState.promptInterval);
    readingState.timerInterval = null;
    readingState.promptInterval = null;
}

window.closeReadingModal = () => {
    if (readingState.timerInterval) clearInterval(readingState.timerInterval);
    if (readingState.promptInterval) clearInterval(readingState.promptInterval);
    $('#reading-modal').classList.add('hidden');
    readingState = {
        bookTitle: '', startedAt: null,
        timerInterval: null, promptInterval: null,
        sentPrompts: [], enablePrompt: false,
    };
};

window.startReading = async () => {
    const title = $('#reading-custom-title').value.trim();
    if (!title) { showToast('書籍を選ぶか入力してね', true); return; }
    readingState.bookTitle = title;
    readingState.startedAt = Date.now();
    readingState.sentPrompts = [];
    readingState.enablePrompt = $('#reading-prompt-toggle').checked;
    $('#reading-current-book').textContent = title;
    $('#reading-memo-input').value = '';
    $('#reading-prompt-area').style.display = 'none';
    $('#reading-prompt-area').textContent = '';
    $('#reading-step-select').style.display = 'none';
    $('#reading-step-active').style.display = '';

    // ライフログ start
    sendActionCommandSilent(`「${title}」を読み始める`);
    apiFetch('/api/zerosec/log_start', {
        method: 'POST',
        body: JSON.stringify({ context: `読書: ${title}` })
    }).catch(() => {});

    // 経過時間タイマー
    updateReadingTimer();
    readingState.timerInterval = setInterval(updateReadingTimer, 1000);

    // マネージャーからの問いかけ（5分間隔）
    if (readingState.enablePrompt) {
        readingState.promptInterval = setInterval(fetchReadingPrompt, 5 * 60 * 1000);
    }
};

function updateReadingTimer() {
    if (!readingState.startedAt) return;
    const elapsed = Math.floor((Date.now() - readingState.startedAt) / 1000);
    const h = Math.floor(elapsed / 3600);
    const m = Math.floor((elapsed % 3600) / 60).toString().padStart(2, '0');
    const s = (elapsed % 60).toString().padStart(2, '0');
    const t = h > 0 ? `${h}:${m}:${s}` : `${m}:${s}`;
    const el = $('#reading-timer');
    if (el) el.textContent = t;
}

async function fetchReadingPrompt() {
    try {
        const data = await apiFetch('/api/reading/prompt', {
            method: 'POST',
            body: JSON.stringify({
                book_title: readingState.bookTitle,
                previous_prompts: readingState.sentPrompts
            })
        });
        const text = (data.prompt || '').trim();
        if (text) {
            readingState.sentPrompts.push(text);
            const area = $('#reading-prompt-area');
            area.textContent = `💬 ${text}`;
            area.style.display = '';
        }
    } catch (e) {
        console.debug('reading prompt fetch failed', e);
    }
}

window.readingAttachImage = () => {
    $('#reading-image-input').click();
};

const readingImageInput = document.getElementById('reading-image-input');
if (readingImageInput) {
    readingImageInput.addEventListener('change', async (e) => {
        const files = Array.from(e.target.files || []);
        if (!files.length) return;
        showToast('手書きを読み取り中...');
        try {
            const images = await Promise.all(files.map(f => fileToBase64(f)));
            const data = await apiFetch('/api/note_from_images', {
                method: 'POST',
                body: JSON.stringify({
                    images: images.map(img => ({ image_base64: img.base64, mime_type: img.mime })),
                    hint: `読書メモ: ${readingState.bookTitle}`
                })
            });
            const ocrText = data.structured_content || data.transcription || '';
            const cur = $('#reading-memo-input').value;
            $('#reading-memo-input').value = cur ? `${cur}\n\n${ocrText}` : ocrText;
            showToast('取り込み完了');
        } catch (err) {
            console.error(err);
            showToast('画像の読み取りに失敗', true);
        }
        e.target.value = '';
    });
}

window.abortReading = () => {
    if (!confirm('中断する？メモは破棄されるよ')) return;
    closeReadingModal();
};

window.finishReading = async () => {
    const memo = $('#reading-memo-input').value.trim();
    const title = readingState.bookTitle;
    const elapsedMin = readingState.startedAt
        ? Math.round((Date.now() - readingState.startedAt) / 60000)
        : 0;

    if (memo) {
        try {
            const memoWithTime = elapsedMin > 0
                ? `${memo}\n\n（読書時間: ${elapsedMin}分）`
                : memo;
            const data = await apiFetch('/api/reading/save', {
                method: 'POST',
                body: JSON.stringify({ book_title: title, memo: memoWithTime })
            });
            showToast(data.message || '保存しました');
        } catch (e) {
            showToast('保存に失敗', true);
            return;
        }
    } else {
        showToast(`お疲れさま！${elapsedMin}分の読書を記録したよ`);
    }
    // ライフログ end
    sendActionCommandSilent(`「${title}」読書終了（${elapsedMin}分）`);
    closeReadingModal();
};

// ============================================================
// 勉強機能 (Study)
// ============================================================
let studyState = {
    subject: '',
    startedAt: null,
    timerInterval: null,
};

window.openStudyModal = async () => {
    studyResetSteps();
    $('#study-modal').classList.remove('hidden');
    $('#study-custom-subject').value = '';
    const listEl = $('#study-subject-list');
    listEl.innerHTML = '<div style="padding:20px; text-align:center; color:var(--text-muted);">候補を読み込み中...</div>';
    try {
        const data = await apiFetch('/api/study/subjects');
        renderStudySubjects(data.subjects || []);
    } catch (e) {
        listEl.innerHTML = '<div style="padding:14px; color:var(--text-muted); font-size:0.85rem;">候補取得に失敗。手動入力してね。</div>';
    }
};

function renderStudySubjects(subjects) {
    const listEl = $('#study-subject-list');
    if (!subjects.length) {
        listEl.innerHTML = '<div style="padding:14px; color:var(--text-muted); font-size:0.85rem;">過去の科目なし。新しく入力してね。</div>';
        return;
    }
    listEl.innerHTML = subjects.map(s => `
        <div class="modal-item study-subj-item" data-subject="${escapeHtml(s)}" onclick="selectStudySubject(this)">
            <span>${escapeHtml(s)}</span>
        </div>
    `).join('');
}

window.selectStudySubject = (el) => {
    document.querySelectorAll('.study-subj-item').forEach(e => e.classList.remove('selected'));
    el.classList.add('selected');
    $('#study-custom-subject').value = el.dataset.subject;
};

function studyResetSteps() {
    $('#study-step-select').style.display = '';
    $('#study-step-active').style.display = 'none';
    if (studyState.timerInterval) clearInterval(studyState.timerInterval);
    studyState.timerInterval = null;
}

window.closeStudyModal = () => {
    if (studyState.timerInterval) clearInterval(studyState.timerInterval);
    $('#study-modal').classList.add('hidden');
    studyState = { subject: '', startedAt: null, timerInterval: null };
};

window.startStudy = async () => {
    const subject = $('#study-custom-subject').value.trim();
    if (!subject) { showToast('科目を選ぶか入力してね', true); return; }
    studyState.subject = subject;
    studyState.startedAt = Date.now();
    $('#study-current-subject').textContent = subject;
    $('#study-memo-input').value = '';
    $('#study-step-select').style.display = 'none';
    $('#study-step-active').style.display = '';

    sendActionCommandSilent(`「${subject}」の勉強を始める`);
    updateStudyTimer();
    studyState.timerInterval = setInterval(updateStudyTimer, 1000);
};

function updateStudyTimer() {
    if (!studyState.startedAt) return;
    const elapsed = Math.floor((Date.now() - studyState.startedAt) / 1000);
    const h = Math.floor(elapsed / 3600);
    const m = Math.floor((elapsed % 3600) / 60).toString().padStart(2, '0');
    const s = (elapsed % 60).toString().padStart(2, '0');
    const t = h > 0 ? `${h}:${m}:${s}` : `${m}:${s}`;
    const el = $('#study-timer');
    if (el) el.textContent = t;
}

window.studyAttachImage = () => {
    $('#study-image-input').click();
};

const studyImageInput = document.getElementById('study-image-input');
if (studyImageInput) {
    studyImageInput.addEventListener('change', async (e) => {
        const files = Array.from(e.target.files || []);
        if (!files.length) return;
        showToast('手書きを読み取り中...');
        try {
            const images = await Promise.all(files.map(f => fileToBase64(f)));
            const data = await apiFetch('/api/note_from_images', {
                method: 'POST',
                body: JSON.stringify({
                    images: images.map(img => ({ image_base64: img.base64, mime_type: img.mime })),
                    hint: `学習メモ: ${studyState.subject}`
                })
            });
            const ocrText = data.structured_content || data.transcription || '';
            const cur = $('#study-memo-input').value;
            $('#study-memo-input').value = cur ? `${cur}\n\n${ocrText}` : ocrText;
            showToast('取り込み完了');
        } catch (err) {
            console.error(err);
            showToast('画像の読み取りに失敗', true);
        }
        e.target.value = '';
    });
}

window.abortStudy = () => {
    if (!confirm('中断する？メモは破棄されるよ')) return;
    closeStudyModal();
};

window.finishStudy = async () => {
    const memo = $('#study-memo-input').value.trim();
    const subject = studyState.subject;
    const elapsedMin = studyState.startedAt
        ? Math.round((Date.now() - studyState.startedAt) / 60000)
        : 0;

    if (memo) {
        try {
            const memoWithTime = elapsedMin > 0
                ? `${memo}\n\n（学習時間: ${elapsedMin}分）`
                : memo;
            const data = await apiFetch('/api/study/save', {
                method: 'POST',
                body: JSON.stringify({ subject, memo: memoWithTime })
            });
            showToast(data.message || '保存しました');
        } catch (e) {
            showToast('保存に失敗', true);
            return;
        }
    } else {
        showToast(`お疲れさま！${elapsedMin}分の学習を記録したよ`);
    }
    sendActionCommandSilent(`「${subject}」の勉強を終了（${elapsedMin}分）`);
    closeStudyModal();
};

// 既存の sendActionCommand のサイレント版（チャット欄に表示せずバックグラウンドで送信）
async function sendActionCommandSilent(text) {
    try {
        await apiFetch('/api/chat', {
            method: 'POST',
            body: JSON.stringify({ message: text })
        });
    } catch (e) {
        console.debug('silent action failed:', e);
    }
}

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

window.changeLinkPurposeFilter = (type, val) => {
    linkPurposeFilters[type] = val;
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

        // 各タイプ別にフィルタ前のリンク一覧（プルダウン候補生成用）
        const allByType = {
            web: links.filter(l => l.type === 'web'),
            youtube: links.filter(l => l.type === 'youtube'),
            recipe: links.filter(l => l.type === 'recipe'),
            map: links.filter(l => l.type === 'map'),
            book: links.filter(l => l.type === 'book'),
        };

        const applyPurpose = (arr, type) => {
            const f = linkPurposeFilters[type];
            if (!f) return arr;
            if (f === '__none__') return arr.filter(l => !l.purpose || !l.purpose.trim());
            return arr.filter(l => (l.purpose || '').trim() === f);
        };

        const webLinks = applyPurpose(allByType.web, 'web').sort(getSortFn('web'));
        const ytLinks = applyPurpose(allByType.youtube, 'youtube').sort(getSortFn('youtube'));
        const recipeLinks = applyPurpose(allByType.recipe, 'recipe').sort(getSortFn('recipe'));
        const mapLinks = applyPurpose(allByType.map, 'map').sort(getSortFn('map'));
        const bookLinks = applyPurpose(allByType.book, 'book').sort(getSortFn('book'));

        const buildPurposeOptions = (type) => {
            const arr = allByType[type] || [];
            const counts = new Map();
            let noneCount = 0;
            for (const l of arr) {
                const p = (l.purpose || '').trim();
                if (!p) { noneCount++; continue; }
                counts.set(p, (counts.get(p) || 0) + 1);
            }
            const sorted = [...counts.entries()].sort((a, b) => b[1] - a[1]);
            const cur = linkPurposeFilters[type];
            const opts = [`<option value="" ${cur===''?'selected':''}>🎯 すべての目的 (${arr.length})</option>`];
            for (const [purpose, cnt] of sorted) {
                const safe = escapeHtml(purpose);
                opts.push(`<option value="${safe}" ${cur===purpose?'selected':''}>${safe} (${cnt})</option>`);
            }
            if (noneCount > 0) {
                opts.push(`<option value="__none__" ${cur==='__none__'?'selected':''}>(目的なし) (${noneCount})</option>`);
            }
            return opts.join('');
        };

        const setupHeader = (id, type) => {
            const container = $(`#${id}`);
            if(!container) return;
            const details = container.closest('details');
            if (!details) return;

            // 旧バージョンが summary 内に残していたコントロールを除去
            details.querySelectorAll('summary .stocked-list-controls, summary .header-controls').forEach(n => n.remove());

            let ctrl = details.querySelector(':scope > .stocked-list-controls');
            if (!ctrl) {
                ctrl = document.createElement('div');
                ctrl.className = 'stocked-list-controls';

                const purposeSelect = document.createElement('select');
                purposeSelect.dataset.role = 'purpose-filter';
                purposeSelect.title = '目的で絞り込み';
                purposeSelect.onchange = (e) => changeLinkPurposeFilter(type, e.target.value);

                const sortSelect = document.createElement('select');
                sortSelect.dataset.role = 'sort-select';
                sortSelect.title = '並び順';
                sortSelect.innerHTML = `
                    <option value="newest" ${linkSorts[type]==='newest'?'selected':''}>新しい順</option>
                    <option value="oldest" ${linkSorts[type]==='oldest'?'selected':''}>古い順</option>
                    <option value="title" ${linkSorts[type]==='title'?'selected':''}>タイトル順</option>
                `;
                sortSelect.onchange = (e) => changeLinkSort(type, e.target.value);

                const addBtn = document.createElement('button');
                addBtn.className = 'add-mini';
                addBtn.textContent = '＋ 追加';
                addBtn.title = '手動で追加';
                addBtn.onclick = () => openManualAddModal(type);

                ctrl.appendChild(purposeSelect);
                ctrl.appendChild(sortSelect);
                ctrl.appendChild(addBtn);

                // details 本体内、リスト直前に挿入
                details.insertBefore(ctrl, container);
            }

            const purposeSelect = ctrl.querySelector('[data-role="purpose-filter"]');
            if (purposeSelect) purposeSelect.innerHTML = buildPurposeOptions(type);

            const sortSelect = ctrl.querySelector('[data-role="sort-select"]');
            if (sortSelect) sortSelect.value = linkSorts[type];
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
            container.classList.add('stocked-list');
            if (items.length === 0) {
                container.innerHTML = '<div class="stocked-empty">登録なし</div>';
                return;
            }
            container.innerHTML = items.map(lk => {
                const dateStr = new Date(lk.added_at).toLocaleString('ja-JP', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
                const titleText = (lk.title && lk.title !== 'Untitled') ? lk.title : (lk.url || '(無題)');
                const titleEl = lk.url
                    ? `<a class="stocked-link-title" href="${lk.url}" target="_blank" rel="noopener">${escapeHtml(titleText)}</a>`
                    : `<span class="stocked-link-title">${escapeHtml(titleText)}</span>`;

                const chips = [];
                if (lk.purpose) chips.push(`<span class="stocked-link-chip purpose">🎯 ${escapeHtml(lk.purpose)}</span>`);
                if (lk.target_date) chips.push(`<span class="stocked-link-chip date">📅 ${escapeHtml(lk.target_date)}</span>`);
                chips.push(`<span class="stocked-link-chip added">${dateStr}</span>`);

                const lkJson = JSON.stringify(lk).replace(/'/g, "&#39;");
                return `
                    <div class="stocked-link" id="stocked-link-${lk.id}">
                        ${titleEl}
                        <div class="stocked-link-meta">${chips.join('')}</div>
                        <div class="stocked-link-actions">
                            <button class="stocked-link-btn edit" onclick='openLinkDetailsModal(${lkJson})'>編集</button>
                            <button class="stocked-link-btn danger" onclick="deleteStockedLink(${lk.id})">削除</button>
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

    // フィールド可視性は LINK_FIELD_VISIBILITY config に集約
    if (typeof applyLinkFieldVisibility === 'function') {
        applyLinkFieldVisibility(lk.type);
    }
    // type 変更時にも追従
    const typeSel = $('#link-type-select');
    if (typeSel) {
        typeSel.addEventListener('change', () => {
            if (typeof applyLinkFieldVisibility === 'function') {
                applyLinkFieldVisibility(typeSel.value);
            }
        });
    }

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

async function registerServiceWorker() {
    if (!('serviceWorker' in navigator)) return null;
    try {
        // ルートスコープ ('/' 以下すべて) を Push の対象にするため、
        // バックエンドが Service-Worker-Allowed: / を付けて配信している /sw.js を登録する。
        const reg = await navigator.serviceWorker.register('/sw.js', { scope: '/' });
        // 即時に最新版を有効化
        if (reg.waiting) reg.waiting.postMessage({ type: 'SKIP_WAITING' });
        return reg;
    } catch (e) {
        console.error('SW register error:', e);
        return null;
    }
}

async function requestNotificationPermission() {
    if (!('Notification' in window)) return;
    // Service Worker を必ず先に登録する（registration が無いと PushManager.subscribe できない）
    await registerServiceWorker();

    if (Notification.permission === 'default') {
        const perm = await Notification.requestPermission();
        if (perm === 'granted') subscribePush();
    } else if (Notification.permission === 'granted') {
        subscribePush();
    }
}

function _urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - base64String.length % 4) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(base64);
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; ++i) out[i] = raw.charCodeAt(i);
    return out;
}

function _arrayBufferToBase64(buf) {
    const bytes = new Uint8Array(buf);
    let bin = '';
    bytes.forEach(b => bin += String.fromCharCode(b));
    return btoa(bin);
}

async function subscribePush() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
        return { ok: false, reason: 'PushManager 未対応のブラウザです' };
    }
    if (!apiKey) return { ok: false, reason: 'APIキー未設定' };
    try {
        await registerServiceWorker();
        const reg = await navigator.serviceWorker.ready;
        const vapidRes = await fetch(`${API_BASE}/api/vapid_public_key`).then(r => r.json());
        if (!vapidRes.configured || !vapidRes.key) {
            const reason = 'サーバーのVAPID鍵が未設定です（管理者に確認）';
            console.warn('Push:', reason);
            return { ok: false, reason };
        }

        const expectedKey = _urlBase64ToUint8Array(vapidRes.key);
        let sub = await reg.pushManager.getSubscription();
        // 既存購読があってもサーバーのVAPID鍵と一致しない場合は破棄してから再購読
        if (sub) {
            const existingKey = sub.options && sub.options.applicationServerKey;
            const sameKey = existingKey && new Uint8Array(existingKey).every((b, i) => b === expectedKey[i]);
            if (!sameKey) {
                try { await sub.unsubscribe(); } catch {}
                try { await apiFetch('/api/push/unsubscribe', { method: 'POST', body: JSON.stringify({ endpoint: sub.endpoint }) }); } catch {}
                sub = null;
            }
        }
        if (!sub) {
            sub = await reg.pushManager.subscribe({
                userVisibleOnly: true,
                applicationServerKey: expectedKey,
            });
        }
        const json = sub.toJSON();
        const p256dh = json.keys && json.keys.p256dh;
        const auth = json.keys && json.keys.auth;
        if (!p256dh || !auth) return { ok: false, reason: 'pushSubscriptionのキー取得失敗' };

        await apiFetch('/api/push/subscribe', {
            method: 'POST',
            body: JSON.stringify({ endpoint: sub.endpoint, p256dh, auth })
        });
        console.info('Web Push 購読完了');
        return { ok: true, endpoint: sub.endpoint };
    } catch (e) {
        console.error('Push subscribe error:', e);
        return { ok: false, reason: e.message || String(e) };
    }
}

window.testPushNotification = async () => {
    try {
        const data = await apiFetch('/api/push/test', { method: 'POST' });
        if (data.delivered > 0) {
            showToast(`通知テスト送信: ${data.delivered}件配信`);
        } else {
            showToast('購読がサーバーに登録されていません。下の「通知ステータスを確認」を実行してください', true);
        }
    } catch (e) {
        showToast('通知テストに失敗しました', true);
    }
};

window.checkNotificationStatus = async () => {
    const lines = [];
    lines.push(`Notification.permission: ${Notification.permission}`);

    let swReg = null;
    if ('serviceWorker' in navigator) {
        swReg = await navigator.serviceWorker.getRegistration('/');
        lines.push(`Service Worker: ${swReg ? '登録済み (' + (swReg.active ? 'active' : 'pending') + ')' : '未登録'}`);
    } else {
        lines.push('Service Worker: 非対応');
    }

    if (swReg && 'PushManager' in window) {
        const sub = await swReg.pushManager.getSubscription();
        lines.push(`Push購読: ${sub ? 'あり' : 'なし'}`);
    }

    try {
        const vapidRes = await fetch(`${API_BASE}/api/vapid_public_key`).then(r => r.json());
        lines.push(`サーバーVAPID設定: ${vapidRes.configured ? 'OK' : '未設定（要修正）'}`);
    } catch {
        lines.push('サーバーVAPID取得失敗');
    }

    if (Notification.permission !== 'granted') {
        const perm = await Notification.requestPermission();
        lines.push(`許可リクエスト結果: ${perm}`);
    }

    const result = await subscribePush();
    lines.push(`購読登録: ${result.ok ? 'OK' : 'NG - ' + result.reason}`);

    alert(lines.join('\n'));
};

function notifyManager(content) {
    if (!('Notification' in window)) return;
    if (Notification.permission !== 'granted') return;
    if (!document.hidden) return;
    const preview = content.replace(/<br>/g, ' ').replace(/&[a-z]+;/g, '').slice(0, 80);
    new Notification('マネージャーからメッセージ', {
        body: preview || 'メッセージが届きました',
        icon: '/static/icons/avatar.png',
    });
}

function initMain() {
    loadHistory();
    loadDashboard();
    requestNotificationPermission();

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
            // 返信先の本文を引きやすいよう辞書化
            const idMap = new Map();
            (data.messages || []).forEach(m => idMap.set(m.id, m));
            (data.messages || []).forEach(m => {
                const replyContent = m.reply_to ? (idMap.get(m.reply_to)?.content || null) : null;
                appendMsg(m.role, m.content, m.timestamp, {
                    id: m.id,
                    starred: !!m.starred,
                    replyContent,
                });
            });
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

        const heatmapSection = $('#habit-heatmap-section');
        if (heatmapSection) heatmapSection.style.display = '';

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

            const trigger = (h.trigger || '').trim();
            const triggerChip = trigger
                ? `<span class="habit-trigger-chip" title="クリックで変更" onclick="event.stopPropagation(); openHabitTriggerModal('${escapeHtml(h.name)}', '${escapeHtml(trigger)}')">⏰ ${escapeHtml(trigger)}</span>`
                : `<button class="habit-trigger-add" title="いつやるかを設定" onclick="event.stopPropagation(); openHabitTriggerModal('${escapeHtml(h.name)}', '')">＋いつ</button>`;

            return `
                <div class="habit-item ${isDone ? 'done' : ''}" id="habit-item-${h.id}">
                    <button class="habit-check-btn" onclick="completeHabit('${h.name}', '${h.id}')" ${isDone ? 'disabled' : ''}>✔</button>
                    <div class="habit-name-wrap" style="flex:1; display:flex; flex-direction:column; gap:2px; min-width:0;">
                        <div class="habit-name">${escapeHtml(h.name)}</div>
                        <div class="habit-trigger-row">${triggerChip}</div>
                    </div>
                    ${streakBadge}
                </div>
            `;
        }).join('');

        // 全完了チェック（初期ロード時は発火しない）
        const isInitialLoad = (window._prevHabitDoneCount === undefined);
        if (isInitialLoad) {
            window._prevHabitDoneCount = (doneCount === total) ? total : -1;
        } else if (doneCount > 0 && doneCount === total && window._prevHabitDoneCount !== total) {
            triggerCelebration();
        }
        window._prevHabitDoneCount = doneCount;
    } catch (e) { console.error('loadHabits error', e); }
}

window.completeHabit = async (habitName, hId) => {
    try {
        const item = $(`#habit-item-${hId}`);
        if (item) item.classList.add('done');
        showToast(`「${habitName}」を完了しました！🎉`);
        const result = await apiFetch('/api/habits/complete', { method: 'POST', body: JSON.stringify({ habit_name: habitName }) });
        checkMilestone(result.message || '');
        loadHabits();
    } catch { showToast('失敗しました', true); }
};

window.openHabitTriggerModal = (habitName, currentTrigger) => {
    const msg = `「${habitName}」をいつ行いますか？\n\n例:\n・朝食後\n・歯磨きの前\n・7:30\n・帰宅後すぐ\n\n（空欄で保存すると解除されます）`;
    const next = prompt(msg, currentTrigger || '');
    if (next === null) return;
    const trimmed = next.trim();
    apiFetch('/api/habits/trigger', {
        method: 'POST',
        body: JSON.stringify({ habit_name: habitName, trigger: trimmed })
    }).then(() => {
        showToast(trimmed ? `「${habitName}」のタイミングを設定しました` : `「${habitName}」のタイミングを解除しました`);
        loadHabits();
    }).catch(() => showToast('保存に失敗しました', true));
};

let _sleepTrendLoading = false;
async function loadSleepTrend() {
    if (_sleepTrendLoading) return;
    const container = $('#dash-sleep');
    if (!container) return;
    _sleepTrendLoading = true;
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
    } finally {
        _sleepTrendLoading = false;
    }
}

// ========== MILESTONE MODAL ==========
const MILESTONE_DEFS = [
    { days:100, icon:'👑', msg:'100日連続達成！', sub:'伝説的な継続力。誇りに思ってください' },
    { days: 60, icon:'🔥', msg:'60日連続達成！',  sub:'2ヶ月継続。これは本物の意志力です' },
    { days: 30, icon:'🔥', msg:'30日連続達成！',  sub:'1ヶ月！もう習慣はあなたの一部です' },
    { days: 14, icon:'⚡', msg:'14日連続達成！',  sub:'2週間継続！習慣が身についてきた証拠' },
    { days:  7, icon:'⚡', msg:'7日連続達成！',   sub:'1週間続けた！最高のスタートです' },
];
function checkMilestone(message) {
    const match = message.match(/現在\s*(\d+)\s*日連続達成中/);
    if (!match) return;
    const streak = parseInt(match[1]);
    const milestone = MILESTONE_DEFS.find(m => m.days === streak);
    if (milestone) showMilestoneModal(milestone);
}
function showMilestoneModal(m) {
    const modal = $('#milestone-modal');
    if (!modal) return;
    $('#milestone-icon').textContent = m.icon;
    $('#milestone-title').textContent = m.msg;
    $('#milestone-sub').textContent = m.sub;
    modal.classList.remove('hidden');
    setTimeout(() => modal.classList.add('hidden'), 3000);
}
window.closeMilestoneModal = () => $('#milestone-modal')?.classList.add('hidden');

// ========== HABIT HEATMAP ==========
let _heatmapOpen = false;
function toggleHeatmap() {
    const heatmapDiv = $('#habit-heatmap');
    const chevron = $('#heatmap-chevron');
    if (!heatmapDiv) return;
    _heatmapOpen = !_heatmapOpen;
    heatmapDiv.style.display = _heatmapOpen ? 'block' : 'none';
    if (chevron) chevron.textContent = _heatmapOpen ? '▼' : '▶';
    if (_heatmapOpen) loadHeatmap();
}
async function loadHeatmap() {
    const grid = $('#heatmap-grid');
    if (!grid) return;
    try {
        const data = await apiFetch('/api/habits/history?days=28');
        grid.innerHTML = data.history.map(d => {
            const bg = d.rate === 0 ? 'rgba(255,255,255,0.04)'
                : d.rate < 0.5 ? 'rgba(0,186,152,0.25)'
                : d.rate < 1.0 ? 'rgba(0,186,152,0.6)'
                : 'var(--accent)';
            return `<div title="${d.date}: ${d.done}/${d.total}" style="aspect-ratio:1;border-radius:3px;background:${bg};cursor:default;"></div>`;
        }).join('');
    } catch(e) { console.error('loadHeatmap error', e); }
}

// ========== CELEBRATION CONFETTI ==========
function triggerCelebration() {
    const modal = $('#celebration-modal');
    if (modal) {
        modal.classList.remove('hidden');
        setTimeout(() => modal.classList.add('hidden'), 3000);
    }
    startConfetti();
    setTimeout(stopConfetti, 3500);
}
window.closeCelebrationModal = () => {
    $('#celebration-modal')?.classList.add('hidden');
    stopConfetti();
};
function startConfetti() {
    const canvas = $('#confetti-canvas');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    canvas.width = window.innerWidth;
    canvas.height = window.innerHeight;
    const colors = ['#00ba98','#00d4ff','#ffcc00','#ff6600','#ff4ecd'];
    const particles = Array.from({length:80}, () => ({
        x: Math.random()*canvas.width,
        y: Math.random()*canvas.height - canvas.height,
        r: Math.random()*6+3,
        d: Math.random()*80+20,
        color: colors[Math.floor(Math.random()*colors.length)],
        tilt: 0,
        tiltAngle: 0,
        tiltAngleIncremental: Math.random()*0.07+0.05
    }));
    let angle = 0;
    function draw() {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        angle += 0.01;
        particles.forEach(p => {
            p.tiltAngle += p.tiltAngleIncremental;
            p.y += (Math.cos(angle + p.d) + 2 + p.r / 2) * 1.2;
            p.x += Math.sin(angle);
            p.tilt = Math.sin(p.tiltAngle) * 12;
            ctx.beginPath();
            ctx.lineWidth = p.r;
            ctx.strokeStyle = p.color;
            ctx.moveTo(p.x + p.tilt + p.r / 3, p.y);
            ctx.lineTo(p.x + p.tilt, p.y + p.tilt + p.r / 5);
            ctx.stroke();
        });
        canvas._confettiAnimId = requestAnimationFrame(draw);
    }
    draw();
}
// ===== メッセージ長押し削除 =====

let _longPressTarget = null;

function attachLongPress(el) {
    const DURATION = 600;
    let timer = null;

    const onLongPress = () => {
        _longPressTarget = el;
        if (navigator.vibrate) navigator.vibrate(40);
        document.getElementById('msg-delete-confirm')?.classList.remove('hidden');
    };

    const start = () => {
        clearTimeout(timer);
        timer = setTimeout(onLongPress, DURATION);
    };

    const cancel = () => {
        clearTimeout(timer);
        timer = null;
    };

    el.addEventListener('touchstart', start, { passive: true });
    el.addEventListener('touchend', cancel);
    el.addEventListener('touchmove', cancel, { passive: true });
    // touchcancel は無視 — iOSでは touchcancel 後もタイマーを発火させる必要がある

    el.addEventListener('mousedown', start);
    el.addEventListener('mouseup', cancel);
    el.addEventListener('mouseleave', cancel);
    el.addEventListener('contextmenu', e => e.preventDefault());
}

function closeMsgDeleteConfirm() {
    document.getElementById('msg-delete-confirm')?.classList.add('hidden');
    _longPressTarget = null;
}

async function confirmMsgDelete() {
    const target = _longPressTarget;
    if (!target) { closeMsgDeleteConfirm(); return; }
    const id = target.dataset.msgId;
    try {
        if (id) await apiFetch(`/api/messages/${id}`, { method: 'DELETE' });
        target.remove();
    } catch (e) {
        showToast('削除に失敗しました', true);
    }
    closeMsgDeleteConfirm();
}

// ===== 手書きメモ読み取り =====

let pendingNote = null;
let notesListCache = null;

const imgAttachBtn = $('#img-attach-btn');
const imageInput = $('#image-input');

if (imgAttachBtn) {
    imgAttachBtn.addEventListener('click', () => imageInput && imageInput.click());
}

function _fileToBase64(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result.split(',')[1]);
        reader.onerror = reject;
        reader.readAsDataURL(file);
    });
}

if (imageInput) {
    imageInput.addEventListener('change', async () => {
        const files = Array.from(imageInput.files || []);
        if (!files.length) return;
        imageInput.value = '';

        // チャットに画像プレビューを表示 (全枚分)
        files.forEach(f => appendImagePreview(URL.createObjectURL(f)));

        const isMulti = files.length > 1;
        const loadingId = appendLoadingBubble(
            isMulti
                ? `📖 ${files.length}枚の手書きメモを読み取り中...`
                : '📖 手書きメモを読み取り中...'
        );

        try {
            let result;
            if (isMulti) {
                const images = await Promise.all(files.map(async (f) => ({
                    image_base64: await _fileToBase64(f),
                    mime_type: f.type || 'image/jpeg',
                })));
                result = await apiFetch('/api/note_from_images', {
                    method: 'POST',
                    body: JSON.stringify({ images }),
                });
            } else {
                const file = files[0];
                const base64 = await _fileToBase64(file);
                result = await apiFetch('/api/note_from_image', {
                    method: 'POST',
                    body: JSON.stringify({ image_base64: base64, mime_type: file.type || 'image/jpeg' }),
                });
            }
            removeLoadingBubble(loadingId);
            pendingNote = result;
            appendNoteCard(result);
        } catch (err) {
            removeLoadingBubble(loadingId);
            appendMsg('assistant', '読み取りに失敗しました。もう一度お試しください。');
        }
    });
}

function appendImagePreview(src) {
    if (!chatMessages) return;
    const div = document.createElement('div');
    div.className = 'message user';
    div.innerHTML = `
        <div class="msg-content">
            <div class="msg-bubble" style="padding:6px; background:transparent; border:1px solid var(--border-glass);">
                <img src="${src}" style="max-width:200px; max-height:200px; border-radius:10px; display:block;" onload="URL.revokeObjectURL(this.src)">
            </div>
        </div>`;
    attachLongPress(div);
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

function appendLoadingBubble(text) {
    if (!chatMessages) return null;
    const id = 'loading-' + Date.now();
    const div = document.createElement('div');
    div.className = 'message assistant';
    div.id = id;
    div.innerHTML = `
        <img src="/static/icons/avatar.png" class="msg-avatar">
        <div class="msg-content">
            <div class="msg-bubble loading-bubble">${escapeHtml(text)}</div>
        </div>`;
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
    return id;
}

function removeLoadingBubble(id) {
    if (!id) return;
    const el = document.getElementById(id);
    if (el) el.remove();
}

const CATEGORY_LABEL = { work: '💼 仕事', study: '📚 勉強', idea: '💡 アイデア', task: '📋 タスク', other: '📝 その他' };

function appendNoteCard(note) {
    if (!chatMessages) return;
    const now = new Date();
    const tStr = now.getHours().toString().padStart(2, '0') + ':' + now.getMinutes().toString().padStart(2, '0');
    const catLabel = CATEGORY_LABEL[note.category] || '📝';

    const actionsHtml = note.action_items && note.action_items.length > 0
        ? `<div class="note-card-actions-list">
             <div style="font-size:0.75rem; color:var(--text-secondary); margin-bottom:4px;">📌 抽出されたタスク</div>
             ${note.action_items.map(a => `<div class="note-action-chip">${escapeHtml(a)}</div>`).join('')}
           </div>`
        : '';

    const structuredHtml = escapeHtml(note.structured_content || note.transcription || '').replace(/\n/g, '<br>');

    const div = document.createElement('div');
    div.className = 'message assistant';
    div.innerHTML = `
        <img src="/static/icons/avatar.png" class="msg-avatar">
        <div class="msg-content">
            <div class="note-card">
                <div class="note-card-header">
                    <span class="note-cat-badge">${catLabel}</span>
                    <span style="font-size:0.7rem; color:var(--text-muted);">${tStr}</span>
                </div>
                <div class="note-card-body">${structuredHtml}</div>
                ${actionsHtml}
                <div class="note-card-footer">
                    <button class="note-quick-btn" onclick="quickSaveNote()">⚡ 即保存</button>
                    <button class="note-save-btn" onclick="openNoteSaveModal()">詳細設定 →</button>
                </div>
            </div>
            <div class="msg-time">${tStr}</div>
        </div>`;
    attachLongPress(div);
    chatMessages.appendChild(div);
    chatMessages.scrollTop = chatMessages.scrollHeight;
}

async function openNoteSaveModal() {
    if (!pendingNote) return;

    // 内容と初期値をセット
    const contentInput = $('#note-content-input');
    const titleInput = $('#note-title-input');
    const categorySelect = $('#note-category-select');
    const subjectInput = $('#note-subject-input');

    if (contentInput) contentInput.value = pendingNote.structured_content || pendingNote.transcription || '';
    if (titleInput) titleInput.value = pendingNote.subject || '';
    if (categorySelect) {
        categorySelect.value = pendingNote.category || 'other';
        toggleSubjectField();
    }
    if (subjectInput) subjectInput.value = pendingNote.subject || '';

    // アクションアイテム表示
    const actionsWrap = $('#note-actions-wrap');
    const actionItemsEl = $('#note-action-items');
    if (actionsWrap && actionItemsEl && pendingNote.action_items && pendingNote.action_items.length > 0) {
        actionItemsEl.innerHTML = pendingNote.action_items.map((a, i) =>
            `<label style="display:flex; align-items:center; gap:8px; font-size:0.85rem;">
               <input type="checkbox" data-action-idx="${i}" checked style="accent-color:var(--accent);">
               <span>${escapeHtml(a)}</span>
             </label>`
        ).join('');
        actionsWrap.style.display = 'block';
    } else if (actionsWrap) {
        actionsWrap.style.display = 'none';
    }

    // ラジオの初期化
    const radioNew = document.querySelector('input[name="save-mode"][value="new"]');
    if (radioNew) { radioNew.checked = true; toggleSaveMode('new'); }

    // ノート一覧を取得（キャッシュあれば再利用）
    await loadNotesList();

    $('#note-save-modal')?.classList.remove('hidden');
}

function closeNoteSaveModal() {
    $('#note-save-modal')?.classList.add('hidden');
}

function toggleSaveMode(mode) {
    const newSec = $('#save-new-section');
    const appendSec = $('#save-append-section');
    if (mode === 'new') {
        if (newSec) newSec.style.display = 'flex';
        if (appendSec) appendSec.style.display = 'none';
    } else {
        if (newSec) newSec.style.display = 'none';
        if (appendSec) appendSec.style.display = 'flex';
    }
}

function toggleSubjectField() {
    const cat = $('#note-category-select')?.value;
    const wrap = $('#note-subject-wrap');
    if (wrap) wrap.style.display = cat === 'study' ? 'flex' : 'none';
}

// ラジオボタンのイベント登録
document.querySelectorAll('input[name="save-mode"]').forEach(radio => {
    radio.addEventListener('change', () => toggleSaveMode(radio.value));
});
$('#note-category-select')?.addEventListener('change', toggleSubjectField);

async function loadNotesList() {
    if (notesListCache) {
        renderNotesList(notesListCache);
        return;
    }
    try {
        const data = await apiFetch('/api/notes/list');
        notesListCache = data.notes || [];
        renderNotesList(notesListCache);
    } catch (e) {
        const sel = $('#note-target-select');
        if (sel) sel.innerHTML = '<option value="">取得に失敗しました</option>';
    }
}

function renderNotesList(notes) {
    const sel = $('#note-target-select');
    if (!sel) return;
    sel.innerHTML = notes.map(n =>
        `<option value="${n.id}" data-folder="${n.folder}" data-filename="${n.filename}">${escapeHtml(n.name)}</option>`
    ).join('');
}

async function executeNoteSave() {
    if (!pendingNote) return;

    const mode = document.querySelector('input[name="save-mode"]:checked')?.value || 'new';
    const content = $('#note-content-input')?.value?.trim() || '';
    if (!content) { showToast('内容を入力してください', true); return; }

    // チェックされたアクションアイテムのみ収集
    const actionItems = [];
    document.querySelectorAll('#note-action-items input[type="checkbox"]:checked').forEach(cb => {
        const idx = parseInt(cb.dataset.actionIdx);
        if (pendingNote.action_items && pendingNote.action_items[idx]) {
            actionItems.push(pendingNote.action_items[idx]);
        }
    });

    const payload = { mode, content, action_items: actionItems };

    if (mode === 'new') {
        payload.title = $('#note-title-input')?.value?.trim() || pendingNote.subject || 'メモ';
        payload.category = $('#note-category-select')?.value || 'other';
        payload.subject = $('#note-subject-input')?.value?.trim() || '';
    } else {
        const sel = $('#note-target-select');
        const selected = sel?.options[sel.selectedIndex];
        if (!selected || !selected.value) { showToast('追記先を選択してください', true); return; }
        payload.target_id = selected.value;
        payload.target_folder = selected.dataset.folder || '';
        payload.target_filename = selected.dataset.filename || '';
    }

    const saveBtn = document.querySelector('#note-save-modal .modal-btn.submit');
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = '保存中...'; }

    try {
        await apiFetch('/api/save_note', { method: 'POST', body: JSON.stringify(payload) });
        closeNoteSaveModal();
        showToast('ノートを保存しました ✓');
        notesListCache = null; // 次回再取得
        pendingNote = null;
    } catch (e) {
        showToast('保存に失敗しました', true);
    } finally {
        if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = '保存'; }
    }
}

function stopConfetti() {
    const canvas = $('#confetti-canvas');
    if (!canvas) return;
    cancelAnimationFrame(canvas._confettiAnimId);
    canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);
}

// =====================================================
// v3.1 機能拡充: アクションシート / 検索 / Quick Save / リンクフィールド可視性
// =====================================================

// ----- 長押しイベント委譲 (chat-messages 全体に1つだけ) -----
let _longPressInitialized = false;
function initLongPressDelegation() {
    if (_longPressInitialized) return;
    if (!chatMessages) return;
    _longPressInitialized = true;
    const DURATION = 600;
    let timer = null;
    let target = null;
    const start = (e) => {
        const msg = e.target.closest && e.target.closest('.message');
        if (!msg) return;
        target = msg;
        msg.classList.add('long-press-active');
        clearTimeout(timer);
        timer = setTimeout(() => {
            if (navigator.vibrate) navigator.vibrate(40);
            _longPressTarget = target;
            target?.classList.remove('long-press-active');
            openMsgActionSheet();
        }, DURATION);
    };
    const cancel = () => {
        clearTimeout(timer);
        timer = null;
        target?.classList.remove('long-press-active');
        target = null;
    };
    chatMessages.addEventListener('touchstart', start, { passive: true });
    chatMessages.addEventListener('touchend', cancel);
    chatMessages.addEventListener('touchmove', cancel, { passive: true });
    chatMessages.addEventListener('touchcancel', cancel);
    chatMessages.addEventListener('mousedown', start);
    chatMessages.addEventListener('mouseup', cancel);
    chatMessages.addEventListener('mouseleave', cancel);
    chatMessages.addEventListener('contextmenu', (e) => {
        if (e.target.closest('.message')) e.preventDefault();
    });
}

// 既存の `attachLongPress(div)` 呼び出しは互換性のためノーオプ化。
// 長押し検知は initLongPressDelegation() による委譲に一本化された。
function attachLongPress(_el) { /* no-op: handled by delegation */ }

// ----- アクションシート -----
function _getLongPressText() {
    if (!_longPressTarget) return '';
    const bubble = _longPressTarget.querySelector('.msg-bubble');
    if (!bubble) return '';
    return bubble.dataset.raw || bubble.innerText;
}

function openMsgActionSheet() {
    const sheet = document.getElementById('msg-action-sheet');
    const starBtn = document.getElementById('msg-action-star-btn');
    if (starBtn && _longPressTarget) {
        const isStarred = _longPressTarget.classList.contains('starred');
        starBtn.textContent = isStarred ? '⭐ お気に入り解除' : '⭐ お気に入り';
        starBtn.classList.toggle('is-active', isStarred);
    }
    sheet?.classList.remove('hidden');
}

function closeMsgActionSheet(e) {
    if (e && e.target.closest && e.target.closest('.action-sheet')) return;
    document.getElementById('msg-action-sheet')?.classList.add('hidden');
    _longPressTarget = null;
}

function msgActionCopy() {
    const text = _getLongPressText();
    if (text && navigator.clipboard) {
        navigator.clipboard.writeText(text)
            .then(() => showToast('コピーしました'))
            .catch(() => showToast('コピーに失敗しました', true));
    }
    closeMsgActionSheet();
}

function msgActionReply() {
    const target = _longPressTarget;
    if (!target) return;
    const text = _getLongPressText();
    const idStr = target.dataset.msgId;
    _pendingReplyToId = idStr ? parseInt(idStr) : null;
    _pendingReplyContent = text;
    if (messageInput) {
        messageInput.placeholder = `↩ 返信中: ${text.slice(0, 28)}${text.length > 28 ? '...' : ''}`;
        messageInput.focus();
    }
    showToast('返信モード ON (送信時に解除)');
    closeMsgActionSheet();
}

async function msgActionStar() {
    const target = _longPressTarget;
    if (!target) { closeMsgActionSheet(); return; }
    const id = target.dataset.msgId;
    if (!id) {
        showToast('未保存のメッセージはお気に入りにできません', true);
        closeMsgActionSheet();
        return;
    }
    try {
        const res = await apiFetch(`/api/messages/${id}/star`, { method: 'POST' });
        target.classList.toggle('starred', !!res.starred);
        showToast(res.starred ? '⭐ お気に入りに追加' : 'お気に入り解除');
    } catch (e) {
        showToast('操作に失敗しました', true);
    }
    closeMsgActionSheet();
}

async function msgActionDelete() {
    const target = _longPressTarget;
    if (!target) { closeMsgActionSheet(); return; }
    const id = target.dataset.msgId;
    try {
        if (id) await apiFetch(`/api/messages/${id}`, { method: 'DELETE' });
        target.remove();
        showToast('削除しました');
    } catch (e) {
        showToast('削除に失敗しました', true);
    }
    closeMsgActionSheet();
}

// ----- アクションチップス展開トグル -----
function toggleMoreChips() {
    const more = document.getElementById('action-chips-more');
    const toggle = document.getElementById('chip-more-toggle');
    if (!more) return;
    const willOpen = more.classList.contains('hidden');
    more.classList.toggle('hidden', !willOpen);
    if (toggle) toggle.textContent = willOpen ? '× 閉じる' : '＋ その他';
}

// ----- ノート Quick Save -----
async function quickSaveNote() {
    if (!pendingNote) return;
    const cat = pendingNote.category || 'other';
    const dateStr = new Date().toISOString().slice(0, 10);
    const payload = {
        mode: 'new',
        content: pendingNote.structured_content || pendingNote.transcription || '',
        action_items: pendingNote.action_items || [],
        title: pendingNote.subject || `メモ_${dateStr}`,
        category: cat,
        subject: pendingNote.subject || '',
    };
    try {
        await apiFetch('/api/save_note', { method: 'POST', body: JSON.stringify(payload) });
        const label = (CATEGORY_LABEL[cat] || '📝');
        showToast(`保存しました ✓ ${label}`);
        pendingNote = null;
    } catch (e) {
        showToast('保存に失敗しました', true);
    }
}

// ----- 検索オーバーレイ -----
function openSearchOverlay() {
    document.getElementById('search-overlay')?.classList.remove('hidden');
    setTimeout(() => $('#search-input')?.focus(), 60);
}

function closeSearchOverlay(e) {
    if (e && e.target.closest && e.target.closest('.search-panel')) return;
    document.getElementById('search-overlay')?.classList.add('hidden');
    const input = $('#search-input');
    if (input) input.value = '';
    const results = $('#search-results');
    if (results) results.innerHTML = '<p class="search-hint">2文字以上で検索開始</p>';
}

let _searchDebounce = null;
$('#search-input')?.addEventListener('input', (e) => {
    clearTimeout(_searchDebounce);
    const q = e.target.value.trim();
    _searchDebounce = setTimeout(() => runMessageSearch(q), 220);
});

async function runMessageSearch(q) {
    const results = $('#search-results');
    if (!results) return;
    if (q.length < 2) {
        results.innerHTML = '<p class="search-hint">2文字以上で検索開始</p>';
        return;
    }
    try {
        const data = await apiFetch(`/api/messages/search?q=${encodeURIComponent(q)}&limit=50`);
        if (!data.results || !data.results.length) {
            results.innerHTML = '<p class="search-empty">該当するメッセージが見つかりません</p>';
            return;
        }
        const safeQ = q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const re = new RegExp(safeQ, 'gi');
        results.innerHTML = data.results.map(m => {
            const dt = new Date(m.timestamp);
            const tStr = `${dt.getMonth()+1}/${dt.getDate()} ${String(dt.getHours()).padStart(2,'0')}:${String(dt.getMinutes()).padStart(2,'0')}`;
            const safeContent = escapeHtml(m.content);
            const highlighted = safeContent.replace(re, m0 => `<mark class="search-hl">${m0}</mark>`);
            const roleLabel = m.role === 'assistant' ? 'AI' : 'YOU';
            return `<div class="search-result-item" onclick="jumpToMessage(${m.id})">
                <div class="search-result-role">${roleLabel}</div>
                <div class="search-result-snippet">${highlighted}</div>
                <div class="search-result-time">${tStr}</div>
            </div>`;
        }).join('');
    } catch (e) {
        results.innerHTML = '<p class="search-empty">検索に失敗しました</p>';
    }
}

function jumpToMessage(id) {
    closeSearchOverlay();
    const target = chatMessages?.querySelector(`.message[data-msg-id="${id}"]`);
    if (target) {
        target.scrollIntoView({ behavior: 'smooth', block: 'center' });
        target.classList.add('long-press-active');
        setTimeout(() => target.classList.remove('long-press-active'), 1200);
    } else {
        showToast('表示中の履歴に該当メッセージがありません');
    }
}

// ----- リンク詳細フィールド可視性 (Phase 2-6) -----
const LINK_FIELD_VISIBILITY = {
    web:    { purpose: true,  date: false, url: true,  summary: true,  memo: true },
    youtube:{ purpose: true,  date: false, url: false, summary: true,  memo: true },
    recipe: { purpose: false, date: false, url: false, summary: false, memo: true },
    map:    { purpose: true,  date: true,  url: false, summary: false, memo: true },
    book:   { purpose: true,  date: false, url: true,  summary: true,  memo: true },
};

function applyLinkFieldVisibility(linkType) {
    const cfg = LINK_FIELD_VISIBILITY[linkType] || LINK_FIELD_VISIBILITY.web;
    Object.entries(cfg).forEach(([k, v]) => {
        const el = document.getElementById(`field-${k}`);
        if (el) el.style.display = v ? 'flex' : 'none';
    });
}

// URL 欄のリアルタイムバリデーション
$('#link-note-url-input')?.addEventListener('input', (e) => {
    const ok = !e.target.value || /^https?:\/\//.test(e.target.value);
    e.target.classList.toggle('input-invalid', !ok);
});

// ----- ストックリンク一括既読化 (Phase 3-4) -----
async function markAllLinksRead(type) {
    if (!confirm(`${type} カテゴリのリンクをすべて既読にしますか？`)) return;
    try {
        const data = await apiFetch('/api/links');
        const targetIds = (data.links || [])
            .filter(l => l.type === type && l.status !== 'saved')
            .map(l => l.id);
        if (!targetIds.length) {
            showToast('未読のリンクはありません');
            return;
        }
        await apiFetch('/api/links/bulk_status', {
            method: 'POST',
            body: JSON.stringify({ link_ids: targetIds, status: 'saved' }),
        });
        showToast(`${targetIds.length} 件を既読化しました`);
        loadDashboard();
    } catch (e) {
        showToast('一括既読化に失敗しました', true);
    }
}

// ----- 初期化フック -----
// initMain() はモジュール冒頭で定義済み。長押し委譲を初期化するための拡張ラッパー。
const _origInitMain = typeof initMain === 'function' ? initMain : null;
window.initMain = function patchedInitMain() {
    if (_origInitMain) _origInitMain();
    initLongPressDelegation();
};