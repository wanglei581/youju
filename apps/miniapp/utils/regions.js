// 省份简称与服务端 sources.onboard.PROVINCES 保持一致。省市区清单由微信原生选择器提供（picker mode="region" 默认三级，
// custom-item 在每列顶部加“全部”），coverage 只说明已有数据，不承担行政区划清单，不能因当前未接入市级或区县来源而隐藏选项。
const PROVINCES = ['北京', '天津', '河北', '山西', '内蒙古', '辽宁', '吉林', '黑龙江', '上海', '江苏', '浙江', '安徽', '福建', '江西', '山东', '河南', '湖北', '湖南', '广东', '广西', '海南', '重庆', '四川', '贵州', '云南', '西藏', '陕西', '甘肃', '青海', '宁夏', '新疆'];
const FULL_NAMES = ['北京市', '天津市', '河北省', '山西省', '内蒙古自治区', '辽宁省', '吉林省', '黑龙江省', '上海市', '江苏省', '浙江省', '安徽省', '福建省', '江西省', '山东省', '河南省', '湖北省', '湖南省', '广东省', '广西壮族自治区', '海南省', '重庆市', '四川省', '贵州省', '云南省', '西藏自治区', '陕西省', '甘肃省', '青海省', '宁夏回族自治区', '新疆维吾尔自治区'];
const MUNICIPALITIES = ['北京', '天津', '上海', '重庆'];
// 选择器每列顶部的自定义项（custom-item）：选它表示这一级不限——城市一栏是全省、区县一栏是全市，不是地名
const ALL = '全部';
// 城市、区县各是一个名称：和服务端一致，不超过 32 字，不含 / \ , ， ; ； 、 | 和制表、换行
const SEPARATORS = /[\/\\,，;；、|\t\n\r]/;
// 旧版行政区划数据里区县一栏的统计占位，不是具体区县
const PLACEHOLDER_DISTRICTS = ['市辖区'];
const unique = (values) => [...new Set(values)];

function provinceName(value) {
  const index = FULL_NAMES.indexOf(value);
  return index >= 0 ? PROVINCES[index] : PROVINCES.includes(value) ? value : '';
}
function cityName(value) { return typeof value === 'string' ? value.trim().replace(/市$/, '') : ''; }
const singleName = (value) => typeof value === 'string' && value.length > 0 && value.length <= 32 && !SEPARATORS.test(value);

// 条件里的写法：城市"省/市"，区县"省/市/区县"。直辖市未选区县按省级；选了区县时市写本市简称（"北京/北京/朝阳区"）
function areaParts(key) {
  const [province = '', city = '', district = ''] = String(key || '').split('/');
  return { province, city, district };
}
// 显示用："山东·青岛""山东·青岛·市南区"；直辖市不重复写市名（"北京·朝阳区"）
function areaLabel(key) {
  const { province, city, district } = areaParts(key);
  return [province, city !== province ? city : '', district].filter(Boolean).join('·');
}
// 来源可能记"青岛市"、条件里存"青岛"：只在同一省内按 cityName 比较；直辖市的市写本市或留空都算本市。
// 区县名完整精确比较，不去掉"区""县"，同名区县不同省市不会混在一起
function cityKey(province, city) {
  const short = cityName(city);
  return `${province}/${MUNICIPALITIES.includes(province) && !short ? province : short}`;
}
const districtKey = (province, city, district) => `${cityKey(province, city)}/${district}`;
const provinceOf = (key) => areaParts(key).province;
const cityOf = (key) => { const a = areaParts(key); return cityKey(a.province, a.city); };
const districtOf = (key) => { const a = areaParts(key); return districtKey(a.province, a.city, a.district); };

