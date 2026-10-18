// 加量体验（D-070 ③）和申报季加量（⑤）：文字工具、批量工具页的入口和提交判断、确认页、工具中心。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const boost = require('../utils/boost.js');
const helpers = require('../utils/advisor.js');
const idempotency = require('../utils/data-idempotency.js');
const terms = require('../content/advisor-terms.js');
const context = require('../utils/tool-context.js');
const { render, textOf, findAll, tapsTo } = require('./wxml-render.js');

const root = path.join(__dirname, '..');
const OWNER = 'b'.repeat(64);
const plain = (value) => JSON.parse(JSON.stringify(value));
const flush = async () => { await new Promise(setImmediate); await new Promise(setImmediate); };
// 开卖之前，体验相关的文字不提购买（D-070）；也不用"试用""订阅""扣钱"这类容易误会成自动扣费的说法
const SALES = /购买|买一|开通|付费|价格|元|客户工作台|订阅|试用|扣钱|收费/;
const NOW = new Date('2026-10-20T02:00:00Z'); // 北京时间 10 月 20 日 10:00

function load(file, deps, globals = {}) {
  let page; const module = { exports: {} };
  vm.runInNewContext(fs.readFileSync(path.join(root, file), 'utf8'), {
    module, Date, Math, Promise, setTimeout, clearTimeout,
    require(key) { assert.ok(Object.hasOwn(deps, key), `${file}: ${key}`); return deps[key]; },
    Page(def) { page = def; }, ...globals,
  }, { filename: file });
  return page ? { ...page, data: plain(page.data), setData(value) { Object.assign(this.data, plain(value)); } } : module.exports;
}

const trialAvailable = () => ({ available: true, active: false, reason: null, hours: 72, dailyLimit: 10, maxRows: 500, startedAt: null, endsAt: null, usedToday: 0 });
const trialActive = (used = 0) => ({ available: false, active: true, reason: null, hours: 72, dailyLimit: 10, maxRows: 500,
  startedAt: '2026-10-20T10:00:00+08:00', endsAt: '2026-10-23T10:00:00+08:00', usedToday: used });
// 按真实的现在算结束时间（页面里用的是当前时间）：还剩 hours 小时结束；hours 为负表示已经结束
const iso = (ms) => new Date(ms).toISOString();
const trialFor = (used, hours) => ({ ...trialActive(used), startedAt: iso(Date.now() + (hours - 72) * 3600e3), endsAt: iso(Date.now() + hours * 3600e3) });
const trialEnded = (hoursAgo) => ({ ...trialAvailable(), available: false, reason: 'used',
  startedAt: iso(Date.now() - (hoursAgo + 72) * 3600e3), endsAt: iso(Date.now() - hoursAgo * 3600e3) });
const season = (active = true) => ({ start: '2026-10-20', end: '2026-10-25', active, limit: 5, normalLimit: 3, maxRows: 50 });
function status({ used = 0, limit = 3, maxRows = 50, trial = null, seasonInfo = null, pass = false } = {}) {
  return { enabled: true, pass: { active: pass }, quota: { compare: { used, limit, maxRows }, track: { used, limit, maxRows } }, trial, season: seasonInfo };
}

