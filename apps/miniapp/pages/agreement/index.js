const legal = require('../../content/legal.js');

Page({
  data: { doc: null, final: false, status: '', disclaimer: '' },
  onLoad(query) {
    const doc = query.type === 'privacy' ? legal.privacy : legal.user;
    this.setData({ doc, final: legal.final, status: legal.status, disclaimer: legal.disclaimer });
    wx.setNavigationBarTitle({ title: doc.title });
  },
});
