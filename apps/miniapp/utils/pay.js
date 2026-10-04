// 名单比对按次包：付费档视图、订单状态轮询和提示文案。
// 这里只放纯逻辑，不调用 wx 和接口，页面负责绑定。
// 以服务端订单状态为准：微信支付的 success 回调不算支付成功，回调后一律查订单。
const POLL_INTERVAL = 2000;
const POLL_TIMEOUT = 60000;
const PAY_MODE = 'short_series_goods';

// 服务端 GET /api/v1/pay/orders/{id} 返回的中文状态（payments.STATUS_LABELS）。
const SUCCESS_STATES = ['处理中', '已完成'];
const FAILED_STATES = ['退款中', '已退款', '需人工处理', '已关闭'];

const TEXT = {
  ordering: '正在创建订单…',
  confirming: '正在确认支付结果…',
  confirmed: '支付已确认，正在打开任务…',
  cancelled: '已取消支付',
  login: '微信登录没有完成，请重试',
  unsupported: '当前微信版本不支持这项支付，请升级微信后重试',
  timeoutPaid: '暂未确认到支付结果，请稍后到“我的任务”查看',
  timeoutFailed: '支付没有完成。如已扣款，请稍后到“我的任务”查看',
  redirect: '支付已确认，请到“我的任务”查看',
  orderFailed: '下单没有完成，请重试',
  fileMode: '付费比对请用“粘贴文本”提交名单',
  empty: '填写客户后可选择付费档',
  missing: '没有找到这个付费档，请返回后重试',
  notes: '比对失败自动退款；结果保存 30 天',
};

function yuan(fen) {
  return `${Math.floor(fen / 100)}.${String(fen % 100).padStart(2, '0')} 元`;
}

// 只保留字段完整的商品；接口 404、失败或返回空列表时都当作没有付费档。
function normalizeProducts(res) {
  const items = res && Array.isArray(res.items) ? res.items : [];
  return items
    .filter((item) => item && typeof item.productId === 'string' && item.productId
      && Number.isInteger(item.price) && item.price > 0
      && Number.isInteger(item.maxRows) && item.maxRows > 0)
    .map((item) => ({ productId: item.productId, price: item.price, maxRows: item.maxRows }))
    .sort((a, b) => a.maxRows - b.maxRows);
}

function findProduct(products, productId) {
  return (products || []).find((item) => item.productId === productId) || null;
}

function hint(products, input) {
  const largest = products[products.length - 1].maxRows;
  if (input.mode !== 'paste') return TEXT.fileMode;
  if (!input.rowCount) return TEXT.empty;
  if (input.rowCount > largest) return `超过 ${largest} 家，请减少客户后再付费比对`;
  if (input.rowCount > input.maxRows) return `超过免费的每次 ${input.maxRows} 家，可按次付费比对`;
  if (input.remaining <= 0) return '今天的免费次数已用完，可按次付费比对';
  return '本次可免费提交；需要比对更多家时可按次付费';
}

// 付费档视图：只有名单比对、且有可售商品时才显示，否则页面和未开通收费时完全一样。
function payView(input) {
  const products = input.products || [];
  if (input.kind !== 'compare' || !products.length) return { visible: false };
  const busy = !!input.busy;
  return {
    visible: true,
    busy,
    hint: hint(products, input),
    notes: TEXT.notes,
    message: input.message || '',
    error: !!input.error,
    items: products.map((item) => ({
      productId: item.productId,
      label: `${item.maxRows} 家以内 · ${yuan(item.price)}`,
      loading: busy && input.selected === item.productId,
      disabled: busy || input.mode !== 'paste' || !input.rowCount || input.rowCount > item.maxRows,
    })),
  };
}

// 下单前检查；返回空字符串表示可以下单。
function canBuy(product, input) {
  if (!product) return TEXT.missing;
  if (input.mode !== 'paste') return TEXT.fileMode;
  if (!input.rowCount) return '请先填写客户';
  if (input.rowCount > product.maxRows) return `这一档覆盖 ${product.maxRows} 家以内，请换一档或减少客户`;
  return '';
}

function confirmModal(product, rowCount) {
  return {
    title: '确认付费比对',
    content: [
      `价格 ${yuan(product.price)}，覆盖 ${product.maxRows} 家以内（本次 ${rowCount} 家）`,
      '比对失败自动退款',
      '结果保存 30 天',
      '整理的是公开信息，第三方工具，不承诺申报结果',
    ].join('\n'),
    confirmText: '确认支付',
  };
}

function paymentParams(order) {
  return { signData: order.signData, paySig: order.paySig, signature: order.signature, mode: order.mode || PAY_MODE };
}

function isCancel(err) {
  return !!err && (err.errCode === -2 || /cancel/i.test(err.errMsg || ''));
}

function orderError(err) {
  return (err && err.message) || TEXT.orderFailed;
}

function classify(order) {
  if (!order) return 'pending';
  if (SUCCESS_STATES.includes(order.status) && order.taskId !== null && order.taskId !== undefined) return 'success';
  if (FAILED_STATES.includes(order.status)) return 'failed';
  return 'pending';
}

// 轮询结果 → 页面文案。context.cancelled：用户取消；context.paid：微信回调 success（仅作提示用，不算成功）。
function resultView(result, context = {}) {
  if (result.state === 'success') return { message: TEXT.confirmed, error: false, taskId: result.order.taskId };
  if (result.state === 'failed') return { message: `订单状态：${result.order.status}`, error: true, taskId: null };
  if (context.cancelled) return { message: TEXT.cancelled, error: false, taskId: null };
  return { message: context.paid ? TEXT.timeoutPaid : TEXT.timeoutFailed, error: false, taskId: null };
}

// 每 2 秒查一次订单，60 秒内没有结果就停止；stop() 后在途结果一律忽略。start(true) 只查一次（用户取消后核对）。
function createPoller(options) {
  const fetch = options.fetch;
  const onDone = options.onDone;
  const timers = options.timers || {};
  const set = timers.set || setTimeout;
  const clear = timers.clear || clearTimeout;
  const now = options.now || (() => Date.now());
  const interval = options.interval || POLL_INTERVAL;
  const timeout = options.timeout === undefined ? POLL_TIMEOUT : options.timeout;
  let epoch = 0; let timer = null; let active = false; let last = null;

  function halt() {
    epoch += 1; active = false;
    if (timer !== null) { clear(timer); timer = null; }
  }
  function finish(current, result) {
    if (current !== epoch) return;
    halt();
    onDone(result);
  }
  function wait(current, deadline) {
    if (current !== epoch) return;
    if (now() + interval > deadline) { finish(current, { state: 'timeout', order: last }); return; }
    timer = set(() => { timer = null; tick(current, deadline); }, interval);
  }
  function tick(current, deadline) {
    Promise.resolve().then(() => fetch()).then((order) => {
      if (current !== epoch) return;
      last = order;
      const state = classify(order);
      if (state === 'pending') wait(current, deadline);
      else finish(current, { state, order });
    }, () => wait(current, deadline));
  }

  return {
    start(once) {
      halt();
      active = true; last = null;
      const current = epoch;
      tick(current, once ? now() : now() + timeout);
    },
    stop: halt,
    isActive: () => active,
  };
}

module.exports = {
  POLL_INTERVAL, POLL_TIMEOUT, PAY_MODE, TEXT, SUCCESS_STATES, FAILED_STATES,
  yuan, normalizeProducts, findProduct, payView, canBuy, confirmModal, paymentParams, isCancel, orderError,
  classify, resultView, createPoller,
};
