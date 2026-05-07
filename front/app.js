// ─── Form ─────────────────────────────────────────────────────────────────────
function resetForm() {
  document.getElementById('input-titular').value   = '';
  document.getElementById('input-exclusoes').value = '';
  document.getElementById('input-pdfs').value      = '';
  document.getElementById('pdf-label').textContent = '+ PDFs';
  document.getElementById('id-label').textContent  = 'Identidade';
  document.getElementById('btn-upload-pdf').classList.remove('has-files');
  document.getElementById('btn-upload-id').classList.remove('has-files');
  document.getElementById('titular-suggestions').setAttribute('hidden', '');
  uploadedPdfs = [];
  renderUploadedFiles();
}

function setSubmitLoading(on) {
  const btn = document.getElementById('btn-submit');
  btn.disabled = on;
  btn.innerHTML = on
    ? '<span class="spinner"></span>'
    : `<svg width="16" height="16" viewBox="0 0 16 16" fill="none">
         <path d="M8 13V3M3 8l5-5 5 5" stroke="currentColor" stroke-width="2"
               stroke-linecap="round" stroke-linejoin="round"/>
       </svg>`;
}

async function handleSubmit() {
  const titular  = document.getElementById('input-titular').value.trim();
  const exclusoes = document.getElementById('input-exclusoes').value.trim();

  if (!titular) {
    toast('Informe o nome do titular', 'error');
    document.getElementById('input-titular').focus();
    return;
  }

  if (uploadedPdfs.length === 0) {
    toast('Adicione pelo menos um extrato em PDF', 'error');
    return;
  }

  if (!currentUser) {
    showModal('magic');
    return;
  }

  // Exibe tela de processamento e inicia animação
  showProcView();
  _setProcStage(0);

  // Aguarda 1.5 s antes de iniciar o feed (etapa 0 fica visível brevemente)
  const procStart = Date.now();
  setTimeout(() => {
    _setProcStage(1);
    _startFeed();
  }, 1500);

  try {
    const contas = [titular, ...exclusoes.split(',').map(s => s.trim()).filter(Boolean)];

    const sessao = await api('POST', '/sessao', {
      file_ids    : uploadedPdfs.map(p => p.file_id),
      session_name: titular,
      config      : { contas_proprias: contas, numeros_contas_proprias: [], modo: selectedMode },
      bancos      : uploadedPdfs.map(p => p.banco || null),
    });

    // Garante mínimo de 2.5 s de animação para que o feed seja visto
    const elapsed = Date.now() - procStart;
    if (elapsed < 2500) await new Promise(r => setTimeout(r, 2500 - elapsed));

    // Etapa final + encerramento da proc-view
    await _finalizeProcView(true);

    // Atualiza lista e créditos
    sessions = await api('GET', '/sessao');
    renderSessions(sessions);
    currentUser = await api('GET', '/auth/me');
    renderUser(currentUser);

    resetForm();
    await selectSession(sessao.session_id);
    toast('Apuração criada com sucesso!', 'success');
    // Abre overlay de validação para o usuário confirmar os lançamentos capturados
    if (currentSession && Object.keys(currentSession.meses || {}).length > 0) {
      await openValidationOverlay();
    }
  } catch (e) {
    await _finalizeProcView(false);

    // Conflito de assinatura: banco declarado não reconheceu o PDF
    if (e.status === 422 && e.data?.error === 'banco_conflito') {
      const { file_id, banco } = e.data;
      const bancoLabel = bancosDisponiveis.find(b => b.key === banco)?.label || banco;
      toast(`Layout do "${bancoLabel}" não reconhecido — selecione a área de assinatura no PDF`, 'error');
      const entry = uploadedPdfs.find(p => p.file_id === file_id);
      captureOnSave = handleSubmit; // retenta o submit após salvar a assinatura
      await openPdfEditorForCapture(file_id, entry?.total_pages || 1);
      return;
    }

    toast(e.message || 'Erro ao criar apuração', 'error');
  }
}

