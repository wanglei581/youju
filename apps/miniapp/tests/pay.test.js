const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const pay = require('../utils/pay.js');

const flush = async () => { for (let n = 0; n < 30; n += 1) await Promise.resolve(); };
const PRODUCTS = { items: [{ productId: 'compare_200', price: 1990, maxRows: 200 }, { productId: 'compare_100', price: 990, maxRows: 100 }] };
const ORDER = { orderId: 41, signData: '{"offerId":"offer-test"}', paySig: 'pay-sig', signature: 'user-sig', mode: 'short_series_goods' };
const BANNED = /最|保证|智能|签约|联系我们|分享|关注|转发/;

function clock() {
  let now = 1000000; let seq = 0; const queue = [];
  return {
    now: () => now,
    set: (fn, delay) => { seq += 1; queue.push({ id: seq, fn, delay, at: now + delay }); return seq; },
    clear: (id) => { const index = queue.findIndex((item) => item.id === id); if (index >= 0) queue.splice(index, 1); },
    pending: () => queue.length,
    delays: () => queue.map((item) => item.delay),
    async runNext() { const item = queue.shift(); now = item.at; item.fn(); await flush(); },
    advance(ms) { now += ms; },
  };
}

function formPage(api, wxOptions = {}, timers = clock()) {
  let definition;
  const helpers = {
    KINDS: { compare: '名单比对', track: '在报项目跟进表' },
    pasteRows: (kind, text) => text.split(/\r?\n/).map((s) => s.trim()).filter(Boolean),
    consent: () => Promise.resolve(true),
  };
  const calls = { redirect: [], modal: [], payment: [], login: 0 };
  const wx = Object.assign({
    setNavigationBarTitle() {}, navigateTo() {}, showToast() {},
    redirectTo: (options) => { calls.redirect.push(options.url); },
    showModal: (options) => { calls.modal.push(options); options.success({ confirm: true }); },
    login: (options) => { calls.login += 1; options.success({ code: 'login-code' }); },
    requestVirtualPayment: (options) => { calls.payment.push(options); options.success({}); },
  }, wxOptions);
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'pages', 'advisor-form', 'index.js'), 'utf8'), {
    require: (name) => name.endsWith('api.js') ? api : name.endsWith('/pay.js') ? pay : helpers,
    Page: (value) => { definition = value; },
    wx,
    setTimeout: timers.set, clearTimeout: timers.clear,
    Date: { now: timers.now }, Math, Promise, JSON, encodeURIComponent,
  });
  definition.setData = (next) => Object.assign(definition.data, next);
  return { view: definition, calls, timers, wx };
}

function apiWith(overrides = {}) {
  const calls = { orders: [], polls: 0 };
  const api = Object.assign({
    advisorStatus: () => Promise.resolve({ enabled: true, quota: { compare: { used: 1, limit: 1, maxRows: 50 }, track: { used: 0, limit: 1, maxRows: 50 } } }),
    payProducts: () => Promise.resolve(PRODUCTS),
    createPayOrder: (body) => { calls.orders.push(body); return Promise.resolve(ORDER); },
    payOrder: () => { calls.polls += 1; return Promise.resolve({ orderId: 41, status: '待支付', taskId: null }); },
    createAdvisorTask: () => Promise.reject(new Error('不应提交免费任务')),
  }, overrides);
  return { api, calls };
}

async function ready(api, wxOptions, kind = 'compare') {
  const page = formPage(api, wxOptions);
  page.view.onLoad({ kind }); page.view.onShow(); await flush();
  return page;
}

async function buy(page, text = '甲公司\n乙公司', productId = 'compare_100') {
  page.view.input({ detail: { value: text } });
  page.view.buy({ currentTarget: { dataset: { id: productId } } });
  await flush();
}

