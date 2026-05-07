
// ─── Editor PDF ───────────────────────────────────────────────────────────────
async function loadPdfJs() {
  if (pdfjsLib) return;
  await new Promise((resolve, reject) => {
    if (window.pdfjsLib) { pdfjsLib = window.pdfjsLib; resolve(); return; }
    const s = document.createElement('script');
    s.src = 'https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.min.js';
    s.onload = () => {
      window.pdfjsLib.GlobalWorkerOptions.workerSrc =
        'https://cdn.jsdelivr.net/npm/pdfjs-dist@3.11.174/build/pdf.worker.min.js';
      pdfjsLib = window.pdfjsLib;
      resolve();
    };
    s.onerror = reject;
    document.head.appendChild(s);
  });
}

async function openPdfEditor(month, mode = 'validation') {
  pdfEditorMode  = mode;
  pdfEditorMonth = month;

  // Configura UI conforme modo
  if (mode === 'validation') {
    document.getElementById('pdf-editor-title').textContent = 'Validação de captura';
    document.getElementById('pdf-editor-subtitle').removeAttribute('hidden');
    document.getElementById('validation-strip').removeAttribute('hidden');
    document.getElementById('pdf-edit-tools').setAttribute('hidden', '');
  } else {
    const monthLabel = month ? ` — ${fmtMonthLabel(month)}` : '';
    document.getElementById('pdf-editor-title').textContent = `Editor de PDF${monthLabel}`;
    document.getElementById('pdf-editor-subtitle').setAttribute('hidden', '');
    document.getElementById('validation-strip').setAttribute('hidden', '');
    document.getElementById('pdf-edit-tools').removeAttribute('hidden');
    // Modo criar lançamento sempre ativo em modo edição
    createLancMode = true;
    document.getElementById('pdf-editor-body').classList.add('capture-mode');
    document.getElementById('create-lanc-hint-banner').removeAttribute('hidden');
    showEditTutorial();
  }

  document.getElementById('btn-capture-toggle').setAttribute('hidden', '');
  document.getElementById('pdf-editor-modal').removeAttribute('hidden');

  const loadingEl = document.getElementById('pdf-loading');
  loadingEl.textContent = 'Carregando PDF…';
  loadingEl.removeAttribute('hidden');
  document.getElementById('pdf-pages-container').innerHTML = '';

  const fileIds = currentSession.file_ids || (currentSession.file_id ? [currentSession.file_id] : []);
  const lancs   = month
    ? (currentSession.meses?.[month] || [])
    : Object.values(currentSession.meses || {}).flat();
  const seen    = new Map();
  lancs.forEach(l => {
    const s  = l.state;
    const fi = s.bbox?.file_idx ?? s.file_idx ?? 0;
    const pg = s.bbox?.page     ?? s.page;
    if (pg === undefined || pg === null) return;
    const key = `${fi}:${pg}`;
    if (!seen.has(key)) seen.set(key, { fileIdx: fi, page: pg });
  });
  pdfPages = [...seen.values()].sort((a, b) => a.fileIdx - b.fileIdx || a.page - b.page);

  if (pdfPages.length === 0) {
    loadingEl.textContent = 'Coordenadas não disponíveis.';
    return;
  }

  if (fileIds.length === 0) {
    loadingEl.textContent = 'PDF original não disponível para esta sessão.';
    return;
  }

  try {
    await loadPdfJs();
    pdfCurrentIdx    = 0;
    pdfCurrentFileId = null;
    await renderPdfPage(0);
    loadingEl.setAttribute('hidden', '');
    updatePdfNav();
  } catch (err) {
    loadingEl.textContent = `Erro ao carregar PDF: ${err.message || err}`;
  }
}

