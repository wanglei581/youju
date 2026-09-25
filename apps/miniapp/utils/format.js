/* Date, size and text helpers for display. */

function pad(value) {
  return value < 10 ? '0' + value : String(value);
}

function toDate(value) {
  if (value instanceof Date) return value;
  if (typeof value === 'number') return new Date(value);
  const date = new Date(String(value || ''));
  return isNaN(date.getTime()) ? null : date;
}

/** "2026-09-25 14:05" in the device's local time. */
function dateTime(value) {
  const date = toDate(value);
  if (!date) return '';
  return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate()) +
    ' ' + pad(date.getHours()) + ':' + pad(date.getMinutes());
}

/** "20260925-140512" for file names. */
function stamp(value) {
  const date = toDate(value) || new Date();
  return date.getFullYear() + pad(date.getMonth() + 1) + pad(date.getDate()) + '-' +
    pad(date.getHours()) + pad(date.getMinutes()) + pad(date.getSeconds());
}

/** "刚刚" / "5 分钟前" / "3 小时前" / full date. */
function relative(value) {
  const date = toDate(value);
  if (!date) return '';
  const diff = Date.now() - date.getTime();
  if (diff < 0) return dateTime(date);
  if (diff < 60 * 1000) return '刚刚';
  if (diff < 60 * 60 * 1000) return Math.floor(diff / 60000) + ' 分钟前';
  if (diff < 24 * 60 * 60 * 1000) return Math.floor(diff / 3600000) + ' 小时前';
  return dateTime(date);
}

/** Date part of an ISO timestamp from the server ("2026-09-25T06:00:00+00:00" -> "2026-09-25"). */
function isoDay(value) {
  const text = String(value || '');
  return /^\d{4}-\d{2}-\d{2}/.test(text) ? text.slice(0, 10) : '';
}

function bytes(size) {
  const value = Number(size) || 0;
  if (value < 1024) return value + ' B';
  if (value < 1024 * 1024) return (value / 1024).toFixed(1) + ' KB';
  return (value / 1024 / 1024).toFixed(2) + ' MB';
}

function truncate(text, max) {
  const value = String(text || '');
  return value.length > max ? value.slice(0, max - 1) + '…' : value;
}

/** Host part of an https URL for compact display; '' if it cannot be parsed. */
function urlHost(url) {
  const match = /^https?:\/\/([^/?#]+)/i.exec(String(url || ''));
  return match ? match[1] : '';
}

module.exports = { dateTime, stamp, relative, isoDay, bytes, truncate, urlHost };