test('付费档只在接口返回可售商品时显示；404、失败、空列表和跟进表都和原来一样', async () => {
  for (const response of [
    Promise.reject(Object.assign(new Error('没有找到接口'), { status: 404 })),
    Promise.reject(new Error('网络不太好，请稍后再试')),
    Promise.resolve({ items: [] }), Promise.resolve({}), Promise.resolve(null),
    Promise.resolve({ items: [{ productId: 'compare_100', price: 0, maxRows: 100 }, { productId: '', price: 990, maxRows: 100 }, { productId: 'x', price: '9.9', maxRows: 100 }] }),
  ]) {
    response.catch(() => {});
    const { api } = apiWith({ payProducts: () => response });
    const { view } = await ready(api);
    view.input({ detail: { value: '甲公司' } });
    assert.deepEqual(view.data.pay, { visible: false });
  }
  let asked = 0;
  const { api } = apiWith({ payProducts: () => { asked += 1; return Promise.resolve(PRODUCTS); } });
  const track = await ready(api, {}, 'track');
  assert.deepEqual(track.view.data.pay, { visible: false }); assert.equal(asked, 0);
  assert.deepEqual(pay.payView({ products: [], kind: 'compare', mode: 'paste', rowCount: 1 }), { visible: false });
  // 模板里所有价格和付费字样都在 pay.visible 区块内。
  const wxml = fs.readFileSync(path.join(__dirname, '..', 'pages', 'advisor-form', 'index.wxml'), 'utf8');
  const block = /\n {2}<view wx:if="\{\{pay\.visible\}\}"[\s\S]*?\n {2}<\/view>/.exec(wxml);
  assert.ok(block);
  const outside = wxml.replace(block[0], '');
  assert.doesNotMatch(outside, /付费|价格|元|退款|buy|pay\.(?!busy)/);
  assert.doesNotMatch(block[0], BANNED);
});

test('有商品时按家数排序显示价格，超过档位、文件模式和未填写时不可选', async () => {
  const { api } = apiWith();
  const { view } = await ready(api);
  assert.equal(view.data.pay.visible, true);
  assert.equal(view.data.pay.hint, pay.TEXT.empty);
  assert.deepEqual(Array.from(view.data.pay.items, (item) => item.label), ['100 家以内 · 9.90 元', '200 家以内 · 19.90 元']);
  assert.ok(view.data.pay.items.every((item) => item.disabled));
  view.input({ detail: { value: '甲公司' } });
  assert.equal(view.data.pay.hint, '今天的免费次数已用完，可按次付费比对');
  assert.ok(view.data.pay.items.every((item) => !item.disabled));
  view.input({ detail: { value: Array.from({ length: 120 }, (_, n) => `公司${n}`).join('\n') } });
  assert.equal(view.data.pay.hint, '超过免费的每次 50 家，可按次付费比对');
  assert.deepEqual(Array.from(view.data.pay.items, (item) => item.disabled), [true, false]);
  view.input({ detail: { value: Array.from({ length: 201 }, (_, n) => `公司${n}`).join('\n') } });
  assert.equal(view.data.pay.hint, '超过 200 家，请减少客户后再付费比对');
  view.mode({ currentTarget: { dataset: { mode: 'file' } } });
  assert.equal(view.data.pay.hint, pay.TEXT.fileMode); assert.ok(view.data.pay.items.every((item) => item.disabled));
  assert.equal(pay.yuan(5), '0.05 元'); assert.equal(pay.yuan(100), '1.00 元'); assert.equal(pay.yuan(123456), '1234.56 元');
  const free = pay.payView({ products: pay.normalizeProducts(PRODUCTS), kind: 'compare', mode: 'paste', rowCount: 3, maxRows: 50, remaining: 1 });
  assert.equal(free.hint, '本次可免费提交；需要比对更多家时可按次付费');
  for (const text of [free.hint, free.notes, ...Object.values(pay.TEXT)]) assert.doesNotMatch(text, BANNED);
});

