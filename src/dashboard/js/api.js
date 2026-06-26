/* Shared API client for all dashboard pages.
   All calls attach the stored JWT as a Bearer token. A 401 response triggers
   automatic logout and redirect to login. */

import { getToken, logout } from './auth.js';

export async function apiGet(path) {
    const response = await fetch(path, {
        headers: { 'Authorization': `Bearer ${getToken()}` }
    });
    if (response.status === 401) { logout(); return null; }
    if (!response.ok) throw new Error(`API ${response.status} on GET ${path}`);
    return response.json();
}

export async function apiPost(path, body) {
    const response = await fetch(path, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${getToken()}`
        },
        body: JSON.stringify(body)
    });
    if (response.status === 401) { logout(); return null; }
    if (!response.ok) throw new Error(`API ${response.status} on POST ${path}`);
    return response.json();
}

export function fmtDate(iso) {
    if (!iso) return '—';
    return new Date(iso).toLocaleDateString('en-GB', {
        day: '2-digit', month: 'short', year: 'numeric'
    });
}

export function fmtDateTime(iso) {
    if (!iso) return '—';
    return new Date(iso).toLocaleString('en-GB', {
        day: '2-digit', month: 'short', year: 'numeric',
        hour: '2-digit', minute: '2-digit'
    });
}

export function badge(text) {
    const cls = `badge badge-${(text || 'unknown').toLowerCase()}`;
    return `<span class="${cls}">${text || '—'}</span>`;
}

export function qs(key) {
    return new URLSearchParams(window.location.search).get(key);
}
