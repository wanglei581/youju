const { areaLabel } = require('./regions.js');

const KIND_LABEL = { all: '全部类型', 申报通知: '申报通知', 名单: '名单与公示', 政策: '政策' };

// 与申报无关的新闻、会议培训、招聘、行政许可、统计数据、办事指南等（服务端的文件类型）
const GUIDE = '资讯与指南';
// 项目类别（与服务端 youju.categories 一致）：按标题归类，只有申报通知和名单有
const CATEGORIES = ['资质与梯度培育', '科技计划与研发', '平台载体与园区', '人才', '表彰奖励', '首台套与新产品', '试点示范', '其他申报'];
// 按标题归类的三组（项目类别、支持方式、产业方向）共用的说明：筛选弹层在这三组右栏顶部各显示一行。
// 下面三个 *_NOTE 只写各组特有的补充，TAG_NOTE + '；' + 补充 就是原来每组的完整说明，意思一个字不少
const TAG_NOTE = '按标题归类，仅供检索参考，非官方分类';
const CATEGORY_NOTE = '只含申报通知和名单';
// 有项目类别的两类文件在类型筛选里的名字
const CATEGORY_TYPES = ['申报通知', '名单与公示'];
// 支持方式、产业方向（与服务端 youju.title_tags 一致）：按标题关键词归类，只用于筛选，列表和详情里
// 不显示。支持方式用详情页核对字段的同一套词，详情页"支持方式"卡片只显示核对原文后标注的；
// "征集入选"只在筛选里有。支持方式只有申报通知和名单有；产业方向所有类型都有，一份文件可以有多个
// 或没有。不显示金额
const SUPPORT_MODES = ['资金支持', '贷款贴息', '资格认定', '入库备案', '项目立项', '征集入选'];
const SUPPORT_NOTE = '只含申报通知和名单，不代表一定提供这种支持';
const INDUSTRIES = ['数字经济', '先进制造', '新材料与化工', '生物医药与健康', '新能源与绿色低碳', '电子信息与集成电路', '现代农业', '文旅与现代服务业'];

function split(value) {
  return (value || '').split(',').map((v) => v.trim()).filter(Boolean);
}

// 地区和层级（UI 第二轮起）：国家部委只在"层级"里选。界面上的意思是"层级选哪几层，地区只管省份那部分"：
//   层级含国家部委 → 国家部委的文件不受地区限制，和所选地区的文件一起列出；
//   层级含省级/市级 → 所选地区（没选就是全部省份）相应层级的文件；
//   层级没选       → 只按地区（没选地区就是全部）。
// 服务端 /api/v1/search 是地区、层级两组取交集，地区里的"国家部委"和层级的"国家部委"是同一个条件，
// 所以请求这样拼（requestScope），后端不用改：
//   选了地区且层级含国家部委：regions=国家部委,省份；层级同时含省级或市级时传所选 levels
//     （(国家部委 ∪ 省份) ∩ (国家部委 ∪ 省级) = 国家部委 ∪ 省份的省级），否则不传 levels
//     （= 国家部委 ∪ 省份的全部文件，包括已收录的市级）；
//   其余照原样：regions=省份或不限地区，levels=所选层级。
// 城市、区县（cities、districts 另传）和省份同样算"选了地区"。
const NATIONAL = '国家部委';

function requestScope(region, level, cities = [], districts = []) {
  const provinces = (region || []).filter((r) => r !== '不限地区' && r !== NATIONAL);
  const levels = level || [];
  if ((provinces.length || cities.length || districts.length) && levels.includes(NATIONAL)) {
    return { regions: [NATIONAL].concat(provinces).join(','), levels: levels.some((l) => l !== NATIONAL) ? levels.join(',') : undefined };
  }
  return { regions: (region || []).join(','), levels: levels.length ? levels.join(',') : undefined };
}

