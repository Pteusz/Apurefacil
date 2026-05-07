
// ─── Auth helpers ─────────────────────────────────────────────────────────────
async function tryLoadUser() {
  if (!getToken()) return null;
  try {
    return await api('GET', '/auth/me');
  } catch {
    clearToken();
    return null;
  }
}

async function onLoginSuccess(result) {
  setToken(result.token);
  currentUser = result.user ?? result;
  hideModal();
  renderUser(currentUser);
  loadBancos();
  try {
    sessions = await api('GET', '/sessao');
    renderSessions(sessions);
  } catch { /* silent */ }
  toast('Bem-vindo!', 'success');
}

// ─── Render do usuário ────────────────────────────────────────────────────────
function renderUser(user) {
  const name    = user ? (user.nome || user.email || '—') : '—';
  const initial = name === '—' ? '?' : name.charAt(0).toUpperCase();
  const plan    = user
    ? `${capitalize(user.plano || 'trial')} · ${user.creditos ?? 0} créditos`
    : '—';
  const first = user
    ? (user.nome?.split(' ')[0] || user.email?.split('@')[0] || 'você').toUpperCase()
    : 'você';

  document.getElementById('user-avatar').textContent       = initial;
  document.getElementById('user-display-name').textContent = name;
  document.getElementById('user-plan-label').textContent   = plan;

  const fn = GREETINGS[Math.floor(Math.random() * GREETINGS.length)];
  document.getElementById('main-greeting').innerHTML = fn(first);
}

function renderSessions(list) {
  const el = document.getElementById('session-list');
  if (!list || list.length === 0) {
    el.innerHTML = '<div class="session-list-empty">Nenhuma apuração ainda</div>';
    return;
  }
  el.innerHTML = list.map(s => `
    <div class="session-item" data-id="${esc(s.session_id)}" data-pinned="${s.pinned ? '1' : '0'}">
      <div class="session-item-main">
        <span class="session-name">${esc(s.session_name)}</span>
        <span class="session-date">${fmtDate(s.created_at)}</span>
      </div>
      <button class="session-menu-btn" data-id="${esc(s.session_id)}"
              aria-label="Opções da apuração" title="Opções">···</button>
    </div>
  `).join('');

  el.querySelectorAll('.session-item').forEach(item => {
    item.addEventListener('click', e => {
      if (e.target.closest('.session-menu-btn')) return;
      selectSession(item.dataset.id);
    });
  });

  el.querySelectorAll('.session-menu-btn').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const item = btn.closest('.session-item');
      openSessionMenu(btn.dataset.id, item.dataset.pinned === '1', btn);
    });
  });
}

// ─── Modal de autenticação ────────────────────────────────────────────────────
function showModal(step = 'magic') {
  setStep(step);
  document.getElementById('auth-modal').removeAttribute('hidden');
  setTimeout(() => {
    const focus = { magic: 'auth-email', password: 'auth-email-pw', sent: null };
    const id = focus[step];
    if (id) document.getElementById(id)?.focus();
  }, 120);
}

function hideModal() {
  document.getElementById('auth-modal').setAttribute('hidden', '');
}

function setStep(step) {
  document.getElementById('step-magic').hidden    = step !== 'magic';
  document.getElementById('step-password').hidden = step !== 'password';
  document.getElementById('step-sent').hidden     = step !== 'sent';
  clearMsg('magic-msg');
  clearMsg('pw-msg');
}

function setMsg(id, text, type = 'error') {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className   = `modal-msg ${type}`;
}

function clearMsg(id) {
  const el = document.getElementById(id);
  el.textContent = '';
  el.className   = 'modal-msg';
}

// ─── Views ────────────────────────────────────────────────────────────────────
function showFormView() {
  document.getElementById('main-center').removeAttribute('hidden');
  document.getElementById('result-view').setAttribute('hidden', '');
  document.getElementById('result-topbar').setAttribute('hidden', '');
  document.getElementById('proc-view').setAttribute('hidden', '');
}

