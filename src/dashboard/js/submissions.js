/* Submission history — lists all submission runs for the tenant. */

import { requireAuth, markActiveNav } from './auth.js';
import { apiGet, fmtDateTime, badge } from './api.js';

if (!requireAuth()) throw new Error('redirect');

document.addEventListener('DOMContentLoaded', async () => {
    markActiveNav();
    const runs = await apiGet('/api/v1/submissions/runs');
    const tbody = document.getElementById('runs-tbody');
    const empty = document.getElementById('empty-msg');

    if (!runs || runs.length === 0) {
        empty.classList.remove('hidden');
        return;
    }

    tbody.innerHTML = runs.map(r => `
        <tr>
            <td><a href="/dashboard/submission-detail.html?id=${r.id}">${r.id.slice(0, 12)}…</a></td>
            <td>${r.reporting_year}</td>
            <td>${badge(r.status)}</td>
            <td>${badge(r.validation_overall_status)}</td>
            <td>${r.validation_issue_count}</td>
            <td>${r.entry_count}</td>
            <td>${fmtDateTime(r.created_at)}</td>
            <td>${r.submitted_at ? fmtDateTime(r.submitted_at) : '<span class="text-muted">—</span>'}</td>
        </tr>
    `).join('');
});
