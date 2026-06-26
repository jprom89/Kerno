/* Submission windows — lists open windows and lets the tenant trigger a submission run. */

import { requireAuth, markActiveNav } from './auth.js';
import { apiGet, apiPost, fmtDate } from './api.js';

if (!requireAuth()) throw new Error('redirect');

document.addEventListener('DOMContentLoaded', async () => {
    markActiveNav();
    const windows = await apiGet('/api/v1/submissions/windows');
    const tbody = document.getElementById('windows-tbody');
    const empty = document.getElementById('empty-msg');
    const notice = document.getElementById('notice');

    if (!windows || windows.length === 0) {
        empty.classList.remove('hidden');
        return;
    }

    tbody.innerHTML = windows.map(w => `
        <tr>
            <td>${_esc(w.authority_code)}</td>
            <td>${w.reporting_year}</td>
            <td>${fmtDate(w.window_open_date)}</td>
            <td>${fmtDate(w.window_close_date)}</td>
            <td>${fmtDate(w.register_reference_date)}</td>
            <td>
                <button class="btn btn-primary trigger-btn"
                    data-window-id="${w.id}"
                    data-authority="${_esc(w.authority_code)}">
                    Build submission
                </button>
            </td>
        </tr>
    `).join('');

    tbody.querySelectorAll('.trigger-btn').forEach(btn => {
        btn.addEventListener('click', () => _triggerRun(btn, notice));
    });
});

async function _triggerRun(btn, notice) {
    const windowId = btn.dataset.windowId;
    const authority = btn.dataset.authority;
    btn.disabled = true;
    btn.textContent = 'Building…';
    notice.classList.add('hidden');

    try {
        const run = await apiPost('/api/v1/submissions/runs', { submission_window_id: windowId });
        if (run) {
            notice.className = 'alert alert-success';
            notice.innerHTML = `Submission run created for <strong>${authority}</strong>.
                <a href="/dashboard/submission-detail.html?id=${run.id}">View run →</a>`;
            notice.classList.remove('hidden');
        }
    } catch (err) {
        notice.className = 'alert alert-error';
        notice.textContent = `Build failed: ${err.message}`;
        notice.classList.remove('hidden');
    } finally {
        btn.disabled = false;
        btn.textContent = 'Build submission';
    }
}

function _esc(str) {
    if (!str) return '';
    return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