test('确认弹窗写清价格、家数、退款、保存期限和第三方说明；下单参数和支付参数来自后端', async () => {
  const { api, calls } = apiWith({ payOrder: () => Promise.resolve({ orderId: 41, status: '处理中', taskId: 9 }) });
  const page = await ready(api);
  await buy(page);
  const modal = page.calls.modal[0];
  assert.equal(modal.title, '确认付费比对');
  for (const phrase of ['价格 9.90 元', '覆盖 100 家以内', '本次 2 家', '比对失败自动退款', '结果保存 30 天', '整理的是公开信息，第三方工具，不承诺申报结果']) assert.ok(modal.content.includes(phrase), phrase);
  assert.doesNotMatch(modal.content + modal.title + modal.confirmText, BANNED);
  assert.deepEqual(JSON.parse(JSON.stringify(calls.orders)), [{ product_id: 'compare_100', rows: ['甲公司', '乙公司'], code: 'login-code' }]);
  const params = page.calls.payment[0];
  assert.deepEqual({ signData: params.signData, paySig: params.paySig, signature: params.signature, mode: params.mode }, { signData: ORDER.signData, paySig: 'pay-sig', signature: 'user-sig', mode: 'short_series_goods' });
  assert.equal(typeof params.signData, 'string');
  assert.deepEqual(page.calls.redirect, ['/pages/advisor-tasks/index?taskId=9']);
  assert.equal(page.view.data.pay.message, pay.TEXT.confirmed);
});

test('微信回调 success 但后端一直未支付：每 2 秒查一次，60 秒后提示未确认，不显示成功也不跳转', async () => {
  const { api, calls } = apiWith();
  const page = await ready(api);
  await buy(page);
  assert.equal(page.view.data.pay.message, pay.TEXT.confirming); assert.equal(page.view.data.pay.busy, true);
  assert.deepEqual(page.timers.delays(), [2000]);
  while (page.timers.pending()) await page.timers.runNext();
  assert.equal(calls.polls, 31);
  assert.equal(page.view.data.pay.message, pay.TEXT.timeoutPaid);
  assert.doesNotMatch(page.view.data.pay.message, /成功|已确认/);
  assert.equal(page.view.data.pay.busy, false); assert.deepEqual(page.calls.redirect, []);
});

test('先待支付后处理中才跳任务页；处理中但还没有任务编号时继续等', async () => {
  const states = [{ status: '待支付', taskId: null }, { status: '处理中', taskId: null }, { status: '处理中', taskId: 12 }];
  const { api } = apiWith({ payOrder: () => Promise.resolve(Object.assign({ orderId: 41 }, states.shift())) });
  const page = await ready(api);
  await buy(page);
  await page.timers.runNext(); assert.deepEqual(page.calls.redirect, []);
  await page.timers.runNext(); assert.deepEqual(page.calls.redirect, ['/pages/advisor-tasks/index?taskId=12']);
  assert.equal(page.timers.pending(), 0);
});

test('页面隐藏时停止轮询并忽略在途结果，回到页面后重新查；卸载后不再查', async () => {
  let resolvePoll; let polls = 0;
  const { api } = apiWith({ payOrder: () => { polls += 1; return new Promise((resolve) => { resolvePoll = resolve; }); } });
  const page = await ready(api);
  await buy(page);
  assert.equal(polls, 1);
  page.view.onHide();
  resolvePoll({ orderId: 41, status: '处理中', taskId: 3 }); await flush();
  assert.deepEqual(page.calls.redirect, []); assert.equal(page.timers.pending(), 0);
  assert.equal(page.view.data.pay.message, pay.TEXT.confirming);
  page.view.onShow(); await flush();
  assert.equal(polls, 2);
  resolvePoll({ orderId: 41, status: '待支付', taskId: null }); await flush();
  assert.equal(page.timers.pending(), 1);
  page.view.onUnload(); assert.equal(page.timers.pending(), 0);
  page.view.onShow(); await flush(); assert.equal(polls, 2);
});

test('支付回调发生在页面隐藏期间时，回到页面再开始查订单', async () => {
  let pending;
  const { api, calls } = apiWith();
  const page = await ready(api, { requestVirtualPayment: (options) => { pending = options; } });
  await buy(page);
  page.view.onHide();
  pending.success({}); await flush();
  assert.equal(calls.polls, 0);
  page.view.onShow(); await flush();
  assert.equal(calls.polls, 1);
});