async function renderPdfPage(idx) {
  selectedLancId = null;
  const container = document.getElementById('pdf-pages-container');
  container.innerHTML = '';

  const { fileIdx, page: pageIdx } = pdfPages[idx];
  const fileIds  = captureFileId
    ? [captureFileId]
    : (currentSession?.file_ids || (currentSession?.file_id ? [currentSession.file_id] : []));
  const fileId   = captureFileId ?? (fileIds[fileIdx] ?? fileIds[0]);

  if (fileId !== pdfCurrentFileId) {
    pdfDoc = await pdfjsLib.getDocument({
      url        : `${API_BASE}/upload/${fileId}/pdf`,
      httpHeaders: getToken() ? { Authorization: `Bearer ${getToken()}` } : {},
    }).promise;
    pdfCurrentFileId = fileId;
  }

  const pageNum = pageIdx;
  const page    = await pdfDoc.getPage(pageNum);
  const vp      = page.getViewport({ scale: PDF_SCALE });

  const wrapper = document.createElement('div');
  wrapper.className = 'pdf-page-wrapper';

  const dpr     = window.devicePixelRatio || 1;
  const canvas  = document.createElement('canvas');
  canvas.width  = vp.width  * dpr;
  canvas.height = vp.height * dpr;
  canvas.style.width  = `${vp.width}px`;
  canvas.style.height = `${vp.height}px`;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  await page.render({ canvasContext: ctx, viewport: vp }).promise;
  wrapper.appendChild(canvas);

  // Overlays de preview — modo teste de assinatura
  if (captureFileId && capturedPreview.length > 0) {
    const lancsNaPagina = capturedPreview.filter(l => l.page === pageIdx);
    lancsNaPagina.forEach(l => {
      const bbox = l.bbox;
      if (!bbox) return;
      const overlay = document.createElement('div');
      overlay.className = 'pdf-overlay pdf-overlay-active';
      overlay.style.left   = `${bbox.x0 * PDF_SCALE}px`;
      overlay.style.top    = `${bbox.y0 * PDF_SCALE}px`;
      overlay.style.width  = `${(bbox.x1 - bbox.x0) * PDF_SCALE}px`;
      overlay.style.height = `${(bbox.y1 - bbox.y0) * PDF_SCALE}px`;
      overlay.style.pointerEvents = 'none';
      const tip = document.createElement('span');
      tip.className = 'pdf-overlay-title';
      const val = (l.entrada != null && l.entrada !== 0)
        ? `+${fmtBRL(l.entrada)}`
        : (l.saida != null && l.saida !== 0) ? `-${fmtBRL(Math.abs(l.saida))}` : '—';
      tip.textContent = `${l.descricao || ''} · ${val}`;
      overlay.appendChild(tip);
      wrapper.appendChild(overlay);
    });
  }

  // Overlays de sessão
  if (!captureFileId && currentSession && pdfEditorMode) {
    const _allLancs = Object.values(currentSession.meses || {}).flat();
    const lancsNaPagina = _allLancs.filter(l => {
      const s  = l.state;
      if (s.flag === 'ignorar') return false;
      const fi = s.bbox?.file_idx ?? s.file_idx ?? 0;
      const pg = s.bbox?.page     ?? s.page;
      return fi === fileIdx && pg === pageIdx;
    });

    lancsNaPagina.forEach(l => {
      const bbox   = l.state.bbox;
      const active = l.state.active;
      const isOtherMonth = pdfEditorMonth
        ? !Object.keys(currentSession.meses || {}).some(m =>
            m === pdfEditorMonth && (currentSession.meses[m] || []).some(x => x.lanc_id === l.lanc_id)
          )
        : false;

      const overlay = document.createElement('div');
      overlay.className = `pdf-overlay ${active ? 'pdf-overlay-active' : 'pdf-overlay-inactive'}`;
      if (isOtherMonth) overlay.style.opacity = '0.35';

      if (bbox) {
        overlay.style.left   = `${bbox.x0 * PDF_SCALE}px`;
        overlay.style.top    = `${bbox.y0 * PDF_SCALE}px`;
        overlay.style.width  = `${(bbox.x1 - bbox.x0) * PDF_SCALE}px`;
        overlay.style.height = `${(bbox.y1 - bbox.y0) * PDF_SCALE}px`;
      } else {
        return;
      }

      const tip = document.createElement('span');
      tip.className   = 'pdf-overlay-title';
      tip.textContent = `${l.state.descricao || ''} · ${fmtBRL(l.state.valor)}`;
      overlay.appendChild(tip);

      if (pdfEditorMode === 'edit') {
        _attachResizeHandles(overlay, el => {
          const b = _elToBbox(el);
          sendMutation('edit_bbox', l.lanc_id, b);
        });
        overlay.addEventListener('click', () => {
          if (overlay.dataset.justDragged === '1') { delete overlay.dataset.justDragged; return; }
          _selectLanc(l.lanc_id, overlay);
        });
      } else {
        overlay.style.cursor        = 'default';
        overlay.style.pointerEvents = 'none';
      }

      wrapper.appendChild(overlay);
    });
  }

  container.appendChild(wrapper);

  if (!captureFileId) _updateTxPanel(pageIdx, fileIdx);
}

