/* Public search, source directory and XLSX/DOCX export for the local library pilot.
 * All server data is written to the DOM as text nodes; links are limited to https URLs. */
(() => {
  'use strict';

  const KIND_LABELS = {
    policy: '政策', application: '申报通知', list: '企业名单',
    statistics: '统计数据', report: '研究报告', other: '其他',
  };
  const RIGHTS_LABELS = {
    directory_only: '仅题录与原站链接', short_excerpt: '题录与短摘要', evidence_use: '可用证据与答案',
  };
  const STATUS_LABELS = { verified: '已核验答案', metadata: '仅题录', pending: '待审' };
  const FORMAT_LABELS = { xlsx: 'Excel', docx: 'Word' };
  const FILTER_KEYS = ['q', 'kind', 'industry', 'region'];
  const RESULT_LIMIT = 50; // server-side LIMIT in store.public_search
  const MAX_SELECTED = 100; // server-side limit in store.public_selected

  const $ = (id) => document.getElementById(id);
  const form = $('search-form');
  const fields = { q: $('query'), kind: $('kind'), industry: $('industry'), region: $('region') };
  const submitButton = form.querySelector('button[type="submit"]');
  const searchStatus = $('search-status');
  const resultCount = $('result-count');
  const results = $('results');
  const selectedBar = $('selected-bar');
  const selectedCount = $('selected-count');
  const selectedNote = selectedBar.querySelector('small');
  const exportButtons = { xlsx: $('export-xlsx'), docx: $('export-docx') };
  const sourceList = $('source-list');

  const selectedNoteDefault = selectedNote.textContent;
  const selected = new Map(); // record id -> title, kept across searches
  let currentItems = [];
  let pageToggle = null;
  let searchController = null;
  let searchSeq = 0;
  let exporting = false;

  selectedNote.setAttribute('aria-live', 'polite');
  const clearButton = el('button', { type: 'button', className: 'button', text: '清空' });
  exportButtons.docx.parentElement.append(clearButton);

  // ---------- DOM helpers (no innerHTML anywhere) ----------

  function el(tag, props = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === 'className') node.className = value;
      else if (key === 'text') node.textContent = String(value);
      else if (key.startsWith('on')) {
        if (typeof value === 'function') node.addEventListener(key.slice(2), value);
      } else node.setAttribute(key, value === true ? '' : String(value));
    }
    for (const child of [].concat(children)) {
      if (child === undefined || child === null || child === false || child === '') continue;
      node.append(child instanceof Node ? child : String(child));
    }
    return node;
  }

  function safeHttpsUrl(value) {
    try {
      const url = new URL(String(value || ''));
      return url.protocol === 'https:' && !url.username && !url.password ? url.href : '';
    } catch (_) {
      return '';
    }
  }

  function extLink(url, label) {
    const href = safeHttpsUrl(url);
    if (!href) return String(label || url || '（地址无效）');
    return el('a', { href, target: '_blank', rel: 'noopener noreferrer', text: label || href });
  }

  function tag(text, variant) {
    return el('span', { className: variant ? 'tag ' + variant : 'tag', text });
  }

  function emptyState(message, action, fullRow) {
    const node = el('div', { className: 'empty' }, [el('div', { text: message })]);
    if (fullRow) node.style.gridColumn = '1 / -1';
    if (action) {
      node.append(el('button', {
        type: 'button', className: 'button secondary', text: action.label, onclick: action.run,
      }));
      node.lastChild.style.marginTop = '12px';
    }
    return node;
  }

  function setStatus(node, message, kind) {
    node.textContent = message || '';
    node.classList.toggle('error', kind === 'error');
  }

  function errorText(error) {
    if (error && error.name === 'TypeError') return '无法连接本地服务，请确认服务仍在运行。';
    return (error && error.message) || '未知错误';
  }

  async function readError(response) {
    let message = '';
    try {
      const data = await response.json();
      if (data && typeof data.error === 'string') message = data.error;
    } catch (_) { /* non-JSON error body */ }
    return message || `请求失败（HTTP ${response.status}）`;
  }

  async function getJson(url, signal) {
    const response = await fetch(url, { headers: { Accept: 'application/json' }, cache: 'no-store', signal });
    if (!response.ok) throw new Error(await readError(response));
    return response.json();
  }

  function filenameFrom(response, fallback) {
    const header = response.headers.get('Content-Disposition') || '';
    const match = /filename="?([^";]+)"?/i.exec(header);
    const name = (match ? match[1] : fallback).replace(/[^\w.\-]+/g, '_');
    return name || fallback;
  }

  function saveBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const link = el('a', { href: url, download: filename });
    link.hidden = true;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
  }

  // ---------- Search ----------

  function currentFilters() {
    const filters = {};
    for (const key of FILTER_KEYS) {
      const value = fields[key].value.trim();
      if (value) filters[key] = value;
    }
    return filters;
  }

  function hasFilters() {
    return Object.keys(currentFilters()).length > 0;
  }

  function restoreFiltersFromUrl() {
    const params = new URLSearchParams(window.location.search);
    for (const key of FILTER_KEYS) {
      const value = (params.get(key) || '').trim().slice(0, 100);
      if (!value) continue;
      if (key === 'kind' && !Object.prototype.hasOwnProperty.call(KIND_LABELS, value)) continue;
      fields[key].value = value;
    }
  }

  function syncUrl(filters) {
    const params = new URLSearchParams(filters).toString();
    const next = window.location.pathname + (params ? '?' + params : '') + window.location.hash;
    window.history.replaceState(null, '', next);
  }

  async function search() {
    const filters = currentFilters();
    const seq = ++searchSeq;
    if (searchController) searchController.abort();
    searchController = new AbortController();

    syncUrl(filters);
    submitButton.disabled = true;
    results.setAttribute('aria-busy', 'true');
    resultCount.textContent = '查询中…';
    setStatus(searchStatus, '正在查询已发布资料…');
    results.replaceChildren(emptyState('正在查询已发布资料…'));

    try {
      const data = await getJson('/api/public/resources?' + new URLSearchParams(filters), searchController.signal);
      if (seq !== searchSeq) return;
      const items = Array.isArray(data.items) ? data.items : [];
      renderResults(items);
      if (!items.length) {
        resultCount.textContent = '0 条结果';
        setStatus(searchStatus, hasFilters() ? '没有符合条件的已发布资料。' : '资料库暂无已发布资料。');
      } else {
        resultCount.textContent = `${items.length} 条已发布资料`;
        const capped = items.length >= RESULT_LIMIT ? `仅显示最新 ${RESULT_LIMIT} 条，可补充关键词、行业或地区缩小范围。` : '';
        setStatus(searchStatus, `找到 ${items.length} 条。勾选后可导出 Excel 或 Word。${capped}`);
      }
    } catch (error) {
      if (seq !== searchSeq || (error && error.name === 'AbortError')) return;
      currentItems = [];
      pageToggle = null;
      resultCount.textContent = '查询失败';
      setStatus(searchStatus, '查询失败：' + errorText(error), 'error');
      results.replaceChildren(emptyState('资料暂时无法加载：' + errorText(error), { label: '重新查询', run: search }));
    } finally {
      if (seq === searchSeq) {
        submitButton.disabled = false;
        results.removeAttribute('aria-busy');
      }
    }
  }

  function renderResults(items) {
    currentItems = items;
    pageToggle = null;
    if (!items.length) {
      results.replaceChildren(emptyState(hasFilters()
        ? '没有找到符合条件的已发布资料。可以减少关键词，或更换类型、行业、地区后再试。'
        : '资料库暂时没有已发布资料。管理员导入并审核发布后会显示在这里。'));
      return;
    }
    pageToggle = el('button', { type: 'button', className: 'button secondary', onclick: togglePage });
    results.replaceChildren(el('div', { className: 'row' }, [pageToggle]), ...items.map(renderItem));
    updateSelection();
  }

  function renderItem(item) {
    const id = Number(item.id);
    const title = String(item.title || '（无标题）');
    const verified = item.review_status === 'verified' && item.answer;

    const checkbox = el('input', { type: 'checkbox', className: 'result-check', 'aria-label': '选择：' + title });
    checkbox.checked = selected.has(id);
    checkbox.dataset.id = String(id);
    checkbox.addEventListener('change', () => {
      if (checkbox.checked && !select(id, title)) checkbox.checked = false;
      if (!checkbox.checked) selected.delete(id);
      updateSelection();
    });

    const meta = [
      tag(KIND_LABELS[item.kind] || item.kind || '未分类'),
      item.region ? tag(item.region, 'muted') : null,
      item.industry ? tag(item.industry, 'muted') : null,
      item.published_at ? tag('发布 ' + item.published_at, 'muted') : tag('发布日期待核', 'warn'),
      tag(STATUS_LABELS[item.review_status] || String(item.review_status || '状态未知'), verified ? '' : 'muted'),
    ];

    const body = [];
    if (verified) {
      body.push(el('p', { className: 'result-answer' }, [el('strong', { text: '答案：' }), String(item.answer)]));
    } else {
      body.push(el('p', {
        className: 'result-answer unverified',
        text: '仅有题录：本库未核验正文答案，请打开原站阅读原文，不以此代替原文件。',
      }));
    }
    if (item.summary) {
      body.push(el('p', { className: 'result-answer unverified' }, [el('strong', { text: '摘要：' }), String(item.summary)]));
    }

    const reviewed = item.reviewed_at ? ' · 审核于 ' + String(item.reviewed_at).slice(0, 10) : '';
    const details = el('div', { className: 'result-details' }, [
      el('div', {}, [
        '发布机构：', String(item.publisher || '未标注'),
        ' · 来源：', String(item.source_name || '未标注'),
        item.rights_level ? `（${RIGHTS_LABELS[item.rights_level] || item.rights_level}）` : '',
      ]),
      el('div', {}, ['原站：', extLink(item.original_url)]),
      item.evidence_locator ? el('div', {}, ['证据位置：', String(item.evidence_locator)]) : null,
      el('div', { text: `资料版本 v${item.version_no || '?'}${reviewed} · 编号 #${id}` }),
    ]);

    return el('article', { className: 'result' }, [
      el('div', { className: 'result-top' }, [checkbox, el('h3', { className: 'result-title', text: title })]),
      el('div', { className: 'result-meta' }, meta),
      ...body,
      details,
    ]);
  }

  // ---------- Selection ----------

  function select(id, title) {
    if (!Number.isInteger(id) || id < 1) return false;
    if (!selected.has(id) && selected.size >= MAX_SELECTED) {
      setStatus(searchStatus, `一次最多导出 ${MAX_SELECTED} 条，请先导出或取消部分已选资料。`, 'error');
      return false;
    }
    selected.set(id, title);
    return true;
  }

  function togglePage() {
    const allSelected = currentItems.length > 0 && currentItems.every((item) => selected.has(Number(item.id)));
    if (allSelected) {
      for (const item of currentItems) selected.delete(Number(item.id));
    } else {
      for (const item of currentItems) {
        if (!select(Number(item.id), String(item.title || ''))) break;
      }
    }
    updateSelection();
  }

  function updateSelection() {
    for (const box of results.querySelectorAll('input.result-check')) {
      box.checked = selected.has(Number(box.dataset.id));
    }
    const onPage = currentItems.filter((item) => selected.has(Number(item.id))).length;
    const offPage = selected.size - onPage;
    selectedCount.textContent = `已选 ${selected.size} 条` + (offPage > 0 ? `（含其他查询 ${offPage} 条）` : '');
    selectedBar.hidden = selected.size === 0;
    if (pageToggle) {
      const all = currentItems.length > 0 && onPage === currentItems.length;
      pageToggle.textContent = all ? `取消本页 ${currentItems.length} 条` : `选择本页 ${currentItems.length} 条`;
    }
    if (!exporting) {
      for (const button of Object.values(exportButtons)) button.disabled = selected.size === 0;
      clearButton.disabled = selected.size === 0;
    }
    if (!selected.size) selectedNote.textContent = selectedNoteDefault;
  }

  // ---------- Export ----------

  function setExporting(on, activeFormat) {
    exporting = on;
    for (const [format, button] of Object.entries(exportButtons)) {
      button.disabled = on || selected.size === 0;
      button.textContent = on && format === activeFormat ? '生成中…' : `导出 ${FORMAT_LABELS[format]}`;
    }
    clearButton.disabled = on || selected.size === 0;
  }

  async function exportSelected(format) {
    if (exporting || !selected.size) return;
    const ids = [...selected.keys()];
    const label = FORMAT_LABELS[format];
    setExporting(true, format);
    selectedNote.textContent = `正在生成 ${label} 文件（${ids.length} 条）…`;
    try {
      const response = await fetch('/api/public/exports', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids, format }),
        cache: 'no-store',
      });
      if (!response.ok) throw new Error(await readError(response));
      const type = response.headers.get('Content-Type') || '';
      if (!type.includes('openxmlformats')) throw new Error('服务返回的不是 Office 文件');
      const blob = await response.blob();
      if (!blob.size) throw new Error('服务返回了空文件');
      const filename = filenameFrom(response, `industry-results.${format}`);
      saveBlob(blob, filename);
      selectedNote.textContent = `已生成 ${filename}（${ids.length} 条），请在浏览器下载目录查看。`;
      setStatus(searchStatus, `已导出 ${ids.length} 条资料为 ${label}。`);
    } catch (error) {
      let message = errorText(error);
      const withdrawn = /资料\s*(\d+)\s*未发布或已撤回/.exec(message);
      if (withdrawn && selected.delete(Number(withdrawn[1]))) {
        message += '；已从所选中移除，请重新导出。';
      }
      selectedNote.textContent = '导出失败：' + message;
      setStatus(searchStatus, '导出失败：' + message, 'error');
    } finally {
      setExporting(false);
      updateSelection();
    }
  }

  // ---------- Source directory ----------

  async function loadSources() {
    sourceList.setAttribute('aria-busy', 'true');
    sourceList.replaceChildren(emptyState('正在加载来源目录…', null, true));
    try {
      const data = await getJson('/api/public/sources');
      const items = Array.isArray(data.items) ? data.items : [];
      if (!items.length) {
        sourceList.replaceChildren(emptyState('暂无已登记的来源。管理员登记来源后会显示在这里。', null, true));
        return;
      }
      sourceList.replaceChildren(...items.map((source) => el('article', { className: 'source-card' }, [
        el('strong', { text: source.name || '未命名来源' }),
        el('p', {}, [
          '发布机构：', String(source.publisher || '未标注'),
          el('br'),
          '使用范围：', RIGHTS_LABELS[source.rights_level] || String(source.rights_level || '未标注'),
        ]),
        extLink(source.homepage_url),
      ])));
    } catch (error) {
      sourceList.replaceChildren(emptyState('来源目录加载失败：' + errorText(error),
        { label: '重新加载', run: loadSources }, true));
    } finally {
      sourceList.removeAttribute('aria-busy');
    }
  }

  // ---------- Wiring ----------

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    search();
  });
  fields.kind.addEventListener('change', search);
  exportButtons.xlsx.addEventListener('click', () => exportSelected('xlsx'));
  exportButtons.docx.addEventListener('click', () => exportSelected('docx'));
  clearButton.addEventListener('click', () => {
    selected.clear();
    setStatus(searchStatus, '已清空所选资料。');
    updateSelection();
  });

  restoreFiltersFromUrl();
  updateSelection();
  search();
  loadSources();
})();
