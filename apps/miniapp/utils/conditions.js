const KIND_LABEL = { all: '全部类型', 申报通知: '申报通知', 名单: '名单与公示', 政策: '政策' };

// 与申报无关的新闻、会议培训、招聘、行政许可、统计数据、办事指南等（服务端的文件类型）
const GUIDE = '资讯与指南';
// 项目类别（与服务端 youju.categories 一致）：按标题归类，只有申报通知和名单有
const CATEGORIES = ['资质与梯度培育', '科技计划与研发', '平台载体与园区', '人才', '表彰奖励', '首台套与新产品', '试点示范', '其他申报'];
const CATEGORY_NOTE = '按标题归类，仅供检索参考，非官方分类；只含申报通知和名单';
// 有项目类别的两类文件在类型筛选里的名字
const CATEGORY_TYPES = ['申报通知', '名单与公示'];

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

// 选了项目类别，但页签或类型里没有申报通知和名单：必然查不到，搜索页在空结果里据此提示调整
function categoryConflict(kind, types, categories) {
  if (!split(categories).length) return false;
  if (kind === '政策') return true;
  const picked = split(types);
  return picked.length > 0 && !picked.some((t) => CATEGORY_TYPES.includes(t));
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
  describe, searchUrl, levelConflict, categoryConflict, foldNote, GUIDE, CATEGORIES, CATEGORY_NOTE,
};