// 反过来：网址参数、保存的关注条件（服务端的写法）换成界面上的地区和层级，requestScope 拼回去的请求
// 和原来结果相同。旧版本的条件也走这里（地区里可能有国家部委）。只有两种原来必然查不到的组合换不出
// 相同结果，按字面意思换：地区只有国家部委、层级只选省级 → 层级国家部委 + 省级；地区是省份、
// 层级只选国家部委 → 现在列出国家部委和这些省份的文件。
function migrateNational(regions, levels) {
  const region = regions || [];
  const level = levels || [];
  const provinces = region.filter((r) => r !== NATIONAL && r !== '不限地区');
  const local = level.filter((l) => l !== NATIONAL);
  const national = level.includes(NATIONAL);
  if (!region.includes(NATIONAL)) {
    if (provinces.length && national && local.length) return { regions: provinces, levels: local };
    return { regions: region, levels: level };
  }
  if (!provinces.length) return { regions: ['不限地区'], levels: local.length && !national ? [NATIONAL].concat(local) : [NATIONAL] };
  if (!level.length) return { regions: provinces, levels: [NATIONAL] };
  if (national && !local.length) return { regions: ['不限地区'], levels: [NATIONAL] };
  if (!national) return { regions: provinces, levels: local };
  return { regions: provinces, levels: [NATIONAL].concat(local) };
}

// 选了项目类别（或支持方式），但页签或类型里没有申报通知和名单：必然查不到，搜索页在空结果里
// 据此提示调整
function categoryConflict(kind, types, categories) {
  if (!split(categories).length) return false;
  if (kind === '政策') return true;
  const picked = split(types);
  return picked.length > 0 && !picked.some((t) => CATEGORY_TYPES.includes(t));
}

function supportConflict(kind, types, supports) {
  return categoryConflict(kind, types, supports);
}

// 折叠说明：折叠了的写"已折叠 X 篇……点开查看"；已经点开、列表里有的写"……收起"；都没有时不写
function foldNote(folded, shown) {
  if (folded > 0) return { text: `已折叠 ${folded} 篇非申报类文件（${GUIDE}），`, link: '点开查看' };
  if (shown > 0) return { text: `已展开 ${shown} 篇非申报类文件（${GUIDE}），`, link: '收起' };
  return { text: '', link: '' };
}

// 关注条件的一句话说明，如"山东、北京 · 省级 · 申报通知 · 近 30 天"；城市、区县写成"山东·青岛""山东·青岛·市南区"
function describe(c) {
  const parts = [];
  if (c.q) parts.push(`"${c.q}"`);
  const regions = split(c.regions).filter((r) => r !== '不限地区');
  const areas = regions.concat(split(c.cities).map(areaLabel), split(c.districts).map(areaLabel));
  parts.push(areas.length ? areas.join('、') : '不限地区');
  const levels = split(c.levels);
  if (levels.length) parts.push(levels.join('、'));
  const types = split(c.types);
  parts.push(types.length ? types.join('、') : KIND_LABEL[c.kind] || '全部类型');
  const statuses = split(c.status);
  if (statuses.length) parts.push(`发布阶段：${statuses.join('、')}`);
  const depts = split(c.depts);
  if (depts.length) parts.push(`发文部门：${depts.join('、')}`);
  const categories = split(c.categories);
  if (categories.length) parts.push(`项目类别：${categories.join('、')}`);
  const supports = split(c.supports);
  if (supports.length) parts.push(`支持方式：${supports.join('、')}`);
  const industries = split(c.industries);
  if (industries.length) parts.push(`产业方向：${industries.join('、')}`);
  if (c.time) parts.push(c.time);
  if (c.open === 1 || c.open === '1') parts.push('只看未截止');
  return parts.join(' · ');
}

function searchUrl(c) {
  const params = Object.keys(c || {}).filter((k) => c[k] !== undefined && c[k] !== null && c[k] !== '')
    .map((k) => `${encodeURIComponent(k)}=${encodeURIComponent(c[k])}`);
  return `/pages/search/index?${params.join('&')}`;
}

module.exports = {
  describe, searchUrl, requestScope, categoryConflict, supportConflict, foldNote, migrateNational, NATIONAL, GUIDE,
  CATEGORIES, TAG_NOTE, CATEGORY_NOTE, SUPPORT_MODES, SUPPORT_NOTE, INDUSTRIES,
};
