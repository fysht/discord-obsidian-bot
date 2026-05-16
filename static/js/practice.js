// ===================================================================
// 楽器練習サポート (PWA UI) — MVP: ドラム
// app_v12.js の apiFetch / showToast をグローバル関数として再利用。
// ===================================================================

const PRACTICE_LEVEL_LABELS = { beginner: '初級', intermediate: '中級', advanced: '上級' };
const PRACTICE_CATEGORY_LABELS = {
    rudiments: 'ルーディメンツ',
    groove:    'グルーヴ',
    fill:      'フィル',
    song_cover:'曲カバー',
    technique: 'テクニック',
    theory:    '理論',
};
const PRACTICE_STATUS_LABELS = { todo: '未', wip: '中', done: '済' };

let _practiceTimerHandle = null;
let _practiceActiveStartedAt = null;
let _practiceVideoFilter = { level: '', category: '' };
let _practicePendingId = null;
let _practiceUiBound = false;

function practiceFmtSeconds(sec) {
    sec = Math.max(0, Math.floor(sec));
    const m = Math.floor(sec / 60);
    const s = sec % 60;
    return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}

function practiceTickTimer() {
    if (!_practiceActiveStartedAt) return;
    const el = document.querySelector('#practice-timer');
    if (!el) return;
    const elapsedMs = Date.now() - _practiceActiveStartedAt.getTime();
    el.textContent = practiceFmtSeconds(elapsedMs / 1000);
}

function practiceStartTimer(startedIso) {
    practiceStopTimer();
    if (!startedIso) return;
    _practiceActiveStartedAt = new Date(startedIso);
    practiceTickTimer();
    _practiceTimerHandle = setInterval(practiceTickTimer, 1000);
}

function practiceStopTimer() {
    if (_practiceTimerHandle) clearInterval(_practiceTimerHandle);
    _practiceTimerHandle = null;
    _practiceActiveStartedAt = null;
    const el = document.querySelector('#practice-timer');
    if (el) el.textContent = '00:00';
}

function practiceRenderState(state) {
    document.querySelector('#practice-today-min').textContent = state.today_minutes || 0;
    document.querySelector('#practice-streak').textContent = state.streak_days || 0;

    const startBtn = document.querySelector('#practice-start-btn');
    const endBtn = document.querySelector('#practice-end-btn');
    const memoArea = document.querySelector('#practice-memo-area');
    const menuListEl = document.querySelector('#practice-menu-list');

    if (state.active_session) {
        startBtn.style.display = 'none';
        endBtn.style.display = '';
        memoArea.style.display = '';
        const menu = state.active_session.menu || [];
        menuListEl.textContent = menu.length ? '今日のメニュー: ' + menu.join(' / ') : '（メニュー未指定）';
        practiceStartTimer(state.active_session.started_at);
    } else {
        startBtn.style.display = '';
        endBtn.style.display = 'none';
        memoArea.style.display = 'none';
        menuListEl.textContent = '';
        practiceStopTimer();
    }
}

async function loadPracticeState() {
    try {
        const res = await apiFetch('/api/practice/state');
        const data = await res.json();
        if (!data.ok) return;
        practiceRenderState(data);
    } catch (e) {
        console.error('practice state load error', e);
    }
}

async function practiceStart() {
    // 直前に「このメニューで開始」用の menu 配列が用意されていれば渡す
    const menu = window._practicePendingMenu || [];
    try {
        const res = await apiFetch('/api/practice/start', {
            method: 'POST',
            body: JSON.stringify({ menu }),
        });
        const data = await res.json();
        if (!data.ok) {
            showToast('開始に失敗: ' + (data.error || ''));
            return;
        }
        window._practicePendingMenu = null;
        await loadPracticeState();
    } catch (e) {
        showToast('開始に失敗しました');
    }
}