function updatePdfNav() {
  document.getElementById('pdf-page-indicator').textContent =
    `${pdfCurrentIdx + 1} / ${pdfPages.length}`;
  document.getElementById('btn-pdf-prev').disabled = pdfCurrentIdx === 0;
  document.getElementById('btn-pdf-next').disabled = pdfCurrentIdx === pdfPages.length - 1;
}

function closePdfEditor() {
  document.getElementById('pdf-editor-modal').setAttribute('hidden', '');
  pdfDoc           = null;
  pdfCurrentFileId = null;
  pdfEditorMonth   = null;
  pdfEditorMode    = null;
  captureFileId    = null;
  createLancPending = null;
  document.getElementById('pdf-pages-container').innerHTML = '';
  document.getElementById('pdf-tx-panel').innerHTML = '';
  if (captureMode) toggleCaptureMode();
  createLancMode = false;
  selectedLancId = null;
  document.getElementById('pdf-editor-body').classList.remove('capture-mode');
  document.getElementById('create-lanc-hint-banner').setAttribute('hidden', '');
  document.getElementById('sig-panel').setAttribute('hidden', '');
  document.getElementById('create-lanc-panel').setAttribute('hidden', '');
  document.getElementById('btn-capture-toggle').removeAttribute('hidden');
  document.getElementById('pdf-editor-subtitle').setAttribute('hidden', '');
  document.getElementById('validation-strip').setAttribute('hidden', '');
  document.getElementById('pdf-edit-tools').setAttribute('hidden', '');
  capturedSig     = null;
  capturedColunas = [];
  _removeCaptureRect();
}

// ─── Overlay de validação pós-upload ─────────────────────────────────────────
async function openValidationOverlay() {
  const fileIds = currentSession.file_ids || (currentSession.file_id ? [currentSession.file_id] : []);
  if (fileIds.length === 0) return;

  const lancs = Object.values(currentSession.meses || {}).flat();
  const seen  = new Map();
  lancs.forEach(l => {
    const s  = l.state;
    const fi = s.bbox?.file_idx ?? s.file_idx ?? 0;
    const pg = s.bbox?.page     ?? s.page;
    if (pg == null) return;
    const key = `${fi}:${pg}`;
    if (!seen.has(key)) seen.set(key, { fileIdx: fi, page: pg });
  });
  valPages = [...seen.values()].sort((a, b) => a.fileIdx - b.fileIdx || a.page - b.page);
  if (valPages.length === 0) return;

  valCurrentIdx       = 0;
  valPdfDoc           = null;
  valPdfCurrentFileId = null;

  const overlay = document.getElementById('val-overlay');
  overlay.removeAttribute('hidden');
  _updateValNav();

  try {
    await loadPdfJs();
    await renderValBlock(0);
  } catch (err) {
    document.getElementById('val-canvas-loading').textContent =
      `Erro ao carregar PDF: ${err.message || err}`;
  }
}