function showResultView() {
  document.getElementById('main-center').setAttribute('hidden', '');
  document.getElementById('result-view').removeAttribute('hidden');
  document.getElementById('result-topbar').removeAttribute('hidden');
  document.getElementById('proc-view').setAttribute('hidden', '');
}

function showProcView() {
  document.getElementById('main-center').setAttribute('hidden', '');
  document.getElementById('result-view').setAttribute('hidden', '');
  document.getElementById('result-topbar').setAttribute('hidden', '');
  document.getElementById('proc-view').removeAttribute('hidden');
}

// ─── Sessão ───────────────────────────────────────────────────────────────────
async function selectSession(id) {
  document.querySelectorAll('.session-item').forEach(el => {
    el.classList.toggle('active', el.dataset.id === id);
  });

  try {
    const estado = await api('GET', `/sessao/${id}/estado`);
    currentSession  = estado.sessao;
    currentApuracao = estado.apuracao;
    currentLaudo    = estado.laudo;
    activeMonth      = null;
    activeSource     = null;
    gruposActiveTab  = 'composicao';
    knownFontes      = [];
    sourcesExpanded  = false;
    sessionEditorOpened.clear();
    renderResultView();
    showResultView();
  } catch (e) {
    toast(e.message || 'Erro ao carregar sessão', 'error');
  }
}

// ─── Render resultado ─────────────────────────────────────────────────────────
function renderResultView() {
  document.getElementById('result-name').textContent = currentSession.session_name || '—';

  const warn = document.getElementById('result-warning');
  if (currentLaudo?.cabecalho?.baixa_confianca) warn.removeAttribute('hidden');
  else warn.setAttribute('hidden', '');

  const badgesEl = document.getElementById('banco-badges');
  badgesEl.innerHTML = '';
  const dets = currentSession.bancos_detectados || [];
  const shown = new Set();
  dets.forEach(k => {
    if (!k || shown.has(k)) return;
    shown.add(k);
    const entry  = bancosDisponiveis.find(b => b.key === k);
    const label  = entry ? entry.label : k;
    const badge  = document.createElement('span');
    badge.className = entry ? 'banco-badge' : 'banco-badge banco-badge-unknown';
    badge.setAttribute('data-banco', k.toLowerCase());
    badge.textContent = label;
    badgesEl.appendChild(badge);
  });

  document.getElementById('btn-edit-pdf').setAttribute('hidden', '');

  document.getElementById('btn-ver-analise').onclick = () =>
    toast('Análise detalhada em breve', 'info');

  renderMetrics();
  renderMonthChips();
  renderSourceCards();
  renderFilterBar();
  renderTransactions();
}

function renderMetrics() {
  const l = currentLaudo;
  if (!l) return;

  document.getElementById('metric-renda').textContent = fmtBRL(l.resumo?.renda_apurada_mensal);
  document.getElementById('metric-meses').textContent = l.cabecalho?.meses_analisados ?? '—';
  document.getElementById('metric-periodo').textContent = l.cabecalho?.periodo ?? '—';

  const concPct  = l.resumo?.concentracao_principal;
  const topFonte = l.fontes?.[0]?.pagador ?? '—';
  document.getElementById('metric-conc').textContent = concPct != null ? `${Math.round(concPct)}%` : '—';
  const concFonteEl = document.getElementById('metric-conc-fonte');
  const MAX_FONTE = 48;
  if (topFonte.length > MAX_FONTE) {
    const short = topFonte.slice(0, MAX_FONTE).trimEnd() + '…';
    concFonteEl.innerHTML =
      `<span class="metric-sub-truncated" data-full="${esc(topFonte)}" data-short="${esc(short)}">${esc(short)}</span>` +
      `<button class="metric-ver-mais" type="button">ver mais</button>`;
    concFonteEl.querySelector('.metric-ver-mais').addEventListener('click', function() {
      const span = concFonteEl.querySelector('.metric-sub-truncated');
      const expanded = span.dataset.expanded === '1';
      if (expanded) {
        span.textContent = span.dataset.short;
        this.textContent = 'ver mais';
        span.dataset.expanded = '0';
      } else {
        span.textContent = span.dataset.full;
        this.textContent = 'ver menos';
        span.dataset.expanded = '1';
      }
    });
  } else {
    concFonteEl.textContent = topFonte;
  }

  const mesFraco = l.resumo?.mes_mais_fraco;
  const valFraco = mesFraco ? (l.totais_por_mes?.[mesFraco] ?? 0) : null;
  document.getElementById('metric-fraco').textContent     = valFraco != null ? fmtBRL(valFraco) : '—';
  document.getElementById('metric-fraco-mes').textContent = mesFraco ? fmtMonthLabel(mesFraco) : '—';
}