// ─── Upload de PDFs ───────────────────────────────────────────────────────────
async function handlePdfFiles(files, banco = null) {
  if (!files || files.length === 0) return;

  const btn = document.getElementById('btn-upload-pdf');
  btn.disabled = true;
  btn.classList.add('uploading');

  try {
    for (const file of files) {
      const result = await uploadPdf(file);
      uploadedPdfs.push({ ...result, banco });
    }
    const total = uploadedPdfs.length;
    document.getElementById('pdf-label').textContent =
      total === 1 ? uploadedPdfs[0].filename : `${total} PDFs`;
    btn.classList.add('has-files');
    renderUploadedFiles();
    toast(files.length === 1 ? 'Arquivo carregado' : `${files.length} arquivos carregados`, 'success');
  } catch (e) {
    toast(e.message || 'Erro ao enviar PDF', 'error');
  } finally {
    btn.disabled = false;
    btn.classList.remove('uploading');
  }
}

// Upload para fluxo de captura ("meu banco não está aqui") — abre editor automaticamente
async function handlePdfFilesForCapture(files) {
  if (!files || files.length === 0) return;

  const btn = document.getElementById('btn-upload-pdf');
  btn.disabled = true;
  btn.classList.add('uploading');

  try {
    const file   = files[0];   // captura: um arquivo por vez
    const result = await uploadPdf(file);
    uploadedPdfs.push({ ...result, banco: null });   // banco será preenchido após captura

    const total = uploadedPdfs.length;
    document.getElementById('pdf-label').textContent =
      total === 1 ? result.filename : `${total} PDFs`;
    btn.classList.add('has-files');
    renderUploadedFiles();

    await openPdfEditorForCapture(result.file_id, result.total_pages);
  } catch (e) {
    toast(e.message || 'Erro ao enviar PDF', 'error');
  } finally {
    btn.disabled = false;
    btn.classList.remove('uploading');
  }
}

// ─── Lista de arquivos enviados ───────────────────────────────────────────────
function renderUploadedFiles() {
  const list = document.getElementById('uploaded-files-list');
  if (uploadedPdfs.length === 0) { list.innerHTML = ''; return; }

  // Agrupa por banco
  const grupos = {};
  uploadedPdfs.forEach(p => {
    const key = p.banco || '__sem_banco__';
    if (!grupos[key]) grupos[key] = [];
    grupos[key].push(p);
  });

  list.innerHTML = Object.entries(grupos).map(([key, files]) => {
    const banco      = bancosDisponiveis.find(b => b.key === key);
    const bancoLabel = banco ? banco.label : (key !== '__sem_banco__' ? key : '—');
    const count      = files.length;
    const label      = count === 1 ? files[0].filename : `${count} PDFs`;
    return `
      <div class="upload-file-chip">
        <span class="upload-file-banco">${esc(bancoLabel)}</span>
        <span class="upload-file-name" title="${files.map(f => f.filename).join(', ')}">${esc(label)}</span>
        <button class="upload-file-del" data-banco="${esc(key)}" title="Remover">×</button>
      </div>`;
  }).join('');

  list.querySelectorAll('.upload-file-del').forEach(btn => {
    btn.addEventListener('click', () => {
      const key = btn.dataset.banco;
      uploadedPdfs = uploadedPdfs.filter(p => (p.banco || '__sem_banco__') !== key);
      const total = uploadedPdfs.length;
      if (total === 0) {
        document.getElementById('pdf-label').textContent = '+ PDFs';
        document.getElementById('btn-upload-pdf').classList.remove('has-files');
      } else {
        document.getElementById('pdf-label').textContent =
          total === 1 ? uploadedPdfs[0].filename : `${total} PDFs`;
      }
      renderUploadedFiles();
    });
  });
}