test('体验状态：字段缺了或格式不对就当没有体验；进行中要有结束时间', () => {
  assert.equal(boost.trialView(null).trialAvailable, false);
  assert.equal(boost.trialView({ ...trialAvailable(), dailyLimit: 'x' }).trialAvailable, false);
  assert.equal(boost.trialView({ ...trialActive(), endsAt: 'bad' }).trialActive, false);
  const active = boost.trialView(trialActive(3), NOW);
  assert.deepEqual([active.trialActive, active.trialAvailable, active.trialLeft, active.trialEnd], [true, false, 7, '10月23日 10:00']);
  assert.match(boost.trialLine(active), /^加量体验中，到 10月23日 10:00 结束：每次最多 500 家，今天还能提交 7 次（名单比对和通知表合计）；不占本月免费次数$/);
  // 当天用完：说明天 0 点恢复，并说清体验期间不能改用每月免费次数
  assert.equal(boost.trialCapText(active, NOW), '今天的 10 次加量体验已用完，明天 0 点恢复；体验期间每月免费次数不扣、也不能改用');
  // 最后一天（体验在明天 0 点之前结束）：不说"明天恢复"，说几点结束、之后回到免费版
  const lastDay = new Date('2026-10-23T01:00:00Z'); // 北京时间 10 月 23 日 9:00，体验 10:00 结束
  assert.equal(boost.trialCapText(boost.trialView(trialActive(10), lastDay), lastDay), '今天的 10 次加量体验已用完；体验到 10月23日 10:00 结束，之后回到免费版');
  assert.equal(boost.trialView(trialActive(12)).trialLeft, 0);
  assert.equal(boost.unavailableText(boost.trialView({ ...trialAvailable(), available: false, reason: 'used' })), '加量体验已经用过，每个微信只能体验一次。');
  assert.match(boost.unavailableText(boost.trialView({ ...trialAvailable(), available: false, reason: 'pass' })), /体验留到以后/);
});

test('确认页写全 D-070 要求的每一项，预计结束时间按现在加 72 小时，不提购买', () => {
  assert.deepEqual(boost.confirmItems(status({ trial: { ...trialAvailable(), available: false, reason: 'used' } }), NOW), []);
  const items = boost.confirmItems(status({ used: 1, trial: trialAvailable() }), NOW);
  const text = items.map((item) => `${item.label}：${item.text}`).join('\n');
  for (const must of [/连续 3 天/, /连续 72 小时/, /中途不能暂停/, /到 10月23日 10:00（北京时间）结束/, /每次最多 500 家（平时每次 50 家）/,
    /两项合计每天最多 10 次，北京时间 0 点重新计数/, /现在本月免费还剩：名单比对 2 次、客户相关通知表 2 次/,
    /体验期间只用体验的次数，这些不扣、也不能改用，体验结束后接着用/,
    /不需要填写任何付款信息/, /自动回到免费版：每次 50 家以内/, /体验结束后照样能看、能导出/, /用过一次后，账号还在就不能再体验/, /同事共用这一次/, /注销账号后 180 天内（这期间再注销的，从最后一次算起），用这个微信号重新登录也不能再体验/, /名单准备好再开始/]) {
    assert.match(text, must);
  }
  assert.doesNotMatch(text, SALES);
  assert.doesNotMatch(text, /跨过月底/);
  // 本月免费次数已经用完：写明体验另算
  assert.match(boost.confirmItems(status({ used: 3, trial: trialAvailable() }), NOW).map((i) => i.text).join(), /本月免费次数已经用完，体验的次数另算。体验期间只用体验的次数，不能改用每月免费次数；体验结束后照常使用。/);
  // 月底开始会跨月：本月剩下的免费次数体验期间用不了、也不顺延，开始前写明（不能写"体验结束后接着用"）
  const monthEnd = new Date('2026-10-30T02:00:00Z'); // 北京时间 10 月 30 日 10:00，体验到 11 月 2 日
  const crossing = boost.confirmItems(status({ used: 1, trial: trialAvailable() }), monthEnd).map((i) => i.text).join('\n');
  assert.match(crossing, /到 11月2日 10:00（北京时间）结束/);
  assert.match(crossing, /注意：现在开始的话体验会跨过月底，本月剩下的免费次数用不上（不顺延到下个月），想用的话先用完再开始/);
  assert.doesNotMatch(crossing, /体验结束后接着用/);
  // 本月已经没有免费次数：跨月也没有可损失的，不提醒
  assert.doesNotMatch(boost.confirmItems(status({ used: 3, trial: trialAvailable() }), monthEnd).map((i) => i.text).join(), /跨过月底/);
});