// 微信省市区选择器选中的值 → { province, city, district, note }。
// 省份一栏选“全部”时返回空省份，由页面决定是改为不限还是提示另选；城市一栏“全部”是全省，区县一栏“全部”是全市。
// 直辖市未选区县按省级（city 为空）；选了区县时 city 记本市简称，不把区县挪到城市。省直辖县级等占位给不出所属城市，
// 一律明确不可查询，不推断上级城市；港澳台等不在来源合同里的地区明确报错，不映射到其他省。
function regionSelection(values, coverage) {
  const [rawProvince, rawCity, rawDistrict] = [0, 1, 2].map((i) => (Array.isArray(values) && typeof values[i] === 'string' ? values[i].trim() : ''));
  if (!rawProvince || rawProvince === ALL) return { province: '', city: '', district: '', note: '' };
  const province = provinceName(rawProvince);
  if (!province) throw new Error('该地区暂不在当前来源范围内，不能按其他省份查询');
  const wholeCity = !rawCity || rawCity === ALL;
  const wholeDistrict = !rawDistrict || rawDistrict === ALL;
  if (!wholeDistrict && PLACEHOLDER_DISTRICTS.includes(rawDistrict)) throw new Error('该选项没有给出具体区县，请选择具体区县，或在区县一栏选“全部”查询市级来源');
  if (!wholeDistrict && !singleName(rawDistrict)) throw new Error('区县名称无效，请重新选择');
  if (MUNICIPALITIES.includes(province) && (wholeCity || provinceName(rawCity) === province || ['市辖区', '县'].includes(rawCity))) {
    if (wholeDistrict) return { province, city: '', district: '', note: '直辖市没选具体区县：查整个直辖市（各级来源都在内）' };
    return { province, city: province, district: rawDistrict, note: '' };
  }
  if (wholeCity) {
    if (!wholeDistrict) throw new Error('没有给出所属城市，无法确定区县，请重新选择');
    return { province, city: '', district: '', note: '' };
  }
  if (/省直辖|自治区直辖|市辖区/.test(rawCity) || rawCity === '县') {
    throw new Error(wholeDistrict ? '该选项未提供具体城市，请选择全省范围；不会推断省直管城市'
      : '该区县的上级是“省直辖”等占位，无法确定所属城市，暂不能按区县查询；可改选全省');
  }
  const short = cityName(rawCity);
  if (!singleName(short)) throw new Error('城市名称无效，请重新选择');
  if (wholeDistrict) {
    const known = (coverage && Array.isArray(coverage.cities) ? coverage.cities : []).find((c) => c.province === province && cityName(c.city) === short);
    return { province, city: known ? known.city : short, district: '', note: '' };
  }
  const known = (coverage && Array.isArray(coverage.districts) ? coverage.districts : [])
    .find((d) => d.province === province && cityName(d.city) === short && d.district === rawDistrict);
  return { province, city: known && singleName(known.city) ? known.city : short, district: rawDistrict, note: '' };
}

// 选择器重新打开时停在当前省份（城市、区县停在“全部”），免得直接点确定把已选省份改掉
function regionValue(province) {
  const index = PROVINCES.indexOf(province);
  return index >= 0 ? [FULL_NAMES[index], ALL, ALL] : [];
}

// 城市：收录范围没取到时不在本机拦，由服务端核对（未收录返回 422，不改为全省）
function uncoveredCities(keys, coverage) {
  if (!coverage || !Array.isArray(coverage.cities)) return [];
  const known = new Set(coverage.cities.map((c) => cityKey(c.province, c.city)));
  return keys.filter((key) => !known.has(cityOf(key)));
}
// 区县只看自己的省、市、区县三项是否已收录，不要求先有市级来源。收录范围没取到、或服务端没给 districts 时
// 一律算未确认：旧服务会忽略区县参数，当作已收录去查就等于偷偷按全市或全省查
function uncoveredDistricts(keys, coverage) {
  if (!coverage || !Array.isArray(coverage.districts)) return keys.slice();
  const known = new Set(coverage.districts.map((d) => districtKey(d.province, d.city, d.district)));
  return keys.filter((key) => !known.has(districtOf(key)));
}

// 具体城市、区县替换同省的全省条件（不限地区也去掉）
function withoutCityProvinces(provinces, cities, districts) {
  const parents = new Set((cities || []).concat(districts || []).map(provinceOf));
  return (provinces || []).filter((p) => p !== '不限地区' && !parents.has(p));
}
// 具体区县替换所属城市的全市条件
function withoutDistrictCities(cities, districts) {
  const parents = new Set((districts || []).map((key) => cityOf(key)));
  return (cities || []).filter((key) => !parents.has(cityOf(key)));
}

// 一次省市区选择并进多选条件（搜索、我的客户共用）：选全省去掉该省已选城市和区县；选全市去掉该市已选区县和全省；
// 选具体区县去掉所属城市和全省。其他省份、城市、区县都保留；同一处只留一条（城市按同省去末尾“市”比较）
function applyAreaChoice(area, choice) {
  const regions = (area.regions || []).filter((r) => r !== '不限地区');
  const cities = area.cities || [];
  const districts = area.districts || [];
  const { province, city, district } = choice;
  const put = (list, key, same) => (list.some(same) ? unique(list.map((k) => (same(k) ? key : k))) : list.concat(key));
  if (!city) {
    return { regions: unique(regions.concat(province)), cities: cities.filter((k) => provinceOf(k) !== province), districts: districts.filter((k) => provinceOf(k) !== province) };
  }
  const inCity = (k) => cityOf(k) === cityKey(province, city);
  if (!district) {
    return { regions: regions.filter((r) => r !== province), cities: put(cities, `${province}/${city}`, inCity), districts: districts.filter((k) => !inCity(k)) };
  }
  const same = (k) => districtOf(k) === districtKey(province, city, district);
  return { regions: regions.filter((r) => r !== province), cities: cities.filter((k) => !inCity(k)), districts: put(districts, `${province}/${city}/${district}`, same) };
}

module.exports = {
  PROVINCES, FULL_NAMES, MUNICIPALITIES, ALL, SEPARATORS, provinceName, cityName, regionSelection, regionValue, areaParts, areaLabel,
  uncoveredCities, uncoveredDistricts, withoutCityProvinces, withoutDistrictCities, applyAreaChoice,
};