// ─── Autocomplete do titular ───────────────────────────────────────────────────
function setupTitularAutocomplete() {
  const input = document.getElementById('input-titular');
  const list  = document.getElementById('titular-suggestions');

  function getSuggestions() {
    const val = input.value.trim().toLowerCase();
    if (!val || sessions.length === 0) return [];
    const seen = new Set();
    return sessions
      .map(s => s.session_name || '')
      .filter(name => {
        const key = name.toLowerCase();
        if (!key.startsWith(val) || seen.has(key)) return false;
        seen.add(key);
        return true;
      })
      .slice(0, 6);
  }

  function render() {
    const names = getSuggestions();
    if (names.length === 0) { list.setAttribute('hidden', ''); return; }
    list.innerHTML = names.map(n => `<div class="autocomplete-item">${esc(n)}</div>`).join('');
    list.removeAttribute('hidden');
    list.querySelectorAll('.autocomplete-item').forEach((item, i) => {
      item.addEventListener('mousedown', e => {
        e.preventDefault();
        input.value = names[i];
        list.setAttribute('hidden', '');
      });
    });
  }

  input.addEventListener('input', render);
  input.addEventListener('focus', render);
  input.addEventListener('blur', () => setTimeout(() => list.setAttribute('hidden', ''), 150));
  input.addEventListener('keydown', e => {
    if (e.key === 'Escape') list.setAttribute('hidden', '');
  });
}

