const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// 本地图标组件（D-053 UI 第一轮）：替代 TDesign 的 t-icon，字体内嵌，不从网络加载。
const ROOT = path.join(__dirname, '..');
const SKIP = new Set(['miniprogram_npm', 'node_modules', 'tests']);
const read = (file) => fs.readFileSync(path.join(ROOT, file), 'utf8');

function files(dir = '', out = []) {
  for (const entry of fs.readdirSync(path.join(ROOT, dir), { withFileTypes: true })) {
    if (SKIP.has(entry.name) || entry.name.startsWith('.')) continue;
    const rel = path.join(dir, entry.name);
    if (entry.isDirectory()) files(rel, out);
    else out.push(rel);
  }
  return out;
}
const source = files();
const byExt = (ext) => source.filter((f) => f.endsWith(ext));
const ICON_WXSS = read('components/icon/index.wxss');
const GLYPHS = new Set([...ICON_WXSS.matchAll(/\.yj-icon-([a-z0-9-]+):before \{ content: '\\e[0-9a-f]{3}'; \}/g)].map((m) => m[1]));

function loadComponent() {
  let def;
  vm.runInNewContext(read('components/icon/index.js'), { Component(d) { def = d; } }, { filename: 'components/icon/index.js' });
  return def;
}

function styleOf(def, size, color) {
  const self = { data: {}, setData(v) { Object.assign(this.data, v); } };
  def.observers['size, color'].call(self, size, color);
  return self.data.iconStyle;
}

test('图标组件和 t-icon 用法兼容：name、size、color；纯数字 size 按 px，不写 color 继承外层颜色', () => {
  const def = loadComponent();
  assert.deepEqual(Object.keys(def.properties).sort(), ['color', 'name', 'size']);
  // 和 TDesign 一样不渲染外层节点，图标在 flex 里的位置不变
  assert.equal(def.options.virtualHost, true);
  assert.equal(styleOf(def, '48rpx', ''), 'font-size: 48rpx');
  assert.equal(styleOf(def, '34rpx', '#86909C'), 'color: #86909C; font-size: 34rpx');
  assert.equal(styleOf(def, 24, ''), 'font-size: 24px');
  assert.equal(styleOf(def, '', ''), '');
  assert.match(read('components/icon/index.wxml'), /class="yj-icon yj-icon-{{name}}" style="{{iconStyle}}"/);
  // 代码里 <t-icon> 只用到 name、size、color 和 wx:if，组件都支持
  for (const file of byExt('.wxml')) {
    for (const [tag] of read(file).matchAll(/<t-icon\b[^>]*>/g)) {
      const attrs = [...tag.matchAll(/\s([\w:-]+)=/g)].map((m) => m[1]);
      for (const attr of attrs) assert.ok(['name', 'size', 'color', 'wx:if'].includes(attr), `${file} 的 t-icon 用了 ${attr}`);
    }
  }
});

test('所有登记 t-icon 的地方都指向本地组件，不再用 TDesign 的 icon', () => {
  const owners = [];
  for (const file of byExt('.json').filter((f) => !f.startsWith('package'))) {
    const json = JSON.parse(read(file));
    const using = json.usingComponents || {};
    for (const [key, value] of Object.entries(using)) assert.ok(!/tdesign-miniprogram\/icon/.test(value), `${file} 的 ${key} 还在用 TDesign 图标`);
    if (using['t-icon']) { assert.equal(using['t-icon'], '/components/icon/index', file); owners.push(file); }
  }
  for (const file of ['app.json', 'custom-tab-bar/index.json', 'components/doc-row/index.json', 'components/notice-bar/index.json', 'pages/report/index.json', 'pages/list-board/index.json']) {
    assert.ok(owners.includes(file), `${file} 没有登记本地图标`);
  }
});

