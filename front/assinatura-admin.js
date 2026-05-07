
// ─── Captura de assinatura (modo retângulo no editor PDF) ─────────────────────
function toggleCaptureMode() {
  captureMode = !captureMode;
  const btn    = document.getElementById('btn-capture-toggle');
  const body   = document.getElementById('pdf-editor-body');
  const banner = document.getElementById('capture-hint-banner');
  if (captureMode) {
    btn.classList.add('active');
    btn.textContent = 'Selecionando…';
    body.classList.add('capture-mode');
    banner.removeAttribute('hidden');
    document.getElementById('sig-panel').setAttribute('hidden', '');
  } else {
    btn.classList.remove('active');
    btn.textContent = 'Selecionar área';
    body.classList.remove('capture-mode');
    banner.setAttribute('hidden', '');
    _removeCaptureRect();
  }
}

// ─── Painel de transações da página ──────────────────────────────────────────
function _updateTxPanel(pageIdx, fileIdx) {
  const panel = document.getElementById('pdf-tx-panel');
  panel.innerHTML = '';

  const _allLancsTx = Object.values(currentSession.meses || {}).flat();
  const lancsNaPagina = _allLancsTx.filter(l => {
    const s  = l.state;
    const fi = s.bbox?.file_idx ?? s.file_idx ?? 0;
    const pg = s.bbox?.page     ?? s.page;
    return fi === fileIdx && pg === pageIdx;
  });

  if (lancsNaPagina.length === 0) return;

  const title = document.createElement('div');
  title.className   = 'pdf-tx-panel-title';
  title.textContent = `${lancsNaPagina.length} lançamento(s) nesta página`;
  panel.appendChild(title);

  lancsNaPagina.forEach(l => {
    const s      = l.state;
    const active = s.active;

    const row = document.createElement('div');
    row.className = `pdf-tx-item ${active ? 'tx-active' : 'tx-inactive'}`;
    row.title     = active ? 'Clique para excluir da apuração' : 'Clique para incluir na apuração';

    const dot  = document.createElement('span');
    dot.className = 'pdf-tx-dot';

    const dateEl = document.createElement('span');
    dateEl.className   = 'pdf-tx-date';
    dateEl.textContent = s.data || '—';

    const desc = document.createElement('span');
    desc.className   = 'pdf-tx-desc';
    desc.textContent = s.descricao || '—';

    const val = document.createElement('span');
    val.className   = 'pdf-tx-val';
    val.textContent = fmtBRL(s.valor);
    val.style.color = active ? '#16a34a' : '#dc2626';

    row.appendChild(dot);
    row.appendChild(dateEl);
    row.appendChild(desc);
    row.appendChild(val);

    row.addEventListener('click', () =>
      sendMutation('toggle_active', l.lanc_id, { active: !active })
    );

    panel.appendChild(row);
  });
}

function _removeCaptureRect() {
  if (captureRectEl) { captureRectEl.remove(); captureRectEl = null; }
  captureStart = null;
}

function _selectLanc(lancId, overlayEl) {
  document.querySelectorAll('.pdf-overlay-selected')
    .forEach(el => el.classList.remove('pdf-overlay-selected'));
  if (selectedLancId === lancId) {
    selectedLancId = null;
    return;
  }
  selectedLancId = lancId;
  overlayEl.classList.add('pdf-overlay-selected');
}

function _onCaptureMousedown(e) {
  if (!captureMode && !createLancMode) return;
  if (e.target.closest('.pdf-overlay')) return;
  const wrapper = e.target.closest('.pdf-page-wrapper');
  if (!wrapper) return;
  if (selectedLancId) {
    selectedLancId = null;
    document.querySelectorAll('.pdf-overlay-selected')
      .forEach(el => el.classList.remove('pdf-overlay-selected'));
  }
  if (captureRectEl && !captureStart) return;
  e.preventDefault();
  _removeCaptureRect();
  const rect = wrapper.getBoundingClientRect();
  captureStart = { x: e.clientX - rect.left, y: e.clientY - rect.top, wrapper };
  captureRectEl = document.createElement('div');
  captureRectEl.className = 'capture-rect';
  wrapper.appendChild(captureRectEl);

  document.body.style.userSelect   = 'none';
  document.body.style.touchAction  = 'none';

  document.addEventListener('mousemove', _onCaptureMousemove);
  document.addEventListener('mouseup',   _onCaptureMouseup);
}

