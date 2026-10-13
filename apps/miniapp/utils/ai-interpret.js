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

function resultView(result = {}) {
  const missing = result.missing || [];
  return {
    notice: result.notice || '', refusal: result.refusal || '', warning: result.warning || '',
    attachmentNotice: result.attachment_notice || '', disclosure: disclosureLine(result.disclosure),
    verified: (result.verified || []).map((item, index) => Object.assign({}, item, {
      key: index, displayValue: [item.value, item.time].filter(Boolean).join(' '),
    })),
    sections: result.refusal ? [] : SECTIONS.map(([type, title]) => ({
      type, title, missing: missing.includes(type),
      items: (result.items || []).filter((item) => item.type === type).map((item, index) => ({
        key: index, text: oneLine(item.text),
        refs: (item.refs || []).map((ref, refIndex) => ({ key: refIndex, pid: ref.pid, quote: oneLine(ref.quote) })),
      })),
    })),
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
function errorText(error) {
  return error.status === 404 ? '暂时没有可用工具' : error.message || '处理失败，请稍后重试';
}
function idempotencyKey() {
  return `interpret-${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}

// 页面生命周期、在途请求和倒计时共用一代编号，隐藏后旧回调不能再写页面或开始轮询。
function createSession({ api, storage, update, docId = '', title = '', taskId = '', now = Date.now,
  setTimer = setTimeout, clearTimer = clearTimeout, makeKey = idempotencyKey,
  kind = 'interpret', company = {}, viewResult = resultView, useTaskMetadata = false }) {
  let agreed = hasConsent(storage); let checked = false; let visible = false; let epoch = 0;
  let pollTimer; let deadlineTimer; let busy = false; let creation; let timedOut = false;
  let creationKey; let deadline = 0;
  const metadata = taskId ? taskContext(storage, taskId) : {};
  docId = docId || metadata.docId || ''; title = title || metadata.title || '';
  const emit = (value) => update(Object.assign({ docId, title, taskId }, value));
  const active = (version) => visible && epoch === version;
  function stop() {
    visible = false; epoch += 1; busy = false;
    clearTimer(pollTimer); clearTimer(deadlineTimer);
  }
  function timeout(version) {
    if (!active(version)) return;
    clearTimer(pollTimer); clearTimer(deadlineTimer); epoch += 1; busy = false; timedOut = true;
    emit({ phase: 'timeout', error: '还在处理，可稍后在任务里查看' });
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
      clearTimer(deadlineTimer); busy = false;
      if (task.status === 'done' && task.result) emit({ phase: 'done', result: viewResult(task.result), error: '' });
      else emit({ phase: 'failed', error: task.error || '结果已失效，请重新提交', canRetry: !!docId });
      return;
    }
    if (now() >= deadline) { timeout(version); return; }
    emit({ phase: 'processing', error: '' });
    pollTimer = setTimer(() => poll(version), 2000);
  }
  function fail(error, version) {
    if (!active(version)) return;
    clearTimer(deadlineTimer); busy = false;
    emit({ phase: 'failed', error: errorText(error), canRetry: error.status !== 404 && error.status !== 429 && !!docId });
  }
  function poll(version) {
    if (!active(version)) return;
    if (now() >= deadline) { timeout(version); return; }
    Promise.resolve().then(() => api.advisorTask(taskId)).then((task) => accept(task, version)).catch((err) => fail(err, version));
  }
  function run() {
    if (!visible || !agreed || busy || timedOut) return;
    if (!taskId && !docId) { emit({ phase: 'failed', error: '缺少通知编号，请从通知详情页进入', canRetry: false }); return; }
    busy = true; const version = ++epoch; deadline = now() + 90000;
    emit({ phase: 'processing', error: '', canRetry: false });
    deadlineTimer = setTimer(() => timeout(version), 90000);
    if (taskId) { poll(version); return; }
    // 隐藏期间创建请求仍可能完成，保留任务编号；重新显示时复用同一在途请求。
    if (!creation) {
      creationKey = creationKey || makeKey();
      const body = { kind, doc_id: Number(docId) };
      if (kind === 'clause') body.company = company;
      creation = Promise.resolve().then(() => api.createAdvisorTask(body, creationKey)).then((task) => {
        taskId = task.id; taskContext(storage, taskId, { docId, title }); return task;
      });
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
    agree() {
      if (!visible || !saveConsent(storage, checked)) return;
      agreed = true; emit({ needsConsent: false }); run();
    },
    retry() {
      if (!visible || !agreed || busy || !docId) return;
      clearTimer(pollTimer); clearTimer(deadlineTimer); epoch += 1;
      taskId = ''; creation = null; creationKey = null; timedOut = false; run();
    },
  };
}

module.exports = { SECTIONS, TERMS, CONSENT_KEY, CONTEXT_KEY, disclosureLine, oneLine, resultView, hasConsent, saveConsent, taskContext, createSession };
