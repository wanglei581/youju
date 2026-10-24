// 附件主入口进入原生阅读页；二进制下载仅保留给显式保存操作。
// 阅读状态由实时阅读接口决定，不用旧副本可下载标记推断是否可读。
const api = require('./api.js');


const DIR = 'attachments';
const KEEP = 3;
// 不写"以原网站为准"：像是要用户出去看（10/23 审核退回"复制链接去浏览器"）
const NOTE_COPIES = '附件在本小程序阅读，以发文机关公布的文本为准';
const NOTE_ORIGINAL = '阅读页会显示附件内容及处理状态';
// 旧副本下载限制的原因（不作为原生阅读结论）。
const REASONS = {
  format: '该格式不能在小程序内打开',
  host: '附件不在政府网站，不提供副本',
  kind: '名单等文件的附件尚不可读',
  pending: '附件暂未缓存副本，内容尚不可读',
  old: '较早文件的附件尚不可读',
  too_large: '附件超过 20MB，内容尚不可读',
  personal: '附件含个人信息，内容尚不可读',
  scan: '扫描件附件尚不可读',
  image: '附件含图片，内容尚不可读',
  failed: '附件副本没能从原网站取到',
};

function reasonText(reason) {
  return REASONS[reason] || NOTE_ORIGINAL;
}

function sizeText(bytes) {
  if (!(bytes > 0)) return '';
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// 详情页只显示副本大小，完整阅读状态进入原生页实时核对。
function decorate(list) {
  return (list || []).map((att) => Object.assign({}, att, {
    canOpen: !!att.preview,
    meta: att.preview ? sizeText(att.size) : '查看内容及处理状态',
    action: '站内阅读',
  }));
}

function note(list) {
  return (list || []).some((att) => att.preview) ? NOTE_COPIES : NOTE_ORIGINAL;
}

// 本机文件名用附件名称：转发给朋友时对方看到的就是这个名字
function fileName(att) {
  const base = String(att.name || '')
    .replace(/[\\/:*?"<>|\u0000-\u001f]/g, ' ')
    .replace(/\.(docx?|xlsx?|pptx?|pdf|wps|et)$/i, '')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 60) || '附件';
  return `${base}.${att.fileType}`;
}

function root() {
  return `${wx.env.USER_DATA_PATH}/${DIR}`;
}

function dirOf(att) {
  return `${root()}/${att.id}-${att.version}`;
}

function sizeOf(fs, path) {
  try {
    return fs.statSync(path).size;
  } catch (e) {
    return -1;
  }
}

function remove(fs, dir) {
  try { fs.rmdirSync(dir, true); } catch (e) { /* 本来就没有 */ }
}

// 只留最近打开的 KEEP 份（刚打开的这份一定留下）
function prune(fs, keep) {
  try {
    const dirs = fs.readdirSync(root())
      .map((name) => {
        const path = `${root()}/${name}`;
        let at = 0;
        try { at = fs.statSync(path).lastModifiedTime || 0; } catch (e) { /* 读不到按最旧 */ }
        return { path, at };
      })
      .filter((d) => d.path !== keep)
      .sort((a, b) => b.at - a.at);
    dirs.slice(KEEP - 1).forEach((d) => remove(fs, d.path));
  } catch (e) {
    /* 清理失败不影响打开 */
  }
}

function valid(att) {
  return att && att.id && /^[0-9a-f]{16}$/.test(att.version || '') && att.size > 0
    && att.partSize > 0 && att.parts === Math.ceil(att.size / att.partSize) && att.fileType;
}

// 分段下载到本机，返回文件路径；已下载过同一版本的直接用。每段大小都核对，不完整就删掉、报错
function download(att, progress) {
  if (!valid(att)) return Promise.reject(new Error('附件信息不完整'));
  const fs = wx.getFileSystemManager();
  const dir = dirOf(att);
  const filePath = `${dir}/${fileName(att)}`;
  if (sizeOf(fs, filePath) === att.size) return Promise.resolve(filePath);
  remove(fs, dir);
  try {
    fs.mkdirSync(dir, true);
  } catch (e) {
    return Promise.reject(e);
  }
  let chain = Promise.resolve();
  for (let i = 0; i < att.parts; i += 1) {
    chain = chain
      .then(() => {
        if (att.parts > 1) progress(i + 1, att.parts);
        return api.attachmentPart(att.id, att.version, i);
      })
      .then((buffer) => {
        const expected = i < att.parts - 1 ? att.partSize : att.size - att.partSize * (att.parts - 1);
        if (!buffer || buffer.byteLength !== expected) throw new Error('附件下载不完整');
        if (i === 0) fs.writeFileSync(filePath, buffer);
        else fs.appendFileSync(filePath, buffer);
      });
  }
  return chain
    .then(() => {
      if (sizeOf(fs, filePath) !== att.size) throw new Error('附件下载不完整');
      prune(fs, dir);
      return filePath;
    })
    .catch((err) => {
      remove(fs, dir);
      throw err;
    });
}

// 正文阅读与文件下载分离：主入口始终在小程序内，由阅读接口说明内容状态。
function open(att, docId) {
  if (!att || !att.id || !/^\d+$/.test(String(docId || ''))) {
    wx.showToast({ title: '附件信息不完整，请刷新详情', icon: 'none' });
    return Promise.resolve(false);
  }
  return new Promise((resolve) => wx.navigateTo({
    url: `/pages/reader/index?id=${encodeURIComponent(docId)}&attachment=${encodeURIComponent(att.id)}`,
    success: () => resolve(true),
    fail: () => { wx.showToast({ title: '阅读页未打开，请重试', icon: 'none' }); resolve(false); },
  }));
}

module.exports = { decorate, note, open, download, fileName, sizeText, reasonText, REASONS, NOTE_COPIES, NOTE_ORIGINAL };
