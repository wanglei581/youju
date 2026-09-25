(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const state = { token: '', batchId: '', sources: [] };
  const status = (id, message, error = false) => {
    const node = $(id);
    node.textContent = message;
    node.classList.toggle('error', error);
  };
  const node = (tag, text, className = '') => {
    const result = document.createElement(tag);
    result.textContent = text;
    if (className) result.className = className;
    return result;
  };
  const request = async (path, options = {}) => {
    const response = await fetch(path, {
      ...options,
      headers: {
        Authorization: `Bearer ${state.token}`,
        ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      },
      cache: 'no-store',
    });
    if (!response.ok) {
      let message = `请求失败（HTTP ${response.status}）`;
      try { message = (await response.json()).error || message; } catch (_) { /* non JSON */ }
      throw new Error(message);
    }
    return response;
  };
  const json = async (path, options) => (await request(path, options)).json();
  const post = (path, data) => json(path, { method: 'POST', body: JSON.stringify(data) });
  const errorText = (error) => error instanceof Error ? error.message : String(error);

  async function refreshSources() {
    const data = await json('/api/admin/sources');
    state.sources = data.items || [];
    const select = $('import-source');
    select.replaceChildren(new Option('选择已登记来源', ''));
    for (const source of state.sources) {
      if (source.status === 'active') select.add(new Option(`${source.name} · ${source.rights_level}`, String(source.id)));
    }
    renderSources();
  }

  function renderSources() {
    const list = $('admin-source-list');
    if (!state.sources.length) { list.replaceChildren(node('p', '尚无来源。')); return; }
    list.replaceChildren(...state.sources.map((source) => {
      const card = node('article', '', 'admin-record');
      card.append(node('strong', `${source.name} · ${source.status === 'suspended' ? '已暂停' : source.rights_level}`));
      card.append(node('p', source.publisher));
      if (source.status === 'active') {
        const buttons = node('div', '', 'row');
        for (const [mode, label] of [['directory_only', '限制为仅题录'], ['suspend', '暂停来源']]) {
          if (mode === 'directory_only' && source.rights_level === 'directory_only') continue;
          const button = node('button', label, 'button secondary');
          button.type = 'button';
          button.addEventListener('click', async () => {
            const reason = prompt(`${label}的原因：`);
            if (reason === null) return;
            if (!reason.trim()) { status('admin-status', '请填写限制原因。', true); return; }
            button.disabled = true;
            try {
              await post(`/api/admin/sources/${source.id}/restrict`, { mode, reason });
              status('admin-status', `来源 ${source.name} 已${mode === 'suspend' ? '暂停' : '限制为仅题录'}。`);
              await Promise.all([refreshSources(), refreshRecords()]);
            } catch (error) { status('admin-status', errorText(error), true); button.disabled = false; }
          });
          buttons.append(button);
        }
        card.append(buttons);
      }
      return card;
    }));
  }

  function recordCard(record) {
    const card = node('article', '', 'admin-record');
    const title = record.draft_title || record.published_title || `资料 #${record.id}`;
    card.append(node('h3', title));
    card.append(node('p', `来源：${record.source_name} · 权限：${record.rights_level}`));
    card.append(node('p', `待审版本：${record.draft_version_no || '无'} · 已发布版本：${record.published_version_no || '无'}`));
    const link = document.createElement('a');
    link.textContent = '打开原站核对';
    link.href = record.original_url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    card.append(link);
    if (record.draft_answer) card.append(node('p', `待审答案：${record.draft_answer}`));
    if (record.draft_evidence_locator) card.append(node('p', `证据位置：${record.draft_evidence_locator}`));
    const actions = node('div', '', 'row');
    if (record.draft_version_no) {
      const publish = node('button', '核验并发布', 'button');
      publish.type = 'button';
      publish.addEventListener('click', async () => {
        const note = prompt('请填写核验说明，写明核对的原站位置或原因：');
        if (note === null) return;
        if (record.draft_answer && !note.trim()) {
          status('admin-status', '有答案的资料必须填写核验说明。', true);
          return;
        }
        publish.disabled = true;
        try {
          await post(`/api/admin/records/${record.id}/publish`, { review_note: note });
          status('admin-status', `资料 #${record.id} 已发布。`);
          await refreshRecords();
        } catch (error) { status('admin-status', errorText(error), true); }
        finally { publish.disabled = false; }
      });
      actions.append(publish);
    }
    if (record.published_version_no) {
      const withdraw = node('button', '撤回公开版本', 'button secondary');
      withdraw.type = 'button';
      withdraw.addEventListener('click', async () => {
        const reason = prompt('撤回原因：');
        if (reason === null) return;
        if (!reason.trim()) { status('admin-status', '请填写撤回原因。', true); return; }
        withdraw.disabled = true;
        try {
          await post(`/api/admin/records/${record.id}/withdraw`, { reason });
          status('admin-status', `资料 #${record.id} 已撤回。`);
          await refreshRecords();
        } catch (error) { status('admin-status', errorText(error), true); }
        finally { withdraw.disabled = false; }
      });
      actions.append(withdraw);
    }
    card.append(actions);
    return card;
  }

  async function refreshRecords() {
    const data = await json('/api/admin/records');
    const records = data.items || [];
    $('records-count').textContent = `${records.length} 条资料`;
    $('admin-records').replaceChildren(...(records.length ? records.map(recordCard) : [node('p', '暂无资料。先导入并提交待审版本。')]));
  }

  async function connect() {
    const token = $('admin-token').value.trim();
    if (!token) { status('admin-status', '请先输入管理令牌。', true); return; }
    state.token = token;
    try {
      await Promise.all([refreshSources(), refreshRecords()]);
      $('admin-token').value = '';
      status('admin-status', '管理台已连接。令牌仅保存在本页内存中。');
    } catch (error) {
      state.token = '';
      status('admin-status', errorText(error), true);
    }
  }

  function formValues(form) {
    return Object.fromEntries(new FormData(form).entries());
  }

  async function addSource(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    try {
      const created = await post('/api/admin/sources', formValues(form));
      status('admin-status', `已登记来源：${created.name}`);
      form.reset();
      await refreshSources();
    } catch (error) { status('admin-status', errorText(error), true); }
    finally { button.disabled = false; }
  }

  function fileBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || '').split(',')[1] || '');
      reader.onerror = () => reject(new Error('文件读取失败'));
      reader.readAsDataURL(file);
    });
  }

  async function previewImport(event) {
    event.preventDefault();
    const file = $('import-file').files[0];
    const sourceId = Number($('import-source').value);
    if (!file || !sourceId) { status('import-status', '请选择来源和文件。', true); return; }
    if (file.size > 2_000_000) { status('import-status', '文件超过 2 MB。', true); return; }
    state.batchId = '';
    $('import-commit').disabled = true;
    $('import-preview').replaceChildren();
    status('import-status', '正在校验文件…');
    try {
      const result = await post('/api/admin/imports/preview', {
        source_id: sourceId, filename: file.name, content_base64: await fileBase64(file),
      });
      state.batchId = result.batch_id || '';
      $('import-commit').disabled = !state.batchId;
      const errors = result.errors || [];
      const preview = result.preview || [];
      const lines = [node('p', `有效 ${result.valid_count || 0} 行；错误 ${errors.length} 行。${state.batchId ? '可提交为待审版本。' : '请修正文件后重新预览。'}`)];
      for (const item of errors) lines.push(node('p', `第 ${item.row} 行：${item.message}`, 'error'));
      for (const item of preview) lines.push(node('p', `${item.title} · ${item.original_url}`));
      $('import-preview').replaceChildren(...lines);
      status('import-status', errors.length ? '预览发现错误。' : '预览通过。', !!errors.length);
    } catch (error) { status('import-status', errorText(error), true); }
  }

  async function commitImport() {
    if (!state.batchId) return;
    const button = $('import-commit');
    button.disabled = true;
    try {
      const result = await post('/api/admin/imports/commit', { batch_id: state.batchId });
      state.batchId = '';
      status('import-status', `已提交 ${result.inserted_drafts} 条待审版本；${result.unchanged} 条未变。`);
      await refreshRecords();
    } catch (error) { status('import-status', errorText(error), true); button.disabled = false; }
  }

  async function download(path, filename) {
    try {
      const response = await request(path);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (error) { status('admin-status', errorText(error), true); }
  }

  $('admin-connect').addEventListener('click', connect);
  $('source-form').addEventListener('submit', addSource);
  $('import-form').addEventListener('submit', previewImport);
  $('import-commit').addEventListener('click', commitImport);
  $('records-refresh').addEventListener('click', () => refreshRecords().catch(error => status('admin-status', errorText(error), true)));
  $('template-download').addEventListener('click', () => download('/api/admin/template.csv', 'industry-import-template.csv'));
  $('inventory-download').addEventListener('click', () => download('/api/admin/inventory.csv', 'industry-inventory.csv'));
})();
