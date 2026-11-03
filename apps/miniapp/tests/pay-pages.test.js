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
      if (!Object.hasOwn(deps, key) && /(^|\/)pages\.js$/.test(key)) return require('./helpers/package-pages-default.js').pages; if (!Object.hasOwn(deps, key) && /utils\/advisor\.js$/.test(key)) return require('./helpers/package-pages-default.js').advisor; assert.ok(Object.hasOwn(deps, key), `${file}: ${key}`);
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
      return options.order ? options.order(id) : Promise.resolve({ orderId: id, status: '待确认' });
    },
    // 付款后每次都请服务端当场向微信核对（POST .../check），不只是读订单
    checkPayOrder(id) {
      calls.push(['check', id]);
      return options.order ? options.order(id) : Promise.resolve({ orderId: id, status: '待确认', check: 'unpaid' });
    },
  };
  const wx = {
    getStorageSync: (key) => storage.get(key),
    setStorageSync: (key, value) => storage.set(key, plain(value)),
    removeStorageSync: (key) => storage.delete(key),
    navigateTo: ({ url }) => urls.push(url),
    switchTab: (payload) => { urls.push(`tab:${payload.url}`); if (options.switchFail && payload.fail) payload.fail({ errMsg: 'switchTab:fail' }); },
    reLaunch: ({ url }) => urls.push(`relaunch:${url}`),
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
  if (options.before) options.before({ page, storage });
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
  const calls = []; const urls = []; const toasts = []; const opened = []; let modal = null;
  const api = {
    payOrders: () => { calls.push(['list']); return options.payOrders ? options.payOrders() : Promise.resolve({ items }); },
    // 打开订单页时替待确认的订单核对、"重新核对"：默认服务端这次没再问微信，原样返回
    checkPayOrder: (id) => {
      calls.push(['check', id]);
      if (options.check) return options.check(id);
      return Promise.resolve({ ...items.find((item) => String(item.orderId) === String(id)), check: null });
    },
    payProducts: () => { calls.push(['products']); return options.payProducts ? options.payProducts() : Promise.reject(Object.assign(new Error('没有找到接口'), { status: 404 })); },
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
  }, { wx: { showModal: (payload) => { modal = payload; }, navigateTo: ({ url }) => opened.push(url), showToast: (toast) => toasts.push(toast),
    switchTab: (payload) => { urls.push(`tab:${payload.url}`); if (options.switchFail && payload.fail) payload.fail({ errMsg: 'switchTab:fail' }); },
    reLaunch: ({ url }) => urls.push(`relaunch:${url}`) } }, { byReference: options.byReference });
  page.onShow();
  return { page, calls, urls, toasts, opened, modal: () => modal };
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

test('商品、下单、能力、我的、提交任务这几个请求带上代码包里的付费协议版本号和小程序环境；读不到环境就不带，防止服务端认不出新版本或把正式版当体验版（D-074）', async () => {
  function api(envVersion, terms = { version: 'pay-2026-11', final: true, title: '付费服务协议', text: '正文' }) {
    const seen = [];
    const request = (options) => {
      seen.push({ url: options.url, header: plain(options.header || {}) });
      Promise.resolve().then(() => options.success({ statusCode: 200, data: options.url.includes('/pay/orders') ? { orderId: 1 } : { items: [] } }));
    };
    const wx = { request, removeStorageSync() {}, getStorageSync() {}, setStorageSync() {} };
    if (envVersion) wx.getAccountInfoSync = () => ({ miniProgram: { envVersion } });
    const module = load('utils/api.js', {
      './config.js': { devBaseUrl: 'https://test.invalid' }, '../mock/data.js': {}, './mockSearch.js': {}, './format.js': {},
      '../content/pay-terms.js': terms,
    }, { wx, setTimeout: () => 0, clearTimeout() {} });
    return { module, seen };
  }
  const trial = api('trial');
  await trial.module.payProducts();
  await trial.module.createPayOrder({ product_id: 'workbench_30d', code: 'c', terms_version: 'pay-2026-11' });
  await trial.module.advisorCapabilities({ refresh: true });
  await trial.module.me();
  await trial.module.createAdvisorTask({ kind: 'compare', text: '合成企业甲' }, 'key-1').catch(() => {});
  const byPath = (part) => trial.seen.find((item) => item.url.includes(part));
  for (const part of ['/api/v1/pay/products', '/api/v1/pay/orders', '/api/v1/advisor/capabilities', '/api/v1/me', '/api/v1/advisor/tasks']) {
    const item = byPath(part);
    assert.ok(item, part);
    assert.equal(item.header['X-Youju-Pay-Terms'], 'pay-2026-11', part);
    assert.equal(item.header['X-Youju-Env'], 'trial', part);
  }
  assert.equal(byPath('/api/v1/advisor/tasks').header['Idempotency-Key'], 'key-1');
  const unknown = api(null);
  await unknown.module.payProducts();
  assert.deepEqual(unknown.seen[0].header, { 'X-Youju-Pay-Terms': 'pay-2026-11' });
  // 协议没定稿的包不带版本号（付款一律现网、真扣钱，沙箱已去掉）：服务端版本号配错也不会卖给草稿包
  const draft = api('trial', { version: 'pay-2026-10-draft', final: false, title: '付费服务协议', text: '【待定稿】' });
  await draft.module.payProducts();
  assert.deepEqual(draft.seen[0].header, { 'X-Youju-Env': 'trial' });
  // 和购买页的 pay.termsFinal 一致：final 写成 true、正文还是占位，也不带（不然入口还在、购买页却回首页）
  const pay = load('utils/pay.js', {}, {});
  for (const terms of [
    { version: 'pay-x', final: true, title: '付费服务协议', text: '【付费服务协议：待定稿】' },
    { version: 'pay-x', final: true, title: '付费服务协议', text: '  ' },
    { version: 'pay-x', final: 'true', title: '付费服务协议', text: '正文' },
    { version: 'pay-x', final: true, title: '付费服务协议', text: '正文' },
  ]) {
    const one = api('trial', terms);
    await one.module.payProducts();
    assert.equal(Object.hasOwn(one.seen[0].header, 'X-Youju-Pay-Terms'), pay.termsFinal(terms) && terms.final === true, JSON.stringify(terms));
  }
});

test('购买页在商品 404 或空列表时直接回首页，页面上不出现"暂未开放"，也没有付款按钮，防止首发包里的购买页被直接打开时像没做完的功能（D-074）', async () => {
  for (const payProducts of [
    () => Promise.reject(Object.assign(new Error('开关关闭'), { status: 404 })),
    () => Promise.resolve({ items: [] }),
  ]) {
    const h = passPage({ payProducts });
    await flush();
    assert.deepEqual(h.urls, ['tab:/pages/home/index']);
    assert.equal(h.page.data.product, null);
    assert.equal(h.page.data.error, '');
    const text = shown('pages/pass/index.wxml', h.page.data);
    assert.doesNotMatch(text, /暂未开放|开关关闭|批量工具/);
    assert.equal(payButtons(h.page.data).length, 0);
    assert.equal(h.calls.length, 0);
  }
});

test('购买页商品接口出错（不是 404）时留在本页显示错误和重试，不回首页，防止网络一抖就把正要付款的人送走', async () => {
  const h = passPage({ payProducts: () => Promise.reject(Object.assign(new Error('服务暂时出错（500），请稍后重试'), { status: 500 })) });
  await flush();
  assert.deepEqual(h.urls, []);
  assert.equal(h.page.data.loading, false);
  assert.equal(h.page.data.error, '服务暂时出错（500），请稍后重试');
  assert.match(shown('pages/pass/index.wxml', h.page.data), /重试/);
  assert.equal(payButtons(h.page.data).length, 0);
});

test('订单页没有订单又不在售时回首页、不写"暂未开放"；在售时写"还没有订单"；接口 404 也回首页，防止首发包里的订单页被直接打开时像没做完（D-074）', async () => {
  const closed = ordersPage([]);
  await flush();
  assert.deepEqual(closed.urls, ['tab:/pages/home/index']);
  assert.doesNotMatch(shown('pages/orders/index.wxml', closed.page.data), /暂未开放/);

  const selling = ordersPage([], { payProducts: () => Promise.resolve({ items: [PRODUCT], invoiceType: INVOICE }) });
  await flush();
  assert.deepEqual(selling.urls, []);
  assert.equal(selling.page.data.loading, false);
  assert.match(shown('pages/orders/index.wxml', selling.page.data), /还没有订单/);

  const gone = ordersPage([], { payOrders: () => Promise.reject(Object.assign(new Error('没有找到接口'), { status: 404 })) });
  const empty = ordersPage([], { payProducts: () => Promise.resolve({ items: [] }) });
  await flush();
  assert.deepEqual(empty.urls, ['tab:/pages/home/index']);
  await flush();
  assert.deepEqual(gone.urls, ['tab:/pages/home/index']);
  assert.doesNotMatch(shown('pages/orders/index.wxml', gone.page.data), /暂未开放|没有找到接口/);
});