async function practiceEnd() {
    const memo = (document.querySelector('#practice-memo-input')?.value || '').trim();
    try {
        const res = await apiFetch('/api/practice/end', {
            method: 'POST',
            body: JSON.stringify({ memo }),
        });
        const data = await res.json();
        if (!data.ok) {
            showToast('終了に失敗: ' + (data.error || ''));
            return;
        }
        const s = data.session || {};
        showToast(`お疲れさま！${s.duration_min || 0}分の練習を記録しました`);
        const memoInput = document.querySelector('#practice-memo-input');
        if (memoInput) memoInput.value = '';
        await loadPracticeState();
    } catch (e) {
        showToast('終了に失敗しました');
    }
}

// ---------- ロードマップ ----------
async function loadPracticeRoadmap() {
    const container = document.querySelector('#practice-roadmap-container');
    if (!container) return;
    try {
        const res = await apiFetch('/api/practice/roadmap');
        const data = await res.json();
        if (!data.ok) {
            container.innerHTML = '<div style="color:#a00;">ロードマップ取得失敗</div>';
            return;
        }
        const phases = data.phases || [];
        const summary = data.summary || { done: 0, total: 0 };
        let html = `<div style="font-size:0.88rem;color:#666;margin-bottom:8px;">達成: <b>${summary.done}/${summary.total}</b></div>`;
        for (const ph of phases) {
            html += `<div class="practice-roadmap-phase">
                <div style="font-weight:600;font-size:0.98rem;">${escapeHtml(ph.label || '')}</div>
                <div style="font-size:0.82rem;color:#666;margin-bottom:6px;">${escapeHtml(ph.description || '')}</div>`;
            for (const m of (ph.milestones || [])) {
                const st = m.status || 'todo';
                html += `<div class="practice-milestone">
                    <div style="flex:1;">
                        <div>${escapeHtml(m.label || '')}</div>
                        <div style="font-size:0.78rem;color:#888;">${escapeHtml(m.criteria || '')}</div>
                    </div>
                    <span class="practice-status-badge practice-status-${st}" data-milestone="${escapeHtml(m.id)}" data-current="${st}">
                        ${PRACTICE_STATUS_LABELS[st] || '?'}
                    </span>
                </div>`;
            }
            html += `</div>`;
        }
        container.innerHTML = html;
        container.querySelectorAll('.practice-status-badge').forEach(el => {
            el.addEventListener('click', practiceCycleMilestone);
        });
    } catch (e) {
        container.innerHTML = '<div style="color:#a00;">ロードマップ取得失敗</div>';
    }
}

async function practiceCycleMilestone(ev) {
    const el = ev.currentTarget;
    const milestoneId = el.getAttribute('data-milestone');
    const current = el.getAttribute('data-current') || 'todo';
    const next = current === 'todo' ? 'wip' : (current === 'wip' ? 'done' : 'todo');
    try {
        const res = await apiFetch('/api/practice/roadmap/mark', {
            method: 'POST',
            body: JSON.stringify({ milestone_id: milestoneId, status: next }),
        });
        const data = await res.json();
        if (!data.ok) {
            showToast('更新に失敗');
            return;
        }
        await loadPracticeRoadmap();
    } catch (e) {
        showToast('更新に失敗');
    }
}

// ---------- 動画ライブラリ ----------
function practiceRenderVideoFilters() {
    const container = document.querySelector('#practice-video-filters');
    if (!container) return;
    let html = '';
    html += `<span class="practice-chip ${_practiceVideoFilter.level === '' ? 'active' : ''}" data-level="">全レベル</span>`;
    for (const [k, label] of Object.entries(PRACTICE_LEVEL_LABELS)) {
        html += `<span class="practice-chip ${_practiceVideoFilter.level === k ? 'active' : ''}" data-level="${k}">${label}</span>`;
    }
    html += `<span style="width:8px;"></span>`;
    html += `<span class="practice-chip ${_practiceVideoFilter.category === '' ? 'active' : ''}" data-category="">全分類</span>`;
    for (const [k, label] of Object.entries(PRACTICE_CATEGORY_LABELS)) {
        html += `<span class="practice-chip ${_practiceVideoFilter.category === k ? 'active' : ''}" data-category="${k}">${label}</span>`;
    }
    container.innerHTML = html;
    container.querySelectorAll('[data-level]').forEach(el => {
        el.addEventListener('click', () => {
            _practiceVideoFilter.level = el.getAttribute('data-level') || '';
            loadPracticeVideos();
        });
    });
    container.querySelectorAll('[data-category]').forEach(el => {
        el.addEventListener('click', () => {
            _practiceVideoFilter.category = el.getAttribute('data-category') || '';
            loadPracticeVideos();
        });
    });
}

