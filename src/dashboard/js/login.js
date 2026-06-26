/* Login page handler — submits credentials, stores JWT, redirects to dashboard. */

import { setToken, getToken } from './auth.js';

if (getToken()) {
    window.location.href = '/dashboard/index.html';
}

document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('login-form');
    const errorEl = document.getElementById('error-msg');
    const btn = document.getElementById('submit-btn');

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        btn.disabled = true;
        btn.textContent = 'Signing in…';
        errorEl.classList.add('hidden');

        const email = document.getElementById('email').value;
        const password = document.getElementById('password').value;

        try {
            const response = await fetch('/api/v1/auth/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email, password })
            });

            if (response.ok) {
                const data = await response.json();
                setToken(data.access_token);
                window.location.href = '/dashboard/index.html';
            } else {
                errorEl.textContent = 'Incorrect email or password.';
                errorEl.classList.remove('hidden');
                btn.disabled = false;
                btn.textContent = 'Sign in';
            }
        } catch {
            errorEl.textContent = 'Connection error. Please try again.';
            errorEl.classList.remove('hidden');
            btn.disabled = false;
            btn.textContent = 'Sign in';
        }
    });
});
