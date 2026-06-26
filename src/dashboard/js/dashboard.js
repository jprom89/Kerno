/* Dashboard home — loads summary counts from three API endpoints in parallel. */

import { requireAuth, markActiveNav } from './auth.js';
import { apiGet, badge, fmtDateTime } from './api.js';

if (!requireAuth()) throw new Error('redirect');

document.addEventListener('DOMContentLoaded', async () => {
    markActiveNav();
    const [entries, runs, windows] = await Promise.all([
        apiGet('/api/v1/register/entries'),
        apiGet('/api/v1/submissions/runs'),
        apiGet('/api/v1/submissions/windows'),
    ]);

    document.getElementById('entry-count').textContent = entries?.length ?? '—';
    document.getElementById('run-count').textContent = runs?.length ?? '—';
    document.getElementById('open-windows').textContent = windows?.length ?? '—';

    const latestEl = document.getElementById('latest-run');
    if (runs?.length > 0) {
        const r = runs[0];
        latestEl.innerHTML = `
            <a href="/dashboard/submission-detail.html?id=${r.id}">${r.id.slice(0, 8)}…</a>
            &nbsp;${badge(r.status)}&nbsp;
            <span class="text-muted">${fmtDateTime(r.created_at)}</span>
        `;
    } else {
        latestEl.textContent = 'No submission runs yet.';
    }

    const windowsEl = document.getElementById('next-window');
    if (windows?.length > 0) {
        const w = windows[0];
        windowsEl.innerHTML = `<strong>${w.authority_code}</strong> — ${w.reporting_year} — closes <strong>${w.window_close_date}</strong>`;
    } else {
        windowsEl.textContent = 'No open submission windows.';
    }
});
