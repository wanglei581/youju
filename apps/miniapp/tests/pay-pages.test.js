// 批量工具（30 天、90 天两档，D-070）：购买、订单与开票、工具表单的价格卡片、我的订单、支付反馈、付费协议。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const idempotency = require('../utils/data-idempotency.js');
const helpers = require('../utils/advisor.js');
const advisorTerms = require('../content/advisor-terms.js');
const payTerms = require('../content/pay-terms.js');
const { render, textOf, findAll, tapsTo } = require('./wxml-render.js');

const root = path.join(__dirname, '..');
const OWNER = 'a'.repeat(64);
const PRODUCT = { productId: 'workbench_30d', price: 2900, days: 30 };
const PERIOD = { passStartsAt: '2026-10-01T00:00:00.000Z', passEndsAt: '2026-10-31T00:00:00.000Z' };
const FINAL_TERMS = {
  version: 'pay-2026-10-final',
  final: true,
  title: '付费服务协议',
  text: '购买批量工具 30 天。使用期从付款成功起算，到期不再扣款。',
};
// 服务端商品接口带回的"能开哪种发票"（YOUJU_PAY_INVOICE_TYPE；真实的一句等创始人确认，D-070）
const INVOICE = '可开电子普通发票（测试文字）';
const plain = (value) => JSON.parse(JSON.stringify(value));
const flush = async () => { await new Promise(setImmediate); await new Promise(setImmediate); };
const shown = (file, data) => textOf(render(file, data));
const payButtons = (data) => findAll(render('pages/pass/index.wxml', data), tapsTo('buy'));

function load(file, deps, globals = {}, options = {}) {
  let page; const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(root, file), 'utf8'), {
    module, Date, Math, Promise, Uint8Array, setTimeout, clearTimeout,
    // 支付的文案、金额和轮询用真模块；其余依赖由本文件给出
    require(key) {
      if (!Object.hasOwn(deps, key) && key === '../../utils/pay.js') return require('../utils/pay.js');
      assert.ok(Object.hasOwn(deps, key), `${file}: ${key}`);
      return deps[key];
    },
    Page(def) { page = def; }, ...globals,
  }, { filename: file });
  return page ? {
    ...page,
    data: plain(page.data),
    // 订单页用 form.title 这种路径写表单，要按小程序的方式落到嵌套字段
    // byReference：按引用落数据、不复制（小程序框架最不利的情形），用来查共用对象被改的问题
    setData(value) {
      const data = options.byReference ? value : plain(value);
      for (const [key, item] of Object.entries(data)) {
        if (!key.includes('.')) { this.data[key] = item; continue; }
        const parts = key.split('.');
        let cursor = this.data;
        for (let i = 0; i < parts.length - 1; i += 1) {
          if (cursor[parts[i]] == null || typeof cursor[parts[i]] !== 'object') cursor[parts[i]] = {};
          cursor = cursor[parts[i]];
        }
        cursor[parts.at(-1)] = item;
      }
    },
  } : module.exports;
}

function makeClock() {
  const queued = [];
  let serial = 0;
  return {
    queued,
    setTimeout(fn, ms) { const id = ++serial; queued.push({ id, fn, ms: Number(ms) || 0 }); return id; },
    clearTimeout(id) {
      const index = queued.findIndex((item) => item.id === id);
      if (index >= 0) queued.splice(index, 1);
    },
  };
}

// 购买页的确认计时是几十秒；拨最快的那个会把 2 秒重试打成死循环，只拨最长的截止计时
function firePollTimeout(clock) {
  assert.ok(clock.queued.length > 0, '确认计时器没有启动');
  const due = clock.queued.reduce((best, item) => (item.ms > best.ms ? item : best));
  assert.ok(due.ms >= 50000, `最长的确认计时器只有 ${due.ms} 毫秒`);
  clock.clearTimeout(due.id);
  due.fn();
}

function passPage(options = {}) {
  const calls = []; const urls = []; const toasts = []; const storage = new Map(); const clock = makeClock();
  const api = {
    payProducts: options.payProducts || (() => Promise.resolve({ items: [PRODUCT], invoiceType: INVOICE })),
    advisorStatus: () => Promise.resolve({ pass: { active: false } }),
    createPayOrder(body) {
      calls.push(['create', plain(body)]);
      if (options.create) return options.create(body);
      return Promise.resolve({ orderId: 41, signData: 'sign-data-1', paySig: 'pay-sig-1', signature: 'signature-1', mode: 'short_series_goods' });
    },
    payOrder(id) {
      calls.push(['order', id]);
      return options.order ? options.order(id) : Promise.resolve({ orderId: id, status: '待支付' });
    },
  };
  const wx = {
    getStorageSync: (key) => storage.get(key),
    setStorageSync: (key, value) => storage.set(key, plain(value)),
    removeStorageSync: (key) => storage.delete(key),
    navigateTo: ({ url }) => urls.push(url),
    showToast: (toast) => toasts.push(toast),
    login: (payload) => payload.success({ code: 'wx-code-1' }),
    requestVirtualPayment(payload) {
      calls.push(['virtual', { signData: payload.signData, paySig: payload.paySig, signature: payload.signature, mode: payload.mode }]);
      if (options.onPay) options.onPay(payload);
    },
  };
  const page = load('pages/pass/index.js', {
    '../../utils/api.js': api,
    '../../utils/consent.js': { loginOpen: () => true },
    '../../utils/data-tools.js': { toolConsent: () => Promise.resolve(options.agreed !== false) },
    '../../content/pay-terms.js': options.terms || FINAL_TERMS,
  }, { wx, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });
  page.onShow();
  return { page, calls, urls, toasts, storage, clock };
}

function orderRecord(partial) {
  return { amount: 2900, status: '已完成', canRefund: false, canInvoice: false, ...PERIOD, ...partial };
}

function orderCard(data, id) {
  return findAll(render('pages/orders/index.wxml', data), (node) => String(node.attrs.class || '').split(/\s+/).includes('order'))
    .find((node) => textOf(node).includes(`订单号 ${id}`));
}

function ordersPage(items, options = {}) {
  const calls = []; let modal = null;
  const api = {
    payOrders: () => { calls.push(['list']); return Promise.resolve({ items }); },
    refundPayOrder: (id) => { calls.push(['refund', id]); return Promise.resolve({}); },
    requestInvoice: (id, body) => {
      calls.push(['invoice', id, plain(body)]);
      if (options.invoice) return options.invoice(id, body);
      return Promise.resolve({
        ...orderRecord({ orderId: id, canInvoice: true }),
        invoice: { status: '待开', canEdit: true, titleType: body.title_type, title: body.title, taxId: body.tax_id, email: body.email },
      });
    },
  };
  const page = load('pages/orders/index.js', {
    '../../utils/api.js': api,
    '../../utils/consent.js': { loginOpen: () => true },
  }, { wx: { showModal: (payload) => { modal = payload; }, navigateTo() {}, showToast() {} } }, { byReference: options.byReference });
  page.onShow();
  return { page, calls, modal: () => modal };
}