function renderMonthChips() {
  const wrap = document.getElementById('month-chips');
  const l    = currentLaudo;
  if (!l) { wrap.innerHTML = ''; return; }

  const totais   = l.totais_por_mes || {};
  const meses    = Object.keys(totais).sort();
  const mesFraco = l.resumo?.mes_mais_fraco;

  if (meses.length === 0) {
    wrap.innerHTML = '<span style="color:var(--color-text-muted);font-size:13px">Nenhum mês encontrado</span>';
    return;
  }

  const byYear = {};
  meses.forEach(m => {
    const yr = m.slice(0, 4);
    if (!byYear[yr]) byYear[yr] = [];
    byYear[yr].push(m);
  });

  wrap.innerHTML = Object.entries(byYear).map(([yr, months]) => {
    const cards = months.map(m => {
      const isSelected = m === activeMonth;
      const isFraco    = m === mesFraco;
      const totalStr   = fmtBRL(totais[m] ?? 0);

      let cls = 'month-card';
      if (isFraco)    cls += ' fraco';
      if (isSelected) cls += ' active';

      const editPill = isSelected
        ? `<span class="month-chip-edit-pill" data-edit-month="${esc(m)}">✎ Editar PDF</span>`
        : '';

      return `
        <div class="${cls}" data-month="${esc(m)}">
          <span class="month-card-label">${fmtMonthShort(m)}</span>
          <span class="month-card-total">${totalStr}</span>
          ${editPill}
        </div>`;
    }).join('');

    return `<div class="month-year-group"><span class="month-year-label">${esc(yr)}</span><div class="month-year-chips">${cards}</div></div>`;
  }).join('');

  wrap.querySelectorAll('.month-card').forEach(card => {
    card.addEventListener('click', e => {
      if (e.target.closest('.month-chip-edit-pill')) {
        const m = e.target.closest('.month-chip-edit-pill').dataset.editMonth;
        if (m) openPdfEditor(m, 'edit');
        return;
      }
      const m = card.dataset.month;
      activeMonth = (activeMonth === m) ? null : m;
      renderMonthChips();
      renderFilterBar();
      renderTransactions();
    });
  });
}

function _mergeFontes(fontes) {
  for (const f of fontes) {
    const idx = knownFontes.findIndex(k => k.pagador === f.pagador);
    if (idx >= 0) {
      knownFontes[idx] = { ...knownFontes[idx], ...f };
    } else {
      knownFontes.push({ ...f });
    }
  }
}

function _grupoIsActive(pagador) {
  const meses = currentSession?.meses || {};
  let total = 0, ativos = 0;
  for (const lancs of Object.values(meses)) {
    for (const l of Object.values(lancs)) {
      const s = l.state;
      if ((s.valor ?? 0) < 0) continue;
      if (!_sourceMatches(s, pagador)) continue;
      total++;
      if (s.active) ativos++;
    }
  }
  return total > 0 && ativos === total;
}

function _sourceMatches(s, pagador) {
  if (!pagador) return true;
  const desc = (s.descricao || '').toLowerCase();
  const pag  = pagador.toLowerCase();
  const campos = s.campos;
  if (campos) {
    const keys = Object.keys(campos);
    const lkeys = keys.map(k => k.toLowerCase());
    const oIdx  = lkeys.findIndex(k => k.includes('origem') || k.includes('pagador') || k.includes('descri'));
    const orVal = oIdx >= 0 ? (campos[keys[oIdx]] || '').toLowerCase() : '';
    if (orVal && orVal.includes(pag)) return true;
    if (orVal && pag.includes(orVal)) return true;
  }
  return desc.includes(pag) || pag.includes(desc);
}

