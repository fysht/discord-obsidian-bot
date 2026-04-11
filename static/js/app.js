/* ==========================================
   Manager AI — Application Logic v2
   ========================================== */

const API_BASE = '';
let apiKey = localStorage.getItem('secretary_api_key') || '';

// ========== Utility ==========
const $ = sel => document.querySelector(sel);
const $$ = sel => document.querySelectorAll(sel);

function showScreen(id) {
  $$('.screen').forEach(s => s.classList.remove('active'));
  $(`#${id}`).classList.add('active');
}

function formatTime(isoStr) {
  if (!isoStr) return '';
  const d = new Date(isoStr);
  return d.toLocaleTimeString('ja-JP', { timeZone: 'Asia/Tokyo', hour: '2-digit', minute: '2-digit' });
}

function escapeHtml(text) {
  const el = document.createElement('div');
  el.textContent = text;
  return el.innerHTML;
}

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

// PC: Enter で送信 / Shift+Enter で改行
// モバイル: Enter で改行 (送信はボタン)
messageInput.addEventListener('keydown', (e) => {
  if (e.isComposing) return; // IME変換中は無視（二重送信防止）
  if (window.innerWidth >= 768 && e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    $('#chat-form').dispatchEvent(new Event('submit'));
  }
});

// メッセージ表示
function addMessage(role, content, timestamp) {
  const welcome = chatMessages.querySelector('.chat-welcome');
  if (welcome) welcome.remove();

  const div = document.createElement('div');
  div.className = `message ${role}`;
  const timeStr = timestamp ? formatTime(timestamp) : formatTime(new Date().toISOString());

  if (role === 'assistant') {
    div.innerHTML = `
      <div class="msg-avatar"><img src="/static/icons/avatar.png" alt="AI"></div>
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

function formatMessageText(text) {
  let html = escapeHtml(text);
  // [BUTTON:add_task:タスク名] をインラインボタンに変換
  html = html.replace(/\[BUTTON:add_task:(.+?)\]/g, (_, taskName) => {
    return `<button class="inline-action-btn" onclick="directAddTask('${escapeHtml(taskName)}')">＋「${escapeHtml(taskName)}」をタスクに追加</button>`;
  });
  return html;
}

// タスク直接追加
window.directAddTask = async function(taskName) {
  try {
    await apiFetch('/api/task_action', {
      method: 'POST',
      body: JSON.stringify({ action: 'create', new_text: taskName }),
    });
    showToast(`「${taskName}」を追加しました`);
    if ($('#dashboard-tab').classList.contains('active')) loadDashboard();
  } catch {
    showToast('追加に失敗しました', true);
  }
};

// タイピングインジケーター
function showTyping() {
  const div = document.createElement('div');
  div.className = 'message assistant';
  div.id = 'typing-msg';
  div.innerHTML = `
    <div class="msg-avatar"><img src="/static/icons/avatar.png" alt="AI"></div>
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

// メッセージ送信
$('#chat-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (sendBtn.disabled) return;

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
  } catch {
    hideTyping();
    addMessage('assistant', 'ごめん、ちょっと通信エラーみたいだ。もう一回試してみて。');
  } finally {
    sendBtn.disabled = false;
    messageInput.focus();
  }
});

// 履歴読み込み
async function loadHistory() {
  try {
    const data = await apiFetch('/api/history?limit=30');
    chatMessages.innerHTML = '';
    if (data.messages && data.messages.length > 0) {
      data.messages.forEach(m => addMessage(m.role, m.content, m.timestamp));
    } else {
      chatMessages.innerHTML = `
        <div class="chat-welcome">
          <div class="welcome-inner">
            <div class="welcome-avatar"><img src="/static/icons/avatar.png" alt=""></div>
            <h2>こんにちは！</h2>
            <p>何でも話しかけてね。予定確認、タスク管理、メモ…何でもお任せ！</p>
          </div>
        </div>`;
    }
  } catch (err) {
    console.error('履歴の読み込みに失敗:', err);
  }
}

// クイックアクション送信
window.sendActionCommand = function(cmd) {
  messageInput.value = cmd;
  $('#chat-form').dispatchEvent(new Event('submit'));
};