test('用户取消显示“已取消支付”，只核对一次订单；核对到已付款时仍以后端为准', async () => {
  const cancel = (options) => options.fail({ errMsg: 'requestVirtualPayment:fail cancel', errCode: -2 });
  const first = apiWith();
  const page = await ready(first.api, { requestVirtualPayment: cancel });
  await buy(page);
  assert.equal(first.calls.polls, 1); assert.equal(page.timers.pending(), 0);
  assert.equal(page.view.data.pay.message, '已取消支付'); assert.equal(page.view.data.pay.error, false);
  assert.equal(page.view.data.pay.busy, false); assert.deepEqual(page.calls.redirect, []);
  const paid = apiWith({ payOrder: () => Promise.resolve({ orderId: 41, status: '处理中', taskId: 5 }) });
  const late = await ready(paid.api, { requestVirtualPayment: cancel });
  await buy(late);
  assert.deepEqual(late.calls.redirect, ['/pages/advisor-tasks/index?taskId=5']);
  assert.equal(pay.isCancel({ errMsg: 'requestVirtualPayment:fail cancel' }), true);
  assert.equal(pay.isCancel({ errMsg: 'requestVirtualPayment:fail', errCode: -1 }), false);
});

test('支付失败（非取消）仍查订单，超时后提示如已扣款到我的任务查看', async () => {
  const { api, calls } = apiWith();
  const page = await ready(api, { requestVirtualPayment: (options) => options.fail({ errMsg: 'requestVirtualPayment:fail', errCode: -1 }) });
  await buy(page);
  while (page.timers.pending()) await page.timers.runNext();
  assert.equal(calls.polls, 31);
  assert.equal(page.view.data.pay.message, pay.TEXT.timeoutFailed);
});

test('退款、人工、关闭显示后端状态文字；查单失败继续重试', async () => {
  for (const status of ['已退款', '需人工处理', '已关闭']) {
    const answers = [() => Promise.reject(new Error('网络不太好，请稍后再试')), () => Promise.resolve({ orderId: 41, status, taskId: 8 })];
    let polls = 0;
    const { api } = apiWith({ payOrder: () => { polls += 1; return answers.shift()(); } });
    const page = await ready(api);
    await buy(page);
    await page.timers.runNext();
    assert.equal(polls, 2);
    assert.equal(page.view.data.pay.message, `订单状态：${status}`);
    assert.equal(page.view.data.pay.error, true); assert.equal(page.view.data.pay.busy, false);
    assert.deepEqual(page.calls.redirect, []);
  }
  assert.equal(pay.classify({ status: '已完成', taskId: 3 }), 'success');
  assert.equal(pay.classify({ status: '处理中', taskId: null }), 'pending');
  assert.equal(pay.classify({ status: '待支付', taskId: null }), 'pending');
  assert.equal(pay.classify({ status: '未知状态', taskId: 1 }), 'pending');
  assert.deepEqual(pay.resultView({ state: 'timeout', order: null }, { paid: true }).message, pay.TEXT.timeoutPaid);
});

test('下单失败显示后端原因；未同意、登录失败、微信版本不支持时不下单', async () => {
  const rejected = apiWith({ createPayOrder: () => Promise.reject(Object.assign(new Error('待支付订单过多，请稍后再试'), { status: 429 })) });
  const page = await ready(rejected.api);
  await buy(page);
  assert.equal(page.view.data.pay.message, '待支付订单过多，请稍后再试'); assert.equal(page.view.data.pay.error, true);
  assert.equal(page.view.data.pay.busy, false); assert.equal(page.calls.payment.length, 0);
  const consent = apiWith({ createPayOrder: () => Promise.reject(Object.assign(new Error('请先阅读并同意工具使用说明'), { status: 403 })) });
  const revoked = await ready(consent.api);
  await buy(revoked); assert.equal(revoked.view.data.agreed, false);
  let opened = 0;
  const unagreed = await ready(apiWith().api, { navigateTo: () => { opened += 1; } });
  unagreed.view.setData({ agreed: false }); await buy(unagreed);
  assert.equal(opened, 1); assert.equal(unagreed.calls.modal.length, 0);
  const login = apiWith();
  const noLogin = await ready(login.api, { login: (options) => options.fail({ errMsg: 'login:fail' }) });
  await buy(noLogin);
  assert.equal(noLogin.view.data.pay.message, pay.TEXT.login); assert.equal(login.calls.orders.length, 0);
  const old = apiWith();
  const unsupported = await ready(old.api, { requestVirtualPayment: undefined });
  await buy(unsupported);
  assert.equal(unsupported.view.data.pay.message, pay.TEXT.unsupported);
  assert.equal(unsupported.calls.modal.length, 0); assert.equal(old.calls.orders.length, 0);
  const over = apiWith();
  const tooMany = await ready(over.api);
  await buy(tooMany, Array.from({ length: 101 }, (_, n) => `公司${n}`).join('\n'), 'compare_100');
  assert.match(tooMany.view.data.pay.message, /覆盖 100 家以内/); assert.equal(over.calls.orders.length, 0);
});