test('代码里用到的图标都在内嵌字体里，字体和 tools/icons/icons.txt 一致', () => {
  const used = new Set();
  for (const file of byExt('.wxml')) {
    for (const [tag] of read(file).matchAll(/<t-icon\b[^>]*>/g)) {
      const name = /\sname="([^"]*)"/.exec(tag)[1];
      if (!name.includes('{{')) used.add(name);
      else for (const m of name.matchAll(/'([a-z0-9-]+)'/g)) used.add(m[1]);
    }
  }
  // 名字放在数据里的（底栏、关注的图标、演示数据）
  for (const file of byExt('.js')) {
    for (const m of read(file).matchAll(/\b(?:icon|iconOn)"?\s*:\s*['"]([a-z0-9-]+)['"]/g)) {
      if (m[1] !== 'none' && m[1] !== 'success') used.add(m[1]);
    }
  }
  assert.ok(used.size >= 25, `只找到 ${used.size} 个图标，检查扫描规则`);
  for (const name of used) assert.ok(GLYPHS.has(name), `字体里没有 ${name}：加到 tools/icons/icons.txt 后重跑 build_icons.py`);
  const listed = fs.readFileSync(path.join(ROOT, '..', '..', 'tools', 'icons', 'icons.txt'), 'utf8')
    .split('\n').map((line) => line.split('#')[0].trim()).filter(Boolean);
  assert.deepEqual([...GLYPHS].sort(), [...listed].sort(), '生成的样式和 icons.txt 不一致，请重跑 build_icons.py');
});

test('图标字体内嵌在组件样式里；小程序代码里不再出现 tdesign.gtimg.com，也不从网络加载字体', () => {
  const fontFaces = [...ICON_WXSS.matchAll(/@font-face\s*{([^}]*)}/g)].map((m) => m[1]);
  assert.equal(fontFaces.length, 1);
  assert.match(fontFaces[0], /src: url\('data:font\/woff;base64,[A-Za-z0-9+/=]+'\) format\('woff'\);/);
  assert.ok(ICON_WXSS.length < 20000, `图标样式 ${ICON_WXSS.length} 字节，超过预期`);
  for (const file of source.filter((f) => /\.(js|json|wxml|wxss|wxs)$/.test(f))) {
    const text = read(file);
    assert.ok(!text.includes('tdesign.gtimg.com'), `${file} 里有 tdesign.gtimg.com`);
    if (file.endsWith('.wxss')) assert.ok(!/url\(\s*['"]?(https?:)?\/\//.test(text), `${file} 从网络加载资源`);
  }
});

test('还在用的 TDesign 弹层不开自带的关闭按钮：它内部的 t-icon 不渲染，不会去请求在线字体', () => {
  const popupUsers = byExt('.wxml').filter((f) => /<t-popup\b/.test(read(f)));
  assert.ok(popupUsers.length >= 1);
  for (const file of popupUsers) {
    for (const [tag] of read(file).matchAll(/<t-popup\b[^>]*>/g)) assert.ok(!/close-btn|closeBtn/.test(tag), `${file} 的 t-popup 开了自带关闭按钮`);
  }
  // 除弹层（及其遮罩）外，不再登记别的 TDesign 组件；新加 TDesign 组件时要先确认它不用在线字体
  for (const file of byExt('.json').filter((f) => !f.startsWith('package'))) {
    const using = JSON.parse(read(file)).usingComponents || {};
    for (const value of Object.values(using)) {
      if (value.startsWith('tdesign-miniprogram/')) assert.equal(value, 'tdesign-miniprogram/popup/popup', `${file} 登记了 ${value}`);
    }
  }
});

test('首页"截止雷达"入口和"申报日历"用不同的图标', () => {
  const home = read('pages/home/index.wxml');
  const iconOf = (label) => new RegExp(`<t-icon name="([a-z0-9-]+)"[^>]*\\/><\\/view><text>${label}<\\/text>`).exec(home)[1];
  assert.equal(iconOf('申报日历'), 'calendar');
  assert.equal(iconOf('截止雷达'), 'hourglass');
});
