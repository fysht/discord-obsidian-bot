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
    const isFormData = (typeof FormData !== 'undefined') && options.body instanceof FormData;
    const baseHeaders = isFormData
        ? { 'X-Api-Key': apiKey }
        : { 'Content-Type': 'application/json', 'X-Api-Key': apiKey };
    const headers = { ...baseHeaders, ...(options.headers || {}) };
    const fetchOpts = { ...options, headers };
    delete fetchOpts._isFormData;
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
    openSettingsModal();
};

// ----- 設定モーダル（コストメーター含む） -----
window.openSettingsModal = async () => {
    let modal = $('#settings-modal');
    if (!modal) {
        const wrap = document.createElement('div');
        wrap.innerHTML = `
            <div id="settings-modal" class="modal-overlay hidden">
                <div class="modal-card" style="max-width:560px;max-height:90vh;overflow-y:auto;">
                    <h3 style="margin-top:0;">⚙️ 設定</h3>

                    <!-- コストメーター -->
                    <details open style="margin-bottom:10px;">
                        <summary style="cursor:pointer;font-weight:700;color:var(--accent);padding:6px 0;">💰 運用コスト</summary>
                        <div id="cost-meter-body" style="padding:8px 0;">
                            <div class="loading-placeholder">読み込み中…</div>
                        </div>
                    </details>

                    <!-- Gemini モデル設定 -->
                    <details style="margin-bottom:10px;">
                        <summary style="cursor:pointer;font-weight:700;color:var(--accent);padding:6px 0;">🧠 Gemini モデル選択</summary>
                        <div id="gemini-models-body" style="padding:8px 0;">
                            <div class="loading-placeholder">読み込み中…</div>
                        </div>
                    </details>

                    <!-- Gemini Gem URL -->
                    <details style="margin-bottom:10px;">
                        <summary style="cursor:pointer;font-weight:700;color:var(--accent);padding:6px 0;">🔗 Gemini Gem URL</summary>
                        <div id="gem-urls-body" style="padding:8px 0;">
                            <div class="loading-placeholder">読み込み中…</div>
                        </div>
                    </details>

                    <!-- マネージャー連絡スケジュール -->
                    <details style="margin-bottom:10px;">
                        <summary style="cursor:pointer;font-weight:700;color:var(--accent);padding:6px 0;">📅 マネージャー連絡スケジュール</summary>
                        <div id="schedules-body" style="padding:8px 0;">
                            <div class="loading-placeholder">読み込み中…</div>
                        </div>
                    </details>

                    <div class="modal-actions" style="margin-top:14px;">
                        <button class="modal-btn cancel" onclick="closeSettingsModal()">閉じる</button>
                    </div>
                </div>
            </div>
        `;
        document.body.appendChild(wrap.firstElementChild);
        modal = $('#settings-modal');
    }
    modal.classList.remove('hidden');
    loadCostMeter();
    loadGeminiModelSettings();
    loadGemUrls();
    loadScheduleSettings();
};

window.loadGemUrls = async () => {
    const body = document.getElementById('gem-urls-body');
    if (!body) return;
    body.innerHTML = '<div class="loading-placeholder">読み込み中…</div>';
    try {
        const data = await apiFetch('/api/settings/gem_urls');
        const items = (data && data.items) || [];
        if (!items.length) {
            body.innerHTML = '<div class="loading-placeholder">登録対象がありません。</div>';
            return;
        }
        body.innerHTML = items.map(it => `
            <div style="padding:6px 0;border-bottom:1px solid var(--border-glass);">
                <div style="font-size:0.82rem;margin-bottom:4px;">${escapeHtml(it.label)}</div>
                <input type="url" class="modern-input gem-url-input" data-key="${escapeHtml(it.key)}"
                    value="${escapeHtml(it.url || '')}"
                    placeholder="https://gemini.google.com/gem/xxxxxxxxxxxx"
                    style="width:100%;font-size:0.78rem;padding:6px;">
            </div>
        `).join('') + `
            <div style="font-size:0.72rem;color:var(--text-muted);margin-top:6px;">空欄で保存するとクリアされます。Gem URL を設定すると外部 Gem 起動ボタンから直接開けます。</div>
            <button class="modal-btn submit" style="width:100%;margin-top:10px;" onclick="saveGemUrls()">Gem URL を保存</button>
        `;
    } catch {
        body.innerHTML = '<div class="loading-placeholder">取得に失敗しました。</div>';
    }
};

window.saveGemUrls = async () => {
    const values = {};
    document.querySelectorAll('.gem-url-input').forEach(inp => {
        const k = inp.dataset.key;
        if (!k) return;
        values[k] = (inp.value || '').trim();
    });
    try {
        const r = await apiFetch('/api/settings/gem_urls', { method: 'POST', body: JSON.stringify({ values }) });
        if (r && r.ok) {
            showToast('Gem URL を保存しました');
            _gemUrlCache = null; // キャッシュをクリア
        } else {
            showToast('保存に失敗しました', true);
        }
    } catch (e) {
        showToast(`通信エラー: ${e.message || e}`, true);
    }
};

// Gemini モデル選択肢（バックエンド KNOWN_MODELS と対応）
const GEMINI_MODEL_OPTIONS = [
    { value: 'flash',         label: '⚡ Flash 2.5' },
    { value: 'flash-lite',    label: '💨 Flash 2.5 Lite' },
    { value: 'pro',           label: '🧠 Pro 2.5' },
    { value: 'flash-preview', label: '🔬 Flash 3 Preview' },
    { value: 'flash-lite-3',  label: '💨 Flash 3.1 Lite' },
    { value: 'pro-preview',   label: '🧪 Pro 3.1 Preview' },
];

window.loadGeminiModelSettings = async () => {
    const body = $('#gemini-models-body');
    if (!body) return;
    body.innerHTML = '<div class="loading-placeholder">読み込み中…</div>';
    try {
        const data = await apiFetch('/api/settings/gemini_models');
        if (!data || !data.ok) {
            body.innerHTML = '<div class="loading-placeholder">取得に失敗しました。</div>';
            return;
        }
        const items = data.items || [];
        const knownValues = GEMINI_MODEL_OPTIONS.map(o => o.value);
        const rows = items.map(it => {
            const isCustom = !knownValues.includes(it.value);
            const customDisplay = isCustom ? '' : 'display:none;';
            const opts = GEMINI_MODEL_OPTIONS.map(o => {
                const sel = !isCustom && it.value === o.value ? 'selected' : '';
                return `<option value="${o.value}" ${sel}>${o.label}</option>`;
            }).join('');
            const customSel = isCustom ? 'selected' : '';
            return `<div style="padding:6px 0;border-bottom:1px solid var(--border-glass);">
                <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
                    <div style="flex:1;min-width:0;">
                        <div style="font-size:0.82rem;color:var(--text-primary);">${escapeHtml(it.label)}</div>
                        <div style="font-size:0.7rem;color:var(--text-muted);">${escapeHtml(it.description || '')}</div>
                    </div>
                    <select class="modern-input gemini-model-select" data-key="${escapeHtml(it.key)}" style="font-size:0.78rem;padding:4px;width:auto;" onchange="_toggleGeminiCustomInput(this)">
                        ${opts}
                        <option value="__custom__" ${customSel}>✏️ カスタム…</option>
                    </select>
                </div>
                <input type="text" class="modern-input gemini-model-custom" data-key="${escapeHtml(it.key)}" value="${escapeHtml(isCustom ? it.value : '')}" placeholder="例: gemini-3-flash-preview" style="margin-top:4px;font-size:0.78rem;padding:4px 6px;width:100%;${customDisplay}">
            </div>`;
        }).join('');
        body.innerHTML = rows + `
            <div style="font-size:0.72rem;color:var(--text-muted);margin-top:8px;">⚡ Flash 系: 低コスト・高速 / 🧠 Pro 系: 高精度・高コスト / 🔬🧪 Preview: 最新版（API変動の可能性）</div>
            <button class="modal-btn submit" style="width:100%;margin-top:10px;" onclick="saveGeminiModelSettings()">設定を保存</button>
        `;
    } catch (e) {
        body.innerHTML = '<div class="loading-placeholder">取得に失敗しました。</div>';
    }
};

window._toggleGeminiCustomInput = (sel) => {
    const key = sel.dataset.key;
    const inp = document.querySelector(`.gemini-model-custom[data-key="${key}"]`);
    if (!inp) return;
    if (sel.value === '__custom__') {
        inp.style.display = '';
        inp.focus();
    } else {
        inp.style.display = 'none';
    }
};

window.saveGeminiModelSettings = async () => {
    const values = {};
    const knownValues = GEMINI_MODEL_OPTIONS.map(o => o.value);
    document.querySelectorAll('.gemini-model-select').forEach(sel => {
        const k = sel.dataset.key;
        if (!k) return;
        let v = sel.value;
        if (v === '__custom__') {
            const inp = document.querySelector(`.gemini-model-custom[data-key="${k}"]`);
            v = (inp?.value || '').trim();
        }
        if (knownValues.includes(v) || v.startsWith('gemini-')) {
            values[k] = v;
        }
    });
    try {
        const r = await apiFetch('/api/settings/gemini_models', { method: 'POST', body: JSON.stringify({ values }) });
        if (r && r.ok) {
            showToast(`Geminiモデル設定を保存しました（${r.saved} 件）`);
        } else {
            showToast('保存に失敗しました', true);
        }
    } catch (e) {
        showToast('保存に失敗しました', true);
    }
};

// マネージャー連絡スケジュール / 自動同期 編集（時刻はカタログ固定。ON/OFFのみ）
function _renderScheduleRow(it) {
    const checked = it.enabled ? 'checked' : '';
    const dowLabel = it.dow_label || '';
    const dowChip = (it.dow && it.dow !== 'daily')
        ? `<span style="font-size:0.7rem;background:rgba(255,212,84,0.18);color:#ffd454;padding:1px 6px;border-radius:8px;margin-left:4px;">${escapeHtml(dowLabel)}</span>`
        : '';
    const desc = it.description
        ? `<div style="font-size:0.7rem;color:var(--text-muted);margin:2px 0 0 22px;">${escapeHtml(it.description)}</div>`
        : '';
    return `<div style="padding:8px 0;border-bottom:1px solid var(--border-glass);">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
            <label style="display:flex;align-items:center;gap:5px;flex:1;min-width:140px;cursor:pointer;">
                <input type="checkbox" class="schedule-enabled" data-key="${escapeHtml(it.key)}" ${checked}>
                <span style="font-size:0.84rem;color:var(--text-primary);font-weight:600;">${escapeHtml(it.label)}</span>
            </label>
            <span style="font-family:monospace;font-size:0.82rem;color:#4ea1ff;min-width:46px;text-align:right;">${escapeHtml(it.time)}</span>
            ${dowChip}
        </div>
        ${desc}
    </div>`;
}

window.loadScheduleSettings = async () => {
    const body = $('#schedules-body');
    if (!body) return;
    body.innerHTML = '<div class="loading-placeholder">読み込み中…</div>';
    try {
        const data = await apiFetch('/api/settings/schedules');
        if (!data || !data.ok) {
            body.innerHTML = '<div class="loading-placeholder">取得に失敗しました。</div>';
            return;
        }
        const manager = data.manager || [];
        const auto = data.auto || [];
        const managerHtml = manager.length
            ? manager.map(_renderScheduleRow).join('')
            : '<div class="loading-placeholder">登録なし</div>';
        const autoHtml = auto.length
            ? auto.map(_renderScheduleRow).join('')
            : '<div class="loading-placeholder">登録なし</div>';
        body.innerHTML = `
            <div style="margin-bottom:10px;">
                <div style="font-size:0.78rem;color:var(--accent);font-weight:700;margin:4px 0;">📨 マネージャー連絡（ユーザーに通知）</div>
                <div style="font-size:0.68rem;color:var(--text-muted);margin-bottom:6px;">時刻は重複しないよう固定。ON/OFFのみ切替できます。</div>
                ${managerHtml}
            </div>
            <div style="margin-top:14px;">
                <div style="font-size:0.78rem;color:var(--text-secondary);font-weight:700;margin:4px 0;">🔄 自動同期（通知なし・内部処理）</div>
                <div style="font-size:0.68rem;color:var(--text-muted);margin-bottom:6px;">ユーザーへの通知は行いません。ON/OFFのみ切替できます。</div>
                ${autoHtml}
            </div>
            <div style="font-size:0.7rem;color:var(--text-muted);margin-top:10px;">⏱ 設定変更は次の実行サイクル（最大1分）から反映されます。Bot 再起動は不要です。</div>
            <button class="modal-btn submit" style="width:100%;margin-top:10px;" onclick="saveScheduleSettings()">スケジュールを保存</button>
        `;
    } catch (e) {
        body.innerHTML = '<div class="loading-placeholder">取得に失敗しました。</div>';
    }
};

window.saveScheduleSettings = async () => {
    const values = {};
    document.querySelectorAll('.schedule-enabled').forEach(chk => {
        const k = chk.dataset.key;
        if (!k) return;
        values[k] = { enabled: chk.checked };
    });
    try {
        const r = await apiFetch('/api/settings/schedules', { method: 'POST', body: JSON.stringify({ values }) });
        if (r && r.ok) {
            showToast(`スケジュール設定を保存しました（${r.saved} 件）`);
        } else {
            showToast('保存に失敗しました', true);
        }
    } catch (e) {
        showToast('保存に失敗しました', true);
    }
};

window.closeSettingsModal = () => {
    $('#settings-modal')?.classList.add('hidden');
};

window.loadCostMeter = async () => {
    const body = $('#cost-meter-body');
    if (!body) return;
    body.innerHTML = '<div class="loading-placeholder">読み込み中…</div>';
    try {
        const [s, settings] = await Promise.all([
            apiFetch('/api/cost_summary?days=30'),
            apiFetch('/api/cost_settings'),
        ]);
        const monthlyTotal = s.this_month_jpy || 0;
        const threshold = s.monthly_threshold_jpy || 0;
        const infra = s.infra_cost_jpy_per_month || 0;
        const ratio = threshold > 0 ? Math.min(100, (monthlyTotal / threshold) * 100) : 0;
        const barColor = ratio >= 100 ? '#ff6b6b' : ratio >= 70 ? '#ffd454' : 'var(--accent)';

        const topModels = (s.by_model || []).slice(0, 5).map(m => `
            <div style="display:flex;justify-content:space-between;gap:6px;font-size:0.82rem;padding:2px 0;border-bottom:1px solid var(--border-glass);">
                <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(m.model)}</span>
                <span style="color:var(--text-muted);font-size:0.76rem;">${(m.in_tokens / 1000).toFixed(1)}k in / ${(m.out_tokens / 1000).toFixed(1)}k out</span>
                <span style="color:var(--text-primary);font-weight:600;">¥${m.jpy.toFixed(1)}</span>
            </div>
        `).join('');

        const daysBars = (s.by_day || []).slice(-7).map(d => `
            <div style="display:flex;justify-content:space-between;font-size:0.76rem;color:var(--text-muted);padding:1px 0;">
                <span>${escapeHtml(d.date.slice(5))}</span>
                <span>¥${d.jpy.toFixed(1)}</span>
            </div>
        `).join('');

        body.innerHTML = `
            <div style="margin-bottom:10px;">
                <div style="font-size:0.78rem;color:var(--text-muted);">今月のAPIコスト（概算）</div>
                <div style="display:flex;align-items:baseline;gap:8px;">
                    <span style="font-size:1.6rem;font-weight:700;color:${barColor};">¥${monthlyTotal.toFixed(0)}</span>
                    <span style="font-size:0.78rem;color:var(--text-muted);">/ 月額閾値 ¥${threshold.toFixed(0)}</span>
                </div>
                <div style="background:rgba(255,255,255,0.08);height:8px;border-radius:4px;overflow:hidden;margin-top:4px;">
                    <div style="background:${barColor};height:100%;width:${ratio}%;transition:width 0.3s;"></div>
                </div>
                <div style="font-size:0.72rem;color:var(--text-muted);margin-top:4px;">
                    入力 ${(s.this_month_in_tokens / 1000).toFixed(1)}k tokens / 出力 ${(s.this_month_out_tokens / 1000).toFixed(1)}k tokens
                    ${infra > 0 ? ` ・ インフラ固定費 ¥${infra.toFixed(0)} を加えると合計 ¥${(monthlyTotal + infra).toFixed(0)}` : ''}
                </div>
            </div>

            <div style="font-size:0.78rem;font-weight:700;color:var(--text-secondary);margin-top:14px;margin-bottom:4px;">📊 モデル別（直近30日）</div>
            ${topModels || '<div class="loading-placeholder">記録なし。</div>'}

            <div style="font-size:0.78rem;font-weight:700;color:var(--text-secondary);margin-top:14px;margin-bottom:4px;">📅 直近7日</div>
            ${daysBars || '<div class="loading-placeholder">記録なし。</div>'}

            <div style="font-size:0.78rem;font-weight:700;color:var(--text-secondary);margin-top:14px;margin-bottom:6px;">⚙️ 設定</div>
            <label style="font-size:0.78rem;color:var(--text-muted);">月額閾値 (円)</label>
            <input id="cost-threshold-input" type="number" class="modern-input" value="${settings.monthly_threshold_jpy || 3000}" style="margin-bottom:6px;">

            <label style="font-size:0.78rem;color:var(--text-muted);">USD→JPY レート</label>
            <input id="cost-rate-input" type="number" step="0.1" class="modern-input" value="${settings.usd_jpy_rate || 150}" style="margin-bottom:6px;">

            <label style="font-size:0.78rem;color:var(--text-muted);">インフラ固定費 (円/月)</label>
            <input id="cost-infra-input" type="number" class="modern-input" value="${settings.infra_cost_jpy_per_month || 0}" style="margin-bottom:8px;">

            <label style="display:flex;align-items:center;gap:8px;font-size:0.85rem;cursor:pointer;margin-bottom:10px;">
                <input id="cost-downgrade-toggle" type="checkbox" ${settings.auto_downgrade_to_flash ? 'checked' : ''}>
                月額閾値の70%超過時に pro モデルを自動で flash に格下げする
            </label>

            <button class="modal-btn submit" style="width:100%;" onclick="saveCostSettings()">設定を保存</button>
        `;
    } catch (e) {
        body.innerHTML = '<div class="loading-placeholder">取得に失敗しました。</div>';
    }
};

window.saveCostSettings = async () => {
    const payload = {
        monthly_threshold_jpy: parseFloat($('#cost-threshold-input')?.value || 3000),
        usd_jpy_rate: parseFloat($('#cost-rate-input')?.value || 150),
        infra_cost_jpy_per_month: parseFloat($('#cost-infra-input')?.value || 0),
        auto_downgrade_to_flash: !!$('#cost-downgrade-toggle')?.checked,
    };
    try {
        await apiFetch('/api/cost_settings', { method: 'POST', body: JSON.stringify(payload) });
        showToast('設定を保存しました');
        loadCostMeter();
    } catch {
        showToast('保存に失敗しました', true);
    }
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

// カード開閉状態を localStorage で記憶（情報タブの <details data-card-key> が対象）
function _restoreCardStates() {
    document.querySelectorAll('#tab-info [data-card-key]').forEach(el => {
        const key = el.dataset.cardKey;
        const saved = localStorage.getItem(`card_open_${key}`);
        if (saved !== null) el.open = saved === '1';
        if (!el.dataset.cardStateInit) {
            el.dataset.cardStateInit = '1';
            el.addEventListener('toggle', () => {
                localStorage.setItem(`card_open_${key}`, el.open ? '1' : '0');
            });
        }
    });
}

function switchTab(tab) {
    $$('.nav-item').forEach(i => i.classList.remove('active'));
    document.querySelector(`.nav-item[data-tab="${tab}"]`)?.classList.add('active');

    $$('.tab-pane').forEach(p => p.classList.remove('active'));
    $(`#tab-${tab}`)?.classList.add('active');

    const titles = { chat: 'チャット', info: '情報', log: 'ライフログ', schedule: '予定', invest: '投資' };
    const titleEl = $('#current-tab-title');
    if (titleEl) titleEl.textContent = titles[tab] || 'Manager AI';

    if (tab !== 'chat' && tab !== 'invest') loadDashboard();
    // 情報タブ: カード開閉状態を復元（localStorage 記憶）
    if (tab === 'info') {
        _restoreCardStates();
    }
    // ログタブを開いたときに Fitbit データとデイリーサマリーを自動ロード
    if (tab === 'log') {
        if (!_fitbitRows.length) loadFitbitAllData(false);
        loadDailySummary();
    }
    if (tab === 'invest') {
        loadInvestmentHistory();
        if (typeof loadPortfolio === 'function') loadPortfolio();
        if (typeof loadWatchlist === 'function') loadWatchlist();
        if (typeof loadJournalList === 'function') loadJournalList();
        if (typeof loadAlertsList === 'function') loadAlertsList();
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
let _isComposing = false;
let _lastSubmitAt = 0;
let _lastSubmitMsg = '';

// IME（日本語変換）中は Enter で送信させない
if (messageInput) {
    messageInput.addEventListener('compositionstart', () => { _isComposing = true; });
    messageInput.addEventListener('compositionend',   () => { _isComposing = false; });
    messageInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && (e.isComposing || _isComposing)) {
            // IME 変換確定の Enter はフォーム送信に伝播させない
            e.stopPropagation();
        }
    });
}

if (chatForm) {
    chatForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        if (isChatSending) return;
        const msg = messageInput.value.trim();
        if (!msg) return;

        // 同一メッセージの 1.5 秒以内の連投は無視（IME 起因の二重送信ガード）
        const now = Date.now();
        if (msg === _lastSubmitMsg && (now - _lastSubmitAt) < 1500) return;
        _lastSubmitAt = now;
        _lastSubmitMsg = msg;

        // 冪等キー（同じ ID のリクエストはサーバ側で 5 秒以内なら無視）
        const clientMsgId = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : `c-${now}-${Math.random().toString(36).slice(2)}`;

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
                body: JSON.stringify({ message: msg, reply_to_id: replyTo, english_mode: _isEnglishMode, client_msg_id: clientMsgId }),
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
    let rawText = String(content || '').replace(/\[ACTION:([^\]]+)\]/g, (_, payload) => {
        actions.push(payload);
        return '';
    });
    // [QUESTIONS:summary:YYYY-MM-DD] マーカーを抽出してインライン回答UIを後で描画
    let questionsScope = null;
    let questionsDate = null;
    rawText = rawText.replace(/\[QUESTIONS:([a-z_]+):(\d{4}-\d{2}-\d{2})\]/g, (_, scope, date) => {
        questionsScope = scope;
        questionsDate = date;
        return '';
    });
    // 内部関数呼び出しの生文字列を除去（「ツールを呼び出す xxx(...)」「tool_call: xxx(...)」など）
    rawText = rawText
        .replace(/^\s*(?:ツールを呼び出す|tool[_ ]?call:?)\s*[\w.]+\([^)]*\)\s*$/gim, '')
        .replace(/^\s*\[?function[_ ]?call\]?:?\s*[\w.]+\([^)]*\)\s*$/gim, '');
    const visibleText = rawText.trim();
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
    if (role === 'assistant' && questionsScope && questionsDate) {
        renderInlineQuestionForm(div, questionsScope, questionsDate);
    }
    chatMessages.scrollTop = chatMessages.scrollHeight;
    if (role === 'assistant') notifyManager(content);
    return div;
}

// [QUESTIONS:scope:YYYY-MM-DD] 付きメッセージにインライン回答UIを描画する
async function renderInlineQuestionForm(msgDiv, scope, date) {
    const wrap = document.createElement('div');
    wrap.className = 'msg-inline-questions';
    wrap.style.cssText = 'margin-top:6px;padding:8px 10px;background:rgba(255,212,84,0.08);border:1px solid rgba(255,212,84,0.3);border-radius:8px;';
    wrap.innerHTML = '<div style="font-size:0.78rem;color:var(--text-muted);">回答欄を読み込み中…</div>';
    const contentEl = msgDiv.querySelector('.msg-content') || msgDiv;
    contentEl.appendChild(wrap);
    try {
        const data = await apiFetch('/api/daily_questions/pending');
        const all = (data && data.questions) || [];
        const items = all.filter(q => q.date === date && (q.scope || 'summary') === scope && q.status !== 'resolved');
        if (!items.length) {
            wrap.innerHTML = '<div style="font-size:0.78rem;color:var(--text-muted);">（すべての質問に回答済み）</div>';
            return;
        }
        wrap.innerHTML = items.map(q => {
            const answered = q.status === 'answered' && q.answer;
            const savedMark = answered ? '<span class="inline-q-saved">✓ 保存済み（編集可）</span>' : '';
            // morning_mit は未回答時、候補リストを回答欄の初期値として流し込む
            let defaultVal = q.answer || '';
            let rows = 2;
            if (!defaultVal && scope === 'morning_mit' && q.context) {
                try {
                    const c = JSON.parse(q.context);
                    if (Array.isArray(c.candidates) && c.candidates.length) {
                        defaultVal = c.candidates.join('\n');
                    }
                } catch (e) { /* context が JSON でなければ無視 */ }
            }
            if (scope === 'morning_mit') rows = 3;
            return `
            <div class="inline-q" data-qid="${q.id}" style="margin-bottom:8px;">
                <div style="font-size:0.82rem;color:var(--text-primary);margin-bottom:4px;display:flex;justify-content:space-between;gap:6px;align-items:flex-start;">
                    <span>${escapeHtml(q.question)}</span>${savedMark}
                </div>
                <textarea class="modern-input inline-q-answer" rows="${rows}" placeholder="回答を入力" style="width:100%;padding:6px;font-size:0.85rem;">${escapeHtml(defaultVal)}</textarea>
                <div style="display:flex;justify-content:flex-end;gap:6px;margin-top:4px;">
                    <button class="modal-btn submit" style="padding:4px 12px;font-size:0.78rem;" onclick="submitInlineAnswer(this, ${q.id}, '${date}', '${scope}')">${answered ? '回答を更新' : '回答'}</button>
                </div>
            </div>`;
        }).join('');
    } catch (e) {
        wrap.innerHTML = '<div style="font-size:0.78rem;color:var(--text-muted);">回答欄の読み込みに失敗しました。</div>';
    }
}

window.submitInlineAnswer = async (btn, qid, date, scope) => {
    const row = btn.closest('.inline-q');
    const ta = row && row.querySelector('.inline-q-answer');
    const answer = (ta && ta.value || '').trim();
    if (!answer) {
        showToast('回答を入力してください', true);
        return;
    }
    btn.disabled = true;
    const origLabel = btn.textContent;
    btn.textContent = '保存中…';
    try {
        await apiFetch(`/api/daily_questions/${qid}/answer`, {
            method: 'POST',
            body: JSON.stringify({ answer }),
        });
        // 回答欄は維持し、ラベルだけ「更新」に切替。次の質問が来るまで再編集可。
        btn.textContent = '回答を更新';
        btn.disabled = false;
        const header = row.querySelector('div:first-child');
        if (header && !header.querySelector('.inline-q-saved')) {
            const mark = document.createElement('span');
            mark.className = 'inline-q-saved';
            mark.textContent = '✓ 保存済み（編集可）';
            header.appendChild(mark);
        }
        showToast('回答を保存しました');
    } catch (e) {
        btn.disabled = false;
        btn.textContent = origLabel;
        showToast('保存に失敗しました', true);
    }
};

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
        case 'propose_perm_note': return `📌 永久ノートにする: ${args.title || ''}`;
        case 'log_life_activity': {
            const s = args.status === 'end' ? '終了' : '開始';
            return `🔥 ライフログを${s}: ${args.activity_name || ''}`;
        }
        case 'save_thought_reflection': return `💭 思考整理を保存: ${args.theme || ''}`;
        case 'habit_complete': return `✅ 習慣を完了: ${args.habit_name || ''}`;
        case 'open_notices': return `📨 マネージャー通知ログを開く`;
        case 'open_link':    return `📂 保存した項目を開く`;
        case 'log_meal':     return `🍽 食事ログに登録: ${args.name || ''}`;
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
    // ナビゲーション系アクション: マネージャー通知ログを開く（ボタンは無効化しない）
    if (action === 'open_notices') {
        switchTab('log');
        setTimeout(() => {
            if (typeof loadManagerNotices === 'function') loadManagerNotices();
            const card = document.querySelector('.manager-notice-card');
            if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }, 300);
        return;
    }
    // ナビゲーション系: 食事ログ登録モーダルを開く（料理名をプレフィル）
    if (action === 'log_meal') {
        try {
            switchTab('info');
            openMealManualModal(null, { name: args.name || '' });
        } catch (e) {
            console.error(e);
            showToast('食事ログを開けませんでした', true);
        }
        return;
    }
    // ナビゲーション系: 保存したストックリンクを直接開く
    if (action === 'open_link') {
        const linkId = parseInt(args.id, 10);
        switchTab('info');
        try {
            const data = await apiFetch('/api/links');
            const link = (data.links || []).find(l => l.id === linkId);
            if (link && typeof openLinkDetailsModal === 'function') {
                openLinkDetailsModal(link);
            } else {
                const det = link ? document.getElementById(`dash-stocked-${link.type}`) : null;
                if (det) {
                    det.open = true;
                    det.scrollIntoView({ behavior: 'smooth', block: 'start' });
                } else {
                    showToast('項目が見つかりませんでした', true);
                }
            }
        } catch (e) {
            console.error(e);
            showToast('項目を開けませんでした', true);
        }
        return;
    }
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
            if (btn) { btn.textContent = '保存モーダルを開いた ✓'; btn.classList.add('done'); }
            return;
        } else if (action === 'propose_perm_note') {
            // 永久ノートの確認モーダルを開く
            window.openPermanentNoteConfirmModal(args.title || '', args.content || '');
            if (btn) { btn.textContent = '確認モーダルを開いた ✓'; btn.classList.add('done'); }
            return;
        } else if (action === 'log_life_activity') {
            const status = (args.status === 'end') ? 'end' : 'start';
            await apiFetch('/api/lifelog_activity', {
                method: 'POST',
                body: JSON.stringify({ activity_name: args.activity_name || '', status })
            });
            showToast(`ライフログを${status === 'start' ? '開始' : '終了'}として記録しました`);
        } else if (action === 'save_thought_reflection') {
            await apiFetch('/api/thought_reflection', {
                method: 'POST',
                body: JSON.stringify({
                    theme: args.theme || '無題',
                    summary: args.summary || '',
                    next_step: args.next_step || ''
                })
            });
            showToast('思考整理を保存しました');
        } else if (action === 'habit_complete') {
            await apiFetch('/api/habits/complete', {
                method: 'POST',
                body: JSON.stringify({ habit_name: args.habit_name || '' })
            });
            showToast(`習慣「${args.habit_name || ''}」を完了として記録しました`);
            if (typeof loadHabits === 'function') loadHabits();
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
    let html = '';
    // 1日の代表天気: アイコン + 天気名 + 気温 + 降水確率を1行に統合
    const today = (w.daily && w.daily[0]) ? w.daily[0] : null;
    const todayIcon = today?.icon || '';
    const todayWeather = today?.weather || w.summary || '';
    const todayMax = today?.max_temp ?? w.max_temp ?? '--';
    const todayMin = today?.min_temp ?? w.min_temp ?? '--';
    const todayPopRaw = today?.pop || '';
    const todayPop = todayPopRaw
        ? (String(todayPopRaw).includes('%') ? todayPopRaw : `${todayPopRaw}%`)
        : '';
    if (todayIcon || todayWeather) {
        html += `<div style="display:flex;align-items:center;gap:10px;padding:8px 4px;">
            <span style="font-size:1.4rem;line-height:1;">${todayIcon}</span>
            <div style="display:flex;flex-direction:column;gap:2px;flex:1;min-width:0;">
                <div style="font-size:0.92rem;font-weight:600;color:var(--text-primary);">${escapeHtml(todayWeather)}</div>
                <div style="font-size:0.82rem;color:var(--text-secondary);display:flex;gap:10px;flex-wrap:wrap;">
                    <span style="color:#ff6b6b;">↑${todayMax}℃</span>
                    <span style="color:#74c0fc;">↓${todayMin}℃</span>
                    ${todayPop ? `<span style="color:var(--text-muted);">☂${escapeHtml(todayPop)}</span>` : ''}
                </div>
            </div>
        </div>`;
    } else if (w.summary) {
        html += `<div class="weather-summary">${escapeHtml(w.summary)}</div>`;
    }
    // 時間別予報を先に表示（今日のものを優先）
    const slots = w.hourly || w.slots || [];
    if (slots.length > 0) {
        html += `<div class="weather-section-label">⏰ 時間別予報</div>`;
        html += `<div class="weather-slots">`;
        let lastDay = '';
        slots.forEach(s => {
            if (s.day && s.day !== lastDay) {
                html += `<div class="weather-day-label">${escapeHtml(s.day)}</div>`;
                lastDay = s.day;
            }
            const popText = s.pop ? (String(s.pop).includes('%') ? s.pop : `${s.pop}%`) : '';
            const tempText = (s.temp ?? '') !== '' ? `${s.temp}℃` : '';
            html += `
                <div class="weather-slot">
                    <div class="ws-time">${escapeHtml(s.time || '')}</div>
                    <div class="ws-icon">${s.icon || ''}</div>
                    <div class="ws-weather">${escapeHtml(s.weather || '')}</div>
                    <div class="ws-pop" title="降水確率">${escapeHtml(popText)}</div>
                    <div class="ws-temp" title="気温">${escapeHtml(tempText)}</div>
                </div>
            `;
        });
        html += `</div>`;
    } else {
        html += `<div class="loading-placeholder">時間別予報の取得に失敗しました。</div>`;
    }
    // 5日先サマリー行（時間別の下に置く）
    if (w.daily && w.daily.length > 0) {
        html += `<div class="weather-section-label">📅 数日先</div>`;
        html += `<div style="display:flex; gap:6px; margin:6px 0 4px; overflow-x:auto; padding-bottom:4px;">`;
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
    // Yahoo!天気へのリンク（location が "33/6710" 形式の場合のみ）
    const loc = w.location || localStorage.getItem('mng_weather_location') || '33/6610';
    if (/^\d+\/\d+$/.test(loc)) {
        // 末尾を `.html` 形式にする（ディレクトリ形式は404になるため）
        html += `<div style="margin-top:8px;text-align:right;">
            <a href="#" onclick="openYahooWeather('${encodeURI(loc)}');return false;"
               style="font-size:0.78rem;color:var(--text-muted);text-decoration:none;">
               Yahoo!天気で詳細を見る ↗
            </a>
        </div>`;
    }
    weatherEl.innerHTML = html;
}

window.openYahooWeather = (loc) => {
    if (!loc) return;
    const webUrl = `https://weather.yahoo.co.jp/weather/jp/${loc}.html`;
    const ua = navigator.userAgent;
    const isAndroid = /Android/i.test(ua);
    const isIOS = /iPhone|iPad|iPod/i.test(ua);
    if (isAndroid) {
        // Android: intent スキームでアプリ起動。未インストールなら browser_fallback_url で Web 版へ
        const intentUrl =
            `intent://weather.yahoo.co.jp/weather/jp/${loc}.html` +
            `#Intent;scheme=https;package=jp.co.yahoo.android.weather.type1` +
            `;S.browser_fallback_url=${encodeURIComponent(webUrl)};end`;
        location.href = intentUrl;
        return;
    }
    if (isIOS) {
        // iOS: Yahoo!天気アプリは Universal Link 設定済みのため、Safari で Web URL を開けば
        // アプリがインストールされていれば自動的にアプリで起動される
        const startedAt = Date.now();
        const fallbackTimer = setTimeout(() => {
            if (document.visibilityState === 'visible' && Date.now() - startedAt < 2500) {
                window.open(webUrl, '_blank', 'noopener');
            }
        }, 1500);
        const onVisChange = () => {
            if (document.visibilityState === 'hidden') {
                clearTimeout(fallbackTimer);
                document.removeEventListener('visibilitychange', onVisChange);
            }
        };
        document.addEventListener('visibilitychange', onVisChange);
        location.href = webUrl;
        return;
    }
    // デスクトップは Web 版を新しいタブで開く
    window.open(webUrl, '_blank', 'noopener');
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
                renderWeather(data.weather, weatherEl);
                // ロケーション名を天気カードタイトルに反映
                if (data.weather.location_name) {
                    const titleEl = $('#weather-card-title');
                    if (titleEl) titleEl.textContent = `Yahoo!天気 (${data.weather.location_name})`;
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
                            if (titleEl) titleEl.textContent = `Yahoo!天気 (${wd.location_name})`;
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
            } else newsEl.innerHTML = '<div class="loading-placeholder">ニュースの取得に失敗しました。</div>';
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
            }).join('') : '<div class="loading-placeholder">予定はありません。</div>';
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
            }).join('') : '<div class="loading-placeholder">ログはまだありません。</div>';
        }

        loadHabits();
        loadMeals();
        _installMealImageListener();
        loadExpenses();
        _installExpenseImageListener();
        loadGmailInbox(currentGmailState);

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
                sleepEl.innerHTML = '<div class="loading-placeholder">昨夜のデータがありません。</div>';
            }
            loadSleepTrend();
        }
        
        const diaryEl = $('#dash-alter-log');
        if (diaryEl) {
            if (data.alter_log) {
                const d = data.alter_log_date || '';
                diaryEl.innerHTML = renderDailyMarkdown(data.alter_log, {
                    dateLabel: d ? `📅 ${d} のメタ観察` : '',
                });
            } else {
                diaryEl.innerHTML = '<div class="loading-placeholder">観察日記はまだ生成されていません。</div>';
            }
        }

        // 今日の日記
        const journalEl = $('#dash-daily-journal');
        if (journalEl) {
            if (data.daily_journal) {
                const d = data.daily_journal_date || '';
                journalEl.innerHTML = renderDailyMarkdown(data.daily_journal, {
                    dateLabel: d ? `📅 ${d} のジャーナル` : '',
                });
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
                    const list = listMatch ? listMatch[1] : '';
                    const text = listMatch ? listMatch[2] : clean;
                    const badge = list
                        ? `<span class="na-list-badge">${escapeHtml(list)}</span>`
                        : `<span class="na-list-badge na-list-badge-empty"></span>`;
                    return `<div class="list-item na-row">
                        ${badge}
                        <span class="na-text">${escapeHtml(text)}</span>
                    </div>`;
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
        loadManagerNotices();

    } catch (err) { console.error(err); }
}

