(() => {
  "use strict";

  const nativeFetch = window.fetch.bind(window);
  const csrfToken = () =>
    document.querySelector('meta[name="csrf-token"]')?.getAttribute('content') || '';

  window.getCSRFToken = csrfToken;

  window.apiFetch = function (url, options = {}) {
    const config = { ...options, credentials: options.credentials || 'same-origin' };
    const method = String(config.method || 'GET').toUpperCase();
    const headers = new Headers(config.headers || {});

    if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
      const token = csrfToken();
      if (token) headers.set('X-CSRF-Token', token);
    }

    // Most JSON API calls in the application pass JSON.stringify(...).  The
    // old helper never declared that content type, so Flask's get_json(...)
    // silently returned {} and the affected endpoints answered 400.  Keep
    // FormData/URLSearchParams behaviour untouched.
    if (
      config.body != null &&
      typeof config.body === 'string' &&
      !headers.has('Content-Type')
    ) {
      headers.set('Content-Type', 'application/json');
    }

    config.headers = headers;
    return nativeFetch(url, config);
  };

  window.escapeHtml = value =>
    String(value ?? '').replace(/[&<>"']/g, m => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#039;'
    }[m]));

  window.readJSON = async response => {
    const text = await response.text();
    try {
      return text ? JSON.parse(text) : {};
    } catch (_) {
      return { error: `Server returned HTTP ${response.status}.` };
    }
  };
})();
