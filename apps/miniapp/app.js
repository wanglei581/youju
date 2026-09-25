/* 行业研究台 · 微信原生小程序（本地受控试用版）
 * All data comes from the configured API (default http://127.0.0.1:8766).
 * No sample data, no AI, no login; watches and export history are device-local. */
const config = require('./utils/config');
const storage = require('./utils/storage');

App({
  globalData: {
    // Filters handed from 关注 → 找答案 (switchTab cannot carry a query string).
    pendingFilters: null,
    // Last /api/health result; also persisted so 我的 can show it immediately.
    health: null,
  },

  onLaunch() {
    this.globalData.health = storage.read(config.STORAGE_KEYS.health, null);
  },

  setHealth(result) {
    this.globalData.health = result;
    storage.write(config.STORAGE_KEYS.health, result);
  },
});