test('支付进行中不能重复下单，也不能同时提交免费任务或切换输入方式', async () => {
  let pending;
  const { api, calls } = apiWith();
  const page = await ready(api, { requestVirtualPayment: (options) => { pending = options; } });
  await buy(page);
  page.view.buy({ currentTarget: { dataset: { id: 'compare_200' } } }); await flush();
  assert.equal(calls.orders.length, 1); assert.equal(page.calls.modal.length, 1);
  page.view.setData({ agreed: true, rowCount: 1 }); page.view.submit();
  page.view.mode({ currentTarget: { dataset: { mode: 'file' } } }); assert.equal(page.view.data.mode, 'paste');
  assert.equal(page.view.data.pay.items[0].loading, true);
  // 支付进行中重新显示页面不刷新商品，付费区块不会消失。
  let refreshed = 0; api.payProducts = () => { refreshed += 1; return Promise.reject(new Error('没有找到接口')); };
  page.view.onShow(); await flush(); assert.equal(refreshed, 0); assert.equal(page.view.data.pay.visible, true);
  assert.ok(pending);
});

test('演示数据不出售：商品为空，下单和查单被拒绝', async () => {
  const config = require('../utils/config.js');
  const original = { cloudEnv: config.cloudEnv, devBaseUrl: config.devBaseUrl };
  global.wx = global.wx || { getStorageSync() {}, setStorageSync() {} };
  config.cloudEnv = ''; config.devBaseUrl = '';
  try {
    const api = require('../utils/api.js');
    assert.deepEqual(await api.payProducts(), { items: [] });
    await assert.rejects(api.createPayOrder({ product_id: 'compare_100', rows: ['甲'], code: 'c' }), /演示数据不能支付/);
    await assert.rejects(api.payOrder(1), /演示数据不能支付/);
    config.devBaseUrl = 'http://test'; let request;
    global.wx.request = (options) => { request = options; options.success({ statusCode: 201, data: ORDER }); };
    await api.createPayOrder({ product_id: 'compare_100', rows: ['甲'], code: 'c' });
    assert.equal(request.method, 'POST'); assert.equal(request.url, 'http://test/api/v1/pay/orders');
    assert.deepEqual(request.data, { product_id: 'compare_100', rows: ['甲'], code: 'c' });
    global.wx.request = (options) => { request = options; options.success({ statusCode: 200, data: { items: [] } }); };
    await api.payOrder(41); assert.equal(request.url, 'http://test/api/v1/pay/orders/41'); assert.equal(request.method, 'GET');
    await api.payProducts(); assert.equal(request.url, 'http://test/api/v1/pay/products');
  } finally { Object.assign(config, original); }
});

test('轮询器：stop 后在途结果忽略；start(true) 只查一次', async () => {
  const timers = clock(); let resolve; const results = [];
  const poller = pay.createPoller({ fetch: () => new Promise((r) => { resolve = r; }), onDone: (result) => results.push(result), timers, now: timers.now });
  poller.start(); await flush(); assert.equal(poller.isActive(), true);
  poller.stop(); resolve({ status: '已完成', taskId: 1 }); await flush();
  assert.deepEqual(results, []); assert.equal(poller.isActive(), false); assert.equal(timers.pending(), 0);
  poller.start(true); await flush(); resolve({ status: '待支付', taskId: null }); await flush();
  assert.equal(results[0].state, 'timeout'); assert.equal(results[0].order.status, '待支付'); assert.equal(timers.pending(), 0);
});
