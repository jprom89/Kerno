/* Register entry detail — loads a single entry by ?id= and renders all fields. */

import { requireAuth, markActiveNav } from './auth.js';
import { apiGet, fmtDate, qs, badge } from './api.js';

if (!requireAuth()) throw new Error('redirect');

document.addEventListener('DOMContentLoaded', async () => {
    markActiveNav();
    const entryId = qs('id');
    if (!entryId) { _showError('No entry ID in URL.'); return; }

    let entry;
    try {
        entry = await apiGet(`/api/v1/register/entries/${entryId}`);
    } catch (err) {
        _showError(err.message.includes('404') ? 'Entry not found.' : 'Failed to load entry.');
        return;
    }
    if (!entry) return;

    document.getElementById('page-title').textContent = entry.provider_name;
    _set('provider-name', entry.provider_name);
    _set('service-name', entry.service_name);
    _set('provider-type', entry.provider_type);
    _set('criticality', entry.criticality_level);
    _set('business-function', entry.business_function);
    _set('data-types', (entry.data_types || []).join(', ') || '—');
    _set('countries', (entry.countries_supported || []).join(', ') || '—');
    _set('contract-start', fmtDate(entry.contract_start_date));
    _set('contract-end', fmtDate(entry.contract_end_date));
    _set('exit-strategy', entry.exit_strategy_summary || '—');
    _set('source-id', entry.source_record_id || '—');
    _set('created-at', fmtDate(entry.created_at));
    _set('updated-at', fmtDate(entry.updated_at));
    document.getElementById('status-badge').innerHTML = badge(entry.is_active ? 'active' : 'inactive');
});

function _set(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val || '—';
}

function _showError(msg) {
    const el = document.getElementById('error-msg');
    if (el) { el.textContent = msg; el.classList.remove('hidden'); }
}