// マネージャー通知ログ：未読バッジ / カード表示 / 既読・未読・削除ボタン
const _NOTICE_CATEGORY_META = {
    market_sentiment: { icon: '🌅', label: '地合い' },
    news_sentiment:   { icon: '📰', label: 'ニュース' },
    alerts_earnings:  { icon: '🔔', label: 'アラート' },
    weekend_stocks:   { icon: '📊', label: '週末株' },
};

function _formatNoticeTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso.replace('T', ' ').slice(0, 16);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const yesterday = new Date(now); yesterday.setDate(yesterday.getDate() - 1);
    const isYesterday = d.toDateString() === yesterday.toDateString();
    const hm = d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0');
    if (sameDay) return `今日 ${hm}`;
    if (isYesterday) return `昨日 ${hm}`;
    return `${d.getMonth()+1}/${d.getDate()} ${hm}`;
}

window.loadManagerNotices = async () => {
    const el = document.getElementById('dash-manager-notices');
    if (!el) return;
    try {
        const data = await apiFetch('/api/manager/notices?limit=30');
        const items = (data && data.items) || [];
        const unread = data && data.unread ? data.unread : 0;
        const badgeEl = document.getElementById('manager-notices-unread');
        if (badgeEl) {
            badgeEl.textContent = unread > 0 ? `未読 ${unread}` : '';
            badgeEl.style.display = unread > 0 ? '' : 'none';
        }
        if (!items.length) {
            el.innerHTML = '<div class="loading-placeholder">まだ通知ログはありません。</div>';
            updateNoticeBulkBtn();
            return;
        }
        el.innerHTML = items.map(it => {
            const meta = _NOTICE_CATEGORY_META[it.category] || { icon: '📨', label: it.category || '通知' };
            const ts = _formatNoticeTime(it.created_at);
            const title = escapeHtml(it.title || '通知');
            const body = renderDailyMarkdown ? renderDailyMarkdown(it.body || '') : escapeHtml(it.body || '');
            const unreadCls = it.is_read ? '' : ' notice-unread';
            const readBtn = it.is_read
                ? `<button class="notice-action-btn" title="未読に戻す" onclick="event.stopPropagation();setNoticeRead(${it.id}, false)">◯ 未読</button>`
                : `<button class="notice-action-btn primary" title="既読にする" onclick="event.stopPropagation();setNoticeRead(${it.id}, true)">✓ 既読</button>`;
            return `<details class="notice-item${unreadCls}" data-id="${it.id}" ontoggle="if(this.open && !${it.is_read?1:0}) setNoticeRead(${it.id}, true)">
                <summary>
                    <span class="notice-summary-row">
                        <input type="checkbox" class="notice-select-cb" value="${it.id}" title="選択" onclick="event.stopPropagation();updateNoticeBulkBtn();" style="margin-right:6px;flex:none;">
                        ${it.is_read ? '' : '<span class="notice-dot" title="未読"></span>'}
                        <span class="notice-icon">${meta.icon}</span>
                        <div class="notice-summary-text">
                            <div class="notice-title">${title}</div>
                            <div class="notice-meta"><span class="notice-chip">${escapeHtml(meta.label)}</span><span class="notice-ts">${escapeHtml(ts)}</span></div>
                        </div>
                    </span>
                </summary>
                <div class="notice-body diary-content">${body}</div>
                <div class="notice-actions">
                    ${readBtn}
                    <button class="notice-action-btn danger" title="削除" onclick="event.stopPropagation();deleteNotice(${it.id})">🗑 削除</button>
                </div>
            </details>`;
        }).join('');
        updateNoticeBulkBtn();
    } catch (e) {
        el.innerHTML = '<div class="loading-placeholder">通知ログの取得に失敗しました。</div>';
    }
};

// 選択中の通知件数に応じて「選択削除」ボタンの表示を更新する
window.updateNoticeBulkBtn = () => {
    const checked = document.querySelectorAll('.notice-select-cb:checked').length;
    const btn = document.getElementById('notice-bulk-delete-btn');
    if (!btn) return;
    btn.style.display = checked > 0 ? '' : 'none';
    btn.textContent = checked > 0 ? `🗑 選択削除 (${checked})` : '🗑 選択削除';
};

// チェックした通知をまとめて削除する
window.deleteSelectedNotices = async () => {
    const ids = [...document.querySelectorAll('.notice-select-cb:checked')].map(cb => cb.value);
    if (!ids.length) return;
    if (!confirm(`選択した ${ids.length} 件の通知を削除しますか？`)) return;
    let ok = 0;
    for (const id of ids) {
        try {
            await apiFetch(`/api/manager/notices/${id}`, { method: 'DELETE' });
            ok++;
        } catch (e) { /* 個別失敗はスキップ */ }
    }
    showToast(`${ok} 件の通知を削除しました`);
    loadManagerNotices();
};

window.setNoticeRead = async (id, isRead) => {
    try {
        await apiFetch(`/api/manager/notices/${id}/read`, {
            method: 'POST',
            body: JSON.stringify({ is_read: !!isRead }),
        });
        loadManagerNotices();
    } catch (e) {
        showToast('既読状態の更新に失敗しました', true);
    }
};

window.deleteNotice = async (id) => {
    if (!confirm('この通知を削除しますか？')) return;
    try {
        await apiFetch(`/api/manager/notices/${id}`, { method: 'DELETE' });
        loadManagerNotices();
    } catch (e) {
        showToast('削除に失敗しました', true);
    }
};

window.reloadManagerNotices = () => loadManagerNotices();

let _currentWorkTasks = [];
let _currentPrivateTasks = [];
let _currentHabitTasks = [];