function dataLogic(api) {
  return load('utils/data-tools.js', {
    './api.js': api,
    './regions.js': require('../utils/regions.js'),
    './advisor.js': helpers,
    '../content/advisor-terms.js': advisorTerms,
    './boost.js': require('../utils/boost.js'),
  });
}

function caps(pay) {
  return { data_tools: { enabled: true, features: ['compare', 'track'] }, ai_advisor: { enabled: false, features: [] }, pay };
}
function quota(used) {
  return { enabled: true, pass: { active: false }, quota: { compare: { used, limit: 3, maxRows: 50 }, track: { used, limit: 3, maxRows: 50 } } };
}

function toolForm(overrides = {}) {
  const seen = { products: 0 }; const urls = []; const storage = new Map();
  const api = Object.assign({
    advisorCapabilities: () => Promise.resolve(caps({ enabled: false })),
    advisorStatus: () => Promise.resolve(quota(0)),
    me: () => Promise.resolve({ advisorAgreed: { version: advisorTerms.version }, taskOwner: OWNER }),
    advisorPreview: () => Promise.resolve({ rowCount: 1 }),
    createAdvisorTask: () => Promise.resolve({ id: 1 }),
    payProducts: () => Promise.resolve({ items: [] }),
  }, overrides.api || {});
  const innerProducts = api.payProducts;
  api.payProducts = (...args) => { seen.products += 1; return innerProducts(...args); };
  const wx = {
    getStorageSync: (key) => storage.get(key), setStorageSync: (key, value) => storage.set(key, plain(value)), removeStorageSync: (key) => storage.delete(key),
    setNavigationBarTitle() {}, navigateTo: ({ url }) => urls.push(url), redirectTo() {}, showToast() {},
  };
  const page = load('pages/advisor-form/index.js', {
    '../../utils/api.js': api,
    '../../utils/data-idempotency.js': idempotency,
    '../../utils/advisor.js': helpers,
    '../../utils/data-tools.js': dataLogic(api),
  }, { wx });
  page.onLoad({ kind: 'compare' });
  page.onShow();
  return { page, urls, seen };
}

test('能力接口把服务端的 pay 原样带给页面（只认严格的 true），失败时按不在售处理，防止收费开放了却不出现开通卡片', async () => {
  // 用真实的 utils/api.js，模拟线上 wx.request 的返回（原来整理返回值时丢了 pay，页面测试替换了这个函数所以没测出来）
  async function capabilities(response) {
    const urls = [];
    // 每次请求都马上按同一个结果应答（失败只用不重试的 403、404；会重试的失败走的是同一个出错分支）
    const request = (options) => {
      urls.push(options.url);
      Promise.resolve().then(() => options.success(response));
    };
    const api = load('utils/api.js', {
      './config.js': { devBaseUrl: 'https://test.invalid' }, '../mock/data.js': {}, './mockSearch.js': {}, './format.js': {},
    }, { wx: { request, removeStorageSync() {}, getStorageSync() {}, setStorageSync() {} },
      // 请求都马上应答：15 秒超时计时器不用真的挂着（否则测试进程要白等 15 秒才结束）
      setTimeout: () => 0, clearTimeout() {} });
    const result = await api.advisorCapabilities({ refresh: true });
    assert.ok(urls.length >= 1 && urls.every((url) => /\/api\/v1\/advisor\/capabilities$/.test(url)), urls.join());
    return result;
  }
  const body = (pay, ai = false) => ({ statusCode: 200, data: { data_tools: { enabled: true, features: ['compare', 'track'] }, ai_advisor: { enabled: ai, features: [] }, pay } });
  assert.deepEqual(plain((await capabilities(body({ enabled: true, hasOrders: false }))).pay), { enabled: true, hasOrders: false });
  assert.deepEqual(plain((await capabilities(body({ enabled: false, hasOrders: true }, true))).pay), { enabled: false, hasOrders: true });
  assert.deepEqual(plain((await capabilities(body({ enabled: 'true', hasOrders: 1 }))).pay), { enabled: false, hasOrders: false });
  assert.deepEqual(plain((await capabilities(body(undefined))).pay), { enabled: false, hasOrders: false });
  assert.deepEqual(plain((await capabilities({ statusCode: 404, data: { detail: '没有找到接口' } })).pay), { enabled: false, hasOrders: false });
  assert.deepEqual(plain((await capabilities({ statusCode: 403, data: { detail: '请先同意' } })).pay), { enabled: false, hasOrders: false });
});

test('购买页在商品 404 或空列表时只显示未开放、没有付款按钮，防止关掉售卖后还能付款', async () => {
  for (const payProducts of [
    () => Promise.reject(Object.assign(new Error('开关关闭'), { status: 404 })),
    () => Promise.resolve({ items: [] }),
  ]) {
    const h = passPage({ payProducts });
    await flush();
    assert.equal(h.page.data.loading, false);
    assert.equal(h.page.data.product, null);
    assert.equal(h.page.data.error, '');
    const text = shown('pages/pass/index.wxml', h.page.data);
    assert.match(text, /付费服务暂未开放/);
    assert.doesNotMatch(text, /开关关闭/);
    assert.equal(payButtons(h.page.data).length, 0);
    assert.equal(h.calls.length, 0);
  }
});

test('购买页协议未定稿时没有付款按钮，调用 buy 也不下单，防止用未定稿协议收款', async () => {
  assert.equal(payTerms.final, false);
  const h = passPage({ terms: payTerms });
  await flush();
  assert.equal(h.page.data.termsFinal, false);
  assert.equal(h.page.data.product.productId, 'workbench_30d');
  assert.equal(payButtons(h.page.data).length, 0);
  assert.match(shown('pages/pass/index.wxml', h.page.data), /付费服务暂未开放/);
  h.page.buy();
  assert.equal(h.page.data.message, '付费服务暂未开放');
  assert.equal(h.calls.length, 0);
});