async function loadPracticeVideos() {
    practiceRenderVideoFilters();
    const grid = document.querySelector('#practice-video-grid');
    if (!grid) return;
    const params = new URLSearchParams();
    if (_practiceVideoFilter.level) params.set('level', _practiceVideoFilter.level);
    if (_practiceVideoFilter.category) params.set('category', _practiceVideoFilter.category);
    try {
        const res = await apiFetch('/api/practice/videos?' + params.toString());
        const data = await res.json();
        if (!data.ok) {
            grid.innerHTML = '<div style="color:#a00;">取得失敗</div>';
            return;
        }
        const videos = data.videos || [];
        const pending = data.pending || [];
        if (pending.length > 0 && !document.querySelector('#practice-video-modal:not(.hidden)')) {
            // 未確認の pending があれば先頭をモーダルで提示
            practiceOpenVideoModal(pending[0]);
        }
        if (videos.length === 0) {
            grid.innerHTML = '<div style="color:#888;font-size:0.92rem;padding:14px;">まだ動画がありません。上の入力欄に YouTube URL を貼って追加してください。</div>';
            return;
        }
        let html = '';
        for (const v of videos) {
            const lv = PRACTICE_LEVEL_LABELS[v.level] || v.level || '?';
            const cat = PRACTICE_CATEGORY_LABELS[v.category] || v.category || '?';
            html += `<div class="practice-video-card">
                <a href="${escapeHtml(v.url)}" target="_blank" rel="noopener">
                    <img loading="lazy" src="${escapeHtml(v.thumbnail || '')}" alt="">
                </a>
                <div class="meta">
                    <div style="font-weight:600;line-height:1.3;max-height:2.6em;overflow:hidden;">${escapeHtml(v.title || '')}</div>
                    <div style="color:#888;font-size:0.78rem;margin-top:2px;">${escapeHtml(v.author || '')}</div>
                    <div style="margin-top:4px;font-size:0.78rem;">
                        <span style="background:#eef;padding:1px 6px;border-radius:6px;">${lv}</span>
                        <span style="background:#efe;padding:1px 6px;border-radius:6px;margin-left:2px;">${cat}</span>
                    </div>
                    <button class="practice-mini-btn" style="margin-top:6px;font-size:0.78rem;padding:4px 8px;" data-delete="${escapeHtml(v.id)}">削除</button>
                </div>
            </div>`;
        }
        grid.innerHTML = html;
        grid.querySelectorAll('[data-delete]').forEach(el => {
            el.addEventListener('click', async () => {
                if (!confirm('この動画を削除しますか？')) return;
                const id = el.getAttribute('data-delete');
                const res = await apiFetch('/api/practice/videos/delete', {
                    method: 'POST', body: JSON.stringify({ video_id: id }),
                });
                const data = await res.json();
                if (data.ok) loadPracticeVideos();
            });
        });
    } catch (e) {
        grid.innerHTML = '<div style="color:#a00;">取得失敗</div>';
    }
}

async function practiceAddVideoByUrl() {
    const input = document.querySelector('#practice-video-url-input');
    const url = (input?.value || '').trim();
    if (!url) return;
    showToast('動画を解析中... 数十秒かかることがあります');
    try {
        const res = await apiFetch('/api/practice/videos/propose', {
            method: 'POST', body: JSON.stringify({ url }),
        });
        const data = await res.json();
        if (!data.ok) {
            showToast('追加失敗: ' + (data.error || ''));
            return;
        }
        if (input) input.value = '';
        practiceOpenVideoModal(data.pending);
    } catch (e) {
        showToast('追加失敗');
    }
}

