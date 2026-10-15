const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const format = require('../utils/format.js');

// 所有依赖显式提供；协议用测试对象，绝不读取占位 legal.js。
function load(file, dependencies, globals = {}) {
  const module = { exports: {} };
  let page;
  const context = {
    module, setTimeout, clearTimeout,
    require(name) {
      assert.ok(Object.hasOwn(dependencies, name), `缺少测试依赖：${name}`);
      return dependencies[name];
    },
    Page(definition) { page = definition; },
    ...globals,
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), context, { filename: file });
  if (!page) return module.exports;
  return { ...page, data: JSON.parse(JSON.stringify(page.data)), setData(values) { Object.assign(this.data, values); } };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));
const tap = (key, value) => ({ currentTarget: { dataset: { [key]: value } } });

function accountHarness({ cancel = false, fail = false, filesFail = false } = {}) {
  const storage = new Map([
    ['youjuAgreedVersion', 'test'], ['youjuExports', [{ id: 1, path: '/local/有据导出_test.xlsx' }]],
    ['personalCache', { value: 'test' }],
  ]);
  const files = ['有据导出_test.xlsx', '有据导出_orphan.docx', 'public.txt'];
  const deleted = [];
  const wx = {
    env: { USER_DATA_PATH: '/local' },
    getStorageSync: (key) => storage.get(key),
    removeStorageSync: (key) => storage.delete(key),
    clearStorageSync: () => storage.clear(),
    showModal: ({ success }) => success({ confirm: !cancel }),
    showToast() {},
    getFileSystemManager: () => ({
      readdirSync: () => files,
      unlinkSync(file) { deleted.push(file); if (filesFail) throw new Error('无法删除'); },
    }),
  };
  const api = {
    AGREED_KEY: 'youjuAgreedVersion',
    forgetAgreed: () => storage.delete('youjuAgreedVersion'),
    deleteMe: () => fail ? Promise.reject(new Error('网络故障')) : Promise.resolve(),
  };
  const exportsLog = load('utils/exports.js', {}, { wx });
  const exporter = load('utils/exporter.js', {
    './api.js': api, './format.js': format, './exports.js': exportsLog,
  }, { wx });
  const consent = load('utils/consent.js', {
    './config.js': { templates: {} },
    '../content/legal.js': { final: true, version: 'test' }, './api.js': api,
    './exports.js': exportsLog, './exporter.js': exporter, './config.js': {},
  }, { wx });
  return { consent, storage, deleted };
}

test('注销成功清空所有本机存储并删除导出文件（含孤立文件）', async () => {
  const { consent, storage, deleted } = accountHarness();
  assert.equal(await consent.withdraw(), true);
  assert.equal(storage.size, 0);
  assert.deepEqual(deleted, ['/local/有据导出_test.xlsx', '/local/有据导出_orphan.docx']);
  assert.equal(consent.consentEpoch(), 1);
});

test('删除文件失败仍完成注销和存储清理', async () => {
  const { consent, storage, deleted } = accountHarness({ filesFail: true });
  assert.equal(await consent.withdraw(), true);
  assert.equal(storage.size, 0);
  assert.equal(deleted.length, 2);
});

test('取消注销或服务端删除失败保留本机数据', async () => {
  for (const options of [{ cancel: true }, { fail: true }]) {
    const { consent, storage, deleted } = accountHarness(options);
    assert.equal(await consent.withdraw(), false);
    assert.equal(storage.size, 3);
    assert.equal(deleted.length, 0);
  }
});

test('我的页请求失败显示错误，重试后恢复服务端登录状态', async () => {
  let fail = true;
  const page = load('pages/me/index.js', {
    '../../content/legal.js': { final: true },
    '../../utils/api.js': { isLive: () => true, me: () => fail ? Promise.reject(new Error('网络故障')) : Promise.resolve({ agreed: { version: 'test' }, follows: 2, subscriptions: { deadline: 1 } }) },
    '../../utils/consent.js': { loginOpen: () => true, consentEpoch: () => 0, applyServer: () => true },
    '../../utils/coverage.js': {}, '../../utils/subscribe.js': { remindOpen: () => true },
  });
  page.refresh();
  await flush();
  assert.match(page.data.profileError, /网络故障/);
  assert.equal(page.data.profileLoading, false);
  fail = false;
  page.refresh();
  assert.equal(page.data.profileLoading, true);
  await flush();
  assert.equal(page.data.profileError, '');
  assert.equal(page.data.signedIn, true);
  assert.equal(page.data.hasRecord, true);
  assert.equal(page.data.reminderCount, 1);
});

test('我的页忽略较早请求的失败，注销后过时请求不恢复个人状态', async () => {
  const requests = [];
  let epoch = 0;
  const page = load('pages/me/index.js', {
    '../../content/legal.js': { final: true },
    '../../utils/api.js': { isLive: () => true, me: () => new Promise((resolve, reject) => requests.push({ resolve, reject })) },
    '../../utils/consent.js': { loginOpen: () => true, consentEpoch: () => epoch, applyServer: () => true, withdraw: () => { epoch += 1; return Promise.resolve(true); } },
    '../../utils/coverage.js': {}, '../../utils/subscribe.js': { remindOpen: () => true },
  });
  page.refresh(); page.refresh();
  requests[1].resolve({ agreed: {}, subscriptions: {} });
  await flush();
  requests[0].reject(new Error('过时错误'));
  await flush();
  assert.equal(page.data.profileError, '');
  page.refresh(); page.deleteAccount();
  await flush();
  requests[2].resolve({ agreed: {}, subscriptions: {} });
  await flush();
  assert.equal(page.data.signedIn, false);
  assert.equal(page.data.hasRecord, false);
});