test('体验结束后 7 天内说一句已结束；进行中、没用过、太久以前的都不说', () => {
  const at = new Date('2026-10-25T02:00:00Z');
  const ended = (endsAt) => ({ ...trialAvailable(), available: false, reason: 'used', startedAt: '2026-10-20T10:00:00+08:00', endsAt });
  assert.equal(boost.trialEndedText(ended('2026-10-23T10:00:00+08:00'), at), '加量体验已于 10月23日 10:00 结束，已回到免费版');
  assert.equal(boost.trialEndedText(ended('2026-10-17T10:00:00+08:00'), at), '');
  assert.equal(boost.trialEndedText(ended('2026-10-26T10:00:00+08:00'), at), '');
  assert.equal(boost.trialEndedText({ ...ended('2026-10-23T10:00:00+08:00'), reason: 'pass' }, at), '');
  assert.equal(boost.trialEndedText(trialActive(1), at), '');
  assert.equal(boost.trialEndedText({ ...trialAvailable(), available: false, reason: 'used' }, at), '');
  assert.equal(boost.trialEndedText(null, at), '');
});

test('申报季加量：进行中写起止日期和回到 3 次的日子；没开始的提前显示；格式不对不显示', () => {
  // 月中结束：当月已用的次数照算
  assert.equal(boost.seasonLine(season()), '申报季加量：10月20日至10月25日，免费次数每月 5 次（每次仍 50 家以内）；10月26日起回到每月 3 次，当月已用的次数照算');
  assert.equal(boost.seasonLine(season(false)), '申报季加量将于10月20日开始：10月20日至10月25日免费次数每月 5 次（每次仍 50 家以内）');
  // 在月底结束：下个月重新计数，不再写"照算"（走查时读起来像说下个月）
  assert.equal(boost.seasonLine({ ...season(), end: '2026-10-31' }), '申报季加量：10月20日至10月31日，免费次数每月 5 次（每次仍 50 家以内）；11月1日起回到每月 3 次');
  assert.match(boost.seasonLine({ ...season(), start: '2026-11-25', end: '2026-12-10' }), /12月11日起回到每月 3 次，当月已用的次数照算$/);
  assert.doesNotMatch(boost.seasonLine({ ...season(), end: '2026-02-28' }), /照算/);
  assert.equal(boost.seasonLine(null), '');
  assert.equal(boost.seasonLine({ ...season(), start: '10-20' }), '');
  assert.equal(boost.seasonLine({ ...season(), limit: 0 }), '');
});

function dataLogic(api) {
  return load('utils/data-tools.js', { './regions.js': require('../utils/regions.js'), './api.js': api, './advisor.js': helpers,
    '../content/advisor-terms.js': terms, './boost.js': boost }, { wx: {} });
}

function form(options = {}) {
  const calls = []; const urls = [];
  const storage = new Map();
  const api = { advisorCapabilities: () => Promise.resolve({ data_tools: { enabled: true, features: ['compare', 'track'], remaining: {} }, ai_advisor: { enabled: false, features: [] }, pay: { enabled: false } }),
    advisorStatus: () => Promise.resolve(options.status || status()), me: () => Promise.resolve({ advisorAgreed: { version: terms.version }, taskOwner: OWNER }),
    advisorPreview: (body) => { calls.push(['preview', plain(body)]); return Promise.resolve({ rowCount: body.rows.length }); },
    createAdvisorTask: (body, key) => { calls.push(['create', plain(body), key]); return Promise.resolve({ id: 5 }); }, ...options.api };
  const wx = { getStorageSync: (key) => storage.get(key), setStorageSync: (key, value) => storage.set(key, plain(value)), removeStorageSync: (key) => storage.delete(key),
    setNavigationBarTitle() {}, navigateTo: ({ url }) => urls.push(url), redirectTo: ({ url }) => urls.push(url), showToast() {} };
  const page = load('pages/advisor-form/index.js', { '../../utils/api.js': api, '../../utils/data-idempotency.js': idempotency, '../../utils/advisor.js': helpers,
    '../../utils/data-tools.js': dataLogic(api), '../../utils/consent.js': { ensureConsent: () => Promise.resolve(true) }, '../../utils/pay.js': require('../utils/pay.js') }, { wx });
  page.onLoad({ kind: 'compare' }); page.onShow();
  return { page, calls, urls };
}
const shown = (page) => render('pages/advisor-form/index.wxml', page.data);
const rows = (n) => Array.from({ length: n }, (_, i) => `合成企业${i}`).join('\n');