// ========== Dashboard ==========
async function loadDashboard() {
  // 日付ラベル
  const dateLabel = $('#dash-date-label');
  if (dateLabel) {
    const now = new Date();
    const days = ['日', '月', '火', '水', '木', '金', '土'];
    dateLabel.textContent = `${now.getMonth() + 1}月${now.getDate()}日 (${days[now.getDay()]})`;
  }

  try {
    const data = await apiFetch('/api/dashboard');

    // Google Calendar
    const gcEl = $('#dash-google-calendar');
    if (gcEl) {
      if (data.g_calendar && data.g_calendar.length > 0) {
        gcEl.innerHTML = data.g_calendar.map(l => {
          const text = l.replace(/^- /, '');
          return `<div class="cal-event"><span class="cal-event-dot"></span><span class="cal-event-text">${escapeHtml(text)}</span></div>`;
        }).join('');
      } else {
        gcEl.innerHTML = '<p class="dash-empty">今日の予定はありません</p>';
      }
    }

    // Google Tasks
    const gtEl = $('#dash-google-tasks');
    if (gtEl) {
      if (data.g_tasks && data.g_tasks.length > 0) {
        gtEl.innerHTML = data.g_tasks.map(l => {
          const text = l.replace(/^- /, '');
          return `<div class="gtask-item"><span class="gtask-dot"></span><span>${escapeHtml(text)}</span></div>`;
        }).join('');
      } else {
        gtEl.innerHTML = '<p class="dash-empty">未完了のタスクはありません</p>';
      }
    }

    // Obsidian Tasks
    const tasksEl = $('#dash-tasks');
    if (data.tasks && data.tasks.length > 0) {
      tasksEl.innerHTML = data.tasks.map(t => `
        <div class="task-item ${t.done ? 'done' : ''}" data-text="${escapeHtml(t.text)}">
          <span class="task-check" onclick="toggleTask('${escapeHtml(t.text)}')">${t.done ? '✅' : '⬜'}</span>
          <span class="task-text">${escapeHtml(t.text)}</span>
          <div class="task-actions">
            <button class="task-action-btn" onclick="editTask('${escapeHtml(t.text)}')" title="編集">✏️</button>
            <button class="task-action-btn danger" onclick="deleteTask('${escapeHtml(t.text)}')" title="削除">🗑️</button>
          </div>
        </div>
      `).join('');
    } else {
      tasksEl.innerHTML = '<p class="dash-empty">今日のタスクはまだありません</p>';
    }

    // Alter Log
    const alterEl = $('#dash-alter-log');
    if (data.alter_log) {
      alterEl.innerHTML = `<div class="alter-log-text">${escapeHtml(data.alter_log)}</div>`;
    } else {
      alterEl.innerHTML = '<p class="dash-empty">今日のAlter Logはまだ生成されていません</p>';
    }
  } catch (err) {
    console.error('ダッシュボードの読み込みに失敗:', err);
  }
}

// タスク操作
async function toggleTask(text) {
  try {
    await apiFetch('/api/task_action', { method: 'POST', body: JSON.stringify({ action: 'toggle', old_text: text }) });
    loadDashboard();
  } catch { showToast('更新に失敗しました', true); }
}

async function editTask(text) {
  const newText = prompt('タスクを編集', text);
  if (newText && newText !== text) {
    try {
      await apiFetch('/api/task_action', { method: 'POST', body: JSON.stringify({ action: 'update', old_text: text, new_text: newText }) });
      loadDashboard();
    } catch { showToast('更新に失敗しました', true); }
  }
}

async function deleteTask(text) {
  if (confirm('このタスクを削除しますか？')) {
    try {
      await apiFetch('/api/task_action', { method: 'POST', body: JSON.stringify({ action: 'delete', old_text: text }) });
      loadDashboard();
    } catch { showToast('削除に失敗しました', true); }
  }
}

// ========== Toast通知 ==========
function showToast(message, isError = false) {
  // 既存のトーストがあれば削除
  const existing = $('#toast-notification');
  if (existing) existing.remove();

  const toast = document.createElement('div');
  toast.id = 'toast-notification';
  toast.textContent = message;
  toast.style.cssText = `
    position: fixed; bottom: 80px; left: 50%; transform: translateX(-50%) translateY(10px);
    padding: 10px 20px; border-radius: 12px; font-size: 0.85rem; font-weight: 500;
    font-family: var(--font); z-index: 1000; opacity: 0;
    transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
    background: ${isError ? 'rgba(248, 113, 113, 0.2)' : 'rgba(52, 211, 153, 0.15)'};
    color: ${isError ? '#f87171' : '#34d399'};
    border: 1px solid ${isError ? 'rgba(248, 113, 113, 0.3)' : 'rgba(52, 211, 153, 0.25)'};
    backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px);
  `;
  document.body.appendChild(toast);

  requestAnimationFrame(() => {
    toast.style.opacity = '1';
    toast.style.transform = 'translateX(-50%) translateY(0)';
  });

  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateX(-50%) translateY(10px)';
    setTimeout(() => toast.remove(), 300);
  }, 2500);
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
      const parts = [inText, inUrl].filter(Boolean);
      messageInput.value = parts.join(' \n');
      messageInput.focus();
      window.history.replaceState({}, document.title, '/');
    }
  } else {
    showScreen('login-screen');
  }
});
