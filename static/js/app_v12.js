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
            obTaskEl.innerHTML = data.tasks.length ? data.tasks.map(t => `
                <div class="list-item">
                    <div class="li-text" style="text-decoration: ${t.done ? 'line-through' : 'none'}">${escapeHtml(t.text)}</div>
                </div>
            `).join('') : '<div class="loading-placeholder">ログはありません</div>';
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

function renderTaskGroup(container, tasks, listName) {
    if (!container) return;
    container.innerHTML = tasks && tasks.length ? tasks.map(t => `
        <div class="list-item">
            <div class="checkbox-custom" onclick="toggleGoogleTask('${t.id}', false, '${listName}')"></div>
            <div class="li-text">${escapeHtml(t.title)}</div>
        </div>
    `).join('') : '<div class="loading-placeholder">未完了のタスクはありません</div>';
}

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
    } catch (e) { console.error("StockedLinks fetch error", e); }
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
        const data = await apiFetch('/api/history?limit=20');
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
            return;
        }
        container.innerHTML = data.habits.map(h => {
            const isDone = data.today_done.includes(h.id);
            return `
                <div class="habit-item ${isDone ? 'done' : ''}" id="habit-item-${h.id}">
                    <button class="habit-check-btn" onclick="completeHabit('${h.name}', '${h.id}')" ${isDone ? 'disabled' : ''}>✔</button>
                    <div class="habit-name">${escapeHtml(h.name)}</div>
                </div>
            `;
        }).join('');
    } catch (e) { }
}
window.completeHabit = async (habitName, hId) => {
    try {
        const item = $(`#habit-item-${hId}`);
        if(item) item.classList.add('done');
        showToast(`「${habitName}」を完了しました！🎉`);
        await apiFetch('/api/habits/complete', { method: 'POST', body: JSON.stringify({ habit_name: habitName }) });
    } catch { showToast('失敗しました', true); }
};