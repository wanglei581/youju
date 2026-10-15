// 省份简称与服务端 sources.onboard.PROVINCES 保持一致。城市清单由微信原生省市选择器提供，
// coverage 只说明已有数据，不承担行政区划清单，不能因当前未接入市级来源而隐藏城市选择。
const PROVINCES = ['北京', '天津', '河北', '山西', '内蒙古', '辽宁', '吉林', '黑龙江', '上海', '江苏', '浙江', '安徽', '福建', '江西', '山东', '河南', '湖北', '湖南', '广东', '广西', '海南', '重庆', '四川', '贵州', '云南', '西藏', '陕西', '甘肃', '青海', '宁夏', '新疆'];
const FULL_NAMES = ['北京市', '天津市', '河北省', '山西省', '内蒙古自治区', '辽宁省', '吉林省', '黑龙江省', '上海市', '江苏省', '浙江省', '安徽省', '福建省', '江西省', '山东省', '河南省', '湖北省', '湖南省', '广东省', '广西壮族自治区', '海南省', '重庆市', '四川省', '贵州省', '云南省', '西藏自治区', '陕西省', '甘肃省', '青海省', '宁夏回族自治区', '新疆维吾尔自治区'];
const MUNICIPALITIES = ['北京', '天津', '上海', '重庆'];
function provinceName(value) {
  const index = FULL_NAMES.indexOf(value);
  return index >= 0 ? PROVINCES[index] : PROVINCES.includes(value) ? value : '';
}
function cityName(value) { return typeof value === 'string' ? value.trim().replace(/市$/, '') : ''; }
function regionSelection(values, coverage) {
  const province = provinceName(values && values[0]);
  if (!province) throw new Error('该地区暂不在当前来源范围内，不能按其他省份查询');
  const raw = values && values[1];
  if (MUNICIPALITIES.includes(province) && (provinceName(raw) === province || ['市辖区', '县'].includes(raw))) {
    return { province, city: '', note: '直辖市按省级范围查询，不推断区县' };
  }
  if (!raw || /省直辖|自治区直辖|市辖区/.test(raw)) throw new Error('该选项未提供具体城市，请选择全省范围；不会推断省直管城市');
  const short = cityName(raw);
  const known = (coverage && coverage.cities || []).find((c) => c.province === province && cityName(c.city) === short);
  const city = known ? known.city : short;
  if (!city || city.length > 32 || /[\/,，;；\n\r]/.test(city)) throw new Error('城市名称无效，请重新选择');
  return { province, city, note: '' };
}
function uncoveredCities(keys, coverage) {
  if (!coverage || !Array.isArray(coverage.cities)) return [];
  const known = new Set(coverage.cities.map((c) => `${c.province}/${c.city}`));
  return keys.filter((key) => !known.has(key));
}
function withoutCityProvinces(provinces, cities) {
  const parents = new Set((cities || []).map((c) => c.split('/')[0]));
  return (provinces || []).filter((p) => p !== '不限地区' && !parents.has(p));
}
module.exports = { PROVINCES, FULL_NAMES, provinceName, cityName, regionSelection, uncoveredCities, withoutCityProvinces };
