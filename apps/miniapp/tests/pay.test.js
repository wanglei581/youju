const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const pay = require('../utils/pay.js');
const { render, textOf, findAll, tapsTo } = require('./wxml-render.js');
const ROOT = path.join(__dirname, '..');
const flush = async () => { for (let n = 0; n < 24; n += 1) await Promise.resolve(); };
const PRODUCT = { productId: 'workbench_30d', price: 2900, days: 30 };
const ORDER = { orderId: 41, amount: 2900, status: '已完成', paidAt: '2026-10-09T01:00:00Z', passStartsAt: '2026-10-09T01:00:00Z', passEndsAt: '2026-11-08T01:00:00Z', passRevokedAt: null, canRefund: true, refundUnavailableReason: null };
function status(used = 1, active = false, expiresAt = ORDER.passEndsAt) {
  return { enabled: true, quota: { compare: { used, limit: 3, maxRows: active ? 500 : 50 }, track: { used, limit: 3, maxRows: active ? 500 : 50 } }, pass: { active, expiresAt: active ? expiresAt : null } };
}
function clock() {
  let now = Date.parse('2026-10-09T01:00:00Z'); let serial = 0; const tasks = new Map();
  class Clock extends Date { static now() { return now; } }
  return { Clock, now: () => now, set: (fn, ms) => { const id = ++serial; tasks.set(id, { fn, at: now + ms }); return id; }, clear: (id) => tasks.delete(id), size: () => tasks.size,
    async advance(ms) {
      const end = now + ms;
      for (;;) {
        const next = [...tasks].filter(([, value]) => value.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!next) break;
        now = next[1].at; tasks.delete(next[0]); next[1].fn(); await flush();
      }
      now = end; await flush();
    },
  };
}
function page(name, overrides = {}, options = {}) {
  const timers = clock(); let view;
  const calls = { orders: [], polls: 0, payments: [], urls: [], toasts: [], modals: [], tasks: [], feedback: [], refunds: [], lists: 0 };
  const api = {
    payProducts: () => Promise.resolve({ items: [PRODUCT] }), advisorStatus: () => Promise.resolve(status()),
    createPayOrder: (body) => { calls.orders.push(body); return Promise.resolve({ orderId: 41, signData: 'data', paySig: 'sig', signature: 'user', mode: pay.PAY_MODE }); },
    payOrder: () => { calls.polls += 1; return Promise.resolve({ status: '待支付' }); },
    payOrders: () => { calls.lists += 1; return Promise.resolve({ items: [ORDER] }); },
    refundPayOrder: (id) => { calls.refunds.push(id); return Promise.resolve({ ...ORDER, status: '退款中' }); },
    createAdvisorTask: (body) => { calls.tasks.push(body); return Promise.resolve({ id: 9 }); },
    advisorPreview: () => Promise.resolve({ rowCount: 120 }), advisorTasks: () => Promise.resolve({ items: [] }),
    feedback: (id, content) => { calls.feedback.push({ id, content }); return Promise.resolve({}); }, ...overrides,
  };
  const deps = {
    '../../utils/api.js': api, '../../utils/pay.js': pay,
    '../../utils/advisor.js': { ...require('../utils/advisor.js'), consent: () => Promise.resolve(options.consent !== false) },
    '../../content/pay-terms.js': { final: options.final !== false, text: options.termsText || '正式正文' }, '../../content/legal.js': { final: true },
    '../../utils/consent.js': { loginOpen: () => options.loginOpen !== false, ensureConsent: () => Promise.resolve(true) },
    '../../utils/tool-context.js': { read: () => null }, '../../utils/subscribe.js': { remindOpen: () => false }, '../../utils/coverage.js': {},
  };
  const wx = {
    setNavigationBarTitle: ({ title }) => { calls.title = title; }, navigateTo: ({ url }) => calls.urls.push(url), redirectTo: ({ url }) => calls.urls.push(url),
    showToast: (value) => calls.toasts.push(value), showModal: (value) => { calls.modals.push(value); value.success({ confirm: options.confirm !== false }); },
    login: (value) => value.success({ code: 'code' }), requestVirtualPayment: (value) => { calls.payments.push(value); if (options.cancel) value.fail({ errMsg: 'requestVirtualPayment:fail cancel' }); else if (options.fail) value.fail({ errCode: -1 }); else value.success({}); }, ...options.wx,
  };
  vm.runInNewContext(fs.readFileSync(path.join(ROOT, 'pages', name, 'index.js'), 'utf8'), {
    require: (key) => { assert.ok(deps[key], `缺少依赖 ${key}`); return deps[key]; }, Page: (value) => { view = value; }, wx, Date: timers.Clock, Math, Promise, setTimeout: timers.set, clearTimeout: timers.clear,
  });
  view.setData = (values) => Object.assign(view.data, values);
  const show = () => render(`pages/${name}/index.wxml`, view.data);
  return { view, calls, api, timers, show, text: () => textOf(show()) };
}
async function purchase(overrides, options) { const p = page('pass', overrides, options); p.view.onShow(); await flush(); return p; }
async function buy(p) { p.view.check({ detail: { value: ['agree'] } }); p.view.buy(); await flush(); }
const event = (id) => ({ currentTarget: { dataset: { id } } });
const BANNED = /试用|订阅|自动续费|充值|余额|原价|立省|AI|智能|推荐|匹配度/;