test('批量工具页：有资格时一直有加量体验入口；还有免费次数时不抢眼，用满或超过 50 家时推荐', async () => {
  const h = form({ status: status({ trial: trialAvailable() }) }); await flush();
  let tree = shown(h.page);
  assert.match(textOf(tree), /加量体验（免费，每个微信一次）：从点“开始体验”起连续 3 天，每次最多 500 家，名单比对和通知表合计每天 10 次，不占本月免费次数。名单准备好再开始。/);
  let entry = findAll(tree, tapsTo('openTrial'));
  assert.equal(entry.length, 1); assert.doesNotMatch(entry[0].attrs.class, /btn-primary/);
  // 这次超过 50 家：入口变成主按钮，预览提示里也推荐，提示下面紧跟一个"了解加量体验"（不用翻回上方找）
  h.page.input({ detail: { value: rows(60) } }); h.page.preview(); await flush();
  assert.equal(h.page.data.inputError, '每次最多 50 家，请减少行数；或开始 3 天加量体验，每次最多 500 家');
  entry = findAll(shown(h.page), tapsTo('openTrial'));
  assert.equal(entry.length, 2); assert.match(entry[0].attrs.class, /btn-primary/);
  h.page.openTrial(); assert.deepEqual(h.urls, ['/pages/advisor-trial/index']);
  h.page.onUnload();

  // 本月免费次数用满：提示里推荐体验，不出现购买（支付关着）
  const used = form({ status: status({ used: 3, trial: trialAvailable() }) }); await flush();
  tree = shown(used.page);
  assert.match(textOf(tree), /本月免费次数已用完，下月可继续使用。不需要购买。也可以先开始 3 天加量体验（见下方）。/);
  assert.match(findAll(tree, tapsTo('openTrial'))[0].attrs.class, /btn-primary/);
  assert.doesNotMatch(textOf(tree), /客户工作台|开通|元/);
  used.page.input({ detail: { value: '合成企业甲' } }); used.page.submit();
  assert.equal(used.page.data.inputError, '本月免费次数已用完，下月 1 日恢复；也可以开始 3 天加量体验（免费，每次 500 家以内）');
  assert.equal(findAll(shown(used.page), tapsTo('openTrial')).length, 2);
  assert.equal(used.calls.length, 0); used.page.onUnload();

  // 别的提示（网络、格式）下面不出现"了解加量体验"，只有推荐了体验的那句下面才有（Codex 审阅）
  const offline = form({ status: status({ used: 3, trial: trialAvailable() }), api: { advisorPreview: () => Promise.reject(new Error('网络连接失败，请检查网络后重试')) } }); await flush();
  offline.page.input({ detail: { value: '合成企业甲' } }); offline.page.preview(); await flush();
  assert.equal(offline.page.data.inputError, '网络连接失败，请检查网络后重试');
  assert.equal(findAll(shown(offline.page), tapsTo('openTrial')).length, 1); offline.page.onUnload();

  // 没有资格（用过了、开关关着）：不出现入口
  const none = form({ status: status({ used: 3, trial: { ...trialAvailable(), available: false, reason: 'used' } }) }); await flush();
  assert.equal(findAll(shown(none.page), tapsTo('openTrial')).length, 0);
  assert.doesNotMatch(textOf(shown(none.page)), /加量体验/); none.page.onUnload();
});