// ─── Event listeners ──────────────────────────────────────────────────────────
function setupEvents() {

  // Nova apuração
  document.getElementById('btn-new-session').addEventListener('click', () => {
    document.querySelectorAll('.session-item').forEach(el => el.classList.remove('active'));
    currentSession  = null;
    currentApuracao = null;
    currentLaudo    = null;
    activeMonth     = null;
    activeSource    = null;
    knownFontes     = [];
    sourcesExpanded = false;
    sessionEditorOpened.clear();
    resetForm();
    showFormView();
    document.getElementById('input-titular').focus();
  });

  // Exportar
  document.getElementById('btn-export').addEventListener('click', openExportModal);
  document.getElementById('export-modal-close').addEventListener('click', closeExportModal);
  document.getElementById('export-modal').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeExportModal();
  });
  document.getElementById('btn-export-laudo').addEventListener('click', exportLaudo);
  document.getElementById('btn-export-tabela').addEventListener('click', exportTabela);

  // Botão "Editar PDF" na header do resultado — abre em modo edição
  document.getElementById('btn-edit-pdf').addEventListener('click', e => {
    const month = e.currentTarget.dataset.month;
    if (month) openPdfEditor(month, 'edit');
  });

  // Validação: confirmar e fechar / ativar edição
  document.getElementById('btn-confirm-apuracao').addEventListener('click', closePdfEditor);
  document.getElementById('btn-confirm-edit').addEventListener('click', closePdfEditor);
  document.getElementById('btn-activate-edit').addEventListener('click', activateEditMode);

  // Painel criar lançamento: confirmar / descartar
  document.getElementById('btn-create-lanc-confirm').addEventListener('click', _confirmCreateLanc);
  document.getElementById('btn-create-lanc-discard').addEventListener('click', () => {
    document.getElementById('create-lanc-panel').setAttribute('hidden', '');
    _removeCaptureRect();
    createLancPending = null;
  });

  // Excluir sessão
  document.getElementById('btn-delete-session').addEventListener('click', async () => {
    if (!currentSession) return;
    if (!confirm(`Excluir a sessão "${currentSession.session_name}"?`)) return;
    try {
      await api('DELETE', `/sessao/${currentSession.session_id}`);
      sessions = await api('GET', '/sessao');
      renderSessions(sessions);
      currentSession  = null;
      currentApuracao = null;
      currentLaudo    = null;
      activeMonth     = null;
      activeSource    = null;
      knownFontes     = [];
      sourcesExpanded = false;
      sessionEditorOpened.clear();
      showFormView();
      toast('Sessão excluída', 'success');
    } catch (e) {
      toast(e.message || 'Erro ao excluir sessão', 'error');
    }
  });

  // PDF editor — navegação de páginas
  document.getElementById('btn-pdf-prev').addEventListener('click', async () => {
    if (pdfCurrentIdx > 0) {
      pdfCurrentIdx--;
      await renderPdfPage(pdfCurrentIdx);
      updatePdfNav();
    }
  });
  document.getElementById('btn-pdf-next').addEventListener('click', async () => {
    if (pdfCurrentIdx < pdfPages.length - 1) {
      pdfCurrentIdx++;
      await renderPdfPage(pdfCurrentIdx);
      updatePdfNav();
    }
  });

  // PDF editor — fechar (apenas pelo botão ×, não por clique fora)
  document.getElementById('btn-close-pdf-editor').addEventListener('click', closePdfEditor);

  // Overlay de validação — navegação e ações
  document.getElementById('val-btn-prev').addEventListener('click', async () => {
    if (valCurrentIdx > 0) {
      valCurrentIdx--;
      _updateValNav();
      await renderValBlock(valCurrentIdx);
    }
  });
  document.getElementById('val-btn-next').addEventListener('click', async () => {
    if (valCurrentIdx < valPages.length - 1) {
      valCurrentIdx++;
      _updateValNav();
      await renderValBlock(valCurrentIdx);
    }
  });
  document.getElementById('val-btn-confirm').addEventListener('click', () => {
    closeValidationOverlay();
  });
  document.getElementById('val-btn-adjust').addEventListener('click', () => {
    closeValidationOverlay();
    openPdfEditor(null, 'edit');
  });
  document.getElementById('val-btn-mismatch').addEventListener('click', async () => {
    closeValidationOverlay();
    const fileIds = currentSession?.file_ids || (currentSession?.file_id ? [currentSession.file_id] : []);
    if (fileIds.length > 0) {
      const fileId = fileIds[0];
      const entry  = uploadedPdfs.find(p => p.file_id === fileId);
      await openPdfEditorForCapture(fileId, entry?.total_pages || 1);
    }
  });

  // Tecla Delete: exclui lançamento selecionado ou remove retângulo de captura ativo
  window.addEventListener('keydown', async e => {
    if (e.key !== 'Delete') return;

    // Prioridade 1: excluir lançamento selecionado (set_flag ignorar = soft-delete)
    if (selectedLancId && pdfEditorMode === 'edit') {
      e.preventDefault();
      const idToDelete = selectedLancId;
      selectedLancId = null;
      document.querySelectorAll('.pdf-overlay-selected')
        .forEach(el => el.classList.remove('pdf-overlay-selected'));
      await sendMutation('set_flag', idToDelete, { flag: 'ignorar' });
      return;
    }

    // Prioridade 2: remover retângulo de captura ativo
    if ((captureMode || createLancMode) && captureRectEl) {
      _removeCaptureRect();
      if (captureMode) {
        document.getElementById('sig-panel').setAttribute('hidden', '');
        capturedSig     = null;
        capturedPreview = [];
        capturedColunas = [];
        await renderPdfPage(pdfCurrentIdx);
      }
      if (createLancMode) {
        document.getElementById('create-lanc-panel').setAttribute('hidden', '');
        createLancPending = null;
      }
    }
  });

  // PDF editor — captura de assinatura
  document.getElementById('btn-capture-toggle').addEventListener('click', toggleCaptureMode);
  document.getElementById('btn-sig-save').addEventListener('click', _saveSig);
  document.getElementById('btn-sig-reset').addEventListener('click', async () => {
    document.getElementById('sig-panel').setAttribute('hidden', '');
    capturedSig     = null;
    capturedPreview = [];
    capturedColunas = [];
    _removeCaptureRect();
    await renderPdfPage(pdfCurrentIdx); // remove overlays de preview do canvas
  });
  document.getElementById('sig-bank-name').addEventListener('keydown', e => {
    if (e.key === 'Enter') _saveSig();
  });

  // Captura — apenas mousedown no container; move/up vão para document durante o drag
  const pdfBody = document.getElementById('pdf-editor-body');
  pdfBody.addEventListener('mousedown', _onCaptureMousedown);

  // Submit
  document.getElementById('btn-submit').addEventListener('click', handleSubmit);

  // Enter no formulário
  document.getElementById('input-titular').addEventListener('keydown', e => {
    if (e.key === 'Enter') document.getElementById('input-exclusoes').focus();
  });

  // Seletor de modo
  document.querySelectorAll('.mode-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      selectedMode = btn.dataset.mode;
    });
  });

  // Upload PDFs — abre modal de banco antes do file picker
  document.getElementById('btn-upload-pdf').addEventListener('click', () => {
    if (!currentUser) { showModal('magic'); return; }
    openBancoModal(banco => {
      _pendingBanco = banco;
      document.getElementById('input-pdfs').click();
    });
  });
  document.getElementById('input-pdfs').addEventListener('change', async e => {
    const files = Array.from(e.target.files);  // copia antes de resetar
    e.target.value = '';                        // permite selecionar o mesmo arquivo novamente
    if (_pendingCapturePick) {
      _pendingCapturePick = false;
      await handlePdfFilesForCapture(files);
    } else if (_pendingBanco) {
      const banco = _pendingBanco;
      _pendingBanco = null;
      await handlePdfFiles(files, banco);
    }
  });

  // Modal de banco
  document.getElementById('banco-modal-close').addEventListener('click', closeBancoModal);
  document.getElementById('banco-modal').addEventListener('click', e => {
    if (e.target === e.currentTarget) closeBancoModal();
  });
  document.getElementById('btn-banco-nao-encontrado').addEventListener('click', () => {
    closeBancoModal();
    _pendingCapturePick = true;
    document.getElementById('input-pdfs').click();
  });

  // Identidade — em breve
  document.getElementById('btn-upload-id').addEventListener('click', () => {
    toast('Em breve disponível para teste', '');
  });

  // ── Menu de contexto das sessões ──────────────────────────────────────────────
  const sessionCtxMenu = document.getElementById('session-ctx-menu');
  sessionCtxMenu.addEventListener('click', e => e.stopPropagation());
  sessionCtxMenu.querySelectorAll('.ctx-menu-item').forEach(btn => {
    btn.addEventListener('click', () => handleSessionMenuAction(btn.dataset.action));
  });

  // ── Footer / menu do usuário ───────────────────────────────────────────────────
  document.getElementById('sidebar-footer').addEventListener('click', e => {
    e.stopPropagation();
    const userMenu = document.getElementById('user-ctx-menu');
    if (!userMenu.hasAttribute('hidden')) {
      closeUserMenu();
    } else {
      openUserMenu();
    }
  });

  const userCtxMenu = document.getElementById('user-ctx-menu');
  userCtxMenu.addEventListener('click', e => e.stopPropagation());
  userCtxMenu.querySelectorAll('.ctx-menu-item').forEach(btn => {
    btn.addEventListener('click', () => {
      closeUserMenu();
      const action = btn.dataset.action;
      if (action === 'perfil')   window.location.href = 'account.html#perfil';
      if (action === 'planos')   window.location.href = 'account.html#plano';
      if (action === 'financas') window.location.href = 'account.html#financas';
    });
  });

  // ── Fechar menus ao clicar fora ────────────────────────────────────────────────
  document.addEventListener('click', () => {
    closeSessionMenu();
    closeUserMenu();
  });

  // Modal — fechar
  document.getElementById('modal-close').addEventListener('click', hideModal);
  document.getElementById('auth-modal').addEventListener('click', e => {
    if (e.target === e.currentTarget) hideModal();
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { hideModal(); closeBancoModal(); }
  });

  // Magic link — enviar
  document.getElementById('btn-send-magic').addEventListener('click', async () => {
    const email = document.getElementById('auth-email').value.trim();
    if (!email) { setMsg('magic-msg', 'Informe seu email'); return; }

    const btn = document.getElementById('btn-send-magic');
    btn.disabled    = true;
    btn.textContent = 'Enviando…';
    try {
      await api('POST', '/auth/magic-link', { email });
      pendingEmail = email;
      document.getElementById('sent-email').textContent = email;
      setStep('sent');
    } catch (e) {
      setMsg('magic-msg', e.message || 'Erro ao enviar link');
    } finally {
      btn.disabled    = false;
      btn.textContent = 'Enviar link de acesso';
    }
  });

  // Toggle → senha
  document.getElementById('btn-to-password').addEventListener('click', () => {
    const email = document.getElementById('auth-email').value.trim();
    if (email) document.getElementById('auth-email-pw').value = email;
    setStep('password');
  });

  // Toggle → magic link
  document.getElementById('btn-to-magic').addEventListener('click', () => {
    const email = document.getElementById('auth-email-pw').value.trim();
    if (email) document.getElementById('auth-email').value = email;
    setStep('magic');
  });

  // Login com senha
  document.getElementById('btn-login-pw').addEventListener('click', async () => {
    const email    = document.getElementById('auth-email-pw').value.trim();
    const password = document.getElementById('auth-password').value;
    if (!email || !password) { setMsg('pw-msg', 'Preencha email e senha'); return; }

    const btn = document.getElementById('btn-login-pw');
    btn.disabled    = true;
    btn.textContent = 'Entrando…';
    try {
      const result = await api('POST', '/auth/login', { email, password });
      await onLoginSuccess(result);
    } catch (e) {
      setMsg('pw-msg', e.message || 'Email ou senha incorretos');
    } finally {
      btn.disabled    = false;
      btn.textContent = 'Entrar';
    }
  });

  // Reenviar link
  document.getElementById('btn-resend').addEventListener('click', () => setStep('magic'));

  // Enter nos inputs do modal
  document.getElementById('auth-email').addEventListener('keydown', e => {
    if (e.key === 'Enter') document.getElementById('btn-send-magic').click();
  });
  document.getElementById('auth-password').addEventListener('keydown', e => {
    if (e.key === 'Enter') document.getElementById('btn-login-pw').click();
  });
}

