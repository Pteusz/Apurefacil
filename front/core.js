
// ─── Configuração ──────────────────────────────────────────────────────────────
const IS_LOCAL = ['localhost', '127.0.0.1'].includes(window.location.hostname);
const API_BASE = IS_LOCAL ? '' : 'https://backend.estabilizei.com.br';

// ─── Estado global ─────────────────────────────────────────────────────────────
let currentUser    = null;   // objeto retornado por /auth/me
let sessions       = [];     // lista de sessões do usuário
let selectedMode   = 'padrao';
let uploadedPdfs   = [];     // [{file_id, filename, total_pages, banco}] — banco selecionado no modal
let pendingEmail   = '';     // email que aguarda magic link
let bancosDisponiveis = []; // [{key, label}] carregados de GET /bancos (vanilla + user)

// ─── Estado do modal de banco e upload ────────────────────────────────────────
let bankSelectionCallback = null; // função chamada ao selecionar banco no modal
let _pendingBanco         = null; // banco key escolhido antes de abrir o file picker
let _pendingCapturePick   = false;// true quando o file picker é para captura sem sessão
let captureFileId         = null; // file_id do editor aberto para captura sem sessão

// ─── Estado dos menus de contexto ─────────────────────────────────────────────
let currentMenuSessionId     = null;   // session_id do menu de sessão aberto
let currentMenuSessionPinned = false;  // estado de pin da sessão no menu

// ─── Estado da sessão aberta ───────────────────────────────────────────────────
let currentSession   = null;  // objeto completo de /sessao/{id}
let currentApuracao  = null;  // resultado de /sessao/{id}/apuracao
let currentLaudo     = null;  // resultado de /sessao/{id}/laudo
let activeMonth      = null;  // 'YYYY-MM' do mês selecionado
let activeSource     = null;  // pagador (string) do card de fonte selecionado
let activeTypeFilter = null;  // tipo de exclusão selecionado ('circular', 'variancia', etc.)
let gruposActiveTab  = null;  // aba ativa na seção de grupos ('composicao', etc.)
let sessionEditorOpened = new Set(); // meses cujo editor já foi aberto
let mutationPending  = false; // bloqueia mutações simultâneas
let knownFontes      = [];    // fontes acumuladas — persiste mesmo quando inativas na apuração
let sourcesExpanded  = false; // estado de expansão do "ver mais" nas fontes de renda

// ─── Estado do editor PDF ──────────────────────────────────────────────────────
let pdfjsLib         = null;
let pdfDoc           = null;
let pdfCurrentFileId = null;  // file_id do pdfDoc carregado atualmente
let pdfEditorMonth   = null;
let pdfPages         = [];    // [{fileIdx, page}] do mês ativo (0-indexed)
let pdfCurrentIdx    = 0;
const PDF_SCALE      = 1.5;

// ─── Estado da captura de assinatura ──────────────────────────────────────────
let captureMode      = false;   // true quando modo captura está ativo
let captureStart     = null;    // {x, y} início do arraste (coords canvas)
let captureRectEl    = null;    // div de preview da seleção
let capturedSig      = null;    // assinatura derivada pelo backend
let capturedPreview  = [];      // preview_lancamentos da última extração
let capturedColunas  = [];      // colunas_detectadas da última extração (para nomeação)
let captureContext   = null;    // {fileId, page, fileIdx} para re-disparar backend ao ajustar handles
let captureOnSave    = null;    // callback executado após salvar assinatura (ex: retry submit)

// ─── Estado do modo validação / edição ────────────────────────────────────────
let pdfEditorMode    = null;    // 'validation' | 'edit' | null
let createLancMode   = false;   // true quando modo criar lançamento está ativo
let createLancPending = null;   // { lancamentos, bbox } aguardando confirmação
let selectedLancId   = null;    // lanc_id do overlay selecionado no modo edição

// ─── Estado do overlay de validação pós-upload ────────────────────────────────
let valPages            = [];   // [{fileIdx, page}] — páginas com lançamentos
let valCurrentIdx       = 0;
let valPdfDoc           = null;
let valPdfCurrentFileId = null;

// ─── Helper: converte estilo CSS do elemento em bbox PDF (÷ PDF_SCALE) ────────
function _elToBbox(el) {
  const x0 = parseFloat(el.style.left)   / PDF_SCALE;
  const y0 = parseFloat(el.style.top)    / PDF_SCALE;
  const w  = parseFloat(el.style.width)  / PDF_SCALE;
  const h  = parseFloat(el.style.height) / PDF_SCALE;
  return { x0, y0, x1: x0 + w, y1: y0 + h };
}

