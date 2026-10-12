// 转发版本（D-055）：同一份部里通知，各地转发或据此发的通知归成一组，管理台确认后才合并显示。
// 列表里只列一条，下面写“另有 N 个转发版本（天津、福建）”，点开看各版本；
// 详情里按版本列出各自核对过的截止（库里有部里原件时它叫“部委”，其余叫“江西版”），
// 不同版本的日期不互相套用，没核对的只写“截止未核对”、不写日期。
const { monthDay, NOTE_SEE_ORIGINAL } = require('./format.js');

// 列表里最多写几个地区，多了写“等”
const MAX_REGIONS = 3;

function groupLine(group) {
  if (!group || !group.versions) return '';
  const regions = group.regions || [];
  const shown = regions.slice(0, MAX_REGIONS).join('、');
  const more = regions.length > MAX_REGIONS ? '等' : '';
  return `另有 ${group.versions} 个转发版本${shown ? `（${shown}${more}）` : ''}`;
}

// 一个版本自己的截止：核对过的写“推荐或审核截止 10月8日 16:00”，几条用“；”连起来
function versionDeadline(version) {
  const items = (version && version.deadlines) || [];
  if (items.length) {
    return items.map((d) => `${d.label} ${monthDay(d.date)}${d.time ? ` ${d.time}` : ''}`).join('；');
  }
  return version && version.deadline_state === 'unverified' ? '截止未核对' : NOTE_SEE_ORIGINAL;
}

// 详情页“各版本的截止”：每行写版本、机关和发布日期、这一版的截止；当前这一版标出来
function versionRows(repost) {
  return ((repost && repost.versions) || []).map((v) => ({
    id: v.id,
    label: v.label,
    current: !!v.current,
    meta: [v.issuer, v.date ? `${v.date} 发布` : ''].filter(Boolean).join(' · '),
    deadline: versionDeadline(v),
    verified: !!(v.deadlines && v.deadlines.length),
    url: v.url,
  }));
}

// 正文里引用的部里通知，只照录标题和文号
function upstreamLine(repost) {
  const up = repost && repost.upstream;
  if (!up || !up.number) return '';
  return `这些通知都转发或依据同一份部委通知：${up.title ? `《${up.title}》` : ''}（${up.number}）`;
}

module.exports = { groupLine, versionDeadline, versionRows, upstreamLine, MAX_REGIONS };
