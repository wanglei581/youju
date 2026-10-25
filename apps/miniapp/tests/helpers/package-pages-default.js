// 测试里没单独给的时候用真的 utils/pages.js（完整版的页面清单）和 utils/advisor.js（记录类型、状态的叫法）。
// 要模拟过审版（首发 16 页）的，测试自己传 '../../utils/pages.js' 的替身
module.exports = {
  pages: require('../../utils/pages.js'),
  advisor: require('../../utils/advisor.js'),
};