function practiceOpenVideoModal(pending) {
    if (!pending) return;
    _practicePendingId = pending.id;
    const s = pending.ai_suggestion || {};
    const body = document.querySelector('#practice-video-modal-body');
    body.innerHTML = `
        <div style="display:flex;gap:10px;margin-bottom:10px;">
            <img src="${escapeHtml(pending.thumbnail || '')}" style="width:120px;aspect-ratio:16/9;object-fit:cover;background:#000;">
            <div style="flex:1;">
                <div style="font-weight:600;">${escapeHtml(pending.title || '')}</div>
                <div style="font-size:0.82rem;color:#888;">${escapeHtml(pending.author || '')}</div>
            </div>
        </div>
        <div style="margin-bottom:8px;font-size:0.85rem;color:#555;">AI提案: ${escapeHtml(s.summary_jp || '')}</div>
        <label style="font-size:0.88rem;display:block;margin-top:6px;">レベル:
            <select id="practice-modal-level" style="margin-left:6px;padding:4px;">
                ${Object.entries(PRACTICE_LEVEL_LABELS).map(([k, v]) =>
                    `<option value="${k}" ${k === s.level ? 'selected' : ''}>${v}</option>`).join('')}
            </select>
        </label>
        <label style="font-size:0.88rem;display:block;margin-top:6px;">分類:
            <select id="practice-modal-category" style="margin-left:6px;padding:4px;">
                ${Object.entries(PRACTICE_CATEGORY_LABELS).map(([k, v]) =>
                    `<option value="${k}" ${k === s.category ? 'selected' : ''}>${v}</option>`).join('')}
            </select>
        </label>
        <label style="font-size:0.88rem;display:block;margin-top:6px;">タグ (カンマ区切り):
            <input id="practice-modal-tags" type="text" value="${escapeHtml((s.tags || []).join(', '))}" style="width:100%;padding:4px;margin-top:2px;">
        </label>
    `;
    document.querySelector('#practice-video-modal').classList.remove('hidden');
}

async function practiceConfirmPending() {
    if (!_practicePendingId) return;
    const level = document.querySelector('#practice-modal-level')?.value;
    const category = document.querySelector('#practice-modal-category')?.value;
    const tagsRaw = document.querySelector('#practice-modal-tags')?.value || '';
    const tags = tagsRaw.split(',').map(t => t.trim()).filter(Boolean);
    try {
        const res = await apiFetch('/api/practice/videos/confirm', {
            method: 'POST',
            body: JSON.stringify({ pending_id: _practicePendingId, overrides: { level, category, tags } }),
        });
        const data = await res.json();
        if (!data.ok) {
            showToast('保存失敗');
            return;
        }
        showToast('動画を保存しました');
        practiceCloseVideoModal();
        await loadPracticeVideos();
    } catch (e) {
        showToast('保存失敗');
    }
}

async function practiceDiscardPending() {
    if (!_practicePendingId) {
        practiceCloseVideoModal();
        return;
    }
    try {
        await apiFetch('/api/practice/videos/discard', {
            method: 'POST',
            body: JSON.stringify({ pending_id: _practicePendingId }),
        });
    } catch (e) { /* noop */ }
    practiceCloseVideoModal();
    await loadPracticeVideos();
}

function practiceCloseVideoModal() {
    _practicePendingId = null;
    document.querySelector('#practice-video-modal')?.classList.add('hidden');
}

// ---------- メニュー（Phase 5 で AI 経由を実装する。今はテンプレ表示のみ） ----------
const PRACTICE_TEMPLATE_MENUS = {
    15: [
        { title: 'ウォームアップ・グリップ確認', minutes: 3 },
        { title: 'シングル/ダブル ストローク 80BPM', minutes: 5 },
        { title: '8ビート 80BPM', minutes: 5 },
        { title: 'クールダウン', minutes: 2 },
    ],
    30: [
        { title: 'ウォームアップ', minutes: 5 },
        { title: 'ルーディメンツ 4種', minutes: 10 },
        { title: '8ビート + フィル', minutes: 10 },
        { title: '曲練（レパートリーから1曲）', minutes: 5 },
    ],
    60: [
        { title: 'ウォームアップ', minutes: 5 },
        { title: 'ルーディメンツ 6種', minutes: 15 },
        { title: 'グルーヴパターン 3種', minutes: 15 },
        { title: 'フィル練習', minutes: 10 },
        { title: '曲練', minutes: 10 },
        { title: '録音&振り返り', minutes: 5 },
    ],
};