test('购买页停卖时：刚开通的照旧显示开通结果、还有待确认的一笔时写去订单页看，都不送回首页；页面已隐藏也不跳，防止刚付款的人被送走（Codex 审阅）', async () => {
  let selling = true;
  const confirmed = passPage({ payProducts: () => Promise.resolve(selling ? { items: [PRODUCT], invoiceType: INVOICE } : { items: [] }) });
  await flush();
  confirmed.page.setData({ confirmed: true, message: '已开通，使用期到 2026-11-18' });
  selling = false;
  confirmed.page.onShow(); await flush();
  assert.deepEqual(confirmed.urls, []);
  assert.equal(confirmed.page.data.confirmed, true);
  assert.equal(confirmed.page.data.loading, false);
  assert.match(shown('pages/pass/index.wxml', confirmed.page.data), /去用工具/);

  const pending = passPage({ payProducts: () => Promise.resolve({ items: [] }),
    before: ({ storage }) => storage.set('pay.unsettled', { orderId: 41, at: Date.now() }) });
  await flush();
  assert.deepEqual(pending.urls, []);
  assert.match(shown('pages/pass/index.wxml', pending.page.data), /我的 → 我的订单/);
  assert.equal(payButtons(pending.page.data).length, 0);

  let release;
  const hidden = passPage({ payProducts: () => new Promise((resolve) => { release = resolve; }) });
  hidden.page.onHide();
  release({ items: [] }); await flush();
  assert.deepEqual(hidden.urls, []);
  hidden.page.onShow(); await flush();
  assert.deepEqual(hidden.urls, []);
  release({ items: [] }); await flush();
  assert.deepEqual(hidden.urls, ['tab:/pages/home/index']);
});

test('回首页时 switchTab 失败就 reLaunch 首页，购买页和订单页都不会一直停在加载中（Codex 审阅）', async () => {
  const pass = passPage({ payProducts: () => Promise.resolve({ items: [] }), switchFail: true });
  await flush();
  assert.deepEqual(pass.urls, ['tab:/pages/home/index', 'relaunch:/pages/home/index']);
  const orders = ordersPage([], { switchFail: true });
  await flush();
  assert.deepEqual(orders.urls, ['tab:/pages/home/index', 'relaunch:/pages/home/index']);
});

test('订单页没有订单时直接问商品接口（不用能力接口的缓存）；没连上或服务端出错时留在本页给重试，不当成停卖回首页（Codex 审阅）', async () => {
  for (const err of [Object.assign(new Error('网络连接失败，请检查网络后重试'), { status: 0 }), Object.assign(new Error('服务暂时出错（502），请稍后重试'), { status: 502 })]) {
    const h = ordersPage([], { payProducts: () => Promise.reject(err) });
    await flush();
    assert.deepEqual(h.urls, []);
    assert.deepEqual(h.calls, [['list'], ['products']]);
    assert.equal(h.page.data.error, err.message);
    assert.match(shown('pages/orders/index.wxml', h.page.data), /重试/);
  }
});

test('订单页有订单时照常列出，不查能力接口也不回首页（停卖以后已付款的人照样看订单、退款、开票）', async () => {
  const h = ordersPage([orderRecord({ orderId: 7 })]);
  await flush();
  assert.deepEqual(h.urls, []);
  assert.deepEqual(h.calls, [['list']]);
  assert.ok(orderCard(h.page.data, 7));
});

test('购买页协议未定稿时没有价格和付款按钮、直接回首页，调用 buy 也不下单，页面不出现"暂未开放"，防止用未定稿协议收款（D-074 ⑧）', async () => {
  assert.equal(payTerms.final, false);
  const h = passPage({ terms: payTerms });
  await flush();
  assert.equal(h.page.data.termsFinal, false);
  assert.deepEqual(h.urls, ['tab:/pages/home/index']);
  assert.equal(h.page.data.product, null);
  assert.equal(payButtons(h.page.data).length, 0);
  assert.doesNotMatch(shown('pages/pass/index.wxml', h.page.data), /暂未开放/);
  h.page.buy();
  assert.equal(h.calls.length, 0);
});

