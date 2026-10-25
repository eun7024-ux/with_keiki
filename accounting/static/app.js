'use strict';
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const won = n => (Math.round(Number(n) || 0)).toLocaleString('ko-KR');
const num = v => { const s = String(v ?? '').replace(/[^0-9.\-]/g, ''); return s === '' ? null : Number(s); };
const iso = d => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10);

const state = { tab: 'dashboard', kind: 'sale', okind: 'sales', settings: {}, clients: [], products: [], entries: [] };
const SETTING_KEYS = ['company_name', 'vat_rate', 'cost_mode', 'expense_categories', 'biz_no', 'ceo', 'address', 'phone', 'biz_type', 'bank_account', 'doc_note'];
const KIND_LABEL = { product: '완제품', goods: '상품', material: '자재' };
const openPrint = path => window.open('/print/' + path, '_blank');

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
  const sec = (tab === 'sale' || tab === 'purchase') ? 'entries' : (tab === 'pos' ? 'orders' : tab);
  $$('.tab').forEach(s => s.classList.toggle('on', s.id === 'tab-' + sec));
  if (sec === 'entries') { state.kind = tab; setupEntryLabels(); resetEntryForm(); }
  if (sec === 'orders') { state.okind = tab === 'pos' ? 'purchase' : 'sales'; setupOrderLabels(); }
  $('.period').style.display = ['products', 'settings', 'import', 'stock'].includes(tab) ? 'none' : '';
  refresh();
}
$$('#tabs button').forEach(b => b.onclick = () => showTab(b.dataset.tab));

