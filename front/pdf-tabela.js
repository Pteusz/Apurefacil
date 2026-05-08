
// ─── Helpers para tabela ──────────────────────────────────────────────────────
function _buildExcMap() {
  const map = {};
  (currentApuracao?.excluidos || []).forEach(e => {
    const key = `${e.data}|${Math.round(Math.abs(e.valor || 0) * 100)}`;
    if (!map[key]) map[key] = e.motivo;
  });
  return map;
}

const MOTIVO_LABEL = {
  circular                    : 'circular',
  circular_longitudinal       : 'circular',
  circular_longitudinal_manual: 'circular',
  duplicado_manual            : 'duplicado',
  variancia                   : 'variância',
  sem_historico               : 'sem histórico',
  auto_transferencia          : 'auto-transf.',
  flag_usuario                : 'ignorado',
};

const _MOTIVO_TO_DROPVAL = {
  variancia                   : 'ruido',
  sem_historico               : 'sem_historico',
  circular                    : 'renda_circular',
  circular_longitudinal       : 'renda_circular',
  circular_longitudinal_manual: 'renda_circular',
  duplicado_manual            : 'renda_duplicada',
  auto_transferencia          : 'auto_transferencia',
  auto_investimento           : 'auto_transferencia',
  flag_usuario                : 'ignorar',
};

function _getLancDecision(s, excMap) {
  const val  = s.valor ?? 0;
  const flag = s.flag || 'renda_recorrente';

  if (val < 0 || flag === 'despesa_recorrente') return { kind: 'saida' };

  if (flag === 'forcar_estavel' || flag === 'estavel') {
    return { kind: 'dropdown', dropVal: 'estavel', badge: 'forçado', badgeType: 'forced', suffix: 'operador' };
  }
  if (flag === 'ignorar') {
    return { kind: 'dropdown', dropVal: 'ignorar', badge: 'forçado', badgeType: 'forced', suffix: 'operador' };
  }
  if (flag === 'ruido') {
    return { kind: 'dropdown', dropVal: 'ruido', badge: 'forçado', badgeType: 'forced', suffix: 'operador' };
  }
  if (flag === 'sem_historico') {
    return { kind: 'dropdown', dropVal: 'sem_historico', badge: 'forçado', badgeType: 'forced', suffix: 'operador' };
  }
  if (flag === 'renda_circular') {
    return { kind: 'dropdown', dropVal: 'renda_circular', badge: 'forçado', badgeType: 'forced', suffix: 'operador' };
  }
  if (flag === 'renda_duplicada') {
    return { kind: 'dropdown', dropVal: 'renda_duplicada', badge: 'forçado', badgeType: 'forced', suffix: 'operador' };
  }
  if (flag === 'auto_transferencia') {
    return { kind: 'dropdown', dropVal: 'auto_transferencia', badge: 'auto-transf.', badgeType: 'algo', suffix: null };
  }

  const key    = `${s.data}|${Math.round(Math.abs(val) * 100)}`;
  const motivo = excMap[key];
  if (!motivo) {
    return { kind: 'dropdown', dropVal: 'estavel', badge: 'estável', badgeType: 'algo', suffix: null };
  }
  const dropVal = _MOTIVO_TO_DROPVAL[motivo] || 'estavel';
  return { kind: 'dropdown', dropVal, badge: MOTIVO_LABEL[motivo] || motivo, badgeType: 'algo', suffix: null };
}

function _getSessionColunas(session) {
  const meses = session.meses || {};
  for (const mes of Object.values(meses)) {
    for (const lanc of Object.values(mes)) {
      const campos = lanc.state?.campos;
      if (campos && Object.keys(campos).length > 0) {
        return Object.keys(campos);
      }
    }
  }
  return null;
}

function _fmtDate(d) {
  if (!d) return '—';
  const parts = d.split('-');
  if (parts.length === 3) return `${parts[2]}/${parts[1]}/${parts[0]}`;
  return d;
}

