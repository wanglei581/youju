// 测试里没给"跨过零点"（utils/daywatch.js）的页面：不起定时器（真的会每分钟查一次日期，测试进程退不出去）。
// 测跨零点的用例另外注入替身，自己调回调。
module.exports = { watchDay() {}, stopDay() {} };
