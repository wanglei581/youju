/* Client constants. Limits mirror services/api (store.public_search LIMIT 50,
 * store.public_selected 1–100 ids, 100-char filters). The API address is
 * user-editable in 我的 → 服务地址与连接. */

module.exports = {
  APP_VERSION: '0.1.0',
  DEFAULT_BASE_URL: 'http://127.0.0.1:8766',
  EXPECTED_PRODUCT: 'industry-policy-local',

  RESULT_LIMIT: 50,
  MAX_SELECTED: 100,
  FILTER_MAX_LENGTH: 100,
  MAX_WATCHES: 20,
  MAX_EXPORT_HISTORY: 50,

  REQUEST_TIMEOUT: 15000,
  EXPORT_TIMEOUT: 60000,

  STORAGE_KEYS: {
    settings: 'ip_settings_v1',
    health: 'ip_health_v1',
    selection: 'ip_selection_v1',
    watches: 'ip_watches_v1',
    exports: 'ip_exports_v1',
  },
};
