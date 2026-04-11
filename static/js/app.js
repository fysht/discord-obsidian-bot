/* ==========================================
   Secretary AI — Main Application Logic
   ========================================== */

const API_BASE = '';
let apiKey = localStorage.getItem('secretary_api_key') || '';

// ========== Utility ==========
function $(sel) { return document.querySelector(sel); }
function $$(sel) { return document.querySelectorAll(sel); }

function showScreen(id) {
  $$('.screen').forEach(s => s.classList.remove('active'));
  $(`#${id}`).classList.add('active');
}

function formatTime(isoStr) {
  if (!isoStr) return '';
  const d = new Date(isoStr);
  return d.toLocaleTimeString('ja-JP', { timeZone: 'Asia/Tokyo', hour: '2-digit', minute: '2-digit' });
}

async function apiFetch(path, options = {}) {
  const headers = { 'Content-Type': 'application/json', 'X-Api-Key': apiKey, ...(options.headers || {}) };
  const res = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (res.status === 401) {
    localStorage.removeItem('secretary_api_key');
    apiKey = '';
    showScreen('login-screen');
    throw new Error('認証エラー');
  }
  if (!res.ok) throw new Error(`API Error: ${res.status}`);
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

// ========== Tab Navigation ==========
$$('.nav-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    $$('.nav-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    $$('.tab-content').forEach(t => t.classList.remove('active'));
    $(`#${btn.dataset.tab}-tab`).classList.add('active');

    if (btn.dataset.tab === 'dashboard') loadDashboard();
  });
});

// ========== Chat ==========
const chatMessages = $('#chat-messages');
const messageInput = $('#message-input');
const sendBtn = $('#send-btn');

// テキストエリアの自動リサイズ
messageInput.addEventListener('input', () => {
  messageInput.style.height = 'auto';
  messageInput.style.height = Math.min(messageInput.scrollHeight, 120) + 'px';
});

// Enter で送信 (PCの場合のみ。スマホの場合は改行)
messageInput.addEventListener('keydown', (e) => {
  // 画面幅が768px以上（PC/タブレット）の場合のみ、Enterで送信する
  if (window.innerWidth >= 768) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      $('#chat-form').dispatchEvent(new Event('submit'));
    }
  }
});

function addMessage(role, content, timestamp) {
  // ウェルカムメッセージがあれば削除
  const welcome = chatMessages.querySelector('.chat-welcome');
  if (welcome) welcome.remove();

  const div = document.createElement('div');
  div.className = `message ${role}`;

  const timeStr = timestamp ? formatTime(timestamp) : formatTime(new Date().toISOString());

  if (role === 'assistant') {
    div.innerHTML = `
      <div class="msg-avatar"><img src="/static/icons/icon-192.png" alt="Avatar" style="width:100%;height:100%;object-fit:cover;border-radius:50%;"></div>
      <div>
        <div class="msg-bubble">${formatMessageText(content)}</div>
        <div class="msg-time">${timeStr}</div>
      </div>`;
  } else {
    div.innerHTML = `
      <div>
        <div class="msg-bubble">${formatMessageText(content)}</div>
        <div class="msg-time">${timeStr}</div>
      </div>`;
  }

  chatMessages.appendChild(div);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function formatMessageText(text) {
  let html = escapeHtml(text);
  // [BUTTON:add_task:タスク名] のパース
  html = html.replace(/\[BUTTON:add_task:(.+?)\]/g, (match, taskName) => {
    return `<button class="inline-action-btn" onclick="directAddTask('${escapeHtml(taskName)}')">＋「${escapeHtml(taskName)}」をタスクに追加</button>`;
  });
  return html;
}

window.directAddTask = async function(taskName) {
  try {
    await apiFetch('/api/task_action', { method: 'POST', body: JSON.stringify({ action: 'create', new_text: taskName }) });
    alert(`「${taskName}」をタスクに追加しました！`);
    if ($('#dashboard-tab').classList.contains('active')) loadDashboard();
  } catch (e) {
    alert('追加エラー');
  }
}

function showTyping() {
  const div = document.createElement('div');
  div.className = 'message assistant';
  div.id = 'typing-msg';
  div.innerHTML = `
    <div class="msg-avatar"><img src="/static/icons/icon-192.png" alt="Avatar" style="width:100%;height:100%;object-fit:cover;border-radius:50%;"></div>
    <div class="typing-indicator">
      <div class="typing-dot"></div>
      <div class="typing-dot"></div>
      <div class="typing-dot"></div>
    </div>`;
  chatMessages.appendChild(div);
  chatMessages.scrollTop = chatMessages.scrollHeight;
}

function hideTyping() {
  const el = $('#typing-msg');
  if (el) el.remove();
}

$('#chat-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const msg = messageInput.value.trim();
  if (!msg) return;

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
    addMessage('assistant', 'ごめん、ちょっと通信エラーみたいだ。');
  } finally {
    sendBtn.disabled = false;
    messageInput.focus();
  }
});