async function refresh() {
  const t = state.tab;
  if (t === 'dashboard') return loadDashboard();
  if (t === 'sale' || t === 'purchase') return loadEntries();
  if (t === 'orders' || t === 'pos') return loadOrders();
  if (t === 'stock') return loadStock();
  if (t === 'production') return loadProduction();
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
  const todo = (tab, l, v, sub, alert) => `<button type="button" data-goto="${tab}" class="${alert ? 'alert' : ''}"><span class="l">${l}</span><span class="v">${v}</span><span class="s">${sub}</span></button>`;
  $('#todo').innerHTML =
    todo('orders', '출고 대기 주문', `${s.open_orders}건`, `${won(s.open_orders_amount)}원${s.due_today ? ` · 오늘 납기 ${s.due_today}건` : ''}`, false) +
    todo('orders', '납기 지난 주문', `${s.overdue_orders}건`, s.overdue_orders ? '바로 확인하세요' : '없음', s.overdue_orders > 0) +
    todo('pos', '입고 예정 발주', `${s.open_pos}건`, `${won(s.open_pos_amount)}원`, false) +
    todo('stock', '재고 부족 품목', `${s.low_stock.length}개`, s.low_stock.slice(0, 3).map(x => esc(x.name)).join(', ') || '없음', s.low_stock.length > 0) +
    todo('stock', '재고 자산 (원가 기준)', `${won(s.inventory_value)}원`, '현재고 × 원가', false);
  $$('#todo [data-goto]').forEach(b => b.onclick = () => showTab(b.dataset.goto));
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
  // 0 기준선 가까이 붙는 눈금 글자는 겹치므로 뺀다
  const ticks = [0, max, (max + min) / 2, min].filter((v, i, a) => a.indexOf(v) === i && (v === 0 || Math.abs(y(v) - y(0)) > 14));
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
    { h: '경로', f: r => `<span class="tag">${{ manual: '수기', import: '파일', folder: '폴더', api: 'API', order: '주문' }[r.source] || esc(r.source)}</span>` },
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
const productCrud = crudForm($('#productForm'), 'products', ['code', 'name', 'unit', 'kind', 'sale_price', 'cost_price', 'safety_stock', 'memo'], () => loadProducts());
const clientCrud = crudForm($('#clientForm'), 'clients', ['name', 'type', 'biz_no', 'contact', 'memo'], () => loadClients());
$('#productReset').onclick = productCrud.reset;
$('#clientReset').onclick = clientCrud.reset;

async function loadProducts() {
  await loadMasters();
  table($('#productTable'), [{ h: '코드', k: 'code' }, { h: '품목명', k: 'name' }, { h: '구분', f: r => KIND_LABEL[r.kind] || '' }, { h: '단위', k: 'unit' },
    { h: '판매단가', n: 1, f: r => won(r.sale_price) },
    { h: '원가', n: 1, f: r => r.cost_price ? won(r.cost_price) : '<span class="tag no">미입력</span>' },
    { h: '마진/개', n: 1, f: r => r.cost_price ? won(r.sale_price - r.cost_price) : '-' },
    { h: '마진율', n: 1, f: r => r.cost_price && r.sale_price ? Math.round((r.sale_price - r.cost_price) / r.sale_price * 100) + '%' : '-' },
    { h: 'BOM 원가', n: 1, f: r => r.bom_cost ? won(r.bom_cost) : '' }, { h: '안전재고', n: 1, f: r => r.safety_stock ? won(r.safety_stock) : '' },
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
    { h: '', cls: 'act', f: r => (r.type !== 'vendor' ? `<button data-stmt="${r.id}" title="${$('#from').value} ~ ${$('#to').value}">거래내역서</button> ` : '') +
      `<button data-edit="${r.id}">수정</button> <button class="danger" data-del="${r.id}">삭제</button>` }], state.clients);
}
$('#clientTable').onclick = async e => {
  const ed = e.target.dataset.edit, del = e.target.dataset.del;
  if (e.target.dataset.stmt) openPrint(`statement?client_id=${e.target.dataset.stmt}&${period()}`);
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
    <div class="btns" style="margin-top:12px"><button class="primary" id="impCommit">${p.rows}행 ${{ order: '주문', sale: '매출', purchase: '매입' }[pending.kind]}으로 가져오기</button></div>`;
  $$('#impPreview select').forEach(s => s.onchange = () => previewImport(currentMapping()));
  $('#impCommit').onclick = async () => {
    const r = await api('import', { method: 'POST', body: { ...pending, mapping: currentMapping() } });
    const errs = r.errors.length ? `<br>건너뛴 행: ${r.errors.slice(0, 10).map(x => `${x.row}행(${esc(x.error)})`).join(', ')}${r.errors.length > 10 ? ' …' : ''}` : '';
    const unit = pending.kind === 'order' ? '주문' : '건';
    $('#impPreview').innerHTML = `<p><b>${r.inserted}${unit === '주문' ? '건의 주문' : '건'}</b> 새로 입력 · 중복 ${r.duplicates}건 제외 · 오류 ${r.errors.length}건${errs}</p>` +
      (pending.kind === 'order' && r.inserted ? '<p class="hint">[주문·출고] 화면에서 출고 처리하면 매출로 확정됩니다.</p>' : '');
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
    { h: '구분', f: r => ({ sale: '매출', purchase: '매입', order: '주문' }[r.kind] || r.kind) }, { h: '입력', k: 'inserted', n: 1 }, { h: '중복', k: 'duplicates', n: 1 },
    { h: '오류', k: 'errors', n: 1 }, { h: '메시지', k: 'message' }], log);
}
async function loadImport() {
  state.settings = await api('settings');
  $('#apiExample').textContent =
`POST ${location.origin}/api/ingest
X-API-Key: ${state.settings.api_key}
Content-Type: application/json

{ "kind": "order",
  "rows": [
    { "date": "${iso(new Date())}", "client": "OO상사", "item": "딸깍키트",
      "qty": 10, "unit_price": 15000, "order_no": "B2B-0001" }
  ] }

# kind: order = 주문(출고 대기), sale = 매출(바로 확정), purchase = 매입
# 단가를 빼면 등록된 단가표로 자동 계산, 같은 주문번호는 중복 등록되지 않습니다.`;
  loadImportLog();
}

// ───────── 설정
async function loadSettings() {
  state.settings = await api('settings');
  const f = $('#settingsForm');
  SETTING_KEYS.forEach(k => f[k].value = state.settings[k] || '');
  $('#apiKey').textContent = state.settings.api_key;
}
$('#settingsForm').onsubmit = async e => {
  e.preventDefault(); const f = e.target;
  state.settings = await api('settings', { method: 'PUT', body: Object.fromEntries(SETTING_KEYS.map(k => [k, f[k].value])) });
  $('#companyName').textContent = state.settings.company_name; toast('저장했습니다');
};
$('#regenKey').onclick = async () => {
  if (!confirm('API 키를 바꾸면 기존 연동이 끊깁니다. 계속할까요?')) return;
  state.settings = await api('settings', { method: 'PUT', body: { regenerate_api_key: true } });
  $('#apiKey').textContent = state.settings.api_key;
};
$$('[data-export]').forEach(b => b.onclick = () => exportCsv(b.dataset.export));
$('#backup').onclick = () => { location.href = '/api/backup'; };


// ───────── 주문·출고 / 발주·입고
const ORDER_TXT = {
  sales: { title: '주문', client: '거래처', due: '납기(출고 예정)', done: '출고', doneDate: '출고일', paid: '입금완료', open: 'received', doc: '거래명세서', quoteDoc: '견적서' },
  purchase: { title: '발주', client: '매입처', due: '입고 예정일', done: '입고', doneDate: '입고일', paid: '지급완료', open: 'ordered', doc: '발주서' },
};
const STATUS = { quote: ['견적', 'warn'], received: ['출고 대기', ''], ordered: ['입고 대기', ''], done: ['완료', 'ok'], cancelled: ['취소', 'done'] };
state.ofilter = 'open';
function setupOrderLabels() {
  const t = ORDER_TXT[state.okind];
  $('#newOrder').textContent = `+ 새 ${t.title}`;
  $('#oClientLabel').textContent = t.client; $('#oDueLabel').textContent = t.due;
  $('#oStatusWrap').hidden = state.okind !== 'sales';
  $('#orderEditor').hidden = true; $('#completePanel').hidden = true;
  const chips = [['open', '진행 중'], ...(state.okind === 'sales' ? [['quote', '견적']] : []), ['done', '완료'], ['cancelled', '취소'], ['', '기간 전체']];
  if (!chips.some(c => c[0] === state.ofilter)) state.ofilter = 'open';
  $('#orderFilter').innerHTML = chips.map(([v, l]) => `<button type="button" data-f="${v}" class="${state.ofilter === v ? 'on' : ''}">${l}</button>`).join('');
}
$('#orderFilter').onclick = e => {
  const b = e.target.closest('[data-f]'); if (!b) return;
  state.ofilter = b.dataset.f; $$('#orderFilter button').forEach(x => x.classList.toggle('on', x === b)); loadOrders();
};
$('#orderSearch').oninput = () => { clearTimeout(state._oh); state._oh = setTimeout(loadOrders, 250); };

async function loadOrders() {
  await loadMasters();
  const t = ORDER_TXT[state.okind];
  const q = encodeURIComponent($('#orderSearch').value.trim());
  const list = await api(`orders?kind=${state.okind}&status=${state.ofilter}&${period()}&q=${q}`);
  state.orders = list;
  $('#orderTotals').innerHTML = `${list.length}건 · 공급가 <b>${won(list.reduce((a, o) => a + o.supply, 0))}</b> · 합계 <b>${won(list.reduce((a, o) => a + o.total, 0))}</b>` +
    (state.ofilter === 'open' ? ' <span class="hint">(진행 중인 건은 기간과 관계없이 모두 표시)</span>' : '');
  table($('#orderTable'), [
    { h: '번호', k: 'order_no' }, { h: '일자', k: 'date' }, { h: t.client, k: 'client_name' },
    { h: '품목', f: r => esc(r.first_item || '') + (r.line_count > 1 ? ` <span class="hint">외 ${r.line_count - 1}건</span>` : '') },
    { h: '수량', n: 1, f: r => won(r.qty) }, { h: '공급가', n: 1, f: r => won(r.supply) }, { h: '합계', n: 1, f: r => won(r.total) },
    { h: t.due.split('(')[0], f: r => r.status === 'done' ? `<span class="hint">${esc(r.done_date)} ${t.done}</span>` : esc(r.due_date || '-'), cls: '' },
    { h: '상태', f: r => { const [l, c] = STATUS[r.status] || [r.status, '']; return `<span class="tag ${r.overdue ? 'no' : c}">${r.overdue ? '납기 지남' : l}</span>`; } },
    { h: '송장', f: r => esc([r.courier, r.tracking_no].filter(Boolean).join(' ')) },
    { h: '', cls: 'act', f: r => {
      const b = [];
      if (r.status === 'quote') b.push(`<button data-a="convert" data-id="${r.id}">주문으로 전환</button>`);
      if (r.status === t.open) b.push(`<button class="primary" data-a="complete" data-id="${r.id}">${t.done} 처리</button>`);
      b.push(`<button data-a="print" data-id="${r.id}">${r.status === 'quote' ? t.quoteDoc : t.doc}</button>`);
      if (r.status === 'done') b.push(`<button data-a="reopen" data-id="${r.id}">${t.done} 취소</button>`);
      else {
        b.push(`<button data-a="edit" data-id="${r.id}">수정</button>`);
        if (r.status !== 'cancelled') b.push(`<button class="danger" data-a="cancel" data-id="${r.id}">취소</button>`);
        else b.push(`<button class="danger" data-a="delete" data-id="${r.id}">삭제</button>`);
      }
      return b.join(' ');
    } }], list, state.ofilter === 'open' ? `진행 중인 ${t.title}이 없습니다` : '자료가 없습니다');
  $$('#orderTable tr').forEach((tr, i) => { const o = list[i - 1]; if (o && o.overdue) tr.children[7].classList.add('late'); });
}

$('#orderTable').onclick = async e => {
  const b = e.target.closest('[data-a]'); if (!b) return;
  const id = b.dataset.id, a = b.dataset.a, t = ORDER_TXT[state.okind];
  if (a === 'print') return openPrint('order/' + id);
  if (a === 'edit') return openOrderEditor(await api('orders/' + id));
  if (a === 'complete') return openComplete(state.orders.find(o => o.id == id));
  if (a === 'convert') { await api(`orders/${id}?action=convert`, { method: 'POST', body: {} }); toast('주문으로 전환했습니다'); }
  if (a === 'cancel' && confirm('이 건을 취소할까요?')) { await api(`orders/${id}?action=status`, { method: 'POST', body: { status: 'cancelled' } }); toast('취소했습니다'); }
  if (a === 'delete' && confirm('완전히 삭제할까요?')) { await api('orders/' + id, { method: 'DELETE' }); toast('삭제했습니다'); }
  if (a === 'reopen' && confirm(`${t.done}을 취소하면 ${state.okind === 'sales' ? '매출' : '매입'} 기록이 지워지고 재고가 되돌아갑니다. 계속할까요?`)) {
    await api(`orders/${id}?action=reopen`, { method: 'POST', body: {} }); toast(`${t.done}을 취소했습니다`);
  }
  loadOrders();
};

// 주문서 편집
function lineRow(ln = {}) {
  return `<tr><td class="item"><input class="li-item" list="productList" value="${esc(ln.item_name || '')}" placeholder="품목명" autocomplete="off"></td>
    <td class="q"><input class="li-qty" inputmode="decimal" value="${ln.qty ?? 1}"></td>
    <td class="p"><input class="li-price" inputmode="numeric" value="${ln.unit_price ?? ''}" placeholder="자동"></td>
    <td class="n li-supply"></td><td class="n li-stock"></td><td><button type="button" class="x" title="줄 삭제">×</button></td></tr>`;
}
function openOrderEditor(o) {
  const t = ORDER_TXT[state.okind], f = $('#orderForm');
  $('#completePanel').hidden = true;
  state.stock = null;
  f.reset();
  f.id.value = o ? o.id : '';
  $('#oDate').value = o ? o.date : iso(new Date());
  $('#oClient').value = o ? (o.client_name || '') : '';
  $('#oDue').value = o ? (o.due_date || '') : '';
  $('#oStatus').value = o && o.status === 'quote' ? 'quote' : 'received';
  $('#oNo').value = o ? o.order_no : ''; $('#oNo').disabled = !!o;
  $('#oMemo').value = o ? (o.memo || '') : '';
  $('#orderEditorTitle').textContent = o ? `${t.title} 수정 · ${o.order_no}` : `새 ${t.title}`;
  $('#oLines').innerHTML = `<tr><th>품목</th><th>수량</th><th>단가</th><th class="n">공급가액</th><th class="n">${state.okind === 'sales' ? '가용 재고' : '현재고'}</th><th></th></tr>` +
    (o ? o.lines.map(lineRow).join('') : lineRow() + lineRow());
  $('#orderEditor').hidden = false;
  updateLines(true);
  $('#orderEditor').scrollIntoView({ behavior: 'smooth' });
}
$('#newOrder').onclick = () => openOrderEditor(null);
$('#oCancel').onclick = () => { $('#orderEditor').hidden = true; };
$('#oAddLine').onclick = () => { $('#oLines').insertAdjacentHTML('beforeend', lineRow()); };
$('#oLines').addEventListener('click', e => { if (e.target.classList.contains('x')) { e.target.closest('tr').remove(); updateLines(); } });
$('#oLines').addEventListener('input', e => { if (!e.target.classList.contains('li-item')) updateLines(); });
$('#oLines').addEventListener('change', e => { if (e.target.classList.contains('li-item')) updateLines(true); });
$('#oClient').addEventListener('change', () => updateLines(true));

async function updateLines(lookup) {
  if (lookup) { if (!state.stock) state.stock = await api('stock'); }
  const c = state.clients.find(x => x.name === $('#oClient').value.trim());
  const ek = state.okind === 'sales' ? 'sale' : 'purchase';
  let sum = 0;
  for (const tr of $$('#oLines tr').slice(1)) {
    const name = $('.li-item', tr).value.trim(), p = state.products.find(x => x.name === name);
    const priceIn = $('.li-price', tr);
    if (lookup && p && !priceIn.value) {
      const r = await api(`price?kind=${ek}&product_id=${p.id}&client_id=${c ? c.id : ''}`);
      priceIn.placeholder = r.price ? won(r.price) : '단가 입력';
      priceIn.dataset.auto = r.price || '';
    } else if (!p) { priceIn.placeholder = '자동'; priceIn.dataset.auto = ''; }
    const qty = num($('.li-qty', tr).value) || 0, price = num(priceIn.value) ?? (Number(priceIn.dataset.auto) || 0);
    const supply = Math.round(qty * price); sum += supply;
    $('.li-supply', tr).textContent = name ? won(supply) : '';
    const st = p && (state.stock || []).find(x => x.id === p.id);
    const avail = st ? (state.okind === 'sales' ? st.available : st.onhand) : null;
    $('.li-stock', tr).innerHTML = st ? `<span class="${state.okind === 'sales' && avail < qty ? 'neg' : ''}">${won(avail)} ${esc(st.unit || '')}</span>` : '';
  }
  const rate = Number(state.settings.vat_rate || 10) / 100;
  $('#oTotal').innerHTML = `공급가 <b>${won(sum)}</b> · 부가세 ${won(sum * rate)} · 합계 <b>${won(sum * (1 + rate))}</b>`;
}

$('#orderForm').onsubmit = async e => {
  e.preventDefault();
  const id = $('#orderForm').id.value;
  const lines = $$('#oLines tr').slice(1).map(tr => {
    const name = $('.li-item', tr).value.trim(), p = state.products.find(x => x.name === name);
    const ln = { item: name, qty: $('.li-qty', tr).value };
    if (p) ln.product_id = p.id;
    if ($('.li-price', tr).value !== '') ln.unit_price = $('.li-price', tr).value;
    return ln;
  }).filter(l => l.item);
  const body = { kind: state.okind, date: $('#oDate').value, client: $('#oClient').value, due_date: $('#oDue').value, memo: $('#oMemo').value, lines,
    status: state.okind === 'sales' ? $('#oStatus').value : 'ordered' };
  if (!id && $('#oNo').value.trim()) body.order_no = $('#oNo').value.trim();
  await api('orders' + (id ? '/' + id : ''), { method: id ? 'PUT' : 'POST', body });
  toast('저장했습니다'); $('#orderEditor').hidden = true; state.stock = null; loadOrders();
};

function openComplete(o) {
  const t = ORDER_TXT[state.okind];
  $('#orderEditor').hidden = true;
  $('#completeForm').id.value = o.id;
  $('#completeTitle').textContent = `${t.done} 처리 · ${o.order_no} · ${o.client_name || ''}`;
  $('#cDateLabel').textContent = t.doneDate; $('#cDate').value = iso(new Date());
  $('#cCourierWrap').hidden = $('#cTrackWrap').hidden = state.okind !== 'sales';
  $('#cCourier').value = ''; $('#cTrack').value = ''; $('#cPaid').checked = false;
  $('#cPaidLabel').textContent = t.paid; $('#cSubmit').textContent = `${t.done} 완료`;
  $('#completeHint').textContent = state.okind === 'sales'
    ? `완료하면 ${won(o.supply)}원(공급가)이 매출로 기록되고 재고에서 빠집니다.`
    : `완료하면 ${won(o.supply)}원(공급가)이 매입으로 기록되고 재고가 늘어납니다.`;
  $('#completePanel').hidden = false;
  $('#completePanel').scrollIntoView({ behavior: 'smooth' });
}
$('#cCancel').onclick = () => { $('#completePanel').hidden = true; };
$('#completeForm').onsubmit = async e => {
  e.preventDefault();
  const id = $('#completeForm').id.value;
  await api(`orders/${id}?action=complete`, { method: 'POST', body: { date: $('#cDate').value, courier: $('#cCourier').value, tracking_no: $('#cTrack').value, paid: $('#cPaid').checked } });
  toast(`${ORDER_TXT[state.okind].done} 완료`); $('#completePanel').hidden = true; state.stock = null; loadOrders();
};

// ───────── 재고
state.sfilter = '';
$('#stockFilter').innerHTML = [['', '전체'], ['low', '부족'], ['product', '완제품'], ['goods', '상품'], ['material', '자재']]
  .map(([v, l]) => `<button type="button" data-f="${v}" class="${v === '' ? 'on' : ''}">${l}</button>`).join('');
$('#stockFilter').onclick = e => {
  const b = e.target.closest('[data-f]'); if (!b) return;
  state.sfilter = b.dataset.f; $$('#stockFilter button').forEach(x => x.classList.toggle('on', x === b)); renderStock();
};
$('#stockSearch').oninput = () => renderStock();
async function loadStock() { state.stock = await api('stock'); renderStock(); }
function renderStock() {
  const f = state.sfilter, kw = $('#stockSearch').value.trim();
  const list = state.stock.filter(s => (!f || (f === 'low' ? ['low', 'negative'].includes(s.state) : s.kind === f)) && (!kw || s.name.includes(kw) || (s.code || '').includes(kw)));
  const lowN = state.stock.filter(s => ['low', 'negative'].includes(s.state)).length;
  $('#stockFilter [data-f="low"]').innerHTML = `부족${lowN ? `<b>${lowN}</b>` : ''}`;
  $('#stockTotals').innerHTML = `재고 자산 <b>${won(state.stock.reduce((a, s) => a + s.value, 0))}</b>원`;
  const chip = { ok: ['정상', 'ok'], watch: ['주의', 'warn'], low: ['부족', 'no'], negative: ['마이너스', 'no'] };
  table($('#stockTable'), [
    { h: '구분', f: r => KIND_LABEL[r.kind] || '' }, { h: '코드', k: 'code' }, { h: '품목', k: 'name' },
    { h: '현재고', n: 1, f: r => `<b class="${r.onhand < 0 ? 'neg' : ''}">${won(r.onhand)}</b> ${esc(r.unit || '')}` },
    { h: '출고 대기', n: 1, f: r => r.reserved ? won(r.reserved) : '' }, { h: '가용', n: 1, f: r => won(r.available) },
    { h: '입고 예정', n: 1, f: r => r.incoming ? won(r.incoming) : '' }, { h: '안전재고', n: 1, f: r => r.safety_stock ? won(r.safety_stock) : '' },
    { h: '상태', f: r => `<span class="tag ${chip[r.state][1]}">${chip[r.state][0]}</span>` },
    { h: '재고금액', n: 1, f: r => won(r.value) },
    { h: '', cls: 'act', f: r => `<button data-adj="${r.id}">실사</button> <button data-hist="${r.id}">내역</button>` }], list, '품목이 없습니다. [품목·단가]에서 먼저 등록하세요.');
}
$('#stockTable').onclick = async e => {
  const adj = e.target.dataset.adj, hist = e.target.dataset.hist;
  if (adj) {
    const s = state.stock.find(x => x.id == adj);
    $('#adjustForm').product_id.value = s.id; $('#adjName').textContent = s.name;
    $('#adjCurrent').value = `${won(s.onhand)} ${s.unit || ''}`; $('#adjActual').value = ''; $('#adjDate').value = iso(new Date()); $('#adjMemo').value = '';
    $('#adjustPanel').hidden = false; $('#adjActual').focus();
  }
  if (hist) {
    const s = state.stock.find(x => x.id == hist), h = await api(`stock/${hist}/history`);
    $('#histTitle').textContent = `입출고 내역 · ${s.name}`;
    table($('#histTable'), [{ h: '일자', k: 'date' }, { h: '구분', k: 'reason' }, { h: '거래처', k: 'party' }, { h: '번호', k: 'ref' },
      { h: '수량', n: 1, f: r => `<span class="${r.qty < 0 ? 'neg' : 'pos'}">${r.qty > 0 ? '+' : ''}${won(r.qty)}</span>` },
      { h: '잔량', n: 1, f: r => won(r.balance) }, { h: '메모', k: 'memo' }], h, '입출고 기록이 없습니다');
    $('#historyPanel').hidden = false; $('#historyPanel').scrollIntoView({ behavior: 'smooth' });
  }
};
$('#adjCancel').onclick = () => { $('#adjustPanel').hidden = true; };
$('#histClose').onclick = () => { $('#historyPanel').hidden = true; };
$('#adjustForm').onsubmit = async e => {
  e.preventDefault(); const f = e.target;
  const r = await api('stock?action=adjust', { method: 'POST', body: { product_id: f.product_id.value, actual: $('#adjActual').value, date: $('#adjDate').value, memo: $('#adjMemo').value } });
  toast(r.diff ? `재고를 ${r.diff > 0 ? '+' : ''}${won(r.diff)} 조정했습니다` : '전산 재고와 같습니다');
  $('#adjustPanel').hidden = true; loadStock();
};

// ───────── 생산 / BOM
async function loadProduction() {
  await loadMasters();
  const finished = state.products.filter(p => p.kind !== 'material'), mats = state.products.filter(p => p.kind === 'material');
  const keep = (sel, list) => { const v = sel.value; sel.innerHTML = list.map(p => `<option value="${p.id}">${esc(p.name)}</option>`).join(''); if (list.some(p => p.id == v)) sel.value = v; };
  keep($('#bomProduct'), finished); keep($('#pProduct'), finished);
  keep($('#bomMaterial'), mats.length ? mats : state.products);
  if (!$('#pDate').value) $('#pDate').value = iso(new Date());
  await Promise.all([loadBom(), loadPlan()]);
  const list = await api('productions?' + period());
  table($('#prodTable'), [{ h: '일자', k: 'date' }, { h: '완제품', k: 'product_name' }, { h: '수량', n: 1, f: r => `${won(r.qty)} ${esc(r.unit || '')}` },
    { h: '투입 자재', f: r => r.materials.map(m => `${esc(m.name)} ${won(m.qty)}`).join(', ') || '<span class="hint">BOM 없음</span>' },
    { h: '메모', k: 'memo' }, { h: '', cls: 'act', f: r => `<button class="danger" data-pdel="${r.id}">삭제</button>` }], list, '선택한 기간에 생산 기록이 없습니다');
}
async function loadBom() {
  const pid = $('#bomProduct').value;
  if (!pid) { $('#bomTable').innerHTML = '<tr><td class="empty">[품목·단가]에서 완제품과 자재를 먼저 등록하세요 (구분: 자재·부자재)</td></tr>'; $('#bomCost').textContent = ''; return; }
  const lines = await api('bom?product_id=' + pid);
  table($('#bomTable'), [{ h: '자재', k: 'material_name' }, { h: '1개당', n: 1, f: r => `${won(r.qty)} ${esc(r.unit || '')}` },
    { h: '자재 단가', n: 1, f: r => won(r.cost_price) }, { h: '금액', n: 1, f: r => won(r.qty * r.cost_price) }, { h: '현재고', n: 1, f: r => won(r.onhand) },
    { h: '', cls: 'act', f: r => `<button class="danger" data-bdel="${r.id}">삭제</button>` }], lines, '아직 BOM이 없습니다. 아래에서 자재를 추가하세요.');
  const cost = lines.reduce((a, r) => a + r.qty * r.cost_price, 0), p = state.products.find(x => x.id == pid);
  $('#bomCost').innerHTML = lines.length ? `BOM 기준 원가 <b>${won(cost)}</b>원 / 등록된 원가 ${won(p.cost_price)}원 ` +
    (Math.round(cost) !== p.cost_price ? `<button type="button" id="applyCost">원가를 ${won(cost)}원으로 바꾸기</button>` : '') : '';
  const ap = $('#applyCost');
  if (ap) ap.onclick = async () => { await api('products/' + p.id, { method: 'PUT', body: { ...p, cost_price: Math.round(cost) } }); toast('원가를 바꿨습니다'); loadProduction(); };
}
async function loadPlan() {
  const pid = $('#pProduct').value, qty = num($('#pQty').value) || 0;
  if (!pid || !qty) { $('#planTable').innerHTML = ''; return; }
  const plan = await api(`productions?plan=1&product_id=${pid}&qty=${qty}`);
  table($('#planTable'), [{ h: '필요 자재', k: 'name' }, { h: '필요량', n: 1, f: r => won(r.need) }, { h: '현재고', n: 1, f: r => won(r.onhand) },
    { h: '부족', n: 1, f: r => r.short ? `<span class="neg">${won(r.short)}</span>` : '<span class="tag ok">충분</span>' }], plan, 'BOM이 없어 자재는 차감되지 않습니다');
}
$('#bomProduct').onchange = loadBom;
$('#pProduct').onchange = loadPlan;
$('#pQty').oninput = () => { clearTimeout(state._ph); state._ph = setTimeout(loadPlan, 250); };
$('#bomForm').onsubmit = async e => {
  e.preventDefault();
  await api('bom', { method: 'POST', body: { product_id: $('#bomProduct').value, material_id: $('#bomMaterial').value, qty: $('#bomQty').value } });
  $('#bomQty').value = ''; loadBom(); loadPlan();
};
$('#bomTable').onclick = async e => { if (e.target.dataset.bdel) { await api('bom/' + e.target.dataset.bdel, { method: 'DELETE' }); loadBom(); loadPlan(); } };
$('#prodForm').onsubmit = async e => {
  e.preventDefault();
  const r = await api('productions', { method: 'POST', body: { date: $('#pDate').value, product_id: $('#pProduct').value, qty: $('#pQty').value, memo: $('#pMemo').value } });
  toast(r.short.length ? `등록했습니다. 자재 부족: ${r.short.map(s => s.name).join(', ')} (재고가 마이너스가 됩니다)` : '생산을 등록했습니다', r.short.length > 0);
  $('#pQty').value = ''; $('#pMemo').value = ''; loadProduction();
};
$('#prodTable').onclick = async e => {
  if (e.target.dataset.pdel && confirm('생산 기록을 지우면 재고도 되돌아갑니다. 계속할까요?')) { await api('productions/' + e.target.dataset.pdel, { method: 'DELETE' }); loadProduction(); }
};

// ───────── 시작
(async () => {
  state.settings = await api('settings');
  $('#companyName').textContent = state.settings.company_name || '회사';
  fillCategories();
  setRange('month');
})();
