const api = require('./api.js');
const { syncConsent } = require('./consent.js');

// 已订阅、还没发出的截止提醒，按文件编号：{ 文件编号: true }。以服务端为准，
// 重进页面、换设备、注销后都能对上；没同意或加载失败时返回空，不影响页面。
function remindedDocs() {
  return syncConsent().then((ok) => (ok ? api.mySubscriptions() : { items: [] }))
    .then((res) => {
      const map = {};
      res.items.forEach((s) => { if (s.kind === 'deadline' && s.docId) map[s.docId] = true; });
      return map;
    })
    .catch(() => ({}));
}

module.exports = { remindedDocs };