function renderSourceCards() {
  const wrap = document.getElementById('source-cards');
  if (!currentSession) { wrap.innerHTML = ''; return; }

  _mergeFontes(currentLaudo?.fontes || []);

  if (knownFontes.length === 0) { wrap.innerHTML = ''; return; }

  wrap.innerHTML = knownFontes.map((f) => {
    const isSelected  = activeSource === f.pagador;
    const grupoAtivo  = _grupoIsActive(f.pagador);
    const barW        = Math.min(Math.max(f.participacao_pct || 0, 0), 100).toFixed(1);
    const toggleLabel = grupoAtivo ? 'Desativar' : 'Ativar';
    const toggleTitle = grupoAtivo
      ? 'Desativar todos os lançamentos deste grupo'
      : 'Ativar todos os lançamentos deste grupo';
    const cv = f.cv_pct != null ? Math.round(f.cv_pct) : null;
    const cvClass  = cv === null ? 'cv-badge--unknown' : cv < 50 ? 'cv-badge--low' : cv <= 80 ? 'cv-badge--mid' : 'cv-badge--high';
    const barClass = cv === null ? '' : cv < 50 ? 'source-card-bar--low' : cv <= 80 ? 'source-card-bar--mid' : 'source-card-bar--high';
    return `
      <div class="source-card${isSelected ? ' active' : ''}${grupoAtivo ? '' : ' source-card-disabled'}" data-source="${esc(f.pagador)}">
        <div class="source-card-header">
          <span class="source-card-name" title="${esc(f.pagador)}">${esc(f.pagador)}</span>
          <button class="source-card-toggle${grupoAtivo ? '' : ' source-card-toggle-off'}" data-action="toggle-grupo" data-grupo-id="${esc(f.grupo_id || '')}" data-pagador="${esc(f.pagador || '')}" data-active="${grupoAtivo ? '0' : '1'}" title="${toggleTitle}">${toggleLabel}</button>
        </div>
        <span class="source-card-val">${fmtBRL(f.renda_base)}</span>
        <div class="source-card-meta">
          <span class="source-card-regularidade">${f.regularidade ?? ''}</span>
          <span class="cv-badge ${cvClass}" title="Variabilidade">${cv !== null ? cv + '%' : '?'}</span>
        </div>
        <div class="source-card-bar-wrap">
          <div class="source-card-bar ${barClass}" style="width:${barW}%"></div>
        </div>
      </div>
    `;
  }).join('');

  wrap.querySelectorAll('.source-card').forEach(card => {
    card.addEventListener('click', e => {
      if (e.target.closest('[data-action="toggle-grupo"]')) return;
      const src = card.dataset.source;
      activeSource = (activeSource === src) ? null : src;
      renderSourceCards();
      renderFilterBar();
      renderTransactions();
    });
  });

  wrap.querySelectorAll('[data-action="toggle-grupo"]').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      const grupo_id = btn.dataset.grupoId;
      const pagador  = btn.dataset.pagador;
      const active   = btn.dataset.active === '1';

      // Optimistic update: aplica o toggle localmente antes da resposta do servidor.
      // O card responde imediatamente; métricas e chips esperam o servidor.
      if (currentSession?.meses) {
        for (const lancs of Object.values(currentSession.meses)) {
          for (const lanc of Object.values(lancs)) {
            const s = lanc.state;
            if ((s.valor ?? 0) < 0) continue;
            if (_sourceMatches(s, pagador)) s.active = active;
          }
        }
        renderSourceCards();
      }

      sendMutation('toggle_grupo', null, { grupo_id, active });
    });
  });

  wrap.parentNode.querySelector('.source-ver-mais-btn')?.remove();
  requestAnimationFrame(() => {
    const cards = [...wrap.querySelectorAll('.source-card')];
    if (cards.length === 0) return;
    wrap.parentNode.querySelector('.source-ver-mais-btn')?.remove();

    const firstTop = cards[0].getBoundingClientRect().top;
    const hasMultipleRows = cards.some(c => c.getBoundingClientRect().top > firstTop + 4);

    if (!hasMultipleRows) {
      wrap.style.maxHeight = '';
      wrap.style.overflow  = '';
      return;
    }

    if (!sourcesExpanded) {
      const firstRowCards = cards.filter(c => c.getBoundingClientRect().top <= firstTop + 4);
      const lastFirstRowCard = firstRowCards[firstRowCards.length - 1];
      const wrapTop = wrap.getBoundingClientRect().top;
      wrap.style.maxHeight = (lastFirstRowCard.getBoundingClientRect().bottom - wrapTop) + 'px';
      wrap.style.overflow  = 'hidden';
    } else {
      wrap.style.maxHeight = '';
      wrap.style.overflow  = '';
    }

    const verMaisBtn = document.createElement('button');
    verMaisBtn.className   = 'source-ver-mais-btn';
    verMaisBtn.textContent = sourcesExpanded ? 'ver menos' : 'ver mais';
    verMaisBtn.addEventListener('click', () => {
      sourcesExpanded = !sourcesExpanded;
      renderSourceCards();
    });
    wrap.parentNode.appendChild(verMaisBtn);
  });
}

