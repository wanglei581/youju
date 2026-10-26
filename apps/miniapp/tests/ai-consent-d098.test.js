// D-098（10/26 创始人定"这次提审就带上 AI"）：解读、对照第一次用要点"同意并开始"才用（上线门槛第 7 条）。
// 手机上记下须知的版本和同意时间，须知改了要重新同意；创建任务时带上同意过的须知版本，对所有人开放时服务端核对。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const logic = require('../utils/ai-interpret.js');
const clause = require('../utils/ai-clause.js');

const read = (file) => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const plain = (value) => JSON.parse(JSON.stringify(value));

function memory(initial) {
  const saved = new Map(initial);
  return { saved, getStorageSync: (key) => saved.get(key), setStorageSync: (key, value) => saved.set(key, plain(value)) };
}

test('同意记录：旧的"我知道了"和别的须知版本都不算；点了同意记下版本和时间', () => {
  assert.notEqual(logic.CONSENT_KEY, 'youjuInterpretNoticeV1');
  assert.equal(logic.hasConsent(memory([[logic.CONSENT_KEY, true]])), false, '旧写法 true 不算');
  assert.equal(logic.hasConsent(memory([['youjuInterpretNoticeV1', true]])), false, '旧的"我知道了"不算');
  assert.equal(logic.hasConsent(memory([[logic.CONSENT_KEY, { version: '2026-01-01', at: '2026-01-01T00:00:00.000Z' }]])), false, '别的版本不算');
  assert.equal(logic.hasConsent(memory([[logic.CONSENT_KEY, logic.consentRecord()]])), true);
  const store = memory([]);
  assert.equal(logic.saveConsent(store, false), false);
  assert.equal(store.saved.size, 0, '没点同意不记');
  assert.equal(logic.saveConsent(store, true), true);
  const record = store.saved.get(logic.CONSENT_KEY);
  assert.equal(record.version, logic.NOTICE_VERSION);
  assert.ok(Number.isFinite(Date.parse(record.at)));
  assert.equal(logic.hasConsent(store), true);
});

test('须知写明交给哪个模型处理：有公示的模型名称就写名称，没配时只写大模型、不写已备案；对照页用同一句', () => {
  assert.equal(logic.modelText({ modelLabel: '某某大模型', filingNo: '备案-1' }), '通知原文会交给 某某大模型 处理。');
  assert.equal(logic.modelText({ modelLabel: '  ' }), '通知原文会交给大模型处理。');
  assert.equal(logic.modelText(null), '通知原文会交给大模型处理。');
  assert.equal(clause.modelText, logic.modelText);
});

test('解读页、对照页：须知按钮写"同意并开始""同意并查看结果"，上面一行写交给哪个模型处理，不再有"我知道了"', () => {
  for (const [file, start] of [['pages/advisor-interpret/index.wxml', '同意并开始解读'], ['pages/advisor-clause/index.wxml', '同意并开始对照']]) {
    const wxml = read(file);
    assert.ok(wxml.includes(start), file);
    assert.ok(wxml.includes('同意并查看结果'), file);
    assert.ok(wxml.includes('点"同意"才开始。') && wxml.includes('点"同意"后查看结果。'), file);
    assert.ok(wxml.includes('{{modelText}}'), file);
    assert.ok(wxml.includes('{{consentError}}'), file);
    assert.doesNotMatch(wxml, /我知道了/, file);
  }
  assert.ok(read('pages/advisor-clause/index.wxml').includes('你填的企业情况不交给模型。'));
  for (const file of ['pages/advisor-interpret/index.js', 'pages/advisor-clause/index.js']) {
    assert.match(read(file), /modelText: (clause\.)?modelText\(ai\.disclosure\)/, file);
  }
});

