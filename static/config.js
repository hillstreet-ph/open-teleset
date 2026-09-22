/* open-teleset public runtime config (safe for Pages — no service-role keys) */
window.OPEN_TELESET_CONFIG = {
  appName: "open-teleset",
  locale: "en",
  supabaseUrl: "https://hoseohvgoiarxluxqwqv.supabase.co",
  // Replaced at deploy time from the GitHub production secret.
  supabaseAnonKey: "__SUPABASE_PUBLISHABLE_KEY__",
  apiBase: "https://open-teleset.hillstreet-ph.workers.dev",
  pagesOrigin: "https://open-teleset.site"
};

/*
 * Auto-configure axios baseURL when the axios CDN script loads.
 *
 * config.js is parsed BEFORE the axios <script> tag, so axios does not exist
 * yet.  We use Object.defineProperty to intercept the global assignment that
 * the axios UMD build performs (self.axios = factory()).  The setter fires
 * synchronously during the axios script's execution — before the Supabase SDK
 * or the Vue application script run — guaranteeing that every axios call
 * already has the correct baseURL.
 *
 * Fallback: if defineProperty fails for any reason, a DOMContentLoaded
 * listener sets the baseURL (may miss API calls made during Vue mount).
 */
(function () {
  var apiBase = window.OPEN_TELESET_CONFIG && window.OPEN_TELESET_CONFIG.apiBase;
  if (!apiBase) return;

  try {
    var _axiosVal;
    Object.defineProperty(window, 'axios', {
      configurable: true,
      enumerable: true,
      set: function (ax) {
        _axiosVal = ax;
        /* Remove the interceptor so window.axios becomes a normal property */
        try { delete window.axios; } catch (_) { /* IE compat */ }
        window.axios = ax;
        /* Configure baseURL */
        try {
          if (ax && ax.defaults) {
            ax.defaults.baseURL = apiBase;
          }
        } catch (e) {
          console.warn('[open-teleset] axios baseURL config failed:', e);
        }
      },
      get: function () { return _axiosVal; }
    });
  } catch (_) {
    /* Fallback for environments that do not support defineProperty on window */
    document.addEventListener('DOMContentLoaded', function () {
      if (typeof axios !== 'undefined' && axios.defaults) {
        axios.defaults.baseURL = apiBase;
      }
    });
  }
})();
