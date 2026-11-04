const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');

test('删除任务只清除指向该任务的本机导出记录', () => {
  let records = [{ source: 'advisor', taskId: '12' }, { source: 'advisor', taskId: '13' }, { source: 'catalogue', taskId: '12' }];
  global.wx = { getStorageSync: () => records, setStorageSync: (_key, value) => { records = value; } };
  assert.equal(require('../utils/exports.js').removeAdvisorTask(12), true);
  assert.deepEqual(records.map((item) => item.taskId), ['13', '12']);
  delete global.wx;
});

test('工具说明和页面文字采用新说法', () => {
  assert.match(require('../content/advisor-terms.js').paragraphs.join(' '), /已有批量工具使用期的/);
  const complaint = fs.readFileSync('pages/complaint/index.wxml', 'utf8');
  assert.match(complaint, /点“纠错”/);
  assert.match(require('../utils/pay.js').stateNote({ status: '已关闭' }), /点“遇到问题”/);
});
