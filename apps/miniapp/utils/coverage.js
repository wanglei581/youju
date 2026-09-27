const api = require('./api.js');

// 收录范围（已收录的地区、还没收录的省份），每次启动取一次；取不到时返回 null，页面不显示相关说明
let pending = null;

function loadCoverage() {
  if (!pending) {
    pending = api.coverage().catch(() => { pending = null; return null; });
  }
  return pending;
}

// 查询条件里涉及的、还没收录的省份：选中的地区，或关键词里写到的省份名
function uncoveredIn(coverage, regions, q) {
  if (!coverage || !coverage.notCovered) return [];
  return coverage.notCovered.filter((name) => regions.indexOf(name) >= 0 || (q && q.indexOf(name) >= 0));
}

module.exports = { loadCoverage, uncoveredIn };
