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
const SUPPORT_NOTE = '只含申报通知和名单，不代表一定提供这种支持。文件详情里的“支持方式”是核对原文后标注的；“征集入选”只用于筛选，详情里没有这一项';
const INDUSTRIES = ['数字经济', '先进制造', '新材料与化工', '生物医药与健康', '新能源与绿色低碳', '电子信息与集成电路', '现代农业', '文旅与现代服务业'];
const INDUSTRY_NOTE = '一份文件可能有多个方向，也可能没有';

function split(value) {
  return (value || '').split(',').map((v) => v.trim()).filter(Boolean);
}

// 地区和层级同时选时两项都要满足（与服务端 /api/v1/search 一致）。"国家部委"在两组里是同一类
// 来源；地区只选了省份而层级只选国家部委，或地区只选国家部委而层级只选省级，必然查不到，
// 搜索页在空结果里据此提示调整。
function levelConflict(regions, levels) {
  const picked = split(regions).filter((r) => r !== '不限地区');
  const lv = split(levels);
  if (!picked.length || !lv.length) return false;
  const national = picked.includes('国家部委') && lv.includes('国家部委');
  const provincial = picked.some((r) => r !== '国家部委') && lv.includes('省级');
  return !national && !provincial;
}

// 国家部委现在只在"层级"里选（UI 第二轮）。旧条件（保存的关注、网址参数）的地区里可能还有它，
// 打开搜索时按服务端的规则（地区、层级组间取交集，两处"国家部委"是同一个条件）换成层级，
// 只做结果完全相同的换法：
//   地区只有国家部委，层级没选或含国家部委 → 层级"国家部委"（地区不限）
//   地区是国家部委 + 省份，层级只选国家部委     → 层级"国家部委"（地区不限）
//   地区是国家部委 + 省份，层级只选省级         → 地区去掉国家部委（层级仍是省级）
// 其余情况没有结果相同的新写法，原样保留，结果不变：
//   国家部委 + 省份、层级没选或两级都选（"国家部委和山东的文件"，新界面的地区、层级取交集表达不了）；
//   地区只有国家部委、层级只选省级（本来就查不到，空结果里照常提示调整）。
// 搜索页把保留下来的"国家部委"在地区里显示为已选，用户去掉后就不再出现。
const NATIONAL = '国家部委';

function migrateNational(regions, levels) {
  const region = regions || [];
  const level = levels || [];
  if (!region.includes(NATIONAL)) return { regions: region, levels: level };
  const provinces = region.filter((r) => r !== NATIONAL && r !== '不限地区');
  const only = (value) => level.length === 1 && level[0] === value;
  if (!provinces.length && (!level.length || level.includes(NATIONAL))) return { regions: ['不限地区'], levels: [NATIONAL] };
  if (provinces.length && only(NATIONAL)) return { regions: ['不限地区'], levels: [NATIONAL] };
  if (provinces.length && only('省级')) return { regions: provinces, levels: ['省级'] };
  return { regions: region, levels: level };
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

// 关注条件的一句话说明，如"山东、北京 · 省级 · 申报通知 · 近 30 天"
function describe(c) {
  const parts = [];
  if (c.q) parts.push(`"${c.q}"`);
  const regions = split(c.regions).filter((r) => r !== '不限地区');
  parts.push(regions.length ? regions.join('、') : '不限地区');
  const levels = split(c.levels);
  if (levels.length) parts.push(levels.join('、'));
  parts.push(KIND_LABEL[c.kind] || '全部类型');
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
  describe, searchUrl, levelConflict, categoryConflict, supportConflict, foldNote, migrateNational, NATIONAL, GUIDE,
  CATEGORIES, TAG_NOTE, CATEGORY_NOTE, SUPPORT_MODES, SUPPORT_NOTE, INDUSTRIES, INDUSTRY_NOTE,
};
