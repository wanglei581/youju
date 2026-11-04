const { operator } = require('../../content/legal.js');
const license = require('../../content/business-license.js');

// 照片按卡片宽度显示时，执照上的小字（法定代表人、经营范围、登记机关）看不清，页面又不能放大：点图片全屏放大（独立审阅 #437）。
// wx.previewImage 官方写的是图片链接：先把包里的照片（代码包文件，从根目录写路径可读）写到用户目录再预览；
// 读写不了时直接用包内路径试。照片能不能进包靠 project.config.json 的 packOptions.include（上传时过滤无依赖文件）
const PREVIEW_NAME = 'business-license.jpg';
function preview(photo) {
  if (!photo) return;
  const show = (url) => wx.previewImage({ urls: [url], current: url });
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
