const api = require('../../utils/api');
const settings = require('../../utils/settings');
const ui = require('../../utils/ui');
const app = getApp();

Page({
  data: { address: '', checking: false, healthText: '', healthOk: false },
  onLoad() { this.setData({ address: settings.getBaseUrl() }); },
  onInput(e) { this.setData({ address: e.detail.value }); },
  onSave() {
    const result = settings.setBaseUrl(this.data.address);
    if (!result.ok) { ui.alert('地址无效', result.error); return; }
    this.setData({ address: result.value, healthText: '' });
    ui.toast('服务地址已保存');
  },
  onReset() {
    settings.resetBaseUrl();
    this.setData({ address: settings.getBaseUrl(), healthText: '' });
    ui.toast('已恢复本机默认地址');
  },
  onCheck() {
    const result = settings.normalizeBaseUrl(this.data.address);
    if (!result.ok) { ui.alert('地址无效', result.error); return; }
    this.setData({ checking: true, healthText: '正在检查…' });
    api.checkHealth(result.value).then((health) => {
      app.setHealth(health);
      this.setData({ checking: false, healthText: health.message, healthOk: health.ok });
    });
  },
});
