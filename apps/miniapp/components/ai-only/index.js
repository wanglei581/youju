// 搜索页"只看能 AI 解读的"（D-075）：从 AI 首页"近 7 天能解读的申报通知 · 全部"进来时带上，只管这一次查询，
// 不算筛选条件、不进关注和转发。bar 是结果上面那一条，exit 是没有结果时"去掉"的按钮（点了发 remove），
// hint 是去掉了那一条、还在选通知时只留下的"怎么选"那一行。
// pick：从 AI 首页的两项能力（interpret、clause）来选通知时，bar 下面一行写怎么选——点行上的哪个按钮、点标题看全文；
// 从条款对照来的，那一条写"只看能做条款对照的"（后台两样资格一样，ai=1 筛出来的都能做对照；agy 10/22：
// 写"能 AI 解读的"会让人以为进错了）。10/22 第 0 步；AI 的字只写在 AI 组件里，搜索页本身不出现
const HINTS = {
  interpret: '点“AI 解读”直接开始，点标题先看全文',
  clause: '点“条款对照”去填企业情况，点标题先看全文',
};
// 去掉了那一条（hint）：列表放宽了，有的通知做不了，写明有按钮的才能做（Grok 10/22）；
// 不写"暂时不能"（名单类本来就不做，不是暂时），句子也要短（agy 10/22）
const WIDE_HINTS = {
  interpret: '有“AI 解读”按钮的才能解读，点它直接开始；点标题看全文',
  clause: '有“条款对照”按钮的才能做对照，点它去填企业情况；点标题看全文',
};
const ONLY = { clause: '只看能做条款对照的' };
const DEFAULT_ONLY = '只看能 AI 解读的';

Component({
  options: { styleIsolation: 'apply-shared' },
  properties: {
    variant: { type: String, value: 'bar' },
    pick: { type: String, value: '' },
  },
  data: { hint: '', only: DEFAULT_ONLY },
  observers: {
    'pick, variant': function (pick, variant) {
      const hints = variant === 'hint' ? WIDE_HINTS : HINTS;
      this.setData({
        hint: Object.prototype.hasOwnProperty.call(hints, pick) ? hints[pick] : '',
        only: Object.prototype.hasOwnProperty.call(ONLY, pick) ? ONLY[pick] : DEFAULT_ONLY,
      });
    },
  },
  methods: {
    remove() {
      this.triggerEvent('remove');
    },
  },
});
