/* Register entry list — fetches all entries for the tenant and renders a table. */

import { requireAuth, markActiveNav } from './auth.js';
import { apiGet, fmtDate, badge } from './api.js';

if (!requireAuth()) throw new Error('redirect');

document.addEventListener('DOMContentLoaded', async () => {
    markActiveNav();
    const entries = await apiGet('/api/v1/register/entries');
    const tbody = document.getElementById('entries-tbody');
    const empty = document.getElementById('empty-msg');

    if (!entries || entries.length === 0) {
        empty.classList.remove('hidden');
        return;
    }

    tbody.innerHTML = entries.map(e => `
        <tr>
            <td><a href="/dashboard/register-detail.html?id=${e.register_entry_id}">${_esc(e.provider_name)}</a></td>
            <td>${_esc(e.service_name)}</td>
            <td>${_esc(e.provider_type)}</td>
            <td>${_esc(e.criticality_level)}</td>
            <td>${_esc(e.business_function)}</td>
            <td>${badge(e.is_active ? 'active' : 'inactive')}</td>
            <td>${fmtDate(e.contract_end_date)}</td>
        </tr>
    `).join('');
});

function _esc(str) {
    if (!str) return '—';
    return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
