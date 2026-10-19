const { reasonView } = require('./ai-reasons.js');

const SECTIONS = [
  ['对象', '申报对象'], ['支持', '支持方式'], ['条件', '申报条件'],
  ['时间', '申报时间'], ['方式', '申报方式'], ['材料', '材料清单'],
];
const CONSENT_KEY = 'youjuInterpretNoticeV1';
const CONTEXT_KEY = 'youjuInterpretTaskContextV1';
const TERMS = [
  '本服务为第三方工具，不是政府平台，不代办，不判断能否申报。',
  '内容由 AI 摘录通知原文原句，经程序逐字核对，不改写；仅供参考。',
  '截止日期、支持方式显示的是人工核对值；其余为 AI 摘录的原文原句。',
  '申报请以通知原文和主管部门的解释为准。',
];

function disclosureLine(value = {}) {
  return value && value.modelLabel && value.filingNo ? `模型：${value.modelLabel}｜备案号：${value.filingNo}` : '';
}

// 结果页（10/17 创始人试用后精简）：同一句原文只出现一次，标出它同时属于哪几类；
// 只剩重复句的类别写"和「X」是同一句原文"；没摘到的类别合成一行（missingText），不再各占一张卡
function resultView(result = {}) {
  const first = new Map();
  const sections = result.refusal ? [] : SECTIONS.map(([type, title]) => {
    const items = []; const sameAs = [];
    (result.items || []).filter((item) => item.type === type).forEach((item) => {
      const text = oneLine(item.text);
      const refs = (item.refs || []).map((ref) => ({ pid: ref.pid, quote: oneLine(ref.quote) }));
      const seen = first.get(text);
      if (seen) {
        // 重复的句子不再单列，但它带的引用并到第一次出现的那条里，引文一条不丢（Codex 复核 #277）
        refs.forEach((ref) => {
          if (!seen.item.refs.some((known) => known.pid === ref.pid && known.quote === ref.quote)) {
            seen.item.refs.push(Object.assign({ key: seen.item.refs.length }, ref));
          }
        });
        if (seen.title !== title) {
          if (!seen.item.alsoIn.includes(title)) seen.item.alsoIn.push(title);
          if (!sameAs.includes(seen.title)) sameAs.push(seen.title);
        }
        return;
      }
      const view = { key: items.length, text, alsoIn: [], refs: [] };
      refs.forEach((ref) => {
        if (!view.refs.some((known) => known.pid === ref.pid && known.quote === ref.quote)) view.refs.push(Object.assign({ key: view.refs.length }, ref));
      });
      first.set(text, { title, item: view }); items.push(view);
    });
    // 有实际条目（包括和别的类别同一句）就以条目为准；一条都没有才算没摘到（Codex 复核 #277）
    return { type, title, items, sameAs: sameAs.join('」「'), missing: !items.length && !sameAs.length };
  });
  sections.forEach((section) => section.items.forEach((item) => { item.alsoInText = item.alsoIn.join('、'); }));
  return {
    notice: result.notice || '', refusal: result.refusal || '', warning: result.warning || '',
    attachmentNotice: result.attachment_notice || '', disclosure: disclosureLine(result.disclosure),
    verified: (result.verified || []).map((item, index) => Object.assign({}, item, {
      key: index, displayValue: [item.kind, item.value, item.time].filter(Boolean).join(' '),
    })),
    sections: sections.filter((section) => !section.missing),
    missingText: sections.filter((section) => section.missing).map((section) => section.title).join('、'),
  };
}

// 后端返回的原文片段可能跨了 PDF 的版面换行（10/13 起接回整句），显示时去掉换行；
// 两边都是字母数字时留一个空格，免得英文单词粘在一起
function oneLine(value) {
  return String(value || '').replace(/([A-Za-z0-9])\s*\n\s*(?=[A-Za-z0-9])/g, '$1 ').replace(/\s*\n\s*/g, '');
}

