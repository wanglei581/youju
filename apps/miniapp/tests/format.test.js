const test = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const { todayISO, daysUntil, dueInfo, monthDay, cnDate, weekday } = require('../utils/format.js');

test('北京时间零点切日，只有日期的截止当天仍有效', () => {
  const before = new Date('2026-09-30T15:59:59Z');
  const after = new Date('2026-09-30T16:00:00Z');
  assert.equal(todayISO(before), '2026-09-30');
  assert.equal(todayISO(after), '2026-10-01');
  assert.equal(daysUntil('2026-09-30', before), 0);
  assert.equal(daysUntil('2026-09-30', after), -1);
  assert.equal(daysUntil('2026-10-01', before), 1);
});

test('当天截止时刻前有效，到点及之后已截止', () => {
  const deadline = { verified: true, date: '2026-09-30', time: '17:00' };
  assert.equal(dueInfo(deadline, new Date('2026-09-30T08:59:59Z')).days, 0);
  const at = dueInfo(deadline, new Date('2026-09-30T09:00:00Z'));
  assert.equal(at.days, -1);
  assert.equal(at.tone, 'past');
  assert.match(at.text, /已截止$/);
  assert.equal(dueInfo(deadline, new Date('2026-09-30T09:00:01Z')).days, -1);
  assert.equal(daysUntil(deadline.date, new Date('2026-09-30T09:00:00Z'), '17:00:01'), 0);
  assert.equal(daysUntil(deadline.date, new Date('2026-09-30T09:00:01Z'), '17:00:01'), -1);
});

test('未核验截止不展示，空时刻按日期处理', () => {
  assert.equal(dueInfo({ verified: false, date: '2026-09-30' }), null);
  assert.equal(dueInfo(null), null);
  const now = new Date('2026-09-30T15:59:59Z');
  for (const time of [null, '', '见原文', '25:00']) {
    assert.equal(daysUntil('2026-09-30', now, time), 0);
  }
});

test('手机时区和夏令时不影响北京时间及日期格式', () => {
  const modulePath = require.resolve('../utils/format.js');
  const code = `
    const assert = require('node:assert/strict');
    const f = require(${JSON.stringify(modulePath)});
    assert.equal(f.todayISO(new Date('2026-09-30T16:00:00Z')), '2026-10-01');
    assert.equal(f.daysUntil('2026-03-09', new Date('2026-03-07T16:00:00Z')), 1);
    assert.equal(f.dueInfo({verified:true,date:'2026-09-30',time:'17:00'}, new Date('2026-09-30T09:01:00Z')).days, -1);
    assert.equal(f.monthDay('2026-09-30'), '9月30日');
    assert.equal(f.cnDate('2026-09-30'), '2026年9月30日');
    assert.equal(f.weekday('2026-09-30'), '周三');
  `;
  for (const TZ of ['UTC', 'Asia/Shanghai', 'America/Los_Angeles', 'Pacific/Auckland']) {
    const result = spawnSync(process.execPath, ['-e', code], { env: { TZ }, encoding: 'utf8' });
    assert.equal(result.status, 0, `${TZ}: ${result.stderr}`);
  }
  assert.equal(monthDay('2026-09-30'), '9月30日');
  assert.equal(cnDate('2026-09-30'), '2026年9月30日');
  assert.equal(weekday('2026-09-30'), '周三');
});
