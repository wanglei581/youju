const { operator } = require('../../content/legal.js');
const license = require('../../content/business-license.js');

// 照片按卡片宽度显示时，执照上的小字（法定代表人、经营范围、登记机关）看不清，页面又不能放大：点图片全屏放大（独立审阅 #437）。
// wx.previewImage 官方写的是图片链接：先把包里的照片（代码包文件，从根目录写路径可读）写到用户目录再预览；
// 读写不了时直接用包内路径试。照片能不能进包靠 project.config.json 的 packOptions.include（上传时过滤无依赖文件）
const PREVIEW_NAME = 'business-license.jpg';
function preview(photo) {
  if (!photo) return;
  // 打不开时提示一句，免得点了没反应（独立审阅 #437 第 2 轮建议）
  const show = (url) => wx.previewImage({ urls: [url], current: url, fail: () => wx.showToast({ title: '图片打不开，请稍后再试', icon: 'none' }) });
  let fs = null;
  try { fs = wx.getFileSystemManager(); } catch (e) { fs = null; }
  const dir = wx.env && wx.env.USER_DATA_PATH;
  if (!fs || !dir) { show(photo); return; }
  const dest = `${dir}/${PREVIEW_NAME}`;
  fs.readFile({
    filePath: photo,
    success: ({ data }) => fs.writeFile({ filePath: dest, data, success: () => show(dest), fail: () => show(photo) }),
    fail: () => show(photo),
  });
}

Page({
  data: { operator, license },
  preview() { preview(this.data.license.photo); },
});