// ─── Carrega lista de bancos do backend ───────────────────────────────────────
async function loadBancos() {
  try {
    const resp    = await api('GET', '/bancos');
    const vanilla = Array.isArray(resp) ? resp : (resp.vanilla || []);
    const user    = Array.isArray(resp) ? []   : (resp.user    || []);
    bancosDisponiveis = [
      ...vanilla.map(b => ({ key: b.key, label: b.label, group: 'vanilla' })),
      ...user.map(b    => ({ key: b.key, label: b.label, group: 'user'    })),
    ];
  } catch { /* silencioso */ }
}

// ─── Modal de seleção de banco ────────────────────────────────────────────────
function openBancoModal(onSelect) {
  bankSelectionCallback = onSelect;

  const list    = document.getElementById('banco-modal-list');
  list.innerHTML = '';

  const vanilla = bancosDisponiveis.filter(b => b.group === 'vanilla');
  const user    = bancosDisponiveis.filter(b => b.group === 'user');

  function addItem(b) {
    const btn       = document.createElement('button');
    btn.className   = 'banco-modal-item';
    btn.textContent = b.label;
    btn.addEventListener('click', () => { closeBancoModal(); onSelect(b.key); });
    list.appendChild(btn);
  }

  if (vanilla.length) {
    const grp       = document.createElement('div');
    grp.className   = 'banco-modal-group-label';
    grp.textContent = 'Bancos suportados';
    list.appendChild(grp);
    vanilla.forEach(addItem);
  }
  if (user.length) {
    const grp       = document.createElement('div');
    grp.className   = 'banco-modal-group-label';
    grp.textContent = 'Minhas assinaturas';
    list.appendChild(grp);
    user.forEach(addItem);
  }
  if (!vanilla.length && !user.length) {
    const empty       = document.createElement('p');
    empty.style.cssText = 'padding:20px;color:var(--color-text-muted);font-size:13px';
    empty.textContent  = 'Nenhum banco disponível';
    list.appendChild(empty);
  }

  document.getElementById('banco-modal').removeAttribute('hidden');
}

