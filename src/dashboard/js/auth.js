/* Shared JWT token management for all dashboard pages.
   Import getToken / requireAuth / logout from this module. */

const TOKEN_KEY = 'kerno_jwt';

export function getToken() {
    return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token) {
    localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
    localStorage.removeItem(TOKEN_KEY);
}

export function logout() {
    clearToken();
    window.location.href = '/dashboard/login.html';
}

export function requireAuth() {
    if (!getToken()) {
        window.location.href = '/dashboard/login.html';
        return false;
    }
    return true;
}

export function markActiveNav() {
    const current = window.location.pathname;
    document.querySelectorAll('.nav-links a').forEach(link => {
        if (link.getAttribute('href') === current) {
            link.classList.add('active');
        }
    });
}

function _wireLogout() {
    const btn = document.querySelector('[data-action="logout"]');
    if (btn) btn.addEventListener('click', logout);
}

if (document.readyState !== 'loading') {
    _wireLogout();
} else {
    document.addEventListener('DOMContentLoaded', _wireLogout);
}
