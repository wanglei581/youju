const api = require('./api.js');

// 收录范围（已收录的地区和栏目、还没收录的地区及原因）。缓存 10 分钟；取不到时返回 null，
// 下次再取（页面不显示相关说明，搜索无结果时只显示普通提示）
const TTL = 10 * 60 * 1000;
let cached = null; // { at, promise }

function loadCoverage() {
  if (!cached || Date.now() - cached.at > TTL) {
    const promise = api.coverage().catch(() => { cached = null; return null; });
    cached = { at: Date.now(), promise };
  }
  return cached.promise;
}

// 还没收录的地区名，如"浙江、广西、甘肃"
function notCoveredNames(coverage) {
  return coverage && coverage.notCovered ? coverage.notCovered.map((r) => r.name).join('、') : '';
}

// 选中的地区（不含"不限地区"）全部还没收录时，返回这些地区及原因；否则返回空（不推断关键词里的地名）
function allUncovered(coverage, regions) {
  const picked = regions.filter((r) => r && r !== '不限地区');
  if (!coverage || !coverage.notCovered || !picked.length) return [];
  const reason = {};
  coverage.notCovered.forEach((r) => { reason[r.name] = r.reason; });
  if (!picked.every((name) => reason[name])) return [];
  return picked.map((name) => ({ name, reason: reason[name] }));
}

module.exports = { loadCoverage, notCoveredNames, allUncovered };