test('体验中：免费次数用满也能提交 500 家以内；额度一行写体验；当天用完只说明天恢复', async () => {
  const h = form({ status: status({ used: 3, maxRows: 500, trial: trialActive(2) }) }); await flush();
  const text = textOf(shown(h.page));
  assert.match(text, /加量体验中，到 (2026年)?10月23日 10:00 结束：每次最多 500 家，今天还能提交 8 次（名单比对和通知表合计）；不占本月免费次数/);
  assert.doesNotMatch(text, /本月免费次数已用完/);
  assert.equal(findAll(shown(h.page), tapsTo('openTrial')).length, 0);
  h.page.input({ detail: { value: rows(120) } }); h.page.preview(); await flush();
  assert.equal(h.page.data.previewed, true); h.page.submit(); await flush();
  assert.equal(h.calls.filter((c) => c[0] === 'create').length, 1); assert.equal(h.page.data.accepted, true);
  h.page.onUnload();

  // 当天用完：一进页面上方就用红字说明（和免费次数用完一样），点提交也是这句；不发请求
  const cap = '今天的 10 次加量体验已用完，明天 0 点恢复；体验期间每月免费次数不扣、也不能改用';
  const capped = form({ status: status({ maxRows: 500, trial: trialFor(10, 48) }) }); await flush();
  assert.match(textOf(shown(capped.page)), new RegExp(cap));
  capped.page.input({ detail: { value: '合成企业甲' } }); capped.page.submit();
  assert.equal(capped.page.data.inputError, cap);
  capped.page.preview(); await flush(); capped.page.submit(); await flush();
  assert.equal(capped.calls.filter((c) => c[0] === 'create').length, 0); capped.page.onUnload();

  // 服务端说当天用完（页面上的数还没更新）：提示换成本页的说法，额度那一行也跟着刷新（不再写"还能提交 1 次"）
  let current = status({ maxRows: 500, trial: trialFor(9, 48) });
  const server = form({ api: { advisorStatus: () => Promise.resolve(current), createAdvisorTask: () => {
    current = status({ maxRows: 500, trial: trialFor(10, 48) });
    return Promise.reject(Object.assign(new Error('今天的 10 次加量体验已用完，明天 0 点恢复'), { status: 429, code: 'TRIAL_DAILY_LIMIT', detail: { dailyLimit: 10 } }));
  } } }); await flush();
  assert.match(server.page.data.statusLine, /今天还能提交 1 次/);
  server.page.input({ detail: { value: '合成企业甲' } }); server.page.preview(); await flush(); server.page.submit(); await flush(); await flush();
  assert.equal(server.page.data.trialLeft, 0); assert.equal(server.page.data.inputError, cap);
  assert.match(server.page.data.statusLine, /今天还能提交 0 次/); server.page.onUnload();
});

