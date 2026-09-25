/* Thin wrappers around wx storage that never throw. wx.getStorageSync returns
 * '' for a missing key, which is mapped to the caller's fallback. */

function read(key, fallback) {
  try {
    const value = wx.getStorageSync(key);
    return value === '' || value === undefined || value === null ? fallback : value;
  } catch (error) {
    return fallback;
  }
}

function write(key, value) {
  try {
    wx.setStorageSync(key, value);
    return true;
  } catch (error) {
    return false;
  }
}

function remove(key) {
  try {
    wx.removeStorageSync(key);
  } catch (error) {
    /* ignore */
  }
}

module.exports = { read, write, remove };