for (const [name, response] of [
  ['404', () => Promise.reject(Object.assign(new Error('未开放'), { status: 404 }))], ['空商品', () => Promise.resolve({ items: [] })], ['无价格', () => Promise.resolve({ items: [{ ...PRODUCT, price: null }] })],
]) test(`购买页${name}：没有价格和支付按钮`, async () => {
  const p = await purchase({ payProducts: response }); assert.match(p.text(), /付费服务暂未开放/); assert.doesNotMatch(p.text(), /29|支付 .*元/);
  assert.equal(findAll(p.show(), tapsTo('buy')).length, 0); await buy(p); assert.equal(p.calls.orders.length, 0);
});
for (const options of [{ final: false }, { termsText: '【待律师定稿】' }, { loginOpen: false }]) test(`购买页协议门槛 ${JSON.stringify(options)}`, async () => {
  const p = await purchase({}, options); assert.match(p.text(), /付费服务暂未开放/); assert.equal(findAll(p.show(), tapsTo('buy')).length, 0); await buy(p); assert.equal(p.calls.orders.length, 0);
});
test('购买完整文字，动态价格、天数、北京时间日期；默认未勾选，点击提示 P17', async () => {
  const p = await purchase({ payProducts: () => Promise.resolve({ items: [{ ...PRODUCT, price: 3105, days: 28 }] }), advisorStatus: () => Promise.resolve(status(1, true, '2026-11-08T17:00:00Z')) });
  for (const text of ['客户工作台 30 天', '31.05元', '买一次用 28 天，到期不再扣钱', '使用期到 2026-11-09 · 再买一次，往后顺延 28 天', '包含什么', '批量名单比对不限次数，每次 500 家以内', '客户相关通知表不限次数，每次 500 家以内', '结果可导出 Excel带发文机关、日期和原文链接', '不买也能用', '批量名单比对、客户相关通知表：每月各 3 次，每次 50 家以内', '搜索、详情、附件、查名单、导出等一直免费', '购买前请知悉', '使用期从付款成功时起算，共 28 天', '只收这一次的钱，到期后不会再扣款。到期后，已经生成的结果仍可查看和导出。', '我已阅读并同意《付费服务协议》', '支付 31.05 元', '遇到问题']) assert.ok(p.text().includes(text), text);
  assert.doesNotMatch(p.text(), BANNED); assert.equal(p.view.data.checked, false); p.view.buy(); await flush(); assert.equal(p.calls.toasts.at(-1).title, pay.TEXT.termsRequired); assert.equal(p.calls.orders.length, 0);
});
test('工具说明未同意先引导，不下单', async () => {
  const p = await purchase({}, { consent: false }); await buy(p); assert.equal(p.calls.orders.length, 0); assert.equal(p.calls.urls.at(-1), '/pages/advisor-terms/index'); assert.equal(p.view.data.busy, false);
});
test('服务端确认开通，P19 和工具按钮；下单不含名单，不自动创建任务', async () => {
  const p = await purchase({ payOrder: () => Promise.resolve(ORDER), advisorStatus: () => Promise.resolve(status(1, true)) }); await buy(p);
  assert.deepEqual(JSON.parse(JSON.stringify(p.calls.orders)), [{ product_id: PRODUCT.productId, code: 'code' }]);
  assert.deepEqual(Object.fromEntries(['mode', 'signData', 'paySig', 'signature'].map((key) => [key, p.calls.payments[0][key]])), { mode: pay.PAY_MODE, signData: 'data', paySig: 'sig', signature: 'user' });
  assert.match(p.text(), /已开通，使用期到 2026-11-08/); assert.equal(findAll(p.show(), tapsTo('tools')).length, 1); assert.equal(p.calls.tasks.length, 0);
  p.view.tools(); assert.equal(p.calls.urls.at(-1), '/pages/advisor/index'); p.view.feedback(); assert.equal(p.calls.urls.at(-1), '/pages/feedback/index?type=pay&orderId=41');
});
for (const options of [{}, { fail: true }, { cancel: true }]) test(`支付回调 ${JSON.stringify(options)} 都持续查单，取消不算错误`, async () => {
  let checked = 0; const p = await purchase({ payOrder: () => Promise.resolve(++checked > 1 ? ORDER : { status: '待支付' }) }, options); await buy(p);
  assert.equal(p.view.data.confirmed, false); assert.equal(p.view.data.busy, true); if (options.cancel) { assert.equal(p.view.data.message, '已取消支付'); assert.equal(p.view.data.payError, false); }
  await p.timers.advance(2000); assert.equal(p.view.data.confirmed, true); assert.equal(checked, 2);
});
for (const hanging of [false, true]) test(`60 秒未确认显示 P20，请求悬挂=${hanging}`, async () => {
  const p = await purchase(hanging ? { payOrder: () => new Promise(() => {}) } : {}); await buy(p); await p.timers.advance(59999); assert.equal(p.view.data.busy, true);
  await p.timers.advance(1); assert.equal(p.view.data.message, pay.TEXT.timeout); assert.equal(p.view.data.busy, false); assert.equal(p.view.data.payError, false); assert.equal(p.timers.size(), 0);
});
test('隐藏停止并忽略在途结果，恢复沿用截止时间；卸载不更新', async () => {
  let resolve; const p = await purchase({ payOrder: () => new Promise((done) => { resolve = done; }) }); await buy(p); p.view.onHide(); resolve(ORDER); await flush(); assert.equal(p.view.data.confirmed, false); assert.equal(p.timers.size(), 0);
  await p.timers.advance(30000); p.view.onShow(); await flush(); await p.timers.advance(30000); assert.equal(p.view.data.message, pay.TEXT.timeout);
  const q = await purchase({ payOrder: () => new Promise((done) => { resolve = done; }) }); await buy(q); q.view.onUnload(); resolve(ORDER); await flush(); assert.equal(q.view.data.confirmed, false); assert.equal(q.timers.size(), 0);
});
test('隐藏时收到回调，返回查单；重复点击不重复下单', async () => {
  let callback; const p = await purchase({ payOrder: () => Promise.resolve(ORDER) }, { wx: { requestVirtualPayment: (value) => { callback = value; } } }); await buy(p); p.view.buy(); await flush(); assert.equal(p.calls.orders.length, 1);
  p.view.onHide(); callback.success(); await flush(); assert.equal(p.view.data.confirmed, false); p.view.onShow(); await flush(); assert.equal(p.view.data.confirmed, true);
});
test('下单失败、登录失败、不支持支付均恢复操作', async () => {
  for (const [overrides, options, message] of [[{ createPayOrder: () => Promise.reject(new Error('下单失败')) }, {}, '下单失败'], [{}, { wx: { login: (o) => o.fail({}) } }, pay.TEXT.login], [{}, { wx: { requestVirtualPayment: undefined } }, pay.TEXT.unsupported]]) {
    const p = await purchase(overrides, options); await buy(p); assert.equal(p.view.data.message, message); assert.equal(p.view.data.busy, false);
  }
});
for (const kind of ['compare', 'track']) {
  for (const [used, count, code, split] of [[0, 120, 'FREE_ROW_LIMIT', true], [3, 1, 'FREE_MONTHLY_LIMIT', false], [3, 120, 'FREE_ROW_LIMIT', false]]) test(`${kind} 本地预判 ${code} 不提交`, async () => {
    const p = page('advisor-form', { advisorStatus: () => Promise.resolve(status(used)) }); p.view.onLoad({ kind }); p.view.onShow(); await flush(); p.view.setData({ rowCount: count, text: '甲公司' }); p.view.submit(); await flush();
    assert.equal(p.calls.tasks.length, 0); assert.equal(p.view.data.limitHint.message, code === 'FREE_ROW_LIMIT' ? `这份名单有 ${count} 家，超过了免费的每次 50 家。` : `本月 3 次免费${kind === 'compare' ? '批量名单比对' : '客户相关通知表'}已用完，下月 1 日恢复。`);
    assert.equal(p.view.data.limitHint.split, split); assert.ok(p.text().includes('客户工作台 30 天（29 元）：不限次数，每次 500 家以内。')); p.view.buy(); assert.equal(p.calls.urls.at(-1), '/pages/pass/index');
  });
  for (const code of ['FREE_ROW_LIMIT', 'FREE_MONTHLY_LIMIT']) test(`${kind} 服务端错误 ${code} 显示指定文案`, async () => {
    const p = page('advisor-form', { createAdvisorTask: () => Promise.reject(Object.assign(new Error('原始文字'), { status: 429, code })) }); p.view.onLoad({ kind }); p.view.onShow(); await flush(); p.view.setData({ rowCount: 1, text: kind === 'track' ? '甲公司,辽宁' : '甲公司' }); p.view.submit(); await flush();
    assert.ok(p.view.data.limitHint); assert.doesNotMatch(p.text(), /原始文字/); assert.equal(p.view.data.submitting, false);
  });
}
test('有效使用期内 500 家可提交，501 家本地拦截，不消耗免费额度', async () => {
  for (const count of [500, 501]) {
    const p = page('advisor-form', { advisorStatus: () => Promise.resolve(status(3, true)) }); p.view.onLoad({}); p.view.onShow(); await flush(); p.view.input({ detail: { value: '甲公司\n'.repeat(count) } }); p.view.submit(); await flush();
    assert.equal(p.calls.tasks.length, count === 500 ? 1 : 0); assert.equal(p.view.data.limitHint, null); if (count > 500) assert.match(p.view.data.inputError, /500/);
  }
});
test('文件预览触发行数预判；关闭商品时不捏造价格、购买入口', async () => {
  const p = page('advisor-form', { payProducts: () => Promise.resolve({ items: [] }) }); p.view.onLoad({}); p.view.onShow(); await flush(); p.view.file = {}; p.view.setData({ mode: 'file' }); p.view.preview(); await flush();
  assert.equal(p.view.data.rowCount, 120); assert.match(p.text(), /这份名单有 120 家/); assert.equal(findAll(p.show(), tapsTo('buy')).length, 0);
});
for (const [name, res, expected] of [['未买', status(1), '本功能每月免费 3 次，每次 50 家以内 · 本月剩余 2 次'], ['用完', status(3), '本功能每月免费 3 次，每次 50 家以内 · 本月剩余 0 次'], ['已买', status(3, true), '客户工作台使用期到 11-08 · 不限次数，每次 500 家以内'], ['临到期', status(3, true, new Date(Date.now() + 2 * 86400000).toISOString()), null]]) test(`工具首页${name}，两张卡状态和一次 T2`, async () => {
  const p = page('advisor', { advisorStatus: () => Promise.resolve(res) }); p.view.onShow(); await flush(); if (expected) assert.equal(p.view.data.skills.filter((s) => s.statusLine === expected).length, 2);
  assert.equal(findAll(p.show(), tapsTo('buy')).length, res.pass.active ? 0 : 1); assert.doesNotMatch(p.text(), /今天剩余/); if (name === '临到期') assert.match(p.text(), /客户工作台 .* 到期，到期不再扣钱；需要可以再买一次/);
});
test('商品关闭隐藏 T2；3 天提醒边界；到期回免费行', async () => {
  const p = page('advisor', { payProducts: () => Promise.reject(Object.assign(new Error('关闭'), { status: 404 })) }); p.view.onShow(); await flush(); assert.equal(p.view.data.showPassLink, false);
  const now = Date.now(); assert.ok(pay.toolStatus(status(0, true, new Date(now + 3 * 86400000).toISOString()), 'track', now).notice); assert.equal(pay.toolStatus(status(0, true, new Date(now + 3 * 86400000 + 1).toISOString()), 'track', now).notice, ''); assert.match(pay.toolStatus(status(), 'track').line, /本月剩余 2 次/);
});
for (const [reason, text] of [['已超过 7 天', '已超过 7 天'], ['已提交过超过 50 家的名单或已超出每月免费次数', '已提交过超过 50 家的名单或已超出每月免费次数'], ['苹果手机付款的订单请在 App Store 申请退款', '苹果手机付款的订单请在 App Store 申请退款'], ['订单当前状态不能自助退款，可点“遇到问题”告诉我们', '订单当前状态不能自助退款，可点“遇到问题”告诉我们']]) test(`订单不可退：${text}`, async () => {
  const p = page('orders', { payOrders: () => Promise.resolve({ items: [{ ...ORDER, canRefund: false, refundUnavailableReason: reason }] }) }); p.view.onShow(); await flush(); assert.ok(p.text().includes(text)); assert.equal(findAll(p.show(), tapsTo('refund')).length, 0); assert.equal(findAll(p.show(), tapsTo('invoice')).length, 1); p.view.refund(event(41)); assert.equal(p.calls.modals.length, 0);
});
test('订单可退：O1、O4、退款成功 O5 刷新；发票和问题带订单号', async () => {
  const p = page('orders'); p.view.onShow(); await flush(); for (const text of ['客户工作台 30 天已开通', '29.00 元 · 2026-10-09 付款', '使用期 10-09 至 11-08', '订单号 41']) assert.ok(p.text().includes(text), text); assert.equal(p.view.data.items[0].summary, '客户工作台 30 天 · 29.00 元 · 2026-10-09 付款 · 已开通 · 使用期 10-09 至 11-08'); assert.equal(findAll(p.show(), tapsTo('refund')).length, 1);
  p.view.refund(event(41)); await flush(); assert.equal(p.calls.modals[0].content, '退款后，这张订单对应的 30 天使用期取消，29.00 元原路退回。确定吗？'); assert.deepEqual(p.calls.refunds, [41]); assert.equal(p.calls.lists, 2); assert.ok(p.text().includes(pay.TEXT.refund));
  p.view.invoice(event(41)); assert.equal(p.calls.urls.at(-1), '/pages/feedback/index?type=invoice&orderId=41'); p.view.feedback(); assert.equal(p.calls.urls.at(-1), '/pages/feedback/index?type=pay&orderId=41');
});
test('退款取消不发请求；退款失败重新刷新资格', async () => {
  const p = page('orders', {}, { confirm: false }); p.view.onShow(); await flush(); p.view.refund(event(41)); assert.equal(p.calls.refunds.length, 0); assert.equal(p.view.data.refunding, null);
  const q = page('orders', { refundPayOrder: () => Promise.reject(new Error('已超过 7 天')) }); q.view.onShow(); await flush(); q.view.refund(event(41)); await flush(); assert.equal(q.calls.lists, 2); assert.match(q.text(), /已超过 7 天/);
});
test('订单空列表和网络错误重试；问题无单号', async () => {
  const p = page('orders', { payOrders: () => Promise.resolve({ items: [] }) }); p.view.onShow(); await flush(); assert.match(p.text(), /还没有订单/); p.view.feedback(); assert.equal(p.calls.urls.at(-1), '/pages/feedback/index?type=pay');
  const q = page('orders', { payOrders: () => Promise.reject(new Error('网络错误')) }); q.view.onShow(); await flush(); assert.equal(findAll(q.show(), tapsTo('load')).length, 1);
});
for (const [open, hasOrders] of [[false, false], [true, false], [false, true], [true, true]]) test(`我的订单入口：可售=${open}、有历史订单=${hasOrders}`, async () => {
  const p = page('me', { payProducts: () => open ? Promise.resolve({ items: [PRODUCT] }) : Promise.reject(Object.assign(new Error('关闭'), { status: 404 })), payOrders: () => Promise.resolve({ items: hasOrders ? [ORDER] : [] }) }); p.view.loadOrdersEntry(); await flush(); assert.equal(p.view.data.showOrders, open || hasOrders); assert.equal(findAll(p.show(), tapsTo('orders')).length, open || hasOrders ? 1 : 0);
});
for (const [type, title] of [['pay', '支付或订单遇到的问题'], ['invoice', '申请发票：请写发票抬头和税号']]) test(`反馈 ${type} 标题占位、订单前缀和总字数`, async () => {
  const p = page('feedback'); p.view.onLoad({ type, orderId: '41', id: '99' }); assert.equal(p.calls.title, title); assert.equal(findAll(p.show(), (n) => n.tag === 'textarea')[0].attrs.placeholder, title); assert.equal(p.view.data.max, 500 - '【订单 41】'.length);
  p.view.onInput({ detail: { value: 'x'.repeat(500) } }); p.view.submit(); await flush(); assert.equal(p.calls.feedback[0].content.length, 500); assert.ok(p.calls.feedback[0].content.startsWith('【订单 41】')); assert.equal(p.calls.feedback[0].id, '');
});
test('支付反馈无单号不加前缀，覆盖反馈保留前缀', async () => {
  for (const [query, prefix] of [[{ type: 'pay' }, ''], [{ type: 'coverage' }, '申请覆盖：']]) { const p = page('feedback'); p.view.onLoad(query); p.view.onInput({ detail: { value: '测试' } }); p.view.submit(); await flush(); assert.equal(p.calls.feedback[0].content, prefix + '测试'); }
});
test('真实 API 支付路径和请求体，保留后端结构化错误码；演示禁付', async () => {
  const config = require('../utils/config.js'); const api = require('../utils/api.js'); const original = { cloudEnv: config.cloudEnv, devBaseUrl: config.devBaseUrl }; const originalWx = global.wx; let request; let response = { statusCode: 200, data: {} };
  global.wx = { request: (options) => { request = options; options.success(response); } }; config.cloudEnv = ''; config.devBaseUrl = 'http://test';
  try {
    await api.payProducts(); assert.equal(request.url, 'http://test/api/v1/pay/products'); await api.createPayOrder({ product_id: PRODUCT.productId, code: 'code' }); assert.equal(request.method, 'POST'); assert.deepEqual(request.data, { product_id: PRODUCT.productId, code: 'code' });
    await api.payOrder(41); assert.equal(request.url, 'http://test/api/v1/pay/orders/41'); await api.payOrders(); assert.equal(request.url, 'http://test/api/v1/pay/orders'); await api.refundPayOrder(41); assert.equal(request.method, 'POST'); assert.equal(request.url, 'http://test/api/v1/pay/orders/41/refund');
    for (const code of ['FREE_ROW_LIMIT', 'FREE_MONTHLY_LIMIT', 'PASS_ROW_LIMIT']) { response = { statusCode: 429, data: { detail: { code, message: '额度不足' } } }; await assert.rejects(api.createAdvisorTask({ kind: 'compare', rows: ['甲'] }, 'key'), (err) => err.status === 429 && err.code === code && err.message === '额度不足'); }
    config.devBaseUrl = ''; assert.deepEqual(await api.payProducts(), { items: [] }); assert.deepEqual(await api.payOrders(), { items: [] }); await assert.rejects(api.createPayOrder({}), /不能支付/); await assert.rejects(api.refundPayOrder(41), /不能支付/);
  } finally { Object.assign(config, original); global.wx = originalWx; }
});
test('新页面注册、标签闭合；旧按次包文字逻辑删除', () => {
  const app = JSON.parse(fs.readFileSync(path.join(ROOT, 'app.json'), 'utf8')); for (const name of ['pass', 'orders']) { assert.ok(app.pages.includes(`pages/${name}/index`)); page(name).show(); }
  for (const file of ['utils/pay.js', 'pages/advisor-form/index.js', 'pages/advisor-form/index.wxml', 'content/pay-terms.js']) assert.doesNotMatch(fs.readFileSync(path.join(ROOT, file), 'utf8'), /按次包|苹果手机免费重跑|比对没有完成，正在原路退款|付款后立即开始比对/);
});