// ─── Tabela de lançamentos ────────────────────────────────────────────────────
function renderTransactions() {
  const tbody = document.getElementById('lanc-tbody');
  const thead = document.getElementById('lanc-thead');
  if (!currentSession) return;

  const colunas = _getSessionColunas(currentSession);
  const numDescCols = colunas ? colunas.length : 1;

  if (thead) {
    const descHeaders = colunas
      ? colunas.map((label, i) =>
          `<th class="col-desc${i === 0 ? ' col-desc-first' : ''}">${esc(label)}</th>`
        ).join('')
      : `<th class="col-desc col-desc-first">Descrição</th>`;
    thead.innerHTML = `<tr>
      <th class="col-check" title="Incluir na apuração">Incluir</th>
      <th class="col-data">Data</th>
      ${descHeaders}
      <th class="col-valor">Valor</th>
      <th class="col-acao">Ação</th>
    </tr>`;
  }

  let lancs = [];
  const meses = currentSession.meses || {};

  if (activeMonth) {
    lancs = meses[activeMonth] || [];
  } else {
    for (const ls of Object.values(meses)) lancs.push(...ls);
    lancs.sort((a, b) => (a.state.data || '').localeCompare(b.state.data || ''));
  }

  if (activeSource) {
    // Usa grupo_id como fonte de verdade quando o back o forneceu
    const fonteAtiva = knownFontes.find(f => f.pagador === activeSource);
    if (fonteAtiva?.grupo_id) {
      lancs = lancs.filter(l => l.state.grupo_id === fonteAtiva.grupo_id);
    } else {
      lancs = lancs.filter(l => _sourceMatches(l.state, activeSource));
    }
  }

  // ── Filtro por tipo de exclusão — usa grupo_id do back como fonte de verdade
  if (activeTypeFilter) {
    const grupoIdsDoTipo = new Set(
      knownFontes
        .filter(f => f.system_excluded &&
          (_MOTIVO_TO_TIPO[f.motivo_exclusao] || f.motivo_exclusao) === activeTypeFilter)
        .map(f => f.grupo_id)
        .filter(Boolean)
    );
    lancs = grupoIdsDoTipo.size > 0
      ? lancs.filter(l => grupoIdsDoTipo.has(l.state.grupo_id))
      : [];
  }

  if (lancs.length === 0) {
    tbody.innerHTML = `<tr><td colspan="${4 + numDescCols}"><div class="empty-state">Nenhum lançamento encontrado</div></td></tr>`;
    return;
  }

  const excMap = _buildExcMap();

  tbody.innerHTML = lancs.map(l => {
    const s       = l.state;
    const val     = s.valor ?? 0;
    const dec     = _getLancDecision(s, excMap);
    const isExcl  = !s.active;
    const dateStr = _fmtDate(s.data);

    let descCells;
    if (colunas) {
      const campos = s.campos || {};
      descCells = colunas.map((label, i) => {
        const v   = campos[label] ?? '—';
        const cls = i === 0
          ? 'col-desc col-desc-first td-text-ellipsis'
          : 'col-desc td-muted';
        return `<td class="${cls}" title="${esc(v)}">${esc(v)}</td>`;
      }).join('');
    } else {
      const v = s.descricao || '—';
      descCells = `<td class="col-desc col-desc-first td-text-ellipsis" title="${esc(v)}">${esc(v)}</td>`;
    }

    let acaoHtml;
    if (dec.kind === 'saida') {
      acaoHtml = `<div class="acao-cell"><span class="acao-saida">saída</span></div>`;
    } else {
      const badgeHtml = dec.badgeType === 'forced'
        ? `<span class="badge badge-forced" data-flag="${esc(dec.dropVal)}">${esc(dec.badge)}</span><span class="badge-operator-label">${esc(dec.suffix)}</span>`
        : `<span class="badge badge-algo" data-flag="${esc(dec.dropVal)}">${esc(dec.badge)}</span>`;
      acaoHtml = `
        <div class="acao-cell">
          <select class="acao-select" data-action="acao" data-id="${esc(l.lanc_id)}" data-exc-key="${esc(`${s.data}|${Math.round(Math.abs(val)*100)}`)}" data-algo-motivo="${esc(excMap[`${s.data}|${Math.round(Math.abs(val)*100)}`] || '')}">
            <option value="estavel"${dec.dropVal === 'estavel' ? ' selected' : ''}>Estável</option>
            <option value="ruido"${dec.dropVal === 'ruido' ? ' selected' : ''}>Ruído</option>
            <option value="sem_historico"${dec.dropVal === 'sem_historico' ? ' selected' : ''}>Sem histórico</option>
            <option value="auto_transferencia"${dec.dropVal === 'auto_transferencia' ? ' selected' : ''}>Auto-transf.</option>
            <option value="renda_circular"${dec.dropVal === 'renda_circular' ? ' selected' : ''}>Circular</option>
            <option value="renda_duplicada"${dec.dropVal === 'renda_duplicada' ? ' selected' : ''}>Duplicado</option>
            <option value="ignorar"${dec.dropVal === 'ignorar' ? ' selected' : ''}>Ignorar</option>
          </select>
          ${badgeHtml}
        </div>`;
    }

    return `
      <tr class="${isExcl ? 'lanc-excluded' : ''}" data-id="${esc(l.lanc_id)}">
        <td class="col-check">
          <input type="checkbox" ${s.active ? 'checked' : ''}
                 data-action="toggle" data-id="${esc(l.lanc_id)}" />
        </td>
        <td class="col-data td-muted">${esc(dateStr)}</td>
        ${descCells}
        <td class="col-valor ${val >= 0 ? 'td-pos' : 'td-neg'}">${val >= 0 ? '+' : ''}${fmtBRL(val)}</td>
        <td class="col-acao">${acaoHtml}</td>
      </tr>`;
  }).join('');

  tbody.querySelectorAll('[data-action="toggle"]').forEach(cb => {
    cb.addEventListener('change', () =>
      sendMutation('toggle_active', cb.dataset.id, { active: cb.checked })
    );
  });

  tbody.querySelectorAll('[data-action="acao"]').forEach(sel => {
    sel.addEventListener('change', () => {
      sendMutation('set_flag', sel.dataset.id, { flag: sel.value });
    });
  });

  tbody.querySelectorAll('.td-text-ellipsis').forEach(td => {
    td.addEventListener('dblclick', () => {
      const full = td.getAttribute('title') || td.textContent.trim();
      openDescModal(full);
    });
  });
}

