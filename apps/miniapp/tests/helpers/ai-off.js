// 测试里没给"AI 板块显示不显示"（utils/ai-access.js）的页面和组件：当作 AI 关着（首发版），后台已确认、不发请求。
// 测 AI 板块本身的用例另外注入真的 ai-access.js（tests/ai-section.test.js）。
const OFF = Object.freeze({ visible: false, confirmed: true, available: false });
module.exports = {
  current: () => Object.assign({}, OFF),
  subscribe: () => () => {},
  ensure: () => Promise.resolve(Object.assign({}, OFF)),
  refresh: () => Promise.resolve(Object.assign({}, OFF)),
  canOpen: () => false,
  clear() {},
  rememberDoc() {},
  lastDoc: () => null,
};