function _onCaptureMousemove(e) {
  if (!captureStart) return;
  const rect = captureStart.wrapper.getBoundingClientRect();
  const cx   = e.clientX - rect.left;
  const cy   = e.clientY - rect.top;
  const x    = Math.min(captureStart.x, cx);
  const y    = Math.min(captureStart.y, cy);
  const w    = Math.abs(cx - captureStart.x);
  const h    = Math.abs(cy - captureStart.y);
  captureRectEl.style.left   = `${x}px`;
  captureRectEl.style.top    = `${y}px`;
  captureRectEl.style.width  = `${w}px`;
  captureRectEl.style.height = `${h}px`;
}

async function _onCaptureMouseup(e) {
  document.removeEventListener('mousemove', _onCaptureMousemove);
  document.removeEventListener('mouseup',   _onCaptureMouseup);
  document.body.style.userSelect  = '';
  document.body.style.touchAction = '';
  if (!(captureMode || createLancMode) || !captureStart) return;
  const rect   = captureStart.wrapper.getBoundingClientRect();
  const cx     = e.clientX - rect.left;
  const cy     = e.clientY - rect.top;
  const x0     = Math.min(captureStart.x, cx)  / PDF_SCALE;
  const y0     = Math.min(captureStart.y, cy)  / PDF_SCALE;
  const x1     = Math.max(captureStart.x, cx)  / PDF_SCALE;
  const y1     = Math.max(captureStart.y, cy)  / PDF_SCALE;

  if (x1 - x0 < 10 || y1 - y0 < 10) { _removeCaptureRect(); return; }

  const { fileIdx, page } = pdfPages[pdfCurrentIdx];
  const fileIds = captureFileId
    ? [captureFileId]
    : (currentSession?.file_ids || (currentSession?.file_id ? [currentSession.file_id] : []));
  const fileId  = captureFileId ?? (fileIds[fileIdx] ?? fileIds[0]);

  captureContext = { fileId, page, fileIdx };

  if (createLancMode) {
    await _runCreateLanc(x0, y0, x1, y1);
  } else {
    await _runCaptureExtract(x0, y0, x1, y1);
  }
}

async function _runCaptureExtract(x0, y0, x1, y1) {
  if (!captureContext) return;
  const { fileId, page } = captureContext;
  const loadingEl = document.getElementById('pdf-loading');
  loadingEl.textContent = 'Derivando assinatura…';
  loadingEl.removeAttribute('hidden');

  try {
    const resultado = await api('POST', '/assinaturas/extrair', {
      file_id: fileId, page, x0, y0, x1, y1,
    });
    capturedSig = resultado.assinatura;
    _showSigPanel(resultado);

    if (captureRectEl && !captureRectEl.classList.contains('resizable')) {
      captureStart = null;
      _attachResizeHandles(captureRectEl, el => {
        const b = _elToBbox(el);
        _runCaptureExtract(b.x0, b.y0, b.x1, b.y1);
      });
      const deleteBtn = document.createElement('button');
      deleteBtn.className = 'capture-rect-delete';
      deleteBtn.title = 'Remover seleção';
      deleteBtn.textContent = '✕';
      deleteBtn.addEventListener('mousedown', e => e.stopPropagation());
      deleteBtn.addEventListener('click', e => { e.stopPropagation(); _removeCaptureRect(); });
      captureRectEl.appendChild(deleteBtn);
    }
  } catch (err) {
    toast(err.message || 'Erro ao extrair assinatura', 'error');
  } finally {
    loadingEl.setAttribute('hidden', '');
  }
}

