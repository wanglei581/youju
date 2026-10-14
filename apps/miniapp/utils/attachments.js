// 附件：服务端存有副本的（doc、docx、xls、xlsx、ppt、pptx、pdf）在小程序里打开，打开后右上角菜单可转发、保存；
// 其他的（wps、压缩包、还没存好的、下载失败的等）复制原文链接并说明原因。
// 副本经云托管分段取（每段不超过返回上限 1000 KiB），写成本机文件后用 wx.openDocument 打开。
// 本机只留最近打开的几份（本地用户文件和缓存共计上限 200MB）。
const api = require('./api.js');
const { copyLink } = require('./copy.js');

const DIR = 'attachments';
const KEEP = 3;
const NOTE_COPIES = '附件副本来自原网站，以原网站为准';
const NOTE_ORIGINAL = '附件在原文页面下载';
// 不能在小程序里打开的原因（服务端给代码）；提示时后面接"，已复制原文链接"
const REASONS = {
  format: '该格式不能在小程序内打开',
  host: '附件不在政府网站，不提供副本',
  kind: '名单等文件的附件请看原文',
  pending: '附件副本还在准备',
  old: '较早文件的附件请看原文',
  too_large: '附件超过 20MB，请看原文',
  personal: '附件含个人信息，请看原文',
  scan: '扫描件附件请看原文',
  image: '附件含图片，请看原文',
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

// 详情页附件列表：能打开的显示大小，不能打开的显示原因
function decorate(list) {
  return (list || []).map((att) => Object.assign({}, att, {
    canOpen: !!att.preview,
    meta: att.preview ? sizeText(att.size) : reasonText(att.reason),
    action: att.preview ? '打开' : '复制链接',
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
      return filePath;
    })
    .catch((err) => {
      remove(fs, dir);
      throw err;
    });
}

function openFile(filePath, fileType) {
  return new Promise((resolve, reject) => {
    wx.openDocument({
      filePath,
      fileType,
      // 右上角菜单：用户自己选择转发、保存或用其他应用打开
      showMenu: true,
      success: () => resolve(),
      fail: () => reject(new Error('打开附件失败')),
    });
  });
}

// 点附件：能打开的下载后打开；不能打开或打开失败的，复制原文链接并说明原因。返回是否打开了
function open(att, docUrl) {
  if (!att || !att.preview) {
    copyLink(docUrl, `${reasonText(att && att.reason)}，已复制原文链接`);
    return Promise.resolve(false);
  }
  wx.showLoading({ title: '正在打开附件…', mask: true });
  const fs = wx.getFileSystemManager();
  return download(att, (n, total) => wx.showLoading({ title: `正在下载 ${n}/${total}`, mask: true }))
    .then((filePath) => {
      wx.hideLoading();
      return openFile(filePath, att.fileType);
    })
    .then(() => {
      prune(fs, dirOf(att));
      return true;
    })
    .catch(() => {
      wx.hideLoading();
      copyLink(docUrl, '附件没能打开，已复制原文链接');
      return false;
    });
}

module.exports = { decorate, note, open, download, fileName, sizeText, reasonText, REASONS, NOTE_COPIES, NOTE_ORIGINAL };
