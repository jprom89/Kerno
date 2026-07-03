/* Embedded side-panel (KER-108) — shows the recommendation and evidence context
   for a control linked to a host-tool issue (e.g. Jira, via ?control_id= and
   optional ?issue=) and posts approve/edit/reject decisions to the KER-106
   override API, refreshing the panel in place after each decision. */

import { requireAuth } from './auth.js';
import { apiGet, apiPost, fmtDateTime, qs } from './api.js';

if (!requireAuth()) throw new Error('redirect');

/* met/partial/gap reuse the existing badge palette instead of new CSS classes. */
const STATUS_BADGE_CLASS = { met: 'badge-pass', partial: 'badge-warn', gap: 'badge-fail' };
const ACTIONS_NEEDING_CORRECTION = ['edit', 'reject'];

let _controlId = null;
let _selectedAction = null;

document.addEventListener('DOMContentLoaded', async () => {
    _controlId = qs('control_id');
    const issueKey = qs('issue');
    if (issueKey) {
        document.getElementById('issue-context').textContent = `Linked from issue ${issueKey}`;
    }
    if (!_controlId) { _showError('No control_id in URL.'); return; }
    _wireActions();
    await _loadPanel();
});

async function _loadPanel() {
    let context;
    try {
        context = await apiGet(`/api/v1/panel/controls/${encodeURIComponent(_controlId)}`);
    } catch (err) {
        _showError('Failed to load control context.');
        return;
    }
    if (!context) return;
    _renderRecommendation(context.recommendation);
    _renderEvidence(context.evidence || []);
}

function _renderRecommendation(rec) {
    _set('control-id', _controlId);
    if (!rec) {
        _set('rec-status', 'No recommendation generated yet');
        ['rec-confidence', 'rec-review', 'rec-generated', 'rec-rationale', 'rec-gaps']
            .forEach(id => _set(id, '—'));
        document.getElementById('status-badge').innerHTML = '';
        return;
    }
    _set('rec-status', rec.status);
    _set('rec-confidence', `${rec.confidence_level} (${Number(rec.confidence_score).toFixed(2)})`);
    _set('rec-review', rec.requires_review ? 'Yes — flagged for human review' : 'No');
    _set('rec-generated', fmtDateTime(rec.generated_at));
    _set('rec-rationale', rec.rationale);
    _set('rec-gaps', rec.gaps || 'None identified');
    const badgeEl = document.createElement('span');
    badgeEl.className = `badge ${STATUS_BADGE_CLASS[rec.status] || ''}`;
    badgeEl.textContent = rec.status;
    document.getElementById('status-badge').replaceChildren(badgeEl);
}

function _renderEvidence(evidence) {
    const list = document.getElementById('evidence-list');
    const empty = document.getElementById('evidence-empty');
    list.replaceChildren();
    empty.classList.toggle('hidden', evidence.length > 0);
    for (const item of evidence) {
        list.appendChild(_evidenceListItem(item));
    }
}

/* Built with createElement/textContent (not innerHTML) because titles and
   external IDs are tenant-supplied text. */
function _evidenceListItem(item) {
    const li = document.createElement('li');
    li.style.cssText = 'padding:8px 0; border-bottom:1px solid var(--border);';

    const title = document.createElement('div');
    title.textContent = item.title || '(source record unavailable)';
    li.appendChild(title);

    const meta = document.createElement('div');
    meta.className = 'text-muted text-sm';
    const provenance = [
        item.source_system || 'unknown source',
        item.external_id ? `ref ${item.external_id}` : null,
        item.relevance_score != null ? `relevance ${Number(item.relevance_score).toFixed(2)}` : null,
        `linked by ${item.linked_by}`,
    ].filter(Boolean).join(' · ');
    meta.textContent = provenance;
    li.appendChild(meta);

    if (item.link_status === 'broken') {
        const broken = document.createElement('span');
        broken.className = 'badge badge-fail';
        broken.textContent = 'broken link';
        li.appendChild(broken);
    }
    return li;
}

function _wireActions() {
    document.querySelectorAll('[data-override-action]').forEach(btn => {
        btn.addEventListener('click', () => _selectAction(btn));
    });
    document.getElementById('submit-override').addEventListener('click', _submitOverride);
}

function _selectAction(clickedButton) {
    _selectedAction = clickedButton.dataset.overrideAction;
    document.querySelectorAll('[data-override-action]').forEach(btn => {
        btn.classList.toggle('btn-primary', btn === clickedButton);
        btn.classList.toggle('btn-secondary', btn !== clickedButton);
    });
    const needsCorrection = ACTIONS_NEEDING_CORRECTION.includes(_selectedAction);
    document.getElementById('corrected-group').classList.toggle('hidden', !needsCorrection);
    document.getElementById('submit-override').disabled = false;
}

async function _submitOverride() {
    _hideAlerts();
    if (!_selectedAction) { _showError('Choose approve, edit, or reject first.'); return; }
    const correctedControl = document.getElementById('corrected-control').value.trim();
    if (ACTIONS_NEEDING_CORRECTION.includes(_selectedAction) && !correctedControl) {
        _showError(`A corrected control ID is required to ${_selectedAction}.`);
        return;
    }
    const justification = document.getElementById('justification').value.trim();
    const body = {
        reviewer_role: document.getElementById('reviewer-role').value,
        action_type: _selectedAction,
        original_control_id: _controlId,
        corrected_control_id: correctedControl || null,
        justification_text: justification || null,
    };
    const submitButton = document.getElementById('submit-override');
    submitButton.disabled = true;
    let result;
    try {
        result = await apiPost('/api/v1/overrides', body);
    } catch (err) {
        _showError(err.message.includes('422')
            ? 'The API rejected the decision — check the corrected control ID.'
            : 'Failed to record the decision.');
        submitButton.disabled = false;
        return;
    }
    if (!result) return;
    _showSuccess(`Recorded ${result.action_type} at ${fmtDateTime(result.created_at)} (ref ${result.override_id.slice(0, 8)}…)`);
    _resetDecisionForm();
    await _loadPanel();
}

function _resetDecisionForm() {
    _selectedAction = null;
    document.querySelectorAll('[data-override-action]').forEach(btn => {
        btn.classList.remove('btn-primary');
        btn.classList.add('btn-secondary');
    });
    document.getElementById('corrected-group').classList.add('hidden');
    document.getElementById('corrected-control').value = '';
    document.getElementById('justification').value = '';
    document.getElementById('submit-override').disabled = true;
}

function _set(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val ?? '—';
}

function _showError(msg) {
    const el = document.getElementById('error-msg');
    el.textContent = msg;
    el.classList.remove('hidden');
}

function _showSuccess(msg) {
    const el = document.getElementById('success-msg');
    el.textContent = msg;
    el.classList.remove('hidden');
}

function _hideAlerts() {
    document.getElementById('error-msg').classList.add('hidden');
    document.getElementById('success-msg').classList.add('hidden');
}