test('查名单的比对范围（D-061 并入名单看板的统计）：按类型合计份数，省份和年度点开看；加载失败不显示这一块', async () => {
  let fail = false;
  const stats = { by_type: [{ type: '高新技术企业', lists: 3, entries: 120 }, { type: '其他', lists: 2, entries: 0 }], by_province: [{ province: '山东', lists: 5, entries: 120 }], by_year: [{ year: 2026, lists: 5, entries: 120 }] };
  const page = load('pages/lists/index.js', { '../../utils/cache.js': { readCache: () => null, writeCache() {}, cachedLabel: () => '' },
    '../../utils/api.js': { lists: () => Promise.resolve({ week_total: 0, total: 0, items: [] }), listBoard: () => (fail ? Promise.reject(new Error('x')) : Promise.resolve({ stats })) },
    '../../utils/format.js': format, '../../utils/daywatch.js': {},
  });
  page.reload(); await flush();
  assert.equal(page.data.scope.lists, 5); assert.equal(page.data.scope.byType.length, 2);
  assert.equal(page.data.scopeOpen, false); page.toggleScope(); assert.equal(page.data.scopeOpen, true);
  fail = true; page.setData({ scope: null }); page.reload(); await flush();
  assert.equal(page.data.scope, null);
  assert.match(fs.readFileSync(path.join(__dirname, '..', 'pages/lists/index.wxml'), 'utf8'), /wx:if="{{scope}}" class="card scope"/);
});

test('名单类型筛选改变显示列表，全部恢复；重新加载保留筛选', async () => {
  const items = [{ title: '科技型中小企业名单' }, { title: '专精特新名单' }, { title: '其他名单' }];
  const page = load('pages/lists/index.js', { '../../utils/cache.js': { readCache: () => null, writeCache() {}, cachedLabel: () => '' },
    '../../utils/api.js': { lists: () => Promise.resolve({ week_total: 3, total: 3, items }), listBoard: () => Promise.resolve({ stats: {} }) },
    '../../utils/format.js': format, '../../utils/daywatch.js': {},
  });
  page.reload(); await flush();
  page.pickChip(tap('chip', '专精特新'));
  assert.equal(page.data.shown.length, 1);
  assert.equal(page.data.shown[0].title, '专精特新名单');
  page.reload(); await flush();
  assert.equal(page.data.shown.length, 1);
  page.pickChip(tap('chip', '全部'));
  assert.equal(page.data.shown.length, 3);
});

test('截止雷达返回跨日重载第一页；停留跨日刷新并清理定时器', async () => {
  let day = '2026-09-30';
  let onChange;
  let stopped = 0;
  const calls = [];
  const page = load('pages/report/index.js', {
    '../../utils/cache.js': { readCache: () => null, writeCache() {}, cachedLabel: () => '' },
    '../../utils/api.js': { deadlineReport: (params) => { calls.push(params); return Promise.resolve({ items: [], total: 0 }); } },
    '../../utils/exporter.js': {}, '../../utils/format.js': { todayISO: () => day },
    '../../utils/daywatch.js': { watchDay: (_, callback) => { onChange = callback; }, stopDay: () => { stopped += 1; } },
  }, { getApp: () => ({ globalData: {} }) });
  page.onLoad(); page.onShow(); await flush();
  assert.equal(calls.length, 2);
  page.onHide(); day = '2026-10-01'; page.data.page = 3; page.onShow(); await flush();
  assert.equal(calls.length, 3);
  assert.equal(calls.at(-1).page, 1);
  day = '2026-10-02'; onChange(); await flush();
  assert.equal(calls.length, 4);
  page.onUnload(); assert.equal(stopped, 2);
});

test('日历上限截断有标记；刚好 1000 条无误报', async () => {
  for (const total of [999, 1000, 1001, 1400]) {
    const requests = [];
    const api = load('utils/api.js', {
      './config.js': { devBaseUrl: 'https://test.invalid' }, '../mock/data.js': {},
      './mockSearch.js': {}, './format.js': format,
    }, { wx: { request: ({ url, success }) => {
      const params = new URL(url).searchParams;
      const offset = Number(params.get('offset'));
      requests.push(offset);
      success({ statusCode: 200, data: { total, items: Array.from({ length: Math.min(200, total - offset) }, (_, i) => ({ id: offset + i })) } });
    } } });
    const result = await api.calendar();
    assert.equal(result.items.length, Math.min(total, 1000));
    assert.equal(result.truncated, total > 1000);
    assert.equal(requests.length, 5);
  }
});

test('截止页按月看：接收截断标记，并按截止时刻移入已截止', async () => {
  const now = new Date('2026-09-30T09:01:00Z');
  const page = load('pages/report/index.js', {
    '../../utils/cache.js': { readCache: () => null, writeCache() {}, cachedLabel: () => '' },
    '../../utils/api.js': {
      deadlineReport: () => Promise.resolve({ items: [], total: 0 }),
      calendar: () => Promise.resolve({ truncated: true, items: [{ id: 'dl1', docId: '1', due: '2026-09-30', time: '17:00', verified: true, title: '甲', what: '企业申报截止' }] }),
    },
    '../../utils/exporter.js': {}, '../../utils/daywatch.js': { watchDay() {}, stopDay() {} },
    '../../utils/format.js': { ...format, todayISO: () => '2026-09-30', dueView: (due) => format.dueView(due, now) },
  }, { getApp: () => ({ globalData: {} }) });
  page.onLoad(); await flush();
  page.pickTab({ currentTarget: { dataset: { key: 0 } } }); await flush();
  assert.equal(page.data.truncated, true);
  assert.equal(page.data.rows.length, 0);
  assert.equal(page.data.past.length, 1);
  assert.equal(page.data.cells.find((c) => c.day === 30).past, true);
});