test('购买页在沙箱下协议没定稿也能联调付款，顶部写明不扣真钱；现网没定稿仍不能付款，防止沙箱联调被协议卡住或现网误收款', async () => {
  const sandbox = passPage({ terms: payTerms, payProducts: () => Promise.resolve({ items: [PRODUCT], sandbox: true }), onPay: (payload) => payload.fail({ errCode: -2 }) });
  await flush();
  assert.equal(sandbox.page.data.termsFinal, false);
  assert.equal(sandbox.page.data.sandbox, true);
  assert.match(shown('pages/pass/index.wxml', sandbox.page.data), /沙箱测试：只对测试人员开放，付款不扣真钱/);
  assert.equal(payButtons(sandbox.page.data).length, 1);
  assert.equal(textOf(payButtons(sandbox.page.data)[0]).trim(), '沙箱支付 29 元（30 天，不扣真钱）');
  sandbox.page.check({ detail: { value: ['agree'] } });
  sandbox.page.buy();
  await flush();
  assert.deepEqual(sandbox.calls.find((call) => call[0] === 'create')[1], {
    product_id: 'workbench_30d', code: 'wx-code-1', terms_version: payTerms.version, invoice_type: '',
  });
  // sandbox 必须是布尔 true：其他值（字符串、缺省）都按现网处理
  for (const res of [{ items: [PRODUCT], sandbox: 'true' }, { items: [PRODUCT], sandbox: false }]) {
    const live = passPage({ terms: payTerms, payProducts: () => Promise.resolve(res) });
    await flush();
    assert.equal(live.page.data.sandbox, false);
    assert.equal(payButtons(live.page.data).length, 0);
    assert.doesNotMatch(shown('pages/pass/index.wxml', live.page.data), /沙箱测试/);
    live.page.buy();
    assert.equal(live.calls.length, 0);
  }
});

test('购买页未勾选协议或未同意工具说明时都不下单，防止没同意就创建支付单', async () => {
  const h = passPage({ agreed: false });
  await flush();
  assert.equal(h.page.data.termsFinal, true);
  assert.equal(payButtons(h.page.data).length, 1);
  assert.equal(textOf(payButtons(h.page.data)[0]).trim(), '支付 29 元（30 天）');
  h.page.buy();
  assert.equal(h.page.data.message, '请先阅读并同意《付费服务协议》');
  assert.equal(h.toasts.at(-1).title, '请先阅读并同意《付费服务协议》');
  assert.equal(h.calls.length, 0);
  h.page.check({ detail: { value: ['agree'] } });
  h.page.buy();
  await flush();
  assert.deepEqual(h.urls, ['/pages/advisor-terms/index']);
  assert.equal(h.calls.length, 0);
});

test('购买页勾选后下单带上商品、登录码和协议版本，支付参数齐全，轮询到已完成才标开通，防止没核对服务端就当付款成功', async () => {
  const h = passPage({
    onPay: (payload) => payload.success(),
    order: () => Promise.resolve({
      orderId: 41, status: '已完成', amount: 2900,
      passStartsAt: '2026-10-16T00:00:00.000Z', passEndsAt: '2026-11-15T00:00:00.000Z',
    }),
  });
  await flush();
  h.page.check({ detail: { value: ['agree'] } });
  h.page.buy();
  await flush();
  assert.deepEqual(h.calls.find((call) => call[0] === 'create')[1], {
    product_id: 'workbench_30d', code: 'wx-code-1', terms_version: FINAL_TERMS.version, invoice_type: INVOICE,
  });
  assert.deepEqual(h.calls.find((call) => call[0] === 'virtual')[1], {
    signData: 'sign-data-1', paySig: 'pay-sig-1', signature: 'signature-1', mode: 'short_series_goods',
  });
  assert.deepEqual(h.calls.filter((call) => call[0] === 'order').map((call) => call[1]), [41]);
  assert.equal(h.page.data.confirmed, true);
  assert.match(h.page.data.message, /已开通，使用期到/);
  assert.equal(payButtons(h.page.data).length, 0);
  assert.equal(findAll(render('pages/pass/index.wxml', h.page.data), tapsTo('tools')).length, 1);
});

test('购买页取消支付不记未完成单；其他失败记下来，确认超时后再次购买只复查上一笔，防止取消被当成已扣款以及重复下单', async () => {
  const cancelled = passPage({ onPay: (payload) => payload.fail({ errCode: -2, errMsg: 'requestVirtualPayment:fail cancel' }) });
  await flush();
  cancelled.page.check({ detail: { value: ['agree'] } });
  cancelled.page.buy();
  await flush();
  assert.equal(cancelled.page.data.message, '已取消支付');
  assert.equal(cancelled.toasts.at(-1).title, '已取消支付');
  assert.equal(cancelled.storage.has('pay.unsettled'), false);
  assert.equal(cancelled.page.unsettledId || null, null);

  const failed = passPage({ onPay: (payload) => payload.fail({ errCode: -1, errMsg: 'requestVirtualPayment:fail' }) });
  await flush();
  failed.page.check({ detail: { value: ['agree'] } });
  failed.page.buy();
  await flush();
  assert.equal(failed.calls.filter((call) => call[0] === 'create').length, 1);
  assert.equal(failed.storage.get('pay.unsettled').orderId, 41);
  assert.deepEqual(failed.calls.filter((call) => call[0] === 'order').map((call) => call[1]), [41]);
  // 这一轮还在确认时 busy 为真，再点会被忽略；超时后仍留着 pay.unsettled，再次购买只查这一笔
  firePollTimeout(failed.clock);
  assert.equal(failed.page.data.busy, false);
  assert.equal(failed.storage.get('pay.unsettled').orderId, 41);
  failed.page.buy();
  await flush();
  assert.equal(failed.calls.filter((call) => call[0] === 'create').length, 1);
  assert.deepEqual(failed.calls.filter((call) => call[0] === 'order').map((call) => call[1]), [41, 41]);
});

test('购买页付款后切到后台超过一分钟，回来时再查一次服务端，查到已完成就显示开通，防止付了款却停在超时提示', async () => {
  let payment = null;
  const h = passPage({
    onPay: (payload) => { payment = payload; },
    order: () => Promise.resolve({ orderId: 41, status: '已完成', amount: 2900, ...PERIOD }),
  });
  await flush();
  h.page.check({ detail: { value: ['agree'] } });
  h.page.buy();
  await flush();
  h.page.onHide();
  payment.success();
  assert.equal(h.page.payResume, true);
  // 在后台待了一分多钟：原来的确认截止时间已经过了
  h.page.payDeadline = Date.now() - 1000;
  h.page.onShow();
  await flush();
  assert.equal(h.page.data.confirmed, true);
  assert.match(h.page.data.message, /已开通，使用期到/);
});