async function renderValBlock(idx) {
  const loading = document.getElementById('val-canvas-loading');
  const canvas  = document.getElementById('val-canvas');
  loading.textContent = 'Carregando…';
  loading.removeAttribute('hidden');
  canvas.setAttribute('hidden', '');

  const { fileIdx, page: pageIdx } = valPages[idx];
  const fileIds = currentSession.file_ids || (currentSession.file_id ? [currentSession.file_id] : []);
  const fileId  = fileIds[fileIdx] ?? fileIds[0];

  if (fileId !== valPdfCurrentFileId) {
    valPdfDoc = await pdfjsLib.getDocument({
      url        : `${API_BASE}/upload/${fileId}/pdf`,
      httpHeaders: getToken() ? { Authorization: `Bearer ${getToken()}` } : {},
    }).promise;
    valPdfCurrentFileId = fileId;
  }

  const page    = await valPdfDoc.getPage(pageIdx);
  const nativeVp = page.getViewport({ scale: 1 });
  const maxW     = Math.min(480, document.getElementById('val-canvas-area').clientWidth - 24);
  const scale    = Math.min(maxW / nativeVp.width, 1.0);
  const vp       = page.getViewport({ scale });

  const dpr = window.devicePixelRatio || 1;
  canvas.width  = vp.width  * dpr;
  canvas.height = vp.height * dpr;
  canvas.style.width  = `${vp.width}px`;
  canvas.style.height = `${vp.height}px`;

  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  await page.render({ canvasContext: ctx, viewport: vp }).promise;

  const allLancs = Object.values(currentSession.meses || {}).flat();
  allLancs.forEach(l => {
    const s  = l.state;
    const fi = s.bbox?.file_idx ?? s.file_idx ?? 0;
    const pg = s.bbox?.page     ?? s.page;
    if (fi !== fileIdx || pg !== pageIdx || !s.bbox) return;

    const { x0, y0, x1, y1 } = s.bbox;
    const x = x0 * scale, y = y0 * scale;
    const w = (x1 - x0) * scale, h = (y1 - y0) * scale;

    ctx.fillStyle   = s.active ? 'rgba(22,163,74,0.15)' : 'rgba(220,38,38,0.08)';
    ctx.strokeStyle = s.active ? '#16a34a'               : '#dc2626';
    ctx.lineWidth   = 1.5;
    ctx.fillRect(x, y, w, h);
    ctx.strokeRect(x, y, w, h);
  });

  loading.setAttribute('hidden', '');
  canvas.removeAttribute('hidden');
}

function _updateValNav() {
  document.getElementById('val-overlay-counter').textContent =
    `Bloco ${valCurrentIdx + 1} de ${valPages.length}`;
  document.getElementById('val-btn-prev').disabled = valCurrentIdx === 0;
  document.getElementById('val-btn-next').disabled = valCurrentIdx === valPages.length - 1;
}

function closeValidationOverlay() {
  document.getElementById('val-overlay').setAttribute('hidden', '');
  valPdfDoc           = null;
  valPdfCurrentFileId = null;
  valPages            = [];
  valCurrentIdx       = 0;
}

function activateEditMode() {
  pdfEditorMode = 'edit';
  document.getElementById('pdf-editor-title').textContent = 'Editor de PDF';
  document.getElementById('pdf-editor-subtitle').setAttribute('hidden', '');
  document.getElementById('validation-strip').setAttribute('hidden', '');
  document.getElementById('pdf-edit-tools').removeAttribute('hidden');
  createLancMode = true;
  document.getElementById('pdf-editor-body').classList.add('capture-mode');
  document.getElementById('create-lanc-hint-banner').removeAttribute('hidden');
  showEditTutorial();
  renderPdfPage(pdfCurrentIdx);
}

// ─── Criador de modelos ───────────────────────────────────────────────────────
async function recriarcaptura() {
  const fileIds = currentSession?.file_ids || (currentSession?.file_id ? [currentSession.file_id] : []);
  if (fileIds.length === 0) { toast('Nenhum arquivo disponível', 'error'); return; }

  if (fileIds.length === 1) {
    _abrirCriadorComArquivo(fileIds[0]);
    return;
  }

  const bancos = currentSession.bancos_detectados || [];
  const picker = document.getElementById('recriar-file-picker');
  const lista  = document.getElementById('recriar-file-list');
  lista.innerHTML = '';

  fileIds.forEach((fid, i) => {
    const banco = bancos[i];
    const entry = bancosDisponiveis.find(b => b.key === banco);
    const label = entry ? entry.label : (banco || `Arquivo ${i + 1}`);

    const btn = document.createElement('button');
    btn.className   = 'recriar-file-btn';
    btn.textContent = label;
    btn.addEventListener('click', () => {
      picker.setAttribute('hidden', '');
      _abrirCriadorComArquivo(fid);
    });
    lista.appendChild(btn);
  });

  picker.removeAttribute('hidden');
}