function closeBancoModal() {
  document.getElementById('banco-modal').setAttribute('hidden', '');
  bankSelectionCallback = null;
}

// ─── Tutorial: captura de modelos ────────────────────────────────────────────
const TUTORIAL_CAPTURE_KEY = 'est_tutorial_capture_seen';

function showCaptureTutorial() {
  if (localStorage.getItem(TUTORIAL_CAPTURE_KEY)) return;

  const modal = document.getElementById('tutorial-capture-modal');
  modal.removeAttribute('hidden');

  function closeTutorial() {
    modal.setAttribute('hidden', '');
  }

  document.getElementById('btn-tutorial-close').onclick = closeTutorial;
  document.getElementById('btn-tutorial-ok').onclick    = closeTutorial;
  document.getElementById('btn-tutorial-skip').onclick  = () => {
    localStorage.setItem(TUTORIAL_CAPTURE_KEY, '1');
    closeTutorial();
  };
}

// ─── Tutorial: edição de lançamentos ─────────────────────────────────────────
const TUTORIAL_EDIT_KEY = 'est_tutorial_edit_seen';

function showEditTutorial() {
  if (localStorage.getItem(TUTORIAL_EDIT_KEY)) return;

  const modal = document.getElementById('tutorial-edit-modal');
  modal.removeAttribute('hidden');

  function closeTutorial() {
    modal.setAttribute('hidden', '');
  }

  document.getElementById('btn-tutorial-edit-close').onclick = closeTutorial;
  document.getElementById('btn-tutorial-edit-ok').onclick    = closeTutorial;
  document.getElementById('btn-tutorial-edit-skip').onclick  = () => {
    localStorage.setItem(TUTORIAL_EDIT_KEY, '1');
    closeTutorial();
  };
}