test('购买页下单时服务端说上一笔已经付款（409），显示服务端文字、重新载入使用期，不调起支付，防止又付一笔', async () => {
  let products = 0;
  const said = '上一笔已经付款，正在为你开通，可到“我的订单”查看';
  const h = passPage({
    payProducts: () => { products += 1; return Promise.resolve({ items: [PRODUCT], invoiceType: INVOICE }); },
    create: () => Promise.reject(Object.assign(new Error(said), { status: 409 })),
  });
  await flush();
  assert.equal(products, 1);
  h.page.check({ detail: { value: ['agree'] } });
  h.page.buy();
  await flush();
  assert.equal(h.page.data.message, said);
  assert.equal(h.page.data.payError, true);
  assert.equal(h.page.data.busy, false);
  assert.equal(products, 2);
  assert.equal(h.calls.filter((call) => call[0] === 'virtual').length, 0);
});

test('订单页换一张订单开票时表单是空的，不带上一张没提交的内容，防止把别的订单的抬头提交上去', async () => {
  const h = ordersPage([orderRecord({ orderId: 41, canInvoice: true }), orderRecord({ orderId: 42, canInvoice: true })], { byReference: true });
  await flush();
  h.page.invoice({ currentTarget: { dataset: { id: 41 } } });
  h.page.titleType({ detail: { value: 'company' } });
  h.page.field({ currentTarget: { dataset: { field: 'title' } }, detail: { value: '甲公司' } });
  h.page.cancelInvoice();
  h.page.invoice({ currentTarget: { dataset: { id: 42 } } });
  assert.equal(h.page.data.invoiceFor, 42);
  assert.deepEqual(plain(h.page.data.form), { titleType: 'personal', title: '', taxId: '', email: '' });
});

test('订单页展示金额、使用期和订单号；可退款要确认后才退并刷新，不能退时写明原因，防止误退和没有原因', async () => {
  const h = ordersPage([
    orderRecord({ orderId: 41, canRefund: true }),
    orderRecord({ orderId: 42, refundUnavailableReason: '已超过 7 天' }),
  ]);
  await flush();
  const text = shown('pages/orders/index.wxml', h.page.data);
  assert.match(text, /29\.00 元/);
  assert.match(text, /使用期 10-01 至 10-31/);
  assert.match(text, /订单号 41/);
  assert.match(textOf(orderCard(h.page.data, 42)), /不能自助退款：已超过 7 天/);
  const refunds = findAll(render('pages/orders/index.wxml', h.page.data), tapsTo('refund'));
  assert.equal(refunds.length, 1);
  assert.equal(String(refunds[0].attrs['data-id']), '41');
  h.page.refund({ currentTarget: { dataset: { id: refunds[0].attrs['data-id'] } } });
  assert.equal(h.modal().title, '申请退款');
  assert.equal(h.modal().confirmText, '申请退款');
  assert.equal(h.calls.filter((call) => call[0] === 'refund').length, 0);
  h.modal().success({ confirm: true });
  await flush();
  assert.deepEqual(h.calls.filter((call) => call[0] === 'refund'), [['refund', 41]]);
  assert.equal(h.calls.filter((call) => call[0] === 'list').length, 2);
});

test('订单页申请发票时税号转成大写、抬头去掉空白，成功后显示待开并可修改，防止税号和抬头提交错误', async () => {
  const h = ordersPage([orderRecord({ orderId: 41, canInvoice: true })]);
  await flush();
  const button = findAll(render('pages/orders/index.wxml', h.page.data), tapsTo('invoice'))[0];
  assert.equal(textOf(button), '申请发票');
  h.page.invoice({ currentTarget: { dataset: { id: 41 } } });
  h.page.titleType({ detail: { value: 'company' } });
  h.page.field({ currentTarget: { dataset: { field: 'title' } }, detail: { value: '  示例科技有限公司  ' } });
  h.page.field({ currentTarget: { dataset: { field: 'taxId' } }, detail: { value: '91370000ma1234567x' } });
  h.page.field({ currentTarget: { dataset: { field: 'email' } }, detail: { value: 'finance@example.com' } });
  h.page.sendInvoice();
  await flush();
  assert.deepEqual(h.calls.find((call) => call[0] === 'invoice'), ['invoice', 41, {
    title_type: 'company', title: '示例科技有限公司', tax_id: '91370000MA1234567X', email: 'finance@example.com',
  }]);
  const text = shown('pages/orders/index.wxml', h.page.data);
  assert.match(text, /发票：待开/);
  assert.equal(textOf(findAll(render('pages/orders/index.wxml', h.page.data), tapsTo('invoice'))[0]), '修改发票信息');
});

test('订单页缺税号或邮箱不对时本机拦住，409 显示服务端文字，已开或不能开票时没有按钮，防止无效申请和改已开发票', async () => {
  const h = ordersPage([
    orderRecord({ orderId: 41, canInvoice: true }),
    orderRecord({ orderId: 42, canInvoice: true, invoice: { status: '已开', canEdit: false, titleType: 'personal', title: '张三', email: 'a@b.co' } }),
    orderRecord({ orderId: 43, status: '已退款', canInvoice: false, refundUnavailableReason: '已经退款' }),
    orderRecord({ orderId: 44, canRefund: true, canInvoice: true, invoice: { status: '不开', canEdit: false, titleType: 'personal', title: '张三', email: 'a@b.co', note: '抬头和付款人不一致' } }),
  ], { invoice: () => Promise.reject(Object.assign(new Error('这张发票正在开具，不能修改'), { status: 409 })) });
  await flush();
  // 能退款、发票已定为不开：只有退款按钮，写明不开的原因
  assert.equal(findAll(orderCard(h.page.data, 44), tapsTo('refund')).length, 1);
  assert.equal(findAll(orderCard(h.page.data, 44), tapsTo('invoice')).length, 0);
  assert.match(textOf(orderCard(h.page.data, 44)), /发票：不开.*抬头和付款人不一致/);
  assert.equal(textOf(findAll(orderCard(h.page.data, 41), tapsTo('invoice'))[0]), '申请发票');
  assert.match(textOf(orderCard(h.page.data, 42)), /发票：已开/);
  assert.equal(findAll(orderCard(h.page.data, 42), tapsTo('invoice')).length, 0);
  assert.equal(findAll(orderCard(h.page.data, 43), tapsTo('invoice')).length, 0);
  h.page.invoice({ currentTarget: { dataset: { id: 41 } } });
  h.page.titleType({ detail: { value: 'company' } });
  h.page.field({ currentTarget: { dataset: { field: 'title' } }, detail: { value: '示例公司' } });
  h.page.field({ currentTarget: { dataset: { field: 'taxId' } }, detail: { value: '' } });
  h.page.field({ currentTarget: { dataset: { field: 'email' } }, detail: { value: 'finance@example.com' } });
  h.page.sendInvoice();
  assert.equal(h.page.data.formError, '单位抬头要填税号（统一社会信用代码）');
  assert.equal(h.calls.filter((call) => call[0] === 'invoice').length, 0);
  h.page.field({ currentTarget: { dataset: { field: 'taxId' } }, detail: { value: '91370000MA1234567X' } });
  h.page.field({ currentTarget: { dataset: { field: 'email' } }, detail: { value: 'nope' } });
  h.page.sendInvoice();
  assert.equal(h.page.data.formError, '请填写能收发票的邮箱');
  assert.match(shown('pages/orders/index.wxml', h.page.data), /请填写能收发票的邮箱/);
  assert.equal(h.calls.filter((call) => call[0] === 'invoice').length, 0);
  h.page.field({ currentTarget: { dataset: { field: 'email' } }, detail: { value: 'finance@example.com' } });
  h.page.sendInvoice();
  await flush();
  assert.equal(h.calls.filter((call) => call[0] === 'invoice').length, 1);
  assert.equal(h.page.data.formError, '这张发票正在开具，不能修改');
  assert.match(shown('pages/orders/index.wxml', h.page.data), /这张发票正在开具，不能修改/);
});

