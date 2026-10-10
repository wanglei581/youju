const api = require('../../utils/api.js');
const { rewardDate, clueTitles, removeClue } = require('../../utils/rewards.js');
Page({
  data: { loading: true, closed: false, error: '', inviteOpen: false, monthDays: 0, rewards: [], clues: [] },
  onShow() { return this.load(); },
  load() {
    const seq = this.seq = (this.seq || 0) + 1;
    this.setData({ loading: true, closed: false, error: '', inviteOpen: false });
    return api.myRewards().then((res) => Promise.all([
      api.myDeadlineClues().then((d) => clueTitles(d.items)),
      api.inviteInfo().then(() => true, () => false),
    ]).then(([clues, inviteOpen]) => {
      if (seq === this.seq) this.setData({ monthDays: res.monthDays, rewards: res.items.map((item) => Object.assign({}, item, { date: rewardDate(item.grantedAt) })), clues, inviteOpen, loading: false });
    })).catch((err) => {
      if (seq === this.seq) this.setData({ loading: false, closed: err.status === 404, error: err.status === 404 ? '' : err.message });
    });
  },
  invite() { wx.navigateTo({ url: '/pages/invite/index' }); },
  remove(e) { return removeClue(this.data.clues, e.currentTarget.dataset.id).then((ok) => { if (ok) return this.load(); }); },
});