test('页面一直开着跨过 0 点：被挡住前先重读额度，新的一天能接着提交；重读只做一次、读不到就提示', async () => {
  let current = status({ maxRows: 500, trial: trialActive(10) });
  let reads = 0;
  const h = form({ api: { advisorStatus: () => { reads += 1; return Promise.resolve(current); } } });
  await flush();
  h.page.input({ detail: { value: '合成企业甲' } }); h.page.preview(); await flush();
  // 模拟这份额度是昨天读的；服务端今天的体验次数已经重新计数
  h.page.setData({ quotaDay: '2026-10-19' });
  current = status({ maxRows: 500, trial: trialActive(0) });
  const before = reads;
  h.page.submit(); await flush(); await flush();
  assert.equal(reads, before + 1);
  assert.equal(h.calls.filter((c) => c[0] === 'create').length, 1);
  h.page.onUnload();

  // 重读后还是用完（或手机时钟比服务器快）：只重读一次，然后照常提示
  let count = 0;
  const still = form({ api: { advisorStatus: () => { count += 1; return Promise.resolve(status({ maxRows: 500, trial: { ...trialActive(10), endsAt: '2026-01-01T00:00:00+08:00' } })); } } });
  await flush();
  still.page.setData({ trialActive: true, trialLeft: 0, trialEndsMs: 1, trialCap: '今天的 10 次加量体验已用完' });
  still.page.input({ detail: { value: '合成企业甲' } }); still.page.preview(); await flush();
  const seen = count;
  still.page.setData({ quotaDay: '2026-10-19' });
  still.page.submit(); await flush(); await flush();
  assert.ok(count - seen <= 1, `重读了 ${count - seen} 次`);
  assert.equal(still.calls.filter((c) => c[0] === 'create').length, 0);
  still.page.onUnload();

  // 重读失败：提示，不提交
  let fail = false;
  const broken = form({ api: { advisorStatus: () => (fail ? Promise.reject(new Error('网络连接失败')) : Promise.resolve(status({ maxRows: 500, trial: trialActive(10) }))) } });
  await flush();
  broken.page.input({ detail: { value: '合成企业甲' } }); broken.page.preview(); await flush();
  fail = true; broken.page.setData({ quotaDay: '2026-10-19' });
  broken.page.submit(); await flush(); await flush();
  assert.match(broken.page.data.inputError, /未能读取工具额度：网络连接失败/);
  assert.equal(broken.calls.filter((c) => c[0] === 'create').length, 0);
  broken.page.onUnload();
});

test('体验刚好到期：服务端按免费处理时页面跟着改，并静默刷新额度那一行', async () => {
  let current = status({ maxRows: 500, trial: trialActive(1) });
  const h = form({ api: { advisorStatus: () => Promise.resolve(current),
    createAdvisorTask: () => Promise.reject(Object.assign(new Error('每次最多提交 50 行'), { status: 429, code: 'FREE_ROW_LIMIT', detail: { maxRows: 50 } })) } }); await flush();
  h.page.input({ detail: { value: rows(80) } }); h.page.preview(); await flush();
  current = status({ trial: trialEnded(0.1) });
  h.page.submit(); await flush(); await flush();
  assert.equal(h.page.data.trialActive, false); assert.equal(h.page.data.maxRows, 50); assert.equal(h.page.data.previewed, false);
  assert.match(h.page.data.statusLine, /^每月免费 3 次，本月剩余 3 次；每次最多 50 家$/);
  // 提示先说体验已结束（走查时只看到"每次最多提交 50 行"，不知道为什么）；额度下面写明几点结束
  assert.equal(h.page.data.inputError, '加量体验已结束，每次最多 50 家，请减少行数');
  assert.match(textOf(shown(h.page)), /加量体验已于 .+ 结束，已回到免费版/);
  h.page.onUnload();

  // 体验到期、本月免费次数也用完了：同样先说体验已结束
  let later = status({ used: 3, maxRows: 500, trial: trialActive(1) });
  const used = form({ api: { advisorStatus: () => Promise.resolve(later),
    createAdvisorTask: () => Promise.reject(Object.assign(new Error('本月免费次数已用完'), { status: 429, code: 'FREE_MONTHLY_LIMIT' })) } }); await flush();
  used.page.input({ detail: { value: '合成企业甲' } }); used.page.preview(); await flush();
  later = status({ used: 3, trial: trialEnded(0.1) });
  used.page.submit(); await flush(); await flush();
  assert.equal(used.page.data.inputError, '加量体验已结束。本月免费次数已用完，下月可继续使用；不需要购买');
  used.page.onUnload();

  // 体验结束后回到页面（7 天内）：额度下面写明体验几点结束；超过 50 家的提示也先说体验已结束
  const back = form({ status: status({ trial: trialEnded(30) }) }); await flush();
  assert.match(back.page.data.trialEnded, /^加量体验已于 .+ 结束，已回到免费版$/);
  back.page.input({ detail: { value: rows(80) } }); back.page.preview(); await flush();
  assert.equal(back.page.data.inputError, '加量体验已结束，每次最多 50 家，请减少行数');
  back.page.onUnload();
});