function renderTaskGroup(container, tasks, listName) {
    if (!container) return;
    if (listName === '仕事') _currentWorkTasks = tasks || [];
    if (listName === 'プライベート') _currentPrivateTasks = tasks || [];
    if (listName === '習慣') _currentHabitTasks = tasks || [];

    if (!tasks || tasks.length === 0) {
        container.innerHTML = '<div class="loading-placeholder">タスクはありません。</div>';
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
                <div class="checkbox-custom" onclick="toggleGoogleTask('${t.id}', '${listName}', false)" style="cursor:pointer;"></div>
                <div class="li-text" style="flex:1;">${escapeHtml(t.title)}${dueLabel ? `<span class="task-due-chip">${escapeHtml(dueLabel)}</span>` : ''}</div>
                <button class="task-due-btn" onclick="openTaskDueEditor('${t.id}', '${listName}', '${escapeHtml(t.title).replace(/'/g, '&#39;')}', '${dueAttr}')" title="締切を編集">📅</button>
            </div>`;
        }),
        ...doneTasks.map(t => `
            <div class="list-item" style="gap:6px; opacity:0.5;" id="gtask-item-${t.id}">
                <div class="checkbox-custom" onclick="toggleGoogleTask('${t.id}', '${listName}', true)" style="background:var(--accent); border-color:var(--accent); cursor:pointer; display:flex; align-items:center; justify-content:center; color:#fff; font-size:0.7rem;" title="未完了に戻す">✓</div>
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

function _getPortfolioOrder() {
    try { return JSON.parse(localStorage.getItem('portfolio_order') || '[]'); } catch { return []; }
}

function initPortfolioSortable(container) {
    if (!window.Sortable) {
        setTimeout(() => initPortfolioSortable(container), 200);
        return;
    }
    if (container._sortable) {
        try { container._sortable.destroy(); } catch {}
    }
    container._sortable = window.Sortable.create(container, {
        handle: '.portfolio-handle',
        animation: 150,
        delay: 200,
        delayOnTouchOnly: true,
        touchStartThreshold: 5,
        fallbackTolerance: 3,
        ghostClass: 'sortable-ghost',
        chosenClass: 'sortable-chosen',
        dragClass: 'sortable-drag',
        onEnd: () => {
            const items = Array.from(container.querySelectorAll('.invest-row[data-code]'));
            const order = items.map(el => el.dataset.code);
            localStorage.setItem('portfolio_order', JSON.stringify(order));
            showToast('並び替えました');
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

window.toggleGoogleTask = async (taskId, listName, currentlyCompleted = false) => {
    const newState = !currentlyCompleted;
    const item = $(`#gtask-item-${taskId}`);
    if (item) {
        item.classList.add('task-syncing');
        const cb = item.querySelector('.checkbox-custom');
        if (newState) {
            item.style.opacity = '0.5';
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
        } else {
            item.style.opacity = '1';
            if (cb) {
                cb.style.background = '';
                cb.style.borderColor = '';
                cb.style.color = '';
                cb.style.fontSize = '';
                cb.textContent = '';
            }
            const text = item.querySelector('.li-text');
            if (text) {
                text.style.textDecoration = '';
                text.style.color = '';
            }
        }
    }
    try {
        await apiFetch('/api/google_tasks_action', {
            method: 'POST',
            body: JSON.stringify({ action: 'toggle', task_id: taskId, completed: newState, list_name: listName })
        });
        showToast(newState ? '完了にしました！' : '未完了に戻しました');
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
        sel.innerHTML = '<option value="">読み込み中…</option>';
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
    bookPasses: [],
    currentPassIndex: null,
    planEditMode: false,
};

window.openReadingModal = async () => {
    readingResetSteps();
    $('#reading-modal').classList.remove('hidden');
    $('#reading-custom-title').value = '';
    $('#reading-prompt-toggle').checked = false;
    $('#reading-plan-area').style.display = 'none';
    readingState.bookPasses = [];
    readingState.currentPassIndex = null;
    readingState.planEditMode = false;
    const listEl = $('#reading-book-list');
    listEl.innerHTML = '<div style="padding:20px; text-align:center; color:var(--text-muted);">候補を読み込み中…</div>';
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
    loadReadingPlan(el.dataset.title);
};

// 手動入力でも本が確定したらプランを読み込む
const _readingCustomTitleEl = document.getElementById('reading-custom-title');
if (_readingCustomTitleEl) {
    _readingCustomTitleEl.addEventListener('change', (e) => {
        loadReadingPlan(e.target.value.trim());
    });
}

// ===== 多読プラン =====
async function loadReadingPlan(title) {
    const area = $('#reading-plan-area');
    if (!title) { area.style.display = 'none'; return; }
    readingState.planEditMode = false;
    $('#reading-plan-edit-actions').style.display = 'none';
    $('#reading-plan-edit-btn').textContent = '✏️ 段階を編集';
    area.style.display = '';
    $('#reading-plan-list').innerHTML = '<div style="color:var(--text-muted); font-size:0.8rem;">読み込み中…</div>';
    try {
        const data = await apiFetch(`/api/reading/plan?book_title=${encodeURIComponent(title)}`);
        readingState.bookPasses = data.passes || [];
    } catch (e) {
        readingState.bookPasses = [];
        $('#reading-plan-list').innerHTML = '<div style="color:var(--text-muted); font-size:0.8rem;">プランを取得できませんでした</div>';
        return;
    }
    // 未読了の最初の段階を初期選択
    const firstUndone = readingState.bookPasses.findIndex(p => !p.done);
    readingState.currentPassIndex = firstUndone >= 0 ? firstUndone : 0;
    renderReadingPlan();
}

function renderReadingPlan() {
    const listEl = $('#reading-plan-list');
    const passes = readingState.bookPasses || [];
    if (!passes.length) { listEl.innerHTML = ''; return; }
    if (readingState.planEditMode) {
        listEl.innerHTML = passes.map((p, i) => `
            <div style="display:flex; align-items:center; gap:6px; margin:4px 0;">
                <span style="color:var(--text-muted); font-size:0.8rem; width:1.4em;">${i + 1}.</span>
                <input type="text" class="modern-input reading-pass-edit" data-idx="${i}" value="${escapeHtml(p.label)}" style="padding:6px; flex:1; font-size:0.85rem;">
                <button onclick="removeReadingPass(${i})" style="background:none; border:none; color:var(--danger,#e66); cursor:pointer;">✕</button>
            </div>
        `).join('');
    } else {
        listEl.innerHTML = passes.map((p, i) => {
            const checked = i === readingState.currentPassIndex ? 'checked' : '';
            const doneMark = p.done
                ? `<span style="color:var(--accent); font-size:0.75rem; margin-left:4px;">✓ ${escapeHtml(p.done_at || '読了')}</span>`
                : '';
            return `
                <label style="display:flex; align-items:center; gap:8px; margin:4px 0; cursor:pointer; ${p.done ? 'opacity:0.65;' : ''}">
                    <input type="radio" name="reading-pass" value="${i}" ${checked} onchange="readingState.currentPassIndex=${i}">
                    <span style="font-size:0.85rem;">${i + 1}. ${escapeHtml(p.label)}${doneMark}</span>
                </label>
            `;
        }).join('');
    }
}

window.toggleReadingPlanEdit = () => {
    if (readingState.planEditMode) {
        // 編集モードのまま編集ボタンを押したらキャンセル扱い
        readingState.planEditMode = false;
        $('#reading-plan-edit-actions').style.display = 'none';
        $('#reading-plan-edit-btn').textContent = '✏️ 段階を編集';
    } else {
        readingState.planEditMode = true;
        $('#reading-plan-edit-actions').style.display = 'flex';
        $('#reading-plan-edit-btn').textContent = '✕ 編集をやめる';
    }
    renderReadingPlan();
};

function syncPassLabelsFromInputs() {
    document.querySelectorAll('.reading-pass-edit').forEach(inp => {
        const idx = parseInt(inp.dataset.idx, 10);
        if (readingState.bookPasses[idx]) {
            readingState.bookPasses[idx].label = inp.value.trim();
        }
    });
}

window.addReadingPass = () => {
    syncPassLabelsFromInputs();
    readingState.bookPasses.push({ label: '新しい段階', done: false, done_at: null });
    renderReadingPlan();
};

window.removeReadingPass = (idx) => {
    syncPassLabelsFromInputs();
    readingState.bookPasses.splice(idx, 1);
    renderReadingPlan();
};

window.saveReadingPlan = async () => {
    syncPassLabelsFromInputs();
    const title = $('#reading-custom-title').value.trim();
    const passes = readingState.bookPasses.filter(p => (p.label || '').trim());
    if (!title) { showToast('本を選んでね', true); return; }
    try {
        await apiFetch('/api/reading/plan', {
            method: 'PUT',
            body: JSON.stringify({ book_title: title, passes })
        });
        readingState.bookPasses = passes;
        if (readingState.currentPassIndex >= passes.length) readingState.currentPassIndex = 0;
        readingState.planEditMode = false;
        $('#reading-plan-edit-actions').style.display = 'none';
        $('#reading-plan-edit-btn').textContent = '✏️ 段階を編集';
        renderReadingPlan();
        showToast('多読プランを保存したよ');
    } catch (e) {
        showToast('保存に失敗', true);
    }
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
        bookPasses: [], currentPassIndex: null, planEditMode: false,
    };
};

// 現在選択中の段階ラベルを返す（無ければ空文字）
function currentPassLabel() {
    const i = readingState.currentPassIndex;
    if (i == null || !readingState.bookPasses[i]) return '';
    return readingState.bookPasses[i].label || '';
}

window.startReading = async () => {
    const title = $('#reading-custom-title').value.trim();
    if (!title) { showToast('書籍を選ぶか入力してね', true); return; }
    readingState.bookTitle = title;
    readingState.startedAt = Date.now();
    readingState.sentPrompts = [];
    readingState.enablePrompt = $('#reading-prompt-toggle').checked;
    const passLabel = currentPassLabel();
    $('#reading-current-book').textContent = title;
    const passEl = $('#reading-current-pass');
    if (passLabel) {
        passEl.textContent = `📚 ${readingState.currentPassIndex + 1}回目: ${passLabel}`;
        passEl.style.display = '';
        $('#reading-pass-done-row').style.display = 'flex';
        $('#reading-pass-done').checked = true;
    } else {
        passEl.style.display = 'none';
        $('#reading-pass-done-row').style.display = 'none';
    }
    $('#reading-memo-input').value = '';
    $('#reading-prompt-area').style.display = 'none';
    $('#reading-prompt-area').textContent = '';
    $('#reading-step-select').style.display = 'none';
    $('#reading-step-active').style.display = '';

    // 過去のメモを折りたたみに後追いで読み込む（タイマー開始はブロックしない）
    $('#reading-past-memo').open = false;
    loadReadingPastMemo(title);

    // ライフログ start（チャット経由でAIにログを記録させる）
    sendActionCommandSilent(passLabel ? `「${title}」読書開始（${passLabel}）` : `「${title}」読書開始`);

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

// 書籍ノートの過去メモ（Reading Log）を折りたたみへ読み込む
async function loadReadingPastMemo(title) {
    const body = $('#reading-past-memo-body');
    if (!body) return;
    body.textContent = '読み込み中…';
    try {
        const data = await apiFetch(`/api/reading/log?book_title=${encodeURIComponent(title)}`);
        const log = (data.log || '').trim();
        if (log) {
            body.innerHTML = escapeHtml(log).replace(/\n/g, '<br>');
        } else {
            body.innerHTML = '<span style="color:var(--text-muted);">過去のメモはまだありません</span>';
        }
    } catch (e) {
        body.innerHTML = '<span style="color:var(--text-muted);">過去のメモを取得できませんでした</span>';
    }
}

async function fetchReadingPrompt() {
    try {
        const data = await apiFetch('/api/reading/prompt', {
            method: 'POST',
            body: JSON.stringify({
                book_title: readingState.bookTitle,
                previous_prompts: readingState.sentPrompts,
                current_pass: currentPassLabel()
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
    const passLabel = currentPassLabel();
    const elapsedMin = readingState.startedAt
        ? Math.round((Date.now() - readingState.startedAt) / 60000)
        : 0;

    if (memo) {
        try {
            const passPrefix = passLabel ? `【${passLabel}】\n` : '';
            const memoWithTime = elapsedMin > 0
                ? `${passPrefix}${memo}\n\n（読書時間: ${elapsedMin}分）`
                : `${passPrefix}${memo}`;
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

    // 多読プラン: この段階を読了にする（チェックがオンのとき）
    const passIdx = readingState.currentPassIndex;
    if (passLabel && passIdx != null && $('#reading-pass-done')?.checked) {
        const pass = readingState.bookPasses[passIdx];
        if (pass) {
            pass.done = true;
            pass.done_at = new Date().toISOString().slice(0, 10);
            try {
                await apiFetch('/api/reading/plan', {
                    method: 'PUT',
                    body: JSON.stringify({ book_title: title, passes: readingState.bookPasses })
                });
            } catch (e) {
                console.debug('reading plan update failed', e);
            }
        }
    }

    // ライフログ end（時刻レンジ形式でログ記録）
    const startDate = new Date(readingState.startedAt);
    const endDate = new Date();
    const startTimeStr = startDate.getHours().toString().padStart(2,'0') + ':' + startDate.getMinutes().toString().padStart(2,'0');
    const endTimeStr = endDate.getHours().toString().padStart(2,'0') + ':' + endDate.getMinutes().toString().padStart(2,'0');
    const endSuffix = passLabel ? `読書終了（${passLabel}）` : '読書終了';
    sendActionCommandSilent(`${startTimeStr}-${endTimeStr} 「${title}」${endSuffix}`);
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

// ============ MEMO MODAL (永久ノート) ============
let _memoSelectedTarget = null;  // {id, folder, filename, name} or null
let _memoSearchTimer = null;

window.openMemoModal = (preset = {}) => {
    _memoSelectedTarget = null;
    const m = $('#memo-modal');
    if (!m) return;
    $('#memo-title-input').value = preset.title || '';
    $('#memo-body-input').value = preset.body || '';
    $('#memo-suggestions').style.display = 'none';
    $('#memo-suggestions').innerHTML = '';
    $('#memo-target-banner').style.display = 'none';
    m.classList.remove('hidden');
    setTimeout(() => { $('#memo-title-input')?.focus(); }, 50);
};

window.closeMemoModal = () => {
    const m = $('#memo-modal');
    if (m) m.classList.add('hidden');
};

window.memoClearTarget = () => {
    _memoSelectedTarget = null;
    $('#memo-target-banner').style.display = 'none';
};

window.memoTitleInput = () => {
    const q = ($('#memo-title-input')?.value || '').trim();
    if (_memoSearchTimer) clearTimeout(_memoSearchTimer);
    if (q.length < 2) {
        $('#memo-suggestions').style.display = 'none';
        return;
    }
    _memoSearchTimer = setTimeout(async () => {
        try {
            const data = await apiFetch(`/api/notes/search?q=${encodeURIComponent(q)}&limit=8`);
            const list = (data && data.candidates) || [];
            const el = $('#memo-suggestions');
            if (!list.length) {
                el.style.display = 'none';
                return;
            }
            el.innerHTML = list.map(c => `
                <div class="modal-list-item" style="cursor:pointer;padding:8px 10px;border-bottom:1px solid var(--border-glass);" onclick='memoPickTarget(${JSON.stringify(c).replace(/'/g, "&#39;")})'>
                    <div style="font-size:0.88rem;">📄 ${escapeHtml(c.name)}</div>
                    <div style="font-size:0.7rem;color:var(--text-muted);">${escapeHtml(c.filename)}</div>
                </div>`).join('');
            el.style.display = 'block';
        } catch (e) {
            console.warn('memo search err', e);
        }
    }, 300);
};

window.memoPickTarget = (c) => {
    if (!c) return;
    if (!confirm(`既存ノート「${c.name}」に追記しますか？\nキャンセルすると新規ノートとして保存します。`)) {
        _memoSelectedTarget = null;
        $('#memo-target-banner').style.display = 'none';
        return;
    }
    _memoSelectedTarget = c;
    $('#memo-target-text').textContent = `追記先: ${c.name}`;
    $('#memo-target-banner').style.display = 'block';
    $('#memo-suggestions').style.display = 'none';
};

window.saveMemoFromModal = async () => {
    const title = ($('#memo-title-input')?.value || '').trim();
    const body = ($('#memo-body-input')?.value || '').trim();
    if (!title && !body) {
        showToast('タイトルか本文を入力してください', true);
        return;
    }
    const btn = $('#memo-save-btn');
    if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
    try {
        let payload;
        if (_memoSelectedTarget) {
            payload = {
                mode: 'append',
                content: (title ? `### ${title}\n` : '') + body,
                target_folder: _memoSelectedTarget.folder,
                target_filename: _memoSelectedTarget.filename,
                target_id: _memoSelectedTarget.id || '',
            };
        } else {
            payload = {
                mode: 'new',
                title: title || 'メモ',
                content: body,
                category: 'other',
            };
        }
        const data = await apiFetch('/api/save_note', { method: 'POST', body: JSON.stringify(payload) });
        if (data && (data.status === 'success' || data.ok || data.success)) {
            showToast(_memoSelectedTarget ? `✏️ 「${_memoSelectedTarget.name}」に追記しました` : '📝 永久ノートに保存しました');
            closeMemoModal();
        } else {
            showToast('保存に失敗しました', true);
        }
    } catch (e) {
        showToast(`通信エラー: ${e.message || e}`, true);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '💾 保存'; }
    }
};

window.openStudyModal = async () => {
    studyResetSteps();
    $('#study-modal').classList.remove('hidden');
    $('#study-custom-subject').value = '';
    const listEl = $('#study-subject-list');
    listEl.innerHTML = '<div style="padding:20px; text-align:center; color:var(--text-muted);">候補を読み込み中…</div>';
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
    const cc = $('#study-cornell-cues'); if (cc) cc.value = '';
    const cn = $('#study-cornell-notes'); if (cn) cn.value = '';
    const cs = $('#study-cornell-summary'); if (cs) cs.value = '';
    const qq = $('#study-qa-q'); if (qq) qq.value = '';
    const qa = $('#study-qa-a'); if (qa) qa.value = '';
    window._studyDraftQA = [];
    _renderStudyQADraft();
    studySwitchTab('free');
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

// ============ STUDY: tabs + Cornell + Q&A + SRS ============
window._studyDraftQA = []; // [{q, a}]

window.studySwitchTab = (tab) => {
    document.querySelectorAll('.study-tab-btn').forEach(b => {
        const active = b.dataset.stab === tab;
        b.classList.toggle('active', active);
        b.style.borderBottomColor = active ? 'var(--accent)' : 'transparent';
        b.style.color = active ? 'var(--text-primary)' : 'var(--text-secondary)';
    });
    document.querySelectorAll('.study-pane').forEach(p => { p.style.display = 'none'; });
    const map = { free: '#study-pane-free', cornell: '#study-pane-cornell', qa: '#study-pane-qa' };
    const sel = map[tab];
    if (sel) { const el = $(sel); if (el) el.style.display = 'block'; }
};

window.studyAddQA = () => {
    const q = ($('#study-qa-q')?.value || '').trim();
    const a = ($('#study-qa-a')?.value || '').trim();
    if (!q || !a) { showToast('Q と A の両方を入力してください', true); return; }
    window._studyDraftQA.push({ q, a });
    $('#study-qa-q').value = '';
    $('#study-qa-a').value = '';
    _renderStudyQADraft();
};

function _renderStudyQADraft() {
    const el = $('#study-qa-list');
    if (!el) return;
    if (!window._studyDraftQA.length) { el.innerHTML = '<div style="color:var(--text-muted);font-size:0.78rem;padding:6px;">まだカードはありません。</div>'; return; }
    el.innerHTML = window._studyDraftQA.map((p, i) => `
        <div style="padding:6px 8px;border:1px solid var(--border-glass);border-radius:6px;margin-bottom:4px;">
            <div><span style="color:#4ea1ff;font-weight:700;">Q.</span> ${escapeHtml(p.q)}</div>
            <div><span style="color:#7cd6a0;font-weight:700;">A.</span> ${escapeHtml(p.a)}</div>
            <button class="mini-link" style="font-size:0.7rem;" onclick="window._studyDraftQA.splice(${i},1);_renderStudyQADraft();">✕ 削除</button>
        </div>`).join('');
}

// ============ SRS storage (localStorage) ============
const SRS_KEY = 'study_srs_cards_v1';
// interval (days) by repetition stage
const SRS_INTERVALS = [1, 3, 7, 14, 30, 60, 120];

function _srsLoad() {
    try { return JSON.parse(localStorage.getItem(SRS_KEY) || '[]'); } catch { return []; }
}
function _srsSave(arr) { localStorage.setItem(SRS_KEY, JSON.stringify(arr)); }

function _srsAddCards(subject, qaList) {
    if (!qaList || !qaList.length) return;
    const now = Date.now();
    const arr = _srsLoad();
    for (const p of qaList) {
        arr.push({
            id: `${now}-${Math.random().toString(36).slice(2, 8)}`,
            subject, q: p.q, a: p.a,
            stage: 0,
            due: now + SRS_INTERVALS[0] * 86400000,
            created: now,
        });
    }
    _srsSave(arr);
    updateSrsDueBadge();
}

window.updateSrsDueBadge = () => {
    const badge = $('#srs-due-badge');
    if (!badge) return;
    const due = _srsLoad().filter(c => c.due <= Date.now()).length;
    if (due > 0) { badge.textContent = String(due); badge.style.display = 'inline-block'; }
    else { badge.style.display = 'none'; }
};

let _srsQueue = [];
let _srsIdx = 0;

window.openSrsReviewModal = () => {
    _srsQueue = _srsLoad().filter(c => c.due <= Date.now()).sort((a, b) => a.due - b.due);
    _srsIdx = 0;
    const m = $('#srs-modal');
    if (m) m.classList.remove('hidden');
    _renderSrsCard();
};
window.closeSrsReviewModal = () => {
    const m = $('#srs-modal');
    if (m) m.classList.add('hidden');
    updateSrsDueBadge();
};

function _renderSrsCard() {
    const empty = $('#srs-empty');
    const area = $('#srs-card-area');
    if (_srsIdx >= _srsQueue.length) {
        if (empty) empty.style.display = 'block';
        if (area) area.style.display = 'none';
        return;
    }
    if (empty) empty.style.display = 'none';
    if (area) area.style.display = 'block';
    const c = _srsQueue[_srsIdx];
    $('#srs-progress').textContent = `${_srsIdx + 1} / ${_srsQueue.length}`;
    $('#srs-subject').textContent = `📚 ${c.subject || ''}`;
    $('#srs-q').textContent = c.q;
    $('#srs-a').textContent = c.a;
    $('#srs-a-wrap').style.display = 'none';
    $('#srs-show-btn').style.display = 'block';
    $('#srs-grade-row').style.display = 'none';
}

window.srsShowAnswer = () => {
    $('#srs-a-wrap').style.display = 'block';
    $('#srs-show-btn').style.display = 'none';
    $('#srs-grade-row').style.display = 'flex';
};

window.srsGrade = (grade) => {
    // grade: 0=Again, 1=Hard, 2=Good, 3=Easy
    const cur = _srsQueue[_srsIdx];
    if (!cur) return;
    const all = _srsLoad();
    const idx = all.findIndex(x => x.id === cur.id);
    if (idx >= 0) {
        let stage = all[idx].stage;
        if (grade === 0) stage = 0;
        else if (grade === 1) stage = Math.max(0, stage); // same stage
        else if (grade === 2) stage = Math.min(SRS_INTERVALS.length - 1, stage + 1);
        else if (grade === 3) stage = Math.min(SRS_INTERVALS.length - 1, stage + 2);
        all[idx].stage = stage;
        all[idx].due = Date.now() + SRS_INTERVALS[stage] * 86400000;
        all[idx].lastReviewed = Date.now();
        _srsSave(all);
    }
    if (grade === 0) {
        // 末尾に再投入
        _srsQueue.push(cur);
    }
    _srsIdx += 1;
    _renderSrsCard();
};

// ページロード時にバッジ更新
document.addEventListener('DOMContentLoaded', () => { try { updateSrsDueBadge(); } catch {} });

window.finishStudy = async () => {
    const freeMemo = ($('#study-memo-input')?.value || '').trim();
    const cCues = ($('#study-cornell-cues')?.value || '').trim();
    const cNotes = ($('#study-cornell-notes')?.value || '').trim();
    const cSum = ($('#study-cornell-summary')?.value || '').trim();
    const cornellBlock = (cCues || cNotes || cSum)
        ? `\n\n## 📐 Cornell\n### 🔑 Cues\n${cCues || '-'}\n\n### 📓 Notes\n${cNotes || '-'}\n\n### 📌 Summary\n${cSum || '-'}\n`
        : '';
    const qaBlock = window._studyDraftQA.length
        ? '\n\n## 🃏 Q&A 暗記カード\n' + window._studyDraftQA.map(p => `Q: ${p.q}\nA: ${p.a}`).join('\n\n') + '\n'
        : '';
    const subject = studyState.subject;
    const elapsedMin = studyState.startedAt
        ? Math.round((Date.now() - studyState.startedAt) / 60000)
        : 0;

    const memo = (freeMemo || '') + cornellBlock + qaBlock;
    if (memo.trim()) {
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
    // SRS にカード登録
    if (window._studyDraftQA.length) {
        _srsAddCards(subject, window._studyDraftQA);
        showToast(`🃏 ${window._studyDraftQA.length} 枚を復習スケジュールに追加`);
    }
    window._studyDraftQA = [];
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

// ===========================================================
// 勉強カード用ヘルパー
// ===========================================================

// メモテキストを Q&A / 箇条書き / 通常テキスト混在でレンダリング
function renderStudyMemo(text) {
    if (!text) return '';
    const lines = text.split('\n');
    let html = '';
    let inUl = false;
    for (const line of lines) {
        if (/^Q:/i.test(line)) {
            if (inUl) { html += '</ul>'; inUl = false; }
            html += `<div class="study-note-line"><span class="study-note-q">Q</span>${escapeHtml(line.slice(2).trim())}</div>`;
        } else if (/^A:/i.test(line)) {
            if (inUl) { html += '</ul>'; inUl = false; }
            html += `<div class="study-note-line"><span class="study-note-a">A</span>${escapeHtml(line.slice(2).trim())}</div>`;
        } else if (/^[\-*]\s/.test(line)) {
            if (!inUl) { html += '<ul class="study-note-ul">'; inUl = true; }
            html += `<li>${escapeHtml(line.slice(2).trim())}</li>`;
        } else if (line.trim()) {
            if (inUl) { html += '</ul>'; inUl = false; }
            html += `<div class="study-note-line">${escapeHtml(line)}</div>`;
        }
    }
    if (inUl) html += '</ul>';
    return html;
}

// 勉強カード専用ヘッダ（ソートのみ・タグ絞り込みなし）
function setupStudyHeader(container, type) {
    if (!container) return;
    const details = container.closest('details');
    if (!details) return;
    details.querySelectorAll(':scope > .stocked-list-controls').forEach(n => n.remove());
    const ctrl = document.createElement('div');
    ctrl.className = 'stocked-list-controls';
    const sortSelect = document.createElement('select');
    sortSelect.dataset.role = 'sort-select';
    sortSelect.title = '並び順';
    sortSelect.innerHTML = `
        <option value="newest" ${linkSorts[type]==='newest'?'selected':''}>新しい順</option>
        <option value="oldest" ${linkSorts[type]==='oldest'?'selected':''}>古い順</option>
        <option value="title" ${linkSorts[type]==='title'?'selected':''}>タイトル順</option>
        <option value="custom" ${linkSorts[type]==='custom'?'selected':''}>カスタム順</option>
    `;
    sortSelect.onchange = (e) => changeLinkSort(type, e.target.value);
    ctrl.appendChild(sortSelect);
    details.insertBefore(ctrl, container);
}

// 勉強アイテム追加モーダル（URL なしでタイトルだけ追加）
window.openAddStudyItemModal = () => {
    // 共通の手動追加モーダルを study タイプで開く
    openManualAddModal('study');
};

// ストックリンクのカスタム並び順（localStorage 永続化）
const STOCKED_ORDER_KEY = (type) => `stocked_links_order_${type}`;
function _loadStockedCustomOrder(type) {
    try { return JSON.parse(localStorage.getItem(STOCKED_ORDER_KEY(type)) || '[]'); }
    catch { return []; }
}
function _saveStockedCustomOrder(type, ids) {
    localStorage.setItem(STOCKED_ORDER_KEY(type), JSON.stringify(ids));
}

window.loadStockedLinks = async () => {
    try {
        const data = await apiFetch('/api/links');
        const webEl = $('#dash-stocked-web');
        const ytEl = $('#dash-stocked-youtube');
        const recipeEl = $('#dash-stocked-recipe');
        const mapEl = $('#dash-stocked-map');
        const bookEl = $('#dash-stocked-book');
        const studyEl = $('#dash-stocked-study');

        let links = data.links || [];

        const getSortFn = (type) => {
            return (a, b) => {
                const sortType = linkSorts[type];
                if (sortType === 'custom') {
                    const order = _loadStockedCustomOrder(type);
                    const idxA = order.indexOf(a.id);
                    const idxB = order.indexOf(b.id);
                    const ia = idxA === -1 ? Infinity : idxA;
                    const ib = idxB === -1 ? Infinity : idxB;
                    if (ia !== ib) return ia - ib;
                    // 並び順に未登録の項目は新しい順で末尾に並ぶ
                    return new Date(b.added_at) - new Date(a.added_at);
                }
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
            study: links.filter(l => l.type === 'study'),
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
        const studyLinks = allByType.study.sort(getSortFn('study'));

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
                    <option value="custom" ${linkSorts[type]==='custom'?'selected':''}>カスタム順</option>
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
        // 勉強カードはソートのみ（タグ絞り込みとURLなし追加はカスタムUI）
        setupStudyHeader(studyEl, 'study');

        // 古い全体のソートプルダウンがあれば削除
        const oldWrap = $('#link-sort-wrapper');
        if (oldWrap) oldWrap.remove();

        const renderGroup = (container, items) => {
            if (!container) return;
            container.classList.add('stocked-list');
            if (items.length === 0) {
                container.innerHTML = '<div class="loading-placeholder">登録がありません。</div>';
                return;
            }
            container.innerHTML = items.map(lk => {
                const dateStr = new Date(lk.added_at).toLocaleString('ja-JP', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
                const titleText = (lk.title && lk.title !== 'Untitled') ? lk.title : (lk.url || '(無題)');
                const rawTitleEl = lk.url
                    ? `<a class="stocked-link-title" href="${lk.url}" target="_blank" rel="noopener">${escapeHtml(titleText)}</a>`
                    : `<span class="stocked-link-title">${escapeHtml(titleText)}</span>`;
                // 並び替え用のドラッグハンドル（タイトル行先頭）
                const dragHandle = `<span class="list-item-drag-handle" title="長押しして並び替え" onclick="event.preventDefault(); event.stopPropagation();">⋮⋮</span>`;
                // 書籍・勉強で NotebookLM 等のノート URL が登録されている場合、タイトル行にワンタッチ起動ボタンを表示
                const noteBtn = ((lk.type === 'book' || lk.type === 'study') && lk.linked_note_url)
                    ? `<a class="stocked-link-notebook-btn" href="${escapeHtml(lk.linked_note_url)}" target="_blank" rel="noopener" title="NotebookLM を開く" onclick="event.stopPropagation();">📓 NotebookLM</a>`
                    : '';
                const titleEl = `<div class="stocked-link-title-row">${dragHandle}${rawTitleEl}${noteBtn}</div>`;

                const chips = [];
                if (lk.tags) {
                    lk.tags.split(',').map(t => t.trim()).filter(Boolean).forEach(tag => {
                        chips.push(`<span class="stocked-link-chip purpose">🏷️ ${escapeHtml(tag)}</span>`);
                    });
                }
                if (lk.target_date) chips.push(`<span class="stocked-link-chip date">📅 ${escapeHtml(lk.target_date)}</span>`);
                chips.push(`<span class="stocked-link-chip added">${dateStr}</span>`);

                const lkJson = JSON.stringify(lk).replace(/'/g, "&#39;");
                // 勉強カードのみ notes（メモ）をQ&A/箇条書き混在形式で展開表示
                const memoBlock = (lk.type === 'study' && lk.notes)
                    ? `<div class="study-memo-block">${renderStudyMemo(lk.notes)}</div>`
                    : '';
                return `
                    <div class="stocked-link" id="stocked-link-${lk.id}">
                        ${titleEl}
                        <div class="stocked-link-meta">${chips.join('')}</div>
                        ${memoBlock}
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
        renderGroup(studyEl, studyLinks);

        // ドラッグ&ドロップ並び替え（SortableJS 必須・defer ロード）
        const setupSortable = (container, type) => {
            if (!container) return;
            // 既存インスタンスは破棄してから再構築（onload 毎の重複防止）
            try { if (container._sortable) { container._sortable.destroy(); container._sortable = null; } } catch {}
            if (typeof window.Sortable === 'undefined') {
                setTimeout(() => setupSortable(container, type), 200);
                return;
            }
            container._sortable = window.Sortable.create(container, {
                handle: '.list-item-drag-handle',
                animation: 150,
                ghostClass: 'sortable-ghost',
                chosenClass: 'sortable-chosen',
                onEnd: () => {
                    const ids = Array.from(container.querySelectorAll('.stocked-link'))
                        .map(el => parseInt((el.id || '').replace('stocked-link-', ''), 10))
                        .filter(n => Number.isFinite(n));
                    _saveStockedCustomOrder(type, ids);
                    // ドラッグした時点で「カスタム順」に自動切替
                    if (linkSorts[type] !== 'custom') {
                        linkSorts[type] = 'custom';
                        const details = container.closest('details');
                        const sel = details?.querySelector('[data-role="sort-select"]');
                        if (sel) sel.value = 'custom';
                    }
                    showToast('並び替えました');
                },
            });
        };
        setupSortable(webEl, 'web');
        setupSortable(ytEl, 'youtube');
        setupSortable(recipeEl, 'recipe');
        setupSortable(mapEl, 'map');
        setupSortable(bookEl, 'book');
        setupSortable(studyEl, 'study');

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

window._openLinkedNote = () => {
    const url = $('#link-note-url-input')?.value?.trim();
    if (!url) {
        showToast('ノートURLが未入力です。先に保存してください', true);
        return;
    }
    if (!/^https?:\/\//i.test(url)) {
        showToast('有効なURLではありません', true);
        return;
    }
    window.open(url, '_blank', 'noopener');
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
            <option value="study" ${lk.type==='study'?'selected':''}>✏️ 勉強</option>
        </select>
    `;
    // study タイプは要約フィールドを非表示にする
    const _updateSummaryVisibility = () => {
        const t = $('#link-type-select')?.value;
        const summaryRow = $('#link-summary-input')?.closest('div[style]') || $('#link-summary-input')?.parentElement;
        if (summaryRow) summaryRow.style.display = (t === 'study') ? 'none' : '';
        const memoInput = $('#link-memo-input');
        if (memoInput) memoInput.placeholder = (t === 'study')
            ? 'Q: ○○とは？\nA: △△のこと\n- ポイント: ◇◇'
            : '個人的なメモ...';
    };
    _updateSummaryVisibility();
    requestAnimationFrame(() => {
        $('#link-type-select')?.addEventListener('change', _updateSummaryVisibility);
    });

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

// 共有テキスト（PayPay 等の決済通知文）から支出メモ用の seed を組み立てる
function _buildExpenseSeedFromText(text) {
    if (!text) return null;
    // PayPay 系キーワードまたは「○○円」を含む場合のみ反応
    const looksLikePayment = /(PayPay|ペイペイ|pay\s*pay|円)/i.test(text);
    if (!looksLikePayment) return null;
    // 金額抽出: "1,234円" "¥1234" "1234 円" などに対応
    const amountMatch = text.match(/(?:¥|￥)?\s*([0-9][0-9,]{0,9})\s*円/);
    const amount = amountMatch ? parseInt(amountMatch[1].replace(/,/g, ''), 10) : 0;
    // 店名推定: 行頭〜「で支払い/に支払/にて」までの最後 30 文字
    let vendor = '';
    const storeMatch = text.match(/(.{1,40}?)\s*(?:で支払い|で決済|に支払|にて支払)/);
    if (storeMatch) {
        vendor = storeMatch[1].trim().slice(-30);
    }
    // PayPay 文字列が含まれていれば支払方法に QR をセット
    const isPayPay = /(PayPay|ペイペイ|pay\s*pay)/i.test(text);
    return {
        amount: amount || undefined,
        vendor: vendor || (isPayPay ? 'PayPay 支払い' : ''),
        payment_method: isPayPay ? 'QR' : '',
        memo: text.length <= 200 ? text : text.slice(0, 200),
        category: 'その他',
    };
}

function _handleExpenseShareTarget() {
    // 旧 GET 形式の share_target 互換: クエリ文字列から PayPay 等のテキストを取り込む
    const params = new URLSearchParams(window.location.search);
    const text = ((params.get('text') || '') + ' ' + (params.get('title') || '')).trim();
    const seed = _buildExpenseSeedFromText(text);
    if (!seed) return false;
    window.history.replaceState({}, '', '/');
    setTimeout(() => {
        try { openExpenseManualModal(null, seed); } catch {}
    }, 600);
    return true;
}

// 共有された画像（PayPay 支払い画面・通販の購入履歴スクショ等）を解析して支出モーダルを開く
async function _analyzeSharedExpenseImage(blob) {
    showToast('🧾 共有された画像を解析中…');
    try {
        const base64data = await _fileToBase64(blob);
        _pendingReceiptBase64 = base64data;
        _pendingReceiptMime = blob.type || 'image/jpeg';
        _pendingReceiptDriveId = '';
        const res = await apiFetch('/api/expenses/analyze', {
            method: 'POST',
            body: JSON.stringify({ image_base64: base64data, mime_type: _pendingReceiptMime }),
        });
        if (!res || !res.ok) { showToast('解析に失敗しました', true); return; }
        _pendingReceiptAnalysis = res.result || {};
        openExpenseManualModal(null, _pendingReceiptAnalysis);
    } catch (e) {
        showToast('画像の解析に失敗しました', true);
    }
}

// PWA Web Share Target (POST) で共有されたペイロードを SW キャッシュ経由で受け取り振り分ける
async function _handleShareTarget() {
    const params = new URLSearchParams(window.location.search);
    if (params.get('share-target') !== '1') return false;
    window.history.replaceState({}, '', '/');
    let payload = null;
    try {
        const r = await fetch('/__share_payload__');
        if (r.ok) payload = await r.json();
    } catch (e) { /* ignore */ }
    if (!payload) return false;

    // 画像が共有された → 支出として解析（PayPay 支払い画面・購入履歴スクショ）
    if (payload.hasImage) {
        try {
            const imgResp = await fetch('/__share_image__');
            if (imgResp.ok) {
                await _analyzeSharedExpenseImage(await imgResp.blob());
                return true;
            }
        } catch (e) { /* fallthrough */ }
    }

    const text = ((payload.text || '') + ' ' + (payload.title || '')).trim();
    // PayPay 等の決済テキスト → 支出メモ
    const seed = _buildExpenseSeedFromText(text);
    if (seed) {
        setTimeout(() => { try { openExpenseManualModal(null, seed); } catch {} }, 500);
        return true;
    }
    // URL を含む → リンクをストック（チャットへ流す）
    const urlToStock = (payload.url || '') || ((text.match(/https?:\/\/[^\s]+/) || [''])[0]);
    if (urlToStock) {
        switchTab('chat');
        setTimeout(async () => {
            const msg = payload.title ? `${payload.title}\n${urlToStock}` : urlToStock;
            appendMsg('user', msg);
            try {
                const data = await apiFetch('/api/chat', { method: 'POST', body: JSON.stringify({ message: msg }) });
                appendMsg('assistant', data.reply);
            } catch (e) {
                appendMsg('assistant', 'リンクのストックに失敗しました。');
            }
        }, 500);
        return true;
    }
    return false;
}

// 全クリック対象に即時タップフィードバック（押した感を出す）
function _installGlobalButtonFeedback() {
    if (window._globalBtnFeedbackInstalled) return;
    window._globalBtnFeedbackInstalled = true;
    document.addEventListener('pointerdown', (e) => {
        const el = e.target.closest('button, .mini-link, .chip-btn, .modal-btn, .nav-item');
        if (!el || el.disabled || el.classList.contains('is-busy')) return;
        el.classList.add('btn-pressed');
    }, true);
    const release = (e) => {
        document.querySelectorAll('.btn-pressed').forEach(el => el.classList.remove('btn-pressed'));
    };
    document.addEventListener('pointerup', release, true);
    document.addEventListener('pointercancel', release, true);
    document.addEventListener('pointerleave', release, true);
}

function initMain() {
    loadHistory();
    _startChatAutoRefresh();
    _installGlobalButtonFeedback();
    loadDashboard();
    requestNotificationPermission();
    // PayPay 等の共有が来ていれば支出モーダル/チャットへ振り分け
    if (apiKey) { _handleExpenseShareTarget(); _handleShareTarget(); }

    const params = new URLSearchParams(window.location.search);
    const sharedUrl = params.get('url') || '';
    const sharedText = params.get('text') || '';
    const sharedTitle = params.get('title') || '';

    // 朝の MIT 提案: プッシュ通知から起動された場合（?openMorningMit=1）はモーダルを開く
    const wantMorningMit = params.get('openMorningMit') === '1';
    // 設定: ?openSettings=1 で設定モーダルを開く（コスト超過アラートからの遷移）
    const wantSettings = params.get('openSettings') === '1';
    // メール: ?openInbox=1 でログタブを開き、メールカードへスクロール
    const wantInbox = params.get('openInbox') === '1';
    if (apiKey) {
        setTimeout(() => { checkMorningMit(wantMorningMit); }, 800);
        if (wantSettings) {
            setTimeout(() => openSettingsModal(), 600);
        }
        if (wantInbox) {
            setTimeout(() => {
                switchTab('log');
                const card = $('#dash-gmail-list');
                if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }, 700);
        }
        if (wantMorningMit || wantSettings || wantInbox) {
            window.history.replaceState({}, '', '/');
        }
    }

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

let _lastChatMsgIds = '';
async function loadHistory(silent = false) {
    try {
        const data = await apiFetch('/api/history?limit=100');
        if (!chatMessages) return;
        const messages = data.messages || [];
        // silent モード: メッセージID列が同じなら描画スキップ（差分なし）
        const idsKey = messages.map(m => `${m.id}:${m.starred ? 1 : 0}`).join(',');
        if (silent && idsKey === _lastChatMsgIds) return;
        _lastChatMsgIds = idsKey;
        // スクロール位置を保存
        const wasAtBottom = chatMessages.scrollHeight - chatMessages.scrollTop - chatMessages.clientHeight < 80;
        chatMessages.innerHTML = '<div class="chat-welcome"><h2>こんにちは。</h2><p>今日はどんなお手伝いをしましょうか？</p></div>';
        lastMsgDate = null;
        // 返信先の本文を引きやすいよう辞書化
        const idMap = new Map();
        messages.forEach(m => idMap.set(m.id, m));
        messages.forEach(m => {
            const replyContent = m.reply_to ? (idMap.get(m.reply_to)?.content || null) : null;
            appendMsg(m.role, m.content, m.timestamp, {
                id: m.id,
                starred: !!m.starred,
                replyContent,
            });
        });
        if (silent && !wasAtBottom) {
            // 元のスクロール位置を保つ（ユーザーが過去メッセージを読んでいる場合）
            // 何もしない: appendMsg が新規分だけスクロールしないようにする必要があるが、フル再描画のため割愛
        }
    } catch {}
}

let _chatPollTimer = null;
function _startChatAutoRefresh() {
    if (_chatPollTimer) return;
    _chatPollTimer = setInterval(() => {
        if (document.visibilityState === 'visible') loadHistory(true);
    }, 30_000);
    document.addEventListener('visibilitychange', () => {
        if (document.visibilityState === 'visible') loadHistory(true);
    });
    window.addEventListener('focus', () => loadHistory(true));
}

window.addEventListener('DOMContentLoaded', () => { if (apiKey) { showScreen('main-screen'); initMain(); } else { showScreen('login-screen'); } });

let _activeHabitsForGantt = [];
async function loadHabits() {
    try {
        const data = await apiFetch('/api/habits');
        const container = $('#dash-habits');
        if (!container) return;
        if (!data.habits || data.habits.length === 0) {
            container.innerHTML = '<div class="loading-placeholder">登録された習慣はありません。</div>';
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

            const weekdays = Array.isArray(h.weekdays) ? h.weekdays : [];
            const dowLabels = ['月','火','水','木','金','土','日'];
            const wdEncoded = encodeURIComponent(JSON.stringify(weekdays));
            const wdChip = weekdays.length === 0 || weekdays.length === 7
                ? `<button class="habit-trigger-add" title="曜日を指定" onclick="event.stopPropagation(); openHabitWeekdaysModal('${escapeHtml(h.name)}','${wdEncoded}')">📅 毎日</button>`
                : `<span class="habit-trigger-chip" title="クリックで変更" onclick="event.stopPropagation(); openHabitWeekdaysModal('${escapeHtml(h.name)}','${wdEncoded}')">📅 ${weekdays.map(d => dowLabels[d]).join('・')}</span>`;

            let freqChip = '';
            if (freq > 1) {
                const freqLabel = freq === 7 ? '週1回' : `${freq}日に1回`;
                const notDueStyle = !dueToday ? 'color:var(--text-muted);' : 'color:var(--accent);';
                freqChip = `<span style="font-size:0.68rem; padding:1px 5px; border-radius:3px; background:rgba(255,255,255,0.06); ${notDueStyle}">${freqLabel}${!dueToday ? ' (今日はお休み)' : ''}</span>`;
            } else if (weekdays.length > 0 && weekdays.length < 7 && !dueToday) {
                freqChip = `<span style="font-size:0.68rem; padding:1px 5px; border-radius:3px; background:rgba(255,255,255,0.06); color:var(--text-muted);">今日はお休み</span>`;
            }

            const dimmed = !dueToday && !isDone;
            return `
                <div class="habit-item ${isDone ? 'done' : ''}" id="habit-item-${h.id}" data-task-id="${h.task_id || ''}" data-name="${escapeHtml(h.name)}" style="${dimmed ? 'opacity:0.45;' : ''}">
                    <span class="habit-handle" style="cursor:grab;touch-action:none;color:var(--text-muted);font-size:1.1rem;padding:12px 10px;margin-left:-8px;user-select:none;" title="長押しして並び替え">⠿</span>
                    <button class="habit-check-btn" onclick="${isDone ? `uncompleteHabit('${h.name}', '${h.id}')` : `completeHabit('${h.name}', '${h.id}')`}" ${!isDone && !dueToday ? 'disabled' : ''} style="${isDone ? 'opacity:0.8;' : ''}">✔</button>
                    <div class="habit-name-wrap" style="flex:1; display:flex; flex-direction:column; gap:2px; min-width:0;">
                        <div class="habit-name">${escapeHtml(h.name)}${freqChip ? ' ' + freqChip : ''}</div>
                        <div class="habit-trigger-row" style="display:flex;gap:6px;flex-wrap:wrap;">${triggerChip}${wdChip}</div>
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

window.openHabitWeekdaysModal = (habitName, weekdaysEncoded) => {
    let current = [];
    try { current = JSON.parse(decodeURIComponent(weekdaysEncoded)) || []; } catch {}
    let modal = $('#habit-weekdays-modal');
    if (!modal) {
        const wrap = document.createElement('div');
        wrap.innerHTML = `
            <div id="habit-weekdays-modal" class="modal-overlay hidden">
                <div class="modal-card" style="max-width:380px;">
                    <h3 id="habit-weekdays-title" style="margin-top:0;">📅 対象の曜日</h3>
                    <p style="font-size:0.78rem;color:var(--text-muted);margin:0 0 10px;">チェックを外した曜日はリマインダーや「今日対象の習慣」から除外されます。すべて選択（または全解除）すると毎日対象になります。</p>
                    <div id="habit-weekdays-checks" style="display:flex;gap:6px;justify-content:space-between;margin-bottom:14px;flex-wrap:wrap;"></div>
                    <div class="modal-actions" style="display:flex;gap:8px;">
                        <button class="modal-btn cancel" onclick="closeHabitWeekdaysModal()">キャンセル</button>
                        <button class="modal-btn submit" onclick="saveHabitWeekdaysModal()">保存</button>
                    </div>
                </div>
            </div>
        `;
        document.body.appendChild(wrap.firstElementChild);
        modal = $('#habit-weekdays-modal');
    }
    $('#habit-weekdays-title').textContent = `📅 ${habitName} の対象曜日`;
    const checks = $('#habit-weekdays-checks');
    const labels = ['月','火','水','木','金','土','日'];
    checks.innerHTML = labels.map((lab, idx) => {
        const isAllDay = current.length === 0; // 空 = 毎日 = 全 ON 表示
        const checked = isAllDay || current.includes(idx) ? 'checked' : '';
        return `<label style="display:flex;flex-direction:column;align-items:center;gap:4px;cursor:pointer;font-size:0.84rem;">
            <input type="checkbox" class="habit-weekday-check" data-day="${idx}" ${checked}>
            <span>${lab}</span>
        </label>`;
    }).join('');
    modal.dataset.habitName = habitName;
    modal.classList.remove('hidden');
};

window.closeHabitWeekdaysModal = () => {
    $('#habit-weekdays-modal')?.classList.add('hidden');
};

window.saveHabitWeekdaysModal = async () => {
    const modal = $('#habit-weekdays-modal');
    if (!modal) return;
    const name = modal.dataset.habitName || '';
    const selected = Array.from(document.querySelectorAll('.habit-weekday-check'))
        .filter(c => c.checked).map(c => parseInt(c.dataset.day, 10))
        .filter(n => Number.isInteger(n));
    // 全選択 or 全解除はどちらも「毎日」として扱う（空配列で送信）
    const weekdays = (selected.length === 0 || selected.length === 7) ? [] : selected;
    try {
        await apiFetch('/api/habits/add', {
            method: 'POST',
            body: JSON.stringify({ name, frequency_days: 1, weekdays }),
        });
        showToast('曜日設定を保存しました');
        closeHabitWeekdaysModal();
        if (typeof loadHabits === 'function') loadHabits();
    } catch (e) {
        showToast('保存に失敗しました', true);
    }
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
            el.innerHTML = '<div class="loading-placeholder">フレーズはまだありません。<span class="muted-hint">メッセージを長押し → 📚 で保存できます。</span></div>';
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
    tabsEl.innerHTML = '<span style="font-size:0.78rem;color:var(--text-muted);">読み込み中…</span>';
    try {
        const data = await apiFetch('/api/messages/collections');
        const labels = data.collections || [];
        if (!labels.length) {
            tabsEl.innerHTML = '<div class="loading-placeholder">コレクションはまだありません</div>';
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
    results.innerHTML = '<p class="search-hint">読み込み中…</p>';
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

// ===========================================================
// 食事ログ (Meal log)
// ===========================================================

let _pendingMealAnalysis = null;

// ダッシュボードに今日の食事一覧を表示
window.loadMeals = async () => {
    const listEl = $('#dash-meals-list');
    const sumEl = $('#dash-meals-summary');
    if (!listEl) return;
    try {
        const data = await apiFetch('/api/meals');
        const meals = data.meals || [];
        const total = data.total || {};

        if (sumEl) {
            if (meals.length) {
                sumEl.innerHTML =
                    `本日合計 <b style="color:var(--text-primary);">${total.calories || 0}kcal</b> ` +
                    `(P${total.protein_g || 0} / F${total.fat_g || 0} / C${total.carbs_g || 0})`;
            } else {
                sumEl.innerHTML = '本日の記録はまだありません。';
            }
        }

        if (!meals.length) {
            listEl.innerHTML = '<div class="loading-placeholder">食事が記録されていません。<span class="muted-hint">「📷 写真」または「✏️ 手動」から追加。</span></div>';
            return;
        }

        listEl.innerHTML = meals.map(m => {
            const memoSafe = escapeHtml(m.memo || '');
            const memoHtml = memoSafe ? `<div style="font-size:0.74rem;color:var(--text-muted);margin-top:2px;">${memoSafe}</div>` : '';
            const adviceHtml = m.advice ? `<div style="font-size:0.74rem;color:var(--accent);margin-top:2px;">💬 ${escapeHtml(m.advice)}</div>` : '';
            return `
                <div class="invest-row" style="cursor:default;">
                    <div class="row-main" style="flex:1;">
                        <div class="row-title">${escapeHtml(m.time)} ${escapeHtml(m.name)}</div>
                        <div class="row-sub" style="font-size:0.78rem;color:var(--text-secondary);">
                            ${m.calories || 0}kcal ・ P${m.protein_g || 0} / F${m.fat_g || 0} / C${m.carbs_g || 0}
                        </div>
                        ${memoHtml}${adviceHtml}
                    </div>
                    <div class="row-actions" style="display:flex;gap:6px;">
                        <button class="mini-link" onclick="openMealManualModal(${m.id})">編集</button>
                        <button class="mini-link" onclick="deleteMeal(${m.id})" style="color:#ff6b6b;">削除</button>
                    </div>
                </div>
            `;
        }).join('');
    } catch {
        listEl.innerHTML = '<div class="loading-placeholder">読み込みに失敗しました。</div>';
    }
};

// 写真撮影 → Vision で解析 → 編集モーダル
window.openMealCaptureModal = () => {
    const input = $('#meal-image-input');
    if (!input) return;
    input.value = '';
    input.click();
};

// 食事画像 input の change ハンドラ（モーダル初回オープン時に登録）
let _mealImageListenerInstalled = false;
function _installMealImageListener() {
    if (_mealImageListenerInstalled) return;
    const input = $('#meal-image-input');
    if (!input) return;
    input.addEventListener('change', async () => {
        const file = (input.files || [])[0];
        if (!file) return;
        showToast('📷 食事を解析中…（数秒）');
        try {
            const base64 = await _fileToBase64(file);
            const res = await apiFetch('/api/meals/analyze', {
                method: 'POST',
                body: JSON.stringify({ image_base64: base64, mime_type: file.type || 'image/jpeg' }),
            });
            if (!res || !res.ok) {
                showToast('解析に失敗しました', true);
                return;
            }
            _pendingMealAnalysis = res.result || {};
            openMealManualModal(null, _pendingMealAnalysis);
        } catch (e) {
            showToast('解析に失敗しました', true);
        } finally {
            input.value = '';
        }
    });
    _mealImageListenerInstalled = true;
}

// 手動入力 / 編集モーダル
// id=null & seed=オブジェクト で「写真解析結果からの新規」モード
window.openMealManualModal = async (id = null, seed = null) => {
    let modal = $('#meal-edit-modal');
    if (!modal) {
        const wrap = document.createElement('div');
        wrap.innerHTML = `
            <div id="meal-edit-modal" class="modal-overlay hidden">
                <div class="modal-card" style="max-width:520px;max-height:90vh;overflow-y:auto;">
                    <h3 id="meal-edit-title" style="margin-top:0;">🍽 食事を記録</h3>
                    <p id="meal-edit-confidence" style="font-size:0.74rem;color:var(--text-muted);margin:-4px 0 8px;"></p>

                    <label style="font-size:0.78rem;color:var(--text-muted);">料理名</label>
                    <input id="meal-name" class="modern-input" style="margin-bottom:8px;" placeholder="例: 唐揚げ定食">

                    <div style="display:flex;gap:8px;margin-bottom:8px;">
                        <div style="flex:1;">
                            <label style="font-size:0.78rem;color:var(--text-muted);">時刻</label>
                            <input id="meal-time" type="time" class="modern-input">
                        </div>
                        <div style="flex:1;">
                            <label style="font-size:0.78rem;color:var(--text-muted);">区分</label>
                            <select id="meal-type" class="modern-input">
                                <option value="">未指定</option>
                                <option value="breakfast">朝食</option>
                                <option value="lunch">昼食</option>
                                <option value="dinner">夕食</option>
                                <option value="snack">間食</option>
                            </select>
                        </div>
                    </div>

                    <div style="display:flex;gap:6px;margin-bottom:8px;">
                        <div style="flex:1;">
                            <label style="font-size:0.78rem;color:var(--text-muted);">カロリー(kcal)</label>
                            <input id="meal-kcal" type="number" min="0" class="modern-input">
                        </div>
                        <div style="flex:1;">
                            <label style="font-size:0.78rem;color:var(--text-muted);">P (g)</label>
                            <input id="meal-p" type="number" step="0.1" min="0" class="modern-input">
                        </div>
                        <div style="flex:1;">
                            <label style="font-size:0.78rem;color:var(--text-muted);">F (g)</label>
                            <input id="meal-f" type="number" step="0.1" min="0" class="modern-input">
                        </div>
                        <div style="flex:1;">
                            <label style="font-size:0.78rem;color:var(--text-muted);">C (g)</label>
                            <input id="meal-c" type="number" step="0.1" min="0" class="modern-input">
                        </div>
                    </div>

                    <label style="font-size:0.78rem;color:var(--text-muted);">メモ（任意）</label>
                    <textarea id="meal-memo" class="modern-input" rows="2" style="font-family:inherit;margin-bottom:8px;"></textarea>

                    <div class="modal-actions" style="margin-top:10px;">
                        <button class="modal-btn cancel" onclick="closeMealEditModal()">キャンセル</button>
                        <button class="modal-btn submit" id="meal-save-btn" onclick="saveMealFromModal()">保存</button>
                    </div>
                </div>
            </div>
        `;
        document.body.appendChild(wrap.firstElementChild);
        modal = $('#meal-edit-modal');
    }

    // 値を流し込む
    const titleEl = $('#meal-edit-title');
    const confEl = $('#meal-edit-confidence');
    const saveBtn = $('#meal-save-btn');
    saveBtn.dataset.mealId = id || '';

    let m = {};
    if (id) {
        // 既存編集 → サーバから取得して反映
        try {
            const data = await apiFetch('/api/meals');
            m = (data.meals || []).find(x => x.id === id) || {};
            titleEl.textContent = '🍽 食事を編集';
            confEl.textContent = '';
        } catch {
            m = {};
        }
    } else {
        const now = new Date();
        m = seed ? { ...seed } : {};
        // 解析結果のフィールド名揺れ吸収
        m.time = m.time || `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;
        if (seed) {
            // レシート/写真解析（confidence あり）か、チャットからの登録かでタイトルを切替
            titleEl.textContent = seed.confidence ? '🍽 解析結果を確認' : '🍽 食事を記録';
            confEl.textContent = seed.confidence ? `信頼度: ${seed.confidence}` : '';
        } else {
            titleEl.textContent = '🍽 食事を記録';
            confEl.textContent = '';
        }
    }

    $('#meal-name').value = m.name || '';
    $('#meal-time').value = m.time || '';
    $('#meal-type').value = m.meal_type || '';
    $('#meal-kcal').value = m.calories ?? '';
    $('#meal-p').value = m.protein_g ?? '';
    $('#meal-f').value = m.fat_g ?? '';
    $('#meal-c').value = m.carbs_g ?? '';
    $('#meal-memo').value = m.memo || '';

    modal.classList.remove('hidden');
};

window.closeMealEditModal = () => {
    $('#meal-edit-modal')?.classList.add('hidden');
    _pendingMealAnalysis = null;
};

window.saveMealFromModal = async () => {
    const btn = $('#meal-save-btn');
    const id = btn?.dataset.mealId ? parseInt(btn.dataset.mealId, 10) : null;
    const payload = {
        name: ($('#meal-name')?.value || '').trim(),
        time: $('#meal-time')?.value || '',
        meal_type: $('#meal-type')?.value || '',
        calories: parseInt($('#meal-kcal')?.value || '0', 10) || 0,
        protein_g: parseFloat($('#meal-p')?.value || '0') || 0,
        fat_g: parseFloat($('#meal-f')?.value || '0') || 0,
        carbs_g: parseFloat($('#meal-c')?.value || '0') || 0,
        memo: ($('#meal-memo')?.value || '').trim(),
    };
    if (!payload.name) { showToast('料理名を入力してください', true); return; }
    try {
        if (id) {
            await apiFetch(`/api/meals/${id}`, { method: 'PATCH', body: JSON.stringify(payload) });
            showToast('食事を更新しました');
        } else {
            await apiFetch('/api/meals', { method: 'POST', body: JSON.stringify(payload) });
            showToast('食事を記録しました');
        }
        closeMealEditModal();
        loadMeals();
    } catch {
        showToast('保存に失敗しました', true);
    }
};

window.deleteMeal = async (id) => {
    if (!confirm('この食事ログを削除しますか？')) return;
    try {
        await apiFetch(`/api/meals/${id}`, { method: 'DELETE' });
        showToast('削除しました');
        loadMeals();
    } catch {
        showToast('削除に失敗しました', true);
    }
};

window.requestMealAdvice = async () => {
    showToast('💬 マネージャーが分析中…');
    try {
        const res = await apiFetch('/api/meals/advice', { method: 'POST' });
        if (!res || !res.ok) {
            showToast(res?.error || 'アドバイス取得に失敗しました', true);
            return;
        }
        const advice = res.advice || '（アドバイス無し）';
        // アドバイスを設定モーダルライクに表示
        let modal = $('#meal-advice-modal');
        if (!modal) {
            const wrap = document.createElement('div');
            wrap.innerHTML = `
                <div id="meal-advice-modal" class="modal-overlay hidden">
                    <div class="modal-card" style="max-width:520px;">
                        <h3 style="margin-top:0;">💬 今日の栄養アドバイス</h3>
                        <div id="meal-advice-body" style="white-space:pre-wrap;font-size:0.88rem;line-height:1.7;color:var(--text-primary);"></div>
                        <div class="modal-actions" style="margin-top:14px;">
                            <button class="modal-btn cancel" onclick="$('#meal-advice-modal')?.classList.add('hidden')">閉じる</button>
                        </div>
                    </div>
                </div>
            `;
            document.body.appendChild(wrap.firstElementChild);
            modal = $('#meal-advice-modal');
        }
        $('#meal-advice-body').textContent = advice;
        modal.classList.remove('hidden');
    } catch {
        showToast('アドバイス取得に失敗しました', true);
    }
};

// 過去のメニューと空白期間を踏まえた献立提案
window.openMealSuggestModal = async () => {
    let modal = $('#meal-suggest-modal');
    if (!modal) {
        const wrap = document.createElement('div');
        wrap.innerHTML = `
            <div id="meal-suggest-modal" class="modal-overlay hidden">
                <div class="modal-card" style="max-width:520px;">
                    <h3 style="margin-top:0;">🍳 献立の提案</h3>
                    <div id="meal-suggest-body" style="white-space:pre-wrap;font-size:0.88rem;line-height:1.7;color:var(--text-primary);"></div>
                    <div class="modal-actions" style="margin-top:14px;">
                        <button class="modal-btn cancel" onclick="$('#meal-suggest-modal')?.classList.add('hidden')">閉じる</button>
                        <button class="modal-btn submit" id="meal-suggest-again" onclick="openMealSuggestModal()">もう一度提案</button>
                    </div>
                </div>
            </div>
        `;
        document.body.appendChild(wrap.firstElementChild);
        modal = $('#meal-suggest-modal');
    }
    const bodyEl = $('#meal-suggest-body');
    bodyEl.textContent = '🍳 過去のメニューを見て考え中…';
    modal.classList.remove('hidden');
    try {
        const res = await apiFetch('/api/meals/suggest', { method: 'POST' });
        if (!res || !res.ok) {
            bodyEl.textContent = res?.error || '提案の取得に失敗しました。';
            return;
        }
        bodyEl.textContent = res.suggestion || '（提案なし）';
    } catch {
        bodyEl.textContent = '提案の取得に失敗しました。';
    }
};

// ===========================================================
// Gmail インボックス
// ===========================================================

let currentGmailState = 'pending';
const GMAIL_STATE_LABELS = {
    pending: '📬 未処理',
    archived: '✅ 既読',
    trashed: '🗑 ゴミ箱',
    all: '📁 全て',
};

window.toggleGmailState = () => {
    const order = ['pending', 'archived', 'trashed', 'all'];
    const idx = order.indexOf(currentGmailState);
    currentGmailState = order[(idx + 1) % order.length];
    const btn = $('#gmail-state-toggle');
    if (btn) btn.textContent = GMAIL_STATE_LABELS[currentGmailState];
    loadGmailInbox(currentGmailState);
};

window.loadGmailInbox = async (state = 'pending') => {
    currentGmailState = state || 'pending';
    const btn = $('#gmail-state-toggle');
    if (btn) btn.textContent = GMAIL_STATE_LABELS[currentGmailState] || '📬 未処理';
    const listEl = $('#dash-gmail-list');
    if (!listEl) return;
    try {
        const data = await apiFetch(`/api/gmail/inbox?state=${encodeURIComponent(currentGmailState)}&limit=50`);
        const items = data.items || [];
        if (!items.length) {
            const empty = currentGmailState === 'pending'
                ? '新着メールはありません。'
                : '該当するメールはありません。';
            listEl.innerHTML = `<div class="loading-placeholder">${empty}<span class="muted-hint">「📥 取り込み」で Gmail を再ポーリングできます。</span></div>`;
            return;
        }
        listEl.innerHTML = items.map(m => {
            const importanceColor = m.importance === 'high' ? '#ff6b6b'
                : m.importance === 'low' ? 'var(--text-muted)' : 'var(--accent)';
            const importanceLabel = m.importance === 'high' ? '🔴 重要'
                : m.importance === 'low' ? '🟢 軽め' : '🟡 通常';
            const fromShort = escapeHtml((m.from_addr || '').split('<')[0].trim().slice(0, 32));
            const subjectSafe = escapeHtml(m.subject || '(件名なし)');
            const summarySafe = escapeHtml(m.summary || m.snippet || '');
            const received = m.received_at ? escapeHtml(m.received_at.replace('T', ' ').slice(5, 16)) : '';
            const idAttr = escapeHtml(m.id);
            const threadAttr = escapeHtml(m.thread_id || '');
            const savedBadge = m.saved_drive_id
                ? '<span style="background:rgba(0,186,152,0.18);color:var(--accent);font-size:0.7rem;padding:1px 6px;border-radius:8px;margin-left:6px;">📌 保存済み</span>'
                : '';
            const saveBtn = m.saved_drive_id
                ? `<button class="mini-link" onclick="openSavedEmail('${escapeHtml(m.saved_drive_id)}')" title="保存先を開く" style="color:var(--accent);">📂 保存先</button>`
                : `<button class="mini-link" onclick="saveGmailToObsidian('${idAttr}')" title="重要メールとして保存">📌 保存</button>`;
            const actions = currentGmailState === 'pending'
                ? `
                    ${saveBtn}
                    <button class="mini-link" onclick="markGmailRead('${idAttr}')" title="既読 / アーカイブ">📥 アーカイブ</button>
                    <button class="mini-link" onclick="trashGmail('${idAttr}')" title="ゴミ箱へ" style="color:#ff6b6b;">🗑 ゴミ箱</button>
                `
                : `${saveBtn}`;
            return `
                <div class="invest-row" style="cursor:default;align-items:flex-start;">
                    <div class="row-main" style="flex:1;min-width:0;">
                        <div class="row-title" style="display:flex;gap:6px;align-items:baseline;flex-wrap:wrap;">
                            <span style="font-size:0.74rem;color:${importanceColor};font-weight:600;flex-shrink:0;">${importanceLabel}</span>
                            <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0;">${subjectSafe}</span>
                            ${savedBadge}
                        </div>
                        <div class="row-sub" style="font-size:0.74rem;color:var(--text-muted);margin-top:2px;">
                            ${fromShort} ・ ${received}
                        </div>
                        <div style="font-size:0.8rem;color:var(--text-secondary);margin-top:3px;line-height:1.4;">
                            ${summarySafe}
                        </div>
                    </div>
                    <div class="row-actions" style="display:flex;flex-direction:column;gap:4px;flex-shrink:0;">
                        ${actions}
                        <button class="mini-link" onclick="openGmail('${idAttr}', '${threadAttr}')" title="Gmail で開く">↗ Gmail</button>
                    </div>
                </div>
            `;
        }).join('');
    } catch {
        listEl.innerHTML = '<div class="loading-placeholder">読み込みに失敗しました。</div>';
    }
};

window.refreshGmailServer = async () => {
    showToast('📥 Gmail を再ポーリング中…');
    try {
        const res = await apiFetch('/api/gmail/refresh', { method: 'POST' });
        if (res && res.ok) {
            showToast('取り込みが完了しました');
            loadGmailInbox(currentGmailState);
        } else {
            showToast(res?.error || '取り込みに失敗しました', true);
        }
    } catch {
        showToast('取り込みに失敗しました', true);
    }
};

window.markGmailRead = async (id) => {
    try {
        await apiFetch(`/api/gmail/${encodeURIComponent(id)}/read`, { method: 'POST' });
        showToast('アーカイブしました');
        loadGmailInbox(currentGmailState);
    } catch {
        showToast('アーカイブに失敗しました', true);
    }
};

window.trashGmail = async (id) => {
    if (!confirm('このメールをゴミ箱に移動しますか？（Gmail 側にも反映されます）')) return;
    try {
        await apiFetch(`/api/gmail/${encodeURIComponent(id)}/trash`, { method: 'POST' });
        showToast('ゴミ箱に移動しました');
        loadGmailInbox(currentGmailState);
    } catch {
        showToast('削除に失敗しました', true);
    }
};

window.openGmail = (id, threadId = '') => {
    if (!id && !threadId) return;
    // Gmail のディープリンクは thread_id ベースの方が確実に該当スレッドへ飛ぶ
    const target = threadId || id;
    const webUrl = `https://mail.google.com/mail/u/0/#all/${encodeURIComponent(target)}`;
    const isMobile = /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
    if (!isMobile) {
        window.open(webUrl, '_blank', 'noopener');
        return;
    }
    // モバイル: Gmail アプリ起動を試みる。アプリ未インストールなら 1.5 秒後に Web 版へフォールバック
    const startedAt = Date.now();
    const fallbackTimer = setTimeout(() => {
        // ページが visible のままならアプリ起動失敗とみなして Web 版へ
        if (document.visibilityState === 'visible' && Date.now() - startedAt < 2500) {
            window.open(webUrl, '_blank', 'noopener');
        }
    }, 1500);
    // visibility が変わった (= アプリへ遷移した) らフォールバックをキャンセル
    const onVisChange = () => {
        if (document.visibilityState === 'hidden') {
            clearTimeout(fallbackTimer);
            document.removeEventListener('visibilitychange', onVisChange);
        }
    };
    document.addEventListener('visibilitychange', onVisChange);
    // Gmail アプリの search クエリで該当メッセージを開く
    location.href = `googlegmail://search?q=rfc822msgid:${encodeURIComponent(id || target)}`;
};

window.saveGmailToObsidian = async (id) => {
    if (!id) return;
    showToast('📌 Obsidian (Drive) に保存中…');
    try {
        const res = await apiFetch(`/api/gmail/${encodeURIComponent(id)}/save`, { method: 'POST' });
        if (res && res.ok) {
            showToast(res.already_saved ? 'すでに保存済みです' : '保存しました');
            loadGmailInbox(currentGmailState);
        } else {
            showToast(res?.error || '保存に失敗しました', true);
        }
    } catch {
        showToast('保存に失敗しました', true);
    }
};

window.openSavedEmail = (driveId) => {
    if (!driveId) return;
    window.open(`https://drive.google.com/file/d/${encodeURIComponent(driveId)}/view`, '_blank', 'noopener');
};

// ===========================================================
// 支出メモ (Expenses)
// ===========================================================

let _pendingReceiptAnalysis = null;
let _pendingReceiptDriveId = '';
let _pendingReceiptBase64 = null;
let _pendingReceiptMime = '';
let _expenseCurrentYear = null;
let _expenseCurrentMonth = null;
const EXPENSE_CATEGORIES_FALLBACK = ['食費','交通費','娯楽','衣服','家電','医療','教育','通信','光熱費','投資','その他'];

window.loadExpenses = async (year = null, month = null) => {
    const listEl = $('#dash-expense-list');
    const sumEl = $('#dash-expense-summary');
    if (!listEl) return;
    try {
        const qs = year && month ? `?year=${year}&month=${month}` : '';
        const data = await apiFetch('/api/expenses' + qs);
        _expenseCurrentYear = data.year;
        _expenseCurrentMonth = data.month;
        const expenses = data.expenses || [];
        const total = data.total || 0;
        const threshold = data.large_threshold || 5000;

        if (sumEl) {
            const byCat = (data.by_category || []).slice(0, 6).map(c =>
                `<span style="font-size:0.74rem;color:var(--text-muted);margin-right:6px;">${escapeHtml(c.category)} ¥${c.amount.toLocaleString()}</span>`
            ).join('');
            sumEl.innerHTML = `
                <div style="display:flex;justify-content:space-between;align-items:baseline;">
                    <span style="font-size:0.78rem;color:var(--text-muted);">${data.year}年${data.month}月 合計</span>
                    <span style="font-size:1.2rem;font-weight:700;color:var(--text-primary);">¥${total.toLocaleString()}</span>
                </div>
                <div style="margin-top:4px;">${byCat}</div>
                <div style="font-size:0.7rem;color:var(--text-muted);margin-top:4px;">大きな支出の閾値: ¥${threshold.toLocaleString()}</div>
            `;
        }

        if (!expenses.length) {
            listEl.innerHTML = '<div class="loading-placeholder">記録された支出はまだありません。<span class="muted-hint">「📷 レシート」または「✏️ 手動」から追加。</span></div>';
            return;
        }

        listEl.innerHTML = expenses.map(e => {
            const bigBadge = e.is_large ? '<span style="background:rgba(255,107,107,0.15);color:#ff6b6b;font-size:0.7rem;padding:1px 6px;border-radius:8px;margin-left:6px;">大きな支出</span>' : '';
            const memoSafe = escapeHtml(e.memo || '');
            const memoHtml = memoSafe ? `<div style="font-size:0.74rem;color:var(--text-muted);margin-top:2px;">${memoSafe}</div>` : '';
            const breakdownHtml = e.breakdown
                ? `<details style="margin-top:2px;"><summary style="font-size:0.74rem;color:var(--text-muted);cursor:pointer;">内訳</summary><div style="font-size:0.74rem;color:var(--text-muted);white-space:pre-wrap;margin-top:2px;">${escapeHtml(e.breakdown)}</div></details>`
                : '';
            const pmSafe = e.payment_method ? escapeHtml(e.payment_method) : '';
            const receiptBtn = e.receipt_drive_id
                ? `<button class="mini-link" onclick="viewReceipt('${escapeHtml(e.receipt_drive_id)}')" title="レシート表示">📷</button>`
                : '';
            return `
                <div class="invest-row" style="cursor:default;">
                    <div class="row-main" style="flex:1;">
                        <div class="row-title">
                            ¥${e.amount.toLocaleString()} ${escapeHtml(e.vendor || e.category)}${bigBadge}
                        </div>
                        <div class="row-sub" style="font-size:0.78rem;color:var(--text-secondary);">
                            ${escapeHtml(e.date)} ・ ${escapeHtml(e.category)}${pmSafe ? ' ・ ' + pmSafe : ''}
                        </div>
                        ${memoHtml}
                        ${breakdownHtml}
                    </div>
                    <div class="row-actions" style="display:flex;gap:6px;">
                        ${receiptBtn}
                        <button class="mini-link" onclick="openExpenseManualModal(${e.id})">編集</button>
                        <button class="mini-link" onclick="deleteExpense(${e.id})" style="color:#ff6b6b;">削除</button>
                    </div>
                </div>
            `;
        }).join('');
    } catch {
        listEl.innerHTML = '<div class="loading-placeholder">読み込みに失敗しました。</div>';
    }
};

window.openExpenseCaptureModal = () => {
    const input = $('#expense-image-input');
    if (!input) return;
    input.value = '';
    input.click();
};

let _expenseImageListenerInstalled = false;
function _installExpenseImageListener() {
    if (_expenseImageListenerInstalled) return;
    const input = $('#expense-image-input');
    if (!input) return;
    input.addEventListener('change', async () => {
        const file = (input.files || [])[0];
        if (!file) return;
        showToast('🧾 レシートを解析中…');
        try {
            const base64 = await _fileToBase64(file);
            _pendingReceiptBase64 = base64;
            _pendingReceiptMime = file.type || 'image/jpeg';
            _pendingReceiptDriveId = '';
            const res = await apiFetch('/api/expenses/analyze', {
                method: 'POST',
                body: JSON.stringify({ image_base64: base64, mime_type: _pendingReceiptMime }),
            });
            if (!res || !res.ok) {
                showToast('解析に失敗しました', true);
                return;
            }
            _pendingReceiptAnalysis = res.result || {};
            openExpenseManualModal(null, _pendingReceiptAnalysis);
        } catch (e) {
            showToast('解析に失敗しました', true);
        } finally {
            input.value = '';
        }
    });
    _expenseImageListenerInstalled = true;
}

window.openExpenseManualModal = async (id = null, seed = null) => {
    let modal = $('#expense-edit-modal');
    if (!modal) {
        const wrap = document.createElement('div');
        wrap.innerHTML = `
            <div id="expense-edit-modal" class="modal-overlay hidden">
                <div class="modal-card" style="max-width:520px;max-height:90vh;overflow-y:auto;">
                    <h3 id="expense-edit-title" style="margin-top:0;">💴 支出を記録</h3>
                    <p id="expense-edit-confidence" style="font-size:0.74rem;color:var(--text-muted);margin:-4px 0 8px;"></p>

                    <div style="display:flex;gap:8px;margin-bottom:8px;">
                        <div style="flex:1;">
                            <label style="font-size:0.78rem;color:var(--text-muted);">日付</label>
                            <input id="exp-date" type="date" class="modern-input">
                        </div>
                        <div style="flex:1;">
                            <label style="font-size:0.78rem;color:var(--text-muted);">金額 (円)</label>
                            <input id="exp-amount" type="number" min="0" class="modern-input">
                        </div>
                    </div>

                    <label style="font-size:0.78rem;color:var(--text-muted);">店名 / 内容</label>
                    <input id="exp-vendor" class="modern-input" style="margin-bottom:8px;" placeholder="例: イオン">

                    <div style="display:flex;gap:8px;margin-bottom:8px;">
                        <div style="flex:1;">
                            <label style="font-size:0.78rem;color:var(--text-muted);">カテゴリ</label>
                            <select id="exp-category" class="modern-input"></select>
                        </div>
                        <div style="flex:1;">
                            <label style="font-size:0.78rem;color:var(--text-muted);">支払方法</label>
                            <select id="exp-payment" class="modern-input">
                                <option value="">未指定</option>
                                <option value="現金">現金</option>
                                <option value="クレジット">クレジット</option>
                                <option value="電子マネー">電子マネー</option>
                                <option value="QR">QR</option>
                                <option value="銀行振込">銀行振込</option>
                                <option value="不明">不明</option>
                            </select>
                        </div>
                    </div>

                    <label style="font-size:0.78rem;color:var(--text-muted);">内訳（任意・1行に1件「品名 ¥金額」）</label>
                    <textarea id="exp-breakdown" class="modern-input" rows="3" style="font-family:inherit;margin-bottom:6px;" placeholder="例:&#10;書籍A ¥1200&#10;ケーブル ¥800"></textarea>

                    <label style="font-size:0.78rem;color:var(--text-muted);">メモ（任意）</label>
                    <textarea id="exp-memo" class="modern-input" rows="2" style="font-family:inherit;margin-bottom:6px;"></textarea>

                    <div id="exp-receipt-hint" style="font-size:0.74rem;color:var(--text-muted);margin-bottom:8px;"></div>

                    <div class="modal-actions" style="margin-top:10px;">
                        <button class="modal-btn cancel" onclick="closeExpenseEditModal()">キャンセル</button>
                        <button class="modal-btn submit" id="exp-save-btn" onclick="saveExpenseFromModal()">保存</button>
                    </div>
                </div>
            </div>
        `;
        document.body.appendChild(wrap.firstElementChild);
        modal = $('#expense-edit-modal');
    }

    // カテゴリの選択肢を埋める（サーバから取得済みのカテゴリリストを使う）
    const catEl = $('#exp-category');
    if (catEl && !catEl.options.length) {
        EXPENSE_CATEGORIES_FALLBACK.forEach(c => {
            const opt = document.createElement('option');
            opt.value = c; opt.textContent = c;
            catEl.appendChild(opt);
        });
    }

    const titleEl = $('#expense-edit-title');
    const confEl = $('#expense-edit-confidence');
    const saveBtn = $('#exp-save-btn');
    saveBtn.dataset.expenseId = id || '';

    let e = {};
    if (id) {
        try {
            const data = await apiFetch('/api/expenses');
            e = (data.expenses || []).find(x => x.id === id) || {};
            titleEl.textContent = '💴 支出を編集';
            confEl.textContent = '';
        } catch { e = {}; }
    } else {
        const now = new Date();
        e = seed ? { ...seed } : {};
        e.date = e.date || now.toISOString().slice(0, 10);
        if (seed) {
            titleEl.textContent = '💴 レシート解析結果を確認';
            confEl.textContent = seed.confidence ? `信頼度: ${seed.confidence}` : '';
        } else {
            titleEl.textContent = '💴 支出を記録';
            confEl.textContent = '';
        }
    }

    // 内訳: 編集時は breakdown 列、レシート解析時は items 配列から組み立てる
    let breakdownText = e.breakdown || '';
    if (!breakdownText && Array.isArray(e.items) && e.items.length) {
        breakdownText = e.items
            .map(it => `${(it.name || '品目').trim()} ¥${Number(it.amount || 0).toLocaleString()}`)
            .join('\n');
    }

    $('#exp-date').value = e.date || '';
    $('#exp-amount').value = e.amount ?? '';
    $('#exp-vendor').value = e.vendor || '';
    $('#exp-category').value = e.category || 'その他';
    $('#exp-payment').value = e.payment_method || '';
    $('#exp-breakdown').value = breakdownText;
    $('#exp-memo').value = e.memo || '';

    const hintEl = $('#exp-receipt-hint');
    if (hintEl) {
        if (_pendingReceiptBase64 && !id) {
            hintEl.textContent = '🧾 撮影したレシート画像も保存時に Drive へアップロードされます。';
        } else if (e.receipt_drive_id) {
            hintEl.textContent = '🧾 既にレシート画像が保存されています。';
        } else {
            hintEl.textContent = '';
        }
    }

    modal.classList.remove('hidden');
};

window.closeExpenseEditModal = () => {
    $('#expense-edit-modal')?.classList.add('hidden');
    _pendingReceiptAnalysis = null;
    _pendingReceiptBase64 = null;
    _pendingReceiptMime = '';
    _pendingReceiptDriveId = '';
};

window.saveExpenseFromModal = async () => {
    const btn = $('#exp-save-btn');
    const id = btn?.dataset.expenseId ? parseInt(btn.dataset.expenseId, 10) : null;
    const amount = parseInt($('#exp-amount')?.value || '0', 10) || 0;
    if (amount <= 0) { showToast('金額を入力してください', true); return; }

    btn.disabled = true;
    btn.textContent = '保存中…';
    try {
        let receiptDriveId = _pendingReceiptDriveId;
        // 新規 + レシート画像があれば Drive にアップロード
        if (!id && _pendingReceiptBase64 && !receiptDriveId) {
            try {
                const up = await apiFetch('/api/expenses/receipt_upload', {
                    method: 'POST',
                    body: JSON.stringify({
                        image_base64: _pendingReceiptBase64,
                        mime_type: _pendingReceiptMime || 'image/jpeg',
                        date: $('#exp-date')?.value || '',
                    }),
                });
                if (up && up.ok) receiptDriveId = up.drive_id || '';
            } catch {}
        }

        const payload = {
            amount,
            date: $('#exp-date')?.value || '',
            vendor: ($('#exp-vendor')?.value || '').trim(),
            category: $('#exp-category')?.value || 'その他',
            payment_method: $('#exp-payment')?.value || '',
            memo: ($('#exp-memo')?.value || '').trim(),
            breakdown: ($('#exp-breakdown')?.value || '').trim(),
        };
        if (id) {
            await apiFetch(`/api/expenses/${id}`, { method: 'PATCH', body: JSON.stringify(payload) });
            showToast('支出を更新しました');
        } else {
            payload.receipt_drive_id = receiptDriveId || '';
            const res = await apiFetch('/api/expenses', { method: 'POST', body: JSON.stringify(payload) });
            if (res?.is_large) {
                showToast(`💴 大きな支出として記録 (¥${amount.toLocaleString()})`);
            } else {
                showToast('支出を記録しました');
            }
        }
        closeExpenseEditModal();
        loadExpenses(_expenseCurrentYear, _expenseCurrentMonth);
    } catch {
        showToast('保存に失敗しました', true);
    } finally {
        btn.disabled = false;
        btn.textContent = '保存';
    }
};

window.deleteExpense = async (id) => {
    if (!confirm('この支出を削除しますか？')) return;
    try {
        await apiFetch(`/api/expenses/${id}`, { method: 'DELETE' });
        showToast('削除しました');
        loadExpenses(_expenseCurrentYear, _expenseCurrentMonth);
    } catch {
        showToast('削除に失敗しました', true);
    }
};

window.viewReceipt = (driveId) => {
    if (!driveId) return;
    window.open(`https://drive.google.com/file/d/${encodeURIComponent(driveId)}/view`, '_blank', 'noopener');
};

window.openExpenseThresholdEditor = async () => {
    try {
        const cur = await apiFetch('/api/expenses/threshold');
        const current = cur?.threshold_jpy || 5000;
        const next = prompt('大きな支出の閾値（円）を入力してください。この金額以上のときに通知＆ライフログ追記されます。', String(current));
        if (next === null) return;
        const v = parseInt(String(next).replace(/[^\d]/g, ''), 10);
        if (isNaN(v) || v < 0) { showToast('正の整数を入力してください', true); return; }
        await apiFetch('/api/expenses/threshold', { method: 'POST', body: JSON.stringify({ threshold_jpy: v }) });
        showToast(`閾値を ¥${v.toLocaleString()} に変更しました`);
        loadExpenses(_expenseCurrentYear, _expenseCurrentMonth);
    } catch {
        showToast('閾値の更新に失敗しました', true);
    }
};

// ----- 朝のマネージャー MIT 提案モーダル -----
let _morningMitQid = null;

window.checkMorningMit = async (autoOpen = false) => {
    try {
        const res = await apiFetch('/api/morning_mit/pending');
        if (!res || !res.candidates || res.candidates.length === 0) return false;
        _morningMitQid = res.qid;
        if (autoOpen) {
            window.openMorningMitModal(res.candidates);
        }
        return true;
    } catch {
        return false;
    }
};

window.openMorningMitModal = (candidates) => {
    let modal = $('#morning-mit-modal');
    if (!modal) {
        const wrap = document.createElement('div');
        wrap.innerHTML = `
            <div id="morning-mit-modal" class="modal-overlay hidden">
                <div class="modal-card" style="max-width:520px;">
                    <h3 style="margin-top:0;">☀️ 今朝のMIT候補</h3>
                    <p style="font-size:0.8rem;color:var(--text-muted);margin:-4px 0 12px;">カレンダー予定と昨日の進捗から提案。編集して「確定」を押すと今日のMITになります。</p>
                    <div id="morning-mit-inputs"></div>
                    <div class="modal-actions" style="margin-top:14px;">
                        <button class="modal-btn cancel" onclick="closeMorningMitModal()">あとで</button>
                        <button class="modal-btn submit" onclick="confirmMorningMit()">確定</button>
                    </div>
                </div>
            </div>
        `;
        document.body.appendChild(wrap.firstElementChild);
        modal = $('#morning-mit-modal');
    }
    const inputsEl = $('#morning-mit-inputs');
    const padded = [...(candidates || []), '', '', ''].slice(0, 3);
    inputsEl.innerHTML = padded.map((c, i) => `
        <label style="font-size:0.78rem;color:var(--text-muted);">MIT ${i + 1}</label>
        <input class="modern-input morning-mit-input" style="margin-bottom:8px;" value="${(c || '').replace(/"/g, '&quot;')}" placeholder="MIT を入力">
    `).join('');
    modal.classList.remove('hidden');
};

window.closeMorningMitModal = () => {
    $('#morning-mit-modal')?.classList.add('hidden');
};

window.confirmMorningMit = async () => {
    const inputs = document.querySelectorAll('.morning-mit-input');
    const items = Array.from(inputs).map(i => i.value.trim()).filter(Boolean);
    if (!items.length) { showToast('MIT を 1 件以上入力してください', true); return; }
    try {
        const res = await apiFetch('/api/morning_mit/confirm', {
            method: 'POST',
            body: JSON.stringify({ items, qid: _morningMitQid })
        });
        if (res && res.ok) {
            showToast('今日のMITを登録しました');
            closeMorningMitModal();
            if (typeof loadDashboard === 'function') loadDashboard();
        } else {
            showToast((res && res.error) || '保存に失敗しました', true);
        }
    } catch {
        showToast('保存に失敗しました', true);
    }
};

// ----- 永久ノート確認モーダル（AI 提案を承認制で保存） -----
window.openPermanentNoteConfirmModal = (title, content) => {
    let modal = $('#perm-note-confirm-modal');
    if (!modal) {
        // 動的にモーダルを生成
        const wrap = document.createElement('div');
        wrap.innerHTML = `
            <div id="perm-note-confirm-modal" class="modal-overlay hidden">
                <div class="modal-card" style="max-width:560px;">
                    <h3 style="margin-top:0;">📌 永久ノートに保存しますか？</h3>
                    <p style="font-size:0.8rem;color:var(--text-muted);margin:-4px 0 12px;">内容を編集してから保存できます。</p>
                    <label style="font-size:0.78rem;color:var(--text-muted);">タイトル</label>
                    <input id="perm-note-title-input" class="modern-input" style="margin-bottom:10px;" />
                    <label style="font-size:0.78rem;color:var(--text-muted);">本文</label>
                    <textarea id="perm-note-content-input" class="modern-input" style="min-height:180px;font-family:inherit;"></textarea>
                    <div class="modal-actions" style="margin-top:14px;">
                        <button class="modal-btn cancel" onclick="closePermNoteConfirmModal()">却下</button>
                        <button class="modal-btn submit" onclick="confirmPermNoteSave()">保存</button>
                    </div>
                </div>
            </div>
        `;
        document.body.appendChild(wrap.firstElementChild);
        modal = $('#perm-note-confirm-modal');
    }
    $('#perm-note-title-input').value = title || '';
    $('#perm-note-content-input').value = content || '';
    modal.classList.remove('hidden');
};

window.closePermNoteConfirmModal = () => {
    $('#perm-note-confirm-modal')?.classList.add('hidden');
};

window.confirmPermNoteSave = async () => {
    const title = ($('#perm-note-title-input')?.value || '').trim();
    const content = ($('#perm-note-content-input')?.value || '').trim();
    if (!title) { showToast('タイトルを入力してください', true); return; }
    try {
        const res = await apiFetch('/api/permanent_notes/confirm', {
            method: 'POST',
            body: JSON.stringify({ title, content })
        });
        if (res && res.ok) {
            showToast('永久ノートを保存しました');
            closePermNoteConfirmModal();
        } else {
            showToast((res && res.error) || '保存に失敗しました', true);
        }
    } catch (e) {
        showToast('保存に失敗しました', true);
    }
};

// ----- 瞑想タイマー -----
let _medState = { interval: null, remaining: 0, total: 0, startAt: null, wakeLock: null };

function _formatHM(dt) {
    return `${String(dt.getHours()).padStart(2,'0')}:${String(dt.getMinutes()).padStart(2,'0')}`;
}

async function _requestMedWakeLock() {
    try {
        if ('wakeLock' in navigator) {
            _medState.wakeLock = await navigator.wakeLock.request('screen');
            // タブ復帰時に再取得
            document.addEventListener('visibilitychange', _reacquireMedWakeLock);
        }
    } catch (e) {
        console.warn('Wake Lock 取得に失敗:', e);
    }
}

async function _reacquireMedWakeLock() {
    if (document.visibilityState === 'visible' && _medState.interval && !_medState.wakeLock) {
        try {
            _medState.wakeLock = await navigator.wakeLock.request('screen');
        } catch {}
    }
}

async function _releaseMedWakeLock() {
    try {
        if (_medState.wakeLock) {
            await _medState.wakeLock.release();
        }
    } catch {}
    _medState.wakeLock = null;
    document.removeEventListener('visibilitychange', _reacquireMedWakeLock);
}

function _vibrateMedDone() {
    try {
        if (navigator.vibrate) {
            // 終了パターン: 短-短-長
            navigator.vibrate([300, 150, 300, 150, 600]);
        }
    } catch {}
}

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
    _medState.startAt = new Date();
    $('#med-step-setup').style.display = 'none';
    $('#med-step-timer').style.display = '';
    _updateMedDisplay();
    _medState.interval = setInterval(() => {
        _medState.remaining--;
        _updateMedDisplay();
        if (_medState.remaining <= 0) _finishMeditation(min);
    }, 1000);
    // 画面が消えないよう Wake Lock を取得
    _requestMedWakeLock();
    // ライフログに開始記録（Bot の log_life_activity と同じレンジ形式）
    _medState.activityName = `瞑想（${min}分）`;
    apiFetch('/api/lifelog_activity', { method: 'POST', body: JSON.stringify({ activity_name: _medState.activityName, status: 'start' }) }).catch(() => {});
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
    // 終了をバイブで知らせる
    _vibrateMedDone();
    // 開始ログを終了レンジ形式に更新（- HH:MM ▶ X → - HH:MM - HH:MM X）
    const activityName = _medState.activityName || `瞑想（${min}分）`;
    apiFetch('/api/lifelog_activity', { method: 'POST', body: JSON.stringify({ activity_name: activityName, status: 'end' }) }).catch(() => {});
    _releaseMedWakeLock();
    showToast(`🧘 ${msg}`);
}

window.stopMeditation = () => {
    if (_medState.interval) { clearInterval(_medState.interval); _medState.interval = null; }
    const elapsed = Math.max(0, _medState.total - _medState.remaining);
    const min = Math.round(elapsed / 60);
    if (min > 0 && _medState.activityName) {
        // 中断時もレンジ形式で終了記録（開始ログが残っている）
        apiFetch('/api/lifelog_activity', { method: 'POST', body: JSON.stringify({ activity_name: _medState.activityName, status: 'end' }) }).catch(() => {});
    }
    _releaseMedWakeLock();
    $('#meditation-modal')?.classList.add('hidden');
};

window.closeMeditationModal = (e) => {
    if (e && e.target.closest && e.target.closest('.modal-card')) return;
    if (_medState.interval) stopMeditation();
    else $('#meditation-modal')?.classList.add('hidden');
};

// ----- ドラム練習タイマー（瞑想と同じ枠組み） -----
let _drumState = { interval: null, remaining: 0, total: 0, startAt: null, wakeLock: null, activityName: null };

async function _requestDrumWakeLock() {
    try {
        if ('wakeLock' in navigator) {
            _drumState.wakeLock = await navigator.wakeLock.request('screen');
            document.addEventListener('visibilitychange', _reacquireDrumWakeLock);
        }
    } catch (e) {
        console.warn('Wake Lock 取得に失敗:', e);
    }
}

async function _reacquireDrumWakeLock() {
    if (document.visibilityState === 'visible' && _drumState.interval && !_drumState.wakeLock) {
        try {
            _drumState.wakeLock = await navigator.wakeLock.request('screen');
        } catch {}
    }
}

async function _releaseDrumWakeLock() {
    try {
        if (_drumState.wakeLock) await _drumState.wakeLock.release();
    } catch {}
    _drumState.wakeLock = null;
    document.removeEventListener('visibilitychange', _reacquireDrumWakeLock);
}

window.openDrumPracticeModal = () => {
    const modal = $('#drum-practice-modal');
    if (!modal) return;
    const lastMin = parseInt(localStorage.getItem('drum_last_minutes') || '30', 10);
    const customInput = $('#drum-custom-min');
    if (customInput) customInput.value = lastMin;
    $('#drum-step-setup').style.display = '';
    $('#drum-step-timer').style.display = 'none';
    $('#drum-step-done').style.display = 'none';
    modal.classList.remove('hidden');
};

window.setDrumDuration = (min) => {
    const input = $('#drum-custom-min');
    if (input) input.value = min;
};

window.startDrumPractice = () => {
    const min = parseInt($('#drum-custom-min').value || '30', 10);
    if (isNaN(min) || min < 1) { showToast('時間を入力してください', true); return; }
    localStorage.setItem('drum_last_minutes', String(min));
    _drumState.total = min * 60;
    _drumState.remaining = min * 60;
    _drumState.startAt = new Date();
    $('#drum-step-setup').style.display = 'none';
    $('#drum-step-timer').style.display = '';
    _updateDrumDisplay();
    _drumState.interval = setInterval(() => {
        _drumState.remaining--;
        _updateDrumDisplay();
        if (_drumState.remaining <= 0) _finishDrumPractice(min);
    }, 1000);
    _requestDrumWakeLock();
    _drumState.activityName = `ドラム練習（${min}分）`;
    apiFetch('/api/lifelog_activity', { method: 'POST', body: JSON.stringify({ activity_name: _drumState.activityName, status: 'start' }) }).catch(() => {});
};

function _updateDrumDisplay() {
    const m = Math.floor(_drumState.remaining / 60);
    const s = _drumState.remaining % 60;
    const el = $('#drum-timer-display');
    if (el) el.textContent = `${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
}

function _finishDrumPractice(min) {
    clearInterval(_drumState.interval);
    _drumState.interval = null;
    $('#drum-step-timer').style.display = 'none';
    $('#drum-step-done').style.display = '';
    const msg = `${min}分間のドラム練習を完了しました。`;
    const doneEl = $('#drum-done-msg');
    if (doneEl) doneEl.textContent = msg;
    try { if (navigator.vibrate) navigator.vibrate([300, 150, 300, 150, 600]); } catch {}
    const activityName = _drumState.activityName || `ドラム練習（${min}分）`;
    apiFetch('/api/lifelog_activity', { method: 'POST', body: JSON.stringify({ activity_name: activityName, status: 'end' }) }).catch(() => {});
    _releaseDrumWakeLock();
    showToast(`🥁 ${msg}`);
}

window.stopDrumPractice = () => {
    if (_drumState.interval) { clearInterval(_drumState.interval); _drumState.interval = null; }
    const elapsed = Math.max(0, _drumState.total - _drumState.remaining);
    const min = Math.round(elapsed / 60);
    if (min > 0 && _drumState.activityName) {
        apiFetch('/api/lifelog_activity', { method: 'POST', body: JSON.stringify({ activity_name: _drumState.activityName, status: 'end' }) }).catch(() => {});
    }
    _releaseDrumWakeLock();
    $('#drum-practice-modal')?.classList.add('hidden');
};

window.closeDrumPracticeModal = (e) => {
    if (e && e.target.closest && e.target.closest('.modal-card')) return;
    if (_drumState.interval) stopDrumPractice();
    else $('#drum-practice-modal')?.classList.add('hidden');
};

// ----- ドラム上達ロードマップ（情報タブのカード内） -----
// ロードマップ本体は静的データ。動画リンクだけサーバから取得する。
const DRUM_ROADMAP_STATIC = {
    phases: [
        {
            id: 'phase_basics', label: 'Phase 1: 基礎づくり',
            description: 'スティックコントロールと最も基本的なビートを身体に入れる。約1〜2ヶ月。',
            milestones: [
                {
                    id: 'M01_grip_stick_control', label: 'マッチドグリップとリバウンドが安定する', criteria: '脱力したグリップで連続したシングルストロークを30秒',
                    practices: ['STEP1: 親指と人差し指でスティックをつまみ、残り3本を添える', 'STEP2: 肘を軽く曲げ、手首の重さを使って振り下ろす', 'STEP3: 膝の上でリバウンドを感じながら左右交互に打つ', 'STEP4: 40BPMのメトロノームに合わせて均等なRLを5分/日'],
                    search_keywords: ['ドラム グリップ 初心者', 'drum matched grip tutorial'],
                },
                {
                    id: 'M02_single_double_80bpm', label: 'シングル/ダブルストローク 80BPM', criteria: 'メトロノーム80BPMで8分音符の粒が揃う',
                    practices: ['STEP1: 50BPMから均等なRL連打を1分間', 'STEP2: 5BPMずつテンポを上げ80BPMを目指す', 'STEP3: ダブルストロークはRR LLを同じ手順で練習', 'STEP4: ストロークの粒が揃っているか録音して確認'],
                    search_keywords: ['ドラム シングルストローク 練習', 'single stroke roll beginner'],
                },
                {
                    id: 'M03_8beat_80bpm', label: '8ビートを80BPMで30秒キープ', criteria: 'テンポを落とさず安定したダイナミクスでキープできる',
                    practices: ['STEP1: 右手でハイハット8分打ちを安定させる', 'STEP2: スネア（2・4拍目）を加える', 'STEP3: バスドラム（1・3拍目）を加えて3点同時', 'STEP4: 40BPMから始め60→80BPMへ段階的に上げる'],
                    search_keywords: ['ドラム 8ビート 初心者', 'beginner drum 8 beat pattern'],
                },
                {
                    id: 'M04_paradiddle', label: 'シングル・パラディドルが叩ける', criteria: 'RLRR LRLL を100BPMで連続',
                    practices: ['STEP1: RLRR LRLL のパターンを手拍子で声に出して覚える', 'STEP2: 60BPMでパッドに叩く、音粒を均等に', 'STEP3: アクセントをRLRLに乗せる', 'STEP4: 100BPMまで5BPMずつ上げる'],
                    search_keywords: ['ドラム パラディドル 初心者', 'paradiddle drum tutorial'],
                },
            ],
        },
        {
            id: 'phase_groove', label: 'Phase 2: グルーヴと表現',
            description: '8ビート以外のグルーヴ・フィル・ダイナミクスを身につける。約3〜6ヶ月。',
            milestones: [
                {
                    id: 'M05_16beat_basic', label: '16ビートの基本パターン', criteria: '90BPMで16分のハイハットが安定',
                    practices: ['STEP1: 右手だけで16分音符のハイハット連打を安定させる', 'STEP2: スネア（2・4拍目）を加える', 'STEP3: バスドラムを加えて3点同時', 'STEP4: 70BPMから90BPMへ段階的に上げる'],
                    search_keywords: ['ドラム 16ビート 初心者', 'sixteenth note drum groove'],
                },
                {
                    id: 'M06_basic_fills', label: '4小節フィルのバリエーション3種', criteria: '4分・8分・16分混在のフィルを曲の流れで自然に入れる',
                    practices: ['STEP1: 4分音符フィル（タム一周）をゆっくり覚える', 'STEP2: 8分音符フィルを同様に', 'STEP3: 16分音符フィルを同様に', 'STEP4: 直前のビートとの繋ぎタイミングを反復練習'],
                    search_keywords: ['ドラム フィルイン 初心者', 'drum fill beginner tutorial'],
                },
                {
                    id: 'M07_shuffle', label: 'シャッフルが叩ける', criteria: 'ブルース/ジャズ系の3連グルーヴが安定',
                    practices: ['STEP1: タ・カ・タ（3連符）を手拍子で体に入れる', 'STEP2: シャッフルのハイハットパターンをゆっくり', 'STEP3: スネア・バスドラムを加えてパターンを完成させる', 'STEP4: ブルース12小節を通して80BPMで叩く'],
                    search_keywords: ['ドラム シャッフル 初心者', 'shuffle drum groove tutorial'],
                },
                {
                    id: 'M08_dynamics', label: 'アクセントとゴーストノートの使い分け', criteria: 'メインアクセントとゴーストノートを明確に区別',
                    practices: ['STEP1: スネアを大・小の2段階で叩き分ける練習', 'STEP2: ゴーストノートはスティックをヘッドから2〜3cm以内で打つ', 'STEP3: 8ビートに弱音ゴーストノートを挿入してみる', 'STEP4: R&Bのドラム動画を参考に実際のフィールを学ぶ'],
                    search_keywords: ['ドラム ゴーストノート 練習', 'ghost note drum tutorial'],
                },
                {
                    id: 'M09_metronome_120', label: 'ルーディメンツ各種を120BPMで', criteria: 'シングル・ダブル・パラディドル系を120BPMで正確に',
                    practices: ['STEP1: 毎日ウォームアップ：シングル→ダブル→パラディドル→フラムの順に各1分', 'STEP2: 100BPMで全て安定させてから120へ', 'STEP3: 各ルーディメンツに強弱アクセントをつける', 'STEP4: 120BPMでの安定を録音で確認'],
                    search_keywords: ['ドラム ルーディメンツ 一覧', 'drum rudiments 120bpm'],
                },
            ],
        },
        {
            id: 'phase_song', label: 'Phase 3: 曲を叩く',
            description: '実曲を頭から終わりまで叩けるようになる。約6ヶ月〜1年。',
            milestones: [
                {
                    id: 'M10_first_full_song', label: '好きな曲を1曲フルで叩ける', criteria: 'イントロからエンディングまでテンポキープ、フィルも再現',
                    practices: ['STEP1: テンポ100前後の比較的シンプルな曲を選ぶ', 'STEP2: 10秒ずつ区切って部分練習し、繋いでいく', 'STEP3: フィル・サビ前・転換のタイミングをマーク', 'STEP4: 通し演奏を録音して問題箇所を特定する'],
                    search_keywords: ['ドラム 初心者 曲 おすすめ', 'easy drum song beginner'],
                },
                {
                    id: 'M11_song_x3', label: '完奏可能曲が3曲', criteria: 'レパートリーとして人前で叩ける状態',
                    practices: ['STEP1: テンポ・難易度・ジャンルが異なる3曲を選ぶ', 'STEP2: 週1曲を目標にマスターしていく', 'STEP3: 毎回の練習で必ず1曲通し演奏する', 'STEP4: 毎回録音して前回からの改善点を確認'],
                    search_keywords: ['ドラム カバー 初心者 曲', 'beginner drum cover songs'],
                },
                {
                    id: 'M12_genre_variety', label: 'ロック以外のジャンルを1曲', criteria: 'ファンク・ジャズ・ラテンなどジャンル特有のフィールが出せる',
                    practices: ['STEP1: ファンク・ジャズ・ラテンから1ジャンル選ぶ', 'STEP2: そのジャンル専門の解説動画でグルーヴの特徴を学ぶ', 'STEP3: 簡単な課題曲を1曲選んで練習する', 'STEP4: ジャンル特有のアクセント位置を意識して叩く'],
                    search_keywords: ['ドラム ファンク グルーヴ', 'funk drum groove tutorial'],
                },
                {
                    id: 'M13_record_playback', label: '自分の演奏を録音して客観評価', criteria: '録音を聴いて課題点を3つ以上挙げられる',
                    practices: ['STEP1: スマホを正面に立てて演奏を動画録画する', 'STEP2: 原曲と自分の演奏を聴き比べる', 'STEP3: テンポのズレ・音量バランス・フィルのタイミングをメモする', 'STEP4: 課題点を次回の練習メニューに組み込む'],
                    search_keywords: ['ドラム 録音 スマホ', 'how to record drum practice'],
                },
            ],
        },
    ],
};

async function loadDrumRoadmap() {
    const container = $('#dash-drum-roadmap');
    if (!container) return;
    // ロードマップ本体は静的データなので即レンダリング（API失敗でも表示は壊れない）
    renderDrumRoadmap(DRUM_ROADMAP_STATIC, {});
    // 動画リンクは stocked_links から取得（YouTubeカードと同じ仕組み）
    // 取得失敗時も静的ロードマップは既に表示済みなので、控えめに無視する
    let linksMap = {};
    try {
        const data = await apiFetch('/api/links');
        const links = (data && data.links) || [];
        for (const lk of links) {
            if (lk.type !== 'drum_video') continue;
            // tags に "milestone:<id>" の形式で紐付け先を埋め込む
            const tags = String(lk.tags || '').split(',').map(t => t.trim()).filter(Boolean);
            const milestoneTag = tags.find(t => t.startsWith('milestone:'));
            if (!milestoneTag) continue;
            const milestoneId = milestoneTag.slice('milestone:'.length);
            if (!linksMap[milestoneId]) linksMap[milestoneId] = [];
            const videoId = _extractYouTubeVideoId(lk.url || '');
            linksMap[milestoneId].push({
                link_id: lk.id,
                url: lk.url,
                title: lk.title || lk.url,
                thumbnail: videoId ? `https://i.ytimg.com/vi/${videoId}/hqdefault.jpg` : '',
            });
        }
    } catch (e) {
        // 失敗してもロードマップは表示済みなので静かにスキップ
    }
    renderDrumRoadmap(DRUM_ROADMAP_STATIC, linksMap);
}

function _extractYouTubeVideoId(url) {
    if (!url) return null;
    try {
        const u = new URL(url);
        const host = u.hostname.toLowerCase();
        if (host.includes('youtu.be')) {
            return u.pathname.replace(/^\//, '').split('/')[0] || null;
        }
        if (host.includes('youtube.com')) {
            if (u.pathname.startsWith('/shorts/')) return u.pathname.split('/shorts/')[1].split('/')[0] || null;
            return u.searchParams.get('v');
        }
    } catch { /* noop */ }
    return null;
}

function renderDrumRoadmap(data, linksMap) {
    const container = $('#dash-drum-roadmap');
    if (!container) return;
    const phases = data.phases || [];
    let html = '';
    for (const ph of phases) {
        html += `<div class="drum-phase-block">
            <div class="drum-phase-title">${escapeHtml(ph.label || '')}</div>
            <div class="drum-phase-desc">${escapeHtml(ph.description || '')}</div>`;
        for (const m of (ph.milestones || [])) {
            const inputId = `drum-add-url-${m.id}`;
            const videos = (linksMap && linksMap[m.id]) || [];
            const practicesHtml = (m.practices && m.practices.length)
                ? `<div class="drum-practices">
                    <div class="drum-section-label">📋 練習メニュー</div>
                    <ol class="drum-practices-list">${m.practices.map(p => `<li>${escapeHtml(p)}</li>`).join('')}</ol>
                   </div>`
                : '';
            const keywordsHtml = (m.search_keywords && m.search_keywords.length)
                ? `<div class="drum-keywords">
                    <div class="drum-section-label">🔍 動画検索キーワード</div>
                    <div class="drum-keyword-tags">${m.search_keywords.map(k => `<a class="drum-keyword-tag" href="https://www.youtube.com/results?search_query=${encodeURIComponent(k)}" target="_blank" rel="noopener">${escapeHtml(k)}</a>`).join('')}</div>
                   </div>`
                : '';
            html += `<div class="drum-milestone-row">
                <div class="drum-milestone-title">${escapeHtml(m.label || '')}</div>
                ${m.criteria ? `<div class="drum-milestone-criteria">基準: ${escapeHtml(m.criteria)}</div>` : ''}
                ${practicesHtml}
                ${keywordsHtml}
                <div class="drum-videos">
                    ${videos.map(v => renderDrumRoadmapVideo(v)).join('')}
                </div>
                <div class="drum-add-row">
                    <input type="url" id="${inputId}" placeholder="YouTube URL を追加">
                    <button class="mini-link" onclick="addDrumRoadmapLink('${escapeHtml(m.id)}', '${inputId}')">追加</button>
                </div>
            </div>`;
        }
        html += `</div>`;
    }
    if (!html) html = '<div class="study-empty">ロードマップが空です</div>';
    container.innerHTML = html;
}

function renderDrumRoadmapVideo(v) {
    const linkId = v.link_id;
    return `<div class="drum-video-row">
        <a href="${escapeHtml(v.url || '')}" target="_blank" rel="noopener">
            <img loading="lazy" src="${escapeHtml(v.thumbnail || '')}" alt="">
        </a>
        <div class="drum-video-meta">
            <div class="drum-video-title">${escapeHtml(v.title || v.url || '')}</div>
        </div>
        <button class="mini-link" style="color:#a00;flex-shrink:0;" onclick="deleteDrumRoadmapLink(${linkId})">削除</button>
    </div>`;
}

window.addDrumRoadmapLink = async (milestoneId, inputId) => {
    const input = document.getElementById(inputId);
    const url = (input?.value || '').trim();
    if (!url) return;
    showToast('追加中…');
    try {
        // 1. /api/links で type=drum_video として作成
        const created = await apiFetch('/api/links', {
            method: 'POST',
            body: JSON.stringify({ url, type: 'drum_video', title: '' }),
        });
        const linkId = created && created.link_id;
        if (!linkId) {
            showToast('追加失敗', true);
            return;
        }
        // 2. tags に milestone:<id> を保存
        await apiFetch(`/api/links/${linkId}`, {
            method: 'PUT',
            body: JSON.stringify({
                tags: `milestone:${milestoneId}`,
                type: 'drum_video',
            }),
        });
        if (input) input.value = '';
        await loadDrumRoadmap();
    } catch (e) {
        showToast('追加失敗', true);
    }
};

window.deleteDrumRoadmapLink = async (linkId) => {
    if (!linkId) return;
    if (!confirm('この動画リンクを削除しますか？')) return;
    try {
        await apiFetch(`/api/links/${linkId}`, { method: 'DELETE' });
        await loadDrumRoadmap();
    } catch (e) {
        showToast('削除失敗', true);
    }
};

window.loadDrumRoadmap = loadDrumRoadmap;

// ----- EDINET 決算関連書類 -----
let _edinetDocs = [];

window.openEdinetModal = () => {
    const modal = $('#edinet-modal');
    if (!modal) return;
    const tickerInput = $('#edinet-ticker');
    const current = ($('#invest-ticker-input')?.value || '').trim();
    if (tickerInput && current) tickerInput.value = current;
    $('#edinet-results').innerHTML = '';
    $('#edinet-save-all-btn').style.display = 'none';
    $('#edinet-status').textContent = '過去N日分の EDINET 提出書類を金融庁公式 API から取得します。検索に数十秒かかることがあります。';
    _edinetDocs = [];
    modal.classList.remove('hidden');
};

window.closeEdinetModal = (e) => {
    if (e && e.target.closest && e.target.closest('.modal-card')) return;
    $('#edinet-modal')?.classList.add('hidden');
};

window.edinetFind = async () => {
    const ticker = ($('#edinet-ticker')?.value || '').trim();
    const days = parseInt($('#edinet-days')?.value || '800', 10);
    const onlyEarnings = $('#edinet-only-earnings')?.checked !== false;
    if (!ticker) { showToast('証券コードを入力してください', true); return; }
    const status = $('#edinet-status');
    const results = $('#edinet-results');
    results.innerHTML = '<div style="padding:10px;color:var(--text-secondary);">検索中…（最大1分程度かかります）</div>';
    $('#edinet-save-all-btn').style.display = 'none';
    if (status) status.textContent = `${ticker} の過去${days}日分を走査中…`;
    try {
        const data = await apiFetch('/api/edinet/find', {
            method: 'POST',
            body: JSON.stringify({ ticker, days, only_earnings: onlyEarnings }),
        });
        if (!data.ok) {
            results.innerHTML = `<div style="padding:10px;color:#a00;">${escapeHtml(data.error || '検索失敗')}</div>`;
            return;
        }
        _edinetDocs = data.documents || [];
        if (status) status.textContent = `${data.ticker || ticker}: ${_edinetDocs.length} 件の書類 (過去${data.days_scanned}日分)`;
        renderEdinetResults(_edinetDocs);
        if (_edinetDocs.length > 0) $('#edinet-save-all-btn').style.display = '';
    } catch (e) {
        results.innerHTML = `<div style="padding:10px;color:#a00;">エラー: ${escapeHtml(String(e))}</div>`;
    }
};

function renderEdinetResults(docs) {
    const results = $('#edinet-results');
    if (!results) return;
    if (!docs || !docs.length) {
        results.innerHTML = '<div style="padding:10px;color:var(--text-secondary);">該当書類が見つかりませんでした。日数を増やして再検索してみてください。</div>';
        return;
    }
    let html = '';
    for (const d of docs) {
        const day = (d.submit_datetime || '').slice(0, 10);
        const period = [d.period_start, d.period_end].filter(Boolean).join(' 〜 ');
        html += `<div class="edinet-doc-row" data-doc-id="${escapeHtml(d.doc_id || '')}" style="padding:8px 4px;border-bottom:1px solid var(--border,#eee);">
            <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start;">
                <div style="flex:1;min-width:0;">
                    <div style="font-weight:600;">${escapeHtml(d.doc_type_label || d.doc_type_code || '')}</div>
                    <div style="font-size:0.8rem;color:var(--text-secondary);">${escapeHtml(d.doc_description || '')}</div>
                    <div style="font-size:0.75rem;color:var(--text-muted);">提出: ${escapeHtml(day)} ${period ? '/ 対象: ' + escapeHtml(period) : ''}</div>
                </div>
                <button class="mini-link" onclick="edinetSaveOne('${escapeHtml(d.doc_id)}')">📥 保存</button>
            </div>
            <div class="edinet-save-status" style="font-size:0.75rem;color:var(--text-muted);margin-top:2px;"></div>
        </div>`;
    }
    results.innerHTML = html;
}

window.edinetSaveOne = async (docId) => {
    const doc = _edinetDocs.find(d => d.doc_id === docId);
    if (!doc) return;
    const row = document.querySelector(`.edinet-doc-row[data-doc-id="${CSS.escape(docId)}"]`);
    const statusEl = row?.querySelector('.edinet-save-status');
    if (statusEl) statusEl.textContent = 'ダウンロード中…';
    try {
        const data = await apiFetch('/api/edinet/download', {
            method: 'POST',
            body: JSON.stringify({
                doc_id: doc.doc_id,
                sec_code: doc.sec_code,
                submit_date: (doc.submit_datetime || '').slice(0, 10),
                doc_type_label: doc.doc_type_label,
            }),
        });
        if (data.ok) {
            if (statusEl) statusEl.textContent = `✅ 保存: ${data.drive_path} (${Math.round((data.bytes || 0) / 1024)} KB)`;
        } else {
            if (statusEl) statusEl.textContent = `❌ ${data.error || '保存失敗'}`;
        }
    } catch (e) {
        if (statusEl) statusEl.textContent = `❌ 例外: ${String(e)}`;
    }
};

window.edinetSaveAll = async () => {
    const btn = $('#edinet-save-all-btn');
    if (btn) btn.disabled = true;
    for (const d of _edinetDocs) {
        await window.edinetSaveOne(d.doc_id);
    }
    if (btn) btn.disabled = false;
    showToast('すべての書類の保存処理が完了しました');
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
    if (tableTarget) tableTarget.innerHTML = '<div class="loading-placeholder">読み込み中…</div>';
    try {
        const url = '/api/fitbit_all_data?days=14' + (forceRefresh ? '&_=' + Date.now() : '');
        const data = await apiFetch(url);
        _fitbitRows = data.data || [];
        if (!_fitbitRows.length) {
            if (tableTarget) tableTarget.innerHTML = '<div class="loading-placeholder">Fitbitデータがありません。</div>';
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
    results.innerHTML = '<p class="search-hint">読み込み中…</p>';
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
    list.innerHTML = '<p class="search-hint">読み込み中…</p>';
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
    if (titleEl) titleEl.textContent = `Yahoo!天気 (${name})`;
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

// 共通: デイリー系カードの軽量 Markdown レンダラ
// 「今日の振り返り」と同じ見た目を「デイリーノート」「マネージャーの気づき」にも適用する。
function renderDailyMarkdown(text, opts = {}) {
    const dateLabel = opts.dateLabel
        ? `<div style="font-size:0.74rem;color:var(--text-muted);margin-bottom:6px;">${escapeHtml(opts.dateLabel)}</div>`
        : '';
    const html = (text || '')
        .split('\n')
        .map(line => {
            if (/^### /.test(line)) return `<div style="margin:8px 0 4px;font-weight:700;font-size:0.88rem;color:var(--text-primary);">${escapeHtml(line.replace(/^### /, ''))}</div>`;
            if (/^## /.test(line)) return `<div style="margin:10px 0 4px;font-weight:700;color:var(--accent);font-size:0.9rem;">${escapeHtml(line.replace(/^## /, ''))}</div>`;
            if (/^# /.test(line)) return `<div style="margin:6px 0;font-weight:700;font-size:1rem;">${escapeHtml(line.replace(/^# /, ''))}</div>`;
            if (/^[\-*] /.test(line)) return `<div style="padding:2px 0 2px 12px;border-left:2px solid rgba(0,186,152,0.3);margin:2px 0;font-size:0.88rem;">${escapeHtml(line.replace(/^[\-*]\s+/, ''))}</div>`;
            if (line.trim() === '') return '<div style="height:6px;"></div>';
            return `<div style="font-size:0.88rem;line-height:1.6;">${escapeHtml(line)}</div>`;
        })
        .join('');
    return dateLabel + html;
}

function renderDailySummaryCard(data, opts = {}) {
    const tEl = $('#dash-daily-summary');
    const qEl = $('#dash-daily-summary-questions');
    if (!tEl || !qEl) return;
    const text = (data && data.text) || '';
    const questions = ((data && data.questions) || []).filter(q => q.status !== 'resolved');
    const displayDate = (data && data.date) || '';
    const isFallback = !!(data && data.fallback);
    const preserveOnEmpty = !!opts.preserveOnEmpty;

    if (text) {
        tEl.innerHTML = renderDailyMarkdown(text, {
            dateLabel: displayDate ? `📅 ${displayDate} の振り返り` : '',
        });
    } else if (!preserveOnEmpty) {
        tEl.innerHTML = '<div class="loading-placeholder">デイリーサマリーはまだ生成されていません。</div>';
    }
    // preserveOnEmpty=true で text が空のときは tEl を上書きせず、前回の振り返りを残す

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
        <button class="modal-btn submit" style="width:100%;font-size:0.85rem;margin-top:6px;" onclick="regenerateSummaryWithAnswers('${date}')">${text ? '回答を反映して再生成' : '回答を反映して生成'}</button>
    `;
}

let _summaryQuestionsHidden = false;
window.toggleSummaryQuestions = () => {
    _summaryQuestionsHidden = !_summaryQuestionsHidden;
    loadDailySummary();
};

window.generateDailySummary = async (finalize, date) => {
    if (_dailySummaryGenerating) return;
    _dailySummaryGenerating = true;
    const tEl = $('#dash-daily-summary');
    const dateLabel = date ? `（${date}）` : '';
    if (tEl) tEl.innerHTML = `<div class="loading-placeholder">サマリーを生成中${dateLabel}…（少し時間がかかります）</div>`;
    try {
        const body = { finalize: !!finalize };
        if (date) body.date = date;
        const result = await apiFetch('/api/daily_summary/generate', {
            method: 'POST',
            body: JSON.stringify(body),
        });
        renderDailySummaryCard(
            { date: result.date, text: result.summary, questions: result.questions },
            { preserveOnEmpty: true },
        );
        if (result.saved) {
            showToast('Obsidianに保存しました');
        } else if (result.questions && result.questions.length) {
            showToast('未確定の質問があります。回答すると確定保存されます。');
        } else if (!result.summary) {
            showToast('対象日のデータが見つかりませんでした', true);
        }
    } catch (e) {
        if (tEl) tEl.innerHTML = '<div class="loading-placeholder">生成に失敗しました。</div>';
        showToast('生成に失敗しました', true);
    } finally {
        _dailySummaryGenerating = false;
    }
};

// 日付を指定して過去日分のデイリーサマリーを生成
window.generateDailySummaryForDate = async () => {
    const today = new Date();
    const ymd = today.toISOString().slice(0, 10);
    let date = prompt('生成する日付を YYYY-MM-DD で入力してください（例: 2026-05-10）', ymd);
    if (!date) return;
    date = String(date).trim();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
        showToast('日付形式が不正です。YYYY-MM-DD で入力してください', true);
        return;
    }
    // マネージャー質問への回答が必要な場合はサマリーを保留する仕様のため finalize=false
    await generateDailySummary(false, date);
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
    const hadSummary = !!(tEl && tEl.textContent && tEl.textContent.trim()
        && !tEl.querySelector('.loading-placeholder'));
    const verb = hadSummary ? '再生成' : '生成';
    if (tEl) tEl.innerHTML = `<div class="loading-placeholder">回答を反映してサマリーを${verb}中…</div>`;
    try {
        const result = await apiFetch('/api/daily_summary/generate', {
            method: 'POST',
            body: JSON.stringify({ date, answers, finalize: false }),
        });
        // result.summary が空（質問のみ追加された保留状態）でも、画面上の前回振り返りは保持する
        renderDailySummaryCard(
            { date: result.date, text: result.summary, questions: result.questions },
            { preserveOnEmpty: true },
        );
        if (result.saved) {
            showToast('Obsidianに保存しました');
        } else if (result.questions && result.questions.length) {
            showToast('まだ確認が必要な点があります。');
        } else if (!result.summary) {
            // text空 & 質問もなし。loadDailySummary でフォールバック読込
            loadDailySummary();
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
        body: `<p>このアプリは <b>5つのタブ</b>でできています。</p>
        <div class="tut-grid">
            <div class="tut-card"><b>💬 チャット</b><br><small>マネージャーAI と対話</small></div>
            <div class="tut-card"><b>📅 予定</b><br><small>MIT・次のアクション・カレンダー・タスク</small></div>
            <div class="tut-card"><b>📒 ログ</b><br><small>習慣・食事・支出・日記・マネージャー通知ログ</small></div>
            <div class="tut-card"><b>📰 情報</b><br><small>天気・ニュース・リンク</small></div>
            <div class="tut-card"><b>💹 投資</b><br><small>銘柄分析・スクリーナー</small></div>
        </div>
        <p style="margin-top:10px;font-size:0.85rem;color:var(--text-muted);">下のナビでタブを切り替えます（左から推奨利用順）。</p>`
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
            <li>🔄 チャットは30秒ごとに自動更新（マネージャーからのメッセージも自動で表示）</li>
        </ul>`
    },
    {
        title: 'アクション提案ボタン',
        target: 'chat',
        body: `<p>マネージャーは記録系アクションを<b>勝手には保存しません</b>。代わりに「このアクション実行する？」というボタンを表示します。</p>
        <ul>
            <li>📝 ノートに保存 / 📌 永久ノートにする</li>
            <li>📅 カレンダーに追加 / ✅ タスクに追加 / 🎯 MITを登録</li>
            <li>🔥 ライフログを記録 / 💭 思考整理を保存 / ✅ 習慣を完了</li>
            <li>各ボタンの右の <b>✕</b> で却下できます</li>
        </ul>
        <p style="font-size:0.85rem;color:var(--text-muted);">ボタンを押した時にだけ実際に保存されます。</p>`
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
            <li>🧘 瞑想タイマー（Wake Lock で画面 ON・完了時バイブ）</li>
        </ul>`
    },
    {
        title: 'Yahoo!天気・ニュース',
        target: 'info',
        body: `<p>情報タブには日々の情報がまとまっています。</p>
        <ul>
            <li>🌤️ Yahoo!天気（岡山 北部 / 南部 を切替）</li>
            <li>🕐 時間別予報（気温・降水確率）をスクロールで確認</li>
            <li>🔗 Yahoo!天気の詳細ページへリンク</li>
            <li>📰 Yahoo!ニュース 主要トピック</li>
        </ul>`
    },
    {
        title: 'ストックリンク',
        target: 'info',
        body: `<p>気になった URL（記事・YouTube・レシピ・地図・本）をチャットに貼ると自動分類で保存。</p>
        <ul>
            <li>🏷️ タグ付け・タグ絞り込み対応</li>
            <li>📅 目標日を設定して管理</li>
            <li>✏️「手動追加」ボタンで直接登録も可能</li>
            <li>📓 NotebookLM URL を設定すると「📓 NotebookLM」ボタンが表示される（書籍・勉強カード共通）</li>
        </ul>`
    },
    {
        title: '勉強カード',
        target: 'info',
        body: `<p>記憶したい知識を管理するカード。書籍カードと同じ仕組みで動きます。</p>
        <ul>
            <li>➕「追加」ボタンでタイトルを入力して登録（URLは任意）</li>
            <li>✏️ 編集モーダルで NotebookLM URL・メモを設定</li>
            <li>📓 NotebookLM URL を入れると「📓 NotebookLM」ボタンが表示</li>
            <li>📝 メモは <b>Q&A 形式と箇条書きを混在</b>して書けます</li>
        </ul>
        <div class="tut-callout" style="font-size:0.8rem;line-height:1.8;">
            Q: ○○とは？<br>
            A: △△のこと<br>
            - ポイント: ◇◇
        </div>
        <p style="margin-top:6px;font-size:0.82rem;color:var(--text-muted);">Q・A は色分け表示、- は箇条書きになります。</p>`
    },
    {
        title: 'ドラム練習ロードマップ',
        target: 'info',
        body: `<p>13のマイルストーンで初心者がゴールまで進める練習計画です。</p>
        <ul>
            <li>📋 各マイルストーンに <b>STEP1〜4 の具体的な練習メニュー</b>を表示</li>
            <li>🔍 <b>動画検索キーワード</b>をタップすると YouTube 検索が新タブで開く</li>
            <li>▶ 参考動画を URL で追加して各マイルストーンに紐付け可能</li>
        </ul>`
    },
    {
        title: '情報タブ カードの開閉',
        target: 'info',
        body: `<p>情報タブの各カードの開閉状態は <b>自動で記憶</b>されます。</p>
        <ul>
            <li>カードを開いたまま離れると、次回も開いた状態で表示</li>
            <li>閉じたカードはリロード後も閉じたまま</li>
            <li>初回アクセス時はすべて閉じた状態でスタート</li>
        </ul>
        <p style="font-size:0.85rem;color:var(--text-muted);">カードが多い場合は、よく使うものだけ開いておくと見やすくなります。</p>`
    },
    {
        title: 'メール受信トレイ',
        target: 'info',
        body: `<p>Gmail の受信メールを AI が要約・重要度判定して表示します。</p>
        <ul>
            <li>📬 未処理 / 既読 / ゴミ箱 / 全てをタブ切替</li>
            <li>🔴 重要メールは赤ラベル付きで通知も届きます</li>
            <li>📌 重要メールは「保存」で Obsidian に Markdown 保存</li>
            <li>📥「取り込み」で手動再ポーリングも可能</li>
        </ul>`
    },
    {
        title: '習慣トラッカー',
        target: 'log',
        body: `<p>毎日の習慣をワンタップでチェック。週N回や特定曜日の習慣も管理できます。</p>
        <ul>
            <li>⭕ チェックで完了記録（Google Tasks「習慣」リストと同期）</li>
            <li>⏰「+いつ」を設定すると指定時刻にマネージャーが声をかけます</li>
            <li>📅「+毎日」をタップ → 月火水…のチェックボックスで対象曜日を指定（例: 月水金のみ）</li>
            <li>⠿ 長押しで並び替え可能</li>
            <li>📊 過去 28 日のヒートマップ ＋ 過去 90 日のガントチャート</li>
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
        title: '食事ログ',
        target: 'log',
        body: `<p>食事を写真または手動で記録。Gemini Vision が自動で栄養分析します。</p>
        <ul>
            <li>📷 写真撮影 → AI が料理名・カロリー・PFC を推定</li>
            <li>✏️ 手動入力も可能</li>
            <li>🥗 1 日の合計カロリー・PFC をカード上部に表示</li>
            <li>💬「アドバイス」ボタンで栄養バランスのフィードバック</li>
        </ul>`
    },
    {
        title: '支出メモ',
        target: 'log',
        body: `<p>レシートを撮影するか手動で支出を記録します。</p>
        <ul>
            <li>📷 レシート撮影 → AI が店名・金額・カテゴリを自動入力</li>
            <li>💸 大きな支出は閾値超過で自動通知（しきい値は設定変更可）</li>
            <li>📊 月別合計をカテゴリ別に集計</li>
            <li>🧾 レシート画像は Google Drive に自動バックアップ</li>
        </ul>`
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
        title: '今日の振り返り / デイリーノート / マネージャーの気づき',
        target: 'log',
        body: `<p>ログタブの最上部に「今日の記録」グループとして並びます。</p>
        <ul>
            <li>📅 <b>今日の振り返り</b>: 毎晩22:00にマネージャーが質問→チャット内の回答欄で答える→翌日の質問が来るまで回答を更新可能。回答後22:00サイクルでサマリー生成・Obsidianへ保存。</li>
            <li>📔 <b>デイリーノート</b>: Obsidian の DailyNotes。「編集」ボタンで内容を書き換え可能</li>
            <li>📒 <b>マネージャーの気づき</b>: 対話から見つけた傾向・パターンの観察ノート</li>
            <li>📨 <b>マネージャー通知ログ</b>: 朝刊ニュース・地合いなどの長文配信。未読バッジ・既読/未読切替・削除ボタン付き。</li>
        </ul>`
    },
    {
        title: '予定タブの並び',
        target: 'schedule',
        body: `<p>予定タブのカードは次の順に並びます。</p>
        <ul>
            <li>🎯 <b>今日のMIT</b> — 今日必ず終わらせたい3件</li>
            <li>🚀 <b>次のアクション</b> — マネージャーが提案する次の一手（情報タブから移設）</li>
            <li>📅 <b>カレンダー</b> — Google Calendar の今日の予定</li>
            <li>💼/🏠 <b>仕事 / プライベート</b> — Google Tasks</li>
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
            <li>🌅 毎朝 6:30 に AI が Calendar を参照してMIT候補を提案（通知あり）</li>
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
        title: '銘柄スクリーナー & ウォッチリスト',
        target: 'invest',
        body: `<p>日本株を機械的にフィルタして、注目銘柄を保存できます。</p>
        <ul>
            <li>🔎 「じわじわ高値ブレイク / バリュー / グロース」のスタイル別スクリーニング</li>
            <li>🕯️ じわじわ高値ブレイクは <b>上ひげ・下ひげが長くない</b>ことも条件に含めて騙しを抑制</li>
            <li>🔗 複数スタイルの <b>AND/OR 検索</b>: AND は完全一致が無くてもマッチしたスタイル数の多い順に top N を表示</li>
            <li>🔀 <b>別スタイルで再評価</b>：機械スクリーニング結果に対し、別スタイル（例：バリュー）の合否を一括チェック</li>
            <li>⭐ 結果から「注目」ボタンでウォッチリストに追加</li>
            <li>🤖 質的分析ボタンで直近 IR・ニュース・決算を補強</li>
            <li>株価データは分割調整済み（チャートと一致）、当日引け後は当日終値で判定</li>
        </ul>`
    },
    {
        title: 'マネージャー通知ログ',
        target: 'log',
        body: `<p>📨 朝の市場の地合い・保有銘柄ニュース朝刊などの <b>長文の自動配信</b> は、チャットを埋めないよう「ログ」タブの「マネージャー通知ログ」に格納されます。</p>
        <ul>
            <li>未読件数バッジ・未読カードのハイライト・カテゴリアイコン・相対時刻表示</li>
            <li>カードを展開すると自動で既読化。「◯ 未読」ボタンで未読に戻せます</li>
            <li>「🗑 削除」ボタンで個別削除</li>
            <li>保有銘柄ニュース朝刊は <b>前回からの変化分のみ</b> 配信されます（毎日同じ記事は出ません）</li>
        </ul>`
    },
    {
        title: 'マネージャー連絡スケジュール',
        target: null,
        body: `<p>⚙ 設定 → 「📅 マネージャー連絡スケジュール」で各タスクの有効/無効を切替できます（時刻は重複しないよう固定）。</p>
        <ul>
            <li>📨 <b>マネージャー連絡</b>（ユーザーへ通知あり）: 朝のMIT 06:30 / 市場の地合い 06:45 / 朝のルーチン 07:00 / 価格アラート＋決算 07:15 / Fitbit朝レポート 08:00 / 保有銘柄ニュース朝刊 08:30 / コストアラート 09:00 / Obsidian振り返り 20:00 / 週末株レビュー 20:15（金）/ 習慣チェック 20:30 / 明日の予定 21:00 / 週次レビュー 21:30（日）/ 夜の振り返り 22:00 / Fitbit夜レポート 22:15 / ロケーション保存リマインド 22:30 / デイリー整理 23:55</li>
            <li>🔄 <b>自動同期</b>（通知なし）: Fitbitキャッシュ事前取得 23:00 / 取扱説明書自動更新 23:45</li>
            <li>ON/OFF のみ切替可能。Bot 再起動不要で即時反映</li>
        </ul>`
    },
    {
        title: 'PayPay 共有から支出記録',
        target: null,
        body: `<p>PayPay の取引履歴で「共有」 → このアプリを選ぶと、自動で支出記録モーダルが開きます。</p>
        <ul>
            <li>💴 金額・店名・支払方法をテキストから自動抽出</li>
            <li>📝 内容を確認・修正してから保存</li>
        </ul>`
    },
    {
        title: 'Gmail 自動同期',
        target: null,
        body: `<p>Gmail の状態がローカル DB と双方向同期されます。</p>
        <ul>
            <li>🆕 受信時: AI が要約・重要度判定して保存</li>
            <li>🗑 Gmail 側で削除: 7分以内に PWA からも消える</li>
            <li>📲 メールの「↗ Gmail で開く」: モバイルでは Gmail アプリを起動（Web 版にフォールバック）</li>
        </ul>`
    },
    {
        title: 'アプリで開く設定（モバイル）',
        target: null,
        body: `<p>Gmail / Yahoo!天気 など外部サービスをアプリで起動するための端末設定。</p>
        <ul>
            <li><b>iPhone</b>: 設定 → Safari → リンク → アプリで開く</li>
            <li><b>Android</b>: 設定 → アプリ → 各アプリ → 既定で開く → 対応するリンクを開く</li>
            <li>一部の Yahoo!天気アプリは Universal Link 未対応で Web 版にフォールバックします</li>
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
            <li>⚙ コスト・スケジュールなどの設定</li>
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

window.openEarningsDocUploadModal = () => {
    const m = $('#earnings-doc-upload-modal');
    if (!m) return;
    const t = ($('#invest-ticker-input')?.value || '').trim();
    $('#edu-ticker').value = t || '';
    $('#edu-url').value = '';
    $('#edu-label').value = '';
    const fileInput = $('#edu-file'); if (fileInput) fileInput.value = '';
    $('#edu-status').textContent = '';
    m.classList.remove('hidden');
};

window.closeEarningsDocUploadModal = () => {
    const m = $('#earnings-doc-upload-modal');
    if (m) m.classList.add('hidden');
};

window.saveEarningsDocFromUrl = async () => {
    const ticker = ($('#edu-ticker')?.value || '').trim();
    const url = ($('#edu-url')?.value || '').trim();
    const label = ($('#edu-label')?.value || '').trim();
    const fileInput = $('#edu-file');
    const file = fileInput && fileInput.files && fileInput.files[0];
    if (!ticker) { showToast('ティッカーを入力してください', true); return; }
    if (!url && !file) { showToast('URL かファイルのどちらかを指定してください', true); return; }

    const btn = $('#edu-save-btn');
    if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
    $('#edu-status').textContent = 'Drive に保存中…';
    try {
        let data;
        if (file) {
            const fd = new FormData();
            fd.append('ticker', ticker);
            fd.append('label', label);
            fd.append('file', file);
            data = await apiFetch('/api/investment/earnings_documents/save_file', {
                method: 'POST',
                body: fd,
                _isFormData: true,
            });
        } else {
            data = await apiFetch('/api/investment/earnings_documents/save_url', {
                method: 'POST',
                body: JSON.stringify({ ticker, url, label }),
            });
        }
        if (data && data.ok) {
            const link = data.drive_link ? `\nDrive: ${data.drive_link}` : '';
            $('#edu-status').innerHTML = `✅ 保存しました: <code>${escapeHtml(data.filename)}</code><br><small>保存先: ${escapeHtml(data.folder)}</small>` + (data.drive_link ? `<br><a href="${data.drive_link}" target="_blank" class="mini-link">📂 Drive で開く</a>` : '');
            showToast(`📥 ${data.filename} を保存しました`);
            $('#edu-url').value = '';
            $('#edu-label').value = '';
            if (fileInput) fileInput.value = '';
        } else {
            const err = (data && (data.error || data.detail)) || '保存に失敗しました';
            $('#edu-status').textContent = `❌ ${err}`;
            showToast(`保存失敗: ${err}`, true);
        }
    } catch (e) {
        $('#edu-status').textContent = `❌ 通信エラー: ${e.message || e}`;
        showToast(`通信エラー: ${e.message || e}`, true);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '💾 保存'; }
    }
};

window.runEarningsDocuments = async () => {
    const ticker = _getTickerInput();
    if (!ticker) return;
    // 取得は Gemini 検索を経由するため 20〜60 秒かかる。ボタンを押した直後にモーダルを開いて待機中であることを明示する。
    window.openInvestmentResultModal(
        `📑 ${ticker} 決算関連資料`,
        `⌛ **取得中…**\n\n公式IRページや EDINET / SEC EDGAR から最新の決算資料情報を Gemini が収集しています。\n通常 20〜60 秒ほどかかります。このモーダルを閉じずにそのままお待ちください。`,
    );
    const data = await _callInvestmentApi('/api/investment/earnings_documents', { ticker }, `${ticker} 決算資料`);
    if (data && data.ok) {
        window.openInvestmentResultModal(`📑 ${data.ticker} 決算関連資料`, data.report);
        window.loadInvestmentHistory();
    } else {
        const err = (data && (data.error || data.detail)) || '取得に失敗しました。少し時間を置いてからもう一度お試しください。';
        window.openInvestmentResultModal(`📑 ${ticker} 決算関連資料`, `❌ ${err}`);
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
    textarea.value = '読み込み中…';
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
    listEl.innerHTML = '<div class="loading-placeholder">読み込み中…</div>';
    try {
        const data = await apiFetch(`/api/investment/history/${encodeURIComponent(_investHistoryCategory)}?limit=20`);
        if (!data || !data.ok) {
            listEl.innerHTML = '<div class="loading-placeholder">履歴の取得に失敗しました。</div>';
            return;
        }
        const items = data.items || [];
        if (!items.length) {
            listEl.innerHTML = '<div class="loading-placeholder">まだ履歴がありません。</div>';
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
        listEl.innerHTML = `<div class="loading-placeholder">エラー: ${escapeHtml(e.message || String(e))}</div>`;
    }
};

window.openInvestHistoryItem = async (category, fileId, name) => {
    showToast(`${name} を読み込み中…`);
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

// =================================================================
// Investment Tab — 銘柄スクリーナー
// =================================================================

let _screenerSelectedStyles = new Set();
let _screenerStylesCache = null;
let _screenerCandidates = [];
let _screenerJobId = null;
let _screenerJobPollTimer = null;
let _screenerFakeProgressTimer = null;
let _screenerFakeProgressPct = 0;
// {style_name: Set<filter_key>} - スタイルごとに ON 状態の構成要素キー
let _screenerStyleFilters = {};
let _screenerLastResult = null;        // 最後のスクリーニング結果（保存用）
let _screenerLastQualitativeReport = null; // 最後の質的分析レポートMarkdown

window.openScreenerModal = async () => {
    const modal = $('#invest-screener-modal');
    if (!modal) return;
    modal.classList.remove('hidden');
    await _loadScreenerStyles();
    await _loadScreenerUniverses();
    _renderScreenerResults('スタイルを1つ以上選んで「機械スクリーニング」を実行してください。');
    _setScreenerAnalyzeEnabled(false);
};

window.closeScreenerModal = () => {
    if (_screenerJobPollTimer) { clearInterval(_screenerJobPollTimer); _screenerJobPollTimer = null; }
    $('#invest-screener-modal')?.classList.add('hidden');
    _screenerSelectedStyles.clear();
    _screenerCandidates = [];
    _screenerStyleFilters = {};
};

async function _loadScreenerStyles() {
    const chipsEl = $('#screener-style-chips');
    if (!chipsEl) return;
    if (!_screenerStylesCache) {
        try {
            const data = await apiFetch('/api/investment/screener/styles');
            _screenerStylesCache = (data && data.styles) || [];
        } catch (e) {
            chipsEl.innerHTML = `<span style="color:#ff6b6b;font-size:0.78rem;">スタイル取得失敗: ${escapeHtml(e.message || e)}</span>`;
            return;
        }
    }
    if (!_screenerStylesCache.length) {
        chipsEl.innerHTML = '<span class="loading-placeholder">スタイルが登録されていません。</span>';
        return;
    }
    chipsEl.innerHTML = _screenerStylesCache.map(s => {
        const active = _screenerSelectedStyles.has(s.name) ? ' active' : '';
        return `<button class="chip-btn${active}" data-style="${escapeHtml(s.name)}" title="${escapeHtml(s.description || '')}" onclick="_selectScreenerStyle('${s.name.replace(/'/g, '&#39;')}')">${escapeHtml(s.display_name)}</button>`;
    }).join('');
    _renderScreenerFilters();
}

function _renderScreenerFilters() {
    const wrap = $('#screener-filters-wrap');
    if (!wrap) return;
    if (!_screenerStylesCache || !_screenerSelectedStyles.size) {
        wrap.innerHTML = '<div style="font-size:0.76rem;color:var(--text-muted);">スタイルを選ぶと、その構成条件が表示されます。チェックを外すと、その条件を必須から外して評価します。</div>';
        return;
    }
    const blocks = [];
    for (const styleName of _screenerSelectedStyles) {
        const s = _screenerStylesCache.find(x => x.name === styleName);
        if (!s || !s.filters || !s.filters.length) continue;
        if (!_screenerStyleFilters[styleName]) {
            _screenerStyleFilters[styleName] = new Set(s.filters.filter(f => f.default).map(f => f.key));
        }
        const enabled = _screenerStyleFilters[styleName];
        const items = s.filters.map(f => {
            const checked = enabled.has(f.key) ? 'checked' : '';
            return `<label style="display:flex;align-items:flex-start;gap:6px;padding:3px 0;cursor:pointer;font-size:0.78rem;">
                <input type="checkbox" ${checked} onchange="_toggleScreenerFilter('${styleName}','${f.key}',this.checked)" style="margin-top:3px;">
                <div>
                    <div style="color:var(--text-primary);">${escapeHtml(f.label)}</div>
                    <div style="color:var(--text-muted);font-size:0.72rem;">${escapeHtml(f.description || '')}</div>
                </div>
            </label>`;
        }).join('');
        blocks.push(`<div style="margin-bottom:8px;padding:8px;border:1px solid var(--border-glass);border-radius:6px;">
            <div style="font-size:0.8rem;font-weight:700;color:var(--accent);margin-bottom:4px;">${escapeHtml(s.display_name)}</div>
            ${items}
        </div>`);
    }
    wrap.innerHTML = blocks.join('') || '<div style="font-size:0.76rem;color:var(--text-muted);">選択スタイルに構成要素はありません。</div>';
}

window._toggleScreenerFilter = (styleName, filterKey, isChecked) => {
    if (!_screenerStyleFilters[styleName]) _screenerStyleFilters[styleName] = new Set();
    if (isChecked) _screenerStyleFilters[styleName].add(filterKey);
    else _screenerStyleFilters[styleName].delete(filterKey);
};

async function _loadScreenerUniverses() {
    const sel = $('#screener-universe-select');
    if (!sel) return;
    try {
        const data = await apiFetch('/api/investment/screener/universes');
        const names = (data && data.universes) || [];
        if (!names.length) return;
        const labelOf = n => {
            if (n === 'all') return '全銘柄 (要 data/jp_universe_all.csv)';
            if (n === 'topix500') return 'TOPIX500';
            return n.toUpperCase();
        };
        sel.innerHTML = names.map(n => `<option value="${escapeHtml(n)}">${escapeHtml(labelOf(n))}</option>`).join('');
    } catch (e) {
        // フォールバック: HTML側のデフォルト値を使う
    }
}

window._selectScreenerStyle = (name) => {
    if (_screenerSelectedStyles.has(name)) {
        _screenerSelectedStyles.delete(name);
    } else {
        _screenerSelectedStyles.add(name);
    }
    document.querySelectorAll('#screener-style-chips .chip-btn').forEach(b => {
        b.classList.toggle('active', _screenerSelectedStyles.has(b.dataset.style));
    });
    _renderScreenerFilters();
};

function _setScreenerAnalyzeEnabled(enabled) {
    const btn = $('#screener-analyze-btn');
    if (btn) {
        btn.disabled = !enabled;
        btn.style.opacity = enabled ? '1' : '0.5';
    }
    const gemBtn = $('#screener-gem-btn');
    if (gemBtn) {
        gemBtn.disabled = !enabled;
        gemBtn.style.opacity = enabled ? '1' : '0.5';
    }
}

const GEMINI_GEM_URL_KEY = 'gemini_screener_gem_url';
const GEMINI_GEM_URL_DEFAULT = 'https://gemini.google.com/gems/view';
// 設定画面で保存された Gem URL のキャッシュ {key: url}
let _gemUrlCache = null;

async function _fetchGemUrls() {
    try {
        const data = await apiFetch('/api/settings/gem_urls');
        const out = {};
        (data.items || []).forEach(it => { out[it.key] = it.url || ''; });
        // 旧 localStorage からの自動移行（1回限り）
        const legacy = localStorage.getItem(GEMINI_GEM_URL_KEY);
        if (legacy && !out.investment_screener) {
            try {
                await apiFetch('/api/settings/gem_urls', {
                    method: 'POST',
                    body: JSON.stringify({ values: { investment_screener: legacy } }),
                });
                out.investment_screener = legacy;
            } catch {}
            localStorage.removeItem(GEMINI_GEM_URL_KEY);
        }
        _gemUrlCache = out;
        return out;
    } catch {
        _gemUrlCache = {};
        return {};
    }
}

async function _getGemUrl(key) {
    if (!_gemUrlCache) await _fetchGemUrls();
    return (_gemUrlCache || {})[key] || '';
}

// 旧式（プロンプト）：設定画面へ誘導するのみ
window.setGeminiGemUrl = () => {
    showToast('Gem URL は ⚙️ 設定 →「🔗 Gemini Gem URL」から登録してください');
    try { if (typeof openSettingsModal === 'function') openSettingsModal(); } catch {}
};

window.openGeminiGemForScreener = async () => {
    if (!_screenerCandidates || !_screenerCandidates.length) {
        showToast('先に機械スクリーニングを実行してください', true);
        return;
    }
    const checked = Array.from(document.querySelectorAll('.screener-cand-check')).filter(c => c.checked).map(c => parseInt(c.dataset.idx, 10));
    const selected = (checked.length ? checked : _screenerCandidates.map((_, i) => i)).map(i => _screenerCandidates[i]).filter(Boolean);
    if (!selected.length) {
        showToast('銘柄が選択されていません', true);
        return;
    }
    const data = _screenerLastResult || {};
    const styleLabel = data.style_display || (data.styles || []).join(' / ') || '-';
    const header = `# 機械スクリーニング結果 (${styleLabel})\n実行: ${data.executed_at || ''} / 基準日: ${data.data_as_of || ''} / 走査 ${data.scanned || '-'} → 通過 ${data.qualified || '-'} → 上位 ${selected.length} 件\n\n`;
    const rows = selected.map((c, i) => {
        const ps = c.price_snapshot || {};
        const sigs = (c.signals || []).map(s => `${s.name}=${s.value}`).join(', ');
        return `${i + 1}. ${c.code} ${c.name || ''}\n   セクター: ${c.sector || '-'} / スコア: ${c.score}\n   終値: ${ps.close ?? '-'} (${ps.change_pct ?? 0}%) / 52週: ${ps.low_52w ?? '-'}〜${ps.high_52w ?? '-'}\n   シグナル: ${sigs}`;
    }).join('\n\n');
    const promptHint = `\n\n---\n上記の銘柄について、直近の IR・ニュース・決算・投資憲法との整合性を踏まえて質的分析をお願いします。`;
    const text = header + rows + promptHint;

    try {
        await navigator.clipboard.writeText(text);
        showToast(`📋 ${selected.length} 銘柄の情報をコピーしました`);
    } catch (e) {
        console.warn('clipboard failed', e);
        showToast('クリップボードへのコピーに失敗しました（ブラウザ権限を確認）', true);
    }

    const stored = await _getGemUrl('investment_screener');
    const url = stored || GEMINI_GEM_URL_DEFAULT;
    if (!stored) {
        showToast('Gem URL が未設定です。⚙️ 設定 → 「🔗 Gemini Gem URL」から登録してください', true);
    }
    window.open(url, '_blank', 'noopener');
};

function _renderScreenerResults(html) {
    const el = $('#screener-results');
    if (el) el.innerHTML = (typeof html === 'string') ? `<div class="loading-placeholder">${escapeHtml(html)}</div>` : html;
}

function _showScreenerProgress(text, percent) {
    const wrap = $('#screener-progress');
    const txt = $('#screener-progress-text');
    const bar = $('#screener-progress-bar');
    if (!wrap) return;
    wrap.classList.remove('hidden');
    if (txt) txt.textContent = text || '処理中...';
    if (typeof percent === 'number') {
        const clamped = Math.max(0, Math.min(100, percent));
        _screenerFakeProgressPct = clamped;
        if (bar) bar.style.width = `${clamped}%`;
    }
}

function _startScreenerFakeProgress(target = 92, intervalMs = 700) {
    _stopScreenerFakeProgress();
    _screenerFakeProgressTimer = setInterval(() => {
        const bar = $('#screener-progress-bar');
        if (!bar) return;
        const remaining = target - _screenerFakeProgressPct;
        if (remaining <= 0.1) return;
        const inc = Math.max(0.4, remaining * 0.07);
        _screenerFakeProgressPct = Math.min(target, _screenerFakeProgressPct + inc);
        bar.style.width = `${_screenerFakeProgressPct}%`;
    }, intervalMs);
}

function _stopScreenerFakeProgress() {
    if (_screenerFakeProgressTimer) {
        clearInterval(_screenerFakeProgressTimer);
        _screenerFakeProgressTimer = null;
    }
}

function _hideScreenerProgress() {
    _stopScreenerFakeProgress();
    _screenerFakeProgressPct = 0;
    const bar = $('#screener-progress-bar');
    if (bar) bar.style.width = '0%';
    const txt = $('#screener-progress-text');
    if (txt) txt.textContent = '';
    const wrap = $('#screener-progress');
    if (wrap) {
        wrap.classList.add('hidden');
        wrap.style.display = 'none';
    }
}

function _setScreenerProgressDone(label = '完了') {
    _stopScreenerFakeProgress();
    _screenerFakeProgressPct = 100;
    const bar = $('#screener-progress-bar');
    if (bar) bar.style.width = '100%';
    const txt = $('#screener-progress-text');
    if (txt) txt.textContent = label;
    // 完了表示は0.8秒ほど残してから消す
    setTimeout(() => _hideScreenerProgress(), 800);
}

window.runScreener = async () => {
    if (_screenerSelectedStyles.size === 0) {
        showToast('スタイルを1つ以上選んでください', true);
        return;
    }
    const universe = $('#screener-universe-select')?.value || 'topix500';
    const topN = parseInt($('#screener-top-n')?.value || '10', 10);
    const minMcapOku = parseInt($('#screener-min-mcap')?.value || '0', 10);
    const minMcapJpy = (minMcapOku && minMcapOku > 0) ? minMcapOku * 100000000 : null;

    _renderScreenerResults('スクリーニング中...');
    _showScreenerProgress('スクリーニング中...', 5);
    _startScreenerFakeProgress(92, 700);
    _setScreenerAnalyzeEnabled(false);

    try {
        const combineMode = document.querySelector('input[name="screener-combine-mode"]:checked')?.value || 'any';
        const body = { styles: [..._screenerSelectedStyles], universe, top_n: topN, combine_mode: combineMode };
        if (minMcapJpy) body.min_market_cap_jpy = minMcapJpy;
        const filter_overrides = {};
        for (const styleName of _screenerSelectedStyles) {
            if (_screenerStyleFilters[styleName]) {
                filter_overrides[styleName] = Array.from(_screenerStyleFilters[styleName]);
            }
        }
        if (Object.keys(filter_overrides).length) body.filter_overrides = filter_overrides;
        const data = await apiFetch('/api/investment/screener/run', { method: 'POST', body: JSON.stringify(body) });
        _setScreenerProgressDone('スクリーニング完了');
        if (!data || !data.ok) {
            const err = (data && (data.error || data.detail)) || '失敗しました';
            _renderScreenerResults(`エラー: ${err}`);
            showToast(`スクリーニング失敗: ${err}`, true);
            return;
        }
        _screenerCandidates = data.candidates || [];
        _screenerLastResult = data;
        _screenerLastQualitativeReport = null;
        _clearQualitativeResult();
        _renderScreenerCandidates(data);
        _setScreenerAnalyzeEnabled(_screenerCandidates.length > 0);
        _updateScreenerSaveButton();
        if (data.used_near_miss) {
            showToast(`条件通過なし。代わりに「条件に近い銘柄」${_screenerCandidates.length} 件を表示`, true);
        } else {
            showToast(`${_screenerCandidates.length} 銘柄が条件を通過しました`);
        }
    } catch (e) {
        _hideScreenerProgress();
        _renderScreenerResults(`通信エラー: ${e.message || e}`);
    }
};

function _renderScreenerCandidates(data) {
    const el = $('#screener-results');
    if (!el) return;
    const cands = data.candidates || [];

    // 実行条件サマリー
    const filtersByStyle = data.applied_filters_by_style || {};
    const filtersHtml = Object.keys(filtersByStyle).length
        ? Object.entries(filtersByStyle).map(([sn, fs]) => {
            const sObj = (_screenerStylesCache || []).find(x => x.name === sn);
            const sLabel = sObj ? sObj.display_name : sn;
            const items = (fs || []).map(f => `<span style="display:inline-block;margin:1px 3px 1px 0;padding:1px 6px;border-radius:8px;background:rgba(78,161,255,0.12);color:#4ea1ff;font-size:0.7rem;">${escapeHtml(f.label)}</span>`).join('');
            return `<div style="margin-top:4px;"><span style="color:var(--text-primary);font-size:0.74rem;font-weight:700;">${escapeHtml(sLabel)}:</span> ${items || '<span style="color:var(--text-muted);font-size:0.7rem;">条件なし</span>'}</div>`;
        }).join('')
        : '';

    const isAnd = data.combine_mode === 'all' && (data.styles || []).length > 1;
    const andBanner = isAnd
        ? `<div style="padding:6px 10px;margin-bottom:8px;background:rgba(78,161,255,0.1);border-left:3px solid #4ea1ff;border-radius:4px;font-size:0.76rem;color:#4ea1ff;">🔗 AND モード: 選択した全スタイルの条件をすべて満たす銘柄のみを表示しています。</div>`
        : '';

    const meta = `<div style="font-size:0.78rem;color:var(--text-muted);margin-bottom:8px;">
        <div>スタイル: ${escapeHtml(data.style_display || (data.styles || [data.style]).join(' / '))}</div>
        <div>ユニバース: ${escapeHtml(data.universe || '-')} / データ基準日: ${escapeHtml(data.data_as_of || '')} / 実行: ${escapeHtml(data.executed_at || '')}</div>
        <div>走査 ${data.scanned} 銘柄 → ${data.qualified} 通過 → 上位 ${cands.length} 件</div>
        ${filtersHtml ? `<details style="margin-top:6px;"><summary style="cursor:pointer;color:var(--text-secondary);font-size:0.74rem;">📋 適用した条件</summary>${filtersHtml}</details>` : ''}
    </div>`;

    if (!cands.length) {
        el.innerHTML = andBanner + meta + `<div class="loading-placeholder">条件に該当する銘柄が見つかりませんでした。${isAnd ? 'ANDモードでは条件が厳しくなります。構成要素のチェックを減らすか、ORモードをお試しください。' : '条件を緩めるか、ユニバースを「全銘柄」に変えてみてください。'}</div>`;
        return;
    }

    const nearMissBanner = data.used_near_miss
        ? `<div style="padding:8px;margin-bottom:8px;background:rgba(255,212,84,0.12);border-left:3px solid #ffd454;border-radius:4px;font-size:0.78rem;color:#ffd454;">⚠️ 全条件を満たす銘柄はありませんでした。代わりに「条件に近い銘柄」を提示しています（赤バッジ＝満たさなかった条件）。</div>`
        : '';

    const bulkToggle = `<div style="font-size:0.74rem;margin-bottom:6px;color:var(--text-secondary);">
        <a href="#" onclick="event.preventDefault();_screenerCheckAll(true);" style="color:#4ea1ff;cursor:pointer;text-decoration:underline;">☑ すべて選択</a>
        <span style="opacity:0.5;margin:0 4px;">/</span>
        <a href="#" onclick="event.preventDefault();_screenerCheckAll(false);" style="color:#4ea1ff;cursor:pointer;text-decoration:underline;">☐ すべて解除</a>
    </div>`;

    const rows = cands.map((c, idx) => {
        const sigBadges = (c.signals || []).map(s => {
            const color = s.passed ? '#7cd6a0' : '#ff8a8a';
            return `<span style="display:inline-block;margin:1px 4px 1px 0;padding:1px 6px;border-radius:8px;background:rgba(255,255,255,0.05);color:${color};font-size:0.72rem;">${escapeHtml(s.name)}: ${escapeHtml(s.value)}</span>`;
        }).join('');
        const failedBadge = (c.is_near_miss && (c.failed_filters || []).length)
            ? `<div style="font-size:0.7rem;color:#ff8a8a;margin-bottom:4px;">🔻 不足: ${(c.failed_filters || []).map(f => escapeHtml(f)).join(' / ')}</div>`
            : '';
        const ps = c.price_snapshot || {};
        const matchedStyles = c.matched_styles || [];
        const styleBadges = matchedStyles.map(ms => `<span style="display:inline-block;margin:1px 4px 1px 0;padding:1px 6px;border-radius:8px;background:rgba(78,161,255,0.15);color:#4ea1ff;font-size:0.70rem;">${escapeHtml(ms)}</span>`).join('');
        const codeEsc = c.code.replace(/'/g, "\\'");
        const nameEsc = (c.name || '').replace(/'/g, "\\'");
        const sectorEsc = (c.sector || '').replace(/'/g, "\\'");
        const sourceEsc = (data.style_display || (data.styles || [data.style]).join(' / ') || '').replace(/'/g, "\\'");
        return `<div class="screener-row" style="padding:8px;border:1px solid var(--border-glass);border-radius:6px;margin-bottom:6px;${c.is_near_miss ? 'background:rgba(255,212,84,0.05);' : ''}">
            <label style="display:flex;align-items:flex-start;gap:8px;cursor:pointer;">
                <input type="checkbox" data-idx="${idx}" class="screener-cand-check" checked style="margin-top:4px;flex-shrink:0;">
                <div style="flex:1;min-width:0;">
                    <div style="margin-bottom:4px;">
                        <strong style="word-break:break-word;line-height:1.3;">${escapeHtml(c.code)} ${escapeHtml(c.name)}</strong>
                    </div>
                    <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;margin-bottom:4px;flex-wrap:wrap;">
                        <span style="font-size:0.78rem;color:var(--text-muted);">スコア ${c.score} / セクター ${escapeHtml(c.sector || '-')}</span>
                        <button class="mini-link" style="font-size:0.72rem;padding:2px 6px;flex-shrink:0;" onclick="event.preventDefault();event.stopPropagation();_addScreenerToWatchlist('${codeEsc}','${nameEsc}','${sectorEsc}','${sourceEsc}')">⭐ 注目</button>
                    </div>
                    ${styleBadges ? `<div style="margin-bottom:4px;">${styleBadges}</div>` : ''}
                    ${failedBadge}
                    <div style="font-size:0.76rem;color:var(--text-muted);margin-bottom:4px;">終値 ${ps.close ?? '-'} (${ps.change_pct ?? 0}%) / 52週: ${ps.low_52w ?? '-'}〜${ps.high_52w ?? '-'}</div>
                    <div>${sigBadges}</div>
                </div>
            </label>
        </div>`;
    }).join('');
    el.innerHTML = andBanner + meta + nearMissBanner + bulkToggle + rows + `<div style="font-size:0.72rem;color:var(--text-muted);margin-top:8px;">⚠️ 機械的なフィルタ結果です。質的分析ボタンで Gemini が直近 IR・ニュース・決算情報を出典 URL 付きで補強します。</div>`;

    // 副条件（スタイル横断フィルタ）UI: スタイル選択肢の補充とボタン有効化
    _populateSecondaryStyleSelect();
    const secBtn = $('#screener-secondary-btn');
    if (secBtn) {
        secBtn.disabled = cands.length === 0;
        secBtn.style.opacity = cands.length ? '1' : '0.5';
    }
}

function _populateSecondaryStyleSelect() {
    const sel = $('#screener-secondary-style');
    if (!sel || !_screenerStylesCache) return;
    if (sel.options.length === _screenerStylesCache.length) return;
    sel.innerHTML = _screenerStylesCache.map(s =>
        `<option value="${escapeHtml(s.name)}">${escapeHtml(s.display_name)}</option>`
    ).join('');
}

window.runScreenerCrossFilter = async () => {
    if (!_screenerCandidates || !_screenerCandidates.length) {
        showToast('先に機械スクリーニングを実行してください', true);
        return;
    }
    const sel = $('#screener-secondary-style');
    const secondary = sel ? sel.value : '';
    if (!secondary) {
        showToast('別スタイルを選択してください', true);
        return;
    }
    const checkedOnly = $('#screener-secondary-checked-only')?.checked;
    let targets = _screenerCandidates;
    if (checkedOnly) {
        const checks = Array.from(document.querySelectorAll('.screener-cand-check'))
            .filter(c => c.checked).map(c => parseInt(c.dataset.idx, 10));
        targets = checks.map(i => _screenerCandidates[i]).filter(Boolean);
    }
    if (!targets.length) {
        showToast('対象銘柄がありません', true);
        return;
    }
    const btn = $('#screener-secondary-btn');
    if (btn) { btn.disabled = true; btn.textContent = '🔀 評価中...'; }
    try {
        const data = await apiFetch('/api/investment/screener/cross_filter', {
            method: 'POST',
            body: JSON.stringify({
                candidates: targets.map(c => ({ code: c.code, name: c.name, sector: c.sector })),
                secondary_style: secondary,
            }),
        });
        if (!data || !data.ok) {
            showToast(`再評価失敗: ${data && data.error || '不明なエラー'}`, true);
            return;
        }
        _renderSecondaryEvaluation(data);
        if (_screenerLastResult) _screenerLastResult.secondary_evaluation = data;
    } catch (e) {
        showToast(`通信エラー: ${e.message || e}`, true);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '🔀 実行'; }
    }
};

function _renderSecondaryEvaluation(data) {
    const el = $('#screener-results');
    if (!el) return;
    let host = document.getElementById('screener-secondary-block');
    if (!host) {
        host = document.createElement('div');
        host.id = 'screener-secondary-block';
        host.style.cssText = 'margin-top:10px;padding:10px;border:1px dashed var(--border-glass);border-radius:6px;';
        el.appendChild(host);
    }
    const items = data.items || [];
    const passN = items.filter(x => x.secondary_pass).length;
    const rowsHtml = items.map(it => {
        const mark = it.secondary_pass ? '🟢 合格' : '🔴 不合格';
        const sigs = (it.secondary_signals || []).map(s => {
            const color = s.passed ? '#7cd6a0' : '#ff8a8a';
            return `<span style="display:inline-block;margin:1px 4px 1px 0;padding:1px 6px;border-radius:8px;background:rgba(255,255,255,0.05);color:${color};font-size:0.72rem;">${escapeHtml(s.name)}: ${escapeHtml(s.value)}</span>`;
        }).join('');
        return `<details style="border-bottom:1px solid var(--border-glass);padding:4px 0;">
            <summary style="cursor:pointer;font-size:0.82rem;">
                <b>${escapeHtml(it.code)}</b> ${escapeHtml(it.name || '')} — ${mark}（スコア ${it.secondary_score ?? '-'}）
            </summary>
            <div style="padding:6px 4px;">${sigs || '<span style="color:var(--text-muted);font-size:0.74rem;">シグナル無し</span>'}</div>
        </details>`;
    }).join('');
    host.innerHTML = `<div style="font-size:0.82rem;font-weight:700;margin-bottom:6px;">🔀 副条件：${escapeHtml(data.secondary_display || data.secondary_style)} の合否（合格 ${passN} / ${items.length}）</div>${rowsHtml}`;
}

window._addScreenerToWatchlist = async (code, name, sector, source) => {
    try {
        const data = await apiFetch('/api/investment/watchlist', {
            method: 'POST',
            body: JSON.stringify({ code, name, sector, source }),
        });
        if (data && data.ok) {
            showToast(`⭐ ${code} ${name} を注目銘柄に追加しました`);
            if (typeof loadWatchlist === 'function') loadWatchlist();
        } else {
            showToast('追加に失敗しました', true);
        }
    } catch (e) {
        showToast(`通信エラー: ${e.message || e}`, true);
    }
};

window.runScreenerAnalyze = async () => {
    if (!_screenerCandidates.length) {
        showToast('先に機械スクリーニングを実行してください', true);
        return;
    }
    const checked = Array.from(document.querySelectorAll('.screener-cand-check')).filter(c => c.checked).map(c => parseInt(c.dataset.idx, 10));
    const selected = checked.map(i => _screenerCandidates[i]).filter(Boolean);
    if (!selected.length) {
        showToast('1 銘柄以上を選択してください', true);
        return;
    }
    const estSecs = selected.length * 15;
    if (!confirm(`${selected.length} 銘柄を Gemini で質的分析します。約 ${estSecs} 秒かかります。よろしいですか?`)) return;

    _setScreenerAnalyzeEnabled(false);
    _showScreenerProgress('質的分析中...', 5);

    try {
        const data = await apiFetch('/api/investment/screener/analyze', {
            method: 'POST',
            body: JSON.stringify({ styles: [..._screenerSelectedStyles], candidates: selected }),
        });
        if (!data || !data.ok) {
            _hideScreenerProgress();
            _setScreenerAnalyzeEnabled(true);
            showToast(`起動失敗: ${(data && (data.error || data.detail)) || '不明'}`, true);
            return;
        }
        _screenerJobId = data.job_id;
        _pollScreenerJob();
    } catch (e) {
        _hideScreenerProgress();
        _setScreenerAnalyzeEnabled(true);
        showToast(`通信エラー: ${e.message || e}`, true);
    }
};

function _pollScreenerJob() {
    if (_screenerJobPollTimer) clearInterval(_screenerJobPollTimer);
    _screenerJobPollTimer = setInterval(async () => {
        if (!_screenerJobId) { clearInterval(_screenerJobPollTimer); _screenerJobPollTimer = null; return; }
        try {
            const data = await apiFetch(`/api/investment/screener/jobs/${encodeURIComponent(_screenerJobId)}`);
            if (!data || !data.ok) return;
            const prog = data.progress || {};
            const total = prog.total || 0;
            const cur = prog.current || 0;
            const ticker = prog.current_ticker || '';
            // 実進捗が出るまでも「質的分析中」のシンプル表示で統一（バーが後退しないように 5% 維持）
            if (data.status === 'pending' || total === 0) {
                _showScreenerProgress('質的分析中...', Math.max(5, _screenerFakeProgressPct));
            } else if (data.status === 'running') {
                const pct = Math.max(5, Math.round(cur / total * 100));
                _showScreenerProgress(`質的分析中... (${cur}/${total})`, pct);
            }
            if (data.status === 'done') {
                clearInterval(_screenerJobPollTimer); _screenerJobPollTimer = null;
                _showScreenerProgress('質的分析中...', 98);
                _setScreenerAnalyzeEnabled(true);
                showToast('質的分析が完了しました');
                if (data.report_markdown) {
                    _showQualitativeResult(data.report_markdown);
                }
                window.loadInvestmentHistory && window.loadInvestmentHistory();
                // 完了レポートが表示された後にゲージを片付ける
                setTimeout(_hideScreenerProgress, 600);
                _screenerJobId = null;
            } else if (data.status === 'error') {
                clearInterval(_screenerJobPollTimer); _screenerJobPollTimer = null;
                _hideScreenerProgress();
                _setScreenerAnalyzeEnabled(true);
                showToast(`エラー: ${data.error || '不明'}`, true);
                _screenerJobId = null;
            }
        } catch (e) {
            // 一過性のエラーは無視して次回ポーリングへ
        }
    }, 3000);
}

// 質的分析レポートをスクリーナーモーダル内に表示
function _showQualitativeResult(markdown) {
    _screenerLastQualitativeReport = markdown;
    const wrap = $('#screener-qualitative-result');
    const body = $('#screener-qualitative-body');
    if (!wrap || !body) return;
    const html = (typeof renderMarkdown === 'function') ? renderMarkdown(markdown) : `<pre style="white-space:pre-wrap;">${escapeHtml(markdown)}</pre>`;
    body.innerHTML = html;
    wrap.classList.remove('hidden');
    // 結果セクションへスクロール
    setTimeout(() => { wrap.scrollIntoView({ behavior: 'smooth', block: 'start' }); }, 100);
    // 質的分析が出たので保存ボタンを再評価
    _updateScreenerSaveButton();
}

window._clearQualitativeResult = () => {
    const wrap = $('#screener-qualitative-result');
    const body = $('#screener-qualitative-body');
    if (body) body.innerHTML = '';
    wrap?.classList.add('hidden');
    _screenerLastQualitativeReport = null;
    _updateScreenerSaveButton();
};

// 候補チェックボックスの一括選択/解除
window._screenerCheckAll = (flag) => {
    document.querySelectorAll('.screener-cand-check').forEach(c => { c.checked = !!flag; });
};

// 「💾 結果を保存」ボタンの活性化
function _updateScreenerSaveButton() {
    const btn = $('#screener-save-btn');
    if (!btn) return;
    const hasResult = _screenerLastResult && (_screenerLastResult.candidates || []).length > 0;
    btn.disabled = !hasResult;
    btn.style.opacity = hasResult ? '1' : '0.5';
}

// 結果を保存（候補一覧 + 質的分析レポート）
window.saveScreenerResult = async () => {
    if (!_screenerLastResult || !(_screenerLastResult.candidates || []).length) {
        showToast('保存できるスクリーニング結果がありません', true);
        return;
    }
    const defaultTitle = `${_screenerLastResult.style_display || '結果'} (${_screenerLastResult.executed_at || ''})`;
    const title = prompt('保存名を入力してください', defaultTitle);
    if (title === null) return;
    try {
        const body = {
            title: title || defaultTitle,
            styles: _screenerLastResult.styles || (_screenerLastResult.style ? [_screenerLastResult.style] : []),
            combine_mode: _screenerLastResult.combine_mode || 'any',
            universe: _screenerLastResult.universe || '',
            applied_filters: _screenerLastResult.applied_filters_by_style || {},
            candidates: _screenerLastResult.candidates || [],
            qualitative_report: _screenerLastQualitativeReport || '',
        };
        const data = await apiFetch('/api/investment/screener/runs', { method: 'POST', body: JSON.stringify(body) });
        if (data && data.ok) {
            showToast('💾 スクリーニング結果を保存しました');
            // 保存済み一覧が開いていたら更新
            if (!$('#screener-saved-details')?.hasAttribute('hidden')) {
                loadScreenerSavedList();
            }
        } else {
            showToast('保存失敗: ' + (data?.error || data?.detail || '不明'), true);
        }
    } catch (e) {
        showToast('保存失敗: ' + (e.message || e), true);
    }
};

// 保存済みスクリーニング一覧をロード
window.loadScreenerSavedList = async () => {
    const el = $('#screener-saved-list');
    if (!el) return;
    el.innerHTML = '<div class="loading-placeholder">読み込み中…</div>';
    try {
        const data = await apiFetch('/api/investment/screener/runs');
        if (!data || !data.ok) {
            el.innerHTML = '<div class="loading-placeholder">取得に失敗しました。</div>';
            return;
        }
        const items = data.items || [];
        if (!items.length) {
            el.innerHTML = '<div class="loading-placeholder">保存済みの結果はまだありません。</div>';
            return;
        }
        el.innerHTML = items.map(it => {
            const date = (it.created_at || '').replace('T', ' ').slice(0, 16);
            const reportTag = it.has_report ? '<span style="color:#7cd6a0;font-size:0.7rem;margin-left:6px;">🤖 質的分析あり</span>' : '';
            return `<div style="padding:6px 8px;border:1px solid var(--border-glass);border-radius:6px;margin-bottom:6px;">
                <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:6px;flex-wrap:wrap;">
                    <div style="flex:1;min-width:0;">
                        <div style="font-weight:600;font-size:0.84rem;word-break:break-word;">${escapeHtml(it.title || '(無題)')}${reportTag}</div>
                        <div style="font-size:0.7rem;color:var(--text-muted);margin-top:2px;">
                            ${escapeHtml(date)} / ${escapeHtml((it.styles || []).join(', '))} [${escapeHtml(it.combine_mode || 'any').toUpperCase()}] / ${it.candidate_count} 銘柄
                        </div>
                    </div>
                    <div style="display:flex;gap:4px;flex-shrink:0;">
                        <button class="mini-link" onclick="restoreScreenerRun(${it.id})">📋 復元</button>
                        <button class="mini-link" style="color:#ff6b6b;" onclick="deleteScreenerRun(${it.id})">🗑</button>
                    </div>
                </div>
            </div>`;
        }).join('');
    } catch (e) {
        el.innerHTML = `<div class="loading-placeholder">エラー: ${escapeHtml(e.message || String(e))}</div>`;
    }
};

window.restoreScreenerRun = async (runId) => {
    try {
        const data = await apiFetch(`/api/investment/screener/runs/${runId}`);
        if (!data || !data.ok || !data.data) {
            showToast('復元失敗', true);
            return;
        }
        const r = data.data;
        // 表示用 data オブジェクトを構築
        const restored = {
            ok: true,
            styles: r.styles || [],
            style_display: (r.styles || []).join(' / ') + ' [' + (r.combine_mode || 'any').toUpperCase() + '] (復元)',
            combine_mode: r.combine_mode || 'any',
            universe: r.universe || '',
            data_as_of: '(保存時点)',
            executed_at: (r.created_at || '').replace('T', ' ').slice(0, 16),
            scanned: '-',
            qualified: (r.candidates || []).length,
            applied_filters_by_style: r.applied_filters || {},
            used_near_miss: false,
            candidates: r.candidates || [],
        };
        _screenerCandidates = restored.candidates;
        _screenerLastResult = restored;
        _screenerLastQualitativeReport = r.qualitative_report || null;
        _renderScreenerCandidates(restored);
        _setScreenerAnalyzeEnabled(_screenerCandidates.length > 0);
        if (r.qualitative_report) {
            _showQualitativeResult(r.qualitative_report);
        } else {
            _clearQualitativeResult();
        }
        _updateScreenerSaveButton();
        showToast('📋 保存済み結果を復元しました');
    } catch (e) {
        showToast('復元失敗: ' + (e.message || e), true);
    }
};

window.deleteScreenerRun = async (runId) => {
    if (!confirm('この保存済み結果を削除しますか？')) return;
    try {
        const data = await apiFetch(`/api/investment/screener/runs/${runId}`, { method: 'DELETE' });
        if (data && data.ok) {
            showToast('🗑 削除しました');
            loadScreenerSavedList();
        } else {
            showToast('削除失敗', true);
        }
    } catch (e) {
        showToast('削除失敗: ' + (e.message || e), true);
    }
};

// =================================================================
// 注目銘柄 (Watchlist)
// =================================================================

window.loadWatchlist = async () => {
    const el = $('#invest-watchlist-list');
    if (!el) return;
    el.innerHTML = '<div class="loading-placeholder">読み込み中…</div>';
    try {
        const data = await apiFetch('/api/investment/watchlist');
        if (!data || !data.ok) {
            el.innerHTML = '<div class="loading-placeholder">取得に失敗しました。</div>';
            return;
        }
        const items = data.items || [];
        if (!items.length) {
            el.innerHTML = '<div class="loading-placeholder">注目銘柄はまだありません。スクリーニング結果から⭐ボタンで追加できます。</div>';
            return;
        }
        el.innerHTML = items.map(it => {
            const codeJs = it.code.replace(/'/g, "\\'");
            return `<div style="padding:8px;border-bottom:1px solid var(--border-glass);">
                <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;margin-bottom:4px;">
                    <strong>${escapeHtml(it.code)} ${escapeHtml(it.name || '')}</strong>
                    <span style="font-size:0.72rem;color:var(--text-muted);">${escapeHtml(it.sector || '')}</span>
                </div>
                ${it.source ? `<div style="font-size:0.7rem;color:var(--text-muted);margin-bottom:4px;">出典: ${escapeHtml(it.source)}</div>` : ''}
                <div style="display:flex;gap:4px;flex-wrap:wrap;">
                    <button class="mini-link" style="font-size:0.72rem;" onclick="runSnapshotForTicker('${codeJs}')">📷 スナップ</button>
                    <button class="mini-link" style="font-size:0.72rem;" onclick="runAuditForTicker('${codeJs}')">🎯 審査</button>
                    <button class="mini-link" style="font-size:0.72rem;" onclick="runPeerComparisonForTicker('${codeJs}')">🔬 同業</button>
                    <button class="mini-link" style="font-size:0.72rem;" onclick="runNewsSentimentForTicker('${codeJs}')">📰 ニュース</button>
                    <button class="mini-link" style="font-size:0.72rem;color:#ff8a8a;margin-left:auto;" onclick="removeFromWatchlist('${codeJs}')">削除</button>
                </div>
            </div>`;
        }).join('');
    } catch (e) {
        el.innerHTML = `<div class="loading-placeholder">通信エラー: ${escapeHtml(e.message || e)}</div>`;
    }
};

window.removeFromWatchlist = async (code) => {
    if (!confirm(`${code} を注目銘柄から削除しますか?`)) return;
    try {
        await apiFetch(`/api/investment/watchlist/${encodeURIComponent(code)}`, { method: 'DELETE' });
        showToast('削除しました');
        loadWatchlist();
    } catch (e) {
        showToast('削除に失敗しました', true);
    }
};

window.runSnapshotForTicker = async (ticker) => {
    const data = await _callInvestmentApi('/api/investment/snapshot', { ticker }, `${ticker} スナップショット`);
    if (data && data.ok) {
        window.openInvestmentResultModal(`📷 ${data.ticker} スナップショット`, data.report);
        window.loadInvestmentHistory && window.loadInvestmentHistory();
    }
};

window.runAuditForTicker = async (ticker) => {
    const data = await _callInvestmentApi('/api/investment/audit', { ticker }, `${ticker} 憲法審査`);
    if (data && data.ok) {
        window.openInvestmentResultModal(`🎯 ${data.ticker} 投資憲法審査`, data.audit);
        window.loadInvestmentHistory && window.loadInvestmentHistory();
    }
};

window.runPeerComparisonForTicker = async (ticker) => {
    const data = await _callInvestmentApi('/api/investment/peer_comparison', { ticker }, `${ticker} 同業比較`);
    if (data && data.ok) {
        window.openInvestmentResultModal(`🔬 ${data.ticker} 同業他社比較`, data.report);
        window.loadInvestmentHistory && window.loadInvestmentHistory();
    }
};

window.runNewsSentimentForTicker = async (ticker) => {
    const data = await _callInvestmentApi('/api/investment/news_sentiment', { ticker }, `${ticker} ニュース`);
    if (data && data.ok) {
        window.openInvestmentResultModal(`📰 ${data.ticker} ニュースセンチメント`, data.report);
        window.loadInvestmentHistory && window.loadInvestmentHistory();
    }
};

// =================================================================
// Investment Tab — 拡張機能 (ポートフォリオ・日記・アラート・他)
// =================================================================

// --- 同業比較 / ニュースセンチメント / 配当 ---

window.runPeerComparison = async () => {
    const ticker = _getTickerInput();
    if (!ticker) return;
    const data = await _callInvestmentApi('/api/investment/peer_comparison', { ticker }, `${ticker} 同業比較`);
    if (data && data.ok) {
        window.openInvestmentResultModal(`🔬 ${data.ticker} 同業他社比較`, data.report);
        window.loadInvestmentHistory();
    }
};

window.runNewsSentiment = async () => {
    const ticker = _getTickerInput();
    if (!ticker) return;
    const data = await _callInvestmentApi('/api/investment/news_sentiment', { ticker }, `${ticker} ニュース`);
    if (data && data.ok) {
        window.openInvestmentResultModal(`📰 ${data.ticker} ニュースセンチメント`, data.report);
        window.loadInvestmentHistory();
    }
};

window.runDividend = async () => {
    const ticker = _getTickerInput();
    if (!ticker) return;
    const data = await _callInvestmentApi('/api/investment/dividend', { ticker, register_calendar: true }, `${ticker} 配当`);
    if (data && data.ok) {
        window.openInvestmentResultModal(`💴 ${data.ticker} 配当カレンダー`, data.report);
        window.loadInvestmentHistory();
    }
};

// --- ポートフォリオ ---

// 直近に取得した保有銘柄キャッシュ（pickerやアクションシートが参照する）
let _investHoldingsCache = [];

window.loadPortfolio = async () => {
    const listEl = $('#invest-portfolio-list');
    if (!listEl) return;
    listEl.innerHTML = '<div class="invest-empty">読み込み中…</div>';
    try {
        const data = await apiFetch('/api/investment/portfolio');
        if (!data || !data.ok) {
            listEl.innerHTML = '<div class="loading-placeholder">取得に失敗しました。</div>';
            _investHoldingsCache = [];
            return;
        }
        const holdings = data.holdings || [];
        _investHoldingsCache = holdings;
        if (!holdings.length) {
            listEl.innerHTML = '<div class="loading-placeholder">保有銘柄がまだありません。「追加」から登録してください。</div>';
            return;
        }
        // 保存済み並び順を適用
        const savedOrder = _getPortfolioOrder();
        if (savedOrder.length) {
            holdings.sort((a, b) => {
                const ai = savedOrder.indexOf(a.code || '');
                const bi = savedOrder.indexOf(b.code || '');
                if (ai === -1 && bi === -1) return 0;
                if (ai === -1) return 1;
                if (bi === -1) return -1;
                return ai - bi;
            });
        }
        listEl.innerHTML = holdings.map(h => {
            const ticker = escapeHtml(h.code || '?');
            const name = escapeHtml(h.name || ticker);
            const sector = escapeHtml(h.sector || '');
            const shares = h.shares || 0;
            const cost = h.avg_cost || 0;
            const currency = escapeHtml(h.currency || '');
            const market = escapeHtml(h.market || '');
            const meta = `${shares}株 @ ${cost} ${currency} / ${market}${sector ? ' / ' + sector : ''}`;
            return `<div class="invest-row" data-code="${ticker.replace(/"/g, '&quot;')}" style="cursor:pointer;" onclick="openHoldingActionModal('${ticker.replace(/'/g, '&#39;')}')">
                <span class="portfolio-handle" onclick="event.stopPropagation();" style="cursor:grab;touch-action:none;color:var(--text-muted);font-size:1.1rem;padding:10px 10px 10px 0;user-select:none;" title="長押しして並び替え">⠿</span>
                <div class="row-main">
                    <div class="row-title">${name} (${ticker})</div>
                    <div class="row-meta">${meta}</div>
                </div>
                <div class="row-actions">
                    <button onclick="event.stopPropagation(); openHoldingActionModal('${ticker.replace(/'/g, '&#39;')}')">▾ 操作</button>
                </div>
            </div>`;
        }).join('');
        initPortfolioSortable(listEl);
    } catch (e) {
        listEl.innerHTML = `<div class="loading-placeholder">エラー: ${escapeHtml(e.message || String(e))}</div>`;
        _investHoldingsCache = [];
    }
};

// === 保有銘柄ピッカー（銘柄分析カード/CEOカード用） ===
let _holdingPickerTarget = null; // 'analysis' or 'ceo'

window.openHoldingPicker = async (target) => {
    _holdingPickerTarget = target;
    const modal = $('#invest-holding-picker-modal');
    const listEl = $('#invest-holding-picker-list');
    if (!modal || !listEl) return;
    listEl.innerHTML = '<div class="invest-empty">読み込み中…</div>';
    modal.classList.remove('hidden');
    try {
        if (!_investHoldingsCache.length) {
            const data = await apiFetch('/api/investment/portfolio');
            _investHoldingsCache = (data && data.ok) ? (data.holdings || []) : [];
        }
        const holdings = _investHoldingsCache;
        if (!holdings.length) {
            listEl.innerHTML = '<div class="invest-empty">保有銘柄がまだありません。ポートフォリオから追加してください</div>';
            return;
        }
        listEl.innerHTML = holdings.map(h => {
            const code = escapeHtml(h.code || '?');
            const name = escapeHtml(h.name || code);
            const market = escapeHtml(h.market || '');
            const shares = h.shares || 0;
            return `<div class="invest-row" style="cursor:pointer;" onclick="pickHolding('${code.replace(/'/g, '&#39;')}')">
                <div class="row-main">
                    <div class="row-title">${name} (${code})</div>
                    <div class="row-meta">${shares}株 / ${market}</div>
                </div>
            </div>`;
        }).join('');
    } catch (e) {
        listEl.innerHTML = `<div class="loading-placeholder">エラー: ${escapeHtml(e.message || String(e))}</div>`;
    }
};

window.closeHoldingPicker = () => {
    $('#invest-holding-picker-modal')?.classList.add('hidden');
};

window.pickHolding = (code) => {
    _setPickerTargetValue(_holdingPickerTarget, code);
    showToast(`${code} を選択しました`);
    window.closeHoldingPicker();
};

// === 注目銘柄ピッカー（銘柄分析・CEO・投資日記用） ===
let _watchlistPickerTarget = null; // 'analysis' | 'ceo' | 'journal'

window.openWatchlistPicker = async (target) => {
    _watchlistPickerTarget = target;
    const modal = $('#invest-watchlist-picker-modal');
    const listEl = $('#invest-watchlist-picker-list');
    if (!modal || !listEl) return;
    listEl.innerHTML = '<div class="invest-empty">読み込み中…</div>';
    modal.classList.remove('hidden');
    try {
        const data = await apiFetch('/api/investment/watchlist');
        const items = (data && data.ok) ? (data.items || []) : [];
        if (!items.length) {
            listEl.innerHTML = '<div class="invest-empty">注目銘柄がまだありません。スクリーニング結果から⭐ボタンで追加してください。</div>';
            return;
        }
        listEl.innerHTML = items.map(it => {
            const code = escapeHtml(it.code || '?');
            const name = escapeHtml(it.name || code);
            const sector = escapeHtml(it.sector || '');
            return `<div class="invest-row" style="cursor:pointer;" onclick="pickWatchlistTicker('${code.replace(/'/g, '&#39;')}')">
                <div class="row-main">
                    <div class="row-title">${name} (${code})</div>
                    <div class="row-meta">${sector}</div>
                </div>
            </div>`;
        }).join('');
    } catch (e) {
        listEl.innerHTML = `<div class="loading-placeholder">エラー: ${escapeHtml(e.message || String(e))}</div>`;
    }
};

window.closeWatchlistPicker = () => {
    $('#invest-watchlist-picker-modal')?.classList.add('hidden');
};

window.pickWatchlistTicker = (code) => {
    _setPickerTargetValue(_watchlistPickerTarget, code);
    showToast(`${code} を選択しました`);
    window.closeWatchlistPicker();
};

// 共通: ピッカーのターゲット種別に応じて対応する input に値を設定
function _setPickerTargetValue(target, code) {
    let id;
    if (target === 'ceo') id = 'invest-ceo-ticker';
    else if (target === 'journal') id = 'journal-add-ticker';
    else id = 'invest-ticker-input'; // 'analysis' or default
    const el = $('#' + id);
    if (el) el.value = code;
}

// === 保有銘柄アクションシート（ポートフォリオ行クリック用） ===
let _holdingActionCode = null;

window.openHoldingActionModal = (code) => {
    _holdingActionCode = code;
    const holding = (_investHoldingsCache || []).find(h => h.code === code);
    const titleEl = $('#invest-holding-action-title');
    const metaEl = $('#invest-holding-action-meta');
    if (titleEl) {
        titleEl.textContent = holding
            ? `${holding.name || code} (${code})`
            : code;
    }
    if (metaEl) {
        if (holding) {
            metaEl.textContent = `${holding.shares}株 @ ${holding.avg_cost} ${holding.currency || ''} / ${holding.market || ''}`;
        } else {
            metaEl.textContent = '';
        }
    }
    $('#invest-holding-action-modal')?.classList.remove('hidden');
};

window.closeHoldingActionModal = () => {
    $('#invest-holding-action-modal')?.classList.add('hidden');
    _holdingActionCode = null;
};

window.runHoldingAction = async (action) => {
    const code = _holdingActionCode;
    if (!code) return;
    // CEO検証は動画URLが必要なのでCEOカードへ誘導
    if (action === 'ceo') {
        const el = $('#invest-ceo-ticker');
        if (el) el.value = code;
        window.closeHoldingActionModal();
        showToast('CEO検証カードにティッカーをセットしました。動画URLを入力して実行してください。');
        // CEOカードへスクロール
        $('#invest-ceo-url')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
        return;
    }
    // 他のアクションはticker入力欄に値を入れて該当の関数を呼ぶ
    const tickerInput = $('#invest-ticker-input');
    if (tickerInput) tickerInput.value = code;
    window.closeHoldingActionModal();
    const handler = {
        snapshot: window.runStockSnapshot,
        audit: window.runStockAudit,
        news: window.runNewsSentiment,
        peer: window.runPeerComparison,
        earnings: window.runEarningsSchedule,
        docs: window.runEarningsDocuments,
        dividend: window.runDividend,
    }[action];
    if (typeof handler === 'function') {
        await handler();
    }
};

window.removeHoldingFromAction = async () => {
    const code = _holdingActionCode;
    if (!code) return;
    if (!confirm(`${code} をポートフォリオから削除しますか？（全数売却扱い）`)) return;
    try {
        const data = await apiFetch('/api/investment/portfolio/remove', {
            method: 'POST',
            body: JSON.stringify({ code, shares: null }),
        });
        if (data && data.ok) {
            showToast(`${code} を削除しました`);
            window.closeHoldingActionModal();
            window.loadPortfolio();
        } else {
            showToast(data?.error || '削除失敗', true);
        }
    } catch (e) {
        showToast('削除失敗: ' + (e.message || e), true);
    }
};

window.openPortfolioEditFromAction = () => {
    const code = _holdingActionCode;
    if (!code) return;
    const holding = (_investHoldingsCache || []).find(h => h.code === code);
    if (!holding) {
        showToast('保有銘柄情報が見つかりません', true);
        return;
    }
    window.closeHoldingActionModal();
    window.openPortfolioEditModal(holding);
};

window.openPortfolioEditModal = (holding) => {
    if (!holding || !holding.code) return;
    const set = (id, value) => { const el = $('#'+id); if (el) el.value = value == null ? '' : value; };
    set('portfolio-edit-ticker', holding.code);
    set('portfolio-edit-name', holding.name || '');
    set('portfolio-edit-sector', holding.sector || '');
    set('portfolio-edit-shares', holding.shares != null ? holding.shares : '');
    set('portfolio-edit-cost', holding.avg_cost != null ? holding.avg_cost : '');
    set('portfolio-edit-currency', holding.currency || '');
    set('portfolio-edit-notes', holding.notes || '');
    const modal = $('#invest-portfolio-edit-modal');
    if (modal) {
        modal.dataset.code = holding.code;
        modal.classList.remove('hidden');
    }
};

window.closePortfolioEditModal = () => $('#invest-portfolio-edit-modal')?.classList.add('hidden');

window.submitPortfolioEdit = async () => {
    const modal = $('#invest-portfolio-edit-modal');
    const code = modal?.dataset?.code;
    if (!code) {
        showToast('対象銘柄が不明です', true);
        return;
    }
    const btn = document.querySelector('#invest-portfolio-edit-modal .modal-btn.submit');
    if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
    const sharesStr = $('#portfolio-edit-shares')?.value?.trim();
    const costStr = $('#portfolio-edit-cost')?.value?.trim();
    const body = { code };
    if (sharesStr) body.shares = parseFloat(sharesStr);
    if (costStr) body.avg_cost = parseFloat(costStr);
    const name = $('#portfolio-edit-name')?.value?.trim();
    const sector = $('#portfolio-edit-sector')?.value?.trim();
    const currency = $('#portfolio-edit-currency')?.value?.trim();
    const notes = $('#portfolio-edit-notes')?.value?.trim();
    if (name) body.name = name;
    if (sector) body.sector = sector;
    if (currency) body.currency = currency;
    if (notes) body.notes = notes;
    try {
        const data = await apiFetch('/api/investment/portfolio/edit', {
            method: 'POST',
            body: JSON.stringify(body),
        });
        if (data && data.ok) {
            showToast(`${code} を更新しました`);
            window.closePortfolioEditModal();
            window.loadPortfolio();
        } else {
            showToast(data?.error || '更新失敗', true);
        }
    } catch (e) {
        showToast('更新失敗: ' + (e.message || e), true);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '保存'; }
    }
};

window.openPortfolioAddModal = () => {
    ['portfolio-add-ticker','portfolio-add-name','portfolio-add-sector','portfolio-add-shares','portfolio-add-cost','portfolio-add-currency','portfolio-add-notes'].forEach(id => {
        const el = $('#'+id);
        if (el) el.value = '';
    });
    $('#invest-portfolio-modal')?.classList.remove('hidden');
};
window.closePortfolioAddModal = () => $('#invest-portfolio-modal')?.classList.add('hidden');

window.submitPortfolioAdd = async () => {
    const ticker = $('#portfolio-add-ticker')?.value?.trim();
    const sharesStr = $('#portfolio-add-shares')?.value?.trim();
    const costStr = $('#portfolio-add-cost')?.value?.trim();
    if (!ticker || !sharesStr || !costStr) {
        showToast('ティッカー・株数・取得単価は必須です', true);
        return;
    }
    const btn = document.querySelector('#invest-portfolio-modal .modal-btn.submit');
    if (btn) { btn.disabled = true; btn.textContent = '追加中…'; }
    const body = {
        ticker,
        shares: parseFloat(sharesStr),
        avg_cost: parseFloat(costStr),
        name: $('#portfolio-add-name')?.value?.trim() || null,
        sector: $('#portfolio-add-sector')?.value?.trim() || null,
        currency: $('#portfolio-add-currency')?.value?.trim() || null,
        notes: $('#portfolio-add-notes')?.value?.trim() || null,
    };
    try {
        const data = await apiFetch('/api/investment/portfolio/add', { method: 'POST', body: JSON.stringify(body) });
        if (data && data.ok) {
            showToast('ポートフォリオに追加しました');
            window.closePortfolioAddModal();
            window.loadPortfolio();
        } else {
            showToast(data?.error || '追加失敗', true);
        }
    } catch (e) {
        showToast('追加失敗: ' + (e.message || e), true);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '追加'; }
    }
};

window.runRiskAssessment = async () => {
    const data = await _callInvestmentApi('/api/investment/risk_assessment', null, 'リスク評価');
    if (data && data.ok) {
        window.openInvestmentResultModal('⚠️ ポートフォリオリスク評価', data.report);
        window.loadInvestmentHistory();
    }
};

// --- 投資日記 ---

window.loadJournalList = async () => {
    const listEl = $('#invest-journal-list');
    if (!listEl) return;
    listEl.innerHTML = '<div class="invest-empty">読み込み中…</div>';
    try {
        const data = await apiFetch('/api/investment/journal?limit=50');
        if (!data || !data.ok) {
            listEl.innerHTML = '<div class="loading-placeholder">取得に失敗しました。</div>';
            return;
        }
        const items = data.items || [];
        if (!items.length) {
            listEl.innerHTML = '<div class="loading-placeholder">日記がまだありません。「追加」から書いてみましょう。</div>';
            return;
        }
        listEl.innerHTML = items.map(it => {
            const title = escapeHtml(it.title || '(無題)');
            const date = escapeHtml(it.date || '');
            const time = escapeHtml(it.time || '');
            const ticker = escapeHtml(it.ticker || '');
            const action = escapeHtml(it.action || '');
            const emotion = escapeHtml(it.emotion || '');
            const meta = [date+' '+time, ticker, action, emotion].filter(Boolean).join(' / ');
            const safeFn = (it.filename || '').replace(/'/g, "\\'");
            const safeTitle = title.replace(/'/g, '&#39;');
            const metaAttr = escapeHtml(JSON.stringify(it));
            return `<div class="invest-row" style="display:flex;align-items:flex-start;gap:6px;">
                <div class="row-main" style="flex:1;min-width:0;cursor:pointer;" onclick="openJournalEntry('${safeFn}', '${safeTitle}')">
                    <div class="row-title">${title}</div>
                    <div class="row-meta">${meta}</div>
                </div>
                <div style="display:flex;gap:4px;flex-shrink:0;">
                    <button class="mini-link" data-journal-meta="${metaAttr}" onclick="event.stopPropagation();openJournalEditModal('${safeFn}', JSON.parse(this.dataset.journalMeta))" title="編集">編集</button>
                    <button class="mini-link" style="color:#ff6b6b;" onclick="event.stopPropagation();deleteJournalEntry('${safeFn}')" title="削除">削除</button>
                </div>
            </div>`;
        }).join('');
    } catch (e) {
        listEl.innerHTML = `<div class="loading-placeholder">エラー: ${escapeHtml(e.message || String(e))}</div>`;
    }
};

window.openJournalEntry = async (filename, title) => {
    if (!filename) return;
    showToast(`${title} を読み込み中…`);
    // 履歴経由で読み込む (category=journal)
    try {
        // ファイルID取得のため、一覧APIを叩いてfilenameに一致するidを探す
        const list = await apiFetch('/api/investment/history/journal?limit=200');
        if (!list || !list.ok) { showToast('一覧取得失敗', true); return; }
        const item = (list.items || []).find(it => it.name === filename);
        if (!item) { showToast('日記エントリが見つかりません', true); return; }
        const data = await apiFetch(`/api/investment/history/journal/${encodeURIComponent(item.id)}`);
        if (!data || !data.ok) { showToast(data?.error || '読み込み失敗', true); return; }
        window.openInvestmentResultModal(title, data.content);
    } catch (e) {
        showToast('読み込み失敗: ' + (e.message || e), true);
    }
};

let _journalEditingFile = null; // null=追加モード, filename=編集モード

window.openJournalAddModal = () => {
    _journalEditingFile = null;
    const titleEl = $('#invest-journal-modal-title');
    if (titleEl) titleEl.textContent = '📔 投資日記を追加';
    const submitBtn = $('#journal-submit-btn');
    if (submitBtn) submitBtn.textContent = '保存';
    ['journal-add-title','journal-add-ticker','journal-add-emotion','journal-add-content'].forEach(id => {
        const el = $('#'+id);
        if (el) el.value = '';
    });
    const sel = $('#journal-add-action');
    if (sel) sel.value = '';
    $('#invest-journal-modal')?.classList.remove('hidden');
};

window.openJournalEditModal = async (filename, meta = null) => {
    if (!filename) return;
    _journalEditingFile = filename;
    const titleEl = $('#invest-journal-modal-title');
    if (titleEl) titleEl.textContent = '✏️ 投資日記を編集';
    const submitBtn = $('#journal-submit-btn');
    // メタ情報（一覧から渡されたもの）で先に埋めて即座にモーダルを開く
    $('#journal-add-title').value = (meta && meta.title) || '';
    $('#journal-add-ticker').value = (meta && meta.ticker) || '';
    $('#journal-add-emotion').value = (meta && meta.emotion) || '';
    const sel = $('#journal-add-action');
    if (sel) sel.value = (meta && meta.action) || '';
    const contentEl = $('#journal-add-content');
    contentEl.value = '';
    contentEl.disabled = true;
    contentEl.placeholder = '本文を読み込み中…';
    if (submitBtn) { submitBtn.textContent = '更新'; submitBtn.disabled = true; }
    $('#invest-journal-modal')?.classList.remove('hidden');

    // 本文（Drive 取得）は非同期で読み込み、届いたら反映する
    try {
        const data = await apiFetch(`/api/investment/journal/${encodeURIComponent(filename)}`);
        // 読み込み中に別のモーダルへ切り替わっていたら破棄
        if (_journalEditingFile !== filename) return;
        if (!data || !data.ok) {
            showToast('読み込み失敗: ' + (data?.error || '不明'), true);
            closeJournalAddModal();
            return;
        }
        $('#journal-add-title').value = data.title || '';
        $('#journal-add-ticker').value = data.ticker || '';
        $('#journal-add-emotion').value = data.emotion || '';
        if (sel) sel.value = data.action || '';
        contentEl.value = data.content || '';
    } catch (e) {
        showToast('読み込みエラー: ' + (e.message || e), true);
        closeJournalAddModal();
    } finally {
        contentEl.disabled = false;
        contentEl.placeholder = '売買理由・観察事項・気づきなど';
        if (submitBtn) submitBtn.disabled = false;
    }
};

window.closeJournalAddModal = () => {
    $('#invest-journal-modal')?.classList.add('hidden');
    _journalEditingFile = null;
};

window.generateJournalTitle = async () => {
    const content = $('#journal-add-content')?.value?.trim();
    if (!content) { showToast('先に本文を入力してね', true); return; }
    const btn = $('#journal-title-gen-btn');
    if (btn) { btn.disabled = true; btn.textContent = '生成中…'; }
    try {
        const data = await apiFetch('/api/investment/journal/suggest_title', {
            method: 'POST',
            body: JSON.stringify({
                content,
                ticker: $('#journal-add-ticker')?.value?.trim() || '',
                action: $('#journal-add-action')?.value || '',
                emotion: $('#journal-add-emotion')?.value?.trim() || '',
            }),
        });
        if (data && data.ok && data.title) {
            $('#journal-add-title').value = data.title;
            showToast('タイトルを生成しました');
        } else {
            showToast('タイトル生成に失敗', true);
        }
    } catch (e) {
        showToast('タイトル生成に失敗: ' + (e.message || e), true);
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '✨ タイトルを生成'; }
    }
};

window.submitJournalAdd = async () => {
    const content = $('#journal-add-content')?.value?.trim();
    if (!content) {
        showToast('本文は必須です', true);
        return;
    }
    const body = {
        title: $('#journal-add-title')?.value?.trim() || '',
        content,
        ticker: $('#journal-add-ticker')?.value?.trim() || '',
        action: $('#journal-add-action')?.value || '',
        emotion: $('#journal-add-emotion')?.value?.trim() || '',
    };
    try {
        let data;
        if (_journalEditingFile) {
            data = await apiFetch(`/api/investment/journal/${encodeURIComponent(_journalEditingFile)}`, {
                method: 'PUT', body: JSON.stringify(body),
            });
        } else {
            data = await apiFetch('/api/investment/journal/add', { method: 'POST', body: JSON.stringify(body) });
        }
        if (data && data.ok) {
            showToast(_journalEditingFile ? '日記を更新しました' : '日記を保存しました');
            window.closeJournalAddModal();
            window.loadJournalList();
        } else {
            showToast(data?.error || '保存失敗', true);
        }
    } catch (e) {
        showToast('保存失敗: ' + (e.message || e), true);
    }
};

window.deleteJournalEntry = async (filename) => {
    if (!filename) return;
    if (!confirm('この日記を削除しますか？（Driveのファイルもゴミ箱に移動されます）')) return;
    try {
        const data = await apiFetch(`/api/investment/journal/${encodeURIComponent(filename)}`, { method: 'DELETE' });
        if (data && data.ok) {
            showToast('🗑 日記を削除しました');
            window.loadJournalList();
        } else {
            showToast('削除失敗: ' + (data?.error || '不明'), true);
        }
    } catch (e) {
        showToast('削除失敗: ' + (e.message || e), true);
    }
};

window.runJournalAnalyze = async () => {
    const data = await _callInvestmentApi('/api/investment/journal/analyze', { limit: 30 }, '投資日記の癖分析');
    if (data && data.ok) {
        window.openInvestmentResultModal('🧠 投資行動パターン分析', data.report);
    }
};

// --- アラート ---

window.loadAlertsList = async () => {
    const listEl = $('#invest-alerts-list');
    if (!listEl) return;
    listEl.innerHTML = '<div class="invest-empty">読み込み中…</div>';
    try {
        const data = await apiFetch('/api/investment/alerts');
        if (!data || !data.ok) {
            listEl.innerHTML = '<div class="loading-placeholder">取得に失敗しました。</div>';
            return;
        }
        const rules = data.rules || [];
        if (!rules.length) {
            listEl.innerHTML = '<div class="loading-placeholder">アラートがまだありません。「追加」から登録してください。</div>';
            return;
        }
        listEl.innerHTML = rules.map(r => {
            const ticker = escapeHtml(r.ticker || '(全体)');
            const type = escapeHtml(r.type || '?');
            const threshold = r.threshold ?? '?';
            const memo = escapeHtml(r.memo || '');
            const enabled = !!r.enabled;
            const cls = enabled ? '' : 'disabled';
            const onLabel = enabled ? '無効化' : '有効化';
            return `<div class="invest-row ${cls}">
                <div class="row-main">
                    <div class="row-title">${ticker} — ${type} (${threshold})</div>
                    <div class="row-meta">${memo || '(メモなし)'}</div>
                </div>
                <div class="row-actions">
                    <button onclick="toggleAlert(${r.id}, ${!enabled})">${onLabel}</button>
                    <button onclick="removeAlert(${r.id})">削除</button>
                </div>
            </div>`;
        }).join('');
    } catch (e) {
        listEl.innerHTML = `<div class="loading-placeholder">エラー: ${escapeHtml(e.message || String(e))}</div>`;
    }
};

window.toggleAlert = async (ruleId, enabled) => {
    try {
        const data = await apiFetch('/api/investment/alerts/toggle', {
            method: 'POST',
            body: JSON.stringify({ rule_id: ruleId, enabled }),
        });
        if (data && data.ok) {
            showToast(enabled ? '有効化しました' : '無効化しました');
            window.loadAlertsList();
        } else {
            showToast(data?.error || '失敗', true);
        }
    } catch (e) {
        showToast('失敗: ' + (e.message || e), true);
    }
};

window.removeAlert = async (ruleId) => {
    if (!confirm('このアラートを削除しますか？')) return;
    try {
        const data = await apiFetch('/api/investment/alerts/remove', {
            method: 'POST',
            body: JSON.stringify({ rule_id: ruleId }),
        });
        if (data && data.ok) {
            showToast('削除しました');
            window.loadAlertsList();
        } else {
            showToast(data?.error || '失敗', true);
        }
    } catch (e) {
        showToast('失敗: ' + (e.message || e), true);
    }
};

window.openAlertAddModal = () => {
    ['alert-add-ticker','alert-add-threshold','alert-add-memo'].forEach(id => {
        const el = $('#'+id);
        if (el) el.value = '';
    });
    const sel = $('#alert-add-type');
    if (sel) sel.value = 'per_below';
    $('#invest-alert-modal')?.classList.remove('hidden');
};
window.closeAlertAddModal = () => $('#invest-alert-modal')?.classList.add('hidden');

window.submitAlertAdd = async () => {
    const type = $('#alert-add-type')?.value;
    const ticker = $('#alert-add-ticker')?.value?.trim();
    const thresholdStr = $('#alert-add-threshold')?.value?.trim();
    if (!type || !thresholdStr) {
        showToast('種別と閾値は必須です', true);
        return;
    }
    if (!ticker && type !== 'earnings_within_days') {
        showToast('ティッカーは必須です（earnings_within_daysを除く）', true);
        return;
    }
    const body = {
        ticker,
        type,
        threshold: parseFloat(thresholdStr),
        memo: $('#alert-add-memo')?.value?.trim() || '',
        enabled: true,
    };
    try {
        const data = await apiFetch('/api/investment/alerts/add', { method: 'POST', body: JSON.stringify(body) });
        if (data && data.ok) {
            showToast('アラートを追加しました');
            window.closeAlertAddModal();
            window.loadAlertsList();
        } else {
            showToast(data?.error || '失敗', true);
        }
    } catch (e) {
        showToast('失敗: ' + (e.message || e), true);
    }
};

window.runAlertsCheck = async () => {
    const data = await _callInvestmentApi('/api/investment/alerts/check', null, 'アラート評価');
    if (!data || !data.ok) return;
    const hits = data.hits || [];
    const lines = [`# 🔔 アラート評価結果 (as of ${data.as_of || '今'})`, '', `- 評価ルール数: ${data.checked || 0}`, `- ヒット数: ${hits.length}`, ''];
    if (!hits.length) {
        lines.push('現時点でヒットしているルールはありません。');
    } else {
        lines.push('## 🚨 発火中');
        hits.forEach(h => {
            lines.push(`- **${h.ticker || '(全体)'}** [${h.type}] 現在値: ${h.current_value} (閾値: ${h.threshold})`);
            if (h.message) lines.push(`  - ${h.message}`);
        });
    }
    window.openInvestmentResultModal('🔔 アラート評価結果', lines.join('\n'));
};

// --- 投資憲法レビュー ---

window.runConstitutionReview = async () => {
    if (!confirm('過去半年の審査履歴・日記・保有銘柄を読み込んで投資憲法のレビューを実行します。少し時間がかかりますがよろしいですか？')) return;
    const data = await _callInvestmentApi('/api/investment/constitution_review', { lookback_days: 180 }, '憲法レビュー');
    if (data && data.ok) {
        window.openInvestmentResultModal('🔄 投資憲法レビュー', data.report);
        window.loadInvestmentHistory();
    }
};