function _showSigPanel(resultado) {
  const panel = document.getElementById('sig-panel');
  const hint  = document.getElementById('sig-panel-hint');
  capturedPreview = resultado.preview_lancamentos || [];
  capturedColunas = resultado.colunas_detectadas  || [];
  const count = capturedPreview.length;

  hint.textContent = count === 0
    ? 'Nenhum lançamento identificado. Ajuste a área selecionada.'
    : `${count} lançamento${count > 1 ? 's' : ''} identificado${count > 1 ? 's' : ''}`;

  const wrap = document.getElementById('sig-table-wrap');
  wrap.innerHTML = '';

  const colsDesc = capturedColunas.filter(c => !c.is_anchor);
  const colTemplate = `58px ${colsDesc.map(() => '1fr').join(' ')} 72px`;

  const thead = document.createElement('div');
  thead.className = 'sig-thead';
  thead.style.gridTemplateColumns = colTemplate;

  const thData = document.createElement('div');
  thData.className = 'sig-th sig-th-anchor';
  thData.innerHTML = '<span class="sig-th-label">Data</span><span class="sig-campo-tag">âncora</span>';
  thead.appendChild(thData);

  colsDesc.forEach((col, i) => {
    const th = document.createElement('div');
    th.className = 'sig-th';

    const input = document.createElement('input');
    input.type        = 'text';
    input.className   = 'sig-campo-input';
    input.placeholder = 'Nomear…';
    input.dataset.colIdx = i;

    const hint2 = document.createElement('span');
    hint2.className = 'sig-th-hint';
    const ex = col.exemplo || '';
    hint2.textContent = ex.length > 14 ? ex.slice(0, 14) + '…' : ex;
    hint2.title = ex;

    th.appendChild(input);
    th.appendChild(hint2);
    thead.appendChild(th);
  });

  const thVal = document.createElement('div');
  thVal.className = 'sig-th sig-th-anchor';
  thVal.innerHTML = '<span class="sig-th-label">Valor</span><span class="sig-campo-tag">monetário</span>';
  thead.appendChild(thVal);

  wrap.appendChild(thead);

  if (count === 0) {
    const empty = document.createElement('div');
    empty.className = 'sig-preview-empty';
    empty.textContent = 'Nenhum lançamento detectado nesta área.';
    wrap.appendChild(empty);
  } else {
    let lastPage = null;
    capturedPreview.forEach(l => {
      if (l.page != null && l.page !== lastPage) {
        lastPage = l.page;
        const sep = document.createElement('div');
        sep.className = 'sig-td sig-td-page';
        sep.style.gridColumn = '1 / -1';
        sep.textContent = `p.${l.page}`;
        const sepRow = document.createElement('div');
        sepRow.className = 'sig-tr';
        sepRow.style.gridTemplateColumns = '1fr';
        sepRow.appendChild(sep);
        wrap.appendChild(sepRow);
      }

      const tr = document.createElement('div');
      tr.className = 'sig-tr';
      tr.style.gridTemplateColumns = colTemplate;

      const _dataVal = l.data || l.date || '';
      const tdDate = document.createElement('div');
      tdDate.className = 'sig-td sig-td-date';
      tdDate.textContent = _dataVal || '—';
      tdDate.title = _dataVal;
      tr.appendChild(tdDate);

      if (l.campos && Object.keys(l.campos).length > 0) {
        Object.values(l.campos).forEach(v => {
          const td = document.createElement('div');
          td.className = 'sig-td';
          td.textContent = v || '—';
          td.title = v || '';
          tr.appendChild(td);
        });
        const filled = Object.keys(l.campos).length;
        for (let i = filled; i < colsDesc.length; i++) {
          const td = document.createElement('div');
          td.className = 'sig-td';
          td.textContent = '—';
          tr.appendChild(td);
        }
      } else {
        const tdDescr = document.createElement('div');
        tdDescr.className = 'sig-td';
        tdDescr.style.gridColumn = colsDesc.length > 1
          ? `2 / ${1 + colsDesc.length}` : '';
        const descrText = l.descricao || '—';
        tdDescr.textContent = descrText;
        tdDescr.title = descrText;
        tr.appendChild(tdDescr);
        for (let i = 1; i < colsDesc.length; i++) {
          const ghost = document.createElement('div');
          ghost.className = 'sig-td';
          ghost.style.display = 'none';
          tr.appendChild(ghost);
        }
      }

      const tdVal = document.createElement('div');
      if (l.entrada != null && l.entrada !== 0) {
        tdVal.className = 'sig-td sig-td-val entrada';
        tdVal.textContent = `+${fmtBRL(l.entrada)}`;
      } else if (l.saida != null && l.saida !== 0) {
        tdVal.className = 'sig-td sig-td-val saida';
        tdVal.textContent = `-${fmtBRL(Math.abs(l.saida))}`;
      } else {
        tdVal.className = 'sig-td sig-td-val';
        tdVal.textContent = '—';
      }
      tr.appendChild(tdVal);

      wrap.appendChild(tr);
    });
  }

  _setSigBalloon('play');

  panel.removeAttribute('hidden');
  document.getElementById('sig-bank-name').value = '';
  const firstInput = wrap.querySelector('.sig-campo-input');
  (firstInput || document.getElementById('sig-bank-name')).focus();
}