test('申报季加量：批量工具页显示起止日期，次数按服务端的 5 次', async () => {
  const h = form({ status: status({ used: 1, limit: 5, seasonInfo: season() }) }); await flush();
  const text = textOf(shown(h.page));
  assert.match(text, /每月免费 5 次，本月剩余 4 次；每次最多 50 家/);
  assert.match(text, /申报季加量：10月20日至10月25日，免费次数每月 5 次（每次仍 50 家以内）；10月26日起回到每月 3 次/);
  h.page.onUnload();
});

function trialPage(options = {}) {
  const calls = []; const nav = []; const toasts = [];
  let current = options.status || status({ used: 1, trial: trialAvailable() });
  const api = { advisorStatus: () => { calls.push('status'); return Promise.resolve(current); },
    startBoostTrial: () => { calls.push('start'); return options.start ? options.start() : Promise.resolve(trialActive()); } };
  const wx = { navigateBack: (o) => { nav.push('back'); if (options.noBack && o && o.fail) o.fail(); }, redirectTo: ({ url }) => nav.push(url), showToast: ({ title }) => toasts.push(title) };
  const consent = options.consent || { ensureConsent: () => Promise.resolve(false) };
  const page = load('pages/advisor-trial/index.js', { '../../utils/api.js': api, '../../utils/boost.js': boost, '../../utils/consent.js': consent }, { wx });
  page.onShow();
  return { page, calls, nav, toasts, set: (value) => { current = value; } };
}
const trialShown = (page) => render('pages/advisor-trial/index.wxml', page.data);

test('确认页：开始时协议已更新（403）就请他重新阅读并同意；同意了重新读状态，没同意就留着提示', async () => {
  const outdated = () => Promise.reject(Object.assign(new Error('用户协议或隐私保护指引已更新，请重新阅读并同意'), { status: 403 }));
  let asked = 0;
  const yes = trialPage({ start: outdated, consent: { ensureConsent: () => { asked += 1; return Promise.resolve(true); } } });
  await flush();
  yes.page.start(); await flush();
  assert.equal(asked, 1);
  assert.deepEqual(yes.calls, ['status', 'start', 'status']);
  const no = trialPage({ start: outdated });
  await flush();
  no.page.start(); await flush();
  assert.deepEqual(no.calls, ['status', 'start']);
  assert.match(textOf(trialShown(no.page)), /已更新，请重新阅读并同意/);
});

test('确认页：开始之前写全规则，点一次才开始，重复点不重复请求；开始后写结束时间', async () => {
  const h = trialPage(); await flush();
  const tree = trialShown(h.page);
  const text = textOf(tree);
  assert.match(text, /3 天内每次最多 500 家（免费）/);
  assert.match(text, /开始之前请看完/);
  for (const must of [/连续 72 小时，中途不能暂停/, /每次最多 500 家/, /两项合计每天最多 10 次/, /本月免费还剩：名单比对 2 次、客户相关通知表 2 次/,
    /不需要填写任何付款信息/, /自动回到免费版/, /照样能看、能导出/, /用过一次后，账号还在就不能再体验/]) assert.match(text, must);
  assert.doesNotMatch(text, SALES);
  const startButton = findAll(tree, tapsTo('start'))[0];
  assert.match(textOf(startButton), /开始体验（立即起算 72 小时）/); assert.match(startButton.attrs.class, /btn-primary/);
  assert.ok(findAll(tree, tapsTo('back')).length === 1);

  h.page.start(); h.page.start(); await flush();
  assert.equal(h.calls.filter((c) => c === 'start').length, 1);
  assert.deepEqual(h.toasts, ['加量体验已开始']);
  const after = textOf(trialShown(h.page));
  assert.match(after, /加量体验中，到 (2026年)?10月23日 10:00 结束/); assert.match(after, /去提交名单/);
  assert.doesNotMatch(after, /开始之前请看完/);
  h.page.back(); assert.deepEqual(h.nav, ['back']);
  h.page.onUnload();
});