test('工具表单在支付关闭时不查商品也不出现批量工具价格，免费用完只提示不需要购买，防止关着支付还引导购买', async () => {
  const h = toolForm({ api: {
    advisorCapabilities: () => Promise.resolve(caps({ enabled: false })),
    advisorStatus: () => Promise.resolve(quota(3)),
  } });
  await flush();
  assert.equal(h.seen.products, 0);
  assert.equal(h.page.data.offer, null);
  const text = shown('pages/advisor-form/index.wxml', h.page.data);
  assert.match(text, /不需要购买/);
  assert.doesNotMatch(text, /客户工作台|批量工具：|元/);
});

test('工具表单在售时给出两档价格卡片并可进入购买页，商品接口失败则不推销且免费功能仍可用，防止接口失败弄坏免费工具', async () => {
  const selling = toolForm({ api: {
    advisorCapabilities: () => Promise.resolve(caps({ enabled: true })),
    advisorStatus: () => Promise.resolve(quota(3)),
    payProducts: () => Promise.resolve({ items: [{ productId: 'workbench_90d', price: 7900, days: 90 }, { productId: 'workbench_30d', price: 2900, days: 30 }] }),
  } });
  await flush();
  assert.equal(selling.seen.products, 1);
  assert.deepEqual(plain(selling.page.data.offer), { line: '30 天 29 元、90 天 79 元', renew: null });
  const tree = render('pages/advisor-form/index.wxml', selling.page.data);
  assert.match(textOf(tree), /批量工具：30 天 29 元、90 天 79 元，不限次数，每次 500 家以内。一次付清，到期不再扣钱。/);
  assert.doesNotMatch(textOf(tree), /客户工作台/);
  assert.equal(findAll(tree, tapsTo('buyPass')).length, 1);
  selling.page.buyPass();
  assert.deepEqual(selling.urls, ['/pages/pass/index']);

  const failed = toolForm({ api: {
    advisorCapabilities: () => Promise.resolve(caps({ enabled: true })),
    advisorStatus: () => Promise.resolve(quota(0)),
    payProducts: () => Promise.reject(new Error('商品列表失败')),
  } });
  await flush();
  assert.equal(failed.seen.products, 1);
  assert.equal(failed.page.data.offer, null);
  assert.equal(failed.page.data.enabled, true);
  const text = shown('pages/advisor-form/index.wxml', failed.page.data);
  assert.match(text, /提交任务/);
  assert.doesNotMatch(text, /客户工作台|批量工具：/);
  failed.page.input({ detail: { value: '合成企业甲' } });
  failed.page.preview();
  await flush();
  assert.equal(failed.page.data.previewed, true);
});

test('我的页只在支付开放或已有订单时显示我的订单并进入订单页，防止没有订单时放一个空入口', async () => {
  const urls = []; const pay = { enabled: false, hasOrders: false }; let meCalls = 0;
  const page = load('pages/me/index.js', {
    '../../content/legal.js': { final: true, version: '2026-10' },
    '../../utils/api.js': { isLive: () => true, me: () => { meCalls += 1; return Promise.resolve({ pay: { enabled: pay.enabled, hasOrders: pay.hasOrders }, follows: 0, subscriptions: {} }); } },
    '../../utils/consent.js': { loginOpen: () => true, consentEpoch: () => 0, applyServer: () => true, withdraw: () => Promise.resolve(false), markAgreed() {}, openPrivacy() {}, CONSENT_PAGE: '', agreeFailed() {}, featureNames: () => '' },
    '../../utils/subscribe.js': { remindOpen: () => false },
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve(null), notCoveredNames: () => '' },
  }, { wx: { navigateTo: ({ url }) => urls.push(url), switchTab() {}, showToast() {} } });
  const row = () => findAll(render('pages/me/index.wxml', page.data), (node) => node.attrs['data-url'] === '/pages/orders/index');
  page.refresh();
  await flush();
  assert.equal(meCalls, 1);
  assert.equal(page.data.ordersEntry, false);
  assert.equal(row().length, 0);
  assert.doesNotMatch(shown('pages/me/index.wxml', page.data), /我的订单/);
  pay.enabled = true;
  page.refresh();
  await flush();
  assert.equal(page.data.ordersEntry, true);
  assert.match(textOf(row()[0]), /我的订单/);
  page.go({ currentTarget: { dataset: { url: row()[0].attrs['data-url'] } } });
  assert.deepEqual(urls, ['/pages/orders/index']);
  pay.enabled = false;
  pay.hasOrders = true;
  page.refresh();
  await flush();
  assert.equal(page.data.ordersEntry, true);
  assert.equal(row().length, 1);
});

