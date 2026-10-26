// 这个包里有没有某个页面。过审版（首发 16 页，D-091）不带工具中心、我的记录、购买、订单、板块首页和使用帮助，
// 指向这些页面的入口一律不显示，免得点了没反应；完整版里这些页面都在，入口照旧（还要看各自的开关）。
const PAGES = require('./package-pages.js');

function has(name) {
  return PAGES.indexOf(`pages/${name}/index`) !== -1;
}

// 给模板用（WXML 里不能调函数）：页面 data 里放一份 has: pages.flags()
function flags() {
  return {
    advisor: has('advisor'),
    records: has('advisor-tasks'),
    pass: has('pass'),
    orders: has('orders'),
    help: has('help'),
    // first-release-cut:start
    ai: has('ai'),
    // first-release-cut:end
  };
}

module.exports = { has, flags };