test('确认页：用过了、使用期内、没开放时说明原因，不给开始；开始失败按服务端最新状态重新显示', async () => {
  for (const [trial, note] of [[{ ...trialAvailable(), available: false, reason: 'used' }, /已经用过，每个微信只能体验一次/],
    [{ ...trialAvailable(), available: false, reason: 'pass' }, /体验留到以后再用/], [null, /加量体验暂未开放/]]) {
    const h = trialPage({ status: status({ trial }) }); await flush();
    const tree = trialShown(h.page);
    assert.match(textOf(tree), note); assert.equal(findAll(tree, tapsTo('start')).length, 0);
    h.page.start(); assert.equal(h.calls.includes('start'), false); h.page.onUnload();
  }
  const conflict = trialPage({ start: () => Promise.reject(Object.assign(new Error('加量体验已经用过，每个微信只能体验一次'), { status: 409, code: 'TRIAL_USED' })) }); await flush();
  conflict.set(status({ trial: { ...trialAvailable(), available: false, reason: 'used' } }));
  conflict.page.start(); await flush(); await flush();
  assert.match(textOf(trialShown(conflict.page)), /已经用过/); assert.equal(findAll(trialShown(conflict.page), tapsTo('start')).length, 0);
  conflict.page.onUnload();
  // 网络断了：留在确认页，可以再点（服务端重复开始不会重新起算）
  const offline = trialPage({ start: () => Promise.reject(new Error('网络连接失败，请检查网络后重试')) }); await flush();
  offline.page.start(); await flush();
  assert.match(offline.page.data.error, /网络连接失败/); assert.equal(offline.page.data.starting, false);
  assert.equal(findAll(trialShown(offline.page), tapsTo('start')).length, 1);
  offline.page.onUnload();
  // 没有上一页（从分享或通知直接打开）：回批量工具页
  const lone = trialPage({ noBack: true }); await flush(); lone.page.back();
  assert.deepEqual(lone.nav, ['back', '/pages/advisor-form/index']); lone.page.onUnload();
});

test('工具中心：体验中写体验的次数，申报季写起止日期', async () => {
  const api = { advisorCapabilities: () => Promise.resolve({ data_tools: { enabled: true, features: ['compare', 'track'], remaining: {} }, ai_advisor: { enabled: false, features: [] }, reason: 'off' }),
    advisorStatus: () => Promise.resolve(status({ used: 3, limit: 5, maxRows: 500, trial: trialActive(4), seasonInfo: season() })), advisorTasks: () => Promise.resolve({ items: [] }) };
  const page = load('pages/advisor/index.js', { '../../utils/api.js': api, '../../utils/advisor.js': helpers, '../../utils/tool-context.js': context, '../../utils/boost.js': boost },
    { wx: { setNavigationBarTitle() {} } });
  page.onLoad({}); page.onShow(); await flush(); await flush();
  const text = textOf(render('pages/advisor/index.wxml', page.data));
  assert.match(text, /加量体验中，到 (2026年)?10月23日 10:00 结束：每次最多 500 家，今天还能提交 6 次/);
  assert.match(text, /申报季加量：10月20日至10月25日/);
  page.onUnload();
});

test('确认页已注册、标题写明；页面不出现购买字样', () => {
  const app = JSON.parse(fs.readFileSync(path.join(root, 'app.json'), 'utf8'));
  assert.ok(app.pages.includes('pages/advisor-trial/index'));
  assert.equal(JSON.parse(fs.readFileSync(path.join(root, 'pages/advisor-trial/index.json'), 'utf8')).navigationBarTitleText, '加量体验');
  for (const file of ['pages/advisor-trial/index.wxml', 'pages/advisor-trial/index.js', 'utils/boost.js']) {
    const source = fs.readFileSync(path.join(root, file), 'utf8').replace(/\/\/.*$/gm, '');
    assert.doesNotMatch(source, SALES, file);
  }
});
