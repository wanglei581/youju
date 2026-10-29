const api = require('../../utils/api.js');
const { exportDocs } = require('../../utils/exporter.js');
// 包里有使用帮助时，页底放"数据说明 ›"：截止日期的几种写法写在帮助里（agy 第 3 轮）
const { flags } = require('../../utils/pages.js');
// 统一按北京时间显示生成时间，不随设备时区改变，也不把原始 ISO 时区串呈现给用户。
function generatedTime(value) {
  if (!value) return '生成时间未提供';
  const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (dateOnly) return `${dateOnly[1]}年${Number(dateOnly[2])}月${Number(dateOnly[3])}日`;
  const stamp = Date.parse(value);
  if (!Number.isFinite(stamp)) return '生成时间未提供';
  const date = new Date(stamp + 8 * 60 * 60 * 1000);
  const pad = n => String(n).padStart(2, '0');
  return `${date.getUTCFullYear()}年${date.getUTCMonth() + 1}月${date.getUTCDate()}日 ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}（北京时间）`;
}
// 站内看结果（D-089）：截止一栏照导出文件的内容写，但不搬文件里给对账用的写法——"（机器摘录）"在页面上叫"原文摘录"，
// 没有能显示的日期只写一句"截止时间见原文"，不写"截止未核对，以原文为准"（10/29 创始人：这类话写在使用帮助里）
const SEE_ORIGINAL = '截止时间见原文';
const EXCERPT_MARK = '（机器摘录）';
function deadlineText(item) {
  const value = String(item.deadline || '');
  if (!/^\d{4}-\d{2}-\d{2}/.test(value)) return SEE_ORIGINAL;
  const role = String(item.date_role || '');
  // 文件里日期和类型后面各标一次"（机器摘录）"，页面上只在末尾标一次"（原文摘录）"：2026-11-06 企业申报截止（原文摘录）
  const excerpt = value.includes(EXCERPT_MARK) || role.includes(EXCERPT_MARK);
  const kind = role.split(EXCERPT_MARK).join('');
  return `${value.split(EXCERPT_MARK).join('')}${kind ? ` ${kind}` : ''}${excerpt ? '（原文摘录）' : ''}`;
}
// 文件里的写法搬到页面上要收一收（10/29 独立审阅按真实数据看到的）：空的发布状态不留两个"·"；支持方式没核对时文件里写
// "见原文"、不是申报通知写"—"，页面上都不写这一行；出处只写网站名称，去掉括号里的网址（D-097，文件里照旧带）
const NO_SUPPORT = ['', '见原文', '—'];
const HOST_IN_BRACKETS = /（[^（）\u4e00-\u9fff]*\.[^（）\u4e00-\u9fff]*）/g;
function catalogueItem(item) {
  const support = String(item.support || '').trim();
  return {
    ...item,
    deadlineText: deadlineText(item),
    metaText: [item.kind, item.status, item.region].filter(Boolean).join(' · '),
    supportText: NO_SUPPORT.includes(support) ? '' : support,
    siteText: String(item.sites || '').replace(HOST_IN_BRACKETS, ''),
  };
}
Page({
  data: { loading: false, error: '', result: null, current: false, saving: false, advisor: false, page: 1, nextCursor: null, changed: false, has: flags() },
  onLoad(q) {
    let value = q.ids || '';
    try { value = decodeURIComponent(value); } catch (e) { value = ''; }
    this.ids = value.split(',').filter(Boolean);
    this.taskId = String(q.taskId || '');
    this.setData({ current: q.current === '1', advisor: q.mode === 'advisor' });
  },
  onShow() { this.visible = true; return this.restart(); },
  onHide() { this.visible = false; this.seq = (this.seq || 0) + 1; this.setData({ result: null, loading: false, saving: false }); },
  onUnload() { this.onHide(); },
  restart() { this.cursors = ['']; this.cursorIndex = 0; this.revision = ''; return this.load(); },
  load() {
    const seq = this.seq = (this.seq || 0) + 1;
    const current = () => this.visible && seq === this.seq;
    this.setData({ loading: true, error: '', result: null, changed: false });
    if (this.data.advisor ? !/^\d+$/.test(this.taskId) : !this.ids.length || this.ids.length > 200 || this.ids.some((id) => !/^\d+$/.test(id))) {
      this.setData({ loading: false, error: '结果地址无效，请返回重新选择。' }); return Promise.resolve();
    }
    const request = this.data.advisor ? api.advisorReading(this.taskId, { cursor: this.cursors[this.cursorIndex], revision: this.revision }) : api.exportPreview(this.ids);
    return request.then((res) => {
      if (!current()) return;
      const kindOk = this.data.advisor ? ['compare', 'track'].includes(res && res.kind) : res && res.kind === 'catalogue';
      if (!res || !kindOk || !res.revision || !Array.isArray(res.items) || res.items.length > 200) throw new Error('结果信息不完整，请重试');
      if (this.revision && res.revision !== this.revision) throw Object.assign(new Error('内容版本已变化'), { status: 409 });
      const next = res.nextCursor == null ? null : String(res.nextCursor);
      if (next !== null && (!next || this.cursors.slice(0, this.cursorIndex + 1).includes(next))) throw new Error('结果分页信息有误，请重试');
      this.revision = res.revision;
      const items = this.data.advisor ? res.items : res.items.map(catalogueItem);
      this.setData({ result: { ...res, items, generatedAtText: generatedTime(res.generatedAt) }, page: this.cursorIndex + 1, nextCursor: next });
    }).catch((err) => {
      if (!current()) return;
      if ([403, 404, 409, 410].includes(err.status)) { this.revision = ''; this.cursors = ['']; this.cursorIndex = 0; }
      this.setData({ result: null, nextCursor: null, changed: err.status === 409, error: err.message || '结果未加载，请重试' });
    }).then(() => { if (current()) this.setData({ loading: false }); });
  },
  retry() { if (this.data.saving) return; return this.data.changed ? this.restart() : this.load(); },
  next() {
    if (this.data.loading || this.data.saving || this.data.nextCursor === null) return;
    this.cursors = this.cursors.slice(0, this.cursorIndex + 1); this.cursors.push(this.data.nextCursor); this.cursorIndex += 1;
    return this.load();
  },
  previous() { if (this.data.loading || this.data.saving || this.cursorIndex < 1) return; this.cursorIndex -= 1; return this.load(); },
  detail(e) { wx.navigateTo({ url: `/pages/detail/index?id=${encodeURIComponent(e.currentTarget.dataset.id)}` }); },
  help() { if (this.data.has.help) wx.navigateTo({ url: '/pages/help/index?topic=deadline' }); },
  read(e) { wx.navigateTo({ url: `/pages/reader/index?id=${encodeURIComponent(e.currentTarget.dataset.id)}` }); },
  save() {
    if (!this.data.result || this.data.saving) return;
    const seq = this.seq; const result = this.data.result;
    this.setData({ saving: true, error: '' });
    const stale = () => { if (this.visible && seq === this.seq) this.setData({ result: null, error: '内容已更新，请重新查看结果后保存。', changed: true }); };
    const job = this.data.advisor ? require('../../utils/data-tools.js').openResult(this.taskId, {
      task: { kind: result.kind, rowCount: result.rowCount }, current: () => this.visible && seq === this.seq,
    }) : exportDocs(this.ids, { revision: result.revision, label: result.title, onStale: stale, current: () => this.visible && seq === this.seq });
    return job.catch((err) => { if (this.visible && seq === this.seq && !err.cancelled) this.setData({ error: err.message }); })
      .then(() => { if (this.visible && seq === this.seq) this.setData({ saving: false }); });
  },
});
