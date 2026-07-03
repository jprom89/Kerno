/* Control-coverage dashboard (KER-109) — summary tiles, category breakdown, and
   a drill-down control list. Status figures are the system of record: human
   overrides (KER-106) win over AI recommendations, resolved server-side by
   /api/v1/coverage. Status is always rendered as icon + text label + badge
   colour, never colour alone (WCAG AA). */

import { requireAuth, markActiveNav, getToken } from './auth.js';
import { apiGet } from './api.js';

if (!requireAuth()) throw new Error('redirect');

/* Icon + label pairs keep status legible without colour; badge classes reuse
   the existing AA-contrast palette. */
const STATUS_LABEL = { met: '✓ met', partial: '◐ partial', gap: '✗ gap' };
const STATUS_BADGE_CLASS = { met: 'badge-pass', partial: 'badge-warn', gap: 'badge-fail' };

let _controls = [];
let _categoryFilter = null;
let _statusFilter = null;

document.addEventListener('DOMContentLoaded', async () => {
    markActiveNav();
    _wireInteractions();
    await _loadCoverage();
});

async function _loadCoverage() {
    let summary;
    try {
        [summary, _controls] = await Promise.all([
            apiGet('/api/v1/coverage/summary'),
            apiGet('/api/v1/coverage/controls'),
        ]);
    } catch (err) {
        _showError('Failed to load coverage data.');
        return;
    }
    if (!summary || !_controls) return;
    _renderSummary(summary);
    _renderCategories(summary.categories);
    _renderControls();
}

function _renderSummary(summary) {
    const total = summary.total_controls;
    for (const status of ['met', 'partial', 'gap']) {
        document.getElementById(`${status}-count`).textContent = summary[status];
        const pct = total > 0 ? Math.round((summary[status] / total) * 100) : 0;
        document.getElementById(`${status}-pct`).textContent =
            `${pct}% of ${total} controls`;
    }
}

function _renderCategories(categories) {
    const tbody = document.getElementById('categories-tbody');
    tbody.replaceChildren();
    for (const cat of categories) {
        const tr = document.createElement('tr');
        const nameCell = document.createElement('td');
        const drillButton = document.createElement('button');
        drillButton.className = 'btn btn-secondary';
        drillButton.textContent = cat.category;
        drillButton.setAttribute('aria-label', `Show controls in category ${cat.category}`);
        drillButton.addEventListener('click', () => _drillIntoCategory(cat.category));
        nameCell.appendChild(drillButton);
        tr.appendChild(nameCell);
        for (const count of [cat.met, cat.partial, cat.gap, cat.total]) {
            const td = document.createElement('td');
            td.textContent = count;
            tr.appendChild(td);
        }
        tbody.appendChild(tr);
    }
}

async function _drillIntoCategory(category) {
    _categoryFilter = category;
    try {
        _controls = await apiGet(`/api/v1/coverage/controls?category=${encodeURIComponent(category)}`);
    } catch (err) {
        _showError('Failed to load controls for this category.');
        return;
    }
    if (!_controls) return;
    _renderControls();
}

function _renderControls() {
    const visible = _statusFilter
        ? _controls.filter(c => c.status === _statusFilter)
        : _controls;
    _updateHeading(visible.length);

    const tbody = document.getElementById('controls-tbody');
    tbody.replaceChildren();
    document.getElementById('controls-empty').classList.toggle('hidden', visible.length > 0);
    for (const control of visible) {
        tbody.appendChild(_controlRow(control));
    }
}

/* Rows built with createElement/textContent — titles are catalogue text and the
   panel link must not be constructable from injected markup. */
function _controlRow(control) {
    const tr = document.createElement('tr');

    const refCell = document.createElement('td');
    const link = document.createElement('a');
    link.href = `/dashboard/panel.html?control_id=${encodeURIComponent(control.control_id)}`;
    link.textContent = control.control_ref;
    link.setAttribute('aria-label', `Open review panel for ${control.control_ref}`);
    refCell.appendChild(link);
    tr.appendChild(refCell);

    _appendTextCell(tr, control.title);

    const statusCell = document.createElement('td');
    const statusBadge = document.createElement('span');
    statusBadge.className = `badge ${STATUS_BADGE_CLASS[control.status] || ''}`;
    statusBadge.textContent = STATUS_LABEL[control.status] || control.status;
    statusCell.appendChild(statusBadge);
    tr.appendChild(statusCell);

    _appendTextCell(tr, control.human_confirmed ? '✓ Human-confirmed' : 'AI (unconfirmed)');
    _appendTextCell(tr, control.confidence_level
        ? `${control.confidence_level} (${Number(control.confidence_score).toFixed(2)})`
        : '—');
    _appendTextCell(tr, String(control.evidence_count));
    return tr;
}

function _appendTextCell(tr, text) {
    const td = document.createElement('td');
    td.textContent = text;
    tr.appendChild(td);
}

function _wireInteractions() {
    document.querySelectorAll('[data-status-filter]').forEach(tile => {
        tile.addEventListener('click', () => _toggleStatusFilter(tile));
    });
    document.getElementById('clear-filters').addEventListener('click', _clearFilters);
    document.getElementById('export-pack').addEventListener('click', _exportEvidencePack);
}

/* A plain <a href> cannot carry the Bearer token (auth is header-based), so the
   export fetches the pack with the token and hands it to the browser as a download. */
async function _exportEvidencePack() {
    if (!_categoryFilter) return;
    let response;
    try {
        response = await fetch(
            `/api/v1/export/evidence-pack?control_family=${encodeURIComponent(_categoryFilter)}`,
            { headers: { 'Authorization': `Bearer ${getToken()}` } },
        );
    } catch (err) {
        _showError('Failed to export the evidence pack.');
        return;
    }
    if (!response.ok) {
        _showError(`Evidence pack export failed (HTTP ${response.status}).`);
        return;
    }
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `kerno-evidence-pack-${_categoryFilter}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
}

function _toggleStatusFilter(tile) {
    const status = tile.dataset.statusFilter;
    _statusFilter = (_statusFilter === status) ? null : status;
    document.querySelectorAll('[data-status-filter]').forEach(t => {
        t.setAttribute('aria-pressed', String(t.dataset.statusFilter === _statusFilter));
    });
    _renderControls();
}

async function _clearFilters() {
    _statusFilter = null;
    _categoryFilter = null;
    document.querySelectorAll('[data-status-filter]').forEach(t => {
        t.setAttribute('aria-pressed', 'false');
    });
    try {
        _controls = await apiGet('/api/v1/coverage/controls');
    } catch (err) {
        _showError('Failed to reload controls.');
        return;
    }
    if (!_controls) return;
    _renderControls();
}

function _updateHeading(visibleCount) {
    const parts = [];
    if (_categoryFilter) parts.push(`category: ${_categoryFilter}`);
    if (_statusFilter) parts.push(`status: ${STATUS_LABEL[_statusFilter]}`);
    document.getElementById('controls-heading').textContent = parts.length
        ? `Controls (${parts.join(', ')}) — ${visibleCount}`
        : `All controls — ${visibleCount}`;
    document.getElementById('clear-filters').classList.toggle('hidden', parts.length === 0);
    document.getElementById('export-pack').classList.toggle('hidden', !_categoryFilter);
}

function _showError(msg) {
    const el = document.getElementById('error-msg');
    el.textContent = msg;
    el.classList.remove('hidden');
}
