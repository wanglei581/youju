// 客户工作台：只开通使用期，付款回调后必须查服务端订单。
const POLL_INTERVAL = 2000;
const POLL_TIMEOUT = 60000;
const PAY_MODE = 'short_series_goods';
const TEXT = {
  ordering: '正在创建订单…', confirming: '正在确认支付结果…', cancelled: '已取消支付',
  login: '微信登录没有完成，请重试', unsupported: '当前微信版本不支持这项支付，请升级微信后重试',
  timeout: '暂未确认到支付结果，请稍后到"我的 → 订单"查看；如已扣款会自动开通。超过 10 分钟还没开通，请点下方"遇到问题"告诉我们。',
  orderFailed: '下单没有完成，请重试', termsPending: '付费服务暂未开放',
  termsRequired: '请先阅读并同意《付费服务协议》',
  refund: '正在原路退款，一般 1–3 个工作日到账',
};
function yuan(fen, fixed = false) { return fixed ? (fen / 100).toFixed(2) : String(Number((fen / 100).toFixed(2))); }
function date(value, short = false) {
  const stamp = value && Date.parse(value);
  return Number.isFinite(stamp) ? new Date(stamp + 8 * 3600000).toISOString().slice(short ? 5 : 0, 10) : '';
}
function normalizeProducts(res) {
  return ((res && Array.isArray(res.items)) ? res.items : []).filter((item) => item
    && typeof item.productId === 'string' && item.productId
    && Number.isInteger(item.price) && item.price > 0 && Number.isInteger(item.days) && item.days > 0);
}
function termsFinal(terms) {
  return !!(terms && terms.final && typeof terms.text === 'string' && terms.text.trim() && !/待.*定稿|占位/.test(terms.text));
}
function toolStatus(res, kind, now = Date.now()) {
  const quota = res.quota[kind];
  const active = !!(res.pass && res.pass.active);
  const expiry = active ? date(res.pass.expiresAt, true) : '';
  return {
    active,
    line: active ? `客户工作台使用期到 ${expiry} · 不限次数，每次 500 家以内`
      : `本功能每月免费 3 次，每次 50 家以内 · 本月剩余 ${Math.max(0, quota.limit - quota.used)} 次`,
    notice: active && Date.parse(res.pass.expiresAt) - now <= 3 * 86400000
      ? `客户工作台 ${expiry} 到期，到期不再扣钱；需要可以再买一次` : '',
  };
}
function limitView(input) {
  if (input.passActive && !['FREE_ROW_LIMIT', 'FREE_MONTHLY_LIMIT'].includes(input.code)) return null;
  const code = input.code || (input.quotaReady && input.rowCount > input.maxRows ? 'FREE_ROW_LIMIT'
    : input.quotaReady && input.rowCount > 0 && input.remaining <= 0 ? 'FREE_MONTHLY_LIMIT' : '');
  if (!['FREE_ROW_LIMIT', 'FREE_MONTHLY_LIMIT'].includes(code)) return null;
  return {
    message: code === 'FREE_ROW_LIMIT' ? `这份名单有 ${input.rowCount} 家，超过了免费的每次 50 家。`
      : `本月 3 次免费${input.kind === 'track' ? '客户相关通知表' : '批量名单比对'}已用完，下月 1 日恢复。`,
    offer: input.product ? `客户工作台 30 天（${yuan(input.product.price)} 元）：不限次数，每次 500 家以内。` : '',
    split: code === 'FREE_ROW_LIMIT' && input.remaining > 0,
  };
}
function refundReason(reason) {
  if (/App Store/.test(reason || '')) return '苹果手机付款的订单请在 App Store 申请退款';
  if (/超过 7 天/.test(reason || '')) return '已超过 7 天';
  if (/用过付费的量/.test(reason || '')) return '已提交过超过 50 家的名单或已超出每月免费次数';
  return reason || '';
}
function orderView(order) {
  const start = date(order.passStartsAt, true); const end = date(order.passEndsAt, true);
  const days = Math.round((Date.parse(order.passEndsAt) - Date.parse(order.passStartsAt)) / 86400000);
  const status = order.status === '已完成' && !order.passRevokedAt ? '已开通' : order.status;
  return Object.assign({}, order, {
    summary: `客户工作台 30 天 · ${yuan(order.amount, true)} 元${order.paidAt ? ` · ${date(order.paidAt)} 付款` : ''} · ${status}${start && end ? ` · 使用期 ${start} 至 ${end}` : ''}`,
    reason: order.canRefund ? '' : refundReason(order.refundUnavailableReason),
    refundConfirm: `退款后，这张订单对应的 ${days} 天使用期取消，${yuan(order.amount, true)} 元原路退回。确定吗？`,
  });
}
function paymentParams(order) { return { signData: order.signData, paySig: order.paySig, signature: order.signature, mode: order.mode || PAY_MODE }; }
function isCancel(err) { return !!err && (err.errCode === -2 || /cancel/i.test(err.errMsg || '')); }
function classify(order) {
  if (order && order.status === '已完成' && order.passStartsAt && order.passEndsAt && !order.passRevokedAt) return 'success';
  if (order && ['退款中', '已退款', '需人工处理', '已关闭'].includes(order.status)) return 'failed';
  return 'pending';
}
// 独立的截止计时器保证查单网络请求悬挂时也能在 60 秒结束；隐藏/卸载忽略在途结果。
function createPoller(options) {
  const timers = options.timers || {}; const set = timers.set || setTimeout; const clear = timers.clear || clearTimeout;
  const now = options.now || (() => Date.now());
  let epoch = 0; let retry = null; let expiry = null; let active = false; let last = null;
  function stop() {
    epoch += 1; active = false;
    if (retry !== null) clear(retry);
    if (expiry !== null) clear(expiry);
    retry = null; expiry = null;
  }
  function finish(current, state) {
    if (current !== epoch) return;
    const order = last; stop(); options.onDone({ state, order });
  }
  function tick(current) {
    Promise.resolve().then(() => current === epoch ? options.fetch() : null).then((order) => {
      if (current !== epoch) return;
      last = order; const state = classify(order);
      if (state !== 'pending') { finish(current, state); return; }
      schedule(current);
    }, () => { if (current === epoch) schedule(current); });
  }
  function schedule(current) {
    retry = set(() => { retry = null; tick(current); }, options.interval || POLL_INTERVAL);
  }
  return {
    start(deadline = now() + (options.timeout === undefined ? POLL_TIMEOUT : options.timeout)) {
      stop(); active = true; last = null; const current = epoch;
      if (deadline <= now()) { finish(current, 'timeout'); return; }
      expiry = set(() => finish(current, 'timeout'), deadline - now()); tick(current);
    },
    stop, isActive: () => active,
  };
}
module.exports = { POLL_INTERVAL, POLL_TIMEOUT, PAY_MODE, TEXT, yuan, date, normalizeProducts, termsFinal, toolStatus, limitView, refundReason, orderView, paymentParams, isCancel, classify, createPoller };