function _abrirCriadorComArquivo(fileId) {
  const maxPage = pdfPages.length > 0 ? Math.max(...pdfPages.map(p => p.page)) : 5;
  openPdfEditorForCapture(fileId, maxPage);
}

function toggleCreateLancMode() {
  createLancMode = !createLancMode;
  const body      = document.getElementById('pdf-editor-body');
  const banner    = document.getElementById('create-lanc-hint-banner');
  const btnCreate = document.getElementById('btn-mode-create-lanc');
  const btnAdjust = document.getElementById('btn-mode-adjust');

  if (createLancMode) {
    body.classList.add('capture-mode');
    banner.removeAttribute('hidden');
    btnCreate.classList.add('active');
    btnAdjust.classList.remove('active');
    document.getElementById('create-lanc-panel').setAttribute('hidden', '');
    _removeCaptureRect();
  } else {
    body.classList.remove('capture-mode');
    banner.setAttribute('hidden', '');
    btnCreate.classList.remove('active');
    btnAdjust.classList.add('active');
    _removeCaptureRect();
    document.getElementById('create-lanc-panel').setAttribute('hidden', '');
    createLancPending = null;
  }
}

async function _runCreateLanc(x0, y0, x1, y1) {
  if (!captureContext) return;
  const { fileId, page, fileIdx } = captureContext;
  const loadingEl = document.getElementById('pdf-loading');
  loadingEl.textContent = 'Extraindo lançamento…';
  loadingEl.removeAttribute('hidden');

  try {
    const resultado = await api('POST', `/upload/${fileId}/extrair-area`, {
      file_id: fileId, page, x0, y0, x1, y1,
    });

    createLancPending = {
      lancamento: { ...resultado, bbox: { x0, y0, x1, y1, file_idx: fileIdx, page } },
    };

    _showCreateLancPanel(createLancPending.lancamento);

    if (captureRectEl && !captureRectEl.classList.contains('resizable')) {
      captureStart = null;
      _attachResizeHandles(captureRectEl, el => {
        const b = _elToBbox(el);
        _runCreateLanc(b.x0, b.y0, b.x1, b.y1);
      });
      const deleteBtn = document.createElement('button');
      deleteBtn.className = 'capture-rect-delete';
      deleteBtn.title = 'Remover seleção';
      deleteBtn.textContent = '✕';
      deleteBtn.addEventListener('mousedown', e => e.stopPropagation());
      deleteBtn.addEventListener('click', e => {
        e.stopPropagation();
        _removeCaptureRect();
        document.getElementById('create-lanc-panel').setAttribute('hidden', '');
        createLancPending = null;
      });
      captureRectEl.appendChild(deleteBtn);
    }
  } catch (err) {
    toast(err.message || 'Erro ao extrair lançamento', 'error');
  } finally {
    loadingEl.setAttribute('hidden', '');
  }
}