function openDescModal(text) {
  const overlay = document.createElement('div');
  overlay.className = 'desc-modal-overlay';
  overlay.innerHTML = `
    <div class="desc-modal" role="dialog" aria-modal="true">
      <p class="desc-modal-body">${esc(text)}</p>
      <button class="btn-ghost btn-sm desc-modal-close">Fechar</button>
    </div>`;
  overlay.addEventListener('click', e => {
    if (e.target === overlay || e.target.classList.contains('desc-modal-close')) {
      overlay.remove();
    }
  });
  document.addEventListener('keydown', function handler(e) {
    if (e.key === 'Escape') { overlay.remove(); document.removeEventListener('keydown', handler); }
  }, { once: true });
  document.body.appendChild(overlay);
}

// ─── Export ───────────────────────────────────────────────────────────────────
function openExportModal()  { document.getElementById('export-modal').removeAttribute('hidden'); }
function closeExportModal() { document.getElementById('export-modal').setAttribute('hidden', ''); }

// ─── Export XLSX ──────────────────────────────────────────────────────────────
let xlsxLib = null;
async function loadXLSX() {
  if (xlsxLib) return;
  await new Promise((resolve, reject) => {
    if (window.XLSX) { xlsxLib = window.XLSX; resolve(); return; }
    const s    = document.createElement('script');
    s.src      = 'https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js';
    s.onload   = () => { xlsxLib = window.XLSX; resolve(); };
    s.onerror  = reject;
    document.head.appendChild(s);
  });
}

async function exportTabela() {
  if (!currentSession) return;
  if (!currentLaudo) { toast('Calcule a apuração antes de exportar', 'warn'); return; }
  try {
    await loadXLSX();
    const wb       = xlsxLib.utils.book_new();
    const nome     = currentSession.session_name || 'apuracao';
    const bancoMap = _xlsBuildBancoMap();

    xlsxLib.utils.book_append_sheet(wb, _xlsResumo(currentLaudo),            'Resumo da Apuração');
    xlsxLib.utils.book_append_sheet(wb, _xlsFontes(currentLaudo, bancoMap),  'Fontes de Renda');
    xlsxLib.utils.book_append_sheet(wb, _xlsEvolucao(currentLaudo),          'Evolução Mensal');

    xlsxLib.writeFile(wb, `${nome}_apuracao.xlsx`, { cellStyles: true });
    closeExportModal();
    toast('Planilha exportada com sucesso', 'success');
  } catch (e) {
    console.error(e);
    toast('Erro ao gerar planilha', 'error');
  }
}

// ─── Helpers de exportação XLSX ───────────────────────────────────────────────
const _XC = {
  navy:   '1B3A6B',
  blue:   '2563EB',
  altBg:  'EFF6FF',
  totBg:  'DBEAFE',
  lblBg:  'F1F5F9',
  white:  'FFFFFF',
  dark:   '1E293B',
};

function _xs(font, fillRgb, align, numFmt) {
  const s = {};
  if (font)    s.font      = font;
  if (fillRgb) s.fill      = { patternType: 'solid', fgColor: { rgb: fillRgb } };
  if (align)   s.alignment = align;
  if (numFmt)  s.numFmt    = numFmt;
  return s;
}

function _xlsSetRow(ws, rowIdx, styles) {
  styles.forEach((s, c) => {
    const addr = xlsxLib.utils.encode_cell({ r: rowIdx, c });
    if (!ws[addr]) ws[addr] = { v: '', t: 's' };
    ws[addr].s = s;
  });
}

function _xlsNorm(s) {
  return (s || '').toLowerCase()
    .normalize('NFD').replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9]/g, ' ').replace(/\s+/g, ' ').trim();
}

function _xlsBuildBancoMap() {
  const bancos = currentSession?.bancos_detectados || [];
  const map    = {};
  for (const lancs of Object.values(currentSession?.meses || {})) {
    for (const l of lancs) {
      const s    = l.state || {};
      const norm = _xlsNorm(s.descricao);
      if (norm && !map[norm]) {
        const fi      = s.bbox?.file_idx ?? s.file_idx ?? 0;
        const rawBanco = bancos[fi] || '';
        map[norm] = {
          banco: rawBanco.startsWith('user:')
            ? rawBanco.slice(5).replace(/_/g, ' ')
            : rawBanco ? rawBanco.charAt(0).toUpperCase() + rawBanco.slice(1) : '—',
          doc: s.campos?.cnpj || s.campos?.cpf || s.campos?.documento || '—',
        };
      }
    }
  }
  return map;
}