function renderFilterBar() {
  const bar    = document.getElementById('filter-bar');
  const crumbs = document.getElementById('filter-crumbs');
  const btnEdit = document.getElementById('btn-edit-pdf');

  const hasMonth  = !!activeMonth;
  const hasSource = !!activeSource;

  if (hasMonth) {
    document.getElementById('edit-pdf-month-label').textContent = fmtMonthLabel(activeMonth);
    btnEdit.dataset.month = activeMonth;
    btnEdit.removeAttribute('hidden');
  } else {
    btnEdit.setAttribute('hidden', '');
  }

  if (!hasMonth && !hasSource) {
    bar.setAttribute('hidden', '');
    return;
  }

  bar.removeAttribute('hidden');

  let html = '';
  if (hasMonth) {
    const monthLabel = fmtMonthLabel(activeMonth);
    html += `<span class="filter-crumb" title="${esc(monthLabel)}">
      <span class="filter-crumb-text">${monthLabel}</span>
      <button class="filter-crumb-x" data-clear="month" title="Remover filtro">×</button>
    </span>`;
  }
  if (hasSource) {
    html += `<span class="filter-crumb" title="${esc(activeSource)}">
      <span class="filter-crumb-text">${esc(activeSource)}</span>
      <button class="filter-crumb-x" data-clear="source" title="Remover filtro">×</button>
    </span>`;
  }
  crumbs.innerHTML = html;

  crumbs.querySelectorAll('.filter-crumb-x').forEach(btn => {
    btn.addEventListener('click', () => {
      if (btn.dataset.clear === 'month')  { activeMonth  = null; renderMonthChips(); }
      if (btn.dataset.clear === 'source') { activeSource = null; renderSourceCards(); }
      renderFilterBar();
      renderTransactions();
    });
  });
}

// ─── Mutações ─────────────────────────────────────────────────────────────────
async function sendMutation(op, lancId, params) {
  if (!currentSession || mutationPending) return;
  mutationPending = true;
  document.getElementById('lanc-tbody')?.querySelectorAll('input,select').forEach(el => el.disabled = true);
  try {
    const resultado = await api('POST', `/sessao/${currentSession.session_id}/mutate`, {
      op, target: lancId, params,
    });
    currentSession  = resultado.sessao;
    currentApuracao = resultado.apuracao;
    currentLaudo    = resultado.laudo;
    renderMetrics();
    renderMonthChips();
    renderSourceCards();
    renderFilterBar();
    renderTransactions();
    if (pdfDoc && pdfEditorMode === 'edit') {
      await renderPdfPage(pdfCurrentIdx);
    }
  } catch (e) {
    toast(e.message || 'Erro ao aplicar mutação', 'error');
    document.getElementById('lanc-tbody')?.querySelectorAll('input,select').forEach(el => el.disabled = false);
  } finally {
    mutationPending = false;
  }
}

// ─── Patch de sessão (renomear / fixar) ───────────────────────────────────────
async function patchSessao(sessionId, updates) {
  return api('PATCH', `/sessao/${sessionId}`, updates);
}