// ─── Helper: adiciona handles de resize + drag-to-move a um elemento ──────────
function _attachResizeHandles(el, onDragEnd) {
  el.classList.add('resizable');
  const DIRS = ['nw','n','ne','e','se','s','sw','w'];

  DIRS.forEach(dir => {
    const h = document.createElement('div');
    h.className  = 'resize-handle';
    h.dataset.dir = dir;
    el.appendChild(h);

    h.addEventListener('mousedown', e => {
      e.preventDefault(); e.stopPropagation();
      const sx = e.clientX, sy = e.clientY;
      const sl = parseFloat(el.style.left)   || 0;
      const st = parseFloat(el.style.top)    || 0;
      const sw = parseFloat(el.style.width)  || 0;
      const sh = parseFloat(el.style.height) || 0;

      const onMove = mv => {
        const dx = mv.clientX - sx, dy = mv.clientY - sy;
        let l = sl, t = st, w = sw, hh = sh;
        if (dir.includes('w')) { l = sl + dx; w = sw - dx; }
        if (dir.includes('e')) { w = sw + dx; }
        if (dir.includes('n')) { t = st + dy; hh = sh - dy; }
        if (dir.includes('s')) { hh = sh + dy; }
        if (w  < 20) { w = 20; if (dir.includes('w')) l = sl + sw - 20; }
        if (hh < 10) { hh = 10; if (dir.includes('n')) t = st + sh - 10; }
        el.style.left = `${l}px`; el.style.top  = `${t}px`;
        el.style.width = `${w}px`; el.style.height = `${hh}px`;
      };
      const onUp = () => {
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        if (onDragEnd) onDragEnd(el);
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  });

  // Drag do corpo para mover
  el.addEventListener('mousedown', e => {
    if (e.target.classList.contains('resize-handle')) return;
    e.preventDefault(); e.stopPropagation();
    const sx = e.clientX, sy = e.clientY;
    const sl = parseFloat(el.style.left) || 0;
    const st = parseFloat(el.style.top)  || 0;
    let moved = false;

    const onMove = mv => {
      moved = true;
      el.style.left = `${sl + mv.clientX - sx}px`;
      el.style.top  = `${st + mv.clientY - sy}px`;
    };
    const onUp = () => {
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      if (moved) {
        el.dataset.justDragged = '1';
        if (onDragEnd) onDragEnd(el);
      }
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });
}

// ─── Persistência do token ─────────────────────────────────────────────────────
const getToken   = ()  => localStorage.getItem('est_token');
const setToken   = t   => localStorage.setItem('est_token', t);
const clearToken = ()  => localStorage.removeItem('est_token');

// ─── Camada de API ─────────────────────────────────────────────────────────────
async function api(method, path, body = null) {
  const headers = {};
  const token   = getToken();
  if (token) headers['Authorization'] = `Bearer ${token}`;
  if (body)  headers['Content-Type']  = 'application/json';

  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : null,
  });

  if (!res.ok) {
    const err    = await res.json().catch(() => ({ detail: res.statusText }));
    const detail = err.detail;
    const msg    = typeof detail === 'string' ? detail : `Erro ${res.status}`;
    const errObj = new Error(msg);
    errObj.status = res.status;
    errObj.data   = detail;
    throw errObj;
  }
  return res.json();
}

async function uploadPdf(file) {
  const fd      = new FormData();
  const token   = getToken();
  const headers = {};
  fd.append('file', file);
  if (token) headers['Authorization'] = `Bearer ${token}`;

  const res = await fetch(`${API_BASE}/upload`, { method: 'POST', headers, body: fd });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || 'Erro ao enviar arquivo');
  }
  return res.json();
}

// ─── Helpers ──────────────────────────────────────────────────────────────────
function fmtDate(iso) {
  try {
    return new Date(iso).toLocaleDateString('pt-BR', {
      day: '2-digit', month: 'short', year: 'numeric',
    });
  } catch { return iso; }
}

function capitalize(s) {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : '';
}

function esc(str) {
  const d = document.createElement('div');
  d.textContent = str;
  return d.innerHTML;
}

function toast(msg, type = '') {
  const el = document.createElement('div');
  el.className = `toast${type ? ` ${type}` : ''}`;
  el.textContent = msg;
  document.getElementById('toast-container').appendChild(el);
  setTimeout(() => el.remove(), 3600);
}

// ─── Formatação ───────────────────────────────────────────────────────────────
function fmtBRL(n) {
  return new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' }).format(n || 0);
}

function fmtMonthLabel(ym) {
  const [y, m] = ym.split('-');
  const names = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez'];
  return `${names[parseInt(m) - 1]} ${y}`;
}

function fmtMonthShort(ym) {
  const [, m] = ym.split('-');
  const names = ['Jan','Fev','Mar','Abr','Mai','Jun','Jul','Ago','Set','Out','Nov','Dez'];
  return names[parseInt(m) - 1];
}