function practiceShowTemplateMenu() {
    const sel = document.querySelector('#practice-menu-minutes');
    const min = parseInt(sel?.value || '30', 10);
    const items = PRACTICE_TEMPLATE_MENUS[min] || PRACTICE_TEMPLATE_MENUS[30];
    practiceRenderMenuResult(items);
}

async function practiceGenerateAiMenu() {
    const sel = document.querySelector('#practice-menu-minutes');
    const min = parseInt(sel?.value || '30', 10);
    const resultEl = document.querySelector('#practice-menu-result');
    resultEl.innerHTML = '<span style="color:#888;">AI生成中...</span>';
    try {
        const res = await apiFetch('/api/practice/menu/generate', {
            method: 'POST',
            body: JSON.stringify({ minutes: min, mode: 'ai' }),
        });
        const data = await res.json();
        if (!data.ok || !data.menu) {
            // Phase 5 未実装の段階ではフォールバック
            practiceShowTemplateMenu();
            return;
        }
        practiceRenderMenuResult(data.menu);
    } catch (e) {
        practiceShowTemplateMenu();
    }
}

function practiceRenderMenuResult(items) {
    const resultEl = document.querySelector('#practice-menu-result');
    const useBtn = document.querySelector('#practice-menu-use-btn');
    if (!items || !items.length) {
        resultEl.innerHTML = '';
        useBtn.style.display = 'none';
        return;
    }
    let html = '<ol style="margin:0;padding-left:20px;">';
    for (const it of items) {
        html += `<li>${escapeHtml(it.title || '')} <span style="color:#888;">(${it.minutes || '?'}分)</span>`;
        if (it.goal) html += `<div style="font-size:0.78rem;color:#999;">${escapeHtml(it.goal)}</div>`;
        html += '</li>';
    }
    html += '</ol>';
    resultEl.innerHTML = html;
    window._practicePendingMenu = items.map(it => it.title);
    useBtn.style.display = '';
}

// ---------- 起動・初期化 ----------
function practiceBindUi() {
    if (_practiceUiBound) return;
    _practiceUiBound = true;
    document.querySelector('#practice-start-btn')?.addEventListener('click', practiceStart);
    document.querySelector('#practice-end-btn')?.addEventListener('click', practiceEnd);
    document.querySelector('#practice-video-add-btn')?.addEventListener('click', practiceAddVideoByUrl);
    document.querySelector('#practice-menu-template-btn')?.addEventListener('click', practiceShowTemplateMenu);
    document.querySelector('#practice-menu-ai-btn')?.addEventListener('click', practiceGenerateAiMenu);
    document.querySelector('#practice-menu-use-btn')?.addEventListener('click', practiceStart);

    // メモは入力するたびに即サーバ反映（離脱しても消えないように）
    const memoInput = document.querySelector('#practice-memo-input');
    if (memoInput) {
        let memoTimer = null;
        memoInput.addEventListener('input', () => {
            if (memoTimer) clearTimeout(memoTimer);
            memoTimer = setTimeout(async () => {
                const text = memoInput.value.trim();
                if (!text) return;
                try {
                    await apiFetch('/api/practice/memo', {
                        method: 'POST', body: JSON.stringify({ text }),
                    });
                } catch (e) { /* noop */ }
            }, 1500);
        });
    }
}

// app_v12.js の switchTab から呼ばれるエントリポイント
async function loadPractice() {
    practiceBindUi();
    await loadPracticeState();
    await loadPracticeRoadmap();
    await loadPracticeVideos();
}

// グローバル公開（app_v12.js から typeof loadPractice === 'function' で参照される）
window.loadPractice = loadPractice;
window.practiceConfirmPending = practiceConfirmPending;
window.practiceDiscardPending = practiceDiscardPending;