function _xlsResumo(laudo) {
  const cab  = laudo.cabecalho  || {};
  const res  = laudo.resumo     || {};
  const excl = laudo.exclusoes  || {};
  const tots = laudo.totais_por_mes || {};

  const mesForteMes  = res.mes_mais_forte;
  const mesFracoMes  = res.mes_mais_fraco;
  const forteLbl     = mesForteMes ? `Mês Mais Forte  (${fmtMonthLabel(mesForteMes)})` : 'Mês Mais Forte';
  const fracoLbl     = mesFracoMes ? `Mês Mais Fraco  (${fmtMonthLabel(mesFracoMes)})` : 'Mês Mais Fraco';
  const forteVal     = mesForteMes ? (tots[mesForteMes] ?? 0) : 0;
  const fracoVal     = mesFracoMes ? (tots[mesFracoMes] ?? 0) : 0;
  const rendaMedia   = res.renda_apurada_mensal ?? 0;

  const indicadores = [
    ['Renda Apurada / Mês',  rendaMedia,                                                         'currency'],
    ['Período Analisado',    cab.periodo ?? '—',                                                  'text'],
    ['Meses Analisados',     cab.meses_analisados ?? '—',                                         'int'],
    ['Fontes Ativas',        res.total_fontes ?? '—',                                             'int'],
    [forteLbl,               forteVal,                                                            'currency'],
    [fracoLbl,               fracoVal,                                                            'currency'],
    ['Média Mensal',         rendaMedia,                                                          'currency'],
    ['Total de Exclusões',   excl.total_excluidos ?? currentApuracao?.excluidos?.length ?? '—',   'int'],
  ];

  const rows = [
    ['Apurei · Laudo de Apuração de Renda', ''],
    [cab.titular || currentSession?.session_name || '—', ''],
    ['INDICADORES GERAIS', ''],
    ...indicadores.map(([l, v]) => [l, v]),
  ];

  const ws      = xlsxLib.utils.aoa_to_sheet(rows);
  ws['!cols']   = [{ wch: 34 }, { wch: 36 }];
  ws['!merges'] = [
    { s: { r: 0, c: 0 }, e: { r: 0, c: 1 } },
    { s: { r: 1, c: 0 }, e: { r: 1, c: 1 } },
    { s: { r: 2, c: 0 }, e: { r: 2, c: 1 } },
  ];

  const titleS = _xs({ bold: true, sz: 14, color: { rgb: _XC.white } }, _XC.navy, { horizontal: 'center', vertical: 'center' });
  const subS   = _xs({ bold: true, sz: 11, color: { rgb: _XC.blue  } }, _XC.lblBg, { horizontal: 'center' });
  const secS   = _xs({ bold: true, sz: 10, color: { rgb: _XC.white } }, _XC.blue);

  _xlsSetRow(ws, 0, [titleS, titleS]);
  _xlsSetRow(ws, 1, [subS,   subS]);
  _xlsSetRow(ws, 2, [secS,   secS]);

  indicadores.forEach(([, , fmt], i) => {
    const r     = i + 3;
    const isAlt = i % 2 === 1;
    const bg    = isAlt ? _XC.altBg : null;
    const lblS  = _xs({ bold: true, sz: 10, color: { rgb: _XC.dark } }, bg);
    const valS  = fmt === 'currency'
      ? _xs({ sz: 10, color: { rgb: _XC.dark } }, bg, { horizontal: 'right' }, '#,##0.00')
      : _xs({ sz: 10, color: { rgb: _XC.dark } }, bg, { horizontal: 'right' });
    _xlsSetRow(ws, r, [lblS, valS]);
  });

  ws['!ref'] = `A1:B${3 + indicadores.length}`;
  return ws;
}