test('创建解读、对照任务带上须知版本；没给就不带；数据工具不带', async () => {
  const requests = [];
  const deps = { './config.js': { devBaseUrl: 'https://test.invalid' }, '../mock/data.js': {}, './mockSearch.js': {}, './format.js': {} };
  const module = { exports: {} };
  vm.runInNewContext(read('utils/api.js'), {
    module, Date, Math, Promise, setTimeout, clearTimeout, console,
    require: (key) => { assert.ok(Object.hasOwn(deps, key), key); return deps[key]; },
    wx: { request: (options) => requests.push(options), getStorageSync() {}, setStorageSync() {}, getAccountInfoSync: () => ({ miniProgram: { envVersion: 'develop' } }) },
  }, { filename: 'utils/api.js' });
  const api = module.exports;
  // 每个请求都答复了再往下（不答复的请求会一直重试，见 tests 里 api 的其他用例）
  const send = async (data, key) => {
    const pending = api.createAdvisorTask(data, key);
    const sent = plain(requests.at(-1).data);
    requests.at(-1).success({ statusCode: 202, data: { id: requests.length } });
    await pending;
    return sent;
  };
  assert.deepEqual(await send({ kind: 'interpret', doc_id: '42', notice: logic.NOTICE_VERSION }, 'k1'),
    { kind: 'interpret', doc_id: 42, notice: logic.NOTICE_VERSION });
  assert.deepEqual(await send({ kind: 'clause', doc_id: 42, company: { employees: 3 }, notice: logic.NOTICE_VERSION }, 'k2'),
    { kind: 'clause', doc_id: 42, company: { employees: 3 }, notice: logic.NOTICE_VERSION });
  assert.deepEqual(await send({ kind: 'interpret', doc_id: 42 }, 'k3'), { kind: 'interpret', doc_id: 42 });
  assert.deepEqual(await send({ kind: 'compare', rows: ['甲'], notice: logic.NOTICE_VERSION }, 'k4'), { kind: 'compare', rows: ['甲'] });
});

test('服务端和小程序的须知版本是同一个', () => {
  const backend = fs.readFileSync(path.join(__dirname, '..', '..', '..', 'backend', 'src', 'youju', 'ai', 'interpret.py'), 'utf8');
  const m = /^AI_NOTICE_VERSION = "([^"]+)"$/m.exec(backend);
  assert.ok(m, '后台 interpret.py 里要有 AI_NOTICE_VERSION');
  assert.equal(m[1], logic.NOTICE_VERSION);
});

test('同意记不下（存储出错）就不开始、不建任务，页面提示再点一次；记下以后才开始', async () => {
  let created = 0; const state = {};
  let failSave = true; const saved = new Map();
  const storage = {
    getStorageSync: (key) => saved.get(key),
    setStorageSync: (key, value) => { if (failSave) throw new Error('存储满了'); saved.set(key, plain(value)); },
  };
  const session = logic.createSession({
    api: { createAdvisorTask: () => { created += 1; return Promise.resolve({ id: 1, status: 'queued' }); }, advisorTask: (id) => Promise.resolve({ id, status: 'running' }) },
    storage, docId: '42', title: '通知', now: () => 0, makeKey: () => 'k', setTimer: () => 0, clearTimer() {}, update: (value) => Object.assign(state, value),
  });
  session.show();
  assert.equal(state.needsConsent, true);
  session.start();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(created, 0, '记不下同意就不建任务');
  assert.equal(state.consentError, logic.CONSENT_SAVE_FAILED);
  assert.equal(state.needsConsent, true);
  failSave = false;
  session.start();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(created, 1);
  assert.equal(state.consentError, '');
  assert.equal(state.needsConsent, false);
  assert.equal(saved.get(logic.CONSENT_KEY).version, logic.NOTICE_VERSION);
  session.hide();
});

test('撤回同意：删掉手机上的记录，下次再请同意；AI 首页底部写同意日期和"撤回同意"', () => {
  const store = memory([[logic.CONSENT_KEY, logic.consentRecord(new Date('2026-10-26T08:00:00.000Z'))]]);
  store.removeStorageSync = (key) => store.saved.delete(key);
  const info = logic.consentInfo(store);
  assert.ok(info && info.at instanceof Date);
  assert.equal(logic.withdrawConsent(store), true);
  assert.equal(logic.hasConsent(store), false);
  assert.equal(logic.consentInfo(store), null);
  const broken = { removeStorageSync() { throw new Error('读不了'); } };
  assert.equal(logic.withdrawConsent(broken), false);
  const wxml = read('pages/ai/index.wxml');
  assert.ok(wxml.includes('{{noticeAgreedText}}') && wxml.includes('bindtap="withdrawNotice"') && wxml.includes('撤回同意'));
  const js = read('pages/ai/index.js');
  assert.match(js, /withdrawNotice\(\) \{/);
  assert.match(js, /this\.noticeLine\(\);/);
});
