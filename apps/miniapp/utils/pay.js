// 批量工具（D-070：30 天、90 天两档，一次付清、到期不扣钱）：只开通使用期，付款回调后必须查服务端订单。
const POLL_INTERVAL = 2000;
const POLL_TIMEOUT = 60000;
const PAY_MODE = 'short_series_goods';
const TEXT = {
  ordering: '正在创建订单…', confirming: '正在确认支付结果…', cancelled: '已取消支付',
  closed: '这笔订单已关闭，可以重新支付',
  unsettled: '上一笔付款还在确认，先不要再付。如果已经扣款会自动开通；超过 10 分钟还没开通，请点下方"遇到问题"告诉我们。',
  login: '微信登录没有完成，请重试', unsupported: '当前微信版本不支持这项支付，请升级微信后重试',
  timeout: '暂未确认到支付结果，请稍后到"我的 → 订单"查看；如已扣款会自动开通。超过 10 分钟还没开通，请点下方"遇到问题"告诉我们。',
  orderFailed: '下单没有完成，请重试', termsPending: '付费服务暂未开放',
  termsRequired: '请先阅读并同意《付费服务协议》',
  refund: '已申请退款，按原支付方式退回，到账时间以微信支付为准',
  invoiceSent: '已提交开票申请：每月统一开具一次电子发票，发到你填的邮箱',
};
function yuan(fen, fixed = false) { return fixed ? (fen / 100).toFixed(2) : String(Number((fen / 100).toFixed(2))); }
function date(value, short = false) {
  const stamp = value && Date.parse(value);
  return Number.isFinite(stamp) ? new Date(stamp + 8 * 3600000).toISOString().slice(short ? 5 : 0, 10) : '';
}
// 北京时间"10月20日"：句子里的到期日用这个，比 10-20 好读
function monthDay(value) {
  const day = date(value);
  return day ? `${Number(day.slice(5, 7))}月${Number(day.slice(8, 10))}日` : '';
}
// 服务端在售的档位，按天数从短到长（30 天、90 天）；格式不对的丢掉
function normalizeProducts(res) {
  return ((res && Array.isArray(res.items)) ? res.items : []).filter((item) => item
    && typeof item.productId === 'string' && item.productId
    && Number.isInteger(item.price) && item.price > 0 && Number.isInteger(item.days) && item.days > 0)
    .sort((a, b) => a.days - b.days);
}
const NAME = '批量工具';
// 一档的页面字段。长于 30 天的写"折合每 30 天约 N 元"（按整元四舍五入），不写"原价""立省"：
// 没有真实成交基准的比较价属于虚假价格比较（《明码标价和禁止价格欺诈规定》第十六、十九条，D-070）
function tierView(product) {
  const per30 = product.days > 30 ? Math.round((product.price * 30) / product.days / 100) : 0;
  return { productId: product.productId, days: product.days, name: `${NAME} ${product.days} 天`, price: yuan(product.price),
    per30: per30 ? `折合每 30 天约 ${per30} 元` : '' };
}
// 工具页上的价格一句（在售时）："30 天 29 元、90 天 79 元"
function tiersLine(products) {
  return products.map((item) => `${item.days} 天 ${yuan(item.price)} 元`).join('、');
}
// 续买多送（D-070 ⑥）：规则只有一句，和价格写在同一屏。服务端开关关着时为 null，页面不提
function renewRule(res) {
  const bonus = res && res.renewBonus;
  if (!bonus || !Number.isInteger(bonus.days) || bonus.days <= 0 || !Number.isInteger(bonus.windowDays) || bonus.windowDays <= 0) return null;
  return { text: `到期前后 ${bonus.windowDays} 天内续买，每次多送 ${bonus.days} 天`, now: bonus.eligibleNow === true };
}
function termsFinal(terms) {
  return !!(terms && terms.final && typeof terms.text === 'string' && terms.text.trim() && !/待.*定稿|占位/.test(terms.text));
}
// 能开哪种发票（D-070：付款前要看得到）：以服务端为准（YOUJU_PAY_INVOICE_TYPE，商品接口带回 invoiceType）；
// 没有时现网没有付款按钮，服务端也不卖（payments.orderable）
function invoiceType(res) {
  const value = res && typeof res.invoiceType === 'string' ? res.invoiceType.trim() : '';
  return value && !/【|待/.test(value) ? value : '';
}
// 到期前 3 天的提示（D-070 第二节"在哪里买"③）：不发消息，只在批量工具页、购买页写一句
function expiryNotice(pass, now = Date.now()) {
  if (!pass || pass.active !== true) return '';
  const end = Date.parse(pass.expiresAt);
  if (!Number.isFinite(end) || end - now > 3 * 86400000) return '';
  return `${NAME} ${monthDay(pass.expiresAt)}到期，到期不再扣钱；需要可以再买一次`;
}
function toolStatus(res, kind, now = Date.now()) {
  const quota = res.quota[kind];
  const active = !!(res.pass && res.pass.active);
  const expiry = active ? date(res.pass.expiresAt, true) : '';
  return {
    active,
    line: active ? `${NAME}使用期到 ${expiry} · 不限次数，每次 500 家以内`
      : `本功能每月免费 3 次，每次 50 家以内 · 本月剩余 ${Math.max(0, quota.limit - quota.used)} 次`,
    notice: expiryNotice(res.pass, now),
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
    offer: input.product ? `${NAME} ${input.product.days} 天（${yuan(input.product.price)} 元）：不限次数，每次 500 家以内。` : '',
    split: code === 'FREE_ROW_LIMIT' && input.remaining > 0,
  };
}
// 不能自助退款的原因：服务端返回的就是给用户看的文字（payments.refund_reason）
function refundReason(reason) {
  return reason || '';
}
// 随这笔订单送的天数，每次一行（续买多送、加量体验折算；D-070 逐笔记账）
function giftLines(gifts) {
  return (Array.isArray(gifts) ? gifts : []).filter((gift) => gift && typeof gift.name === 'string' && Number.isInteger(gift.days) && gift.days > 0)
    .map((gift) => {
      const start = date(gift.startsAt, true); const end = date(gift.endsAt, true);
      return `${gift.name} ${gift.days} 天${gift.revoked ? '（已收回）' : start && end ? `：${start} 至 ${end}` : ''}`;
    });
}
function orderView(order) {
  const start = date(order.passStartsAt, true); const end = date(order.passEndsAt, true);
  const days = Number.isInteger(order.days) && order.days > 0 ? order.days
    : Math.round((Date.parse(order.passEndsAt) - Date.parse(order.passStartsAt)) / 86400000);
  const status = order.status === '已完成' && !order.passRevokedAt ? '已开通' : order.status;
  const name = typeof order.name === 'string' && order.name ? order.name : `${NAME} ${days} 天`;
  // 随这笔送的续买天数退款时一起收回；体验折算的是自己的体验时间，不收回（服务端 payments.revoke_pass）
  const bonus = (Array.isArray(order.gifts) ? order.gifts : []).filter((gift) => gift && gift.kind === 'renewal' && !gift.revoked && Number.isInteger(gift.days))
    .reduce((sum, gift) => sum + gift.days, 0);
  // 按剩余天数退的（系统故障、停止服务）：退完了写"已退回"，还在办理的写"处理中"，不让人以为钱已到账
  const partial = Number.isInteger(order.refundAmount) && order.refundAmount > 0 && order.refundAmount !== order.amount;
  return Object.assign({}, order, {
    name,
    summary: `${name} · ${yuan(order.amount, true)} 元${order.paidAt ? ` · ${date(order.paidAt)} 付款` : ''} · ${status}${start && end ? ` · 使用期 ${start} 至 ${end}` : ''}`,
    // 订单页分行显示用：状态、金额、付款日期、使用期、送的天数
    statusText: status,
    statusTone: status === '已开通' ? 'on' : /退款/.test(status) ? 'refund' : 'plain',
    amountText: yuan(order.amount, true),
    paidText: order.paidAt ? date(order.paidAt) : '',
    period: start && end ? `${start} 至 ${end}` : '',
    gifts: giftLines(order.gifts),
    refundText: !partial ? '' : order.status === '已退款' ? `已退回 ${yuan(order.refundAmount, true)} 元（按剩余天数）`
      : order.status === '退款中' ? `按剩余天数退 ${yuan(order.refundAmount, true)} 元，处理中`
        : order.status === '需人工处理' ? `按剩余天数退 ${yuan(order.refundAmount, true)} 元，正在人工核对` : '',
    // 已经退款或正在退款的不再写"不能自助退款"
    reason: order.canRefund || /退款/.test(order.status || '') ? '' : refundReason(order.refundUnavailableReason),
    refundConfirm: `退款后，这张订单对应的 ${days} 天使用期${bonus ? `和续买多送的 ${bonus} 天` : ''}取消，${yuan(order.amount, true)} 元原路退回。确定吗？`,
    // 发票：已完成、没退款的订单可以申请；待开时可以改
    invoiceAction: !order.canInvoice ? '' : !order.invoice ? '申请发票' : order.invoice.canEdit ? '修改发票信息' : '',
    invoiceText: invoiceText(order.invoice),
  });
}
function invoiceText(invoice) {
  if (!invoice) return '';
  return [invoice.titleType === 'company' ? '单位' : '个人', invoice.title, invoice.titleType === 'company' ? invoice.taxId : '', invoice.email].filter(Boolean).join(' · ');
}
// 和服务端 api/pay.py 的校验一致：先在本机拦下明显填错的，服务端仍会再查一遍
const TAX_ID = /^(?:[0-9A-Z]{15}|[0-9A-Z]{18}|[0-9A-Z]{20})$/;
const EMAIL = /^[^@\s]{1,64}@[^@\s.]+(?:\.[^@\s.]+)+$/;
function invoiceBody(form) {
  const company = form.titleType === 'company';
  return { title_type: company ? 'company' : 'personal', title: String(form.title || '').trim(),
    tax_id: company ? String(form.taxId || '').trim().toUpperCase() : null, email: String(form.email || '').trim() };
}
function invoiceProblem(form) {
  const body = invoiceBody(form);
  if (!body.title || /[\u0000-\u001f\u007f]/.test(body.title)) return '请填写发票抬头';
  if (body.title.length > 100) return '发票抬头不超过 100 字';
  if (body.title_type === 'company' && !TAX_ID.test(body.tax_id)) return '单位抬头要填税号（统一社会信用代码）';
  if (!EMAIL.test(body.email) || body.email.length > 254) return '请填写能收发票的邮箱';
  return '';
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
module.exports = { POLL_INTERVAL, POLL_TIMEOUT, PAY_MODE, TEXT, yuan, date, monthDay, normalizeProducts, tierView, tiersLine, renewRule, termsFinal, invoiceType, expiryNotice, toolStatus, limitView, refundReason, giftLines, orderView, invoiceText, invoiceBody, invoiceProblem, paymentParams, isCancel, classify, createPoller };