test('后端免费次数用完提示不会被商品迟到响应清掉，再提交先本地拦截', async () => {
  let resolveProducts; let submitted = 0;
  const p = page('advisor-form', {
    payProducts: () => new Promise((resolve) => { resolveProducts = resolve; }),
    createAdvisorTask: () => { submitted += 1; return Promise.reject(Object.assign(new Error('本月用完'), { code: 'FREE_MONTHLY_LIMIT', status: 429 })); },
  });
  p.view.onLoad({}); p.view.onShow(); await flush(); p.view.input({ detail: { value: '甲公司' } }); p.view.submit(); await flush();
  resolveProducts({ items: [PRODUCT] }); await flush(); assert.match(p.view.data.limitHint.message, /下月 1 日恢复/); assert.match(p.view.data.limitHint.offer, /29 元/);
  p.view.submit(); await flush(); assert.equal(submitted, 1);
});
test('使用期在提交前过期，后端免费错误仍按 F1/F2 展示', async () => {
  const p = page('advisor-form', { advisorStatus: () => Promise.resolve(status(3, true)), createAdvisorTask: () => Promise.reject(Object.assign(new Error('次数用完'), { code: 'FREE_MONTHLY_LIMIT' })) });
  p.view.onLoad({}); p.view.onShow(); await flush(); p.view.input({ detail: { value: '甲公司' } }); p.view.submit(); await flush(); assert.match(p.text(), /本月 3 次免费批量名单比对已用完，下月 1 日恢复/); assert.equal(p.view.data.passActive, false);
});
test('订单失败状态没有工具按钮，不把退款状态视为任务失败退款', async () => {
  for (const state of ['退款中', '已退款', '需人工处理', '已关闭']) {
    const p = await purchase({ payOrder: () => Promise.resolve({ ...ORDER, status: state }) }); await buy(p);
    assert.equal(p.view.data.confirmed, false); assert.equal(p.view.data.busy, false); assert.equal(p.view.data.message, `订单状态：${state}`); assert.equal(p.view.data.payError, true);
  }
});
test('购买页网络失败保留重试入口，没下单时问题入口无订单号', async () => {
  const p = await purchase({ payProducts: () => Promise.reject(new Error('网络失败')) }); assert.match(p.text(), /网络失败/); assert.equal(findAll(p.show(), tapsTo('load')).length, 1);
  p.view.feedback(); assert.equal(p.calls.urls.at(-1), '/pages/feedback/index?type=pay');
});
test('先停轮询再执行微任务，不再发送查单请求', async () => {
  const timers = clock(); let fetched = 0;
  const poller = pay.createPoller({ fetch: () => { fetched += 1; return Promise.resolve(ORDER); }, onDone: () => assert.fail('不应返回'), timers, now: timers.now });
  poller.start(); poller.stop(); await flush(); assert.equal(fetched, 0); assert.equal(timers.size(), 0);
});
