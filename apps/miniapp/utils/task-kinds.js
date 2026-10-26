// 批量工具任务的类型名和任务状态：批量工具、我的记录、我的数据共用（过审版的"我的数据"也要用，所以单独放一份）
const DATA_KINDS = { compare: '批量名单比对', track: '客户相关通知表' };
const STATES = { queued: '排队中', running: '处理中', done: '已完成', failed: '失败', cancelled: '已取消' };

module.exports = { DATA_KINDS, STATES };
