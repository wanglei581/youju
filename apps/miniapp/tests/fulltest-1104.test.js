const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

test('删除任务只清除指向该任务的本机导出记录，本机那份文件也一起删', () => {
  let records = [{ source: 'advisor', taskId: '12', path: 'wxfile://usr/有据导出_a.xlsx' }, { source: 'advisor', taskId: '13', path: 'wxfile://usr/有据导出_b.xlsx' }, { source: 'catalogue', taskId: '12', path: 'wxfile://usr/有据导出_c.xlsx' }];
  global.wx = { getStorageSync: () => records, setStorageSync: (_key, value) => { records = value; } };
  const removed = [];
  assert.equal(require('../utils/exports.js').removeAdvisorTask(12, (path) => removed.push(path)), true);
  assert.deepEqual(records.map((item) => item.taskId), ['13', '12']);
  assert.deepEqual(removed, ['wxfile://usr/有据导出_a.xlsx']);
  // 没有指向它的记录：不写存储，照样算成功
  assert.equal(require('../utils/exports.js').removeAdvisorTask(99, () => removed.push('x')), true);
  assert.deepEqual(removed, ['wxfile://usr/有据导出_a.xlsx']);
  delete global.wx;
});

test('工具说明和页面文字采用新说法', () => {
  assert.match(require('../content/advisor-terms.js').paragraphs.join(' '), /已有批量工具使用期的/);
  // 按测试文件的位置找页面（CI 从仓库根目录跑，相对路径找不到）
  const complaint = fs.readFileSync(path.join(__dirname, '..', 'pages/complaint/index.wxml'), 'utf8');
  assert.match(complaint, /点“纠错”/);
  assert.match(require('../utils/pay.js').stateNote({ status: '已关闭' }), /点“遇到问题”/);
});