// ─── Frases de boas-vindas ────────────────────────────────────────────────────
const GREETINGS = [
  (n) => `Pronto para mais uma apuração,\u00a0<span id="greeting-name">${n}</span>?`,
  (n) => `O que vamos analisar hoje,\u00a0<span id="greeting-name">${n}</span>?`,
  (n) => `Seus extratos estão esperando,\u00a0<span id="greeting-name">${n}</span>.`,
  (n) => `Vamos trabalhar,\u00a0<span id="greeting-name">${n}</span>?`,
  (n) => `Tudo pronto para a apuração de hoje,\u00a0<span id="greeting-name">${n}</span>.`,
  (n) => `Mais uma análise precisa chegando,\u00a0<span id="greeting-name">${n}</span>.`,
  (n) => `Qual extrato vamos destrinchar hoje,\u00a0<span id="greeting-name">${n}</span>?`,
  (n) => `Pronto para descobrir a renda real,\u00a0<span id="greeting-name">${n}</span>?`,
  (n) => `Que bom ter você aqui,\u00a0<span id="greeting-name">${n}</span>. Vamos começar?`,
  (n) => `De volta para mais dados,\u00a0<span id="greeting-name">${n}</span>?`,
];

// ─── Tela de processamento ────────────────────────────────────────────────────
const PROC_STAGE_LABELS = [
  'Lendo os extratos...',
  'Identificando lançamentos...',
  'Classificando entradas e saídas...',
];

const PROC_TXNS = [
  { d: '08/04', desc: 'PIX RECEBIDO — MARIA S.',        v: 'R$ 3.200,00' },
  { d: '07/04', desc: 'TED ENTRADA — FREELANCE',         v: 'R$ 1.800,00' },
  { d: '06/04', desc: 'PIX RECEBIDO — JOÃO C.',          v: 'R$   950,00' },
  { d: '05/04', desc: 'TRANSFERÊNCIA RECEBIDA',          v: 'R$ 4.500,00' },
  { d: '04/04', desc: 'PIX — CLIENTE SERVIÇOS',          v: 'R$ 2.100,00' },
  { d: '03/04', desc: 'DEPÓSITO EM CONTA',               v: 'R$   750,00' },
  { d: '02/04', desc: 'TED — PAGAMENTO HONORÁRIOS',      v: 'R$ 6.000,00' },
  { d: '01/04', desc: 'PIX RECEBIDO — ANA L.',           v: 'R$ 1.350,00' },
  { d: '31/03', desc: 'SALÁRIO — EMPRESA XYZ',           v: 'R$ 5.200,00' },
  { d: '30/03', desc: 'PIX RECEBIDO — CARLOS M.',        v: 'R$   880,00' },
  { d: '29/03', desc: 'TRANSFERÊNCIA ENTRADA',           v: 'R$ 2.700,00' },
  { d: '28/03', desc: 'PIX — SERVIÇOS PRESTADOS',        v: 'R$ 1.600,00' },
  { d: '27/03', desc: 'TED RECEBIDO — CLIENTE',          v: 'R$ 3.900,00' },
  { d: '26/03', desc: 'PIX RECEBIDO — PEDRO A.',         v: 'R$   450,00' },
  { d: '25/03', desc: 'DEPÓSITO — PAGAMENTO OBRA',       v: 'R$ 8.500,00' },
  { d: '24/03', desc: 'PIX RECEBIDO — LUCIA F.',         v: 'R$ 1.100,00' },
  { d: '23/03', desc: 'TED — COMISSÃO VENDAS',           v: 'R$ 2.250,00' },
  { d: '22/03', desc: 'PIX RECEBIDO — ROBERTO S.',       v: 'R$ 3.600,00' },
  { d: '21/03', desc: 'ENTRADA — OUTROS SERVIÇOS',       v: 'R$   700,00' },
  { d: '20/03', desc: 'PIX — CONSULTORIA MENSAL',        v: 'R$ 4.800,00' },
];

let _procFeedInterval = null;
let _procTxnIdx       = 0;

function _setProcStage(n) {
  document.getElementById('proc-stage-label').textContent = PROC_STAGE_LABELS[n];
  document.querySelectorAll('.proc-dot').forEach((dot, i) => {
    dot.classList.toggle('active', i <= n);
  });
}

function _startFeed() {
  const feed = document.getElementById('proc-feed');
  _procTxnIdx = Math.floor(Math.random() * PROC_TXNS.length);

  _procFeedInterval = setInterval(() => {
    const tx  = PROC_TXNS[_procTxnIdx % PROC_TXNS.length];
    _procTxnIdx++;

    const row       = document.createElement('div');
    row.className   = 'proc-row';
    row.innerHTML   = `
      <span class="proc-row-date">${tx.d}</span>
      <span class="proc-row-desc">${tx.desc}</span>
      <span class="proc-row-val">${tx.v}</span>`;
    feed.appendChild(row);
    row.addEventListener('animationend', () => row.remove(), { once: true });
  }, 390);
}

function _stopFeed() {
  clearInterval(_procFeedInterval);
  _procFeedInterval = null;
  document.getElementById('proc-feed').innerHTML = '';
}

async function _finalizeProcView(success) {
  _stopFeed();
  if (success) {
    _setProcStage(2);
    await new Promise(r => setTimeout(r, 900));
  }
  document.getElementById('proc-view').setAttribute('hidden', '');
  if (!success) showFormView();
}