test('反馈页支付问题带上纯数字订单号，订单号不纯时不加订单号，可写字数扣掉前缀，防止对不上订单或前缀挤占字数', async () => {
  async function send(orderId) {
    const sent = []; const clock = makeClock();
    const page = load('pages/feedback/index.js', {
      '../../utils/api.js': { feedback: (_id, content) => { sent.push(content); return Promise.resolve({}); } },
      '../../utils/consent.js': { loginOpen: () => true, ensureConsent: () => Promise.resolve(true) },
    }, { wx: { setNavigationBarTitle() {}, showToast() {}, navigateBack() {}, switchTab() {} }, setTimeout: clock.setTimeout, clearTimeout: clock.clearTimeout });
    page.onLoad({ type: 'pay', orderId });
    page.onInput({ detail: { value: '还没开通' } });
    page.submit();
    await flush();
    return { page, sent };
  }
  const numbered = await send('41');
  assert.equal(numbered.page.data.prefix, '支付问题（订单 41）：');
  assert.equal(numbered.page.data.max, 500 - '支付问题（订单 41）：'.length);
  assert.equal(findAll(render('pages/feedback/index.wxml', numbered.page.data), (node) => node.tag === 'textarea')[0].attrs.maxlength, numbered.page.data.max);
  assert.equal(numbered.sent[0], '支付问题（订单 41）：还没开通');
  const plainId = await send('41a');
  assert.equal(plainId.page.data.prefix, '支付问题：');
  assert.equal(plainId.page.data.max, 500 - '支付问题：'.length);
  assert.equal(plainId.sent[0], '支付问题：还没开通');
  assert.equal(plainId.sent[0].includes('订单'), false);
  // 提示只在真的带上了订单号时说"已自动带上"；没带上时请用户自己写（隐私保护指引一·6 的说法，Codex 10/19）
  assert.match(shown('pages/feedback/index.wxml', numbered.page.data), /订单号已自动带上/);
  const untagged = shown('pages/feedback/index.wxml', plainId.page.data);
  assert.doesNotMatch(untagged, /已自动带上/);
  assert.match(untagged, /请写上订单号（在“我的订单”里能看到）/);
  assert.doesNotMatch(shown('pages/feedback/index.wxml', (await send(undefined)).page.data), /已自动带上/);
});

test('协议页 type=pay 只显示一份文件，标题用付费协议的 title，防止打开成用户协议或隐私保护指引', () => {
  const titles = [];
  const page = load('pages/agreement/index.js', {
    '../../content/legal.js': { final: true, status: '', disclaimer: '', version: '2026-10', user: { title: '用户协议', sections: [] }, privacy: { title: '隐私保护指引', sections: [] } },
    '../../content/pay-terms.js': payTerms,
    '../../utils/api.js': {},
    '../../utils/consent.js': { loginOpen: () => true, consentOpen: () => false, markAgreed() {}, agreeFailed() {}, featureNames: () => '' },
  }, { wx: { setNavigationBarTitle: ({ title }) => titles.push(title) } });
  page.onLoad({ type: 'pay' });
  assert.equal(page.data.docs.length, 1);
  assert.equal(page.data.docs[0].title, payTerms.title);
  assert.deepEqual(titles, [payTerms.title]);
  const heads = findAll(render('pages/agreement/index.wxml', page.data), (node) => String(node.attrs.class || '').split(/\s+/).includes('doc-title'));
  assert.equal(heads.length, 1);
  assert.equal(textOf(heads[0]), `《${payTerms.title}》`);
});

// ---- D-070 开卖那批：两档、续买多送、付款前要看得到的、到期提示、订单逐笔、我的 → 批量工具、纠错规则 ----

const TIERS = [{ productId: 'workbench_30d', price: 2900, days: 30 }, { productId: 'workbench_90d', price: 7900, days: 90 }];
const pay = require('../utils/pay.js');

test('购买页两档并排，默认 30 天，90 天写折合每 30 天约 26 元、不写原价和立省；选 90 天后按 90 天下单，防止价格比较违规和下错档', async () => {
  const h = passPage({ payProducts: () => Promise.resolve({ items: [TIERS[1], TIERS[0]], invoiceType: INVOICE }), onPay: (payload) => payload.fail({ errCode: -2 }) });
  await flush();
  assert.deepEqual(plain(h.page.data.tiers).map((tier) => [tier.days, tier.price, tier.per30]), [[30, '29', ''], [90, '79', '折合每 30 天约 26 元']]);
  assert.equal(h.page.data.selected, 0);
  const text = shown('pages/pass/index.wxml', h.page.data);
  assert.match(text, /批量工具/);
  assert.match(text, /折合每 30 天约 26 元/);
  assert.doesNotMatch(text, /原价|立省|省 \d|推荐|限时|名额/);
  assert.equal(textOf(payButtons(h.page.data)[0]).trim(), '支付 29 元（30 天）');
  const tiers = findAll(render('pages/pass/index.wxml', h.page.data), tapsTo('pick'));
  assert.equal(tiers.length, 2);
  h.page.pick({ currentTarget: { dataset: { index: tiers[1].attrs['data-index'] } } });
  assert.equal(textOf(payButtons(h.page.data)[0]).trim(), '支付 79 元（90 天）');
  // 选中的那一档在页面上标出来（无障碍也读得到）
  const picked = findAll(render('pages/pass/index.wxml', h.page.data), tapsTo('pick')).map((node) => String(node.attrs['aria-checked']));
  assert.deepEqual(picked, ['false', 'true']);
  h.page.check({ detail: { value: ['agree'] } });
  h.page.buy();
  await flush();
  assert.equal(h.calls.find((call) => call[0] === 'create')[1].product_id, 'workbench_90d');
  // 带上页面写的发票类型，服务端核对一致才下单
  assert.equal(h.calls.find((call) => call[0] === 'create')[1].invoice_type, INVOICE);
  // 付款中不能换档
  h.page.setData({ busy: true });
  h.page.pick({ currentTarget: { dataset: { index: 0 } } });
  assert.equal(h.page.data.product.days, 90);
});

test('购买页第一句写给谁用，付款前写全收录范围、只限本人、苹果退款、发票和到期后结果，防止只管本企业的人被带去付款、付了才知道限制', async () => {
  const h = passPage({ payProducts: () => Promise.resolve({ items: TIERS, invoiceType: INVOICE }) });
  await flush();
  const text = shown('pages/pass/index.wxml', h.page.data);
  assert.match(text, /给要一次比对很多家客户的顾问用；查通知、查单家企业一直免费/);
  assert.match(text, /只限本人的微信使用/);
  assert.match(text, /苹果手机付款的，只能向 App Store 申请退款/);
  assert.match(text, /发票：可开电子普通发票（测试文字）/);
  assert.match(text, /到期后，已经生成的结果仍可查看、导出/);
  assert.match(text, /按剩余天数退款/);
  assert.match(text, /停止服务（会提前至少 30 天公告）/);
  assert.match(text, /没用完的体验时间折成天数加在后面/);
  assert.match(text, /使用期从开通时起算（一般付款后几秒内就开通）/);
  assert.match(text, /开通时体验结束/);
  assert.doesNotMatch(text, /马上开通|马上生效/);
  // 第一句在价格前面
  assert.ok(text.indexOf('给要一次比对很多家客户的顾问用') < text.indexOf('29'));
  const links = findAll(render('pages/pass/index.wxml', h.page.data), tapsTo('coverage'));
  assert.equal(links.length, 1);
  assert.match(textOf(links[0]), /查看收录范围/);
  h.page.coverage();
  assert.deepEqual(h.urls, ['/pages/about/index']);
});

