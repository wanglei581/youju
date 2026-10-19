// 搜索页"只看能 AI 解读的"（D-075）：从 AI 首页"近 7 天能解读的申报通知 · 全部"进来时带上，只管这一次查询，
// 不算筛选条件、不进关注和转发。bar 是结果上面那一条，exit 是没有结果时"去掉"的按钮；点了发 remove
Component({
  options: { styleIsolation: 'apply-shared' },
  properties: {
    variant: { type: String, value: 'bar' },
  },
  methods: {
    remove() {
      this.triggerEvent('remove');
    },
  },
});
