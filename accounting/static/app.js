'use strict';
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const won = n => (Math.round(Number(n) || 0)).toLocaleString('ko-KR');
const num = v => { const s = String(v ?? '').replace(/[^0-9.\-]/g, ''); return s === '' ? null : Number(s); };
const iso = d => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);

const state = { tab: 'dashboard', kind: 'sale', settings: {}, clients: [], products: [], entries: [] };

function toast(msg, err) {
  const t = $('#toast');
  t.textContent = msg; t.className = 'show' + (err ? ' err' : '');
  clearTimeout(t._h); t._h = setTimeout(() => (t.className = ''), err ? 5000 : 2200);
}

async function api(path, opts = {}) {
  const res = await fetch('/api/' + path, {
    method: opts.method || 'GET',
    headers: opts.body ? { 'Content-Type': 'application/json' } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { toast(data.error || '오류가 발생했습니다', true); throw new Error(data.error); }
  return data;
}
const period = () => `from=${$('#from').value}&to=${$('#to').value}`;

// ───────── 기간 선택
function setRange(r) {
  const t = new Date(); let a, b;
  if (r === 'today') a = b = t;
  if (r === 'month') { a = new Date(t.getFullYear(), t.getMonth(), 1); b = new Date(t.getFullYear(), t.getMonth() + 1, 0); }
  if (r === 'lastmonth') { a = new Date(t.getFullYear(), t.getMonth() - 1, 1); b = new Date(t.getFullYear(), t.getMonth(), 0); }
  if (r === 'quarter') { const q = Math.floor(t.getMonth() / 3) * 3; a = new Date(t.getFullYear(), q, 1); b = new Date(t.getFullYear(), q + 3, 0); }
  if (r === 'year') { a = new Date(t.getFullYear(), 0, 1); b = new Date(t.getFullYear(), 11, 31); }
  $('#from').value = iso(a); $('#to').value = iso(b);
  $$('.quick button').forEach(x => x.classList.toggle('on', x.dataset.range === r));
  refresh();
}
$$('.quick button').forEach(b => b.onclick = () => setRange(b.dataset.range));
['#from', '#to'].forEach(s => $(s).onchange = () => { $$('.quick button').forEach(x => x.classList.remove('on')); refresh(); });

// ───────── 탭
function showTab(tab) {
  state.tab = tab;
  $$('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === tab));
  const sec = (tab === 'sale' || tab === 'purchase') ? 'entries' : tab;
  $$('.tab').forEach(s => s.classList.toggle('on', s.id === 'tab-' + sec));
  if (sec === 'entries') { state.kind = tab; setupEntryLabels(); resetEntryForm(); }
  $('.period').style.display = ['products', 'clients', 'settings', 'import'].includes(tab) ? 'none' : '';
  refresh();
}
$$('#tabs button').forEach(b => b.onclick = () => showTab(b.dataset.tab));

async function refresh() {
  const t = state.tab;
  if (t === 'dashboard') return loadDashboard();
  if (t === 'sale' || t === 'purchase') return loadEntries();
  if (t === 'expense') return loadExpenses();
  if (t === 'products') return loadProducts();
  if (t === 'clients') return loadClients();
  if (t === 'import') return loadImport();
  if (t === 'settings') return loadSettings();
}

async function loadMasters() {
  [state.clients, state.products] = await Promise.all([api('clients'), api('products')]);
  $('#clientList').innerHTML = state.clients.map(c => `<option value="${esc(c.name)}">`).join('');
  $('#productList').innerHTML = state.products.map(p => `<option value="${esc(p.name)}">${esc(p.code)}</option>`).join('');
}

function table(el, cols, data, empty = '자료가 없습니다') {
  const head = '<tr>' + cols.map(c => `<th class="${c.n ? 'n' : ''}">${c.h}</th>`).join('') + '</tr>';
  const body = data.length ? data.map(r => '<tr>' + cols.map(c => {
    const v = c.f ? c.f(r) : esc(r[c.k]);
    return `<td class="${c.n ? 'n' : ''}${c.cls ? ' ' + c.cls : ''}">${v}</td>`;
  }).join('') + '</tr>').join('') : `<tr><td class="empty" colspan="${cols.length}">${empty}</td></tr>`;
  el.innerHTML = head + body;
}

// ───────── 대시보드
async function loadDashboard() {
  const s = await api('summary?' + period());
  const sign = v => v < 0 ? 'neg' : 'pos';
  const kpi = (l, v, sub, cls = '', main) => `<div class="kpi${main ? ' main' : ''}"><div class="l">${l}</div><div class="v ${cls}">${won(v)}<small>원</small></div><div class="s">${sub || '&nbsp;'}</div></div>`;
  $('#kpis').innerHTML =
    kpi('매출 (공급가)', s.sales, `부가세 ${won(s.sales_vat)}원 별도`, '', true) +
    kpi('매출원가', s.cogs, s.cost_mode === 'purchase' ? '기간 내 매입 합계' : '판매수량 × 원가') +
    kpi('매출총이익', s.gross_profit, s.gross_margin != null ? `이익률 ${s.gross_margin}%` : '', sign(s.gross_profit)) +
    kpi('비용 (판관비)', s.expenses) +
    kpi('순이익', s.net_profit, s.net_margin != null ? `순이익률 ${s.net_margin}%` : '', sign(s.net_profit), true) +
    kpi('미수금', s.receivables_total, '입금 대기 (VAT 포함)', s.receivables_total ? 'neg' : '') +
    kpi('예상 부가세', s.vat_payable, '매출세액 − 매입세액');

  $('#costModeNote').textContent = `${s.from} ~ ${s.to}`;
  const row = (l, v, cls = '') => `<tr class="${cls}"><td>${l}</td><td class="n">${won(v)}</td></tr>`;
  $('#pl').innerHTML = row('Ⅰ. 매출액', s.sales) + row('Ⅱ. 매출원가', s.cogs) +
    row('Ⅲ. 매출총이익', s.gross_profit, 'sum') + row('Ⅳ. 판매비와관리비', s.expenses) +
    s.expense_by_category.map(c => row(c.category, c.amount, 'sub')).join('') +
    row('Ⅴ. 순이익 (세전)', s.net_profit, 'sum') +
    row('참고: 매입 합계(공급가)', s.purchases, 'sub');
  $('#costWarn').textContent = (s.cost_mode !== 'purchase' && s.missing_cost_count)
    ? `⚠ 원가가 등록되지 않은 매출 ${s.missing_cost_count}건이 있어 순이익이 실제보다 크게 보일 수 있습니다. [품목·단가]에서 원가를 입력하세요.` : '';

  drawChart(s.monthly);
  table($('#topClients'), [{ h: '거래처', k: 'name' }, { h: '건수', k: 'cnt', n: 1 }, { h: '매출', n: 1, f: r => won(r.sales) }], s.top_clients);
  table($('#topProducts'), [{ h: '품목', k: 'name' }, { h: '수량', n: 1, f: r => won(r.qty) }, { h: '매출', n: 1, f: r => won(r.sales) },
    { h: '마진', n: 1, f: r => `<span class="${sign(r.margin)}">${won(r.margin)}</span>` }], s.top_products);
  table($('#receivables'), [{ h: '거래처', k: 'name' }, { h: '최초', k: 'since' }, { h: '건수', k: 'cnt', n: 1 }, { h: '미수금', n: 1, f: r => won(r.amount) }], s.receivables, '미수금 없음 👍');
  table($('#daily'), [{ h: '일자', k: 'date' }, { h: '건수', k: 'cnt', n: 1 }, { h: '매출', n: 1, f: r => won(r.sales) }], s.daily);
  table($('#expCats'), [{ h: '계정', k: 'category' }, { h: '금액', n: 1, f: r => won(r.amount) }], s.expense_by_category);
}

function drawChart(months) {
  const el = $('#chart');
  if (!months.length) { el.innerHTML = '<p class="empty">선택한 기간에 자료가 없습니다</p>'; return; }
  const W = 640, H = 260, pl = 56, pb = 28, pt = 10, pr = 10;
  const vals = months.flatMap(m => [m.sales, m.net]);
  const max = Math.max(0, ...vals), min = Math.min(0, ...vals);
  const span = (max - min) || 1;
  const y = v => pt + (max - v) / span * (H - pt - pb);
  const bw = (W - pl - pr) / months.length;
  const barW = Math.min(28, bw * 0.34);
  const ticks = [max, (max + min) / 2, min, 0].filter((v, i, a) => a.indexOf(v) === i);
  const short = v => Math.abs(v) >= 1e8 ? (v / 1e8).toFixed(1) + '억' : Math.abs(v) >= 1e4 ? Math.round(v / 1e4).toLocaleString() + '만' : Math.round(v);
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="월별 매출과 순이익">`;
  ticks.forEach(t => svg += `<line x1="${pl}" x2="${W - pr}" y1="${y(t)}" y2="${y(t)}" stroke="#E6E2DA" ${t === 0 ? 'stroke-width="1.5" stroke="#A5A29C"' : ''}/><text x="${pl - 6}" y="${y(t) + 4}" text-anchor="end" font-size="11" fill="#6B6B6B">${short(t)}</text>`);
  months.forEach((m, i) => {
    const cx = pl + bw * i + bw / 2;
    const bar = (v, x, color) => `<rect x="${x}" y="${Math.min(y(v), y(0))}" width="${barW}" height="${Math.max(1, Math.abs(y(v) - y(0)))}" rx="3" fill="${color}"><title>${m.month} ${won(v)}원</title></rect>`;
    svg += bar(m.sales, cx - barW - 1, '#7B5EA7') + bar(m.net, cx + 1, m.net < 0 ? '#C2410C' : '#3E8E74');
    svg += `<text x="${cx}" y="${H - 8}" text-anchor="middle" font-size="11" fill="#6B6B6B">${m.month.slice(2).replace('-', '.')}</text>`;
  });
  el.innerHTML = `<div class="legend"><span><i style="background:#7B5EA7"></i>매출</span><span><i style="background:#3E8E74"></i>순이익</span></div>${svg}</svg>`;
}

// ───────── 매출 / 매입
function setupEntryLabels() {
  const sale = state.kind === 'sale';
  $('#entryFormTitle').textContent = sale ? '매출 입력' : '매입 입력';
  $('#paidLabel').textContent = sale ? '입금완료' : '지급완료';
  $('#unpaidLabel').textContent = sale ? '미입금만' : '미지급만';
  $('#markPaidLabel').textContent = sale ? '입금완료' : '지급완료';
}
function resetEntryForm() {
  const f = $('#entryForm'); f.reset(); f.id.value = ''; f.date.value = iso(new Date()); f.qty.value = 1; f.unit_price.placeholder = '자동';
}
$('#entryReset').onclick = resetEntryForm;

async function autoPrice() {
  const f = $('#entryForm');
  const p = state.products.find(x => x.name === f.item.value.trim());
  if (!p || f.unit_price.value) return;
  const c = state.clients.find(x => x.name === f.client.value.trim());
  const r = await api(`price?kind=${state.kind}&product_id=${p.id}&client_id=${c ? c.id : ''}`);
  if (r.price) f.unit_price.placeholder = won(r.price) + ' (자동)';
}
$('#entryForm').item.addEventListener('change', autoPrice);
// 수량·단가를 고치면 공급가액·부가세는 다시 자동 계산되도록 비움
['qty', 'unit_price'].forEach(k => $('#entryForm')[k].addEventListener('input', () => { $('#entryForm').supply.value = ''; $('#entryForm').vat.value = ''; }));
$('#entryForm').client.addEventListener('change', () => { $('#entryForm').unit_price.placeholder = '자동'; autoPrice(); });

$('#entryForm').onsubmit = async e => {
  e.preventDefault();
  const f = e.target;
  const body = { kind: state.kind };
  ['date', 'client', 'item', 'qty', 'unit_price', 'supply', 'vat', 'order_no', 'memo'].forEach(k => { if (f[k].value !== '') body[k] = f[k].value; });
  body.paid = f.paid.checked ? 1 : 0;
  if (f.vat_free.checked) { body.vat_free = 1; body.vat = 0; }
  const id = f.id.value;
  await api('entries' + (id ? '/' + id : ''), { method: id ? 'PUT' : 'POST', body });
  toast('저장했습니다'); resetEntryForm(); await loadMasters(); loadEntries();
};

$('#entrySearch').oninput = () => { clearTimeout(state._sh); state._sh = setTimeout(loadEntries, 250); };
$('#unpaidOnly').onchange = loadEntries;

async function loadEntries() {
  await loadMasters();
  const q = encodeURIComponent($('#entrySearch').value.trim());
  const list = await api(`entries?kind=${state.kind}&${period()}&q=${q}${$('#unpaidOnly').checked ? '&unpaid=1' : ''}`);
  state.entries = list;
  const sum = k => list.reduce((a, r) => a + (r[k] || 0), 0);
  const sale = state.kind === 'sale';
  $('#entryTotals').innerHTML = `${list.length}건 · 공급가 <b>${won(sum('supply'))}</b> · 부가세 <b>${won(sum('vat'))}</b> · 합계 <b>${won(sum('total'))}</b>` +
    (sale ? ` · 원가 <b>${won(sum('cost_amount'))}</b> · 마진 <b>${won(sum('supply') - sum('cost_amount'))}</b>` : '');
  const cols = [
    { h: '<input type="checkbox" id="checkAll">', f: r => `<input type="checkbox" class="rowchk" value="${r.id}">` },
    { h: '일자', k: 'date' }, { h: '거래처', k: 'client_name' }, { h: '품목', k: 'item_name' },
    { h: '수량', n: 1, f: r => won(r.qty) }, { h: '단가', n: 1, f: r => won(r.unit_price) },
    { h: '공급가액', n: 1, f: r => won(r.supply) }, { h: '부가세', n: 1, f: r => won(r.vat) }, { h: '합계', n: 1, f: r => won(r.total) },
  ];
  if (sale) cols.push({ h: '마진', n: 1, f: r => r.cost_amount ? won(r.supply - r.cost_amount) : '<span class="tag no">원가없음</span>' });
  cols.push(
    { h: sale ? '입금' : '지급', f: r => r.paid ? '<span class="tag ok">완료</span>' : '<span class="tag no">대기</span>' },
    { h: '주문번호', k: 'order_no' },
    { h: '경로', f: r => `<span class="tag">${{ manual: '수기', import: '파일', folder: '폴더', api: 'API' }[r.source] || esc(r.source)}</span>` },
    { h: '', cls: 'act', f: r => `<button data-edit="${r.id}">수정</button> <button class="danger" data-del="${r.id}">삭제</button>` });
  table($('#entryTable'), cols, list);
  $('#checkAll').onchange = e => $$('.rowchk').forEach(c => c.checked = e.target.checked);
}

$('#entryTable').onclick = async e => {
  const ed = e.target.dataset.edit, del = e.target.dataset.del;
  if (del && confirm('이 항목을 삭제할까요?')) { await api('entries/' + del, { method: 'DELETE' }); toast('삭제했습니다'); loadEntries(); }
  if (ed) {
    const r = state.entries.find(x => x.id == ed), f = $('#entryForm');
    f.id.value = r.id; f.date.value = r.date; f.client.value = r.client_name || ''; f.item.value = r.item_name || '';
    f.qty.value = r.qty; f.unit_price.value = r.unit_price; f.supply.value = r.supply; f.vat.value = r.vat;
    f.vat_free.checked = !r.vat && r.supply > 0; f.paid.checked = !!r.paid; f.order_no.value = r.order_no || ''; f.memo.value = r.memo || '';
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }
};
async function markPaid(paid) {
  const ids = $$('.rowchk:checked').map(c => c.value);
  if (!ids.length) return toast('항목을 먼저 선택하세요', true);
  await api('entries?action=paid', { method: 'POST', body: { ids, paid } });
  toast(`${ids.length}건 변경했습니다`); loadEntries();
}
$('#markPaid').onclick = () => markPaid(true);
$('#markUnpaid').onclick = () => markPaid(false);
const exportCsv = type => { location.href = `/api/export?type=${type}&${period()}`; };
$('#entryExport').onclick = () => exportCsv(state.kind);

// ───────── 비용
function fillCategories() {
  $('#expCategory').innerHTML = (state.settings.expense_categories || '기타').split(',').map(c => c.trim()).filter(Boolean)
    .map(c => `<option>${esc(c)}</option>`).join('');
}
function resetExpenseForm() { const f = $('#expenseForm'); f.reset(); f.id.value = ''; f.date.value = iso(new Date()); }
$('#expenseReset').onclick = resetExpenseForm;
$('#expenseForm').onsubmit = async e => {
  e.preventDefault();
  const f = e.target, id = f.id.value;
  const body = Object.fromEntries(['date', 'category', 'description', 'amount', 'vat', 'memo'].map(k => [k, f[k].value]));
  await api('expenses' + (id ? '/' + id : ''), { method: id ? 'PUT' : 'POST', body });
  toast('저장했습니다'); resetExpenseForm(); loadExpenses();
};
async function loadExpenses() {
  fillCategories();
  if (!$('#expenseForm').date.value) resetExpenseForm();
  const list = await api('expenses?' + period());
  state.expenses = list;
  $('#expenseTotals').innerHTML = `${list.length}건 · 합계 <b>${won(list.reduce((a, r) => a + r.amount, 0))}</b>원`;
  table($('#expenseTable'), [{ h: '일자', k: 'date' }, { h: '계정', k: 'category' }, { h: '내용', k: 'description' },
    { h: '금액', n: 1, f: r => won(r.amount) }, { h: '부가세', n: 1, f: r => won(r.vat) }, { h: '비고', k: 'memo' },
    { h: '', cls: 'act', f: r => `<button data-edit="${r.id}">수정</button> <button class="danger" data-del="${r.id}">삭제</button>` }], list);
}
$('#expenseTable').onclick = async e => {
  const ed = e.target.dataset.edit, del = e.target.dataset.del;
  if (del && confirm('삭제할까요?')) { await api('expenses/' + del, { method: 'DELETE' }); loadExpenses(); }
  if (ed) {
    const r = state.expenses.find(x => x.id == ed), f = $('#expenseForm');
    ['id', 'date', 'category', 'description', 'amount', 'vat', 'memo'].forEach(k => f[k].value = r[k] ?? '');
  }
};
$('#expenseExport').onclick = () => exportCsv('expense');

// ───────── 품목 · 거래처 (공통 CRUD 폼)
function crudForm(form, resource, fields, reload) {
  const reset = () => { form.reset(); form.id.value = ''; };
  form.onsubmit = async e => {
    e.preventDefault();
    const id = form.id.value;
    const body = Object.fromEntries(fields.map(k => [k, form[k].value]));
    await api(resource + (id ? '/' + id : ''), { method: id ? 'PUT' : 'POST', body });
    toast('저장했습니다'); reset(); reload();
  };
  return {
    reset,
    edit: r => { form.id.value = r.id; fields.forEach(k => form[k].value = r[k] ?? ''); form.scrollIntoView({ behavior: 'smooth' }); },
  };
}
const productCrud = crudForm($('#productForm'), 'products', ['code', 'name', 'unit', 'sale_price', 'cost_price', 'memo'], () => loadProducts());
const clientCrud = crudForm($('#clientForm'), 'clients', ['name', 'type', 'biz_no', 'contact', 'memo'], () => loadClients());
$('#productReset').onclick = productCrud.reset;
$('#clientReset').onclick = clientCrud.reset;

async function loadProducts() {
  await loadMasters();
  table($('#productTable'), [{ h: '코드', k: 'code' }, { h: '품목명', k: 'name' }, { h: '단위', k: 'unit' },
    { h: '판매단가', n: 1, f: r => won(r.sale_price) },
    { h: '원가', n: 1, f: r => r.cost_price ? won(r.cost_price) : '<span class="tag no">미입력</span>' },
    { h: '마진/개', n: 1, f: r => r.cost_price ? won(r.sale_price - r.cost_price) : '-' },
    { h: '마진율', n: 1, f: r => r.cost_price && r.sale_price ? Math.round((r.sale_price - r.cost_price) / r.sale_price * 100) + '%' : '-' },
    { h: '비고', k: 'memo' },
    { h: '', cls: 'act', f: r => `<button data-edit="${r.id}">수정</button> <button class="danger" data-del="${r.id}">삭제</button>` }], state.products);
  $('#cpClient').innerHTML = state.clients.filter(c => c.type !== 'vendor').map(c => `<option value="${c.id}">${esc(c.name)}</option>`).join('');
  $('#cpProduct').innerHTML = state.products.map(p => `<option value="${p.id}">${esc(p.name)} (기본 ${won(p.sale_price)})</option>`).join('');
  const cps = await api('client-prices');
  table($('#cpTable'), [{ h: '거래처', k: 'client_name' }, { h: '품목', k: 'product_name' }, { h: '기본단가', n: 1, f: r => won(r.sale_price) },
    { h: '특별단가', n: 1, f: r => won(r.price) }, { h: '', cls: 'act', f: r => `<button class="danger" data-cpdel="${r.id}">삭제</button>` }], cps);
}
$('#productTable').onclick = async e => {
  const ed = e.target.dataset.edit, del = e.target.dataset.del;
  if (ed) productCrud.edit(state.products.find(x => x.id == ed));
  if (del && confirm('품목을 삭제할까요? (기존 매출 기록은 남습니다)')) { await api('products/' + del, { method: 'DELETE' }); loadProducts(); }
};
$('#cpForm').onsubmit = async e => {
  e.preventDefault(); const f = e.target;
  await api('client-prices', { method: 'POST', body: { client_id: f.client_id.value, product_id: f.product_id.value, price: f.price.value } });
  f.price.value = ''; toast('저장했습니다'); loadProducts();
};
$('#cpTable').onclick = async e => { if (e.target.dataset.cpdel) { await api('client-prices/' + e.target.dataset.cpdel, { method: 'DELETE' }); loadProducts(); } };

async function loadClients() {
  await loadMasters();
  const typ = { customer: '매출처', vendor: '매입처', both: '매출+매입' };
  table($('#clientTable'), [{ h: '상호', k: 'name' }, { h: '구분', f: r => typ[r.type] || r.type }, { h: '사업자번호', k: 'biz_no' },
    { h: '연락처', k: 'contact' }, { h: '미수금', n: 1, f: r => r.receivable ? `<span class="neg">${won(r.receivable)}</span>` : '-' }, { h: '비고', k: 'memo' },
    { h: '', cls: 'act', f: r => `<button data-edit="${r.id}">수정</button> <button class="danger" data-del="${r.id}">삭제</button>` }], state.clients);
}
$('#clientTable').onclick = async e => {
  const ed = e.target.dataset.edit, del = e.target.dataset.del;
  if (ed) clientCrud.edit(state.clients.find(x => x.id == ed));
  if (del && confirm('거래처를 삭제할까요? (기존 거래 기록은 남습니다)')) { await api('clients/' + del, { method: 'DELETE' }); loadClients(); }
};

// ───────── 자동 입력
const fileB64 = file => new Promise((ok, fail) => {
  const r = new FileReader();
  r.onload = () => ok(r.result.split(',')[1] || '');
  r.onerror = fail; r.readAsDataURL(file);
});
let pending = null;
$('#impFile').onchange = async () => {
  const file = $('#impFile').files[0];
  if (!file) return;
  pending = { filename: file.name, content_b64: await fileB64(file), kind: $('#impKind').value };
  previewImport();
};
$('#impKind').onchange = () => { if (pending) { pending.kind = $('#impKind').value; previewImport(); } };

async function previewImport(mapping) {
  const p = await api('import', { method: 'POST', body: { ...pending, preview: true, mapping } });
  const opts = sel => '<option value="">(사용 안 함)</option>' + p.headers.map((h, i) => `<option value="${i}" ${sel === i ? 'selected' : ''}>${esc(h || `${i + 1}열`)}</option>`).join('');
  const sampleCols = Object.keys(p.fields).filter(f => f in p.mapping);
  $('#impPreview').innerHTML = `
    <p>${p.rows}행을 찾았습니다. 열이 맞게 연결됐는지 확인하세요. ${esc(p.note)}</p>
    <div class="mapgrid">${Object.entries(p.fields).map(([f, l]) => `<label>${l}<select data-field="${f}">${opts(p.mapping[f])}</select></label>`).join('')}</div>
    <div class="scroll"><table class="list">
      <tr>${sampleCols.map(f => `<th>${p.fields[f]}</th>`).join('')}</tr>
      ${p.sample.map(r => `<tr>${sampleCols.map(f => `<td>${esc(r[f])}</td>`).join('')}</tr>`).join('')}
    </table></div>
    <div class="btns" style="margin-top:12px"><button class="primary" id="impCommit">${p.rows}행 ${pending.kind === 'sale' ? '매출' : '매입'}으로 가져오기</button></div>`;
  $$('#impPreview select').forEach(s => s.onchange = () => previewImport(currentMapping()));
  $('#impCommit').onclick = async () => {
    const r = await api('import', { method: 'POST', body: { ...pending, mapping: currentMapping() } });
    const errs = r.errors.length ? `<br>건너뛴 행: ${r.errors.slice(0, 10).map(x => `${x.row}행(${esc(x.error)})`).join(', ')}${r.errors.length > 10 ? ' …' : ''}` : '';
    $('#impPreview').innerHTML = `<p><b>${r.inserted}건</b> 새로 입력 · 중복 ${r.duplicates}건 제외 · 오류 ${r.errors.length}건${errs}</p>`;
    $('#impFile').value = ''; pending = null; loadImportLog();
    toast(`${r.inserted}건 입력 완료`);
  };
}
const currentMapping = () => Object.fromEntries($$('#impPreview select').filter(s => s.value !== '').map(s => [s.dataset.field, Number(s.value)]));

$('#scanNow').onclick = async () => {
  const r = await api('scan-inbox', { method: 'POST' });
  toast(r.length ? r.map(x => `${x.file}: ${x.error ? '오류' : x.inserted + '건'}`).join(' / ') : '새 파일이 없습니다');
  loadImportLog();
};
async function loadImportLog() {
  const log = await api('import-log');
  const src = { import: '파일 올리기', folder: '폴더 자동', api: 'API' };
  table($('#importLog'), [{ h: '시각', k: 'created_at' }, { h: '경로', f: r => src[r.source] || esc(r.source) }, { h: '파일/출처', k: 'filename' },
    { h: '구분', f: r => r.kind === 'sale' ? '매출' : '매입' }, { h: '입력', k: 'inserted', n: 1 }, { h: '중복', k: 'duplicates', n: 1 },
    { h: '오류', k: 'errors', n: 1 }, { h: '메시지', k: 'message' }], log);
}
async function loadImport() {
  state.settings = await api('settings');
  $('#apiExample').textContent =
`POST ${location.origin}/api/ingest
X-API-Key: ${state.settings.api_key}
Content-Type: application/json

{ "kind": "sale",
  "rows": [
    { "date": "${iso(new Date())}", "client": "OO상사", "item": "딸깍키트",
      "qty": 10, "unit_price": 15000, "order_no": "B2B-0001" }
  ] }

# 단가를 빼면 등록된 단가표로 자동 계산, 같은 주문번호는 중복 등록되지 않습니다.`;
  loadImportLog();
}

// ───────── 설정
async function loadSettings() {
  state.settings = await api('settings');
  const f = $('#settingsForm');
  ['company_name', 'vat_rate', 'cost_mode', 'expense_categories'].forEach(k => f[k].value = state.settings[k] || '');
  $('#apiKey').textContent = state.settings.api_key;
}
$('#settingsForm').onsubmit = async e => {
  e.preventDefault(); const f = e.target;
  state.settings = await api('settings', { method: 'PUT', body: Object.fromEntries(['company_name', 'vat_rate', 'cost_mode', 'expense_categories'].map(k => [k, f[k].value])) });
  $('#companyName').textContent = state.settings.company_name; toast('저장했습니다');
};
$('#regenKey').onclick = async () => {
  if (!confirm('API 키를 바꾸면 기존 연동이 끊깁니다. 계속할까요?')) return;
  state.settings = await api('settings', { method: 'PUT', body: { regenerate_api_key: true } });
  $('#apiKey').textContent = state.settings.api_key;
};
$$('[data-export]').forEach(b => b.onclick = () => exportCsv(b.dataset.export));
$('#backup').onclick = () => { location.href = '/api/backup'; };

// ───────── 시작
(async () => {
  state.settings = await api('settings');
  $('#companyName').textContent = state.settings.company_name || '회사';
  fillCategories();
  setRange('month');
})();
