// 本地图标：替代 TDesign 的 t-icon（它从 TDesign 的网站在线加载字体，外部网址不可用时图标会消失）。
// 字体以 base64 内嵌在 index.wxss 里，不从网络加载。
// 用法和 t-icon 相同：<t-icon name="search" size="36rpx" color="#86909C" />。
// size 是纯数字时按 px，和 TDesign 一样；不写 color 时继承外层文字颜色。
// 字体只含 tools/icons/icons.txt 里列出的图标，样式由 tools/icons/build_icons.py 生成。

function addUnit(value) {
  if (value === null || value === undefined || value === '') return '';
  const text = String(value);
  return /^-?\d+(\.\d+)?$/.test(text) ? `${text}px` : text;
}

function iconStyle(size, color) {
  const parts = [];
  if (color) parts.push(`color: ${color}`);
  const unit = addUnit(size);
  if (unit) parts.push(`font-size: ${unit}`);
  return parts.join('; ');
}

Component({
  // 和 TDesign 的 t-icon 一样不渲染外层节点：图标直接参与外层的排版（多在 flex 里），换组件后位置不变
  options: { virtualHost: true },
  properties: {
    name: { type: String, value: '' },
    size: { type: null, value: '' },
    color: { type: String, value: '' },
  },
  data: { iconStyle: '' },
  observers: {
    'size, color': function (size, color) {
      this.setData({ iconStyle: iconStyle(size, color) });
    },
  },
});