test('购买页的发票类型以服务端为准，服务端没给时现网没有付款按钮，沙箱照常联调，防止带着发票占位上线收费', async () => {
  for (const invoiceType of [undefined, null, '', '  ', '发票类型待创始人确认', '【发票类型】']) {
    const live = passPage({ payProducts: () => Promise.resolve({ items: TIERS, invoiceType }) });
    await flush();
    assert.equal(live.page.data.invoiceText, '');
    assert.equal(payButtons(live.page.data).length, 0);
    assert.match(shown('pages/pass/index.wxml', live.page.data), /能开哪种发票还没确定/);
    live.page.buy();
    assert.equal(live.calls.length, 0);
  }
  const sandbox = passPage({ payProducts: () => Promise.resolve({ items: TIERS, sandbox: true }) });
  await flush();
  assert.equal(payButtons(sandbox.page.data).length, 1);
  const ready = passPage({ payProducts: () => Promise.resolve({ items: TIERS, invoiceType: ` ${INVOICE} ` }) });
  await flush();
  assert.equal(ready.page.data.invoiceText, INVOICE);
  assert.equal(payButtons(ready.page.data).length, 1);
  // 协议文件里不另写一份发票类型（只认服务端那一句）
  assert.equal(Object.hasOwn(payTerms, 'invoice'), false);
});

test('续买规则只有一句，和价格在同一屏；现在付款符合时说一声，开关关着时不提，防止规则藏在别处或不在售时出现', async () => {
  const on = passPage({ payProducts: () => Promise.resolve({ items: TIERS, renewBonus: { days: 3, windowDays: 7, eligibleNow: true } }) });
  await flush();
  const hero = findAll(render('pages/pass/index.wxml', on.page.data), (node) => String(node.attrs.class || '').split(/\s+/).includes('pass-hero'))[0];
  assert.match(textOf(hero), /29.*79.*到期前后 7 天内续买，每次多送 3 天：现在付款就送/);
  const later = passPage({ payProducts: () => Promise.resolve({ items: TIERS, renewBonus: { days: 3, windowDays: 7, eligibleNow: false } }) });
  await flush();
  const text = shown('pages/pass/index.wxml', later.page.data);
  assert.match(text, /到期前后 7 天内续买，每次多送 3 天/);
  assert.doesNotMatch(text, /现在付款就送/);
  for (const renewBonus of [null, undefined, { days: '3', windowDays: 7 }, { days: 3, windowDays: 0 }]) {
    const off = passPage({ payProducts: () => Promise.resolve({ items: TIERS, renewBonus }) });
    await flush();
    assert.equal(off.page.data.renew, null);
    assert.doesNotMatch(shown('pages/pass/index.wxml', off.page.data), /续买，每次多送/);
  }
});

test('购买页写出现有使用期到哪天，3 天内到期改成到期提示，防止续买的人不知道还剩多久', async () => {
  const soon = new Date(Date.now() + 2 * 86400000).toISOString();
  const far = new Date(Date.now() + 20 * 86400000).toISOString();
  for (const [expiresAt, pattern] of [[soon, /批量工具 \d{1,2}月\d{1,2}日到期，到期不再扣钱；需要可以再买一次/], [far, /你的批量工具到 \d{4}-\d{2}-\d{2}；再买一次，接着往后顺延/]]) {
    const status = { pass: { active: true, expiresAt } };
    const api = { payProducts: () => Promise.resolve({ items: TIERS }), advisorStatus: () => Promise.resolve(status) };
    const page = load('pages/pass/index.js', {
      '../../utils/api.js': api, '../../utils/consent.js': { loginOpen: () => true },
      '../../utils/data-tools.js': { toolConsent: () => Promise.resolve(true) }, '../../content/pay-terms.js': FINAL_TERMS,
    }, { wx: { getStorageSync() {}, setStorageSync() {}, removeStorageSync() {}, navigateTo() {}, showToast() {} } });
    page.onShow();
    await flush();
    assert.match(shown('pages/pass/index.wxml', page.data), pattern);
  }
});

test('订单页按档位写名称，逐笔列出续买多送和体验折算，按剩余天数退的写退回多少，退款确认说明送的天数一起收回，防止看不出每笔订单得了什么', async () => {
  const h = ordersPage([
    orderRecord({ orderId: 51, name: '批量工具 90 天', days: 90, amount: 7900, canRefund: true,
      gifts: [{ kind: 'renewal', name: '续买多送', days: 3, startsAt: '2026-12-30T00:00:00Z', endsAt: '2027-01-02T00:00:00Z', revoked: false },
        { kind: 'trial', name: '加量体验折算', days: 2, startsAt: '2027-01-02T00:00:00Z', endsAt: '2027-01-04T00:00:00Z', revoked: false }] }),
    orderRecord({ orderId: 52, name: '批量工具 30 天', days: 30, status: '已退款', refundAmount: 1934,
      gifts: [{ kind: 'renewal', name: '续买多送', days: 3, revoked: true }] }),
    orderRecord({ orderId: 53, status: '已退款', refundAmount: 2900 }),
    orderRecord({ orderId: 54, status: '退款中', refundAmount: 1934 }),
    orderRecord({ orderId: 55, status: '需人工处理', refundAmount: 1934 }),
  ]);
  await flush();
  const first = textOf(orderCard(h.page.data, 51));
  assert.match(first, /批量工具 90 天/);
  assert.match(first, /79\.00 元/);
  assert.match(first, /续买多送 3 天：12-30 至 01-02/);
  assert.match(first, /加量体验折算 2 天：01-02 至 01-04/);
  const second = textOf(orderCard(h.page.data, 52));
  assert.match(second, /批量工具 30 天/);
  assert.match(second, /续买多送 3 天（已收回）/);
  assert.match(second, /已退回 19\.34 元（按剩余天数）/);
  // 已退款的不再写"不能自助退款"
  assert.doesNotMatch(second, /不能自助退款/);
  // 全额退的不另写退回多少（金额就是订单金额）；还在办理的不说"已退回"
  assert.doesNotMatch(textOf(orderCard(h.page.data, 53)), /退回/);
  assert.match(textOf(orderCard(h.page.data, 54)), /按剩余天数退 19\.34 元，处理中/);
  assert.doesNotMatch(textOf(orderCard(h.page.data, 54)), /已退回/);
  // 转了人工的不写"处理中"
  assert.match(textOf(orderCard(h.page.data, 55)), /按剩余天数退 19\.34 元，正在人工核对/);
  h.page.refund({ currentTarget: { dataset: { id: 51 } } });
  assert.equal(h.modal().content, '退款后，这张订单对应的 90 天使用期和续买多送的 3 天取消，79.00 元原路退回。确定吗？');
  assert.doesNotMatch(shown('pages/orders/index.wxml', h.page.data), /客户工作台/);
});

