
// ─── Pagamento ────────────────────────────────────────────────────────────────
// A lógica de pagamento reside no backend (modules/pagamentos.py).
// No frontend, as ações de planos e faturas são gerenciadas em account.html.
// Este arquivo centraliza eventuais chamadas de API relacionadas a pagamento
// que precisem ser feitas a partir do app principal (index.html).

async function fetchPlanoAtual() {
  return api('GET', '/pagamento/plano');
}

async function iniciarCheckout(plano) {
  return api('POST', '/pagamento/checkout', { plano });
}

async function fetchFaturas() {
  return api('GET', '/pagamento/faturas');
}

