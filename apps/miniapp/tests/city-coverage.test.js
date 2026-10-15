const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { render, textOf } = require('./wxml-render.js');
const format = require('../utils/format.js');

const shown = (page, data) => textOf(render(`pages/${page}/index.wxml`, data));
const stats = { sources: 72, provinces: 28, week_total: 5 };
const cities = [{ province: '山东', city: '青岛', sources: 2 }];
const coverage = { ...stats, national: true, regions: [], notCovered: [], updatedAt: null };

function load(file, deps) {
  let page;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', file), 'utf8'), {
    require: (name) => { assert.ok(Object.hasOwn(deps, name), `缺少依赖 ${name}`); return deps[name]; },
    Page: (d) => { page = d; },
  });
  return { ...page, data: JSON.parse(JSON.stringify(page.data)), setData(v) { Object.assign(this.data, v); } };
}
const flush = () => new Promise((r) => setImmediate(r));

test('首页和我的：无城市时文字与旧版相同，有城市追加数量', () => {
  for (const count of [undefined, 0, 1]) {
    const value = { ...stats, cities: count };
    const home = shown('home', { stats: value, loaded: true, shown: [], loginOpen: false });
    const me = shown('me', { stats: value, loginOpen: false });
    assert.ok(home.includes(`来自 28 个省和国家部委${count ? '、1 个城市' : ''} · 每条都有出处`));
    assert.ok(home.includes(`数据来自 72 个政府网站栏目，覆盖国家部委和 28 个省${count ? '、1 个城市' : ''}，每条都附原文链接。`));
    assert.ok(me.includes('28个省 + 国家部委'));
    assert.equal(me.includes('其中已收录 1 个城市的栏目'), count === 1);
    if (!count) { assert.ok(!home.includes('城市')); assert.ok(!me.includes('城市')); }
  }
});

test('收录范围显示省份、城市、栏目数；旧响应无 cities 时仍兼容', async () => {
  for (const list of [undefined, [], cities]) {
    const page = load('pages/about/index.js', {
      '../../content/legal.js': { operator: {}, disclaimer: '' },
      '../../utils/coverage.js': { loadCoverage: () => Promise.resolve({ ...coverage, cities: list }) },
      '../../utils/format.js': format,
      '../../utils/consent.js': { loginOpen: () => false, remindAny: () => false },
    });
    page.loadRange(); await flush();
    const text = shown('about', page.data);
    const count = (list || []).length;
    assert.ok(text.includes(`共 72 个政府网站栏目，覆盖国家部委和 28 个省级地区${count ? '、1 个城市' : ''}。点地区查看栏目。`));
    if (count) assert.ok(text.includes('山东·青岛（2 个栏目）'));
    else assert.ok(!text.includes('已收录城市'));
    for (const forbidden of ['AI', '智能', '大模型']) assert.ok(!text.includes(forbidden));
  }
});

test('列表和详情照录市级地区文字；省级原样显示', () => {
  for (const region of ['青岛 · 市级', '山东 · 省级']) {
    const doc = { id: '1', region, title: '关于组织申报项目的通知', kind: '申报通知', points: [], fields: {} };
    assert.ok(textOf(render('components/doc-row/index.wxml', { doc })).includes(region));
    assert.ok(shown('detail', { doc }).includes(region));
  }
});