// ─── Inicialização ─────────────────────────────────────────────────────────────
async function init() {
  setupEvents();
  setupTitularAutocomplete();
  loadBancos();

  // 1. Verifica se URL contém magic token (usuário clicou no link do email)
  const params     = new URLSearchParams(window.location.search);
  const magicToken = params.get('magic');

  if (magicToken) {
    try {
      const result = await api('GET', `/auth/verify/${magicToken}`);
      await onLoginSuccess(result);
    } catch {
      toast('Link de acesso inválido ou expirado.', 'error');
    }
    // Remove o parâmetro da URL sem recarregar a página
    window.history.replaceState({}, '', window.location.pathname);
    return;
  }

  // 2. Tenta recuperar sessão do token armazenado
  currentUser = await tryLoadUser();
  renderUser(currentUser);

  // 3. Carrega histórico de sessões se autenticado
  if (currentUser) {
    try {
      sessions = await api('GET', '/sessao');
      renderSessions(sessions);
    } catch { /* silent */ }
  }
}

init().catch(console.error);

// ─── Resize das sidebars ──────────────────────────────────────────────────────
(function initSidebarResize() {
  const MIN_W = 220;
  const MAX_W = 800;
  let dragging = null; // { handle, sidebar, startX, startW }

  document.addEventListener('mousedown', e => {
    const handle = e.target.closest('.sig-resize-handle');
    if (!handle) return;
    const sidebar = handle.closest('.pdf-sig-sidebar');
    if (!sidebar) return;
    dragging = {
      handle,
      sidebar,
      startX: e.clientX,
      startW: sidebar.offsetWidth,
    };
    handle.classList.add('dragging');
    document.body.style.cursor = 'ew-resize';
    document.body.style.userSelect = 'none';
    e.preventDefault();
  });

  document.addEventListener('mousemove', e => {
    if (!dragging) return;
    const delta = dragging.startX - e.clientX; // left handle: drag left = wider
    const newW  = Math.min(MAX_W, Math.max(MIN_W, dragging.startW + delta));
    dragging.sidebar.style.width = newW + 'px';
  });

  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging.handle.classList.remove('dragging');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    dragging = null;
  });
})();


