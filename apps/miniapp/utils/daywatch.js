const { todayISO } = require('./format.js');

// 页面停留时跨过零点也刷新：剩余天数按天计算。每分钟检查一次日期，变了就回调。
function watchDay(page, onChange) {
  stopDay(page);
  page.__day = todayISO();
  page.__dayTimer = setInterval(() => {
    const day = todayISO();
    if (day !== page.__day) {
      page.__day = day;
      onChange(day);
    }
  }, 60000);
}

function stopDay(page) {
  if (page.__dayTimer) {
    clearInterval(page.__dayTimer);
    page.__dayTimer = null;
  }
}

// 今天的日期（页面记"哪天被拦下的"用；测试里可以换成替身）
module.exports = { watchDay, stopDay, today: todayISO };