function _showCreateLancPanel(l) {
  const hint   = document.getElementById('create-lanc-hint');
  const listEl = document.getElementById('create-lanc-preview-list');

  hint.textContent = 'Confirme os dados antes de adicionar.';
  listEl.innerHTML = '';

  const dateWrap = document.createElement('div');
  dateWrap.className = 'create-lanc-field';
  const dateLbl = document.createElement('label');
  dateLbl.textContent = 'Data';
  dateLbl.className   = 'create-lanc-label';
  const dateInput = document.createElement('input');
  dateInput.type        = 'date';
  dateInput.id          = 'create-lanc-data';
  dateInput.className   = 'form-input';
  dateInput.value       = l.data || '';
  dateInput.placeholder = 'AAAA-MM-DD';
  if (!l.data) dateInput.style.borderColor = 'var(--color-warn, #f59e0b)';
  dateWrap.append(dateLbl, dateInput);

  const descrWrap = document.createElement('div');
  descrWrap.className = 'create-lanc-field';
  const descrLbl = document.createElement('label');
  descrLbl.textContent = 'Descrição';
  descrLbl.className   = 'create-lanc-label';
  const descrInput = document.createElement('input');
  descrInput.type      = 'text';
  descrInput.id        = 'create-lanc-descricao';
  descrInput.className = 'form-input';
  descrInput.value     = l.descricao || '';
  descrWrap.append(descrLbl, descrInput);

  const valWrap = document.createElement('div');
  valWrap.className = 'create-lanc-field';
  const valLbl = document.createElement('label');
  valLbl.textContent = 'Valor';
  valLbl.className   = 'create-lanc-label';
  const valorNum = (l.entrada != null && l.entrada !== 0)
    ? l.entrada
    : (l.saida != null && l.saida !== 0) ? -Math.abs(l.saida) : 0;
  const valInput = document.createElement('input');
  valInput.type      = 'number';
  valInput.id        = 'create-lanc-valor';
  valInput.className = 'form-input';
  valInput.step      = '0.01';
  valInput.value     = valorNum !== 0 ? valorNum : '';
  valInput.placeholder = 'Ex: 1500.00 ou -45.90';
  valWrap.append(valLbl, valInput);

  listEl.append(dateWrap, descrWrap, valWrap);

  if (!l.data) {
    const warn = document.createElement('p');
    warn.className   = 'create-lanc-warn';
    warn.textContent = 'Data não encontrada na seleção — preencha acima.';
    listEl.appendChild(warn);
  }

  document.getElementById('create-lanc-panel').removeAttribute('hidden');
  if (!l.data) dateInput.focus();
}

async function _confirmCreateLanc() {
  if (!createLancPending || !currentSession) return;

  const data      = (document.getElementById('create-lanc-data')?.value      || '').trim();
  const descricao = (document.getElementById('create-lanc-descricao')?.value || '').trim();
  const valorRaw  = parseFloat(document.getElementById('create-lanc-valor')?.value || '0');

  if (!data) {
    toast('Informe a data do lançamento', 'error');
    document.getElementById('create-lanc-data')?.focus();
    return;
  }
  if (isNaN(valorRaw) || valorRaw === 0) {
    toast('Informe o valor do lançamento', 'error');
    document.getElementById('create-lanc-valor')?.focus();
    return;
  }

  const bbox  = createLancPending.lancamento.bbox;
  const valor = valorRaw;

  try {
    await sendMutation('create_lanc', null, {
      data,
      descricao,
      valor,
      active: valor > 0,
      flag  : valor > 0 ? 'renda_recorrente' : 'despesa_recorrente',
      bbox,
    });

    toast('Lançamento adicionado', 'success');
    document.getElementById('create-lanc-panel').setAttribute('hidden', '');
    _removeCaptureRect();
    createLancPending = null;
    await renderPdfPage(pdfCurrentIdx);
  } catch (e) {
    toast(e.message || 'Erro ao adicionar lançamento', 'error');
  }
}

async function openPdfEditorForCapture(fileId, totalPages = 1) {
  captureFileId   = fileId;
  pdfEditorMonth  = null;
  capturedPreview = [];
  capturedSig     = null;
  capturedColunas = [];

  document.getElementById('pdf-editor-title').textContent = 'Identificar padrão do extrato';
  document.getElementById('btn-capture-toggle').removeAttribute('hidden');
  document.getElementById('pdf-editor-modal').removeAttribute('hidden');
  showCaptureTutorial();

  const loadingEl = document.getElementById('pdf-loading');
  loadingEl.textContent = 'Carregando PDF…';
  loadingEl.removeAttribute('hidden');
  document.getElementById('pdf-pages-container').innerHTML = '';
  document.getElementById('pdf-tx-panel').innerHTML = '';

  pdfPages      = Array.from({ length: totalPages }, (_, i) => ({ fileIdx: 0, page: i + 1 }));
  pdfCurrentIdx = 0;

  try {
    await loadPdfJs();
    pdfCurrentFileId = null;
    await renderPdfPage(0);
    loadingEl.setAttribute('hidden', '');
    updatePdfNav();
    if (!captureMode) toggleCaptureMode();
  } catch (err) {
    loadingEl.textContent = `Erro ao carregar PDF: ${err.message || err}`;
  }
}