// ─── Menu de contexto das sessões ─────────────────────────────────────────────
function openSessionMenu(sessionId, isPinned, anchorEl) {
  currentMenuSessionId     = sessionId;
  currentMenuSessionPinned = isPinned;

  const menu = document.getElementById('session-ctx-menu');
  menu.querySelector('[data-action="pin"]').textContent = isPinned ? 'Desafixar' : 'Fixar';

  menu.removeAttribute('hidden');
  const ar = anchorEl.getBoundingClientRect();
  const mr = menu.getBoundingClientRect();
  let top  = ar.bottom + 4;
  let left = ar.left;

  if (top + mr.height > window.innerHeight - 8) top = ar.top - mr.height - 4;
  if (left + mr.width > window.innerWidth - 8)  left = window.innerWidth - mr.width - 8;

  menu.style.top  = `${top}px`;
  menu.style.left = `${left}px`;
}

function closeSessionMenu() {
  document.getElementById('session-ctx-menu').setAttribute('hidden', '');
  currentMenuSessionId = null;
}

async function handleSessionMenuAction(action) {
  const id      = currentMenuSessionId;
  const isPinned = currentMenuSessionPinned;
  closeSessionMenu();
  if (!id) return;

  if (action === 'rename') {
    const s = sessions.find(s => s.session_id === id);
    const novo = prompt('Novo nome da apuração:', s?.session_name || '');
    if (!novo || !novo.trim()) return;
    try {
      const res = await patchSessao(id, { session_name: novo.trim() });
      const idx = sessions.findIndex(s => s.session_id === id);
      if (idx !== -1) sessions[idx].session_name = res.session_name;
      renderSessions(sessions);
      if (currentSession?.session_id === id) {
        currentSession.session_name = res.session_name;
        document.getElementById('result-name').textContent = res.session_name;
      }
      toast('Apuração renomeada', 'success');
    } catch (e) {
      toast(e.message || 'Erro ao renomear', 'error');
    }

  } else if (action === 'pin') {
    const novoPin = !isPinned;
    try {
      await patchSessao(id, { pinned: novoPin });
      const idx = sessions.findIndex(s => s.session_id === id);
      if (idx !== -1) sessions[idx].pinned = novoPin;
      sessions.sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0));
      renderSessions(sessions);
      toast(novoPin ? 'Apuração fixada' : 'Apuração desafixada', 'success');
    } catch (e) {
      toast(e.message || 'Erro ao fixar', 'error');
    }

  } else if (action === 'delete') {
    const s = sessions.find(s => s.session_id === id);
    if (!confirm(`Excluir a apuração "${s?.session_name || id}"?`)) return;
    try {
      await api('DELETE', `/sessao/${id}`);
      sessions = sessions.filter(s => s.session_id !== id);
      renderSessions(sessions);
      if (currentSession?.session_id === id) {
        currentSession  = null;
        currentApuracao = null;
        currentLaudo    = null;
        activeMonth     = null;
        activeSource    = null;
        knownFontes     = [];
        sourcesExpanded = false;
        sessionEditorOpened.clear();
        showFormView();
      }
      toast('Apuração excluída', 'success');
    } catch (e) {
      toast(e.message || 'Erro ao excluir', 'error');
    }
  }
}

// ─── Menu de contexto do usuário (footer) ─────────────────────────────────────
function openUserMenu() {
  const menu   = document.getElementById('user-ctx-menu');
  const footer = document.getElementById('sidebar-footer');
  const fr     = footer.getBoundingClientRect();

  menu.removeAttribute('hidden');
  const mr   = menu.getBoundingClientRect();
  const top  = fr.top - mr.height - 6;
  const left = fr.left;

  menu.style.top  = `${Math.max(8, top)}px`;
  menu.style.left = `${left}px`;
  if (left + mr.width > window.innerWidth - 8) {
    menu.style.left = `${window.innerWidth - mr.width - 8}px`;
  }
}

function closeUserMenu() {
  document.getElementById('user-ctx-menu').setAttribute('hidden', '');
}