function _xlsFontes(laudo, bancoMap) {
  const fontes = laudo.fontes || [];
  const total  = laudo.resumo?.renda_apurada_mensal ?? 0;
  const NCOLS  = 10;

  const HDRS = ['Fonte', 'CNPJ / CPF', 'Banco', 'Status',
                'Renda Média (R$)', 'Regularidade', 'Variabilidade',
                'Faixa Mín (R$)', 'Faixa Máx (R$)', '% da Renda'];

  const STATUS_COLOR = { estavel: '1A7A4A', forcado: 'B45309', sem_historico: '6B7280' };
  const statusLabel  = c => c === 'estavel' ? 'Estável' : c === 'forcado' ? 'Forçado' : c || '—';

  const dataRows = fontes.map(f => {
    const info = bancoMap[f.grupo_id] || bancoMap[_xlsNorm(f.pagador)] || {};
    return [
      f.pagador || '—',
      info.doc  || '—',
      info.banco || '—',
      statusLabel(f.classificacao),
      f.renda_base ?? 0,
      f.regularidade || '—',
      f.cv_pct != null ? f.cv_pct / 100 : '—',
      f.faixa_mensal?.min ?? 0,
      f.faixa_mensal?.max ?? 0,
      (f.participacao_pct ?? 0) / 100,
    ];
  });

  const rows = [
    ['Apurei · Fontes de Renda Apuradas', ...Array(NCOLS - 1).fill('')],
    HDRS,
    ...dataRows,
    ['TOTAL', '', '', '', total, '', '', '', '', 1],
  ];

  const ws = xlsxLib.utils.aoa_to_sheet(rows);
  ws['!cols'] = [
    { wch: 44 }, { wch: 18 }, { wch: 28 }, { wch: 12 },
    { wch: 16 }, { wch: 13 }, { wch: 14 },
    { wch: 14 }, { wch: 14 }, { wch: 11 },
  ];
  ws['!merges']     = [{ s: { r: 0, c: 0 }, e: { r: 0, c: NCOLS - 1 } }];
  ws['!autofilter'] = { ref: `A2:J2` };
  ws['!freeze']     = { xSplit: 0, ySplit: 2, topLeftCell: 'A3', activePane: 'bottomLeft' };

  const titleS = _xs({ bold: true, sz: 13, color: { rgb: _XC.white } }, _XC.navy);
  const hdrS   = _xs({ bold: true, sz: 10, color: { rgb: _XC.white } }, _XC.navy, { horizontal: 'center', wrapText: true });
  const hdrSL  = _xs({ bold: true, sz: 10, color: { rgb: _XC.white } }, _XC.navy);

  _xlsSetRow(ws, 0, Array(NCOLS).fill(titleS));
  _xlsSetRow(ws, 1, [hdrSL, ...Array(NCOLS - 1).fill(hdrS)]);

  dataRows.forEach((row, i) => {
    const r      = i + 2;
    const bg     = i % 2 === 1 ? _XC.altBg : null;
    const isCv   = typeof row[6] === 'number';
    const stCls  = fontes[i]?.classificacao;
    const stRgb  = STATUS_COLOR[stCls] || _XC.dark;
    _xlsSetRow(ws, r, [
      _xs({ sz: 10 }, bg),
      _xs({ sz: 10 }, bg, { horizontal: 'center' }),
      _xs({ sz: 10 }, bg),
      _xs({ bold: true, sz: 10, color: { rgb: stRgb } }, bg, { horizontal: 'center' }),
      _xs({ sz: 10 }, bg, { horizontal: 'right' }, '#,##0.00'),
      _xs({ sz: 10 }, bg, { horizontal: 'center' }),
      isCv ? _xs({ sz: 10 }, bg, { horizontal: 'center' }, '0.0%') : _xs({ sz: 10 }, bg, { horizontal: 'center' }),
      _xs({ sz: 10 }, bg, { horizontal: 'right' }, '#,##0.00'),
      _xs({ sz: 10 }, bg, { horizontal: 'right' }, '#,##0.00'),
      _xs({ sz: 10 }, bg, { horizontal: 'right' }, '0.0%'),
    ]);
  });

  const tR   = dataRows.length + 2;
  const totS = _xs({ bold: true, sz: 10 }, _XC.totBg);
  const totN = _xs({ bold: true, sz: 10 }, _XC.totBg, { horizontal: 'right' }, '#,##0.00');
  const totP = _xs({ bold: true, sz: 10 }, _XC.totBg, { horizontal: 'right' }, '0%');
  _xlsSetRow(ws, tR, [totS, totS, totS, totS, totN, totS, totS, totS, totS, totP]);

  ws['!ref'] = `A1:J${tR + 1}`;
  return ws;
}

function _xlsEvolucao(laudo) {
  const fontes = laudo.fontes || [];
  const tots   = laudo.totais_por_mes || {};
  const meses  = Object.keys(tots).sort();
  const NCOLS  = 1 + meses.length + 1;

  const mesLbls = meses.map(fmtMonthLabel);
  const HDRS    = ['Fonte de Renda', ...mesLbls, 'Média'];

  const dataRows = fontes.map(f => {
    const vpm  = f.valores_por_mes || {};
    const vals = meses.map(m => (vpm[m] > 0 ? vpm[m] : null));
    return [f.pagador || '—', ...vals, f.renda_base ?? 0];
  });

  const totalRow = ['TOTAL DO MÊS', ...meses.map(m => tots[m] ?? 0), laudo.resumo?.renda_apurada_mensal ?? 0];

  const rows = [
    ['Apurei · Evolução Mensal por Fonte de Renda', ...Array(NCOLS - 1).fill('')],
    HDRS,
    ...dataRows,
    totalRow,
  ];

  const ws = xlsxLib.utils.aoa_to_sheet(rows);
  ws['!cols']       = [{ wch: 44 }, ...meses.map(() => ({ wch: 11 })), { wch: 11 }];
  ws['!merges']     = [{ s: { r: 0, c: 0 }, e: { r: 0, c: NCOLS - 1 } }];
  ws['!autofilter'] = { ref: `A2:${xlsxLib.utils.encode_col(NCOLS - 1)}2` };
  ws['!freeze']     = { xSplit: 1, ySplit: 2, topLeftCell: 'B3', activePane: 'bottomRight' };

  const titleS = _xs({ bold: true, sz: 13, color: { rgb: _XC.white } }, _XC.navy);
  const hdrS   = _xs({ bold: true, sz: 10, color: { rgb: _XC.white } }, _XC.navy, { horizontal: 'center' });
  const hdrSL  = _xs({ bold: true, sz: 10, color: { rgb: _XC.white } }, _XC.navy);

  _xlsSetRow(ws, 0, Array(NCOLS).fill(titleS));
  _xlsSetRow(ws, 1, [hdrSL, ...Array(NCOLS - 1).fill(hdrS)]);

  dataRows.forEach((row, i) => {
    const r   = i + 2;
    const bg  = i % 2 === 1 ? _XC.altBg : null;
    const nameS = _xs({ sz: 10 }, bg);
    const numS  = _xs({ sz: 10 }, bg, { horizontal: 'right' }, '#,##0.00');
    const emtS  = _xs({ sz: 10 }, bg);
    for (let c = 0; c < NCOLS; c++) {
      const addr = xlsxLib.utils.encode_cell({ r, c });
      if (!ws[addr]) ws[addr] = { v: '', t: 's' };
      ws[addr].s = c === 0 ? nameS : (row[c] != null ? numS : emtS);
    }
  });

  const tR   = dataRows.length + 2;
  const totS = _xs({ bold: true, sz: 10 }, _XC.totBg);
  const totN = _xs({ bold: true, sz: 10 }, _XC.totBg, { horizontal: 'right' }, '#,##0.00');
  _xlsSetRow(ws, tR, [totS, ...Array(NCOLS - 1).fill(totN)]);

  ws['!ref'] = `A1:${xlsxLib.utils.encode_cell({ r: tR, c: NCOLS - 1 })}`;
  return ws;
}

