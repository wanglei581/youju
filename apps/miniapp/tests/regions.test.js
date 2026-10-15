const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { PROVINCES, FULL_NAMES, regionSelection, uncoveredCities } = require('../utils/regions.js');
test('31 省份选项与真实后端来源清单一致，每个全称都可规范化；城市来自微信而非残缺静态清单', () => {
  const source = fs.readFileSync(path.join(__dirname, '../../../backend/src/youju/sources/onboard.py'), 'utf8');
  const backend = [...source.match(/PROVINCES = \(([\s\S]*?)\)/)[1].matchAll(/"([^"]+)"/g)].map(m => m[1]).filter(p => p !== '国家');
  assert.deepEqual(PROVINCES, backend); assert.equal(new Set(PROVINCES).size, 31);
  FULL_NAMES.forEach((name, index) => assert.equal(regionSelection([name, '合成市']).province, PROVINCES[index]));
});
test('自治州、地区名称保留，直辖市按省级，省直管占位和不支持地区不虚构城市', () => {
  assert.equal(regionSelection(['吉林省', '延边朝鲜族自治州']).city, '延边朝鲜族自治州');
  assert.equal(regionSelection(['新疆维吾尔自治区', '阿克苏地区']).city, '阿克苏地区');
  assert.equal(regionSelection(['北京市', '市辖区']).city, '');
  assert.equal(regionSelection(['山东省', '青岛市'], { cities: [{ province: '山东', city: '青岛市' }] }).city, '青岛市');
  assert.throws(() => regionSelection(['河南省', '省直辖县级行政区划']), /具体城市/);
  assert.throws(() => regionSelection(['香港特别行政区', '香港岛']), /暂不在/);
  assert.deepEqual(uncoveredCities(['山东/青岛'], { cities: [] }), ['山东/青岛']);
});
