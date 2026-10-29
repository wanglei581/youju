const legal = require('../../content/legal.js');
const payTerms = require('../../content/pay-terms.js');
const api = require('../../utils/api.js');
const { consentOpen, markAgreed, loginOpen, agreeFailed, featureNames } = require('../../utils/consent.js');

// 阅读协议。consent=1 时是"同意模式"：依次显示《用户协议》和《隐私保护指引》全文，
// 同意按钮放在最后，读完才能同意（首次同意"先看协议"、协议更新后重新同意都走这里）。
// 协议定稿前（loginOpen 为 false，D-056）不显示草案：入口都改为打开官方隐私保护指引，直接打开这一页时只显示"暂未开放"
// 重点条款（民法典第 496 条，10/14）：每份文件顶部列出 keyPoints，正文里以 keyParagraphs 开头的段落加粗
function withKeys(doc) {
  const keys = doc.keyParagraphs || [];
  return {
    title: doc.title,
    keyPoints: doc.keyPoints || [],
    sections: (doc.sections || []).map((s) => ({
      h: s.h,
      p: (s.p || []).map((text) => ({ text, key: keys.some((k) => String(text).startsWith(k)) })),
    })),
  };
}

Page({
  data: { docs: [], final: false, status: '', disclaimer: '', consent: false, sending: false, loginOpen: loginOpen(), pay: false, mismatch: '' },

  onLoad(query) {
    if (!this.data.loginOpen) return;
    const consent = query.consent === '1' && consentOpen();
    this.forAi = consent && query.for === 'ai';
    const isPay = !consent && query.type === 'pay';
    this.orderId = query.orderId || '';
    const mismatch = isPay && query.version && query.version !== payTerms.version ? query.version : '';
    if (mismatch) {
      this.setData({ pay: true, mismatch });
      wx.setNavigationBarTitle({ title: payTerms.title });
      return;
    }
    let docs;
    if (consent) docs = [legal.user, legal.privacy];
    // type=pay：购买页的《付费服务协议》（只在协议定稿后由购买页打开，见 utils/pay.js termsFinal）
    else if (query.type === 'pay') docs = [{ title: payTerms.title, sections: [{ h: '', p: String(payTerms.text || '').split(/\n+/).filter(Boolean) }] }];
    else docs = [query.type === 'privacy' ? legal.privacy : legal.user];
    // 付费协议还没定稿（占位）时不给"保存全文"
    this.setData({ docs: docs.map(withKeys), final: legal.final, status: legal.status, disclaimer: legal.disclaimer, consent, pay: isPay && require('../../utils/pay.js').termsFinal(payTerms) });
    wx.setNavigationBarTitle({ title: consent ? '用户协议和隐私保护指引' : docs[0].title });
  },

  feedback() { wx.navigateTo({ url: `/pages/feedback/index?type=pay${this.orderId ? `&orderId=${encodeURIComponent(this.orderId)}` : ''}` }); },

  savePay() {
    if (typeof wx.shareFileMessage !== 'function') {
      wx.showToast({ title: '当前微信版本不支持保存文件，请升级微信后再试', icon: 'none' });
      return;
    }
    const fileName = `付费服务协议_${payTerms.version}.txt`;
    const filePath = `${wx.env.USER_DATA_PATH}/${fileName}`;
    const fail = () => wx.showToast({ title: '没能保存，请稍后再试', icon: 'none' });
    try {
      wx.getFileSystemManager().writeFile({ filePath, data: `《付费服务协议》（版本 ${payTerms.version}）\n\n${payTerms.text}`, encoding: 'utf8',
        success: () => {
          try { wx.shareFileMessage({ filePath, fileName, fail: (err) => { if (!err || !/cancel/i.test(err.errMsg || '')) fail(); } }); }
          catch (err) { fail(); }
        }, fail });
    } catch (err) { fail(); }
  },

  agree() {
    if (this.data.sending) return;
    this.setData({ sending: true });
    api.agree(legal.version)
      .then(() => {
        markAgreed();
        // 从 AI 板块来的（for=ai）不写"关注和纠错可以用了"，免得以为同意的是别的
        wx.showToast({ title: this.forAi ? '已同意，可以继续使用了' : `已同意，${featureNames()}可以用了`, icon: 'none' });
        setTimeout(() => wx.navigateBack({ fail: () => wx.switchTab({ url: '/pages/me/index' }) }), 1000);
      })
      .catch((err) => {
        this.setData({ sending: false });
        agreeFailed(err);
      });
  },
});