test('没有沙箱了：服务端多带 sandbox 也不显示沙箱提示、不写"沙箱支付"，按钮只写"支付"；协议没定稿照样不能付款，防止测试人员以为付的不是真钱（创始人 10/29）', async () => {
  for (const sandbox of [true, 'true', false]) {
    const draft = passPage({ terms: payTerms, payProducts: () => Promise.resolve({ items: [PRODUCT], invoiceType: INVOICE, sandbox }) });
    await flush();
    assert.equal(payButtons(draft.page.data).length, 0);
    draft.page.buy();
    assert.equal(draft.calls.length, 0);
    const live = passPage({ payProducts: () => Promise.resolve({ items: [PRODUCT], invoiceType: INVOICE, sandbox }) });
    await flush();
    assert.equal(Object.hasOwn(live.page.data, 'sandbox'), false);
    assert.doesNotMatch(shown('pages/pass/index.wxml', live.page.data), /沙箱/);
    assert.equal(payButtons(live.page.data).length, 1);
    assert.equal(textOf(payButtons(live.page.data)[0]).trim(), '支付 29 元（30 天）');
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
  assert.deepEqual(h.calls.filter((call) => call[0] === 'check').map((call) => call[1]), [41]);
  assert.equal(h.calls.filter((call) => call[0] === 'order').length, 0);
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
  assert.deepEqual(failed.calls.filter((call) => call[0] === 'check').map((call) => call[1]), [41]);
  // 这一轮还在确认时 busy 为真，再点会被忽略；超时后仍留着 pay.unsettled，再次购买只查这一笔
  firePollTimeout(failed.clock);
  assert.equal(failed.page.data.busy, false);
  assert.equal(failed.storage.get('pay.unsettled').orderId, 41);
  failed.page.buy();
  await flush();
  assert.equal(failed.calls.filter((call) => call[0] === 'create').length, 1);
  assert.deepEqual(failed.calls.filter((call) => call[0] === 'check').map((call) => call[1]), [41, 41]);
});

test('付款前就被微信拒了的错误（道具没发布到现网、发布还没生效、签名错、风控拦截等）和调起付款时就出错的，不记待确认、不挡下一次付款，照取消那样再查几秒，说明里写错误码、不断言没扣钱；-1、-5 和表里没有的错误码照旧当成可能扣了款，防止切现网后测试人员没花钱却被锁 48 小时（独立审阅、Codex 第 6 轮、agy 第 4 轮，10/29）', async () => {
  function fireCheckDeadline(clock) {
    const due = clock.queued.reduce((best, item) => (item.ms > best.ms ? item : best));
    clock.clearTimeout(due.id);
    due.fn();
  }
  const cases = [-15010, -15014, -15013, -15006, -15001, -15021, -4, 1001].map((errCode) => ({ errCode, fail: true }))
    .concat([{ errCode: undefined, fail: false }]);
  for (const { errCode, fail } of cases) {
    const onPay = fail ? (payload) => payload.fail({ errCode, errMsg: 'requestVirtualPayment:fail' }) : () => { throw new Error('调起失败'); };
    const h = passPage({ onPay });
    await flush();
    h.page.check({ detail: { value: ['agree'] } });
    h.page.buy();
    await flush();
    const note = errCode === undefined ? '支付没有完成，请稍后再试。' : `支付没有完成（错误码 ${errCode}），请稍后再试。`;
    assert.equal(h.page.data.message, `${note}如果其实扣了款，会自动开通，可以在"我的 → 我的订单"查看。`, String(errCode));
    assert.equal(h.page.data.payError, true);
    assert.doesNotMatch(h.page.data.message, /没有扣钱/);
    assert.equal(h.storage.has('pay.unsettled'), false);
    assert.equal(h.toasts.length, 0);
    // 照取消那样再查几秒（万一扣了款也能开通），查完说明还在、可以再付
    assert.ok(h.calls.some((call) => call[0] === 'check'));
    fireCheckDeadline(h.clock);
    assert.equal(h.page.data.busy, false);
    assert.equal(h.page.data.message.startsWith(note), true);
    h.page.buy();
    await flush();
    assert.equal(h.calls.filter((call) => call[0] === 'create').length, 2, String(errCode));
  }
  for (const errCode of [-1, -5, -15003, -15015, -15022, -3, undefined]) {
    const h = passPage({ onPay: (payload) => payload.fail({ errCode, errMsg: 'requestVirtualPayment:fail' }) });
    await flush();
    h.page.check({ detail: { value: ['agree'] } });
    h.page.buy();
    await flush();
    assert.equal(h.storage.get('pay.unsettled').orderId, 41, String(errCode));
    assert.doesNotMatch(h.page.data.message, /支付没有完成/);
  }
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
    orderRecord({ orderId: 42, refundUnavailableReason: '已超过 24 小时' }),
  ]);
  await flush();
  const text = shown('pages/orders/index.wxml', h.page.data);
  assert.match(text, /29\.00 元/);
  assert.match(text, /使用期 10-01 至 10-31/);
  assert.match(text, /订单号 41/);
  assert.match(textOf(orderCard(h.page.data, 42)), /不能自助退款：已超过 24 小时/);
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

test('提交时服务端说免费次数用完、现在买不了（canBuy=false）：丢掉页面缓存的价格，提示里不再提批量工具，防止刚停卖还引导购买（Codex 审阅，D-074）', async () => {
  const limit = Object.assign(new Error('本月免费次数已用完，下月 1 日恢复'), {
    status: 429, code: 'FREE_MONTHLY_LIMIT', detail: { code: 'FREE_MONTHLY_LIMIT', canBuy: false, message: '本月免费次数已用完，下月 1 日恢复' },
  });
  const h = toolForm({ api: {
    advisorCapabilities: () => Promise.resolve(caps({ enabled: true })),
    advisorStatus: () => Promise.resolve(quota(2)),
    payProducts: () => Promise.resolve({ items: [{ productId: 'workbench_30d', price: 2900, days: 30 }] }),
    createAdvisorTask: () => Promise.reject(limit),
  } });
  await flush();
  assert.ok(h.page.data.offer, '在售时页面先缓存了价格');
  h.page.input({ detail: { value: '合成企业甲' } });
  h.page.preview(); await flush();
  h.page.submit(); await flush(); await flush();
  assert.equal(h.page.data.offer, null);
  assert.match(h.page.data.inputError, /本月免费次数已用完/);
  assert.doesNotMatch(h.page.data.inputError, /批量工具/);
});

test('价格请求比"现在买不了"的 429 晚回来：不再把价格写回去，防止刚停卖时价格又冒出来（Codex 审阅）', async () => {
  const limit = Object.assign(new Error('本月免费次数已用完，下月 1 日恢复'), {
    status: 429, code: 'FREE_MONTHLY_LIMIT', detail: { code: 'FREE_MONTHLY_LIMIT', canBuy: false, message: '本月免费次数已用完，下月 1 日恢复' },
  });
  let release;
  let calls = 0;
  const h = toolForm({ api: {
    advisorCapabilities: () => Promise.resolve(caps({ enabled: true })),
    advisorStatus: () => Promise.resolve(quota(2)),
    payProducts: () => { calls += 1; return calls === 1 ? Promise.resolve({ items: [{ productId: 'workbench_30d', price: 2900, days: 30 }] }) : new Promise((resolve) => { release = resolve; }); },
    createAdvisorTask: () => Promise.reject(limit),
  } });
  await flush();
  assert.ok(h.page.data.offer);
  // 页面回到前台重新载入时又发了一次价格请求，还没回来就提交、收到 canBuy=false
  h.page.onShow(); await flush();
  h.page.input({ detail: { value: '合成企业甲' } });
  h.page.preview(); await flush();
  h.page.submit(); await flush(); await flush();
  assert.equal(h.page.data.offer, null);
  if (release) { release({ items: [{ productId: 'workbench_30d', price: 2900, days: 30 }] }); await flush(); }
  assert.equal(h.page.data.offer, null);
});

test('我的页只在有订单时显示"我的订单"并进入订单页；在售但没买过时不放空的订单入口（D-076）', async () => {
  const urls = []; const pay = { enabled: false, hasOrders: false }; let meCalls = 0;
  const page = load('pages/me/index.js', {
    '../../content/legal.js': { final: true, version: '2026-10' },
    '../../utils/consent.js': { loginOpen: () => true, consentEpoch: () => 0, applyServer: () => true, withdraw: () => Promise.resolve(false), markAgreed() {}, openPrivacy() {}, CONSENT_PAGE: '', agreeFailed() {}, featureNames: () => '' },
    '../../utils/subscribe.js': { remindOpen: () => false },
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve(null), notCoveredNames: () => '' },
    '../../utils/api.js': { isLive: () => true, me: () => { meCalls += 1; return Promise.resolve({ pay: { ...pay }, follows: 0, subscriptions: {} }); },
      payProducts: () => Promise.resolve({ items: TIERS }), advisorStatus: () => Promise.reject(new Error('没有使用期')) },
  }, { wx: { navigateTo: ({ url }) => urls.push(url), switchTab() {}, showToast() {} } });
  const row = () => findAll(render('pages/me/index.wxml', page.data), (node) => node.attrs['data-url'] === '/pages/orders/index');
  for (const [enabled, hasOrders, shownRow] of [[false, false, false], [true, false, false], [false, true, true], [true, true, true]]) {
    Object.assign(pay, { enabled, hasOrders });
    page.refresh();
    await flush();
    assert.equal(page.data.ordersEntry, shownRow, JSON.stringify(pay));
    assert.equal(row().length, shownRow ? 1 : 0, JSON.stringify(pay));
    if (!shownRow) assert.doesNotMatch(shown('pages/me/index.wxml', page.data), /我的订单/);
  }
  assert.equal(meCalls, 4);
  assert.match(textOf(row()[0]), /我的订单/);
  page.go({ currentTarget: { dataset: { url: row()[0].attrs['data-url'] } } });
  assert.deepEqual(urls, ['/pages/orders/index']);
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
  // 提示只在真的带上了订单号时说"已自动带上"，并写出是哪一笔（订单页记的可能是之前操作过的那一笔）；
  // 没带上时请用户自己写（隐私保护指引一·6 的说法，Codex 10/19）
  assert.match(shown('pages/feedback/index.wxml', numbered.page.data), /已自动带上订单号 41，问的不是这一笔的，请在内容里写明/);
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
  // 付款前就写明（agy 10/29）：未成年人未经监护人同意购买的，《付费服务协议》第二条第 7 项写了可以退
  assert.match(text, /未成年人请在监护人同意后再购买/);
  // 苹果退款：由苹果公司通过 App Store 处理，小程序里也能提交、我们协助（协议第二条第 7 项、运营规范 14.6）
  assert.match(text, /苹果手机付款的，退款由苹果公司通过 App Store 处理，是否退款由苹果公司决定；也可以在“遇到问题”里提交，我们协助办理/);
  assert.doesNotMatch(text, /只能向 App Store 申请退款/);
  // 无理由退款：付款后 24 小时内、没用过付费的量（D-081，原为 7 天）
  assert.match(text, /付款后 24 小时内没用过付费的量（没超出每月免费次数、没有一次超过 50 家）、也没开过发票，可在“我的 → 我的订单”申请无理由退款，全额原路退回；每个账号每月只能申请 1 次无理由退款（每月 1 日起重新计算）；超过 24 小时或者已经用过的，不能无理由退款/);
  assert.doesNotMatch(text, /付款 7 天内/);
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

test('购买页的发票类型以服务端为准，服务端没给时没有付款按钮、回首页，防止带着发票占位上线收费', async () => {
  for (const invoiceType of [undefined, null, '', '  ', '发票类型待创始人确认', '【发票类型】']) {
    const live = passPage({ payProducts: () => Promise.resolve({ items: TIERS, invoiceType }) });
    await flush();
    assert.equal(live.page.data.invoiceText, '');
    assert.equal(payButtons(live.page.data).length, 0);
    // 付不了款就和没在卖一样回首页，不显示"还没确定"的占位（D-074 ⑧）
    assert.deepEqual(live.urls, ['tab:/pages/home/index']);
    assert.doesNotMatch(shown('pages/pass/index.wxml', live.page.data), /能开哪种发票还没确定|暂未开放/);
    live.page.buy();
    assert.equal(live.calls.length, 0);
  }
  // 没有沙箱了：服务端多带 sandbox 也不能不写发票就付款
  const sandbox = passPage({ payProducts: () => Promise.resolve({ items: TIERS, sandbox: true }) });
  await flush();
  assert.equal(payButtons(sandbox.page.data).length, 0);
  const ready = passPage({ payProducts: () => Promise.resolve({ items: TIERS, invoiceType: ` ${INVOICE} ` }) });
  await flush();
  assert.equal(ready.page.data.invoiceText, INVOICE);
  assert.equal(payButtons(ready.page.data).length, 1);
  // 协议文件里不另写一份发票类型（只认服务端那一句）
  assert.equal(Object.hasOwn(payTerms, 'invoice'), false);
});

test('续买规则只有一句，和价格在同一屏；现在付款符合时说一声，开关关着时不提，防止规则藏在别处或不在售时出现', async () => {
  const on = passPage({ payProducts: () => Promise.resolve({ items: TIERS, invoiceType: INVOICE, renewBonus: { days: 3, windowDays: 7, eligibleNow: true } }) });
  await flush();
  const hero = findAll(render('pages/pass/index.wxml', on.page.data), (node) => String(node.attrs.class || '').split(/\s+/).includes('pass-hero'))[0];
  assert.match(textOf(hero), /29.*79.*到期前后 7 天内续买，每次多送 3 天批量工具使用期：现在付款就送/);
  const later = passPage({ payProducts: () => Promise.resolve({ items: TIERS, invoiceType: INVOICE, renewBonus: { days: 3, windowDays: 7, eligibleNow: false } }) });
  await flush();
  const text = shown('pages/pass/index.wxml', later.page.data);
  assert.match(text, /到期前后 7 天内续买，每次多送 3 天/);
  assert.doesNotMatch(text, /现在付款就送/);
  for (const renewBonus of [null, undefined, { days: '3', windowDays: 7 }, { days: 3, windowDays: 0 }]) {
    const off = passPage({ payProducts: () => Promise.resolve({ items: TIERS, invoiceType: INVOICE, renewBonus }) });
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
    const api = { payProducts: () => Promise.resolve({ items: TIERS, invoiceType: INVOICE }), advisorStatus: () => Promise.resolve(status) };
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

test('我的页在售时所有人都显示"批量工具"一行：没买过写最便宜一档，买过写到期日；不在售时不显示，点了进购买页（D-076）', async () => {
  const urls = []; const pay = { enabled: false, hasOrders: false };
  let status = () => Promise.reject(new Error('读不到'));
  const page = load('pages/me/index.js', {
    '../../content/legal.js': { final: true, version: '2026-10' },
    '../../utils/consent.js': { loginOpen: () => true, consentEpoch: () => 0, applyServer: () => true, withdraw: () => Promise.resolve(false), markAgreed() {}, openPrivacy() {}, CONSENT_PAGE: '', agreeFailed() {}, featureNames: () => '' },
    '../../utils/subscribe.js': { remindOpen: () => false },
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve(null), notCoveredNames: () => '' },
    '../../utils/api.js': { isLive: () => true, me: () => Promise.resolve({ pay: { ...pay }, follows: 0, subscriptions: {} }),
      payProducts: () => Promise.resolve({ items: TIERS }), advisorStatus: () => status() },
  }, { wx: { navigateTo: ({ url }) => urls.push(url), switchTab() {}, showToast() {} } });
  const row = () => findAll(render('pages/me/index.wxml', page.data), (node) => node.attrs['data-url'] === '/pages/pass/index');
  const show = async (enabled, hasOrders) => { Object.assign(pay, { enabled, hasOrders }); page.refresh(); await flush(); await flush(); };
  for (const [enabled, hasOrders, shownRow] of [[false, false, false], [false, true, false], [true, false, true], [true, true, true]]) {
    await show(enabled, hasOrders);
    assert.equal(row().length, shownRow ? 1 : 0, JSON.stringify(pay));
  }
  await show(true, false);
  assert.equal(page.data.passHint, '30 天 29 元起');
  assert.match(textOf(row()[0]), /批量工具/);
  assert.match(textOf(row()[0]), /30 天 29 元起/);
  status = () => Promise.resolve({ pass: { active: true, expiresAt: '2026-11-18T04:00:00Z' } });
  await show(true, true);
  assert.equal(page.data.passHint, '到 11月18日');
  status = () => Promise.resolve({ pass: { active: false, expiresAt: null } });
  await show(true, true);
  assert.equal(page.data.passHint, '已到期，可再买一次');
  status = () => Promise.reject(new Error('读不到'));
  await show(true, true);
  assert.equal(page.data.passHint, '');
  assert.equal(row().length, 1);
  await show(false, true);
  assert.equal(row().length, 0);
  assert.equal(page.data.passHint, '');
  await show(true, false);
  page.go({ currentTarget: { dataset: { url: row()[0].attrs['data-url'] } } });
  assert.deepEqual(urls, ['/pages/pass/index']);
});

test('我的页刷新失败时收起"批量工具"一行，不在可能已停卖时留着购买入口（D-074）', async () => {
  let fail = false;
  const page = load('pages/me/index.js', {
    '../../content/legal.js': { final: true, version: '2026-10' },
    '../../utils/consent.js': { loginOpen: () => true, consentEpoch: () => 0, applyServer: () => true, withdraw: () => Promise.resolve(false), markAgreed() {}, openPrivacy() {}, CONSENT_PAGE: '', agreeFailed() {}, featureNames: () => '' },
    '../../utils/subscribe.js': { remindOpen: () => false },
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve(null), notCoveredNames: () => '' },
    '../../utils/api.js': { isLive: () => true, me: () => (fail ? Promise.reject(new Error('网络不稳定')) : Promise.resolve({ pay: { enabled: true, hasOrders: false }, follows: 0, subscriptions: {} })),
      payProducts: () => Promise.resolve({ items: TIERS }), advisorStatus: () => Promise.reject(new Error('x')) },
  }, { wx: { navigateTo() {}, switchTab() {}, showToast() {} } });
  page.refresh(); await flush(); await flush();
  assert.equal(page.data.passEntry, true);
  fail = true;
  page.refresh(); await flush(); await flush();
  assert.equal(page.data.passEntry, false);
  assert.equal(page.data.passHint, '');
  assert.doesNotMatch(shown('pages/me/index.wxml', page.data), /批量工具|元起/);
});

const centerCaps = (pay) => Promise.resolve({ data_tools: { enabled: true, features: ['compare', 'track'], remaining: {} }, ai_advisor: { enabled: false, features: [] }, reason: 'off', pay });
const centerStatus = ({ used = 3, pass = null, rows = pass ? 500 : 50, trackRows = rows } = {}) => Promise.resolve({ enabled: true, quota: { compare: { used, limit: 3, maxRows: rows }, track: { used, limit: 3, maxRows: trackRows } }, pass, trial: null, season: null });
function toolCenter(api) {
  const urls = []; let products = 0;
  const page = load('pages/advisor/index.js', {
    '../../utils/api.js': { advisorTasks: () => Promise.resolve({ items: [] }), ...api, payProducts: () => { products += 1; return api.payProducts ? api.payProducts() : Promise.resolve({ items: TIERS }); } },
    '../../utils/advisor.js': require('../utils/advisor.js'),
    '../../utils/tool-context.js': require('../utils/tool-context.js'),
    '../../utils/boost.js': require('../utils/boost.js'),
    // AI 板块（#317）加的两个依赖：这里只测批量工具卡片，AI 当作关着、不盯跨零点
    '../../utils/ai-access.js': require('./helpers/ai-off.js'),
    '../../utils/daywatch.js': require('./helpers/day-off.js'),
  }, { wx: { setNavigationBarTitle() {}, navigateTo: ({ url }) => urls.push(url) } });
  page.onLoad({}); page.onShow();
  return { page, urls, products: () => products };
}

test('工具中心在售时最上面放"批量工具"卡片：免费用完写"已用完"、两档价格、"了解批量工具"进购买页，不再写"不需要购买"（D-076）', async () => {
  const h = toolCenter({ advisorCapabilities: () => centerCaps({ enabled: true, hasOrders: false }), advisorStatus: () => centerStatus({ used: 3 }) });
  await flush(); await flush();
  const text = shown('pages/advisor/index.wxml', h.page.data);
  assert.match(text, /本月免费次数已用完，下月 1 日恢复/);
  assert.match(text, /要一次处理更多家：30 天 29 元、90 天 79 元，不限次数，每次 500 家以内；一次付清，到期不再扣钱/);
  assert.doesNotMatch(text, /不需要购买/);
  assert.ok(text.indexOf('批量工具') < text.indexOf('我的记录'), '卡片要在最上面');
  const buy = findAll(render('pages/advisor/index.wxml', h.page.data), tapsTo('buyPass'));
  assert.equal(buy.length, 1);
  assert.equal(textOf(buy[0]), '了解批量工具');
  h.page.buyPass();
  assert.deepEqual(h.urls, ['/pages/pass/index']);
  // 还有免费次数时写还剩几次，不标红
  const some = toolCenter({ advisorCapabilities: () => centerCaps({ enabled: true, hasOrders: false }), advisorStatus: () => centerStatus({ used: 1 }) });
  await flush(); await flush();
  assert.equal(some.page.data.payCard.usedUp, false);
  assert.match(shown('pages/advisor/index.wxml', some.page.data), /本月免费：比对还剩 2 次、通知表还剩 2 次，每次最多 50 家/);
});

test('工具中心不在售时没有批量工具卡片、不取价格，照旧写"免费额度不需要购买"；取不到价格时也不显示卡片', async () => {
  const off = toolCenter({ advisorCapabilities: () => centerCaps({ enabled: false, hasOrders: false }), advisorStatus: () => centerStatus({ used: 3 }) });
  await flush(); await flush();
  assert.equal(off.page.data.payCard, null);
  assert.equal(off.products(), 0);
  const text = shown('pages/advisor/index.wxml', off.page.data);
  assert.doesNotMatch(text, /了解批量工具|元/);
  assert.match(text, /免费额度不需要购买/);
  const broken = toolCenter({ advisorCapabilities: () => centerCaps({ enabled: true, hasOrders: false }), advisorStatus: () => centerStatus({ used: 3 }), payProducts: () => Promise.reject(Object.assign(new Error('没有找到接口'), { status: 404 })) });
  await flush(); await flush();
  assert.equal(broken.page.data.payCard, null);
  assert.doesNotMatch(shown('pages/advisor/index.wxml', broken.page.data), /暂未开放|了解批量工具/);
});

test('工具中心只用完一项时那一项写"已用完"，不写"还剩 0 次"', async () => {
  const quota = { enabled: true, quota: { compare: { used: 3, limit: 3, maxRows: 50 }, track: { used: 1, limit: 3, maxRows: 50 } }, pass: null, trial: null, season: null };
  const h = toolCenter({ advisorCapabilities: () => centerCaps({ enabled: true, hasOrders: false }), advisorStatus: () => Promise.resolve(quota) });
  await flush(); await flush();
  const text = shown('pages/advisor/index.wxml', h.page.data);
  assert.match(text, /本月免费：比对已用完、通知表还剩 2 次，每次最多 50 家/);
  assert.doesNotMatch(text, /还剩 0 次/);
  assert.equal(h.page.data.payCard.usedUp, false);
});

test('工具中心额度有了结果才画批量工具卡片；"刷新额度"后旧请求晚到不覆盖新额度；读不到额度时卡片只写价格（Codex 审阅）', async () => {
  const pending = [];
  const h = toolCenter({ advisorCapabilities: () => centerCaps({ enabled: true, hasOrders: false }),
    advisorStatus: () => new Promise((resolve, reject) => pending.push({ resolve, reject })) });
  await flush(); await flush();
  // 价格已到、额度还没回来：先不画
  assert.equal(h.page.data.payCard, null);
  // 点"刷新额度"：第二个请求先回来（已用完），第一个晚到（还剩 3 次）不能盖掉
  h.page.refreshQuota();
  pending[1].resolve({ enabled: true, quota: { compare: { used: 3, limit: 3, maxRows: 50 }, track: { used: 3, limit: 3, maxRows: 50 } }, pass: null, trial: null, season: null });
  await flush(); await flush();
  pending[0].resolve({ enabled: true, quota: { compare: { used: 0, limit: 3, maxRows: 50 }, track: { used: 0, limit: 3, maxRows: 50 } }, pass: null, trial: null, season: null });
  await flush(); await flush();
  assert.equal(h.page.data.payCard.usedUp, true);
  assert.match(shown('pages/advisor/index.wxml', h.page.data), /本月免费次数已用完/);
  // 读不到额度：卡片只写价格，不写免费次数
  const broken = toolCenter({ advisorCapabilities: () => centerCaps({ enabled: true, hasOrders: false }), advisorStatus: () => Promise.reject(new Error('网络不稳定')) });
  await flush(); await flush();
  assert.equal(broken.page.data.payCard.quotaLine, '');
  assert.match(shown('pages/advisor/index.wxml', broken.page.data), /要一次处理更多家：30 天 29 元、90 天 79 元/);
  assert.match(shown('pages/advisor/index.wxml', broken.page.data), /额度未能更新/);
});

test('工具中心刷新额度失败时丢掉上一次的免费次数；有使用期但取不到价格时不放"续买"按钮（Codex 第二轮）', async () => {
  let fail = false;
  const h = toolCenter({ advisorCapabilities: () => centerCaps({ enabled: true, hasOrders: false }),
    advisorStatus: () => (fail ? Promise.reject(new Error('网络不稳定')) : centerStatus({ used: 1 })) });
  await flush(); await flush();
  assert.match(h.page.data.payCard.quotaLine, /还剩 2 次/);
  fail = true;
  h.page.refreshQuota();
  await flush(); await flush();
  assert.equal(h.page.data.payCard.quotaLine, '');
  assert.doesNotMatch(shown('pages/advisor/index.wxml', h.page.data), /还剩 2 次/);
  const pass = toolCenter({ advisorCapabilities: () => centerCaps({ enabled: true, hasOrders: true }),
    advisorStatus: () => centerStatus({ used: 0, pass: { active: true, expiresAt: '2026-11-18T04:00:00Z' } }),
    payProducts: () => Promise.reject(Object.assign(new Error('没有找到接口'), { status: 404 })) });
  await flush(); await flush();
  assert.match(shown('pages/advisor/index.wxml', pass.page.data), /批量工具使用期到 11月18日/);
  assert.equal(findAll(render('pages/advisor/index.wxml', pass.page.data), tapsTo('buyPass')).length, 0);
  pass.page.buyPass();
  assert.deepEqual(pass.urls, []);
});

test('工具中心两项每次上限不同时分别写，按额度接口的数字，不写死（Codex 第三轮）', async () => {
  const h = toolCenter({ advisorCapabilities: () => centerCaps({ enabled: true, hasOrders: false }), advisorStatus: () => centerStatus({ used: 1, rows: 50, trackRows: 30 }) });
  await flush(); await flush();
  assert.match(shown('pages/advisor/index.wxml', h.page.data), /本月免费：比对还剩 2 次、通知表还剩 2 次，比对每次最多 50 家、通知表每次最多 30 家/);
});

test('工具中心使用期内的批量工具卡片写到期日和"续买批量工具"', async () => {
  const h = toolCenter({ advisorCapabilities: () => centerCaps({ enabled: true, hasOrders: true }), advisorStatus: () => centerStatus({ used: 0, pass: { active: true, expiresAt: '2026-11-18T04:00:00Z' } }) });
  await flush(); await flush();
  const text = shown('pages/advisor/index.wxml', h.page.data);
  assert.match(text, /批量工具使用期到 11月18日：批量名单比对、客户相关通知表不限次数，每次最多 500 家/);
  assert.match(text, /再买一次接着往后顺延：30 天 29 元、90 天 79 元/);
  assert.equal(textOf(findAll(render('pages/advisor/index.wxml', h.page.data), tapsTo('buyPass'))[0]), '续买批量工具');
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

test('购买页写清怎么算次数和苹果手机的付款条件；工具页免费和体验时写"交一份名单算一次"，使用期内不写，防止顾问算不清次数、苹果手机付到一半才知道付不了（D-074）', async () => {
  const h = passPage();
  await flush();
  const text = shown('pages/pass/index.wxml', h.page.data);
  assert.match(text, /交一份名单算一次，系统出错、没出结果的不算/);
  assert.match(text, /iOS 15 以上、微信 8\.0\.68 以上，并且用中国大陆的 App Store 账户/);
  const free = toolForm();
  await flush();
  assert.equal(free.page.data.countRule, '交一份名单算一次，系统出错、没出结果的不算');
  assert.match(shown('pages/advisor-form/index.wxml', free.page.data), /交一份名单算一次/);
  const paid = toolForm({ api: { advisorStatus: () => Promise.resolve({ ...quota(0), pass: { active: true, expiresAt: '2026-12-01T00:00:00.000Z' } }) } });
  await flush();
  assert.equal(paid.page.data.countRule, '');
  assert.doesNotMatch(shown('pages/advisor-form/index.wxml', paid.page.data), /交一份名单算一次/);
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

// ---- 付款后主动核对、待确认和人工核对中的订单、自助退款、我的页标识（10/19 创始人：付了款一直显示"待支付"）----

test('付款后的确认每次都请服务端当场向微信核对（核对接口），服务端还没有这个接口（404）时退回读订单，订单也查不到才当已关闭，防止付了款还要等推送和补单', async () => {
  function api(answers) {
    const seen = [];
    const request = (options) => {
      seen.push({ url: options.url, method: options.method || 'GET' });
      const answer = answers.shift();
      Promise.resolve().then(() => options.success(answer));
    };
    const wx = { request, removeStorageSync() {}, getStorageSync() {}, setStorageSync() {} };
    const module = load('utils/api.js', {
      './config.js': { devBaseUrl: 'https://test.invalid' }, '../mock/data.js': {}, './mockSearch.js': {}, './format.js': {},
      '../content/pay-terms.js': { version: 'pay-2026-11', final: true, title: '付费服务协议', text: '正文' },
    }, { wx, setTimeout: () => 0, clearTimeout() {} });
    return { module, seen };
  }
  const now = api([{ statusCode: 200, data: { orderId: 7, status: '已完成', check: 'paid' } }]);
  assert.deepEqual(plain(await now.module.checkPayOrder(7)), { orderId: 7, status: '已完成', check: 'paid' });
  assert.deepEqual(now.seen.map((item) => [item.method, item.url.replace('https://test.invalid', '')]), [['POST', '/api/v1/pay/orders/7/check']]);
  const old = api([{ statusCode: 404, data: { detail: '没有找到接口' } }, { statusCode: 200, data: { orderId: 7, status: '待确认' } }]);
  assert.deepEqual(plain(await old.module.checkPayOrder(7)), { orderId: 7, status: '待确认', check: null });
  assert.deepEqual(old.seen.map((item) => item.method), ['POST', 'GET']);
  const gone = api([{ statusCode: 404, data: { detail: '没有找到接口' } }, { statusCode: 404, data: { detail: '没有找到接口' } }]);
  await assert.rejects(gone.module.checkPayOrder(7), (err) => err.status === 404);
});

test('购买页付款后核对到"需人工处理"：按这张订单的实际情况写（能自助退的才说可以申请退款），并给"查看我的订单"；确认超时也给这个入口，自己取消的不给', async () => {
  const said = async (order) => {
    const h = passPage({ onPay: (payload) => payload.success(), order: () => Promise.resolve({ orderId: 41, status: '需人工处理', amount: 2900, check: 'manual', ...order }) });
    await flush();
    h.page.check({ detail: { value: ['agree'] } });
    h.page.buy();
    await flush();
    assert.equal(h.page.data.busy, false);
    assert.equal(h.page.data.ordersLink, true);
    return h.page.data.message;
  };
  assert.equal(await said({ canRefund: true, paidAmount: 2900 }), '付款已收到，但开通时出了问题，还没开通。我们 1 个工作日内处理；不想等可以直接申请退款，全额原路退回。');
  // 金额要人工核对、投诉中这类不能自助退的：不说可以退
  assert.equal(await said({ canRefund: false, paidAmount: 2900 }), '付款已收到，但开通时出了问题，还没开通。我们 1 个工作日内处理，结果会显示在这里。');
  // 微信没确认收到付款（查单状态少见等）：不写"付款已收到"（Codex 复核）
  assert.equal(await said({ canRefund: false, paidAmount: null }), '这笔订单的付款情况需要人工核对，我们 1 个工作日内处理，结果会显示在这里。');
  // 开通过的：不说"还没开通"
  assert.equal(await said({ canRefund: false, ...PERIOD }), '这笔订单需要人工核对，我们 1 个工作日内处理，结果会显示在这里。');
  const manual = passPage({
    onPay: (payload) => payload.success(),
    order: () => Promise.resolve({ orderId: 41, status: '需人工处理', amount: 2900, check: 'manual', canRefund: true }),
  });
  await flush();
  manual.page.check({ detail: { value: ['agree'] } });
  manual.page.buy();
  await flush();
  const links = findAll(render('pages/pass/index.wxml', manual.page.data), tapsTo('orders'));
  assert.equal(links.length, 1);
  assert.match(textOf(links[0]), /查看我的订单/);
  manual.page.orders();
  assert.deepEqual(manual.urls, ['/pages/orders/index']);

  const slow = passPage({ onPay: (payload) => payload.success() });
  await flush();
  slow.page.check({ detail: { value: ['agree'] } });
  slow.page.buy();
  await flush();
  firePollTimeout(slow.clock);
  assert.match(slow.page.data.message, /暂未确认到支付结果：已扣款的会自动开通/);
  assert.match(slow.page.data.message, /我的 → 我的订单/);
  assert.equal(slow.page.data.ordersLink, true);

  const cancelled = passPage({ onPay: (payload) => payload.fail({ errCode: -2, errMsg: 'requestVirtualPayment:fail cancel' }) });
  await flush();
  cancelled.page.check({ detail: { value: ['agree'] } });
  cancelled.page.buy();
  await flush();
  // 取消后只再查约 6 秒（防取消回调不准）：拨那个截止计时
  const due = cancelled.clock.queued.reduce((best, item) => (item.ms > best.ms ? item : best));
  assert.ok(due.ms >= 5000 && due.ms < 50000, `取消后的截止计时是 ${due.ms} 毫秒`);
  cancelled.clock.clearTimeout(due.id);
  due.fn();
  assert.equal(cancelled.page.data.message, '已取消支付');
  assert.equal(cancelled.page.data.ordersLink, false);
});

const PENDING = { orderId: 51, name: '批量工具 30 天', days: 30, status: '待确认', amount: 2900, canRefund: false, canInvoice: false,
  refundUnavailableReason: '支付方式还没确认，可点“遇到问题”告诉我们', createdAt: '2026-10-19T14:57:48Z', paidAt: null, passStartsAt: null, passEndsAt: null };

test('订单页的待确认订单：写"待确认"（老服务端的"待支付"也写成待确认）、几点下的单和说明，不写"不能自助退款"，有"重新核对"和"遇到问题"，防止付了款看到"待支付"以为钱白扣了', async () => {
  for (const status of ['待确认', '待支付']) {
    const h = ordersPage([{ ...PENDING, status }], { check: () => new Promise(() => {}) });
    await flush();
    const card = textOf(orderCard(h.page.data, 51));
    assert.match(card, /待确认/);
    assert.doesNotMatch(card, /待支付/);
    assert.match(card, /10-19 22:57 下单/);
    assert.match(card, /正在确认付款结果：已扣款的会自动开通；没付款不会扣钱/);
    assert.doesNotMatch(card, /不能自助退款/);
    const page = render('pages/orders/index.wxml', h.page.data);
    assert.equal(findAll(page, tapsTo('recheck')).length, 1);
    assert.equal(findAll(page, tapsTo('problem')).length, 1);
    assert.equal(findAll(page, tapsTo('refund')).length, 0);
  }
});

test('订单页打开时替待确认的订单向微信核对（最多 3 张、一张一张查）：付了款的当场变成已开通、能申请退款，没付的写"还没查到这笔付款"', async () => {
  const items = [51, 52, 53, 54].map((orderId) => ({ ...PENDING, orderId }));
  const h = ordersPage(items, {
    check: (id) => Promise.resolve(id === 51
      ? orderRecord({ orderId: 51, canRefund: true, check: 'paid' })
      : { ...PENDING, orderId: id, check: 'unpaid' }),
  });
  await flush(); await flush(); await flush(); await flush();
  assert.deepEqual(h.calls.filter((call) => call[0] === 'check').map((call) => call[1]), [51, 52, 53]);
  const opened = textOf(orderCard(h.page.data, 51));
  assert.match(opened, /已开通/);
  assert.match(opened, /使用期 10-01 至 10-31/);
  assert.equal(findAll(orderCard(h.page.data, 51), tapsTo('refund')).length, 1);
  assert.match(textOf(orderCard(h.page.data, 52)), /还没查到这笔付款：刚付过款的一般几秒内自动开通/);
  // 第 4 张没核对，照旧写一般的说明
  assert.match(textOf(orderCard(h.page.data, 54)), /正在确认付款结果/);
});

test('订单页"重新核对"：结果写在这张订单上，另给一句提示；核对中按钮不能再点；切走再回来时放开按钮，防止连点和结果写错订单', async () => {
  let answer = null;
  const h = ordersPage([PENDING], { check: () => (answer ? answer() : new Promise(() => {})) });
  await flush();
  const hang = h.page.data.checking;
  assert.equal(hang, null);
  answer = () => new Promise(() => {});
  h.page.recheck({ currentTarget: { dataset: { id: 51 } } });
  assert.equal(h.page.data.checking, 51);
  h.page.recheck({ currentTarget: { dataset: { id: 51 } } });
  assert.equal(h.calls.filter((call) => call[0] === 'check').length, 2); // 打开时一次 + 这一次
  // 切走再回来：重新载入，"重新核对"放开
  h.page.onHide(); h.page.onShow();
  await flush();
  assert.equal(h.page.data.checking, null);
  for (const [result, toast] of [
    [orderRecord({ orderId: 51, check: 'paid' }), '已核对到付款，已开通'],
    [{ ...PENDING, check: 'unpaid' }, '还没查到这笔付款'],
    [{ ...PENDING, check: 'unavailable' }, '暂时没能核对，请稍后再试'],
    [{ ...PENDING, check: null }, '正在核对，请过几秒再看'],
    // 转人工：微信确认收到付款的才说"付款已收到"（Codex 复核）
    [{ ...PENDING, status: '需人工处理', check: 'manual', paidAmount: 2900 }, '付款已收到，正在人工核对'],
    [{ ...PENDING, status: '需人工处理', check: 'manual', paidAmount: null }, '需要人工核对，结果会显示在这里'],
    [{ ...PENDING, status: '已关闭', check: 'closed' }, '没有确认到付款，订单已关闭'],
  ]) {
    h.page.setData({ items: [pay.orderView(PENDING)] });
    answer = () => Promise.resolve(result);
    h.page.recheck({ currentTarget: { dataset: { id: 51 } } });
    await flush();
    assert.equal(h.page.data.checking, null);
    assert.equal(h.toasts.at(-1).title, toast);
    assert.equal(String(h.page.data.items[0].orderId), '51');
  }
  assert.match(textOf(orderCard(h.page.data, 51)), /没有确认到付款，订单已关闭/);
  answer = () => Promise.reject(new Error('网络连接失败，请检查网络后重试'));
  h.page.setData({ items: [pay.orderView(PENDING)] });
  h.page.recheck({ currentTarget: { dataset: { id: 51 } } });
  await flush();
  assert.equal(h.page.data.checking, null);
  assert.equal(h.toasts.at(-1).title, '网络连接失败，请检查网络后重试');
  // 这张订单上的"遇到问题"：带着订单号去反馈页
  h.page.problem({ currentTarget: { dataset: { id: 51 } } });
  assert.deepEqual(h.opened, ['/pages/feedback/index?type=pay&orderId=51']);
});

test('订单页"人工核对中"：付了款没开通、能自助退的写可以直接申请退款，确认框写全额原路退回（没有使用期可取消）；开通过的、金额要人工核对的不能自助退，只写多久处理', async () => {
  const h = ordersPage([
    { ...PENDING, orderId: 61, status: '需人工处理', canRefund: true, refundUnavailableReason: null, paidAt: '2026-10-19T14:58:06Z', paidAmount: 2900 },
    orderRecord({ orderId: 62, status: '需人工处理', refundUnavailableReason: '订单当前状态不能自助退款，可点“遇到问题”告诉我们' }),
    { ...PENDING, orderId: 63, status: '需人工处理', refundUnavailableReason: '这笔付款需要人工核对，可点“遇到问题”告诉我们', paidAmount: 3000 },
    { ...PENDING, orderId: 64, status: '已关闭' },
    // 申请了全额退款、退款转回人工：写已申请退款、要人工核对（Codex 复核）
    { ...PENDING, orderId: 66, status: '需人工处理', paidAmount: 2900, refundAmount: 2900 },
    // 微信没确认收到付款就转了人工：不写"付款已收到"
    { ...PENDING, orderId: 67, status: '需人工处理', paidAmount: null },
  ]);
  await flush();
  assert.equal(h.calls.filter((call) => call[0] === 'check').length, 0);
  const undelivered = textOf(orderCard(h.page.data, 61));
  assert.match(undelivered, /人工核对中/);
  assert.doesNotMatch(undelivered, /需人工处理/);
  assert.match(undelivered, /付款已收到，但开通时出了问题，还没开通。我们 1 个工作日内处理；不想等可以直接申请退款，全额原路退回/);
  assert.equal(findAll(orderCard(h.page.data, 61), tapsTo('refund')).length, 1);
  assert.equal(findAll(orderCard(h.page.data, 61), tapsTo('problem')).length, 1);
  h.page.refund({ currentTarget: { dataset: { id: 61 } } });
  assert.equal(h.modal().content, '这笔付款还没开通使用期，申请后 29.00 元全额原路退回。确定吗？');
  h.modal().success({ confirm: false });
  const opened = textOf(orderCard(h.page.data, 62));
  assert.match(opened, /这笔订单需要人工核对，我们 1 个工作日内处理，结果会显示在这里/);
  assert.doesNotMatch(opened, /不能自助退款/);
  assert.equal(findAll(orderCard(h.page.data, 62), tapsTo('refund')).length, 0);
  const staff = textOf(orderCard(h.page.data, 63));
  assert.match(staff, /付款已收到，但开通时出了问题，还没开通。我们 1 个工作日内处理，结果会显示在这里/);
  assert.doesNotMatch(staff, /申请退款/);
  assert.match(textOf(orderCard(h.page.data, 66)), /已申请退款，退款需要人工核对，我们 1 个工作日内处理/);
  const unverified = textOf(orderCard(h.page.data, 67));
  assert.match(unverified, /这笔订单的付款情况需要人工核对/);
  assert.doesNotMatch(unverified, /付款已收到/);
  const closed = textOf(orderCard(h.page.data, 64));
  assert.match(closed, /没有确认到付款，订单已关闭。如果其实扣了款，点"遇到问题"告诉我们/);
  assert.equal(findAll(orderCard(h.page.data, 64), tapsTo('recheck')).length, 0);
  // 已关闭的也有带订单号的"遇到问题"：被关单的那笔其实扣了款时，反馈对得上订单（Codex 复核）
  const report = findAll(orderCard(h.page.data, 64), tapsTo('problem'));
  assert.equal(report.length, 1);
  h.page.problem({ currentTarget: { dataset: { id: report[0].attrs['data-id'] } } });
  assert.deepEqual(h.opened, ['/pages/feedback/index?type=pay&orderId=64']);
  // 已开通、已退款的没有
  assert.equal(findAll(render('pages/orders/index.wxml', { ...h.page.data, items: [pay.orderView(orderRecord({ orderId: 65 }))] }), tapsTo('problem')).length, 0);
});

function mePage(me, extra = {}) {
  const urls = [];
  const page = load('pages/me/index.js', {
    '../../content/legal.js': { final: true, version: '2026-10' },
    '../../utils/consent.js': { loginOpen: () => true, consentEpoch: () => 0, applyServer: () => extra.signedIn !== false, withdraw: () => Promise.resolve(false), markAgreed() {}, openPrivacy() {}, CONSENT_PAGE: '', agreeFailed() {}, featureNames: () => '' },
    '../../utils/subscribe.js': { remindOpen: () => false },
    '../../utils/coverage.js': { loadCoverage: () => Promise.resolve(null), notCoveredNames: () => '' },
    '../../utils/api.js': { isLive: () => true, me: () => Promise.resolve({ agreed: { version: '2026-10' }, follows: 0, subscriptions: {}, ...me() }),
      payProducts: () => Promise.resolve({ items: TIERS }), advisorStatus: () => Promise.reject(new Error('不该再读')) },
  }, { wx: { navigateTo: ({ url }) => urls.push(url), switchTab() {}, showToast() {} } });
  return { page, urls };
}

test('我的页头：批量工具使用期内名字后面有"批量工具 · 到某日"的标识（3 天内写"某日到期"），停卖了也照样显示；没开通、未登录、加载失败时没有；点了在售去购买页、不在售去订单页', async () => {
  const now = Date.now();
  const far = new Date(now + 30 * 86400000).toISOString();
  const soon = new Date(now + 2 * 86400000).toISOString();
  let me = { pay: { enabled: true, hasOrders: true }, pass: { active: true, expiresAt: far, expired: false } };
  const h = mePage(() => me);
  const badge = () => findAll(render('pages/me/index.wxml', h.page.data), tapsTo('openPass'));
  h.page.refresh(); await flush(); await flush();
  assert.equal(badge().length, 1);
  assert.equal(textOf(badge()[0]), require('../utils/pay.js').passBadge(me.pass));
  assert.match(textOf(badge()[0]), /^批量工具 · 到 \d+月\d+日$/);
  // "批量工具"一行的小字用 /me 带的使用期，不再另读额度接口
  assert.match(h.page.data.passHint, /^到 \d+月\d+日$/);
  h.page.openPass();
  assert.deepEqual(h.urls, ['/pages/pass/index']);
  me = { pay: { enabled: false, hasOrders: true }, pass: { active: true, expiresAt: soon, expired: false } };
  h.page.refresh(); await flush(); await flush();
  assert.match(textOf(badge()[0]), /^批量工具 · \d+月\d+日到期$/);
  h.page.openPass();
  assert.deepEqual(h.urls, ['/pages/pass/index', '/pages/orders/index']);
  // 只有送的使用期、没有订单、又没在卖：去工具中心，不会点了没反应（Codex 复核）
  me = { pay: { enabled: false, hasOrders: false }, pass: { active: true, expiresAt: far, expired: false } };
  h.page.refresh(); await flush(); await flush();
  assert.equal(badge().length, 1);
  h.page.openPass();
  assert.deepEqual(h.urls, ['/pages/pass/index', '/pages/orders/index', '/pages/advisor/index']);
  me = { pay: { enabled: true, hasOrders: true }, pass: { active: false, expiresAt: null, expired: true } };
  h.page.refresh(); await flush(); await flush();
  assert.equal(badge().length, 0);
  assert.equal(h.page.data.passHint, '已到期，可再买一次');
  // 只有待确认、已关闭的订单（从没开通过）：不写"已到期"，写价格
  me = { pay: { enabled: true, hasOrders: true }, pass: { active: false, expiresAt: null, expired: false } };
  h.page.refresh(); await flush(); await flush();
  assert.equal(h.page.data.passHint, '30 天 29 元起');
  const out = mePage(() => ({ pay: { enabled: true, hasOrders: true }, pass: { active: true, expiresAt: far, expired: false } }), { signedIn: false });
  out.page.refresh(); await flush(); await flush();
  assert.equal(findAll(render('pages/me/index.wxml', out.page.data), tapsTo('openPass')).length, 0);
  assert.doesNotMatch(shown('pages/me/index.wxml', out.page.data), /批量工具 · /);
});

test('开卖必做第 7 项：订单带协议版本才显示《付费服务协议》入口，版本号编进链接', async () => {
  const h = ordersPage([orderRecord({ termsVersion: 'v 1/2' }), orderRecord({ orderId: 42 })]);
  await flush();
  assert.equal(h.page.data.items[0].termsVersion, 'v 1/2');
  assert.equal(findAll(render('pages/orders/index.wxml', h.page.data), tapsTo('terms')).length, 1);
  h.page.terms({ currentTarget: { dataset: { version: 'v 1/2' } } });
  assert.equal(h.opened[0], '/pages/agreement/index?type=pay&version=v%201%2F2');
});

test('开卖必做第 7 项：协议页版本对不上时只提示、可点遇到问题；对得上或没带版本时可保存全文', () => {
  const writes = []; const shares = []; const toasts = []; const urls = [];
  const wx = { env: { USER_DATA_PATH: '/local' }, setNavigationBarTitle() {}, navigateTo: ({ url }) => urls.push(url), showToast: (v) => toasts.push(v.title),
    getFileSystemManager: () => ({ writeFile: (v) => { writes.push(v); v.success(); } }), shareFileMessage: (v) => shares.push(v) };
  const make = () => load('pages/agreement/index.js', { '../../content/legal.js': { final: true }, '../../content/pay-terms.js': FINAL_TERMS, '../../utils/api.js': {}, '../../utils/consent.js': { loginOpen: () => true, consentOpen: () => false } }, { wx });
  const old = make(); old.onLoad({ type: 'pay', version: 'old' });
  const text = shown('pages/agreement/index.wxml', old.data);
  assert.match(text, /这笔订单同意的是《付费服务协议》old 版/); assert.doesNotMatch(text, /购买批量工具 30 天/);
  old.feedback(); assert.equal(urls[0], '/pages/feedback/index?type=pay');
  for (const version of [undefined, FINAL_TERMS.version]) { const page = make(); page.onLoad({ type: 'pay', version }); page.savePay(); }
  assert.equal(writes[0].filePath, `/local/付费服务协议_${FINAL_TERMS.version}.txt`);
  assert.equal(writes[0].encoding, 'utf8'); assert.equal(writes[0].data, `《付费服务协议》（版本 ${FINAL_TERMS.version}）\n\n${FINAL_TERMS.text}`);
  shares[0].fail({ errMsg: 'shareFileMessage:fail cancel' }); assert.equal(toasts.length, 0);
  shares[0].fail({ errMsg: 'fail' }); assert.equal(toasts.pop(), '没能保存，请稍后再试');
  wx.shareFileMessage = undefined; make().savePay(); assert.equal(toasts.pop(), '当前微信版本不支持保存文件，请升级微信后再试');
  wx.shareFileMessage = () => {}; wx.getFileSystemManager = () => ({ writeFile: (v) => v.fail() }); make().savePay(); assert.equal(toasts.pop(), '没能保存，请稍后再试');
});

test('开卖必做第 4、5 项和收费方案第 9 项：购买页注销提示、续买赠品写品名、工具中心写怎么算次数', () => {
  const pass = fs.readFileSync(path.join(root, 'pages/pass/index.wxml'), 'utf8');
  assert.match(pass, /注销账号后，未到期的使用期和赠送天数作废，注销本身不退剩余天数，也不减少按《付费服务协议》第二条可以退的钱。/);
  assert.equal(pay.renewRule({ renewBonus: { days: 3, windowDays: 7 } }).text, '到期前后 7 天内续买，每次多送 3 天批量工具使用期');
  const advisor = fs.readFileSync(path.join(root, 'pages/advisor/index.wxml'), 'utf8');
  assert.match(advisor, /\{\{countNote\}\}/);
});

test('收费方案第 9 项：工具中心怎么算次数只写用得上的家数——没在卖不提批量工具，加量体验用不了不提它', () => {
  const page = load('pages/advisor/index.js', { '../../utils/api.js': {}, '../../utils/ai-access.js': {}, '../../utils/advisor.js': {}, '../../utils/tool-context.js': {}, '../../utils/boost.js': {}, '../../utils/daywatch.js': {}, '../../utils/consent.js': {} });
  const line = (quota, payOn, offer) => { page.quota = quota; page.payOn = payOn; page.offer = offer; return page.countLine(); };
  const free = (available) => ({ rows: { compare: 50, track: 50 }, pass: null, trial: { trialActive: false, trialAvailable: available } });
  assert.equal(line(free(false), false, null), '交一份名单算一次，系统出错、没出结果的不算；免费每次 50 家以内。');
  assert.equal(line(free(true), true, { line: '30 天 29 元' }), '交一份名单算一次，系统出错、没出结果的不算；免费每次 50 家以内，加量体验和批量工具使用期内每次 500 家以内。');
  assert.equal(line(free(false), true, { line: '30 天 29 元' }), '交一份名单算一次，系统出错、没出结果的不算；免费每次 50 家以内，批量工具使用期内每次 500 家以内。');
  assert.equal(line({ ...free(false), pass: { active: true } }, true, null), '交一份名单算一次，系统出错、没出结果的不算。');
  assert.equal(line(null, true, null), '交一份名单算一次，系统出错、没出结果的不算。');
});

test('审核规范 3.3.1：登录没开放时订单页、购买页回首页，跳转失败再重新启动，不出现暂未开放', () => {
  for (const file of ['orders', 'pass']) {
    const urls = []; const calls = [];
    const page = load(`pages/${file}/index.js`, { '../../utils/api.js': { payProducts: () => calls.push('products'), payOrders: () => calls.push('orders') }, '../../utils/consent.js': { loginOpen: () => false }, '../../content/pay-terms.js': FINAL_TERMS, '../../utils/data-tools.js': {} }, { wx: { switchTab: (v) => { urls.push(v.url); v.fail(); }, reLaunch: (v) => urls.push(v.url) } });
    page.onShow(); assert.deepEqual(urls, ['/pages/home/index', '/pages/home/index']); assert.deepEqual(calls, []);
    assert.equal(shown(`pages/${file}/index.wxml`, page.data).includes('暂未开放'), false);
  }
});

test('开卖必做第 3 项：两个注销入口都走 withdrawAccount', async () => {
  for (const file of ['me', 'my-data']) {
    let calls = 0;
    const page = load(`pages/${file}/index.js`, {
      '../../content/legal.js': {}, '../../utils/api.js': {},
      '../../utils/consent.js': { loginOpen: () => true, withdrawAccount: () => { calls += 1; return Promise.resolve(false); }, remindAny: () => false },
      '../../utils/coverage.js': {}, '../../utils/conditions.js': {}, '../../utils/subscribe.js': { remindOpen: () => false },
    }, { wx: {} });
    page[file === 'me' ? 'deleteAccount' : 'deleteAll'](); await flush();
    assert.equal(calls, 1);
  }
});

test('开卖必做第 3 项：注销前读使用期——包里没有订单页（过审版）不读也不提付费；读不到按有算；连点只进一次', async () => {
  const run = async (pagesInPack, me, taps = 1) => {
    const modals = []; const asked = [];
    const consent = load('utils/consent.js', { './config.js': {}, '../content/legal.js': { final: true, operator: { email: 'help@example.com' } },
      './api.js': { me: () => { asked.push('me'); return me(); }, deleteMe: () => Promise.resolve() }, './exports.js': { clear() {} }, './exporter.js': { removeFiles() {} },
      './ai-access.js': {}, './pages.js': { has: (name) => pagesInPack.includes(name) } },
    { wx: { showModal: (v) => { modals.push(v.content); v.success({ confirm: false }); }, showToast() {}, clearStorageSync() {}, removeStorageSync() {} } });
    const results = await Promise.all(Array.from({ length: taps }, () => consent.withdrawAccount()));
    return { modals, asked, results };
  };
  const review = await run([], () => Promise.resolve({ pass: { active: true } }));
  assert.deepEqual(review.asked, []); assert.equal(review.modals.length, 1);
  assert.doesNotMatch(review.modals.join(''), /付费|使用期/);
  const offline = await run(['orders'], () => Promise.reject(new Error('offline')));
  assert.deepEqual(offline.asked, ['me']);
  const active = await run(['orders'], () => Promise.resolve({ pass: { active: true } }), 3);
  assert.deepEqual(active.asked, ['me']); assert.deepEqual(active.results, [false, false, false]);
  const none = await run(['orders'], () => Promise.resolve({ pass: { active: false } }));
  assert.equal(none.modals.length, 1);
});