test('我的页只给在售时买过的人显示"批量工具"续买入口，进入购买页，防止停卖后还引导续买、没买过的人在我的页看到推销', async () => {
  const urls = []; const pay = { enabled: false, hasOrders: false };
  const page = load('pages/me/index.js', {
    '../../content/legal.js': { final: true, version: '2026-10' },
    '../../utils/api.js': { isLive: () => true, me: () => Promise.resolve({ pay: { ...pay }, follows: 0, subscriptions: {} }) },
    '../../utils/consent.js': { loginOpen: () => true, consentEpoch: () => 0, applyServer: () => true, withdraw: () => Promise.resolve(false), markAgreed() {}, openPrivacy() {}, CONSENT_PAGE: '', agreeFailed() {}, featureNames: () => '' },
    '../../utils/subscribe.js': { remindOpen: () => false },
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve(null), notCoveredNames: () => '' },
  }, { wx: { navigateTo: ({ url }) => urls.push(url), switchTab() {}, showToast() {} } });
  const row = () => findAll(render('pages/me/index.wxml', page.data), (node) => node.attrs['data-url'] === '/pages/pass/index');
  for (const [enabled, hasOrders, shownRow] of [[false, false, false], [true, false, false], [false, true, false], [true, true, true]]) {
    Object.assign(pay, { enabled, hasOrders });
    page.refresh();
    await flush();
    assert.equal(row().length, shownRow ? 1 : 0, JSON.stringify(pay));
  }
  assert.match(textOf(row()[0]), /批量工具/);
  page.go({ currentTarget: { dataset: { url: row()[0].attrs['data-url'] } } });
  assert.deepEqual(urls, ['/pages/pass/index']);
});

test('工具表单在使用期 3 天内到期且在售时写到期提示、续买规则和续买按钮；不在售或还早时不写，防止催买或停卖后还让人买', async () => {
  const pass = (days) => ({ enabled: true, pass: { active: true, expiresAt: new Date(Date.now() + days * 86400000).toISOString() },
    quota: { compare: { used: 0, limit: 3, maxRows: 500 }, track: { used: 0, limit: 3, maxRows: 500 } } });
  const products = () => Promise.resolve({ items: TIERS, renewBonus: { days: 3, windowDays: 7, eligibleNow: true } });
  const soon = toolForm({ api: { advisorCapabilities: () => Promise.resolve(caps({ enabled: true })), advisorStatus: () => Promise.resolve(pass(2)), payProducts: products } });
  await flush();
  const text = shown('pages/advisor-form/index.wxml', soon.page.data);
  assert.match(text, /批量工具 \d{1,2}月\d{1,2}日到期，到期不再扣钱；需要可以再买一次/);
  assert.match(text, /到期前后 7 天内续买，每次多送 3 天/);
  assert.match(text, /批量工具使用期内，每次最多 500 家，不限提交次数/);
  const renew = findAll(render('pages/advisor-form/index.wxml', soon.page.data), tapsTo('buyPass'));
  assert.equal(renew.length, 1);
  assert.equal(textOf(renew[0]), '续买批量工具');
  for (const [entry, days] of [[{ enabled: false }, 2], [{ enabled: true }, 10]]) {
    const quiet = toolForm({ api: { advisorCapabilities: () => Promise.resolve(caps(entry)), advisorStatus: () => Promise.resolve(pass(days)), payProducts: products } });
    await flush();
    const words = shown('pages/advisor-form/index.wxml', quiet.page.data);
    assert.doesNotMatch(words, /再买一次|续买/);
    assert.equal(findAll(render('pages/advisor-form/index.wxml', quiet.page.data), tapsTo('buyPass')).length, 0);
  }
});

test('纠错页在开关开着时写出纠错送天数的采纳标准，申请覆盖、支付问题不写，读不到就不写，防止规则不公开或误导', async () => {
  async function open(query, me) {
    const page = load('pages/feedback/index.js', {
      '../../utils/api.js': { me, feedback: () => Promise.resolve({}) },
      '../../utils/consent.js': { loginOpen: () => true, ensureConsent: () => Promise.resolve(true) },
    }, { wx: { setNavigationBarTitle() {}, showToast() {}, navigateBack() {}, switchTab() {} } });
    page.onLoad(query);
    await flush();
    return shown('pages/feedback/index.wxml', page.data);
  }
  const rule = '第一个指出、经核对属实的事实错误（日期、链接、名单状态等）才算；重复的、核对不了的不算';
  const on = () => Promise.resolve({ correctionReward: { days: 1, monthLimit: 3, rule } });
  assert.match(await open({ id: '7', title: '某通知' }, on), /纠错被采纳送 1 天批量工具使用期：第一个指出、经核对属实的事实错误.*每人每月最多 3 天/);
  for (const query of [{ type: 'coverage' }, { type: 'pay', orderId: '41' }]) assert.doesNotMatch(await open(query, on), /纠错被采纳送/);
  for (const me of [() => Promise.resolve({ correctionReward: null }), () => Promise.reject(new Error('离线')), () => Promise.resolve({ correctionReward: { days: '1', monthLimit: 3, rule } })]) {
    assert.doesNotMatch(await open({}, me), /纠错被采纳送/);
  }
});

test('小程序的页面和工具代码里不再有"客户工作台"，改叫"批量工具"，防止改名漏掉', () => {
  const files = [];
  const walk = (dir) => {
    for (const name of fs.readdirSync(path.join(root, dir))) {
      const rel = path.join(dir, name);
      if (fs.statSync(path.join(root, rel)).isDirectory()) walk(rel);
      else if (/\.(js|wxml|json|wxss)$/.test(name)) files.push(rel);
    }
  };
  ['pages', 'utils', 'components'].forEach(walk);
  files.push('content/pay-terms.js', 'app.json');
  const hits = files.filter((file) => /客户工作台|工作台使用期/.test(fs.readFileSync(path.join(root, file), 'utf8')));
  assert.deepEqual(hits, []);
  assert.equal(JSON.parse(fs.readFileSync(path.join(root, 'pages/pass/index.json'), 'utf8')).navigationBarTitleText, '批量工具');
});

test('付费协议的草稿版本号不能标定稿：服务端也拒绝用草稿版本号收费，防止带着草稿协议上线', () => {
  assert.ok(!/draft/i.test(payTerms.version) || payTerms.final === false, '草稿版本号不能标定稿');
});