async function loadHistory() {
  try {
    const data = await apiFetch('/api/history?limit=30');
    // 既存メッセージをクリア
    chatMessages.innerHTML = '';
    if (data.messages && data.messages.length > 0) {
      data.messages.forEach(m => addMessage(m.role, m.content, m.timestamp));
    } else {
      chatMessages.innerHTML = '<div class="chat-welcome"><p>俺たちの会話、ここから始めようぜ。</p></div>';
    }
  } catch (err) {
    console.error('履歴の読み込みに失敗:', err);
  }
}

// ========== Action Menu ==========
function toggleActionMenu() {
  const menu = $('#action-menu');
  if (menu.classList.contains('hidden')) {
    menu.classList.remove('hidden');
  } else {
    menu.classList.add('hidden');
  }
}

// 画面外クリックでメニューを閉じる
document.addEventListener('click', (e) => {
  const menu = $('#action-menu');
  const btn = $('#plus-btn');
  if (menu && btn && !menu.contains(e.target) && !btn.contains(e.target)) {
    menu.classList.add('hidden');
  }
});

function sendActionCommand(cmd) {
  const input = $('#message-input');
  input.value = cmd;
  $('#action-menu').classList.add('hidden');
  $('#chat-form').dispatchEvent(new Event('submit'));
}

// ========== Dashboard ==========
async function loadDashboard() {
  try {
    const data = await apiFetch('/api/dashboard');

    // Tasks
    const tasksEl = $('#dash-tasks');
    if (data.tasks && data.tasks.length > 0) {
      tasksEl.innerHTML = data.tasks.map((t, idx) => `
        <div class="task-item ${t.done ? 'done' : ''}" data-text="${escapeHtml(t.text)}">
          <span class="task-check" style="cursor:pointer" onclick="toggleTask('${escapeHtml(t.text)}')">${t.done ? '✅' : '🔄'}</span>
          <span class="task-text" style="flex:1;">${escapeHtml(t.text)}</span>
          <div class="task-actions" style="display:flex;gap:4px;">
            <button onclick="editTask('${escapeHtml(t.text)}')" style="background:transparent;border:none;color:var(--text-secondary);cursor:pointer;">✏️</button>
            <button onclick="deleteTask('${escapeHtml(t.text)}')" style="background:transparent;border:none;color:var(--danger);cursor:pointer;">🗑️</button>
          </div>
        </div>
      `).join('');
    } else {
      tasksEl.innerHTML = '<p class="dash-empty">今日のタスクはまだありません。</p>';
    }

    // Alter Log
    const alterEl = $('#dash-alter-log');
    if (data.alter_log) {
      alterEl.innerHTML = `<p style="font-size:0.88rem;line-height:1.7;white-space:pre-wrap;">${escapeHtml(data.alter_log)}</p>`;
    } else {
      alterEl.innerHTML = '<p class="dash-empty">今日のAlter Logはまだ生成されていません。</p>';
    }
  } catch (err) {
    console.error('ダッシュボードの読み込みに失敗:', err);
  }
}

async function toggleTask(text) {
  try {
    await apiFetch('/api/task_action', { method: 'POST', body: JSON.stringify({ action: 'toggle', old_text: text }) });
    loadDashboard();
  } catch (e) { alert('更新エラー'); }
}

async function editTask(text) {
  const newText = prompt('タスクを編集', text);
  if (newText && newText !== text) {
    try {
      await apiFetch('/api/task_action', { method: 'POST', body: JSON.stringify({ action: 'update', old_text: text, new_text: newText }) });
      loadDashboard();
    } catch (e) { alert('更新エラー'); }
  }
}

async function deleteTask(text) {
  if (confirm('このタスクを削除しますか？')) {
    try {
      await apiFetch('/api/task_action', { method: 'POST', body: JSON.stringify({ action: 'delete', old_text: text }) });
      loadDashboard();
    } catch (e) { alert('削除エラー'); }
  }
}

// ========== 初期化 ==========
window.addEventListener('DOMContentLoaded', () => {
  // Service Worker登録
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/static/js/sw.js').catch(err =>
      console.error('SW registration failed:', err)
    );
  }

  // 認証済みならメイン画面へ
  if (apiKey) {
    showScreen('main-screen');
    loadHistory();
    
    // Web Share API 等からの連携パラメータ処理
    const params = new URLSearchParams(window.location.search);
    const inText = params.get('text');
    const inUrl = params.get('url');
    if (inText || inUrl) {
      let sharedQuery = [];
      if (inText) sharedQuery.push(inText);
      if (inUrl) sharedQuery.push(inUrl);
      const inputEl = document.getElementById('message-input');
      if (inputEl) {
        inputEl.value = sharedQuery.join(' \n');
        inputEl.focus();
        // 処理済みパラメータをURLから消去してクリーンに
        window.history.replaceState({}, document.title, "/");
      }
    }
  } else {
    showScreen('login-screen');
  }
});
