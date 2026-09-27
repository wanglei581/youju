// 数据接口。接口就绪前用 mock/data.js 的演示数据；接口就绪后改为微信云托管 callContainer。
const mock = require('../mock/data.js');

function resolve(value) {
  return Promise.resolve(JSON.parse(JSON.stringify(value)));
}

function home() {
  return resolve({ stats: mock.stats, items: mock.home });
}

function search() {
  return resolve(mock.search);
}

// 演示数据只有辽宁这一份完整详情；其他文件用列表里的信息拼出基本详情
function detail(id) {
  if (mock.detail[id]) return resolve(mock.detail[id]);
  const pools = [mock.home, mock.search.items, mock.lists.items, mock.follow.changes];
  for (const pool of pools) {
    const doc = pool.find((d) => d.id === id);
    if (doc) {
      return resolve({
        id: doc.id,
        kind: doc.kind,
        tags: [doc.kind],
        region: doc.region,
        title: doc.title,
        issuer: doc.issuer,
        date: doc.date,
        url: doc.url,
        deadline: null,
        points: [['发文机关', doc.issuer]],
        excerpt: null,
        attachments: [],
        checked: null,
      });
    }
  }
  return Promise.reject(new Error('没有找到这份文件'));
}

function calendar() {
  return resolve({ today: mock.today, items: mock.calendar });
}

function lists() {
  return resolve(mock.lists);
}

function follow() {
  return resolve(mock.follow);
}

function files() {
  return resolve(mock.files);
}

module.exports = { home, search, detail, calendar, lists, follow, files };
