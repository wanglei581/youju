// AI 解读、条款对照两页的标题和"先同意协议"那张卡片（正式包，config.aiInternal 为 false）。
// 带 AI 提审包里审核员是点"AI 解读"进来的：标题不能写"工具"，没同意协议时卡片也不说"使用工具前"
// （D-097 会话 10/27 实测发现，协调窗口转来）。AI 关着、从旧链接进来时页面上仍然没有 AI 字样（D-075）：
// 标题留空，等能力接口回话，关着就回首页。
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { render } = require('./helpers/wxml.js');

const read = (file) => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
const plain = (value) => JSON.parse(JSON.stringify(value));
const flush = () => new Promise((resolve) => setImmediate(resolve));
const PAGES = { 'advisor-interpret': 'AI 解读', 'advisor-clause': '条款对照' };

function load(file, deps, globals) {
  let definition;
  vm.runInNewContext(read(file), {
    module: { exports: {} }, Date, Math, Promise, Set, setTimeout, clearTimeout,
    require(name) {
      if (Object.hasOwn(deps, name)) return deps[name];
      if (/daywatch\.js$/.test(name)) return require('./helpers/day-off.js');
      const real = name.replace('../../utils/', '../utils/');
      assert.ok(real !== name, `缺少依赖 ${name}`);
      return require(real);
    },
    Page(value) { definition = value; },
    ...globals,
  }, { filename: file });
  return { ...definition, data: plain(definition.data), setData(values) { Object.assign(this.data, plain(values)); } };
}

function access(visible) {
  const state = { visible, confirmed: true, available: visible };
  return { current: () => ({ ...state }), reconcile() {}, refresh: () => Promise.resolve({ ...state }), canOpen: () => visible };
}

const CONSENT = { data_tools: { enabled: false, features: [], remaining: {} }, ai_advisor: { enabled: false, features: [], remaining: {} }, reason: 'consent' };
const OPEN = {
  data_tools: { enabled: false, features: [], remaining: {} },
  ai_advisor: { enabled: true, features: ['interpret', 'clause'], remaining: { interpret: 5, clause: 5 }, available: true, siteFull: false, disclosure: { modelLabel: '模型名称', filingNo: '备案号' } },
};

async function open(page, visible, capabilities) {
  const titles = [];
  const switched = [];
  const values = new Map();
  const view = load(`pages/${page}/index.js`, {
    '../../utils/api.js': { advisorCapabilities: () => Promise.resolve(capabilities), forgetCapabilities() {}, advisorTask: () => Promise.resolve({}) },
    '../../utils/ai-access.js': access(visible),
  }, {
    wx: {
      setNavigationBarTitle: ({ title }) => titles.push(title),
      switchTab: ({ url }) => switched.push(url),
      getStorageSync: (key) => values.get(key), setStorageSync: (key, value) => values.set(key, value), removeStorageSync: (key) => values.delete(key),
    },
  });
  view.onLoad({ docId: '42', title: '关于开展申报的通知' });
  view.onShow(); await flush(); await flush();
  return { view, titles, switched, text: render(read(`pages/${page}/index.wxml`), view.data) };
}

test('两页的配置不写"工具"：打开的那一瞬间标题是空的，不会先闪一下"工具"', () => {
  for (const page of Object.keys(PAGES)) {
    const config = JSON.parse(read(`pages/${page}/index.json`));
    assert.equal(config.navigationBarTitleText, '', page);
  }
});

test('AI 开着、还没同意协议：标题照入口写，卡片说"使用前，请先阅读并同意"，全页没有"工具"', async () => {
  for (const [page, name] of Object.entries(PAGES)) {
    const { view, titles, switched, text } = await open(page, true, CONSENT);
    assert.deepEqual(titles, [name], page);
    assert.equal(view.data.gate, 'consent', page);
    assert.match(text, /使用前，请先阅读并同意《用户协议》和《隐私保护指引》/, page);
    assert.doesNotMatch(text, /工具/, page);
    assert.deepEqual(switched, [], page);
    view.onUnload();
  }
});

test('AI 关着、从旧链接进来：标题留空、页面没有 AI 字样，能力接口说关着就回首页', async () => {
  for (const page of Object.keys(PAGES)) {
    // 没同意协议（403）：不知道开没开，标题留空，卡片只说先同意，不提 AI
    const consent = await open(page, false, CONSENT);
    assert.deepEqual(consent.titles, [''], page);
    assert.doesNotMatch(consent.text, /AI|工具/, page);
    consent.view.onUnload();
    // 关着：回首页
    const off = await open(page, false, { ...CONSENT, reason: undefined });
    assert.deepEqual(off.titles, [''], page);
    assert.deepEqual(off.switched, ['/pages/home/index'], page);
    off.view.onUnload();
  }
});

test('AI 开着、能用：标题照入口写', async () => {
  for (const [page, name] of Object.entries(PAGES)) {
    // 板块没来得及显示（刚装好）也一样：能力接口说开着就补上标题
    for (const visible of [true, false]) {
      const { view, titles } = await open(page, visible, OPEN);
      assert.equal(titles.at(-1), name, `${page} ${visible}`);
      assert.ok(titles.every((title) => title === '' || title === name), `${page} ${visible}`);
      view.onUnload();
    }
  }
});