function hasConsent(storage) {
  try { return storage.getStorageSync(CONSENT_KEY) === true; } catch (e) { return false; }
}
function saveConsent(storage, checked) {
  if (!checked) return false;
  try { storage.setStorageSync(CONSENT_KEY, true); } catch (e) { /* 本次仍可使用，下次重新确认 */ }
  return true;
}
function taskContext(storage, id, value) {
  try {
    const saved = storage.getStorageSync(CONTEXT_KEY) || {};
    if (value) {
      const entries = Object.entries(saved).filter(([key]) => key !== String(id)).slice(-99);
      storage.setStorageSync(CONTEXT_KEY, Object.assign(entries.reduce((contexts, [key, entry]) => {
        contexts[key] = entry; return contexts;
      }, {}), { [id]: value }));
    }
    return value || saved[id] || {};
  } catch (e) { return value || {}; }
}
// 任务失败按后台给的错误代码写（D-075），不再比对文字：排队后才撞上全站上限、admin 处理不了时退回的，
// 都已退回次数。被移出白名单（AI_OFF）也写"暂时不可用"，不写"未开放"
const FAILED_TEXT = {
  AI_DAILY_CAP: '今天 AI 的总用量满了，明天 0 点恢复；这次没有扣你的次数。',
  AI_UNAVAILABLE: 'AI 暂时不可用，请稍后再试；这次没有扣你的次数。',
  AI_OFF: 'AI 暂时不可用，请稍后再试；这次没有扣你的次数。',
};
const UNAVAILABLE_TEXT = 'AI 暂时不可用，请稍后再试。';
function failedText(task) {
  return FAILED_TEXT[task && task.errorCode] || (task && task.error) || '结果已失效，请重新提交';
}
// 提交被拒：这份不能解读（422 带原因代码）写原因；后台处理不了（429 AI_UNAVAILABLE）、接口说没开（404）写暂时不可用；
// 提交时撞上全站上限写总用量满了
function errorText(error) {
  if (error.code === 'AI_NOT_ELIGIBLE') return reasonView(error.reason, false).text;
  if (error.code === 'AI_UNAVAILABLE' || error.status === 404) return UNAVAILABLE_TEXT;
  if (error.code === 'AI_DAILY_CAP') return '今天 AI 的总用量满了，明天 0 点恢复；可以先看原文。';
  return error.message || '处理失败，请稍后重试';
}
// 这几种重试也没用：这份不能解读、暂时不可用、次数满了。别的 422（条款对照填的情况不合要求）照旧能回去改
function retryable(error) {
  return error.status !== 404 && error.status !== 429 && error.code !== 'AI_NOT_ELIGIBLE';
}
function idempotencyKey() {
  return `interpret-${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}

// 等结果：头 90 秒每 2 秒查一次；还没好就改成每 15 秒查一次，页面开着就接着等（最多 15 分钟），
// 离开页面再回来按任务号接着查。排队时显示前面还有几个、大约等多久（后台给了才显示）。
const FAST_WINDOW = 90000;
const FAST_INTERVAL = 2000;
const SLOW_INTERVAL = 15000;
const SLOW_WINDOW = 15 * 60000;
const SLOW_TEXT = '还在处理，可以先离开，之后在“我的 → 我的记录”里查看；停在这一页会自动显示结果。';
const GIVE_UP_TEXT = '还在处理，请稍后在“我的 → 我的记录”里查看结果。';

function progressText(task) {
  if (task && task.status === 'queued') {
    const ahead = Number(task.queuePosition);
    const eta = Number(task.etaSeconds);
    if (Number.isFinite(ahead) && ahead > 0) {
      const wait = Number.isFinite(eta) && eta > 0 ? `，预计约 ${Math.max(1, Math.round(eta / 60))} 分钟` : '';
      return `排队中：前面还有 ${ahead} 个${wait}`;
    }
    return '排队中，马上开始';
  }
  return '正在对照原文摘录…';
}

// 请求没连上（没有状态码、服务端出错）和任务本身失败要分开：前者重试时接着查同一个任务、或用同一个请求编号再提交
// （服务端按请求编号认出已建的任务，不会重复建、重复扣次数）；后者重试才重新提交。
function transient(error) {
  return !error || !error.status || error.status >= 500 || error.retryable === true;
}

// 页面生命周期、在途请求和倒计时共用一代编号，隐藏后旧回调不能再写页面或开始轮询。
function createSession({ api, storage, update, docId = '', title = '', taskId = '', now = Date.now,
  setTimer = setTimeout, clearTimer = clearTimeout, makeKey = idempotencyKey,
  kind = 'interpret', company = {}, viewResult = resultView, useTaskMetadata = false }) {
  let agreed = hasConsent(storage); let checked = false; let visible = false; let epoch = 0;
  let pollTimer; let deadlineTimer; let busy = false; let creation; let creationKey; let creationError = null;
  let deadline = 0; let giveUpAt = 0; let slow = false; let failure = '';
  const metadata = taskId ? taskContext(storage, taskId) : {};
  docId = docId || metadata.docId || ''; title = title || metadata.title || '';
  const emit = (value) => update(Object.assign({ docId, title, taskId }, value));
  const active = (version) => visible && epoch === version;
  function stop() {
    visible = false; epoch += 1; busy = false;
    clearTimer(pollTimer); clearTimer(deadlineTimer);
  }
  function later(version) {
    pollTimer = setTimer(() => poll(version), slow ? SLOW_INTERVAL : FAST_INTERVAL);
  }
  // 90 秒还没好：作废在途的查询（晚到的旧结果不再写页面），改成慢慢查
  function timeout(version) {
    if (!active(version)) return;
    clearTimer(pollTimer); clearTimer(deadlineTimer);
    slow = true; const next = ++epoch;
    emit({ phase: 'timeout', error: SLOW_TEXT });
    later(next);
  }
  function accept(task, version) {
    if (!active(version)) return;
    taskId = task.id;
    if (useTaskMetadata) {
      // 服务端隐藏通知时同步清掉本机编号，避免旧缓存继续打开原文。
      docId = task.docId || ''; title = task.docTitle || '';
    } else {
      if (task.docId) docId = task.docId;
      if (task.docTitle) title = task.docTitle;
    }
    if (docId) taskContext(storage, taskId, { docId, title });
    emit({ taskId, docId, title });
    if (task.status === 'done' && task.hasResult && !Object.prototype.hasOwnProperty.call(task, 'result')) {
      poll(version); return;
    }
    if (task.status === 'done' || task.status === 'failed' || task.status === 'cancelled') {
      clearTimer(pollTimer); clearTimer(deadlineTimer); busy = false;
      if (task.status === 'done' && task.result) {
        // D-073：服务端标了 outdated（解读之后核对或附件有更新、正文没变）时，结果页加一句说明
        failure = ''; emit({ phase: 'done', result: viewResult(task.result), outdated: task.outdated === true, error: '', progress: '' });
      } else {
        failure = 'task'; emit({ phase: 'failed', outdated: false, error: failedText(task), canRetry: !!docId, resumable: false, progress: '', errorCode: task.errorCode || '' });
      }
      return;
    }
    if (!slow && now() >= deadline) { timeout(version); return; }
    if (slow && now() >= giveUpAt) {
      clearTimer(pollTimer); busy = false;
      emit({ phase: 'timeout', error: GIVE_UP_TEXT, progress: '' }); return;
    }
    emit(slow ? { phase: 'timeout', error: SLOW_TEXT, progress: progressText(task) }
      : { phase: 'processing', error: '', progress: progressText(task) });
    later(version);
  }
  function fail(error, version) {
    if (!active(version)) return;
    if (slow && taskId && transient(error)) { later(version); return; } // 慢慢等的时候偶尔没连上，下次再查
    clearTimer(pollTimer); clearTimer(deadlineTimer); busy = false;
    failure = transient(error) ? 'request' : 'task';
    emit({ phase: 'failed', outdated: false, error: errorText(error), progress: '', resumable: failure === 'request',
      canRetry: retryable(error) && !!docId, errorCode: error.code || '',
      // 后台说处理不了、或接口说没开：让页面重新问一次 AI 开没开（入口随即收起或变灰）
      unavailable: error.code === 'AI_UNAVAILABLE' || error.status === 404 });
  }
  function poll(version) {
    if (!active(version)) return;
    if (!slow && now() >= deadline) { timeout(version); return; }
    if (!taskId) {
      // 创建请求还没回来（接口服务冷启动）：等它；它失败了就报出来，重试时用同一个请求编号
      if (creationError) { slow = false; fail(creationError, version); return; }
      later(version); return;
    }
    Promise.resolve().then(() => api.advisorTask(taskId)).then((task) => accept(task, version)).catch((err) => fail(err, version));
  }
  function run() {
    if (!visible || !agreed || busy) return;
    if (!taskId && !docId) { emit({ phase: 'failed', error: '缺少通知编号，请从通知详情页进入', canRetry: false }); return; }
    busy = true; slow = false; failure = ''; const version = ++epoch;
    deadline = now() + FAST_WINDOW; giveUpAt = now() + SLOW_WINDOW;
    emit({ phase: 'processing', outdated: false, error: '', canRetry: false, resumable: false, progress: '' });
    deadlineTimer = setTimer(() => timeout(version), FAST_WINDOW);
    if (taskId) { poll(version); return; }
    // 隐藏期间创建请求仍可能完成，保留任务编号；重新显示时复用同一在途请求。
    if (!creation) {
      creationKey = creationKey || makeKey(); creationError = null;
      const body = { kind, doc_id: Number(docId) };
      if (kind === 'clause') body.company = company;
      creation = Promise.resolve().then(() => api.createAdvisorTask(body, creationKey)).then((task) => {
        taskId = task.id; taskContext(storage, taskId, { docId, title }); return task;
      });
      creation.catch((err) => { creationError = err; });
    }
    creation.then((task) => accept(task, version)).catch((err) => fail(err, version));
  }
  return {
    show() {
      visible = true; emit({ terms: TERMS, checked, needsConsent: !agreed });
      if (!agreed) { emit({ phase: 'consent' }); return; }
      run();
    },
    hide: stop,
    check(value) { checked = value === true; emit({ checked }); },
    // 使用须知只有一个"我知道了，开始"按钮（10/17）：等于确认后开始
    start() { checked = true; emit({ checked }); this.agree(); },
    agree() {
      if (!visible || !saveConsent(storage, checked)) return;
      agreed = true; emit({ needsConsent: false }); run();
    },
    // 上次是没连上（不是任务本身失败）：重试时接着用原来的任务或请求编号
    resumable() { return failure === 'request'; },
    retry() {
      if (!visible || !agreed || busy || !docId) return;
      clearTimer(pollTimer); clearTimer(deadlineTimer); epoch += 1;
      if (failure === 'request') {
        if (!taskId) { creation = null; creationError = null; }
        run(); return;
      }
      taskId = ''; creation = null; creationError = null; creationKey = null; run();
    },
  };
}

module.exports = { SECTIONS, TERMS, CONSENT_KEY, CONTEXT_KEY, FAILED_TEXT, UNAVAILABLE_TEXT, disclosureLine, oneLine, resultView, hasConsent, saveConsent, taskContext, progressText, transient, failedText, errorText, createSession };