function _setSigBalloon(state) {
  const balloon = document.getElementById('sig-balloon');
  const text    = document.getElementById('sig-balloon-text');
  const btn     = document.getElementById('sig-balloon-action');

  balloon.classList.toggle('sig-balloon-reset', state === 'reset');
  balloon.removeAttribute('hidden');

  if (state === 'play') {
    text.textContent = 'Clique para conferir os lançamentos selecionados direto no PDF.';
    btn.onclick = async () => {
      await _testSig();
      _setSigBalloon('reset');
    };
  } else {
    text.textContent = 'Quer refazer a seleção? Clique para voltar ao padrão original.';
    btn.onclick = () => document.getElementById('btn-sig-reset').click();
  }
}

async function _testSig() {
  const count = capturedPreview.length;

  if (!count) {
    toast('Nenhum lançamento detectado com esta seleção', 'error');
    return;
  }

  const firstPage = capturedPreview[0]?.page;
  if (firstPage != null) {
    const targetIdx = pdfPages.findIndex(p => p.page === firstPage);
    if (targetIdx !== -1 && targetIdx !== pdfCurrentIdx) {
      pdfCurrentIdx = targetIdx;
      updatePdfNav();
    }
  }

  await renderPdfPage(pdfCurrentIdx);
}

async function _saveSig() {
  const name = document.getElementById('sig-bank-name').value.trim();
  if (!name) { toast('Informe o nome do banco', 'error'); return; }
  if (!capturedSig) return;

  const colsParaNomear = capturedColunas.filter(c => !c.is_anchor);
  const campos = [];
  for (const input of document.querySelectorAll('#sig-table-wrap .sig-campo-input')) {
    const idx   = parseInt(input.dataset.colIdx, 10);
    const label = input.value.trim();
    if (label && colsParaNomear[idx]) {
      campos.push({
        label,
        x_min: colsParaNomear[idx].x_min,
        x_max: colsParaNomear[idx].x_max,
      });
    }
  }

  try {
    const saved = await api('POST', '/assinaturas', { ...capturedSig, bank_name: name, campos });
    toast(`Padrão do banco "${name}" salvo com sucesso`);

    await loadBancos();

    if (captureFileId) {
      const newKey = `user:${saved.id}`;
      const idx    = uploadedPdfs.findIndex(p => p.file_id === captureFileId);
      if (idx !== -1) uploadedPdfs[idx].banco = newKey;
    }

    document.getElementById('sig-panel').setAttribute('hidden', '');
    capturedSig     = null;
    capturedPreview = [];
    capturedColunas = [];
    _removeCaptureRect();
    toggleCaptureMode();
    closePdfEditor();
    renderUploadedFiles();

    if (captureOnSave) {
      const cb  = captureOnSave;
      captureOnSave = null;
      cb();
    }
  } catch (err) {
    toast(err.message || 'Erro ao salvar assinatura', 'error');
  }
}