// ─── Export PDF (laudo) ───────────────────────────────────────────────────────
let jsPDFLib = null;
async function loadJsPDF() {
  if (jsPDFLib) return;
  await new Promise((resolve, reject) => {
    if (window.jspdf) { jsPDFLib = window.jspdf.jsPDF; resolve(); return; }
    const s   = document.createElement('script');
    s.src     = 'https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js';
    s.onload  = () => { jsPDFLib = window.jspdf.jsPDF; resolve(); };
    s.onerror = reject;
    document.head.appendChild(s);
  });
}

async function exportLaudo() {
  if (!currentSession || !currentApuracao) return;
  try {
    await loadJsPDF();
    const laudo = await api('GET', `/sessao/${currentSession.session_id}/laudo`);

    const doc = new jsPDFLib({ unit: 'mm', format: 'a4' });
    const W = 210, ML = 14, MR = 14, CW = W - ML - MR;
    let y = 0;

    const SOURCE_COLORS = [
      [66, 133, 244], [52, 168, 83], [218, 155, 20], [155, 89, 182],
      [229, 57, 53], [0, 172, 193], [230, 126, 34], [84, 110, 122],
    ];

    const checkPage = (needed = 10) => {
      if (y + needed > 282) { doc.addPage(); y = 14; }
    };

    const cab = laudo.cabecalho || {};
    const res = laudo.resumo || {};
    const totais = laudo.totais_por_mes || {};
    const totVals = Object.values(totais).filter(v => v > 0);

    // ── BARRA DE CABEÇALHO ────────────────────────────────────────────────────
    doc.setFillColor(248, 249, 250);
    doc.rect(0, 0, W, 24, 'F');
    doc.setDrawColor(220, 222, 226);
    doc.line(0, 24, W, 24);

    doc.setFontSize(9);
    doc.setFont(undefined, 'bold');
    doc.setTextColor(40, 40, 40);
    doc.text('Apurei', ML, 8);
    doc.setFontSize(8);
    doc.setFont(undefined, 'normal');
    doc.setTextColor(150);
    doc.text('  |  Laudo de Apuração de Renda', ML + 18, 8);

    doc.setFontSize(6.5);
    doc.setFont(undefined, 'normal');
    doc.setTextColor(130);
    doc.text('RENDA APURADA/MÊS', W - MR, 6, { align: 'right' });
    doc.setFontSize(15);
    doc.setFont(undefined, 'bold');
    doc.setTextColor(25, 25, 25);
    doc.text(fmtBRL(res.renda_apurada_mensal || 0), W - MR, 14, { align: 'right' });

    const sessId = cab.session_id || currentSession.session_id || '';
    if (sessId) {
      doc.setFontSize(6.5);
      doc.setFont(undefined, 'normal');
      doc.setTextColor(170);
      doc.text(`EST-${String(sessId).toUpperCase()}`, W - MR, 20.5, { align: 'right' });
    }

    y = 32;

    // ── TITULAR ───────────────────────────────────────────────────────────────
    doc.setFontSize(20);
    doc.setFont(undefined, 'bold');
    doc.setTextColor(20, 20, 20);
    doc.text(cab.titular || '—', ML, y);
    y += 9;

    doc.setFontSize(8);
    doc.setFont(undefined, 'normal');
    doc.setTextColor(120);
    const mesesN = res.meses_analisados || Object.keys(totais).length || 0;
    const fontesN = res.total_fontes ?? 0;
    doc.text(
      `${cab.periodo || '—'}    ${mesesN} meses analisados    ${fontesN} fontes de renda`,
      ML, y
    );
    y += 10;

    if (cab.baixa_confianca) {
      checkPage(14);
      doc.setFillColor(255, 243, 205);
      doc.setDrawColor(251, 188, 5);
      doc.roundedRect(ML, y, CW, 11, 1.5, 1.5, 'FD');
      doc.setFontSize(7.5);
      doc.setFont(undefined, 'bold');
      doc.setTextColor(150, 90, 0);
      doc.text(
        '\u26a0  Período insuficiente — menos de 3 meses. Resultado com baixa confiança.',
        ML + 4, y + 7
      );
      doc.setTextColor(0);
      y += 16;
    }

    // ── 4 CARDS DE RESUMO ─────────────────────────────────────────────────────
    checkPage(22);
    const cW4 = (CW - 6) / 4, cH = 18;
    const cardItems = [
      { label: 'FONTES ATIVAS',   value: String(fontesN) },
      { label: 'MÊS MAIS FORTE', value: totVals.length ? fmtBRL(Math.max(...totVals)) : '—' },
      { label: 'MÊS MAIS FRACO', value: totVals.length ? fmtBRL(Math.min(...totVals)) : '—' },
      { label: 'MÉDIA MENSAL',   value: fmtBRL(res.renda_apurada_mensal || 0) },
    ];
    for (let i = 0; i < 4; i++) {
      const cx = ML + i * (cW4 + 2);
      doc.setFillColor(244, 245, 248);
      doc.setDrawColor(225, 227, 232);
      doc.roundedRect(cx, y, cW4, cH, 2, 2, 'FD');
      doc.setFontSize(6);
      doc.setFont(undefined, 'normal');
      doc.setTextColor(145);
      doc.text(cardItems[i].label, cx + cW4 / 2, y + 6, { align: 'center' });
      doc.setFontSize(10.5);
      doc.setFont(undefined, 'bold');
      doc.setTextColor(25, 25, 25);
      doc.text(cardItems[i].value, cx + cW4 / 2, y + 13.5, { align: 'center' });
    }
    y += cH + 10;

    // ── FONTES DE RENDA ───────────────────────────────────────────────────────
    if (laudo.fontes?.length) {
      doc.setFontSize(7);
      doc.setFont(undefined, 'bold');
      doc.setTextColor(155);
      doc.text('FONTES DE RENDA', ML, y);
      y += 6;

      for (let idx = 0; idx < laudo.fontes.length; idx++) {
        const fonte = laudo.fontes[idx];
        const cardH = 26;
        checkPage(cardH + 3);

        const clr = SOURCE_COLORS[idx % SOURCE_COLORS.length];
        const isEstavel = fonte.classificacao === 'estavel' || fonte.classificacao === 'forcado';

        doc.setFillColor(252, 252, 254);
        doc.setDrawColor(225, 227, 232);
        doc.roundedRect(ML, y, CW, cardH, 1.5, 1.5, 'FD');

        doc.setFillColor(...clr);
        doc.roundedRect(ML, y, 3.5, cardH, 1.5, 1.5, 'F');
        doc.rect(ML + 1.75, y, 1.75, cardH, 'F');

        const pagador = fonte.pagador || '—';
        const badgeX = W - MR - 20 - 28;
        const maxNameX = badgeX - 4;

        doc.setFontSize(9.5);
        doc.setFont(undefined, 'bold');
        doc.setTextColor(...clr);
        let displayPagador = pagador;
        if (doc.getTextWidth(displayPagador) > maxNameX - (ML + 8)) {
          while (displayPagador.length > 4 && doc.getTextWidth(displayPagador + '…') > maxNameX - (ML + 8)) {
            displayPagador = displayPagador.slice(0, -1);
          }
          displayPagador += '…';
        }
        doc.text(displayPagador, ML + 8, y + 7.5);

        if (fonte.banco) {
          const nameW = doc.getTextWidth(displayPagador);
          const viaText = `via ${fonte.banco}`;
          doc.setFontSize(7.5);
          doc.setFont(undefined, 'normal');
          const viaW = doc.getTextWidth(viaText);
          if (ML + 8 + nameW + 2 + viaW < maxNameX) {
            doc.setTextColor(160);
            doc.text(viaText, ML + 8 + nameW + 2, y + 7.5);
          }
        }

        const badgeW = 20, badgeH = 6;
        doc.setFillColor(...(isEstavel ? [209, 243, 220] : [225, 225, 240]));
        doc.roundedRect(badgeX, y + 3, badgeW, badgeH, 1, 1, 'F');
        doc.setFontSize(6.5);
        doc.setFont(undefined, 'bold');
        doc.setTextColor(...(isEstavel ? [34, 110, 60] : [70, 70, 120]));
        doc.text('Estável', badgeX + badgeW / 2, y + 7.2, { align: 'center' });

        doc.setFontSize(10);
        doc.setFont(undefined, 'bold');
        doc.setTextColor(20, 20, 20);
        doc.text(fmtBRL(fonte.renda_base || 0), W - MR - 3, y + 7.5, { align: 'right' });

        const pct = fonte.participacao_pct ?? 0;
        doc.setFontSize(7);
        doc.setFont(undefined, 'normal');
        doc.setTextColor(140);
        doc.text(`${pct}%`, W - MR - 3, y + 13, { align: 'right' });

        const barX = ML + 8, barY = y + 14.5, barW = CW - 14, barH2 = 2;
        doc.setFillColor(218, 220, 228);
        doc.roundedRect(barX, barY, barW, barH2, 1, 1, 'F');
        const fillW = Math.max(barW * Math.min(pct, 100) / 100, 0.5);
        doc.setFillColor(...clr);
        doc.roundedRect(barX, barY, fillW, barH2, 1, 1, 'F');

        doc.setFontSize(7);
        doc.setFont(undefined, 'normal');
        doc.setTextColor(125);
        const reg = fonte.regularidade || '—';
        const cv = fonte.cv_pct ?? 0;
        const fxMin = fmtBRL(fonte.faixa_mensal?.min ?? 0);
        const fxMax = fmtBRL(fonte.faixa_mensal?.max ?? 0);
        doc.text(
          `Regularidade ${reg}    Variabilidade ${cv}%    Faixa ${fxMin} – ${fxMax}`,
          ML + 8, y + 22
        );

        y += cardH + 3;
      }
    }

    // ── EVOLUÇÃO MENSAL ───────────────────────────────────────────────────────
    if (Object.keys(totais).length) {
      checkPage(20);
      y += 4;
      doc.setFontSize(7);
      doc.setFont(undefined, 'bold');
      doc.setTextColor(155);
      doc.text('EVOLUÇÃO MENSAL', ML, y);
      y += 6;

      const entries = Object.entries(totais).sort();
      const maxV = entries.reduce((m, [, v]) => Math.max(m, v), 0);
      const cols = 3, colW2 = (CW - (cols - 1) * 3) / cols, rowH = 11;

      for (let row = 0; row < Math.ceil(entries.length / cols); row++) {
        checkPage(rowH + 3);
        for (let col = 0; col < cols; col++) {
          const i = row * cols + col;
          if (i >= entries.length) break;
          const [mes, val] = entries[i];
          const bx = ML + col * (colW2 + 3);
          const ratio = maxV > 0 ? val / maxV : 0;
          const isHigh = val === maxV;

          doc.setFillColor(233, 236, 244);
          doc.roundedRect(bx, y, colW2, rowH, 1, 1, 'F');

          doc.setFillColor(...(isHigh ? [46, 160, 90] : [88, 140, 220]));
          doc.roundedRect(bx, y, Math.max(colW2 * ratio, 1.5), rowH, 1, 1, 'F');

          doc.setFontSize(6.5);
          doc.setFont(undefined, 'bold');
          doc.setTextColor(255, 255, 255);
          doc.text(fmtMonthLabel(mes), bx + 2.5, y + 4.5);

          doc.setFontSize(6.5);
          doc.setFont(undefined, 'normal');
          doc.text(fmtBRL(val), bx + 2.5, y + 8.8);
        }
        y += rowH + 2;
      }
      y += 6;
    }

    // ── EXCLUSÕES ─────────────────────────────────────────────────────────────
    const excl = laudo.exclusoes;
    if (excl && excl.total_excluidos > 0) {
      checkPage(30);
      doc.setFontSize(7);
      doc.setFont(undefined, 'bold');
      doc.setTextColor(155);
      doc.text('EXCLUSÕES', ML, y);
      y += 6;

      const byMotivo = excl.por_motivo || {};
      const exBoxes = [
        { label: 'Lançamentos excluídos', val: excl.total_excluidos },
        { label: 'Marcados pelo usuário', val: byMotivo.flag_usuario  ?? 0 },
        { label: 'Por variância',         val: byMotivo.variancia     ?? 0 },
        { label: 'Sem histórico',         val: byMotivo.sem_historico ?? 0 },
      ];
      const eW = (CW - 6) / 4, eH = 18;
      for (let i = 0; i < 4; i++) {
        const ex = ML + i * (eW + 2);
        doc.setFillColor(247, 248, 250);
        doc.setDrawColor(225, 227, 232);
        doc.roundedRect(ex, y, eW, eH, 1.5, 1.5, 'FD');
        doc.setFontSize(13);
        doc.setFont(undefined, 'bold');
        doc.setTextColor(30, 30, 30);
        doc.text(String(exBoxes[i].val), ex + eW / 2, y + 9.5, { align: 'center' });
        doc.setFontSize(6);
        doc.setFont(undefined, 'normal');
        doc.setTextColor(135);
        doc.text(exBoxes[i].label, ex + eW / 2, y + 14.5, { align: 'center' });
      }
      y += eH + 10;
    }

    // ── FONTES INCONCLUSIVAS ──────────────────────────────────────────────────
    if (laudo.inconclusivos?.length) {
      checkPage(20);
      doc.setFontSize(7);
      doc.setFont(undefined, 'bold');
      doc.setTextColor(155);
      doc.text('FONTES INCONCLUSIVAS — presença irregular, não compõem a renda apurada', ML, y);
      y += 7;

      for (const inc of laudo.inconclusivos) {
        checkPage(8);
        const ap = inc.aparicoes ?? 0;
        doc.setFontSize(8);
        doc.setFont(undefined, 'normal');
        doc.setTextColor(66, 120, 200);
        doc.text(inc.pagador || '—', ML + 3, y);
        doc.setTextColor(135);
        doc.text(`${ap} ${ap !== 1 ? 'aparições' : 'aparição'}`, W - MR - 3, y, { align: 'right' });
        doc.setDrawColor(232, 234, 238);
        doc.line(ML + 3, y + 2.5, W - MR, y + 2.5);
        y += 8;
      }
    }

    doc.save(`${currentSession.session_name || 'apuracao'}_laudo.pdf`);
    closeExportModal();
    toast('Laudo exportado', 'success');
  } catch (e) {
    console.error(e);
    toast('Erro ao gerar laudo', 'error');
  }
}

