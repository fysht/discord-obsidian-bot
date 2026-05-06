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
    const fetchOpts = { ...options, headers };
    if (options.signal) fetchOpts.signal = options.signal;
    const res = await fetch(`${API_BASE}${path}`, fetchOpts);
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

    const titles = { chat: 'チャット', info: '情報', log: 'ライフログ', schedule: '予定', invest: '投資' };
    const titleEl = $('#current-tab-title');
    if (titleEl) titleEl.textContent = titles[tab] || 'Manager AI';

    if (tab !== 'chat' && tab !== 'invest') loadDashboard();
    // ログタブを開いたときに Fitbit データとデイリーサマリーを自動ロード
    if (tab === 'log') {
        if (!_fitbitRows.length) loadFitbitAllData(false);
        loadDailySummary();
    }
    if (tab === 'invest') {
        loadInvestmentHistory();
    }
}

const chatMessages = $('#chat-messages');
const messageInput = $('#message-input');
const sendBtn = $('#send-btn');
let isChatSending = false;
const chatForm = $('#chat-form');
let _pendingReplyToId = null;
let _pendingReplyContent = null;
let _isEnglishMode = false;

// ENモードのチェックボックスを監視してフラグと視覚フィードバックを更新
const _engCheckbox = $('#english-mode-checkbox');
if (_engCheckbox) {
    _engCheckbox.addEventListener('change', () => {
        _isEnglishMode = _engCheckbox.checked;
        const toggle = _engCheckbox.closest('.eng-mode-toggle');
        if (toggle) toggle.style.opacity = _isEnglishMode ? '1' : '0.5';
    });
}

// iOS Safari fix: 送信ボタンをタップするとtextareaがblurしてキーボードが閉じ、
// その後のsubmitがキャンセルされる問題を防ぐため pointerdown でblurを抑止する
if (sendBtn) {
    sendBtn.addEventListener('pointerdown', (e) => { e.preventDefault(); }, { passive: false });
}

let _chatAbortCtrl = null;
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

        _chatAbortCtrl = new AbortController();
        try {
            const data = await apiFetch('/api/chat', {
                method: 'POST',
                body: JSON.stringify({ message: msg, reply_to_id: replyTo, english_mode: _isEnglishMode }),
                signal: _chatAbortCtrl.signal,
            });
            if (userEl && data.user_message_id) userEl.dataset.msgId = String(data.user_message_id);

            // ENモード: 翻訳テキストを表示
            if (_isEnglishMode && data.translation) {
                const transEl = document.createElement('div');
                transEl.className = 'msg-translation-hint';
                transEl.textContent = `🔤 ${data.translation}`;
                transEl.style.cssText = 'font-size:0.78rem;color:var(--text-muted);padding:2px 12px 6px;font-style:italic;';
                if (userEl) userEl.appendChild(transEl);
            }

            appendMsg('assistant', data.reply, null, { id: data.assistant_message_id, showTts: _isEnglishMode });

            // AI応答後にダッシュボードをリロードして反映させる
            if (typeof loadDashboard === 'function') loadDashboard();
        } catch (err) {
            if (err.name !== 'AbortError') {
                appendMsg('assistant', 'すみません、エラーが発生しました。');
            }
        } finally {
            _chatAbortCtrl = null;
            isChatSending = false;
            sendBtn.style.opacity = '1';
            sendBtn.disabled = false;
        }
    });
}

