'use strict';

// Classic script, loaded after workspace.js. These two bindings are shared with updateBusy().
let DRAFT = null;
let PREVIEW_EMPLOYEES = [];

const templateEditor = (() => {
  let fonts = [], dirty = false, savedDraft = '';
  let revision = 0, previewRevision = -1, previewRequest = 0;
  let images = {}, previewError = '', timer = null;
  let employeesDirty = true, employeeVersion = 0;
  let bound = false;
  const keys = () => Object.keys(DRAFT?.templates || {});
  const currentKey = () => $('#tpl-key')?.value || keys()[0];
  const currentTemplate = () => DRAFT?.templates?.[currentKey()];
  const selectedEmployee = () => PREVIEW_EMPLOYEES.find(e => sameId(e.id, $('#pv-employee').value));
  const alignFactor = { l: 0, m: 0.5, r: 1 };
  const cssAlign = { l: 'left', m: 'center', r: 'right' };
  const bodyAlignment = body => own(alignFactor, body.halign) ? body.halign : 'l';
  function alignSignature(tpl) {
    const body = tpl.layers[1], signature = tpl.layers[4];
    const width = body.max_width, left = body.xy[0] - width * alignFactor[bodyAlignment(body)];
    signature.xy = [left + width, signature.xy?.[1] ?? 1160];
    signature.anchor = 'r' + (signature.anchor?.[1] || 't');
    signature.halign = 'r'; signature.max_width = width; signature.flow = true;
  }
  const today = () => {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  };

  function normalize(cfg) {
    if (!cfg?.templates || typeof cfg.templates !== 'object' || Array.isArray(cfg.templates) || !Object.keys(cfg.templates).length) {
      throw new Error('模板配置为空或格式错误');
    }
    const draft = clone(cfg);
    draft.vars = { company: '', ...(draft.vars || {}) };
    for (const [key, tpl] of Object.entries(draft.templates)) {
      if (!Array.isArray(tpl.layers) || tpl.layers.length !== 5 || tpl.layers.some(l => !l || !['text', 'paragraph'].includes(l.type || 'text'))) {
        throw new Error(`${tpl.label || key}：需要原有的五层文字模板`);
      }
      const body = tpl.layers[1];
      const merged = tpl.layers.slice(2, 4).some(l => String(l.text ?? '').length > 0);
      const text = merged ? tpl.layers.slice(1, 4).map(l => String(l.text ?? '')).filter(v => v.length > 0).join('\n\n') : String(body.text ?? '');
      const size = body.size ?? tpl.layers[2].size ?? 40;
      // Normalize every template in memory, including those never selected in the UI.
      // The name layer is untouched; empty legacy layers retain their original indexes.
      Object.assign(body, {
        type: 'paragraph', text, size, font: body.font || tpl.layers[2].font || 'auto',
        bold: merged ? false : Boolean(body.bold), flow: true, wrap: true,
        xy: body.xy || [130, key === 'birthday' ? 450 : 410],
        anchor: body.anchor || 'lt', halign: bodyAlignment(body), max_width: body.max_width ?? 820, max_height: body.max_height ?? 700,
        min_size: Math.min(body.min_size ?? 24, size), line_gap: body.line_gap ?? Math.round(size * 1.5), spacing: body.spacing ?? 0
      });
      tpl.layers[2].text = '';
      tpl.layers[3].text = '';
      alignSignature(tpl);
    }
    return draft;
  }

  function setDraftState() {
    const badge = $('#draft-state');
    if (!badge) return;
    badge.textContent = !DRAFT ? '尚未加载' : dirty ? '有未保存修改' : savedDraft ? '已保存' : '已载入';
    badge.className = 'badge ' + (dirty ? 'b-pending' : DRAFT ? 'b-verified' : '');
  }

  function updatePreviewState() {
    const stage = $('#tpl-preview'), status = $('#preview-state');
    if (!stage || !status) return;
    const stale = previewRevision !== revision || Boolean(previewError);
    stage.classList.toggle('stale', stale);
    status.textContent = BUSY.has('preview') ? '正在使用当前草稿渲染…'
      : previewError ? '预览失败：' + previewError
        : previewRevision < 0 ? '预览当前模板，未选择员工时保留名单字段。'
          : stale ? '内容或员工资料已变化，请更新预览。'
            : '预览与当前草稿一致' + (dirty || !savedDraft ? '；保存后正式生效。' : '。');
  }

  function schedulePreview() {
    clearTimeout(timer);
    if (DRAFT && $('#auto-preview')?.checked && TAB === 'tpl') {
      timer = setTimeout(previewTpl, 850);
    }
  }

  function markStale() {
    revision++;
    previewError = '';
    updatePreviewState();
    schedulePreview();
  }

  function draftChanged() {
    dirty = true;
    setDraftState();
    markStale();
  }

  function renderBackground() {
    const tpl = currentTemplate();
    const name = String(tpl.base_image || '').split(/[\\/]/).pop() || '未设置';
    $('#tpl-background').innerHTML = `<div class="bar"><span class="meta" title="${esc(tpl.base_image || '')}">当前底图：${esc(name)}</span><details><summary>替换底图</summary><label class="field">选择图片<input id="te-background-file" type="file" accept="image/png,image/jpeg,image/webp"></label><div class="bar"><button type="button" id="te-upload-background">上传底图</button><span class="meta">PNG / JPG / WebP，最大 10 MB；保存后生效。</span></div></details></div>`;
  }

  function render() {
    const tpl = currentTemplate();
    if (!tpl) return;
    const body = tpl.layers[1];
    const choices = new Map([['auto', '自动选择字体']]);
    const chineseFonts = { 'msyh.ttc':'微软雅黑', 'simsun.ttc':'宋体', 'simhei.ttf':'黑体',
      'simkai.ttf':'楷体', 'simfang.ttf':'仿宋', 'deng.ttf':'等线',
      'notosanssc-vf.ttf':'思源黑体', 'notoserifsc-vf.ttf':'思源宋体' };
    for (const font of fonts) {
      if (typeof font?.value !== 'string' || !font.value) continue;
      const filename = font.value.split(/[\\/]/).pop().toLowerCase();
      const label = chineseFonts[filename];
      if (label || /(?:^|[\\/])assets[\\/]fonts[\\/]/i.test(font.value)) choices.set(font.value, label || font.label);
    }
    if (!choices.has(body.font)) choices.set(body.font, body.font);
    const options = [...choices].map(([value, label]) => `<option value="${esc(value)}"${body.font === value ? ' selected' : ''}>${esc(label)}</option>`).join('');
    $('#tpl-editor').innerHTML = `<div class="hint" style="margin-top:14px"><b>名单姓名 → 自动称呼</b><p>${esc(tpl.layers[0].text || '{name}')}</p></div>
      <label class="field" style="margin-top:14px" for="te-body">祝福文案</label>
      <div class="text-toolbar"><div role="group" aria-label="正文整体对齐">${[['l','左对齐'],['m','居中'],['r','右对齐']].map(([value,label])=>`<button type="button" data-te-align="${value}" aria-pressed="${bodyAlignment(body)===value}">${label}</button>`).join('')}</div><span class="spacer"></span><button type="button" data-te-insert="space">插入空格</button><button type="button" data-te-insert="blank-line">插入空行</button></div>
      <textarea id="te-body" data-te-field="text" rows="6" style="text-align:${cssAlign[bodyAlignment(body)]}" aria-describedby="te-body-help">${esc(body.text)}</textarea>
      <div class="bar" aria-label="插入名单字段">${[['name', '姓名'], ['department', '部门'], ['years', '周年数'], ['date', '事件日期']].map(([key, label]) => `<button type="button" data-te-token="${key}" title="插入${label}" aria-label="插入${label}字段">${esc('{' + key + '}')} ${label}</button>`).join('')}</div>
      <p class="meta" id="te-body-help">对齐方式应用于整段正文。空格、换行和空行会保留；「插入空格」添加两个中文空格。字段自动对应名单。</p>
      <div class="grid" style="margin-top:14px"><label class="field">文案字体<select id="te-font" data-te-field="font">${options}</select></label><label class="field">文案字号<input id="te-size" data-te-field="size" type="number" min="1" max="512" step="1" required value="${esc(body.size)}"></label><label><input id="te-bold" data-te-field="bold" type="checkbox"${body.bold ? ' checked' : ''}> 文案加粗</label><label class="field full">公司名称（所有模板共用）<input id="te-company" data-te-field="company" value="${esc(DRAFT.vars.company)}"></label></div>
      <p class="meta">公司落款默认右对齐，与正文右边缘对齐；随正文高度自动下移。</p>`;
    $('#te-body').value = body.text;
    renderBackground();
    updateEmployee(false);
    renderPreview();
  }

  function edit(event) {
    const input = event.target, field = input.dataset.teField;
    const body = currentTemplate()?.layers[1];
    if (!body || !field || BUSY.has('tpl')) return;
    if (field === 'company') DRAFT.vars.company = input.value;
    else if (field === 'size') {
      body.size = input.value === '' ? null : input.valueAsNumber;
      if (Number.isInteger(body.size) && body.size > 0) {
        body.min_size = Math.min(24, body.size);
        body.line_gap = Math.round(body.size * 1.5);
      }
    } else if (field === 'bold') body.bold = input.checked;
    else if (field === 'text' || field === 'font') body[field] = input.value;
    else return;
    draftChanged();
  }

  function insertField(event) {
    const button = event.target.closest('[data-te-token]');
    if (!button || !['name', 'department', 'years', 'date'].includes(button.dataset.teToken) || BUSY.has('tpl')) return;
    const input = $('#te-body');
    input.setRangeText('{' + button.dataset.teToken + '}', input.selectionStart, input.selectionEnd, 'end');
    input.focus();
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }

  function formatBody(event) {
    if (BUSY.has('tpl')) return;
    const align = event.target.closest('[data-te-align]'), insert = event.target.closest('[data-te-insert]');
    const tpl = currentTemplate(), input = $('#te-body');
    if (!tpl || !input) return;
    if (align && own(alignFactor, align.dataset.teAlign)) {
      const body = tpl.layers[1], value = align.dataset.teAlign;
      const left = body.xy[0] - body.max_width * alignFactor[bodyAlignment(body)];
      body.xy[0] = left + body.max_width * alignFactor[value];
      body.halign = value; body.anchor = value + (body.anchor?.[1] || 't');
      alignSignature(tpl);
      input.style.textAlign = cssAlign[value];
      for (const button of document.querySelectorAll('[data-te-align]')) button.setAttribute('aria-pressed', String(button.dataset.teAlign === value));
      draftChanged();
    } else if (insert && ['space','blank-line'].includes(insert.dataset.teInsert)) {
      input.setRangeText(insert.dataset.teInsert === 'space' ? '\u3000\u3000' : '\n\n', input.selectionStart, input.selectionEnd, 'end');
      input.focus();
      input.dispatchEvent(new Event('input', { bubbles: true }));
    }
  }

  function renderEmployees(previous = '') {
    $('#pv-employee').innerHTML = '<option value="">仅预览模板字段</option>' + PREVIEW_EMPLOYEES.map(e =>
      `<option value="${esc(e.id)}">${esc(e.name)} ｜ ${esc(e.department || '未填部门')} ｜ ${esc(e.employee_no || 'ID ' + e.id)}</option>`
    ).join('');
    $('#pv-employee').value = PREVIEW_EMPLOYEES.some(e => sameId(e.id, previous)) ? previous : '';
    updateEmployee(false);
  }

  function updateEmployee(stale = true) {
    const e = selectedEmployee(), input = $('#pv-years');
    const anniversary = currentKey() === 'anniversary';
    const label = anniversary ? '周年数（按资料计算）' : '年龄（按资料计算）';
    input.readOnly = true; input.required = false; input.value = '';
    if (e) {
      const source = anniversary ? e.join_date : e.birth_date;
      const year = Number($('#pv-date').value.slice(0, 4)), startYear = Number(String(source || '').slice(0, 4));
      input.value = source && year && startYear && (anniversary || startYear > 1901) ? String(Math.max(0, year - startYear)) : '';
      $('#pv-identity').textContent = `${e.name} ｜ ${e.department || '未填部门'} ｜ ${labelFor(IDENTITY, e.identity_status || 'pending')}`;
    } else {
      $('#pv-identity').textContent = PREVIEW_EMPLOYEES.length ? '未选择员工，预览保留名单字段。' : '当前没有员工资料，预览保留 {name} 等字段。';
    }
    input.setAttribute('aria-label', label);
    const labelNode = input.labels?.[0];
    const textNode = labelNode && [...labelNode.childNodes].find(node => node.nodeType === 3 && node.textContent.trim());
    if (textNode) textNode.textContent = label;
    if (stale) markStale();
  }

  function invalidateEmployees() {
    employeesDirty = true;
    employeeVersion++;
    PREVIEW_EMPLOYEES = [];
    if ($('#pv-employee')) renderEmployees();
    markStale();
  }

  async function loadEmployees() {
    const version = employeeVersion, previous = $('#pv-employee').value;
    try {
      const rows = requireOK(await api('/api/employees?keyword=&only_active=true'));
      if (version !== employeeVersion) return;
      if (!Array.isArray(rows)) throw new Error('预览员工列表格式错误');
      PREVIEW_EMPLOYEES = rows.filter(e => e && e.id != null && isActive(e));
      employeesDirty = false;
      renderEmployees(previous);
    } catch (error) {
      if (version !== employeeVersion) return;
      PREVIEW_EMPLOYEES = [];
      employeesDirty = true;
      renderEmployees();
      report('tpl', '员工列表加载失败，可继续预览模板字段', [error.message], true);
    }
    markStale();
    updateBusy();
  }

  async function load(force = false) {
    bind();
    if (BUSY.has('tpl')) return;
    await busy('tpl', 'tpl', async () => {
      if (!DRAFT || force) {
        const previous = currentKey();
        const results = await Promise.allSettled([api('/api/templates'), api('/api/fonts')]);
        if (results[0].status === 'rejected') throw results[0].reason;
        const draft = normalize(requireOK(results[0].value));
        const fontResult = results[1];
        if (fontResult.status === 'fulfilled' && fontResult.value?.ok !== false && Array.isArray(fontResult.value?.fonts)) fonts = fontResult.value.fonts;
        else {
          fonts = [];
          report('tpl', '字体列表加载失败', ['已保留当前字体，可继续编辑文案，重载模板后重试。'], true);
        }
        DRAFT = draft;
        dirty = false; savedDraft = '';
        images = {}; previewRevision = -1; previewRequest++; revision++; previewError = '';
        $('#tpl-key').innerHTML = keys().map(key => `<option value="${esc(key)}">${esc(DRAFT.templates[key].label || key)}</option>`).join('');
        if (keys().includes(previous)) $('#tpl-key').value = previous;
        render();
        setDraftState();
      }
      await loadEmployees();
    });
    updatePreviewState();
    schedulePreview();
  }

  function collect() {
    if (!DRAFT) throw new Error('请先加载模板');
    for (const [key, tpl] of Object.entries(DRAFT.templates)) {
      const body = tpl.layers[1], prefix = `${tpl.label || key}：`;
      if (!Number.isInteger(body.size) || body.size < 1 || body.size > 512) throw new Error(prefix + '文案字号须为 1 到 512 的整数');
      if (typeof body.text !== 'string' || body.text.length > 10000) throw new Error(prefix + '祝福文案最多 10000 个字符');
    }
    // Full five-entry arrays are essential: the backend merges layers by index.
    return { templates: clone(DRAFT.templates), vars: clone(DRAFT.vars || {}) };
  }

  function renderPreview() {
    const key = currentKey(), url = own(images, key) ? fileURL(images[key]) : '';
    $('#tpl-preview').innerHTML = url
      ? `<button type="button" class="image-button" data-zoom="${esc(url)}" aria-label="放大模板预览"><img src="${esc(url)}" alt="${esc(currentTemplate()?.label || key)}预览"></button>`
      : '<div class="empty">当前模板尚无预览<br>点击「更新预览」，未选员工时保留字段</div>';
    updatePreviewState();
  }

  async function preview() {
    clearTimeout(timer);
    if (!DRAFT || BUSY.has('preview') || BUSY.has('tpl')) return;
    let sentRevision = -1, request = -1;
    await busy('preview', 'tpl', async () => {
      try {
        if (employeesDirty) await loadEmployees();
        const e = selectedEmployee();
        if ($('#pv-employee').value && (!e || !isActive(e))) throw new Error('所选员工资料不可用，请刷新后重选');
        if (!$('#pv-date').value || !$('#pv-date').reportValidity()) throw new Error('请填写有效的事件日期');
        const payload = { ...collect(), event_date: $('#pv-date').value };
        if (e) payload.employee_id = e.id;
        sentRevision = revision; request = ++previewRequest;
        previewError = ''; updatePreviewState();
        const result = requireOK(await post('/api/templates/preview', payload));
        if (request !== previewRequest || sentRevision !== revision) return;
        if (!result.images || typeof result.images !== 'object' || Array.isArray(result.images)) throw new Error('预览接口未返回 images');
        const missing = Object.keys(payload.templates).filter(key => !own(result.images, key) || !fileURL(result.images[key]));
        if (missing.length) throw new Error('以下模板未返回有效预览：' + missing.map(key => DRAFT.templates[key].label || key).join('、'));
        images = clone(result.images); previewRevision = sentRevision;
        renderPreview();
      } catch (error) {
        if (request >= 0 && (request !== previewRequest || sentRevision !== revision)) return;
        previewError = error.message;
        throw error;
      }
    });
    updatePreviewState();
    if (sentRevision >= 0 && revision !== sentRevision && !previewError) schedulePreview();
  }

  async function save() {
    await busy('tpl', 'tpl', async () => {
      clearTimeout(timer);
      const payload = collect(), sentDraft = JSON.stringify(payload);
      requireOK(await post('/api/templates', payload));
      savedDraft = sentDraft;
      dirty = JSON.stringify(collect()) !== sentDraft;
      setDraftState(); updatePreviewState();
      report('tpl', '全部模板已保存', ['所有模板的文案、公司名称和底图已保存为正式配置；之后新生成的海报使用本次配置。']);
      toast('全部模板已保存');
    });
    schedulePreview();
  }

  async function upload() {
    await busy('tpl', 'tpl', async () => {
      const file = $('#te-background-file')?.files[0];
      if (!file) throw new Error('请先选择底图文件');
      if (!/\.(png|jpe?g|webp)$/i.test(file.name)) throw new Error('底图仅支持 PNG、JPG 或 WebP');
      if (!file.size || file.size > 10 * 1024 * 1024) throw new Error('底图须为非空图片，且不能超过 10 MB');
      const key = currentKey(), form = new FormData();
      form.append('file', file);
      const result = requireOK(await api('/api/templates/' + encodeURIComponent(key) + '/background', { method: 'POST', body: form }));
      if (typeof result.base_image !== 'string' || !result.base_image.trim() || !Number.isFinite(Number(result.width)) || Number(result.width) <= 0 || !Number.isFinite(Number(result.height)) || Number(result.height) <= 0) throw new Error('上传接口缺少有效的底图路径或尺寸');
      DRAFT.templates[key].base_image = result.base_image;
      draftChanged();
      renderBackground();
      report('tpl', '底图已上传至草稿', [`${DRAFT.templates[key].label || key}：${result.width} × ${result.height}；请预览并保存后生效。`]);
    });
    schedulePreview();
  }

  async function reset() {
    if (BUSY.has('tpl')) return;
    if (dirty && !await askConfirm('放弃模板草稿', '将重载已保存的配置，所有模板尚未保存的文案、字体和底图修改会丢失。', [], '放弃修改')) return;
    await load(true);
  }

  function bind() {
    if (bound || !$('#tpl-editor')) return;
    bound = true;
    if (!$('#pv-date').value) $('#pv-date').value = today();
    $('#pv-date').required = true;
    renderEmployees();
    $('#tpl-key').addEventListener('change', render);
    $('#tpl-editor').addEventListener('input', edit);
    $('#tpl-editor').addEventListener('click', insertField);
    $('#tpl-editor').addEventListener('click', formatBody);
    $('#tpl-background').addEventListener('click', event => {
      if (event.target.closest('#te-upload-background')) upload();
    });
    $('#pv-employee').addEventListener('change', () => updateEmployee());
    $('#pv-date').addEventListener('input', () => updateEmployee());
    $('#auto-preview').addEventListener('change', () => {
      clearTimeout(timer);
      if ($('#auto-preview').checked) previewTpl();
    });
    $('#preview-tpl').addEventListener('click', previewTpl);
    $('#save-tpl').addEventListener('click', save);
    $('#reset-tpl').addEventListener('click', reset);
    window.addEventListener('beforeunload', event => {
      if (dirty) { event.preventDefault(); event.returnValue = ''; }
    });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind, { once: true });
  else bind();
  return { load, preview, invalidateEmployees };
})();

function loadTpl(force = false) { return templateEditor.load(force); }
function previewTpl() { return templateEditor.preview(); }
function invalidatePreviewEmployees() { return templateEditor.invalidateEmployees(); }
