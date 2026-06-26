/* Submission run detail — loads a single run by ?id= and renders all fields. */

import { requireAuth, markActiveNav } from './auth.js';
import { apiGet, fmtDateTime, qs, badge } from './api.js';

if (!requireAuth()) throw new Error('redirect');

document.addEventListener('DOMContentLoaded', async () => {
    markActiveNav();
    const runId = qs('id');
    if (!runId) { _showError('No run ID in URL.'); return; }

    let run;
    try {
        run = await apiGet(`/api/v1/submissions/runs/${runId}`);
    } catch (err) {
        _showError(err.message.includes('404') ? 'Submission run not found.' : 'Failed to load run.');
        return;
    }
    if (!run) return;

    document.getElementById('page-title').textContent = `Run ${run.id.slice(0, 8)}…`;
    document.getElementById('status-badge').innerHTML = badge(run.status);
    document.getElementById('val-badge').innerHTML = badge(run.validation_overall_status);
    _set('run-id', run.id);
    _set('window-id', run.submission_window_id);
    _set('reporting-year', run.reporting_year);
    _set('val-issues', run.validation_issue_count);
    _set('entry-count', run.entry_count);
    _set('created-at', fmtDateTime(run.created_at));
    _set('updated-at', fmtDateTime(run.updated_at));
    _set('submitted-at', run.submitted_at ? fmtDateTime(run.submitted_at) : '—');
    _set('reference', run.submission_reference || '—');
});

function _set(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val ?? '—';
}

function _showError(msg) {
    const el = document.getElementById('error-msg');
    if (el) { el.textContent = msg; el.classList.remove('hidden'); }
}