// 画面が閉じられた / バックグラウンドに移った場合は送信中の chat リクエストを中断し、
// 二重送信や宙吊り状態を防ぐ。
window.addEventListener('pagehide', () => {
    if (_chatAbortCtrl) _chatAbortCtrl.abort();
});
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden' && _chatAbortCtrl) {
        _chatAbortCtrl.abort();
    }
});

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
            return `<div class="msg-action-row">`
                + `<button class="msg-action-btn" onclick="executeAction('${safe}', this)">${escapeHtml(label)}</button>`
                + `<button class="msg-action-cancel" onclick="cancelAction(this)" title="キャンセル" aria-label="キャンセル">✕</button>`
                + `</div>`;
        }).join('') + '</div>';
    }

    // ENモード時、assistant メッセージに🔊ボタンを追加
    let ttsHtml = '';
    if (role === 'assistant' && opts.showTts) {
        const safeText = escapeHtml(visibleText).replace(/'/g, "&#39;");
        ttsHtml = `<button onclick="speakText('${safeText}')" style="background:none;border:none;cursor:pointer;font-size:0.9rem;opacity:0.6;padding:2px 4px;margin-left:4px;" title="読み上げ">🔊</button>`;
    }

    html += `
        <div class="msg-content">
            <div class="msg-bubble" data-raw="${escapeHtml(content)}">${quoteHtml}${processedContent}${actionHtml}</div>
            <div class="msg-time">${tStr}${ttsHtml}</div>
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
        case 'note_create':  return `📝 ノートに保存: ${args.title || ''}`;
        default:             return `▶ ${action} を実行`;
    }
}

window.cancelAction = function(btn) {
    const row = btn && btn.closest && btn.closest('.msg-action-row');
    if (!row) return;
    row.style.transition = 'opacity 0.15s ease, transform 0.15s ease';
    row.style.opacity = '0';
    row.style.transform = 'translateX(8px)';
    setTimeout(() => {
        const wrap = row.parentElement;
        row.remove();
        if (wrap && wrap.classList.contains('msg-actions') && wrap.children.length === 0) {
            wrap.remove();
        }
    }, 150);
};

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
        } else if (action === 'note_create') {
            // メッセージ本文を pendingNote にセットして保存モーダルを開く
            const msgEl = btn ? btn.closest('.message') : null;
            const bubble = msgEl ? msgEl.querySelector('.msg-bubble') : null;
            const raw = bubble ? (bubble.dataset.raw || bubble.innerText || '') : '';
            const cleanContent = String(raw).replace(/\[ACTION:[^\]]+\]/g, '').trim();
            pendingNote = {
                structured_content: cleanContent,
                transcription: cleanContent,
                subject: args.title || '',
                category: args.category || 'other',
                action_items: [],
            };
            await openNoteSaveModal();
            // モーダルを開いたらアクションボタン側は閉じる扱い（実行扱い）
            if (btn) { btn.textContent = '保存モーダルを開いた ✓'; btn.classList.add('done'); }
            return;
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

function renderWeather(w, weatherEl) {
    if (!weatherEl) return;
    let html = `<div class="weather-summary">${escapeHtml(w.summary || '')}</div>`;
    if (w.max_temp || w.min_temp) {
        html += `<div class="weather-temps">
            <span class="temp-max">↑${w.max_temp}℃</span>
            <span class="temp-min">↓${w.min_temp}℃</span>
        </div>`;
    }
    // 3日間サマリー行
    if (w.daily && w.daily.length > 0) {
        html += `<div style="display:flex; gap:6px; margin:10px 0 4px; overflow-x:auto; padding-bottom:4px;">`;
        w.daily.forEach(d => {
            html += `
                <div style="flex:1; min-width:60px; display:flex; flex-direction:column; align-items:center; gap:3px; background:rgba(255,255,255,0.05); border-radius:8px; padding:8px 4px; font-size:0.75rem;">
                    <div style="font-weight:700; color:var(--text-secondary);">${escapeHtml(d.day)}</div>
                    <div style="font-size:1.4rem;">${d.icon || ''}</div>
                    <div style="font-size:0.7rem; color:var(--text-secondary);">${escapeHtml(d.weather || '')}</div>
                    <div style="display:flex; gap:4px;">
                        <span style="color:#ff6b6b;">↑${d.max_temp !== undefined ? d.max_temp : '--'}</span>
                        <span style="color:#74c0fc;">↓${d.min_temp !== undefined ? d.min_temp : '--'}</span>
                    </div>
                    ${d.pop ? `<div style="color:var(--text-muted);">☂${escapeHtml(d.pop)}</div>` : ''}
                </div>
            `;
        });
        html += `</div>`;
    }
    // 時間別
    const slots = w.hourly || w.slots || [];
    if (slots.length > 0) {
        html += `<div class="weather-slots">`;
        let lastDay = '';
        slots.forEach(s => {
            if (s.day && s.day !== lastDay) {
                html += `<div class="weather-day-label">${escapeHtml(s.day)}</div>`;
                lastDay = s.day;
            }
            html += `
                <div class="weather-slot">
                    <div class="ws-time">${escapeHtml(s.time || '')}</div>
                    <div class="ws-icon">${s.icon || ''}</div>
                    <div class="ws-weather">${escapeHtml(s.weather || '')}</div>
                    <div class="ws-pop">${escapeHtml(s.pop || '')}</div>
                    <div class="ws-temp">${escapeHtml(String(s.temp ?? ''))}℃</div>
                </div>
            `;
        });
        html += `</div>`;
    }
    // Yahoo!天気へのリンク（location が "33/6710" 形式の場合のみ）
    const loc = w.location || localStorage.getItem('mng_weather_location') || '33/6610';
    if (/^\d+\/\d+$/.test(loc)) {
        // 末尾を `.html` 形式にする（ディレクトリ形式は404になるため）
        const yahooUrl = `https://weather.yahoo.co.jp/weather/jp/${encodeURI(loc)}.html`;
        html += `<div style="margin-top:8px;text-align:right;">
            <a href="${yahooUrl}" target="_blank" rel="noopener"
               style="font-size:0.78rem;color:var(--text-muted);text-decoration:none;">
               Yahoo!天気で詳細を見る ↗
            </a>
        </div>`;
    }
    weatherEl.innerHTML = html;
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
                renderWeather(data.weather, weatherEl);
                // ロケーション名を天気カードタイトルに反映
                if (data.weather.location_name) {
                    const titleEl = $('#weather-card-title');
                    if (titleEl) titleEl.textContent = `天気 (${data.weather.location_name})`;
                }
            } else {
                weatherEl.innerHTML = `<div class="loading-placeholder">気象データを取得できませんでした</div>`;
            }
            // カスタムロケーションが設定されていれば上書き取得
            // 旧コード（札幌など岡山以外）が残っていた場合は岡山南部に上書き
            let customLoc = localStorage.getItem('mng_weather_location');
            if (customLoc && !customLoc.startsWith('33/')) {
                customLoc = '33/6610';
                localStorage.setItem('mng_weather_location', customLoc);
            }
            // 旧地域コードからの自動マイグレーション
            if (customLoc === '33/6710') {
                customLoc = '33/6610';
                localStorage.setItem('mng_weather_location', customLoc);
            } else if (customLoc === '33/6720') {
                customLoc = '33/6620';
                localStorage.setItem('mng_weather_location', customLoc);
            }
            if (customLoc) {
                apiFetch(`/api/weather?location=${encodeURIComponent(customLoc)}`).then(wd => {
                    if (wd && wd.summary !== '取得失敗') {
                        renderWeather(wd, weatherEl);
                        if (wd.location_name) {
                            const titleEl = $('#weather-card-title');
                            if (titleEl) titleEl.textContent = `天気 (${wd.location_name})`;
                        }
                    }
                }).catch(() => {});
            }
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
            obTaskEl.innerHTML = data.tasks.length ? data.tasks.map((t, idx) => {
                const isLog = t.is_log || false;
                const isRunning = isLog && t.text.includes('▶');
                const rawAttr = t.text.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
                // 時刻 / マーク / 本文 に構造化して列幅を固定
                const m = t.text.match(/^\s*(\d{1,2}:\d{2}|\?\?:\?\?)(?:\s*[-–~〜]\s*(\d{1,2}:\d{2}))?\s*([▶■])?\s*(.*)$/);
                let inner;
                if (m) {
                    const startStr = m[1] === '??:??' ? '' : m[1];
                    const endStr = m[2] || '';
                    const mark = m[3] || '';
                    const body = m[4] || '';
                    const sep = (startStr || endStr) ? '–' : '';
                    inner = `
                        <div style="display:grid; grid-template-columns:38px 12px 38px; align-items:center; font-family:ui-monospace, 'SF Mono', Consolas, monospace; font-size:0.78rem; color:var(--text-muted);">
                            <span style="text-align:right;">${escapeHtml(startStr)}</span>
                            <span style="text-align:center;">${sep}</span>
                            <span style="text-align:left;">${escapeHtml(endStr)}</span>
                        </div>
                        <span class="lifelog-mark">${escapeHtml(mark)}</span>
                        <span class="lifelog-body">${escapeHtml(body)}</span>
                    `;
                } else {
                    inner = `<span class="lifelog-body" style="grid-column: 1 / -1;">${escapeHtml(t.text)}</span>`;
                }
                return `
                    <div class="list-item lifelog-row" style="border-left: 3px solid rgba(255,255,255,0.1); cursor: pointer; ${t.done ? 'text-decoration:line-through; opacity:0.5;' : ''}"
                         onclick="editLifeLog(${idx}, '${rawAttr}')">
                        ${inner}
                    </div>
                `;
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

        // 今日の日記
        const journalEl = $('#dash-daily-journal');
        if (journalEl) {
            if (data.daily_journal) {
                // 軽量Markdownレンダリング
                const jLines = data.daily_journal.split('\n');
                journalEl.innerHTML = jLines.map(line => {
                    const trimmed = line.trim();
                    if (trimmed.startsWith('- ')) {
                        return `<div style="padding:2px 0 2px 12px; border-left:2px solid rgba(0,186,152,0.3); margin:2px 0; font-size:0.88rem;">${escapeHtml(trimmed.slice(2))}</div>`;
                    }
                    return escapeHtml(line) + '<br>';
                }).join('');
            } else {
                journalEl.innerHTML = '<div class="loading-placeholder">今日の日記はまだ生成されていません。</div>';
            }
        }

        // 次のアクション
        const naEl = $('#dash-next-actions');
        if (naEl) {
            if (data.next_actions && data.next_actions.trim()) {
                const lines = data.next_actions.split('\n').filter(l => l.trim());
                naEl.innerHTML = lines.map(line => {
                    const clean = line.replace(/^-\s*/, '').trim();
                    const listMatch = clean.match(/^\[(.+?)\]\s*(.*)/);
                    if (listMatch) {
                        return `<div class="list-item" style="gap:8px;">
                            <span class="na-list-badge">${escapeHtml(listMatch[1])}</span>
                            <span>${escapeHtml(listMatch[2])}</span>
                        </div>`;
                    }
                    return `<div class="list-item">${escapeHtml(clean)}</div>`;
                }).join('');
            } else {
                naEl.innerHTML = '<div class="loading-placeholder">次のアクションはまだ生成されていません。</div>';
            }
        }

        // MIT バナー + スケジュールタブMITカード
        const mitBanner = $('#mit-banner');
        const mitItemsEl = $('#mit-banner-items');
        const mitScheduleEl = $('#mit-schedule-items');
        if (data.mit && data.mit.length > 0) {
            const mitHtml = data.mit.map((item, idx) => {
                const done = item.startsWith('[x]') || item.startsWith('[X]');
                const text = item.replace(/^\[[ xX]\]\s*/, '').trim();
                return `<div class="mit-banner-item ${done ? 'done' : ''}" data-mit-index="${idx}" onclick="toggleMit(${idx}, this)" role="button" tabindex="0" title="クリックで完了切替">${escapeHtml(text)}</div>`;
            }).join('');
            if (mitBanner && mitItemsEl) { mitItemsEl.innerHTML = mitHtml; mitBanner.classList.remove('hidden'); }
            if (mitScheduleEl) {
                mitScheduleEl.innerHTML = data.mit.map((item, idx) => {
                    const done = item.startsWith('[x]') || item.startsWith('[X]');
                    const text = item.replace(/^\[[ xX]\]\s*/, '').trim();
                    return `<div class="list-item mit-schedule-row" data-mit-index="${idx}" style="gap:8px;cursor:pointer;" onclick="toggleMit(${idx}, this)">
                        <div class="checkbox-custom" style="${done ? 'background:var(--accent);border-color:var(--accent);color:#fff;font-size:0.7rem;display:flex;align-items:center;justify-content:center;' : ''}">${done ? '✓' : ''}</div>
                        <span style="${done ? 'text-decoration:line-through;color:var(--text-muted);' : ''}">${escapeHtml(text)}</span>
                    </div>`;
                }).join('');
            }
        } else {
            if (mitBanner) mitBanner.classList.add('hidden');
            if (mitScheduleEl) mitScheduleEl.innerHTML = '<div class="loading-placeholder">MITはまだ設定されていません。「設定」ボタンから登録できます。</div>';
        }

        // 「書籍＆ナレッジ」のテキストを「書籍」に置換
        document.querySelectorAll('.section-title').forEach(el => {
            if(el.textContent.includes('書籍＆ナレッジ')) {
                el.textContent = el.textContent.replace('書籍＆ナレッジ', '書籍');
            }
        });

        loadStockedLinks();
        loadEnglishPhrases();

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

    if (!tasks || tasks.length === 0) {
        container.innerHTML = '<div class="loading-placeholder">未完了のタスクはありません</div>';
        return;
    }

    // 並び順は Google Tasks 側の position をマスターとし、API レスポンスの順序をそのまま尊重する。
    // （ユーザーが UI で並び替えた結果は /api/google_tasks_move で Google 側に書き込まれ、
    //   次回 fetch 時に position 順で返されてくる）
    // 完了タスクは末尾に
    const sortedActive = (tasks || []).filter(t => !t.completed).slice();
    const doneTasks = (tasks || []).filter(t => t.completed);

    // 親→子の階層構造を作る（parent フィールドあり）
    const byParent = new Map();
    for (const t of sortedActive) {
        const p = t.parent || '';
        if (!byParent.has(p)) byParent.set(p, []);
        byParent.get(p).push(t);
    }
    const orderedActive = [];
    const visit = (parentId, depth) => {
        const children = byParent.get(parentId) || [];
        for (const c of children) {
            orderedActive.push({ ...c, _depth: depth });
            if (depth === 0) visit(c.id, 1); // 1段だけネスト
        }
    };
    visit('', 0);
    // parent が存在しないが orderedActive に未登場のもの（孤児）を末尾に
    for (const t of sortedActive) {
        if (!orderedActive.find(x => x.id === t.id)) {
            orderedActive.push({ ...t, _depth: 0 });
        }
    }

    container.innerHTML = [
        ...orderedActive.map(t => {
            const dueLabel = t.due ? _formatDueLabel(t.due) : '';
            const dueAttr = t.due ? t.due.slice(0, 10) : '';
            const indent = t._depth ? 'margin-left:24px;' : '';
            const childMark = t._depth ? '<span style="color:var(--text-muted);margin-right:2px;">└</span>' : '';
            return `
            <div class="list-item gtask-item" style="gap:6px;${indent}" id="gtask-item-${t.id}" data-task-id="${t.id}" data-list="${listName}" data-parent="${t.parent || ''}">
                <span class="gtask-handle" style="cursor:grab;touch-action:none;color:var(--text-muted);font-size:1.1rem;padding:12px 10px;margin-left:-8px;user-select:none;" title="長押しして並び替え">⠿</span>
                ${childMark}
                <div class="checkbox-custom" onclick="toggleGoogleTask('${t.id}', '${listName}')" style="cursor:pointer;"></div>
                <div class="li-text" style="flex:1;">${escapeHtml(t.title)}${dueLabel ? `<span class="task-due-chip">${escapeHtml(dueLabel)}</span>` : ''}</div>
                <button class="task-due-btn" onclick="openTaskDueEditor('${t.id}', '${listName}', '${escapeHtml(t.title).replace(/'/g, '&#39;')}', '${dueAttr}')" title="締切を編集">📅</button>
            </div>`;
        }),
        ...doneTasks.map(t => `
            <div class="list-item" style="gap:6px; opacity:0.5;" id="gtask-item-${t.id}">
                <div class="checkbox-custom" style="background:var(--accent); border-color:var(--accent); pointer-events:none; display:flex; align-items:center; justify-content:center; color:#fff; font-size:0.7rem;">✓</div>
                <div class="li-text" style="flex:1; text-decoration:line-through; color:var(--text-muted);">${escapeHtml(t.title)}</div>
            </div>
        `)
    ].join('');

    // SortableJS でモバイル対応の並び替えを設定
    initTaskSortable(container, listName);
}

function initHabitSortable(container) {
    if (!window.Sortable) {
        setTimeout(() => initHabitSortable(container), 200);
        return;
    }
    if (container._sortable) {
        try { container._sortable.destroy(); } catch {}
    }
    container._sortable = window.Sortable.create(container, {
        handle: '.habit-handle',
        animation: 150,
        delay: 200,
        delayOnTouchOnly: true,
        touchStartThreshold: 5,
        fallbackTolerance: 3,
        ghostClass: 'sortable-ghost',
        chosenClass: 'sortable-chosen',
        dragClass: 'sortable-drag',
        onEnd: async (evt) => {
            const item = evt.item;
            const taskId = item && item.dataset && item.dataset.taskId;
            if (!taskId) return;
            const items = Array.from(container.querySelectorAll('.habit-item'));
            const idx = items.findIndex(el => el === item);
            if (idx === -1) return;
            const previous = idx > 0 ? items[idx - 1] : null;
            const previousId = previous ? (previous.dataset.taskId || null) : null;
            try {
                await apiFetch('/api/google_tasks_move', {
                    method: 'POST',
                    body: JSON.stringify({
                        task_id: taskId,
                        list_name: '習慣',
                        previous_task_id: previousId,
                    }),
                });
                showToast('並び替えました');
                setTimeout(() => loadHabits(), 100);
            } catch (e) {
                showToast('並び替えに失敗しました', true);
                loadHabits();
            }
        },
    });
}

function initTaskSortable(container, listName) {
    if (!window.Sortable) {
        // ライブラリがまだロードされていなければ少し待ってから再試行
        setTimeout(() => initTaskSortable(container, listName), 200);
        return;
    }
    // 古い Sortable インスタンスを破棄
    if (container._sortable) {
        try { container._sortable.destroy(); } catch {}
    }
    container._sortable = window.Sortable.create(container, {
        handle: '.gtask-handle',
        animation: 150,
        delay: 200,
        delayOnTouchOnly: true,
        touchStartThreshold: 5,
        fallbackTolerance: 3,
        ghostClass: 'sortable-ghost',
        chosenClass: 'sortable-chosen',
        dragClass: 'sortable-drag',
        // 完了済みは並び替え不可
        filter: '.list-item:not(.gtask-item)',
        onEnd: async (evt) => {
            const item = evt.item;
            const taskId = item && item.dataset && item.dataset.taskId;
            if (!taskId) return;
            const ln = item.dataset.list || listName;
            const items = Array.from(container.querySelectorAll('.gtask-item'));
            const idx = items.findIndex(el => el === item);
            if (idx === -1) return;
            const previous = idx > 0 ? items[idx - 1] : null;
            const previousId = previous ? previous.dataset.taskId : null;
            // 親が同じ階層に来た場合は parent を引き継ぐ
            const tasksRef = ln === '仕事' ? _currentWorkTasks : ln === '習慣' ? _currentHabitTasks : _currentPrivateTasks;
            const previousTask = previousId ? tasksRef.find(t => t.id === previousId) : null;
            const body = { task_id: taskId, list_name: ln };
            if (previousId) body.previous_task_id = previousId;
            if (previousTask && previousTask.parent) body.parent = previousTask.parent;

            try {
                await apiFetch('/api/google_tasks_move', {
                    method: 'POST',
                    body: JSON.stringify(body),
                });
                showToast('並び替えました');
                // 習慣と完全に同じパターン: 軽量エンドポイントで該当リストだけ再取得して再描画
                setTimeout(() => loadTaskList(ln, container), 100);
            } catch (e) {
                showToast('並び替えに失敗しました', true);
                loadTaskList(ln, container);
            }
        },
    });
}

// 指定リスト（仕事/プライベート/習慣）のタスクだけを軽量に再取得して再描画する。
// initTaskSortable の onEnd と完了トグル後の再描画で使用。loadDashboard を回避してスムーズ化。
async function loadTaskList(listName, container) {
    if (!container) return;
    try {
        const data = await apiFetch(`/api/google_tasks?list_name=${encodeURIComponent(listName)}`);
        const tasks = data.tasks || [];
        renderTaskGroup(container, tasks, listName);
    } catch (e) {
        console.error('loadTaskList error', e);
    }
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

let _taskDueCtx = null;
window.openTaskDueEditor = (taskId, listName, title, currentDue) => {
    _taskDueCtx = { taskId, listName, title };
    const modal = $('#task-due-modal');
    const input = $('#task-due-input');
    const titleEl = $('#task-due-title');
    if (titleEl) titleEl.textContent = `「${title}」の期限`;
    if (input) input.value = currentDue || '';
    if (modal) modal.classList.remove('hidden');
};
window.closeTaskDueModal = () => {
    const modal = $('#task-due-modal');
    if (modal) modal.classList.add('hidden');
    _taskDueCtx = null;
};
window.setDueQuick = (daysAhead) => {
    const input = $('#task-due-input');
    if (!input) return;
    const d = new Date();
    d.setDate(d.getDate() + daysAhead);
    input.value = d.toISOString().slice(0, 10);
};
window.clearTaskDue = () => {
    if (!_taskDueCtx) return;
    apiFetch('/api/google_tasks_action', {
        method: 'POST',
        body: JSON.stringify({ action: 'update', task_id: _taskDueCtx.taskId, list_name: _taskDueCtx.listName, due: '' })
    }).then(() => {
        showToast('締切をクリアしました');
        closeTaskDueModal();
        loadDashboard();
    }).catch(() => showToast('締切の更新に失敗しました', true));
};
window.submitTaskDue = () => {
    if (!_taskDueCtx) return;
    const input = $('#task-due-input');
    const value = input ? input.value.trim() : '';
    if (value && !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
        showToast('日付形式が不正です', true);
        return;
    }
    apiFetch('/api/google_tasks_action', {
        method: 'POST',
        body: JSON.stringify({ action: 'update', task_id: _taskDueCtx.taskId, list_name: _taskDueCtx.listName, due: value })
    }).then(() => {
        showToast(value ? `締切を ${value} に設定しました` : '締切をクリアしました');
        closeTaskDueModal();
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
            cb.style.background = 'var(--accent)';
            cb.style.borderColor = 'var(--accent)';
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
        const msgEl = appendMsg('assistant', data.reply);
        if (data.type === 'morning' && msgEl) _injectBriefingTTS(msgEl, data.reply);
        showToast(data.type === 'morning' ? '朝のブリーフィングです' : '夜のレビューです');
    } catch (e) {
        console.error(e);
        showToast('ブリーフィングの生成に失敗しました', true);
    }
};

function _injectBriefingTTS(msgEl, rawText) {
    // 「今日のワンフレーズ」セクションから英語フレーズを抽出してTTSボタンを追加
    const match = rawText.match(/今日のワンフレーズ[^\n]*\n+([^\n]*[A-Za-z][^\n]*)/);
    if (!match) return;
    const phrase = match[1].replace(/^[「"「"*\s]+|[」"」"*\s]+$/g, '').trim();
    if (!phrase || !/[A-Za-z]{3}/.test(phrase)) return;
    const bubble = msgEl.querySelector('.msg-bubble');
    if (!bubble) return;
    const btn = document.createElement('button');
    btn.style.cssText = 'display:inline-flex;align-items:center;gap:4px;margin-top:8px;padding:5px 12px;border:1px solid rgba(0,186,152,0.4);border-radius:20px;background:rgba(0,186,152,0.08);color:var(--accent);font-size:0.8rem;cursor:pointer;';
    btn.innerHTML = '🔊 今日のフレーズを聞く';
    btn.onclick = () => speakText(phrase);
    bubble.appendChild(document.createElement('br'));
    bubble.appendChild(btn);
}

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

window.onBreakdownExistingChange = (sel) => {
    if (!sel.value) return;
    try {
        const parsed = JSON.parse(sel.value);
        const listSelect = $('#breakdown-list-name');
        if (listSelect && parsed.list_name) listSelect.value = parsed.list_name;
    } catch (e) {}
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
    if (res) {
        // ゼロ秒思考のライフログ終了を記録
        sendActionCommandSilent(`ゼロ秒思考終了（テーマ：${res.theme}）`);
        closeZeroSecModal();
    }
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

    // ライフログ start（チャット経由でAIにログを記録させる）
    sendActionCommandSilent(`「${title}」読書開始`);

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
    // ライフログ end（時刻レンジ形式でログ記録）
    const startDate = new Date(readingState.startedAt);
    const endDate = new Date();
    const startTimeStr = startDate.getHours().toString().padStart(2,'0') + ':' + startDate.getMinutes().toString().padStart(2,'0');
    const endTimeStr = endDate.getHours().toString().padStart(2,'0') + ':' + endDate.getMinutes().toString().padStart(2,'0');
    sendActionCommandSilent(`${startTimeStr}-${endTimeStr} 「${title}」読書終了`);
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
            if (f === '__none__') return arr.filter(l => !l.tags || !l.tags.trim());
            return arr.filter(l => (l.tags || '').split(',').map(t => t.trim()).includes(f));
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
                const tags = (l.tags || '').trim();
                if (!tags) { noneCount++; continue; }
                for (const tag of tags.split(',').map(t => t.trim()).filter(Boolean)) {
                    counts.set(tag, (counts.get(tag) || 0) + 1);
                }
            }
            const sorted = [...counts.entries()].sort((a, b) => b[1] - a[1]);
            const cur = linkPurposeFilters[type];
            const opts = [`<option value="" ${cur===''?'selected':''}>🏷️ すべてのタグ (${arr.length})</option>`];
            for (const [tag, cnt] of sorted) {
                const safe = escapeHtml(tag);
                opts.push(`<option value="${safe}" ${cur===tag?'selected':''}>${safe} (${cnt})</option>`);
            }
            if (noneCount > 0) {
                opts.push(`<option value="__none__" ${cur==='__none__'?'selected':''}>(タグなし) (${noneCount})</option>`);
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
                purposeSelect.title = 'タグで絞り込み';
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
                if (lk.tags) {
                    lk.tags.split(',').map(t => t.trim()).filter(Boolean).forEach(tag => {
                        chips.push(`<span class="stocked-link-chip purpose">🏷️ ${escapeHtml(tag)}</span>`);
                    });
                }
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

    // タグ入力欄を設定（既存 purpose は後方互換フォールバック）
    const tagsInput = $('#link-tags-input');
    const tagsPreview = $('#link-tags-preview');
    if (tagsInput) {
        tagsInput.value = lk.tags || lk.purpose || '';
        const updatePreview = () => {
            if (!tagsPreview) return;
            const tags = tagsInput.value.split(',').map(t => t.trim()).filter(Boolean);
            tagsPreview.innerHTML = tags.map(t => `<span style="background:rgba(0,186,152,0.15); color:var(--accent); border-radius:12px; padding:2px 10px; font-size:0.75rem;">🏷️ ${escapeHtml(t)}</span>`).join('');
        };
        updatePreview();
        tagsInput.oninput = updatePreview;
    }

    $('#link-date-input').value = lk.target_date || '';
    $('#link-note-url-input').value = lk.linked_note_url || '';
    $('#link-summary-input').value = lk.summary || '';
    $('#link-memo-input').value = lk.memo || '';
    $('#link-calendar-check').checked = true;

    $('#link-details-modal').classList.remove('hidden');
    // モーダル表示後に textarea を内容量に合わせて自動リサイズ
    requestAnimationFrame(() => {
        document.querySelectorAll('#link-details-modal .auto-grow-textarea').forEach(autoResizeTextarea);
    });
};

// textarea の高さを内容に応じて自動調整するユーティリティ
function autoResizeTextarea(el) {
    if (!el) return;
    el.style.height = 'auto';
    const max = 600;
    el.style.height = Math.min(el.scrollHeight + 2, max) + 'px';
}
// auto-grow-textarea クラスを持つ全 textarea に input イベントで自動拡張を仕込む
document.addEventListener('input', (e) => {
    if (e.target && e.target.classList && e.target.classList.contains('auto-grow-textarea')) {
        autoResizeTextarea(e.target);
    }
});

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
        tags: ($('#link-tags-input')?.value || '').trim(),
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

// ENモード：Manager's reply 部分を抽出して Web Speech API で読み上げ
function speakEnglishReply(replyText) {
    if (!('speechSynthesis' in window)) return;
    // "🗣️ **Manager's reply:**\n> ..." の > から始まる行を抽出
    const match = replyText.match(/Manager(?:'s|s) reply[:\*\s]*\n>(.*?)(?:\n\n|\n💬|$)/si);
    const text = match ? match[1].replace(/^>?\s*/gm, '').trim() : '';
    if (!text) return;
    window.speechSynthesis.cancel();
    const utter = new SpeechSynthesisUtterance(text);
    utter.lang = 'en-US';
    utter.rate = 0.9;
    // 英語音声を優先して選択
    const voices = window.speechSynthesis.getVoices();
    const enVoice = voices.find(v => v.lang.startsWith('en') && !v.localService === false) ||
                    voices.find(v => v.lang.startsWith('en'));
    if (enVoice) utter.voice = enVoice;
    window.speechSynthesis.speak(utter);
}

// 汎用TTS関数（フレーズ帳・ブリーフィングで共用）
function speakText(text, lang = 'en-US') {
    if (!('speechSynthesis' in window)) { showToast('このブラウザは音声再生に対応していません', true); return; }
    if (!text) return;
    window.speechSynthesis.cancel();
    const utter = new SpeechSynthesisUtterance(text);
    utter.lang = lang;
    utter.rate = 0.9;
    const trySpeak = () => {
        const voices = window.speechSynthesis.getVoices();
        const voice = voices.find(v => v.lang.startsWith(lang.slice(0, 2)) && !v.localService === false)
                    || voices.find(v => v.lang.startsWith(lang.slice(0, 2)));
        if (voice) utter.voice = voice;
        utter.onerror = () => showToast('音声の再生に失敗しました', true);
        window.speechSynthesis.speak(utter);
    };
    if (window.speechSynthesis.getVoices().length > 0) {
        trySpeak();
    } else {
        window.speechSynthesis.onvoiceschanged = () => { window.speechSynthesis.onvoiceschanged = null; trySpeak(); };
    }
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

let _activeHabitsForGantt = [];
async function loadHabits() {
    try {
        const data = await apiFetch('/api/habits');
        const container = $('#dash-habits');
        if (!container) return;
        if (!data.habits || data.habits.length === 0) {
            container.innerHTML = '<div class="p-20 text-center text-secondary">登録された習慣はありません</div>';
            const wrap = $('#habit-progress-wrap');
            if (wrap) wrap.style.display = 'none';
            _activeHabitsForGantt = [];
            return;
        }
        // ガントチャート用に「現在の習慣」リストを順序付きで保持
        _activeHabitsForGantt = data.habits.map(h => ({ id: h.id, name: h.name }));

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
            const dueToday = h.due_today !== false; // デフォルトtrue
            const freq = h.frequency_days || 1;
            const streakText = (data.streaks && data.streaks[h.id]) || '';
            const streakMatch = streakText.match(/(\d+)/);
            const streakNum = streakMatch ? parseInt(streakMatch[1]) : 0;

            let streakBadge = '';
            if (streakNum > 0) {
                const color = streakNum >= 30 ? '#ff6600' : streakNum >= 14 ? '#ff9900' : streakNum >= 7 ? '#ffcc00' : streakNum >= 3 ? 'var(--accent)' : 'var(--text-muted)';
                const icon = streakNum >= 30 ? '🔥' : streakNum >= 7 ? '⚡' : '✨';
                streakBadge = `<span style="font-size:0.72rem; color:${color}; font-weight:700; white-space:nowrap; min-width:30px; text-align:right;">${icon}${streakNum}</span>`;
            }

            const trigger = (h.trigger || '').trim();
            const triggerChip = trigger
                ? `<span class="habit-trigger-chip" title="クリックで変更" onclick="event.stopPropagation(); openHabitTriggerModal('${escapeHtml(h.name)}', '${escapeHtml(trigger)}')">⏰ ${escapeHtml(trigger)}</span>`
                : `<button class="habit-trigger-add" title="いつやるかを設定" onclick="event.stopPropagation(); openHabitTriggerModal('${escapeHtml(h.name)}', '')">＋いつ</button>`;

            let freqChip = '';
            if (freq > 1) {
                const freqLabel = freq === 7 ? '週1回' : `${freq}日に1回`;
                const notDueStyle = !dueToday ? 'color:var(--text-muted);' : 'color:var(--accent);';
                freqChip = `<span style="font-size:0.68rem; padding:1px 5px; border-radius:3px; background:rgba(255,255,255,0.06); ${notDueStyle}">${freqLabel}${!dueToday ? ' (今日はお休み)' : ''}</span>`;
            }

            const dimmed = !dueToday && !isDone;
            return `
                <div class="habit-item ${isDone ? 'done' : ''}" id="habit-item-${h.id}" data-task-id="${h.task_id || ''}" data-name="${escapeHtml(h.name)}" style="${dimmed ? 'opacity:0.45;' : ''}">
                    <span class="habit-handle" style="cursor:grab;touch-action:none;color:var(--text-muted);font-size:1.1rem;padding:12px 10px;margin-left:-8px;user-select:none;" title="長押しして並び替え">⠿</span>
                    <button class="habit-check-btn" onclick="${isDone ? `uncompleteHabit('${h.name}', '${h.id}')` : `completeHabit('${h.name}', '${h.id}')`}" ${!isDone && !dueToday ? 'disabled' : ''} style="${isDone ? 'opacity:0.8;' : ''}">✔</button>
                    <div class="habit-name-wrap" style="flex:1; display:flex; flex-direction:column; gap:2px; min-width:0;">
                        <div class="habit-name">${escapeHtml(h.name)}${freqChip ? ' ' + freqChip : ''}</div>
                        <div class="habit-trigger-row">${triggerChip}</div>
                    </div>
                    ${streakBadge}
                </div>
            `;
        }).join('');

        initHabitSortable(container);

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

window.uncompleteHabit = async (habitName, hId) => {
    try {
        const item = $(`#habit-item-${hId}`);
        if (item) item.classList.remove('done');
        showToast(`「${habitName}」を未完了に戻しました`);
        await apiFetch('/api/habits/uncomplete', { method: 'POST', body: JSON.stringify({ habit_name: habitName }) });
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
            const color = score >= 80 ? 'var(--accent)' : score >= 60 ? '#ffaa00' : score > 0 ? '#ff5555' : 'rgba(255,255,255,0.1)';
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

const CATEGORY_LABEL = { work: '💼 仕事', study: '📚 勉強', idea: '💡 アイデア', reading: '📖 読書', task: '📋 タスク', other: '📝 その他' };

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
    // 保険: 文書全体での pointerup/touchend で残留 long-press-active を全消去（黒塗り表示の防止）
    const clearAllLongPress = () => {
        document.querySelectorAll('.message.long-press-active').forEach(el => el.classList.remove('long-press-active'));
    };
    document.addEventListener('pointerup', clearAllLongPress);
    document.addEventListener('touchend', clearAllLongPress);
    document.addEventListener('touchcancel', clearAllLongPress);
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
    const phraseBtn = document.getElementById('msg-action-phrase-btn');
    const transBtn = document.getElementById('msg-action-translate-btn');
    if (_longPressTarget) {
        sheet._targetEl = _longPressTarget;
        const isStarred = _longPressTarget.classList.contains('starred');
        if (starBtn) {
            starBtn.textContent = isStarred ? '⭐ お気に入り解除' : '⭐ お気に入り';
            starBtn.classList.toggle('is-active', isStarred);
        }
        const isAssistant = _longPressTarget.classList.contains('assistant');
        // 📚 フレーズ保存: AI/ユーザー両方のメッセージで利用可能（複数文選択モーダルで対応）
        if (phraseBtn) phraseBtn.style.display = '';
        // 英訳保存はユーザーメッセージのみ
        if (transBtn) transBtn.style.display = !isAssistant ? '' : 'none';
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

// ----- 英語フレーズ保存 -----
function _extractEnglishCandidates(text) {
    // 引用符・装飾を除去しつつ、英文として成立しそうな文/フレーズを抽出する。
    if (!text) return [];
    const clean = text.replace(/\*\*/g, '');
    const seen = new Set();
    const results = [];
    const push = (s) => {
        const v = (s || '').replace(/^[\s「"「"'"'>*\-]+|[\s」"」"'"'*]+$/g, '').trim();
        if (!v) return;
        if (v.length < 4) return;
        if (!/[A-Za-z]/.test(v)) return;
        // ある程度英語が支配的（半数以上が英字 or 空白記号）
        const enChars = (v.match(/[A-Za-z\s.,'’"\-?!]/g) || []).length;
        if (enChars / v.length < 0.6) return;
        const key = v.toLowerCase();
        if (seen.has(key)) return;
        seen.add(key);
        results.push(v);
    };
    // 1) 行ごとに走査し、文末記号で更に分割
    clean.split('\n').forEach(line => {
        const trimmed = line.trim();
        if (!trimmed) return;
        // 行頭の "> " や "- " 等を除去
        const stripped = trimmed.replace(/^[>\-•*]+\s*/, '');
        const sentences = stripped.split(/(?<=[.!?])\s+(?=[A-Z"'(])/);
        if (sentences.length > 1) {
            sentences.forEach(push);
        } else {
            push(stripped);
        }
    });
    // 2) 引用符内の英文も拾う（複数行をまたぐもの）
    const quoteRe = /[「"「"]([A-Za-z][^「"「"」"」"]{4,})[」"」"]/g;
    let m;
    while ((m = quoteRe.exec(clean))) push(m[1]);
    return results;
}

function _extractTranslationHint(text) {
    if (!text) return '';
    const m = text.match(/💬\s*(.+)/);
    return m ? m[1].trim() : '';
}

async function msgActionSavePhrase() {
    const text = _getLongPressText();
    closeMsgActionSheet();
    if (!text) return;

    const candidates = _extractEnglishCandidates(text);
    if (candidates.length === 0) {
        showToast('英文が見つかりませんでした', true);
        return;
    }

    const translationHint = _extractTranslationHint(text);
    openPhraseSelectModal(candidates, translationHint, text.slice(0, 500));
}

let _phraseSelectCtx = null;
function openPhraseSelectModal(candidates, translationHint, context) {
    _phraseSelectCtx = { candidates, translationHint, context };
    // 既存の残骸があれば必ず削除して新規作成（暗転残りを防止）
    document.getElementById('phrase-select-modal')?.remove();
    const modal = document.createElement('div');
    modal.id = 'phrase-select-modal';
    modal.className = 'modal-overlay';
    modal.innerHTML = `
        <div class="modal-card" style="max-width:480px;max-height:80vh;display:flex;flex-direction:column;">
            <h3 class="modal-title">📚 保存するフレーズを選択</h3>
            <p style="font-size:0.78rem;color:var(--text-muted);margin:0 0 8px;">複数選択して一括保存できます。</p>
            <div id="phrase-select-list" style="flex:1;overflow-y:auto;padding:4px 0;display:flex;flex-direction:column;gap:6px;"></div>
            <div style="display:flex;gap:8px;margin-top:12px;">
                <button class="modal-btn cancel" onclick="closePhraseSelectModal()">キャンセル</button>
                <button class="modal-btn submit" id="phrase-select-save" onclick="submitPhraseSelection()">保存</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    const listEl = modal.querySelector('#phrase-select-list');
    listEl.innerHTML = candidates.map((c, i) => `
        <label style="display:flex;align-items:flex-start;gap:8px;padding:8px;border:1px solid var(--border-glass);border-radius:8px;cursor:pointer;">
            <input type="checkbox" data-idx="${i}" ${candidates.length === 1 ? 'checked' : ''} style="margin-top:3px;flex-shrink:0;">
            <span style="font-size:0.88rem;line-height:1.4;flex:1;word-break:normal;overflow-wrap:anywhere;">${escapeHtml(c)}</span>
        </label>
    `).join('');
}

window.closePhraseSelectModal = () => {
    // hidden クラスではなく DOM から完全に削除して、暗いオーバーレイの残留を防ぐ
    document.getElementById('phrase-select-modal')?.remove();
    _phraseSelectCtx = null;
};

window.submitPhraseSelection = async () => {
    if (!_phraseSelectCtx) return;
    const modal = document.getElementById('phrase-select-modal');
    if (!modal) return;
    const checked = Array.from(modal.querySelectorAll('input[type="checkbox"]:checked'));
    if (checked.length === 0) { showToast('1つ以上選択してください', true); return; }
    const phrases = checked
        .map(cb => _phraseSelectCtx.candidates[parseInt(cb.dataset.idx, 10)])
        .filter(Boolean)
        .map(phrase => ({
            phrase,
            translation: phrases_pickTranslation(phrase, _phraseSelectCtx.translationHint, checked.length),
            context: _phraseSelectCtx.context,
        }));
    const saveBtn = document.getElementById('phrase-select-save');
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = '保存中…'; }
    try {
        await apiFetch('/api/english_phrases/bulk', {
            method: 'POST',
            body: JSON.stringify({ phrases }),
        });
        showToast(`📚 ${phrases.length}件のフレーズを保存しました`);
        loadEnglishPhrases();
    } catch (e) {
        showToast('保存に失敗しました', true);
    } finally {
        // 成功でも失敗でも必ず閉じる（モーダル残留 = 画面が暗いままの原因）
        closePhraseSelectModal();
    }
};

function phrases_pickTranslation(phrase, hint, total) {
    // 訳ヒントは選択数が1つのときだけ流用（複数選択時はどの文の訳かが曖昧なため空に）
    return total === 1 ? hint : '';
}

async function loadEnglishPhrases() {
    const el = $('#dash-english-phrases');
    if (!el) return;
    try {
        const data = await apiFetch('/api/english_phrases');
        if (!data.phrases || data.phrases.length === 0) {
            el.innerHTML = '<div class="loading-placeholder" style="font-size:0.8rem;">フレーズはまだありません。<br>メッセージを長押し → 📚 で保存できます。</div>';
            return;
        }
        el.innerHTML = data.phrases.map(p => {
            const date = new Date(p.created_at).toLocaleDateString('ja-JP', { month: 'short', day: 'numeric' });
            const safePhraseAttr = escapeHtml(p.phrase).replace(/'/g, "&#39;");
            return `
            <div class="phrase-item" style="display:flex;align-items:flex-start;gap:8px;padding:10px 18px;border-bottom:1px solid rgba(255,255,255,0.04);">
                <div style="flex:1;min-width:0;">
                    <div style="font-size:0.9rem;font-weight:600;color:var(--text-primary);line-height:1.4;">${escapeHtml(p.phrase)}</div>
                    ${p.translation ? `<div style="font-size:0.8rem;color:var(--text-secondary);margin-top:2px;">${escapeHtml(p.translation)}</div>` : ''}
                    <div style="font-size:0.72rem;color:var(--text-muted);margin-top:3px;">${date}</div>
                </div>
                <button onclick="speakText('${safePhraseAttr}')" style="background:none;border:none;cursor:pointer;font-size:1rem;opacity:0.75;padding:4px;flex-shrink:0;" title="読み上げ">🔊</button>
                <button onclick="deleteEnglishPhrase(${p.id})" style="background:none;border:none;cursor:pointer;font-size:0.85rem;opacity:0.45;padding:4px;flex-shrink:0;" title="削除">🗑</button>
            </div>`;
        }).join('');
    } catch {
        el.innerHTML = '<div class="loading-placeholder">フレーズ帳の読み込みに失敗しました</div>';
    }
}

async function deleteEnglishPhrase(id) {
    if (!confirm('このフレーズを削除しますか？')) return;
    try {
        await apiFetch(`/api/english_phrases/${id}`, { method: 'DELETE' });
        showToast('削除しました');
        loadEnglishPhrases();
    } catch {
        showToast('削除に失敗しました', true);
    }
}

// ----- コレクション機能 -----
let _collectionCurrentLabel = '';

window.openCollectionOverlay = async () => {
    document.getElementById('collection-overlay')?.classList.remove('hidden');
    const tabsEl = $('#collection-tabs');
    if (!tabsEl) return;
    tabsEl.innerHTML = '<span style="font-size:0.78rem;color:var(--text-muted);">読み込み中...</span>';
    try {
        const data = await apiFetch('/api/messages/collections');
        const labels = data.collections || [];
        if (!labels.length) {
            tabsEl.innerHTML = '<span style="font-size:0.78rem;color:var(--text-muted);">コレクションはまだありません</span>';
            $('#collection-results').innerHTML = '<p class="search-hint">メッセージを長押し → 🏷 で保存できます</p>';
            return;
        }
        tabsEl.innerHTML = labels.map(l => `
            <button class="modal-btn" style="font-size:0.78rem;padding:4px 12px;white-space:nowrap;" onclick="loadCollectionMessages('${escapeHtml(l)}')">${escapeHtml(l)}</button>
        `).join('');
        loadCollectionMessages(labels[0]);
    } catch (e) {
        tabsEl.innerHTML = '<span style="font-size:0.78rem;color:var(--text-muted);">取得失敗</span>';
    }
};

window.closeCollectionOverlay = (e) => {
    if (e && e.target.closest && e.target.closest('.search-panel')) return;
    document.getElementById('collection-overlay')?.classList.add('hidden');
};

window.loadCollectionMessages = async (label) => {
    _collectionCurrentLabel = label;
    const results = $('#collection-results');
    if (!results) return;
    results.innerHTML = '<p class="search-hint">読み込み中...</p>';
    try {
        const data = await apiFetch(`/api/messages/labeled?label=${encodeURIComponent(label)}`);
        const list = data.messages || [];
        if (!list.length) {
            results.innerHTML = `<p class="search-empty">「${escapeHtml(label)}」のメッセージはありません</p>`;
            return;
        }
        results.innerHTML = list.map(m => {
            const dt = new Date(m.timestamp);
            const tStr = `${dt.getMonth()+1}/${dt.getDate()} ${String(dt.getHours()).padStart(2,'0')}:${String(dt.getMinutes()).padStart(2,'0')}`;
            const preview = m.content.length > 160 ? m.content.slice(0, 160) + '...' : m.content;
            return `<div class="search-result-item" style="position:relative;">
                <div style="display:flex;justify-content:space-between;align-items:flex-start;">
                    <div onclick="jumpToMessage(${m.id}); closeCollectionOverlay();" style="flex:1;cursor:pointer;">
                        <div class="search-result-role">${m.role === 'assistant' ? 'AI' : 'YOU'}</div>
                        <div class="search-result-snippet">${escapeHtml(preview)}</div>
                        <div class="search-result-time">🏷 ${escapeHtml(label)} · ${tStr}</div>
                    </div>
                    <button onclick="removeMessageFromCollection(${m.id}, '${escapeHtml(label)}')" style="background:none;border:none;cursor:pointer;font-size:0.75rem;color:var(--text-muted);padding:4px 6px;white-space:nowrap;flex-shrink:0;" title="コレクションから解除">解除</button>
                </div>
            </div>`;
        }).join('');
    } catch (e) {
        results.innerHTML = '<p class="search-empty">取得に失敗しました</p>';
    }
};

window.removeMessageFromCollection = async (msgId, label) => {
    try {
        await apiFetch(`/api/messages/${msgId}/label`, { method: 'POST', body: JSON.stringify({ label: '' }) });
        showToast('コレクションから解除しました');
        loadCollectionMessages(label);
    } catch {
        showToast('解除に失敗しました', true);
    }
};

let _actionSheetMsgRole = 'assistant';

window.msgActionSaveToCollection = async () => {
    const msgEl = document.getElementById('msg-action-sheet')?._targetEl;
    if (!msgEl) { closeMsgActionSheet(); return; }
    const msgId = msgEl.dataset.msgId;
    if (!msgId) { showToast('メッセージIDが取得できません', true); closeMsgActionSheet(); return; }

    let collections = [];
    try {
        const d = await apiFetch('/api/messages/collections');
        collections = d.collections || [];
    } catch {}

    let labelName = '';
    if (collections.length > 0) {
        const hint = collections.slice(0, 5).join(' / ');
        labelName = prompt(`コレクション名を入力してください\n（既存: ${hint}）`);
    } else {
        labelName = prompt('コレクション名を入力してください（例: 仕事メモ、英語、アイデア）');
    }
    if (!labelName || !labelName.trim()) { closeMsgActionSheet(); return; }

    try {
        await apiFetch(`/api/messages/${msgId}/label`, { method: 'POST', body: JSON.stringify({ label: labelName.trim() }) });
        showToast(`「${labelName.trim()}」に保存しました`);
    } catch {
        showToast('保存に失敗しました', true);
    }
    closeMsgActionSheet();
};

window.msgActionTranslateAndSave = async () => {
    const raw = _getLongPressText();
    if (!raw) { closeMsgActionSheet(); return; }
    closeMsgActionSheet();
    showToast('英訳中...');
    try {
        const data = await apiFetch('/api/english_phrases/translate_and_save', {
            method: 'POST',
            body: JSON.stringify({ text: raw }),
        });
        showToast(`📚 英訳して保存: ${data.phrase}`);
        loadEnglishPhrases();
    } catch {
        showToast('保存に失敗しました', true);
    }
};

// ----- MIT設定モーダル -----
// 重い /api/dashboard ではなく専用 /api/mit_get を使う。先にモーダルを表示し、
// 取得は非同期で行う（体感的な遅延を排除）。
window.toggleMit = async (index, el) => {
    if (el && el.dataset && el.dataset.mitToggling === '1') return;
    if (el) el.dataset.mitToggling = '1';
    // 楽観更新
    document.querySelectorAll(`[data-mit-index="${index}"]`).forEach(node => {
        const wasDone = node.classList.contains('done') || node.querySelector('.checkbox-custom')?.textContent === '✓';
        if (node.classList.contains('mit-banner-item')) {
            node.classList.toggle('done', !wasDone);
        } else if (node.classList.contains('mit-schedule-row')) {
            const cb = node.querySelector('.checkbox-custom');
            const span = node.querySelector('span');
            const willBeDone = !wasDone;
            if (cb) {
                if (willBeDone) {
                    cb.style.cssText = 'background:var(--accent);border-color:var(--accent);color:#fff;font-size:0.7rem;display:flex;align-items:center;justify-content:center;';
                    cb.textContent = '✓';
                } else {
                    cb.style.cssText = '';
                    cb.textContent = '';
                }
            }
            if (span) span.style.cssText = willBeDone ? 'text-decoration:line-through;color:var(--text-muted);' : '';
        }
    });
    try {
        await apiFetch('/api/mit_toggle', {
            method: 'POST',
            body: JSON.stringify({ index }),
        });
    } catch (e) {
        showToast('MIT の更新に失敗しました', true);
        // 失敗したら再ロード
        if (typeof loadDashboard === 'function') loadDashboard();
    } finally {
        if (el) delete el.dataset.mitToggling;
    }
};

window.openMitModal = async () => {
    const modal = $('#mit-modal');
    if (!modal) return;
    // 即時にモーダルを表示し、入力欄を「読み込み中…」プレースホルダに
    ['#mit-input-1', '#mit-input-2', '#mit-input-3'].forEach(sel => {
        const el = $(sel);
        if (el) {
            el.value = '';
            el.placeholder = '読み込み中…';
            el.disabled = true;
        }
    });
    modal.classList.remove('hidden');
    setTimeout(() => $('#mit-input-1')?.focus(), 100);

    // バックグラウンドで MIT を取得して反映
    try {
        const data = await apiFetch('/api/mit_get');
        const items = (data.items || []).map(it => it.text || '');
        $('#mit-input-1').value = items[0] || '';
        $('#mit-input-2').value = items[1] || '';
        $('#mit-input-3').value = items[2] || '';
    } catch {}
    ['#mit-input-1', '#mit-input-2', '#mit-input-3'].forEach((sel, i) => {
        const el = $(sel);
        if (el) {
            el.placeholder = `MIT ${i + 1}`;
            el.disabled = false;
        }
    });
};

window.closeMitModal = (e) => {
    if (e && e.target.closest && e.target.closest('.modal-card')) return;
    $('#mit-modal')?.classList.add('hidden');
};

window.saveMitFromModal = async () => {
    const items = [
        $('#mit-input-1').value.trim(),
        $('#mit-input-2').value.trim(),
        $('#mit-input-3').value.trim(),
    ].filter(Boolean);
    if (!items.length) { showToast('MITを1つ以上入力してください', true); return; }

    const btn = $('#mit-save-btn');
    btn.textContent = '保存中...';
    btn.disabled = true;
    try {
        await apiFetch('/api/mit_set', { method: 'POST', body: JSON.stringify({ items }) });
        showToast('🎯 MITを保存しました');
        $('#mit-modal').classList.add('hidden');
        loadDashboard();
    } catch {
        showToast('保存に失敗しました', true);
    } finally {
        btn.textContent = '保存';
        btn.disabled = false;
    }
};

// ----- 瞑想タイマー -----
let _medState = { interval: null, remaining: 0, total: 0 };

window.openMeditationModal = () => {
    const modal = $('#meditation-modal');
    if (!modal) return;
    const lastMin = parseInt(localStorage.getItem('med_last_minutes') || '10', 10);
    const customInput = $('#med-custom-min');
    if (customInput) customInput.value = lastMin;
    $('#med-step-setup').style.display = '';
    $('#med-step-timer').style.display = 'none';
    $('#med-step-done').style.display = 'none';
    modal.classList.remove('hidden');
};

window.setMedDuration = (min) => {
    const input = $('#med-custom-min');
    if (input) input.value = min;
};

window.startMeditation = () => {
    const min = parseInt($('#med-custom-min').value || '10', 10);
    if (isNaN(min) || min < 1) { showToast('時間を入力してください', true); return; }
    localStorage.setItem('med_last_minutes', String(min));
    _medState.total = min * 60;
    _medState.remaining = min * 60;
    $('#med-step-setup').style.display = 'none';
    $('#med-step-timer').style.display = '';
    _updateMedDisplay();
    _medState.interval = setInterval(() => {
        _medState.remaining--;
        _updateMedDisplay();
        if (_medState.remaining <= 0) _finishMeditation(min);
    }, 1000);
    // ライフログに開始記録
    apiFetch('/api/task_action', { method: 'POST', body: JSON.stringify({ action: 'create', new_text: `瞑想開始（${min}分）` }) }).catch(() => {});
};

function _updateMedDisplay() {
    const m = Math.floor(_medState.remaining / 60);
    const s = _medState.remaining % 60;
    const el = $('#med-timer-display');
    if (el) el.textContent = `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}

function _finishMeditation(min) {
    clearInterval(_medState.interval);
    _medState.interval = null;
    $('#med-step-timer').style.display = 'none';
    $('#med-step-done').style.display = '';
    const msg = `${ min }分間の瞑想を完了しました。`;
    const doneEl = $('#med-done-msg');
    if (doneEl) doneEl.textContent = msg;
    // ライフログに完了記録
    apiFetch('/api/task_action', { method: 'POST', body: JSON.stringify({ action: 'create', new_text: `瞑想完了（${min}分）` }) }).catch(() => {});
    showToast(`🧘 ${msg}`);
}

window.stopMeditation = () => {
    if (_medState.interval) { clearInterval(_medState.interval); _medState.interval = null; }
    const elapsed = Math.max(0, _medState.total - _medState.remaining);
    const min = Math.round(elapsed / 60);
    if (min > 0) {
        apiFetch('/api/task_action', { method: 'POST', body: JSON.stringify({ action: 'create', new_text: `瞑想終了（${min}分）` }) }).catch(() => {});
    }
    $('#meditation-modal')?.classList.add('hidden');
};

window.closeMeditationModal = (e) => {
    if (e && e.target.closest && e.target.closest('.modal-card')) return;
    if (_medState.interval) stopMeditation();
    else $('#meditation-modal')?.classList.add('hidden');
};

// ----- Fitbit全データ -----
let _fitbitRows = [];
let _fitbitChart = null;
const FITBIT_METRIC_LABELS = {
    sleep_score: '睡眠スコア',
    total_sleep_minutes: '総睡眠時間（分）',
    deep_sleep_minutes: '深い睡眠（分）',
    rem_sleep_minutes: 'REM睡眠（分）',
    steps: '歩数',
    calories_out: '消費カロリー',
    resting_heart_rate: '安静時心拍数',
    distance_km: '距離（km）',
    active_minutes_very: '高強度活動分',
    hr_zone_fat_burn_minutes: '脂肪燃焼ゾーン分',
};

window.loadFitbitAllData = async (forceRefresh = false) => {
    const el = $('#dash-fitbit-all');
    const tableTarget = el;
    if (tableTarget) tableTarget.innerHTML = '<div class="loading-placeholder">読み込み中...</div>';
    try {
        const url = '/api/fitbit_all_data?days=14' + (forceRefresh ? '&_=' + Date.now() : '');
        const data = await apiFetch(url);
        _fitbitRows = data.data || [];
        if (!_fitbitRows.length) {
            if (tableTarget) tableTarget.innerHTML = '<div class="loading-placeholder">Fitbitデータがありません</div>';
            return;
        }
        renderFitbitTable();
        renderFitbitChart();
    } catch {
        if (tableTarget) tableTarget.innerHTML = '<div class="loading-placeholder">取得に失敗しました</div>';
    }
};

function renderFitbitTable() {
    const el = $('#dash-fitbit-all');
    if (!el) return;
    if (!_fitbitRows.length) { el.innerHTML = '<div class="loading-placeholder">データがありません</div>'; return; }
    el.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:0.78rem;">
        <thead><tr style="color:var(--text-muted);">
            <th style="padding:6px 8px;text-align:left;">日付</th>
            <th style="padding:6px 4px;text-align:center;">睡眠スコア</th>
            <th style="padding:6px 4px;text-align:center;">睡眠時間</th>
            <th style="padding:6px 4px;text-align:center;">歩数</th>
            <th style="padding:6px 4px;text-align:center;">カロリー</th>
        </tr></thead>
        <tbody>${_fitbitRows.map(r => `<tr style="border-top:1px solid rgba(255,255,255,0.05);">
            <td style="padding:6px 8px;color:var(--text-secondary);">${r.date}</td>
            <td style="padding:6px 4px;text-align:center;color:${r.sleep_score >= 80 ? 'var(--accent)' : r.sleep_score >= 60 ? '#ffd43b' : 'var(--text-primary)'};">${r.sleep_score ?? '—'}</td>
            <td style="padding:6px 4px;text-align:center;">${r.sleep_duration ?? '—'}</td>
            <td style="padding:6px 4px;text-align:center;">${r.steps ? r.steps.toLocaleString() : '—'}</td>
            <td style="padding:6px 4px;text-align:center;">${(r.calories_out ?? r.calories) ? (r.calories_out ?? r.calories).toLocaleString() : '—'}</td>
        </tr>`).join('')}</tbody>
    </table>`;
}

window.renderFitbitChart = () => {
    const canvas = $('#fitbit-chart');
    if (!canvas || !window.Chart) return;
    if (!_fitbitRows.length) return;
    const sel = $('#fitbit-metric-select');
    const metric = sel ? sel.value : 'sleep_score';
    if (sel) localStorage.setItem('mng_fitbit_metric', metric);

    const labels = _fitbitRows.map(r => r.date);
    const values = _fitbitRows.map(r => {
        const v = r[metric];
        return (v === null || v === undefined) ? null : Number(v);
    });
    // 7日移動平均
    const ma = values.map((_, i) => {
        const slice = values.slice(Math.max(0, i - 6), i + 1).filter(x => x !== null);
        if (slice.length === 0) return null;
        return slice.reduce((a, b) => a + b, 0) / slice.length;
    });

    if (_fitbitChart) {
        _fitbitChart.destroy();
        _fitbitChart = null;
    }
    _fitbitChart = new Chart(canvas, {
        type: 'line',
        data: {
            labels,
            datasets: [
                {
                    label: FITBIT_METRIC_LABELS[metric] || metric,
                    data: values,
                    borderColor: 'rgb(0,186,152)',
                    backgroundColor: 'rgba(0,186,152,0.15)',
                    tension: 0.2,
                    spanGaps: true,
                    fill: true,
                },
                {
                    label: '7日移動平均',
                    data: ma,
                    borderColor: 'rgba(255,212,84,0.85)',
                    borderDash: [4, 3],
                    pointRadius: 0,
                    tension: 0.2,
                    spanGaps: true,
                    fill: false,
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { labels: { color: '#cfd6df', font: { size: 11 } } },
            },
            scales: {
                x: { ticks: { color: '#7a8390', font: { size: 10 } }, grid: { color: 'rgba(255,255,255,0.05)' } },
                y: { ticks: { color: '#7a8390', font: { size: 10 } }, grid: { color: 'rgba(255,255,255,0.05)' } },
            },
        },
    });
};

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

// ----- お気に入りメッセージ オーバーレイ -----
function openStarredOverlay() {
    document.getElementById('starred-overlay')?.classList.remove('hidden');
    loadStarredMessages();
}

function closeStarredOverlay(e) {
    if (e && e.target.closest && e.target.closest('.search-panel')) return;
    document.getElementById('starred-overlay')?.classList.add('hidden');
}

async function loadStarredMessages() {
    const results = $('#starred-results');
    if (!results) return;
    results.innerHTML = '<p class="search-hint">読み込み中...</p>';
    try {
        const data = await apiFetch('/api/messages/starred');
        const list = data.messages || [];
        if (!list.length) {
            results.innerHTML = '<p class="search-empty">お気に入りメッセージがありません</p>';
            return;
        }
        results.innerHTML = list.map(m => {
            const dt = new Date(m.timestamp);
            const tStr = `${dt.getMonth()+1}/${dt.getDate()} ${String(dt.getHours()).padStart(2,'0')}:${String(dt.getMinutes()).padStart(2,'0')}`;
            const roleLabel = m.role === 'assistant' ? 'AI' : 'YOU';
            const preview = m.content.length > 160 ? m.content.slice(0, 160) + '...' : m.content;
            return `<div class="search-result-item" style="position:relative;">
                <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px;">
                    <div onclick="jumpToMessage(${m.id}); closeStarredOverlay();" style="flex:1;cursor:pointer;">
                        <div class="search-result-role">${roleLabel}</div>
                        <div class="search-result-snippet">${escapeHtml(preview)}</div>
                        <div class="search-result-time">⭐ ${tStr}</div>
                    </div>
                    <button onclick="event.stopPropagation(); unstarMessageInList(${m.id})" style="background:none;border:none;cursor:pointer;font-size:0.75rem;color:var(--text-muted);padding:4px 6px;white-space:nowrap;flex-shrink:0;" title="お気に入りから解除">解除</button>
                </div>
            </div>`;
        }).join('');
    } catch (e) {
        results.innerHTML = '<p class="search-empty">取得に失敗しました</p>';
    }
}

window.unstarMessageInList = async (msgId) => {
    try {
        await apiFetch(`/api/messages/${msgId}/star`, { method: 'POST' });
        showToast('お気に入りから解除しました');
        loadStarredMessages();
        // 画面に該当メッセージが表示されていれば UI も更新
        const el = document.querySelector(`.message[data-msg-id="${msgId}"]`);
        if (el) {
            el.classList.remove('starred');
            const btn = el.querySelector('.star-btn');
            if (btn) btn.textContent = '☆';
        }
    } catch {
        showToast('解除に失敗しました', true);
    }
};

// ----- 天気場所選択（岡山 北部/南部のみ） -----
let _weatherRegions = [
    { code: '33/6610', name: '岡山（南部）' },
    { code: '33/6620', name: '岡山（北部）' },
];

async function openWeatherLocationPicker() {
    document.getElementById('weather-location-overlay')?.classList.remove('hidden');
    const list = $('#weather-location-list');
    if (!list) return;
    list.innerHTML = '<p class="search-hint">読み込み中...</p>';
    try {
        const data = await apiFetch('/api/weather/locations');
        if (Array.isArray(data.regions) && data.regions.length) {
            _weatherRegions = data.regions;
        }
    } catch (e) {
        // 失敗してもデフォルト 2 地域を使う
    }
    _renderWeatherRegions();
}

function _renderWeatherRegions() {
    const list = $('#weather-location-list');
    if (!list) return;
    const current = localStorage.getItem('mng_weather_location') || '33/6610';
    list.innerHTML = '<p class="search-hint" style="font-size:0.82rem;color:var(--text-secondary);margin-bottom:8px;">地域を選択</p>' +
        _weatherRegions.map(r => `
            <div class="search-result-item" style="${current === r.code ? 'background:rgba(0,186,152,0.12);' : ''}" onclick="selectWeatherLocation('${r.code}', '${escapeHtml(r.name)}')">
                <div class="search-result-role">🌤️</div>
                <div class="search-result-snippet">${escapeHtml(r.name)}</div>
                ${current === r.code ? '<div class="search-result-time">✓ 選択中</div>' : ''}
            </div>
        `).join('');
}

function closeWeatherLocationPicker(e) {
    if (e && e.target.closest && e.target.closest('.search-panel')) return;
    document.getElementById('weather-location-overlay')?.classList.add('hidden');
}

async function selectWeatherLocation(code, name) {
    localStorage.setItem('mng_weather_location', code);
    closeWeatherLocationPicker();
    showToast(`場所を「${name}」に変更しました`);
    const weatherEl = $('#dash-weather');
    if (weatherEl) weatherEl.innerHTML = '<div class="loading-placeholder">天気を取得中...</div>';
    const titleEl = $('#weather-card-title');
    if (titleEl) titleEl.textContent = `天気 (${name})`;
    try {
        const wd = await apiFetch(`/api/weather?location=${encodeURIComponent(code)}`);
        if (wd && wd.summary !== '取得失敗') renderWeather(wd, weatherEl);
    } catch (e) {
        if (weatherEl) weatherEl.innerHTML = '<div class="loading-placeholder">気象データを取得できませんでした</div>';
    }
}

// ----- ロケーションログ手動同期 -----
window.triggerLocationSync = async () => {
    const dateFrom = $('#location-sync-date-from')?.value || '';
    const dateTo = $('#location-sync-date-to')?.value || '';
    const resultEl = $('#location-sync-result');
    if (resultEl) resultEl.textContent = '同期中...';
    try {
        const body = dateFrom && dateTo
            ? { date_from: dateFrom, date_to: dateTo }
            : { date: dateFrom || new Date().toISOString().slice(0, 10) };
        const res = await apiFetch('/api/location_log/sync', {
            method: 'POST',
            body: JSON.stringify(body)
        });
        if (resultEl) resultEl.textContent = res.message || '同期完了';
    } catch (e) {
        if (resultEl) resultEl.textContent = '同期に失敗しました';
    }
};

// ライフログ行の編集（モーダル: 開始時刻・終了時刻・内容に分離）
let _lifelogEditCtx = null;
window.editLifeLog = (lineIndex, currentTextEncoded) => {
    // currentText は HTML エンコード済みで来る（'は &#39;）。デコード
    const tmp = document.createElement('textarea');
    tmp.innerHTML = currentTextEncoded;
    const currentText = tmp.value;
    _lifelogEditCtx = { lineIndex, original: currentText };
    // パース: "HH:MM - HH:MM 内容" / "HH:MM ▶ 内容" / "HH:MM 内容" / "??:?? - HH:MM 内容" などに対応
    const m = currentText.match(/^\s*-?\s*(\d{1,2}:\d{2}|\?\?:\?\?)(?:\s*[-–~〜]\s*(\d{1,2}:\d{2}))?\s*▶?\s*(.*)$/);
    let start = '', end = '', body = currentText;
    if (m) {
        start = m[1] === '??:??' ? '' : (m[1] || '');
        end = m[2] || '';
        body = (m[3] || '').trim();
    }
    const startEl = $('#lifelog-edit-start');
    const endEl = $('#lifelog-edit-end');
    const textEl = $('#lifelog-edit-text');
    if (startEl) startEl.value = start.length === 4 ? '0' + start : start;
    if (endEl) endEl.value = end.length === 4 ? '0' + end : end;
    if (textEl) textEl.value = body;
    const modal = $('#lifelog-edit-modal');
    if (modal) modal.classList.remove('hidden');
};

window.closeLifeLogEdit = () => {
    const modal = $('#lifelog-edit-modal');
    if (modal) modal.classList.add('hidden');
    _lifelogEditCtx = null;
};

window.submitLifeLogEdit = async () => {
    if (!_lifelogEditCtx) return;
    const start = ($('#lifelog-edit-start')?.value || '').trim();
    const end = ($('#lifelog-edit-end')?.value || '').trim();
    const body = ($('#lifelog-edit-text')?.value || '').trim();
    if (!body) {
        showToast('内容を入力してください', true);
        return;
    }
    let line;
    if (start && end) {
        line = `${start} - ${end} ${body}`;
    } else if (start) {
        line = `${start} ▶ ${body}`;
    } else if (end) {
        line = `??:?? - ${end} ${body}`;
    } else {
        line = body;
    }
    try {
        await apiFetch('/api/task_action', {
            method: 'POST',
            body: JSON.stringify({ action: 'edit_log', line_index: _lifelogEditCtx.lineIndex, new_text: line })
        });
        showToast('ログを更新しました');
        closeLifeLogEdit();
        loadDashboard();
    } catch (e) {
        showToast('更新に失敗しました', true);
    }
};

window.confirmLifeLogDelete = async () => {
    if (!_lifelogEditCtx) return;
    if (!confirm('このログ行を削除しますか？')) return;
    try {
        await apiFetch('/api/task_action', {
            method: 'POST',
            body: JSON.stringify({ action: 'delete_log', line_index: _lifelogEditCtx.lineIndex })
        });
        showToast('ログを削除しました');
        closeLifeLogEdit();
        loadDashboard();
    } catch (e) {
        showToast('削除に失敗しました', true);
    }
};

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

// ----- リンク詳細フィールド可視性 (全フィールド常時表示に統一済み) -----
// HTMLモーダルからtype別の表示切り替えを廃止したため no-op を維持
function applyLinkFieldVisibility(_linkType) { /* no-op */ }

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
    // Fitbit 指標選択状態の復元
    const savedMetric = localStorage.getItem('mng_fitbit_metric');
    const sel = $('#fitbit-metric-select');
    if (sel && savedMetric) sel.value = savedMetric;
};


// ===========================================================
// デイリーサマリー（1日の統合ログ）
// ===========================================================

let _dailySummaryGenerating = false;

window.loadDailySummary = async () => {
    const tEl = $('#dash-daily-summary');
    const qEl = $('#dash-daily-summary-questions');
    if (!tEl || !qEl) return;
    try {
        const data = await apiFetch('/api/daily_summary');
        renderDailySummaryCard(data);
    } catch {
        tEl.innerHTML = '<div class="loading-placeholder">読み込みに失敗しました</div>';
    }
};

function renderDailySummaryCard(data) {
    const tEl = $('#dash-daily-summary');
    const qEl = $('#dash-daily-summary-questions');
    if (!tEl || !qEl) return;
    const text = (data && data.text) || '';
    const questions = ((data && data.questions) || []).filter(q => q.status !== 'resolved');

    if (text) {
        // 軽量 Markdown レンダリング: 改行、見出し、箇条書き
        const html = text
            .split('\n')
            .map(line => {
                if (/^## /.test(line)) return `<div style="margin:10px 0 4px;font-weight:700;color:var(--accent);font-size:0.9rem;">${escapeHtml(line.replace(/^## /, ''))}</div>`;
                if (/^# /.test(line)) return `<div style="margin:6px 0;font-weight:700;font-size:1rem;">${escapeHtml(line.replace(/^# /, ''))}</div>`;
                if (/^[\-*] /.test(line)) return `<div style="padding:2px 0 2px 12px;border-left:2px solid rgba(0,186,152,0.3);margin:2px 0;font-size:0.88rem;">${escapeHtml(line.replace(/^[\-*]\s+/, ''))}</div>`;
                return `<div style="font-size:0.88rem;line-height:1.6;">${escapeHtml(line)}</div>`;
            })
            .join('');
        tEl.innerHTML = html;
    } else {
        tEl.innerHTML = '<div class="loading-placeholder">「生成」を押してデイリーサマリーを作成します</div>';
    }

    if (!questions.length) {
        qEl.innerHTML = '';
        qEl.style.display = 'none';
        return;
    }
    if (_summaryQuestionsHidden) {
        qEl.style.display = '';
        qEl.innerHTML = `
            <div style="display:flex;align-items:center;justify-content:space-between;font-size:0.78rem;color:var(--text-muted);">
                <span>❓ ${questions.length} 件の質問があります（折りたたみ中）</span>
                <button class="mini-link" onclick="toggleSummaryQuestions()">開く</button>
            </div>
        `;
        return;
    }
    qEl.style.display = '';
    const date = (data && data.date) || (new Date()).toISOString().slice(0, 10);
    qEl.innerHTML = `
        <div style="display:flex;align-items:center;justify-content:space-between;font-size:0.78rem;color:#ffd454;margin-bottom:8px;gap:6px;">
            <span style="flex:1;">❓ マネージャーから ${questions.length} 件の質問があります（回答すると正確なサマリーが生成されます）</span>
            <button onclick="toggleSummaryQuestions()" title="質問を閉じる" style="background:none;border:none;color:var(--text-muted);font-size:1.1rem;cursor:pointer;padding:0 4px;line-height:1;">✕</button>
        </div>
        ${questions.map(q => `
            <div class="summary-question" data-qid="${q.id}" style="background:rgba(255,255,255,0.04);border-radius:8px;padding:10px;margin-bottom:8px;">
                <div style="font-size:0.84rem;color:var(--text-primary);margin-bottom:6px;">${escapeHtml(q.question)}</div>
                <textarea class="modern-input summary-question-answer" rows="2" placeholder="回答を入力（後でも構いません）" style="width:100%;padding:6px;font-size:0.85rem;">${escapeHtml(q.answer || '')}</textarea>
                <div style="display:flex;gap:6px;margin-top:6px;justify-content:flex-end;">
                    <button class="modal-btn cancel" style="padding:4px 10px;font-size:0.78rem;" onclick="dismissSummaryQuestion(${q.id})">削除</button>
                </div>
            </div>
        `).join('')}
        <button class="modal-btn submit" style="width:100%;font-size:0.85rem;margin-top:6px;" onclick="regenerateSummaryWithAnswers('${date}')">回答を反映して再生成</button>
    `;
}

let _summaryQuestionsHidden = false;
window.toggleSummaryQuestions = () => {
    _summaryQuestionsHidden = !_summaryQuestionsHidden;
    loadDailySummary();
};

window.generateDailySummary = async (finalize) => {
    if (_dailySummaryGenerating) return;
    _dailySummaryGenerating = true;
    const tEl = $('#dash-daily-summary');
    if (tEl) tEl.innerHTML = '<div class="loading-placeholder">サマリーを生成中…（少し時間がかかります）</div>';
    try {
        const result = await apiFetch('/api/daily_summary/generate', {
            method: 'POST',
            body: JSON.stringify({ finalize: !!finalize }),
        });
        renderDailySummaryCard({ date: result.date, text: result.summary, questions: result.questions });
        if (result.saved) {
            showToast('Obsidianに保存しました');
        } else if (result.questions && result.questions.length) {
            showToast('未確定の質問があります。回答すると確定保存されます。');
        }
    } catch (e) {
        if (tEl) tEl.innerHTML = '<div class="loading-placeholder">生成に失敗しました</div>';
        showToast('生成に失敗しました', true);
    } finally {
        _dailySummaryGenerating = false;
    }
};

window.saveSummaryAnswer = async (qid) => {
    const row = document.querySelector(`.summary-question[data-qid="${qid}"]`);
    if (!row) return;
    const ta = row.querySelector('.summary-question-answer');
    const answer = (ta && ta.value || '').trim();
    if (!answer) {
        showToast('回答を入力してください', true);
        return;
    }
    try {
        await apiFetch(`/api/daily_questions/${qid}/answer`, {
            method: 'POST',
            body: JSON.stringify({ answer }),
        });
        showToast('回答を保存しました');
        loadDailySummary();
    } catch {
        showToast('保存に失敗しました', true);
    }
};

window.dismissSummaryQuestion = async (qid) => {
    if (!confirm('この質問を削除しますか？')) return;
    try {
        await apiFetch(`/api/daily_questions/${qid}`, { method: 'DELETE' });
        loadDailySummary();
    } catch {
        showToast('削除に失敗しました', true);
    }
};

window.regenerateSummaryWithAnswers = async (date) => {
    const answers = {};
    document.querySelectorAll('.summary-question').forEach(row => {
        const qid = row.dataset.qid;
        const ta = row.querySelector('.summary-question-answer');
        const v = (ta && ta.value || '').trim();
        if (qid && v) answers[qid] = v;
    });
    if (_dailySummaryGenerating) return;
    _dailySummaryGenerating = true;
    const tEl = $('#dash-daily-summary');
    if (tEl) tEl.innerHTML = '<div class="loading-placeholder">回答を反映してサマリーを再生成中…</div>';
    try {
        const result = await apiFetch('/api/daily_summary/generate', {
            method: 'POST',
            body: JSON.stringify({ date, answers, finalize: false }),
        });
        renderDailySummaryCard({ date: result.date, text: result.summary, questions: result.questions });
        if (result.saved) {
            showToast('Obsidianに保存しました');
        } else if (result.questions && result.questions.length) {
            showToast('まだ確認が必要な点があります。');
        }
    } catch (e) {
        showToast('生成に失敗しました', true);
    } finally {
        _dailySummaryGenerating = false;
    }
};


// ===========================================================
// 今日の日記 編集
// ===========================================================

window.openDailyJournalEditor = async () => {
    const modal = $('#daily-journal-edit-modal');
    const ta = $('#daily-journal-edit-text');
    if (!modal || !ta) return;
    // 既存内容（生成済みの今日の日記）をデフォルトで表示
    const journalEl = $('#dash-daily-journal');
    const fallback = journalEl ? (journalEl.textContent || '').trim() : '';
    ta.value = fallback;
    ta.disabled = true;
    modal.classList.remove('hidden');
    setTimeout(() => ta.focus(), 100);
    try {
        const data = await apiFetch('/api/daily_journal');
        if (data && typeof data.text === 'string') {
            ta.value = data.text;
        }
    } catch (e) {
        showToast('読み込みに失敗しました', true);
    } finally {
        ta.disabled = false;
    }
};

window.closeDailyJournalEditor = () => {
    $('#daily-journal-edit-modal')?.classList.add('hidden');
};

window.submitDailyJournalEdit = async () => {
    const ta = $('#daily-journal-edit-text');
    if (!ta) return;
    const text = ta.value;
    try {
        await apiFetch('/api/daily_journal', {
            method: 'POST',
            body: JSON.stringify({ text }),
        });
        showToast('日記を保存しました（Obsidian反映済み）');
        closeDailyJournalEditor();
        loadDashboard();
    } catch (e) {
        showToast('保存に失敗しました', true);
    }
};

// ===========================================================
// デイリーサマリー 編集
// ===========================================================

window.openDailySummaryEditor = async () => {
    const modal = $('#daily-summary-edit-modal');
    const ta = $('#daily-summary-edit-text');
    if (!modal || !ta) return;
    // 生成済みのサマリー本文をデフォルトで表示
    ta.value = '';
    ta.disabled = true;
    modal.classList.remove('hidden');
    setTimeout(() => ta.focus(), 100);
    try {
        const data = await apiFetch('/api/daily_summary');
        ta.value = (data && data.text) || '';
    } catch (e) {
        showToast('読み込みに失敗しました', true);
    } finally {
        ta.disabled = false;
    }
};

window.closeDailySummaryEditor = () => {
    $('#daily-summary-edit-modal')?.classList.add('hidden');
};

window.submitDailySummaryEdit = async () => {
    const ta = $('#daily-summary-edit-text');
    if (!ta) return;
    const text = ta.value;
    try {
        await apiFetch('/api/daily_summary', {
            method: 'POST',
            body: JSON.stringify({ text }),
        });
        showToast('サマリーを保存しました（Obsidian反映済み）');
        closeDailySummaryEditor();
        loadDailySummary();
    } catch (e) {
        showToast('保存に失敗しました', true);
    }
};


// ===========================================================
// 英語フレーズ クイズ
// ===========================================================

let _quizCurrent = null;
let _quizSession = { correct: 0, total: 0 };

window.openEnglishQuiz = async () => {
    const modal = $('#english-quiz-modal');
    if (!modal) return;
    _quizSession = { correct: 0, total: 0 };
    modal.classList.remove('hidden');
    await loadNextQuiz();
};

window.closeEnglishQuiz = () => {
    $('#english-quiz-modal')?.classList.add('hidden');
    _quizCurrent = null;
};

window.loadNextQuiz = async () => {
    const qEl = $('#quiz-question');
    const cEl = $('#quiz-context');
    const oEl = $('#quiz-options');
    const fEl = $('#quiz-feedback');
    const progEl = $('#quiz-progress');
    if (!qEl || !oEl) return;
    qEl.textContent = '読み込み中…';
    cEl.textContent = '';
    oEl.innerHTML = '';
    fEl.classList.add('hidden');
    fEl.textContent = '';
    try {
        const data = await apiFetch('/api/english_phrases/quiz');
        _quizCurrent = data;
        // 出題: 日本語訳または context を提示し、英語を当てさせる
        const cue = data.translation || data.context || '（訳語未設定）';
        qEl.textContent = cue;
        if (data.context && data.context !== data.translation) {
            cEl.textContent = '文脈: ' + data.context;
        }
        const options = data.options || [data.phrase];
        oEl.innerHTML = options.map(opt => `
            <button class="quiz-option-btn" onclick="answerEnglishQuiz('${escapeHtml(opt).replace(/'/g, '&#39;')}')">${escapeHtml(opt)}</button>
        `).join('');
        if (progEl) {
            const acc = data.attempt_count > 0 ? Math.round((data.correct_count / data.attempt_count) * 100) : null;
            progEl.textContent = `セッション正解 ${_quizSession.correct}/${_quizSession.total}` +
                (acc !== null ? ` ・ この問題の正解率 ${acc}% (${data.correct_count}/${data.attempt_count})` : ' ・ 初出題');
        }
    } catch (e) {
        qEl.textContent = 'フレーズが登録されていません';
        oEl.innerHTML = '';
    }
};

window.answerEnglishQuiz = async (chosenEncoded) => {
    if (!_quizCurrent) return;
    const tmp = document.createElement('textarea');
    tmp.innerHTML = chosenEncoded;
    const chosen = tmp.value;
    const correct = chosen === _quizCurrent.phrase;
    _quizSession.total++;
    if (correct) _quizSession.correct++;
    const fEl = $('#quiz-feedback');
    if (fEl) {
        fEl.classList.remove('hidden');
        if (correct) {
            fEl.style.background = 'rgba(0,186,152,0.15)';
            fEl.style.color = 'var(--accent)';
            fEl.textContent = '✅ 正解！';
        } else {
            fEl.style.background = 'rgba(255,107,107,0.15)';
            fEl.style.color = '#ff8080';
            fEl.textContent = `❌ 不正解 — 正解: ${_quizCurrent.phrase}`;
        }
    }
    // ボタンを無効化
    document.querySelectorAll('.quiz-option-btn').forEach(b => {
        b.disabled = true;
        if (b.textContent === _quizCurrent.phrase) {
            b.style.borderColor = 'var(--accent)';
            b.style.color = 'var(--accent)';
        } else if (b.textContent === chosen && !correct) {
            b.style.borderColor = '#ff8080';
            b.style.color = '#ff8080';
        }
    });
    try {
        await apiFetch('/api/english_phrases/answer', {
            method: 'POST',
            body: JSON.stringify({ phrase_id: _quizCurrent.id, correct }),
        });
    } catch {}
};


// ===========================================================
// 習慣ガントチャート
// ===========================================================

window.toggleHabitGantt = async () => {
    const wrap = $('#habit-gantt');
    const chev = $('#habit-gantt-chevron');
    if (!wrap) return;
    const opening = wrap.style.display === 'none' || !wrap.style.display;
    wrap.style.display = opening ? 'block' : 'none';
    if (chev) chev.textContent = opening ? '▼' : '▶';
    if (opening) {
        await renderHabitGantt();
    }
};

async function renderHabitGantt() {
    const wrap = $('#habit-gantt');
    if (!wrap) return;
    wrap.innerHTML = '<div class="loading-placeholder">読み込み中…</div>';
    try {
        const data = await apiFetch('/api/habits/gantt?days=90');
        const allHabits = data.habits || [];
        const dates = data.dates || [];

        // 現在の習慣（Google Tasks にあるもの）のみ・習慣トラッカー順
        let activeOrder = _activeHabitsForGantt;
        if (!activeOrder || !activeOrder.length) {
            try {
                const hd = await apiFetch('/api/habits');
                activeOrder = (hd.habits || []).map(h => ({ id: h.id, name: h.name }));
                _activeHabitsForGantt = activeOrder;
            } catch {
                activeOrder = [];
            }
        }
        const orderMap = new Map(activeOrder.map((h, i) => [h.id, i]));
        const filtered = allHabits
            .filter(h => orderMap.has(h.id))
            .sort((a, b) => orderMap.get(a.id) - orderMap.get(b.id));

        if (!filtered.length) {
            wrap.innerHTML = '<div class="loading-placeholder">表示できる習慣がありません</div>';
            return;
        }

        const cellWidth = 8;
        const cellHeight = 14;
        const labelWidth = 110;
        const totalWidth = labelWidth + dates.length * (cellWidth + 1);

        // 各習慣に色を割り当て（HSL 色相を均等配分）
        const colorFor = (i, total) => {
            const hue = Math.round((360 / Math.max(total, 1)) * i);
            return {
                strong: `hsl(${hue}, 70%, 55%)`,
                mid:    `hsl(${hue}, 65%, 48%)`,
                weak:   `hsl(${hue}, 50%, 38%)`,
                empty:  'rgba(255,255,255,0.04)',
            };
        };

        const leftHtml = filtered.map((h, i) => {
            const palette = colorFor(i, filtered.length);
            return `
                <div style="height:18px;display:flex;align-items:center;gap:5px;padding-right:6px;margin-bottom:4px;font-size:0.72rem;">
                    <span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${palette.strong};flex-shrink:0;"></span>
                    <span style="color:var(--text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(h.name)}">${escapeHtml(h.name)}</span>
                </div>
            `;
        }).join('');

        const rightHtml = filtered.map((h, i) => {
            const palette = colorFor(i, filtered.length);
            const cellsHtml = h.cells.map((v, idx) => {
                const prev = idx > 0 ? h.cells[idx - 1] : 0;
                const next = idx < h.cells.length - 1 ? h.cells[idx + 1] : 0;
                let bg = palette.empty;
                let h2 = cellHeight;
                if (v) {
                    if (prev && next) { bg = palette.strong; }
                    else if (prev || next) { bg = palette.mid; }
                    else { bg = palette.weak; h2 = cellHeight - 2; }
                }
                const isToday = idx === h.cells.length - 1;
                const border = isToday ? 'box-shadow:0 0 0 1px var(--accent);' : '';
                return `<span style="display:inline-block;width:${cellWidth}px;height:${h2}px;margin-right:1px;border-radius:2px;background:${bg};${border}vertical-align:middle;" title="${dates[idx] || ''}"></span>`;
            }).join('');
            return `
                <div style="height:18px;display:flex;align-items:center;margin-bottom:4px;white-space:nowrap;">
                    ${cellsHtml}
                </div>
            `;
        }).join('');

        wrap.style.overflowX = 'hidden';
        wrap.innerHTML = `
            <div style="display:flex; padding-bottom:8px; max-width:100%;">
                <div style="width:${labelWidth}px; flex-shrink:0;">
                    <div style="height:14px; margin-bottom:6px;"></div>
                    ${leftHtml}
                </div>
                <div style="flex:1 1 0; min-width:0; overflow-x:auto; -webkit-overflow-scrolling:touch; scrollbar-width:thin;" class="hide-scroll">
                    <div style="font-size:0.7rem;color:var(--text-muted);margin-bottom:6px;white-space:nowrap;">過去 ${dates.length} 日 ・ 習慣ごとに色分け（連続達成は濃く表示）</div>
                    <div style="min-width:${dates.length * (cellWidth + 1)}px;">
                        ${rightHtml}
                    </div>
                </div>
            </div>
        `;
    } catch (e) {
        wrap.innerHTML = '<div class="loading-placeholder">取得に失敗しました</div>';
    }
}

// 習慣カードに既存のヒートマップが描画されたら、ガントセクションも表示
const _origLoadHabits = typeof window.loadHabits === 'function' ? window.loadHabits : null;
// loadHabits は app 内で定義済み。表示制御は heatmap セクションと同じパターン。
function _ensureGanttSectionVisible() {
    const sec = $('#habit-gantt-section');
    if (sec) sec.style.display = 'block';
}
// ヒートマップセクションが表示状態になったら、ガントもセットで表示する
const _heatmapSection = $('#habit-heatmap-section');
if (_heatmapSection) {
    const observer = new MutationObserver(() => {
        if (_heatmapSection.style.display !== 'none') _ensureGanttSectionVisible();
    });
    observer.observe(_heatmapSection, { attributes: true, attributeFilter: ['style'] });
}


// ===========================================================
// チュートリアル（ヘルプボタン）
// ===========================================================

const TUTORIAL_SLIDES = [
    {
        title: 'はじめに',
        target: null,
        body: `<p>このアプリは <b>4つのタブ</b>でできています。</p>
        <div class="tut-grid">
            <div class="tut-card"><b>💬 チャット</b><br><small>AI に何でも頼める</small></div>
            <div class="tut-card"><b>📰 情報</b><br><small>天気・ニュース・ストック</small></div>
            <div class="tut-card"><b>📒 ログ</b><br><small>習慣・ライフログ・Fitbit</small></div>
            <div class="tut-card"><b>📅 予定</b><br><small>MIT・カレンダー・タスク</small></div>
        </div>
        <p style="margin-top:10px;font-size:0.85rem;color:var(--text-muted);">下のナビ（チャット・情報・ログ・予定）でタブを切り替えます。</p>`
    },
    {
        title: 'チャット',
        target: 'chat',
        body: `<p>マネージャー AI に自然言語で話しかけてください。</p>
        <ul>
            <li>📨 メッセージ入力欄に書いて送信</li>
            <li>👆 メッセージ長押しでお気に入り・コレクション・翻訳保存などのアクション</li>
            <li>🌐 ヘッダーの EN トグルで英会話モード</li>
            <li>🔍 ヘッダーの検索アイコンで全メッセージ検索</li>
        </ul>`
    },
    {
        title: 'タスク開始 / タスク終了',
        target: 'chat',
        body: `<p>チャット下の <b>▶ タスク開始</b> / <b>■ タスク終了</b> でその場の作業を記録します。</p>
        <ul>
            <li>開始 → 候補から選んで「◯◯開始」と送信</li>
            <li>終了 → 開始忘れでも候補リストから選べます</li>
            <li>記録は <b>ライフログ</b> セクションに自動で残ります</li>
        </ul>`
    },
    {
        title: '+ その他メニュー',
        target: 'chat',
        body: `<p>「+ その他」に折りたたまれたクイックアクション：</p>
        <ul>
            <li>☀️ ブリーフィング（朝/夜の振り返り）</li>
            <li>💭 ゼロ秒思考</li>
            <li>📖 読書 / 📝 勉強</li>
            <li>🔨 タスク分解</li>
            <li>🧹 仕事・プライベート整理</li>
            <li>🧘 瞑想タイマー</li>
        </ul>`
    },
    {
        title: '天気・ニュース',
        target: 'info',
        body: `<p>情報タブには日々の情報がまとまっています。</p>
        <ul>
            <li>🌤️ 天気（岡山 北部 / 南部 を切替）</li>
            <li>🔗 Yahoo!天気の詳細ページへリンク</li>
            <li>📰 Yahoo!ニュース 主要トピック</li>
        </ul>`
    },
    {
        title: 'ストックリンク',
        target: 'info',
        body: `<p>気になった URL（記事・YouTube・レシピ・地図・本）をチャットに貼ると自動分類で保存。</p>
        <p style="margin-top:8px;">⭐ お気に入り設定や、長押しで詳細編集できます。</p>`
    },
    {
        title: '習慣トラッカー',
        target: 'log',
        body: `<p>毎日の習慣をワンタップでチェック。継続状況を 2 種類の図で可視化します。</p>
        <ul>
            <li>⭕ チェックで完了記録（Google Tasks「習慣」リストと同期）</li>
            <li>⏰「+いつ」を設定すると指定時刻にマネージャーが声をかけます</li>
            <li>⠿ 長押しで並び替え可能</li>
            <li>📊 過去 28 日のヒートマップ ＋ 過去 90 日のガントチャート（習慣ごとに色分け）</li>
        </ul>`
    },
    {
        title: 'ライフログ',
        target: 'log',
        body: `<p>1 日の活動記録。<b>行をタップ</b>で編集モーダルが開きます。</p>
        <div class="tut-callout">
            開始時刻: <code>09:00</code><br>
            終了時刻: <code>10:30</code>（空欄なら ▶ 実行中扱い）<br>
            内容: 自由記入
        </div>
        <p style="margin-top:8px;font-size:0.85rem;color:var(--text-muted);">時刻・マーク・内容は列が揃って表示されます。</p>`
    },
    {
        title: 'Fitbit データ',
        target: 'log',
        body: `<p>14 日分のヘルスデータを<b>自動取得・キャッシュ</b>。</p>
        <ul>
            <li>📈 指標を選んで折れ線グラフ＋ 7 日移動平均で推移を確認</li>
            <li>🔄 23 時に当日データを事前取得</li>
            <li>「更新」で強制再取得も可能</li>
        </ul>`
    },
    {
        title: '英語フレーズ帳 + クイズ',
        target: 'log',
        body: `<p>気になった英語表現をワンタップで保存し、<b>クイズで定着</b>。</p>
        <ul>
            <li>📚 メッセージ長押し → 翻訳して保存</li>
            <li>🎯「クイズ」ボタンで出題（<b>正解率の低い問題から優先</b>）</li>
            <li>📊 解答実績は次回出題に反映</li>
        </ul>`
    },
    {
        title: '今日の日記 / デイリーサマリー',
        target: 'log',
        body: `<p>1 日の記録を統合した日報を表示・編集できます。</p>
        <ul>
            <li>📔 <b>今日の日記</b>: AI が振り返り → 「編集」ボタンで自分で書き換え可能（Obsidian 反映）</li>
            <li>📅 <b>デイリーサマリー</b>: 予定・天気・会話・ライフログ・位置・Fitbit を一画面に集約</li>
            <li>❓ AI が判断に迷う点は<b>質問キュー</b>に保存され、後で答えると確定します</li>
        </ul>`
    },
    {
        title: '今日の MIT',
        target: 'schedule',
        body: `<p>その日に必ず終わらせたい <b>3 つのタスク</b>を設定。</p>
        <ul>
            <li>「設定」ボタンですぐ入力画面が開きます</li>
            <li>チャット上部にバナーで常時表示</li>
            <li>達成チェックは Obsidian に反映</li>
        </ul>`
    },
    {
        title: 'タスク管理（仕事・プライベート）',
        target: 'schedule',
        body: `<p>Google Tasks と連携。</p>
        <ul>
            <li>📋 古い順（作成順）に並びます</li>
            <li>⠿ 長押し → ドラッグで並び替え</li>
            <li>👉 横にドラッグするとサブタスク化</li>
            <li>📅 期限はカレンダーピッカー＆「今日 / 明日 / 1週間後」のクイック設定</li>
        </ul>`
    },
    {
        title: 'ヘッダーのアイコン',
        target: null,
        body: `<p>ヘッダー右側のアイコンの意味：</p>
        <ul>
            <li>⭐ お気に入り一覧（一覧から直接解除可）</li>
            <li>🏷 コレクション（ラベル別の閲覧）</li>
            <li>🔍 メッセージ検索</li>
            <li>🔄 アプリ更新（PWA キャッシュ更新）</li>
            <li>❓ このヘルプを再表示</li>
            <li>🗑 チャット履歴リセット</li>
            <li>⚙ 通知などの設定</li>
        </ul>`
    },
];

let _tutorialIdx = 0;

window.openTutorial = () => {
    const modal = $('#tutorial-modal');
    if (!modal) return;
    const saved = parseInt(localStorage.getItem('mng_tutorial_last_slide') || '0', 10);
    _tutorialIdx = isNaN(saved) ? 0 : Math.max(0, Math.min(saved, TUTORIAL_SLIDES.length - 1));
    modal.classList.remove('hidden');
    renderTutorialSlide();
};

window.closeTutorial = () => {
    $('#tutorial-modal')?.classList.add('hidden');
    localStorage.setItem('mng_tutorial_last_slide', String(_tutorialIdx));
};

window.tutorialPrev = () => {
    if (_tutorialIdx > 0) {
        _tutorialIdx--;
        renderTutorialSlide();
    }
};

window.tutorialNext = () => {
    if (_tutorialIdx < TUTORIAL_SLIDES.length - 1) {
        _tutorialIdx++;
        renderTutorialSlide();
    } else {
        closeTutorial();
    }
};

function renderTutorialSlide() {
    const slide = TUTORIAL_SLIDES[_tutorialIdx];
    if (!slide) return;
    const titleEl = $('#tutorial-title');
    const contentEl = $('#tutorial-content');
    const progressEl = $('#tutorial-progress');
    const prevBtn = $('#tutorial-prev');
    const nextBtn = $('#tutorial-next');
    if (titleEl) titleEl.textContent = slide.title;
    if (contentEl) contentEl.innerHTML = slide.body;
    if (progressEl) progressEl.textContent = `${_tutorialIdx + 1} / ${TUTORIAL_SLIDES.length}`;
    if (prevBtn) prevBtn.disabled = _tutorialIdx === 0;
    if (nextBtn) nextBtn.textContent = (_tutorialIdx === TUTORIAL_SLIDES.length - 1) ? '完了' : '次へ →';
    // 対象タブを自動切替（チャット中はそのまま）
    if (slide.target) {
        try { switchTab(slide.target); } catch {}
    }
    localStorage.setItem('mng_tutorial_last_slide', String(_tutorialIdx));
}

// =================================================================
// Investment Tab — 投資サポート機能
// =================================================================

let _investHistoryCategory = 'audit';
let _investBusy = false;

// シンプルなMarkdown→HTMLレンダラ（見出し・太字・斜体・リスト・テーブル・コード・リンク・水平線）
function renderInvestmentMarkdown(md) {
    if (!md) return '';
    const esc = (t) => escapeHtml(t);
    // テーブルを先に処理して、後段の処理で壊さないようにプレースホルダ化
    const tables = [];
    md = md.replace(/((?:^\|.*\|\s*$\n?)+)/gm, (block) => {
        const lines = block.trim().split('\n');
        if (lines.length < 2) return block;
        const headerCells = lines[0].split('|').slice(1, -1).map(s => s.trim());
        const sepOk = /^\|?\s*:?-{2,}/.test(lines[1]);
        if (!sepOk) return block;
        const rows = lines.slice(2).map(l => l.split('|').slice(1, -1).map(s => s.trim()));
        let html = '<table><thead><tr>';
        headerCells.forEach(h => { html += `<th>${esc(h)}</th>`; });
        html += '</tr></thead><tbody>';
        rows.forEach(r => {
            html += '<tr>';
            r.forEach(c => { html += `<td>${esc(c)}</td>`; });
            html += '</tr>';
        });
        html += '</tbody></table>';
        const placeholder = `\n@@TBL${tables.length}@@\n`;
        tables.push(html);
        return placeholder;
    });

    // コードブロック
    const codeBlocks = [];
    md = md.replace(/```([a-z]*)\n([\s\S]*?)```/g, (_, lang, code) => {
        const ph = `@@CODE${codeBlocks.length}@@`;
        codeBlocks.push(`<pre style="background:rgba(0,0,0,0.3);padding:10px;border-radius:6px;overflow-x:auto;font-size:0.78rem;"><code>${esc(code)}</code></pre>`);
        return ph;
    });

    function inlineFormat(t) {
        let s = esc(t);
        // インラインコード
        s = s.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`);
        // リンク [label](url)
        s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, label, url) => `<a href="${url}" target="_blank" rel="noopener">${label}</a>`);
        // 自動URLリンク
        s = s.replace(/(^|[\s(])((?:https?:\/\/)[^\s)]+)/g, (_, lead, url) => `${lead}<a href="${url}" target="_blank" rel="noopener">${url}</a>`);
        // 太字
        s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
        // 斜体
        s = s.replace(/(^|[^*])\*([^*]+)\*([^*]|$)/g, '$1<em>$2</em>$3');
        return s;
    }

    // 行ごとに処理
    const lines = md.split('\n');
    const out = [];
    let inUl = false, inOl = false;
    const closeLists = () => {
        if (inUl) { out.push('</ul>'); inUl = false; }
        if (inOl) { out.push('</ol>'); inOl = false; }
    };

    for (let i = 0; i < lines.length; i++) {
        const line = lines[i];

        if (/^@@TBL\d+@@$/.test(line.trim())) { closeLists(); out.push(line); continue; }
        if (/^@@CODE\d+@@$/.test(line.trim())) { closeLists(); out.push(line); continue; }

        // 区切り線
        if (/^\s*---\s*$/.test(line)) { closeLists(); out.push('<hr>'); continue; }
        // 見出し
        const h = line.match(/^(#{1,4})\s+(.+)$/);
        if (h) {
            closeLists();
            const lvl = h[1].length;
            out.push(`<h${lvl}>${inlineFormat(h[2])}</h${lvl}>`);
            continue;
        }
        // ブロッククォート
        const bq = line.match(/^>\s?(.*)$/);
        if (bq) {
            closeLists();
            out.push(`<blockquote>${inlineFormat(bq[1])}</blockquote>`);
            continue;
        }
        // 番号付きリスト
        const ol = line.match(/^\s*\d+\.\s+(.*)$/);
        if (ol) {
            if (!inOl) { closeLists(); out.push('<ol>'); inOl = true; }
            out.push(`<li>${inlineFormat(ol[1])}</li>`);
            continue;
        }
        // 箇条書き
        const ul = line.match(/^\s*[-*]\s+(.*)$/);
        if (ul) {
            if (!inUl) { closeLists(); out.push('<ul>'); inUl = true; }
            out.push(`<li>${inlineFormat(ul[1])}</li>`);
            continue;
        }
        // 空行
        if (!line.trim()) {
            closeLists();
            continue;
        }
        // 通常段落
        closeLists();
        out.push(`<p style="margin:6px 0;">${inlineFormat(line)}</p>`);
    }
    closeLists();
    let html = out.join('\n');

    // テーブル/コードブロックのプレースホルダを戻す
    tables.forEach((t, idx) => { html = html.replace(`@@TBL${idx}@@`, t); });
    codeBlocks.forEach((c, idx) => { html = html.replace(`@@CODE${idx}@@`, c); });

    return html;
}

window.openInvestmentResultModal = (title, markdown) => {
    const modal = $('#invest-result-modal');
    const titleEl = $('#invest-result-title');
    const bodyEl = $('#invest-result-body');
    if (!modal || !bodyEl) return;
    if (titleEl) titleEl.textContent = title || '結果';
    bodyEl.innerHTML = renderInvestmentMarkdown(markdown || '');
    modal.classList.remove('hidden');
};

window.closeInvestmentResultModal = () => {
    $('#invest-result-modal')?.classList.add('hidden');
};

function _setInvestBusy(busy) {
    _investBusy = busy;
    document.querySelectorAll('#tab-invest .chip-btn, #tab-invest .modal-btn').forEach(b => {
        b.disabled = busy;
        b.style.opacity = busy ? '0.5' : '1';
    });
}

async function _callInvestmentApi(path, body, label) {
    if (_investBusy) {
        showToast('処理中です。完了まで待ってください', true);
        return null;
    }
    _setInvestBusy(true);
    showToast(`${label}を実行中...`);
    try {
        const opts = { method: 'POST' };
        if (body !== null && body !== undefined) opts.body = JSON.stringify(body);
        const data = await apiFetch(path, opts);
        if (!data || data.ok === false) {
            const err = (data && (data.error || data.detail)) || '失敗しました';
            showToast(`${label}: ${err}`, true);
            return data;
        }
        showToast(`${label} 完了`);
        return data;
    } catch (e) {
        showToast(`${label}失敗: ${e.message || e}`, true);
        return null;
    } finally {
        _setInvestBusy(false);
    }
}

window.runMarketSentiment = async () => {
    const sentinelEl = $('#invest-sentiment-result');
    if (sentinelEl) sentinelEl.textContent = '取得中...';
    const data = await _callInvestmentApi('/api/investment/sentiment', null, '地合い分析');
    if (data && data.ok && data.report) {
        if (sentinelEl) sentinelEl.innerHTML = renderInvestmentMarkdown(data.report);
        window.openInvestmentResultModal('🌍 市場の地合い', data.report);
        window.loadInvestmentHistory();
    } else if (sentinelEl) {
        sentinelEl.textContent = '取得に失敗しました。再度お試しください。';
    }
};

function _getTickerInput() {
    const t = $('#invest-ticker-input')?.value?.trim();
    if (!t) {
        showToast('ティッカーを入力してください', true);
        return null;
    }
    return t;
}

window.runStockSnapshot = async () => {
    const ticker = _getTickerInput();
    if (!ticker) return;
    const data = await _callInvestmentApi('/api/investment/snapshot', { ticker }, `${ticker} スナップショット`);
    if (data && data.ok) {
        window.openInvestmentResultModal(`📷 ${data.ticker} スナップショット`, data.report);
        window.loadInvestmentHistory();
    }
};

window.runStockAudit = async () => {
    const ticker = _getTickerInput();
    if (!ticker) return;
    const data = await _callInvestmentApi('/api/investment/audit', { ticker }, `${ticker} 憲法審査`);
    if (data && data.ok) {
        window.openInvestmentResultModal(`🎯 ${data.ticker} 投資憲法審査`, data.audit);
        window.loadInvestmentHistory();
    }
};

window.runEarningsSchedule = async () => {
    const ticker = _getTickerInput();
    if (!ticker) return;
    const data = await _callInvestmentApi('/api/investment/earnings_schedule', { ticker, register_calendar: true }, `${ticker} 決算予定`);
    if (data && data.ok) {
        const d = data.data || {};
        const lines = [
            `# 📅 ${d.company_name || data.ticker} (${data.ticker}) 決算予定`,
            '',
            `- 次回決算日: **${d.next_earnings_date || '不明'}**`,
            `- 発表時間帯: ${d.earnings_time || '不明'}`,
            `- 会計期間: ${d.fiscal_period || '不明'}`,
            `- 信頼度: ${d.confidence || 'N/A'}`,
            `- 出典: ${d.source || 'N/A'}`,
            '',
        ];
        const events = d.related_events || [];
        if (events.length) {
            lines.push('## 🗓 関連イベント');
            events.forEach(ev => lines.push(`- ${ev.title}: ${ev.date}`));
            lines.push('');
        }
        const reg = data.registered || [];
        if (reg.length) {
            lines.push('## ✅ カレンダー登録結果');
            reg.forEach(r => {
                if (r.error) lines.push(`- ❌ ${r.summary}: ${r.error}`);
                else lines.push(`- ✅ ${r.summary} (${r.date}): ${r.result || ''}`);
            });
        }
        window.openInvestmentResultModal(`📅 ${data.ticker} 決算予定`, lines.join('\n'));
    }
};

window.runEarningsDocuments = async () => {
    const ticker = _getTickerInput();
    if (!ticker) return;
    const data = await _callInvestmentApi('/api/investment/earnings_documents', { ticker }, `${ticker} 決算資料`);
    if (data && data.ok) {
        window.openInvestmentResultModal(`📑 ${data.ticker} 決算関連資料`, data.report);
        window.loadInvestmentHistory();
    }
};

window.runCEOCheck = async () => {
    const ticker = $('#invest-ceo-ticker')?.value?.trim();
    const url = $('#invest-ceo-url')?.value?.trim();
    const title = $('#invest-ceo-title')?.value?.trim();
    if (!ticker) { showToast('ティッカーを入力してください', true); return; }
    if (!url) { showToast('YouTube URLを入力してください', true); return; }
    const data = await _callInvestmentApi('/api/investment/ceo_check', { ticker, video_url: url, video_title: title }, `${ticker} CEO検証`);
    if (data && data.ok) {
        window.openInvestmentResultModal(`🎬 ${data.ticker} CEO発言クロスチェック`, data.analysis);
        window.loadInvestmentHistory();
    }
};

window.openConstitutionEditor = async () => {
    const modal = $('#invest-constitution-modal');
    const textarea = $('#invest-constitution-text');
    if (!modal || !textarea) return;
    textarea.value = '読み込み中...';
    modal.classList.remove('hidden');
    try {
        const data = await apiFetch('/api/investment/constitution');
        if (data && data.ok) {
            textarea.value = data.content || '';
        } else {
            textarea.value = '';
            showToast('憲法が未作成です。サンプルにリセットで作成できます。', true);
        }
    } catch (e) {
        textarea.value = '';
        showToast('読み込み失敗: ' + (e.message || e), true);
    }
};

window.closeConstitutionEditor = () => {
    $('#invest-constitution-modal')?.classList.add('hidden');
};

window.saveConstitution = async () => {
    const text = $('#invest-constitution-text')?.value || '';
    if (!text.trim()) { showToast('内容が空です', true); return; }
    try {
        const data = await apiFetch('/api/investment/constitution', {
            method: 'POST',
            body: JSON.stringify({ content: text }),
        });
        if (data && data.ok) {
            showToast('投資憲法を保存しました');
            window.closeConstitutionEditor();
        } else {
            showToast(data?.error || '保存失敗', true);
        }
    } catch (e) {
        showToast('保存失敗: ' + (e.message || e), true);
    }
};

window.resetConstitution = async () => {
    if (!confirm('投資憲法をサンプルで上書きしますか？現在の内容は失われます。')) return;
    try {
        const data = await apiFetch('/api/investment/constitution/init', {
            method: 'POST',
            body: JSON.stringify({ force: true }),
        });
        if (data && data.ok) {
            $('#invest-constitution-text').value = data.content || '';
            showToast('サンプルで上書きしました');
        } else {
            showToast(data?.error || 'リセット失敗', true);
        }
    } catch (e) {
        showToast('リセット失敗: ' + (e.message || e), true);
    }
};

window.switchInvestHistory = (cat) => {
    _investHistoryCategory = cat;
    document.querySelectorAll('#tab-invest .invest-history-tab').forEach(b => {
        b.classList.toggle('active', b.dataset.cat === cat);
    });
    window.loadInvestmentHistory();
};

window.loadInvestmentHistory = async () => {
    const listEl = $('#invest-history-list');
    if (!listEl) return;
    listEl.innerHTML = '<div style="padding:12px;font-size:0.85rem;color:var(--text-muted);">読み込み中...</div>';
    try {
        const data = await apiFetch(`/api/investment/history/${encodeURIComponent(_investHistoryCategory)}?limit=20`);
        if (!data || !data.ok) {
            listEl.innerHTML = '<div style="padding:12px;font-size:0.85rem;color:var(--text-muted);">履歴の取得に失敗しました</div>';
            return;
        }
        const items = data.items || [];
        if (!items.length) {
            listEl.innerHTML = '<div style="padding:12px;font-size:0.85rem;color:var(--text-muted);">まだ履歴がありません</div>';
            return;
        }
        const cat = _investHistoryCategory;
        listEl.innerHTML = items.map(it => {
            const modified = it.modifiedTime ? new Date(it.modifiedTime).toLocaleString('ja-JP', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '';
            const safeName = escapeHtml(it.name || '(名前なし)');
            const safeId = escapeHtml(it.id || '');
            return `<div class="invest-history-item" onclick="openInvestHistoryItem('${cat}','${safeId}','${safeName.replace(/'/g, '&#39;')}')"><span class="name">${safeName}</span><span class="meta">${modified}</span></div>`;
        }).join('');
    } catch (e) {
        listEl.innerHTML = `<div style="padding:12px;font-size:0.85rem;color:var(--text-muted);">エラー: ${escapeHtml(e.message || String(e))}</div>`;
    }
};

window.openInvestHistoryItem = async (category, fileId, name) => {
    showToast(`${name} を読み込み中...`);
    try {
        const data = await apiFetch(`/api/investment/history/${encodeURIComponent(category)}/${encodeURIComponent(fileId)}`);
        if (!data || !data.ok) {
            showToast(data?.error || '読み込み失敗', true);
            return;
        }
        window.openInvestmentResultModal(name, data.content);
    } catch (e) {
        showToast('読み込み失敗: ' + (e.message || e), true);
    }
};