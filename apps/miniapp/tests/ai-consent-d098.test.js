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

test('须知写明交给哪个模型处理：有公示的模型名称就写名称，没配时写已备案的大模型；对照页用同一句', () => {
  assert.equal(logic.modelText({ modelLabel: '某某大模型', filingNo: '备案-1' }), '通知原文会交给 某某大模型 处理。');
  assert.equal(logic.modelText({ modelLabel: '  ' }), '通知原文会交给已备案的大模型处理。');
  assert.equal(logic.modelText(null), '通知原文会交给已备案的大模型处理。');
  assert.equal(clause.modelText, logic.modelText);
});

test('解读页、对照页：须知按钮写"同意并开始""同意并查看结果"，上面一行写交给哪个模型处理，不再有"我知道了"', () => {
  for (const [file, start] of [['pages/advisor-interpret/index.wxml', '同意并开始解读'], ['pages/advisor-clause/index.wxml', '同意并开始对照']]) {
    const wxml = read(file);
    assert.ok(wxml.includes(start), file);
    assert.ok(wxml.includes('同意并查看结果'), file);
    assert.match(wxml, /\{\{modelText\}\}点"同意"才开始。/, file);
    assert.doesNotMatch(wxml, /我知道了/, file);
  }
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
