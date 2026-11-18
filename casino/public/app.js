'use strict';
/* РофлКазино — клиент */
const socket = io();
const $ = (s, el = document) => el.querySelector(s);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const r2 = (x) => Math.round(x * 100) / 100;
const rub = (x) => r2(x).toLocaleString('ru-RU', { maximumFractionDigits: 2 }) + ' ₽';
const range = (a, b) => Array.from({ length: b - a + 1 }, (_, i) => a + i);
const ls = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* приватный режим */ } },
};

const GROWTH = 0.00009;
const multAt = (ms) => (ms <= 0 ? 1 : Math.floor(100 * Math.exp(GROWTH * ms)) / 100);
const timeFor = (m) => Math.log(m) / GROWTH;
const CASE_SPIN = 5200;
const ROUL_DUR = { betting: 15000, spinning: 7000, result: 4000 };
const PLANE_BET = 8000;
const WHEEL = [0, 32, 15, 19, 4, 21, 2, 25, 17, 34, 6, 27, 13, 36, 11, 30, 8, 23, 10, 5, 24, 16, 33, 1, 20, 14, 31, 9, 22, 18, 29, 7, 28, 12, 35, 3, 26];
const RED = new Set([1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]);
const colorOf = (n) => (n === 0 ? 'green' : RED.has(n) ? 'red' : 'black');
const GAME_TITLE = { crash: 'Краш 1×1', mines: 'Мины', towers: 'Башни', cases: 'Кейс-батл' };

const S = {
  me: null, players: [], duels: [], cases: [], towerDiff: {}, towerRows: 8,
  plane: null, roul: null, minesSolo: null, minesLast: null, towersSolo: null, towersLast: null,
  offset: null, tab: ls.get('rc_tab') || 'crash', view: {},
  mode: { mines: 'solo', towers: 'solo', cases: 'open' },
  selCase: 'bomzh', chip: 100, towersDiff: 'medium', built: false,
  prevDuels: new Map(), battleAnim: {}, roulAnimRound: -1, caseBusy: false,
};
const me = () => (S.me ? S.me.name : '');
function sync(serverNow) {
  if (!serverNow) return;
  const o = serverNow - Date.now();
  if (S.offset === null || o > S.offset) S.offset = o;
}
const sNow = () => Date.now() + (S.offset || 0);

function toast(text, kind = '') {
  const t = document.createElement('div');
  t.className = 'toast ' + kind;
  t.textContent = text;
  $('#toasts').appendChild(t);
  setTimeout(() => t.remove(), 4000);
}
function emitRaw(ev, data = {}) { return new Promise((res) => socket.emit(ev, data, (r) => res(r || {}))); }
async function emit(ev, data = {}) {
  const r = await emitRaw(ev, data);
  if (r.error) { toast(r.error, 'bad'); return null; }
  return r;
}

// ───────────────────────── вход ─────────────────────────
function myToken() {
  let t = ls.get('rc_token');
  if (!t) {
    t = Array.from({ length: 32 }, () => 'abcdefghijklmnopqrstuvwxyz0123456789'[Math.floor(Math.random() * 36)]).join('');
    ls.set('rc_token', t);
  }
  return t;
}
async function login(name) {
  const r = await emitRaw('login', { name, token: myToken() });
  if (r.error) {
    $('#loginErr').textContent = r.error;
    $('#login').classList.remove('hidden'); $('#app').classList.add('hidden');
    return;
  }
  ls.set('rc_nick', name);
  S.offset = null; sync(r.serverNow);
  S.me = r.me; S.cases = r.cases; S.towerDiff = r.towerDiff; S.towerRows = r.towerRows;
  S.plane = r.plane; S.roul = r.roulette; S.minesSolo = r.mines; S.towersSolo = r.towers;
  if (!S.built) buildUI();
  $('#login').classList.add('hidden'); $('#app').classList.remove('hidden');
  renderMe(); renderFeed(r.feed); renderChat(r.chat);
  renderMinesSolo(); renderTowersSolo(); renderPlane(); renderRoulette(true); renderCaseOpener();
}
$('#loginBtn').onclick = () => login($('#nick').value);
$('#nick').onkeydown = (e) => { if (e.key === 'Enter') login($('#nick').value); };
$('#nick').value = ls.get('rc_nick') || '';
socket.on('connect', () => { const n = ls.get('rc_nick'); if (n) login(n); });
socket.on('disconnect', () => toast('Связь потеряна, переподключаюсь…', 'bad'));

// ───────────────────────── сборка интерфейса ─────────────────────────
function betField(id, def = 100) {
  return `<div class="field"><label>Ставка, ₽</label><div class="row">
    <input id="${id}" type="number" min="1" step="1" value="${def}">
    <button class="btn small" data-act="betq" data-t="${id}" data-op="half">½</button>
    <button class="btn small" data-act="betq" data-t="${id}" data-op="dbl">×2</button>
    <button class="btn small" data-act="betq" data-t="${id}" data-op="max">Всё</button></div></div>`;
}
function seg(name, opts) {
  return `<div class="seg" data-seg="${name}">${opts.map(([k, l]) => `<button data-act="mode" data-k="${k}">${l}</button>`).join('')}</div>`;
}
function lobbyHTML(game) {
  let cfg = '';
  if (game === 'mines') cfg = `<div class="field"><label>Мин на поле</label><select data-k="mines">${range(1, 24).map((i) => `<option ${i === 5 ? 'selected' : ''}>${i}</option>`).join('')}</select></div>`;
  if (game === 'towers') cfg = `<div class="field"><label>Сложность</label><select data-k="diff">${Object.entries(S.towerDiff).map(([k, d]) => `<option value="${k}" ${k === 'medium' ? 'selected' : ''}>${d.label} (${d.cols} кл., ${d.bombs} 💣)</option>`).join('')}</select></div>`;
  if (game === 'cases') cfg = `<div class="field"><label>Кейс</label><select data-k="caseId">${S.cases.map((c) => `<option value="${c.id}">${c.emoji} ${c.name} — ${rub(c.price)}</option>`).join('')}</select></div>
    <div class="field"><label>Кол-во кейсов</label><select data-k="count">${range(1, 5).map((i) => `<option>${i}</option>`).join('')}</select></div>`;
  const stake = game === 'cases' ? '' : `<div class="field"><label>Ставка каждого, ₽</label><input data-k="stake" type="number" min="1" value="500"></div>`;
  return `<div class="panel"><h2>⚔️ Дуэли: ${GAME_TITLE[game]}</h2>
    <div class="row lobby-form" data-game="${game}" style="align-items:flex-end">${stake}${cfg}
      <button class="btn primary" data-act="create">Создать дуэль</button></div>
    <div class="duel-list" id="list-${game}" style="margin-top:12px"></div></div>`;
}
const RULES = {
  crash: 'Оба ставите одинаково. Множитель растёт — жми <b>ЗАБРАТЬ</b>. Кто заберёт на большем иксе — забирает весь банк. Не вывел до краша — 0. Если оба не успели — ничья, ставки возвращаются.',
  mines: 'У обоих <b>одинаковое поле</b>. Открывай клетки и жми <b>Стоп</b>, чтобы зафиксировать результат. Мина — 0. Кто открыл больше алмазов — забирает банк.',
  towers: 'У обоих <b>одинаковая башня</b>. Лезь вверх и жми <b>Стоп</b>, чтобы зафиксировать этаж. Мина — 0. Чья башня выше — тот забирает банк.',
  cases: 'Каждый открывает одинаковые кейсы. У кого сумма дропа больше — <b>забирает всё</b>, что выпало обоим.',
};
function buildUI() {
  S.built = true;
  $('#tab-crash').innerHTML = `
    <div class="panel"><h2>📈 Краш 1×1</h2><div class="rules">${RULES.crash}</div></div>
    <div class="panel hidden" id="arena-crash"></div>${lobbyHTML('crash')}`;

  $('#tab-mines').innerHTML = `${seg('mines', [['solo', '🎯 Соло'], ['duel', '⚔️ Дуэль с другом']])}
    <div id="mines-solo" class="grid2">
      <div class="panel controls">${betField('mBet')}
        <div class="field"><label>Количество мин</label><select id="mCount">${range(1, 24).map((i) => `<option ${i === 3 ? 'selected' : ''}>${i}</option>`).join('')}</select></div>
        <div id="mInfo" class="rules"></div>
        <button class="btn huge primary" id="mBtn" data-act="mstart">Играть</button>
      </div>
      <div class="panel"><div class="mines-grid" id="mGrid"></div></div>
    </div>
    <div id="mines-duel" class="tab-inner">
      <div class="panel"><h2>💣 Мины-дуэль</h2><div class="rules">${RULES.mines}</div></div>
      <div class="panel hidden" id="arena-mines"></div>${lobbyHTML('mines')}
    </div>`;

  $('#tab-towers').innerHTML = `${seg('towers', [['solo', '🎯 Соло'], ['duel', '⚔️ Дуэль с другом']])}
    <div id="towers-solo" class="grid2">
      <div class="panel controls">${betField('tBet')}
        <div class="field"><label>Сложность</label><div class="seg" id="tDiff">${Object.entries(S.towerDiff).map(([k, d]) => `<button data-act="tdiff" data-k="${k}">${d.label}</button>`).join('')}</div></div>
        <div id="tInfo" class="rules"></div>
        <button class="btn huge primary" id="tBtn" data-act="tstart">Играть</button>
      </div>
      <div class="panel"><div class="tower" id="tTower"></div></div>
    </div>
    <div id="towers-duel">
      <div class="panel"><h2>🗼 Башни-дуэль</h2><div class="rules">${RULES.towers}</div></div>
      <div class="panel hidden" id="arena-towers"></div>${lobbyHTML('towers')}
    </div>`;

  const nums = range(1, 36).map((n) => {
    const col = Math.ceil(n / 3) + 1, row = 3 - ((n - 1) % 3);
    return `<button class="rb ${colorOf(n)}" style="grid-column:${col};grid-row:${row}" data-act="rbet" data-type="num" data-v="${n}">${n}</button>`;
  }).join('');
  const cols = [1, 2, 3].map((r) => `<button class="rb outer" style="grid-column:14;grid-row:${r}" data-act="rbet" data-type="column" data-v="${4 - r}">2:1</button>`).join('');
  $('#tab-roulette').innerHTML = `
    <div class="panel"><div class="row" style="justify-content:space-between"><h2 style="margin:0">🎡 Рулетка</h2><div class="hist rhist" id="rHist"></div></div>
      <div class="rstrip-wrap" style="margin-top:12px"><div class="marker"></div><div class="rstrip" id="rStrip"></div></div>
      <div class="row" style="justify-content:space-between;margin-top:10px"><b id="rPhase"></b><span id="rMyWin"></span></div>
      <div class="rtimer" style="margin-top:6px"><div id="rTimer"></div></div>
    </div>
    <div class="panel">
      <div class="row" style="margin-bottom:10px"><span class="muted">Фишка:</span>
        <div class="chips" id="rChips">${[10, 50, 100, 500, 1000, 5000].map((v) => `<button class="chip" data-act="chip" data-v="${v}">${v.toLocaleString('ru-RU')}</button>`).join('')}</div>
        <input id="rChipCustom" type="number" min="1" placeholder="своя" style="width:90px">
        <span style="flex:1"></span><span>Мои ставки: <b id="rMine">0 ₽</b></span>
        <button class="btn small" data-act="rclear">Убрать мои</button></div>
      <div class="board"><button class="rb green zero" style="grid-column:1;grid-row:1/span 3" data-act="rbet" data-type="num" data-v="0">0</button>${nums}${cols}</div>
      <div class="board-doz">${[1, 2, 3].map((v) => `<button class="rb outer" data-act="rbet" data-type="dozen" data-v="${v}">${v * 12 - 11}–${v * 12}</button>`).join('')}</div>
      <div class="board-out">
        <button class="rb outer" data-act="rbet" data-type="low">1–18</button>
        <button class="rb outer" data-act="rbet" data-type="even">Чёт</button>
        <button class="rb red" data-act="rbet" data-type="red">Красное</button>
        <button class="rb black" data-act="rbet" data-type="black">Чёрное</button>
        <button class="rb outer" data-act="rbet" data-type="odd">Нечет</button>
        <button class="rb outer" data-act="rbet" data-type="high">19–36</button>
      </div>
      <div class="rules" style="margin-top:10px">Число — ×36, дюжина/колонка — ×3, цвет/чёт/половина — ×2. Стол общий: друг видит твои ставки.</div>
    </div>
    <div class="panel"><h3>Ставки за столом</h3><div class="bets-table" id="rBets"></div></div>`;

  $('#tab-plane').innerHTML = `
    <div class="grid2">
      <div class="panel controls"><h2>✈️ Самолётик</h2>${betField('pBet')}
        <div class="field"><label>Автовывод на ×(необязательно)</label><input id="pAuto" type="number" min="1.01" step="0.01" placeholder="например 2.00"></div>
        <button class="btn huge primary" id="pBtn" data-act="pbet">Поставить</button>
        <div class="rules">Общий раунд для всех. Самолёт взлетает — множитель растёт. Успей забрать до того, как он улетит!</div>
        <h3 style="margin:6px 0 0">Ставки раунда</h3><div class="bets-table" id="pBets"></div>
      </div>
      <div class="panel" style="display:grid;gap:10px">
        <div class="hist" id="pHist"></div>
        <div class="graph-wrap"><canvas id="pCv"></canvas><div class="graph-mult" id="pOv"><div><div class="big">1.00×</div><div class="small"></div></div></div></div>
      </div>
    </div>`;

  $('#tab-cases').innerHTML = `${seg('cases', [['open', '📦 Открыть'], ['battle', '⚔️ Кейс-батл с другом']])}
    <div id="cases-open" style="display:grid;gap:16px">
      <div class="panel"><div class="cases-grid" id="cGrid">${S.cases.map((c) => `
        <div class="case-card" data-act="selcase" data-id="${c.id}"><div class="ce">${c.emoji}</div><div class="cn">${c.name}</div><div class="cp">${rub(c.price)}</div></div>`).join('')}</div></div>
      <div class="panel" style="display:grid;gap:12px">
        <div class="row" style="justify-content:space-between"><h2 style="margin:0" id="cTitle"></h2><button class="btn gold" id="cBtn" data-act="copen">Открыть</button></div>
        <div class="cstrip-wrap"><div class="marker"></div><div class="cstrip" id="cStrip"></div></div>
        <div id="cResult" class="banner hidden"></div>
        <h3 style="margin:0">Содержимое</h3><div class="items-list" id="cItems"></div>
      </div>
    </div>
    <div id="cases-battle" style="display:grid;gap:16px">
      <div class="panel"><h2>⚔️ Кейс-батл</h2><div class="rules">${RULES.cases}</div></div>
      <div class="panel hidden" id="arena-cases"></div>${lobbyHTML('cases')}
    </div>`;

  setTab(S.tab);
  applyModes();
  renderChips();
  requestAnimationFrame(frame);
}

function setTab(t) {
  if (!$('#tab-' + t)) t = 'crash';
  S.tab = t; ls.set('rc_tab', t);
  document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('active', b.dataset.tab === t));
  document.querySelectorAll('.tab').forEach((s) => s.classList.toggle('active', s.id === 'tab-' + t));
  if (t === 'roulette') renderRoulette(true);
}
$('#tabs').onclick = (e) => { const b = e.target.closest('button'); if (b) setTab(b.dataset.tab); };
function applyModes() {
  for (const [g, m] of Object.entries(S.mode)) {
    const sg = document.querySelector(`[data-seg="${g}"]`);
    if (!sg) continue;
    sg.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b.dataset.k === m));
    const opts = g === 'cases' ? ['open', 'battle'] : ['solo', 'duel'];
    for (const o of opts) $(`#${g}-${o}`).classList.toggle('hidden', o !== m);
  }
}

// ───────────────────────── действия ─────────────────────────
const ACT = {
  betq(b) {
    const inp = $('#' + b.dataset.t);
    let v = Number(inp.value) || 0;
    if (b.dataset.op === 'half') v = Math.max(1, Math.floor(v / 2));
    if (b.dataset.op === 'dbl') v = Math.min(Math.floor(S.me.balance), v * 2 || 1);
    if (b.dataset.op === 'max') v = Math.floor(S.me.balance);
    inp.value = v;
  },
  mode(b) {
    const g = b.closest('[data-seg]').dataset.seg;
    S.mode[g] = b.dataset.k; applyModes();
  },
  async create(b) {
    const f = b.closest('.lobby-form'), game = f.dataset.game;
    const cfg = {}; let stake;
    f.querySelectorAll('[data-k]').forEach((i) => { if (i.dataset.k === 'stake') stake = i.value; else cfg[i.dataset.k] = i.value; });
    const r = await emit('duel:create', { game, stake, config: cfg });
    if (r) { S.view[game] = r.id; toast('Дуэль создана. Позови друга!', 'ok'); }
  },
  async join(b) {
    const id = Number(b.dataset.id);
    const r = await emit('duel:join', { id });
    if (r) S.view[b.dataset.game] = id;
  },
  cancel(b) { emit('duel:cancel', { id: Number(b.dataset.id) }); },
  watch(b) { S.view[b.dataset.game] = Number(b.dataset.id); renderArena(b.dataset.game); },
  closeArena(b) { S.view[b.dataset.game] = null; renderArena(b.dataset.game); },
  cashout() { const d = viewDuel('crash'); if (d) emit('duel:cashout', { id: d.id }); },
  dmine(b) { const d = viewDuel('mines'); if (d) emit('duel:mines:open', { id: d.id, cell: Number(b.dataset.c) }); },
  dtower(b) { const d = viewDuel('towers'); if (d) emit('duel:towers:pick', { id: d.id, col: Number(b.dataset.c) }); },
  dstop(b) { emit('duel:stop', { id: Number(b.dataset.id) }); },
  dsurrender(b) { if (confirm('Точно сдаться? Твой результат будет 0.')) emit('duel:surrender', { id: Number(b.dataset.id) }); },

  async mstart() {
    const r = await emit('mines:start', { bet: $('#mBet').value, mines: $('#mCount').value });
    if (r) { S.minesSolo = r.state; S.minesLast = null; S.minesNew = null; renderMinesSolo(); }
  },
  async mopen(b) {
    const cell = Number(b.dataset.c);
    const r = await emit('mines:open', { cell });
    if (!r) return;
    S.minesNew = cell;
    if (r.boom) { S.minesLast = { field: r.field, opened: r.opened, hit: cell }; S.minesSolo = null; toast('💥 Бабах! Ставка сгорела', 'bad'); }
    else if (r.cashout) finishMinesSolo(r);
    else S.minesSolo = r.state;
    renderMinesSolo();
  },
  async mcash() {
    const r = await emit('mines:cashout');
    if (r) { finishMinesSolo(r); renderMinesSolo(); }
  },
  tdiff(b) { if (!S.towersSolo) { S.towersDiff = b.dataset.k; renderTowersSolo(); } },
  async tstart() {
    const r = await emit('towers:start', { bet: $('#tBet').value, diff: S.towersDiff });
    if (r) { S.towersSolo = r.state; S.towersLast = null; renderTowersSolo(); }
  },
  async tpick(b) {
    const r = await emit('towers:pick', { col: Number(b.dataset.c) });
    if (!r) return;
    if (r.boom) { S.towersLast = { diff: S.towersSolo.diff, layout: r.layout, picks: r.picks, hit: { row: r.row, col: r.col } }; S.towersSolo = null; toast('💥 Бабах! Башня рухнула', 'bad'); }
    else if (r.cashout) finishTowersSolo(r);
    else S.towersSolo = r.state;
    renderTowersSolo();
  },
  async tcash() {
    const r = await emit('towers:cashout');
    if (r) { finishTowersSolo(r); renderTowersSolo(); }
  },

  chip(b) { S.chip = Number(b.dataset.v); $('#rChipCustom').value = ''; renderChips(); },
  rbet(b) {
    const amount = Number($('#rChipCustom').value) || S.chip;
    emit('roulette:bet', { type: b.dataset.type, value: b.dataset.v !== undefined ? Number(b.dataset.v) : null, amount });
  },
  rclear() { emit('roulette:clear'); },

  pbet() { emit('plane:bet', { amount: $('#pBet').value, auto: $('#pAuto').value }); },
  async pcash() {
    const r = await emit('plane:cashout');
    if (r) toast(`✈️ Вывод ×${r.mult.toFixed(2)} → +${rub(r.win)}`, 'ok');
  },

  selcase(b) { if (S.caseBusy) return; S.selCase = b.dataset.id; renderCaseOpener(); },
  async copen() {
    if (S.caseBusy) return;
    const c = S.cases.find((x) => x.id === S.selCase);
    const r = await emit('case:open', { id: c.id });
    if (!r) return;
    S.caseBusy = true; renderCaseOpener();
    $('#cResult').classList.add('hidden');
    buildStrip($('#cStrip'), c, r.item, r.spinMs);
    setTimeout(() => {
      S.caseBusy = false; renderCaseOpener();
      const res = $('#cResult');
      const profit = r.item.value - c.price;
      res.className = 'banner ' + (profit >= 0 ? 'win' : 'lose');
      res.textContent = `${r.item.emoji} ${r.item.name} — ${rub(r.item.value)} (${profit >= 0 ? '+' : ''}${rub(profit)})`;
    }, r.spinMs);
  },
};
document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-act]');
  if (!b || b.disabled || !ACT[b.dataset.act]) return;
  ACT[b.dataset.act](b, e);
});
$('#refillBtn').onclick = () => emit('refill');
$('#trBtn').onclick = async () => {
  const r = await emit('transfer', { to: $('#trTo').value, amount: $('#trAmount').value });
  if (r) { $('#trAmount').value = ''; toast('Перевод отправлен', 'ok'); }
};
$('#chatForm').onsubmit = (e) => {
  e.preventDefault();
  const t = $('#chatInput').value.trim();
  if (t) emit('chat', { text: t });
  $('#chatInput').value = '';
};

// ───────────────────────── общие события ─────────────────────────
socket.on('me', (u) => {
  const old = S.me && S.me.balance;
  S.me = u; renderMe();
  if (old !== undefined && u.balance !== old) {
    const b = $('#balance'); b.classList.remove('bump'); void b.offsetWidth; b.classList.add('bump');
  }
});
function renderMe() {
  $('#meName').textContent = S.me.name;
  $('#balance').textContent = rub(S.me.balance);
}
socket.on('players', (list) => {
  S.players = list;
  $('#players').innerHTML = list.map((p) => `<div class="player"><span class="st ${p.online ? 'on' : ''}"></span>
    <div class="nm">${esc(p.name)}${p.name === me() ? ' <span class="muted">(ты)</span>' : ''}
      <div class="sub">долгов у мамы: ${p.refills || 0}${p.best ? ` · рекорд ×${p.best.toFixed(2)}` : ''}</div></div>
    <span class="bl">${rub(p.balance)}</span></div>`).join('');
  const sel = $('#trTo'), cur = sel.value;
  sel.innerHTML = list.filter((p) => p.name !== me()).map((p) => `<option value="${esc(p.name)}">${esc(p.name)}</option>`).join('') || '<option value="">нет игроков</option>';
  if (cur) sel.value = cur;
});
function feedItem(f) { return `<div class="${f.kind}">${esc(f.text)}</div>`; }
function renderFeed(list) { $('#feed').innerHTML = list.map(feedItem).join(''); }
socket.on('feed', (f) => {
  $('#feed').insertAdjacentHTML('afterbegin', feedItem(f));
  while ($('#feed').children.length > 40) $('#feed').lastChild.remove();
});
function chatLine(m) { return `<div class="m"><b>${esc(m.name)}:</b> ${esc(m.text)}</div>`; }
function renderChat(list) { const c = $('#chat'); c.innerHTML = list.map(chatLine).join(''); c.scrollTop = c.scrollHeight; }
socket.on('chat', (m) => {
  const c = $('#chat'); c.insertAdjacentHTML('beforeend', chatLine(m)); c.scrollTop = c.scrollHeight;
  if (m.name !== me()) toast(`💬 ${m.name}: ${m.text}`);
});

// ───────────────────────── дуэли ─────────────────────────
const viewDuel = (game) => S.duels.find((d) => d.id === S.view[game] && d.game === game);
socket.on('duels', ({ list, serverNow }) => {
  sync(serverNow);
  S.duels = list;
  for (const d of list) {
    const prev = S.prevDuels.get(d.id);
    const mine = d.players.includes(me());
    if (mine && d.status !== 'done') S.view[d.game] = d.id;
    if (prev === 'open' && d.status === 'playing' && d.creator === me()) {
      toast(`⚔️ Вызов принят: ${d.players[1]} (${GAME_TITLE[d.game]})!`, 'ok');
      setTab(d.game); if (S.mode[d.game]) { S.mode[d.game] = d.game === 'cases' ? 'battle' : 'duel'; applyModes(); }
    }
    if (!prev && d.status === 'open' && d.creator !== me() && S.prevDuels.size) toast(`⚔️ ${d.creator} вызывает: ${GAME_TITLE[d.game]} на ${rub(d.stake)}`);
  }
  S.prevDuels = new Map(list.map((d) => [d.id, d.status]));
  for (const g of ['crash', 'mines', 'towers', 'cases']) { renderLobby(g); renderArena(g); }
  // красная точка на вкладках с открытыми вызовами
  document.querySelectorAll('#tabs button').forEach((b) => {
    const has = list.some((d) => d.game === b.dataset.tab && d.status === 'open' && d.creator !== me());
    const dot = b.querySelector('.dot');
    if (has && !dot) b.insertAdjacentHTML('beforeend', '<span class="dot"></span>');
    if (!has && dot) dot.remove();
  });
});
function duelCfgText(d) {
  if (d.game === 'mines') return `${d.config.mines} 💣`;
  if (d.game === 'towers') return S.towerDiff[d.config.diff].label;
  if (d.game === 'cases') { const c = S.cases.find((x) => x.id === d.config.caseId); return `${c.emoji} ${c.name} ×${d.config.count}`; }
  return '';
}
function renderLobby(game) {
  const el = $('#list-' + game);
  if (!el) return;
  const list = S.duels.filter((d) => d.game === game);
  if (!list.length) { el.innerHTML = '<div class="muted">Пока никого. Создай дуэль и позови друга.</div>'; return; }
  el.innerHTML = list.map((d) => {
    let act = '', who;
    const [a, b] = d.players;
    if (d.status === 'open') {
      who = `<b>${esc(a)}</b> <span class="muted">ждёт соперника</span>`;
      act = d.creator === me() ? `<button class="btn small red" data-act="cancel" data-id="${d.id}">Отменить</button>`
        : `<button class="btn small green" data-act="join" data-game="${game}" data-id="${d.id}">Принять вызов</button>`;
    } else {
      who = `<b>${esc(a)}</b> vs <b>${esc(b)}</b>`;
      if (d.status === 'done') who += d.result.winner ? ` — 🏆 ${esc(d.result.winner)}` : ' — 🤝 ничья';
      else who += ' <span class="muted">— идёт игра</span>';
      act = `<button class="btn small" data-act="watch" data-game="${game}" data-id="${d.id}">${d.players.includes(me()) && d.status !== 'done' ? 'Открыть' : 'Смотреть'}</button>`;
    }
    return `<div class="duel-item ${d.status}"><div class="who">${who}</div><span class="muted">${duelCfgText(d)}</span><span class="stake">${rub(d.stake)}</span>${act}</div>`;
  }).join('');
}
function vsHTML(d, info) {
  const card = (n) => {
    if (!n) return '<div class="pcard"><div class="pn">???</div><div class="pv">⏳</div><div class="ps">ждём соперника…</div></div>';
    const i = info(n);
    const w = d.result && d.result.winner === n;
    return `<div class="pcard ${n === me() ? 'me' : ''} ${w ? 'winner' : ''}"><div class="pn">${w ? '👑 ' : ''}${esc(n)}${n === me() ? ' (ты)' : ''}</div><div class="pv">${i.value}</div><div class="ps">${i.sub}</div></div>`;
  };
  return `<div class="vs">${card(d.players[0])}<div class="vs-mid">VS</div>${card(d.players[1])}</div>`;
}
function bannerHTML(d) {
  if (d.status !== 'done') return '';
  const r = d.result;
  if (!r.winner) return `<div class="banner">🤝 Ничья — ${d.game === 'cases' ? 'каждому своё' : 'ставки вернулись'}</div>`;
  if (d.players.includes(me())) {
    return r.winner === me() ? `<div class="banner win">🏆 Победа! +${rub(r.prize)}</div>`
      : `<div class="banner lose">💀 Проигрыш. ${esc(r.winner)} забирает ${rub(r.prize)}</div>`;
  }
  return `<div class="banner">🏆 Победил ${esc(r.winner)} (+${rub(r.prize)})</div>`;
}
function arenaHead(d) {
  const st = d.status === 'open' ? 'ждём соперника' : d.status === 'playing' ? 'идёт игра' : 'завершена';
  const cancel = d.status === 'open' && d.creator === me() ? `<button class="btn small red" data-act="cancel" data-id="${d.id}">Отменить</button>` : '';
  const close = d.status === 'done' || !d.players.includes(me()) ? `<button class="btn small" data-act="closeArena" data-game="${d.game}">✕ Закрыть</button>` : '';
  return `<div class="row" style="justify-content:space-between;margin-bottom:12px"><h2 style="margin:0">Дуэль #${d.id} · ${GAME_TITLE[d.game]} <span class="muted" style="font-size:14px">${duelCfgText(d)} · ${st}</span></h2>
    <div class="row"><span class="stake" style="color:var(--gold);font-weight:800">${d.game === 'cases' ? 'Кейсов на' : 'Банк:'} ${rub(d.stake * 2)}</span>${cancel}${close}</div></div>`;
}
function renderArena(game) {
  const el = $('#arena-' + game);
  if (!el) return;
  const d = viewDuel(game);
  if (!d) { el.classList.add('hidden'); el.innerHTML = ''; el.dataset.id = ''; return; }
  el.classList.remove('hidden');
  ARENA[game](d, el);
}
const STATUS_TXT = { playing: 'в игре…', stopped: '✋ стоп', boom: '💥 мина' };
const ARENA = {
  crash(d, el) {
    if (el.dataset.id !== String(d.id)) {
      el.dataset.id = d.id;
      el.innerHTML = `<div class="cr-head"></div>
        <div class="graph-wrap"><canvas id="crCv"></canvas><div class="graph-mult" id="crOv"><div><div class="big">1.00×</div><div class="small"></div></div></div></div>
        <div class="cr-vs" style="margin-top:12px"></div>
        <button class="btn huge green" id="crBtn" data-act="cashout" style="margin-top:12px">ЗАБРАТЬ</button>
        <div class="cr-banner" style="margin-top:12px"></div>`;
    }
    $('.cr-head', el).innerHTML = arenaHead(d);
    const cash = d.state ? d.state.cash : {};
    $('.cr-vs', el).innerHTML = vsHTML(d, (n) => ({
      value: cash[n] ? `×${cash[n].toFixed(2)}` : d.status === 'done' ? '💥 0' : '…',
      sub: cash[n] ? 'вывод' : d.status === 'done' ? 'краш' : d.status === 'open' ? 'ждёт' : 'держит',
    }));
    $('.cr-banner', el).innerHTML = bannerHTML(d);
    $('#crBtn').classList.toggle('hidden', !d.players.includes(me()) || d.status === 'done');
  },
  mines(d, el) {
    let html = arenaHead(d);
    const s = d.state;
    html += vsHTML(d, (n) => {
      if (!s) return { value: '💎 0', sub: 'ждёт' };
      const p = s.p[n];
      return { value: `💎 ${p.count}`, sub: STATUS_TXT[p.status] };
    });
    const mineIn = d.players.includes(me()) && s;
    if (mineIn || (s && d.status === 'done')) {
      const viewer = mineIn ? me() : d.players[0];
      const p = s.p[viewer];
      const opened = new Set(p.opened || []);
      const field = new Set(s.field || []);
      const done = d.status === 'done';
      const canPlay = !done && mineIn && p.status === 'playing';
      html += `<div style="margin-top:14px">${mineIn ? '' : `<div class="muted" style="text-align:center;margin-bottom:6px">Поле игрока ${esc(viewer)}</div>`}<div class="mines-grid">${range(0, 24).map((i) => {
        let cls = '', ch = '';
        if (opened.has(i)) { cls = 'gem'; ch = '💎'; }
        else if (p.hit === i) { cls = 'bomb hit'; ch = '💣'; }
        else if (done && field.has(i)) { cls = 'bomb dim'; ch = '💣'; }
        else if (done || p.status !== 'playing') { cls = 'dim'; }
        return `<button class="cell ${cls}" ${canPlay && !opened.has(i) ? `data-act="dmine" data-c="${i}"` : 'disabled'}>${ch}</button>`;
      }).join('')}</div></div>`;
      if (canPlay) {
        html += `<div class="row" style="margin-top:14px;justify-content:center">
          <button class="btn green" data-act="dstop" data-id="${d.id}" ${p.count ? '' : 'disabled'}>✋ Стоп — зафиксировать ${p.count} 💎</button>
          <button class="btn red" data-act="dsurrender" data-id="${d.id}">🏳️ Сдаться</button></div>`;
      } else if (!done && mineIn) html += '<div class="muted" style="text-align:center;margin-top:10px">Ждём, пока соперник закончит…</div>';
    }
    html += `<div style="margin-top:12px">${bannerHTML(d)}</div>`;
    el.innerHTML = html;
  },
  towers(d, el) {
    let html = arenaHead(d);
    const s = d.state;
    html += vsHTML(d, (n) => {
      if (!s) return { value: '🗼 0', sub: 'ждёт' };
      const p = s.p[n];
      return { value: `🗼 ${p.level}/${S.towerRows}`, sub: STATUS_TXT[p.status] };
    });
    const mineIn = d.players.includes(me()) && s;
    if (mineIn || (s && d.status === 'done')) {
      const viewer = mineIn ? me() : d.players[0];
      const p = s.p[viewer];
      const done = d.status === 'done';
      const canPlay = !done && mineIn && p.status === 'playing';
      html += `<div style="margin-top:14px">${mineIn ? '' : `<div class="muted" style="text-align:center;margin-bottom:6px">Башня игрока ${esc(viewer)}</div>`}
        ${towerHTML({ diff: d.config.diff, picks: p.picks || [], hit: p.hit, layout: s.layout, active: canPlay, act: 'dtower', showMult: false })}</div>`;
      if (canPlay) {
        html += `<div class="row" style="margin-top:14px;justify-content:center">
          <button class="btn green" data-act="dstop" data-id="${d.id}" ${p.level ? '' : 'disabled'}>✋ Стоп — зафиксировать ${p.level} этаж</button>
          <button class="btn red" data-act="dsurrender" data-id="${d.id}">🏳️ Сдаться</button></div>`;
      } else if (!done && mineIn) html += '<div class="muted" style="text-align:center;margin-top:10px">Ждём, пока соперник закончит…</div>';
    }
    html += `<div style="margin-top:12px">${bannerHTML(d)}</div>`;
    el.innerHTML = html;
  },
  cases(d, el) {
    const fresh = el.dataset.id !== String(d.id);
    if (fresh) {
      el.dataset.id = d.id;
      el.innerHTML = `<div class="cb-head"></div>
        <div class="vs" style="align-items:start">${[0, 1].map((i) => `<div class="battle-col" data-p="${i}">
          <div class="pcard"><div class="pn"></div><div class="pv">0 ₽</div><div class="ps"></div></div>
          <div class="cstrip-wrap"><div class="marker"></div><div class="cstrip"></div></div>
          <div class="mini-items"></div></div>${i === 0 ? '<div class="vs-mid" style="align-self:center">VS</div>' : ''}`).join('')}</div>
        <div class="cb-banner" style="margin-top:12px"></div>`;
      S.battleAnim = { id: d.id, round: -2, revealed: -1 };
    }
    $('.cb-head', el).innerHTML = arenaHead(d);
    $('.cb-banner', el).innerHTML = bannerHTML(d);
    tickBattle(true);
  },
};

// ── кейс-батл: анимация раундов
function tickBattle(force) {
  const d = viewDuel('cases');
  const el = $('#arena-cases');
  if (!d || !el || el.classList.contains('hidden')) return;
  const A = S.battleAnim;
  const c = S.cases.find((x) => x.id === d.config.caseId);
  const cols = el.querySelectorAll('.battle-col');
  if (!d.state) {
    d.players.forEach((n, i) => { $('.pn', cols[i]).textContent = n; $('.ps', cols[i]).textContent = 'готов'; });
    $('.pn', cols[1]).textContent = d.players[1] || '???';
    $('.ps', cols[1]).textContent = d.players[1] ? '' : 'ждём соперника…';
    return;
  }
  const per = CASE_SPIN + 800, count = d.config.count;
  const el0 = sNow() - d.state.startAt;
  const done = d.status === 'done';
  const r = done ? count - 1 : Math.floor(el0 / per);
  if (r !== A.round && (el0 >= 0 || done) && r < count) {
    A.round = r;
    const left = done ? 0 : Math.max(250, r * per + CASE_SPIN - el0);
    d.players.forEach((n, i) => buildStrip($('.cstrip', cols[i]), c, d.state.rolls[n][r], left));
  }
  const revealed = done ? count : el0 < CASE_SPIN ? 0 : Math.min(count, Math.floor((el0 - CASE_SPIN) / per) + 1);
  if (revealed !== A.revealed || force) {
    A.revealed = revealed;
    d.players.forEach((n, i) => {
      const items = d.state.rolls[n].slice(0, revealed);
      const sum = items.reduce((s, it) => s + it.value, 0);
      const col = cols[i];
      const win = d.result && d.result.winner === n;
      col.querySelector('.pcard').className = `pcard ${n === me() ? 'me' : ''} ${win ? 'winner' : ''}`;
      $('.pn', col).textContent = (win ? '👑 ' : '') + n + (n === me() ? ' (ты)' : '');
      $('.pv', col).textContent = rub(sum);
      $('.ps', col).textContent = `кейс ${Math.min(count, Math.max(0, r + 1))}/${count}`;
      $('.mini-items', col).innerHTML = items.map((it) => `<span class="${it.rarity}">${it.emoji} ${rub(it.value)}</span>`).join('');
    });
  }
}

// ── лента кейса (общая для открытия и батла)
function weightedPick(c) {
  const W = c.items.reduce((s, i) => s + i.w, 0);
  let x = Math.random() * W;
  for (const it of c.items) { if ((x -= it.w) < 0) return it; }
  return c.items[0];
}
function itemHTML(it, extra = '') {
  return `<div class="citem ${it.rarity}"><div class="ie">${it.emoji}</div><div class="in">${esc(it.name)}</div><div class="iv">${rub(it.value)}</div>${extra}</div>`;
}
function buildStrip(strip, c, winner, ms) {
  const total = 60, winIdx = 50, W = 128;
  const items = Array.from({ length: total }, (_, i) => (i === winIdx ? winner : weightedPick(c)));
  strip.innerHTML = items.map((it) => itemHTML(it)).join('');
  strip.style.transition = 'none';
  strip.style.transform = 'translateX(0)';
  void strip.offsetWidth;
  const wrapW = strip.parentElement.clientWidth;
  const jitter = (Math.random() - 0.5) * 60;
  const x = wrapW / 2 - (winIdx * W + 60) + jitter;
  strip.style.transition = ms > 0 ? `transform ${ms}ms cubic-bezier(.08,.75,.15,1)` : 'none';
  strip.style.transform = `translateX(${x}px)`;
}
function renderCaseOpener() {
  const c = S.cases.find((x) => x.id === S.selCase) || S.cases[0];
  if (!c || !$('#cTitle')) return;
  document.querySelectorAll('.case-card').forEach((e) => e.classList.toggle('sel', e.dataset.id === c.id));
  $('#cTitle').textContent = `${c.emoji} ${c.name}`;
  const btn = $('#cBtn');
  btn.disabled = S.caseBusy;
  btn.textContent = S.caseBusy ? 'Крутим…' : `Открыть за ${rub(c.price)}`;
  if ($('#cItems').dataset.id !== c.id) {
    $('#cItems').dataset.id = c.id;
    $('#cItems').innerHTML = c.items.map((it) => itemHTML(it, `<div class="in muted">${it.chance}%</div>`)).join('');
    const st = $('#cStrip');
    st.style.transition = 'none'; st.style.transform = 'translateX(0)';
    st.innerHTML = c.items.concat(c.items, c.items).map((it) => itemHTML(it)).join('');
    $('#cResult').classList.add('hidden');
  }
}

// ───────────────────────── мины соло ─────────────────────────
function finishMinesSolo(r) {
  S.minesLast = { field: r.field, opened: r.opened, hit: null };
  S.minesSolo = null;
  toast(`💎 Вывод ×${r.mult} → +${rub(r.win)}`, 'ok');
}
function minesMult(mines, n) {
  if (n <= 0) return 1;
  let m = 0.97;
  for (let i = 0; i < n; i++) m *= (25 - i) / (25 - mines - i);
  return Math.floor(m * 100) / 100;
}
function renderMinesSolo() {
  const s = S.minesSolo, L = S.minesLast;
  const grid = $('#mGrid');
  if (!grid) return;
  const opened = new Set(s ? s.opened : L ? L.opened : []);
  const field = new Set(L ? L.field : []);
  grid.innerHTML = range(0, 24).map((i) => {
    let cls = '', ch = '';
    const isNew = i === S.minesNew ? ' new' : '';
    if (opened.has(i)) { cls = 'gem' + isNew; ch = '💎'; }
    else if (L && field.has(i)) { cls = 'bomb ' + (L.hit === i ? 'hit' + isNew : 'dim'); ch = '💣'; }
    else if (L) { cls = 'dim'; ch = '💎'; }
    return `<button class="cell ${cls}" ${s && !opened.has(i) ? `data-act="mopen" data-c="${i}"` : 'disabled'}>${ch}</button>`;
  }).join('');
  S.minesNew = null;
  const btn = $('#mBtn');
  $('#mBet').disabled = !!s; $('#mCount').disabled = !!s;
  if (s) {
    const win = r2(s.bet * s.mult);
    $('#mInfo').innerHTML = `Открыто: <b>${s.opened.length}</b> · Множитель: <b>×${s.mult.toFixed(2)}</b><br>Следующая клетка: <b>×${s.next.toFixed(2)}</b>`;
    btn.dataset.act = 'mcash'; btn.className = 'btn huge green';
    btn.disabled = !s.opened.length;
    btn.textContent = s.opened.length ? `Забрать ${rub(win)}` : 'Открой клетку';
  } else {
    const mines = Number($('#mCount').value);
    $('#mInfo').innerHTML = `Первая клетка: <b>×${minesMult(mines, 1).toFixed(2)}</b> · 5 клеток: <b>×${minesMult(mines, Math.min(5, 25 - mines)).toFixed(2)}</b>`;
    btn.dataset.act = 'mstart'; btn.className = 'btn huge primary'; btn.disabled = false; btn.textContent = 'Играть';
  }
}
document.addEventListener('change', (e) => { if (e.target.id === 'mCount') renderMinesSolo(); });

// ───────────────────────── башни ─────────────────────────
function towerMult(diff, level) {
  if (level <= 0) return 1;
  const d = S.towerDiff[diff];
  return Math.floor(0.97 * Math.pow(d.cols / (d.cols - d.bombs), level) * 100) / 100;
}
// o: {diff, picks, hit:{row,col}|null, layout|null, active, act, showMult, bet}
function towerHTML(o) {
  const d = S.towerDiff[o.diff];
  const level = o.picks.length;
  let html = '';
  for (let row = 0; row < S.towerRows; row++) {
    const cur = o.active && row === level;
    const label = o.showMult ? (o.bet ? rub(o.bet * towerMult(o.diff, row + 1)) : `×${towerMult(o.diff, row + 1)}`) : `${row + 1} этаж`;
    html += `<div class="trow ${cur ? 'cur' : ''}"><div class="tm">${label}</div><div class="tcells" style="grid-template-columns:repeat(${d.cols},1fr)">`;
    for (let col = 0; col < d.cols; col++) {
      let cls = '', ch = '';
      const bomb = o.layout && o.layout[row].includes(col);
      if (row < level && o.picks[row] === col) { cls = 'gem'; ch = '💎'; }
      else if (o.hit && o.hit.row === row && o.hit.col === col) { cls = 'bomb hit'; ch = '💣'; }
      else if (o.layout) { cls = bomb ? 'bomb dim' : 'dim'; ch = bomb ? '💣' : ''; }
      else if (row < level) cls = 'dim';
      html += `<button class="tcell ${cls}" ${cur ? `data-act="${o.act}" data-c="${col}"` : 'disabled'}>${ch}</button>`;
    }
    html += '</div></div>';
  }
  return html;
}
function finishTowersSolo(r) {
  S.towersLast = { diff: S.towersSolo.diff, layout: r.layout, picks: r.picks, hit: null };
  S.towersSolo = null;
  toast(`🗼 Вывод ×${r.mult} → +${rub(r.win)}`, 'ok');
}
function renderTowersSolo() {
  const s = S.towersSolo, L = S.towersLast;
  if (!$('#tTower')) return;
  const diff = s ? s.diff : S.towersDiff;
  if (s) S.towersDiff = s.diff;
  document.querySelectorAll('#tDiff button').forEach((b) => { b.classList.toggle('active', b.dataset.k === diff); b.disabled = !!s; });
  const bet = Number($('#tBet').value) || 0;
  $('#tTower').innerHTML = towerHTML({
    diff: s ? s.diff : L && L.diff === diff ? L.diff : diff,
    picks: s ? s.picks : L && L.diff === diff ? L.picks : [],
    hit: L && !s && L.diff === diff ? L.hit : null,
    layout: L && !s && L.diff === diff ? L.layout : null,
    active: !!s, act: 'tpick', showMult: true, bet: s ? s.bet : bet,
  });
  const btn = $('#tBtn');
  $('#tBet').disabled = !!s;
  const dd = S.towerDiff[diff];
  if (s) {
    $('#tInfo').innerHTML = `Этаж: <b>${s.level}/${S.towerRows}</b> · Множитель: <b>×${s.mult.toFixed(2)}</b><br>Следующий этаж: <b>×${s.next.toFixed(2)}</b>`;
    btn.dataset.act = 'tcash'; btn.className = 'btn huge green'; btn.disabled = !s.level;
    btn.textContent = s.level ? `Забрать ${rub(s.bet * s.mult)}` : 'Выбери клетку';
  } else {
    $('#tInfo').innerHTML = `${dd.label}: ${dd.cols} клетки в ряду, ${dd.bombs} 💣. Вершина — <b>×${towerMult(diff, S.towerRows)}</b>`;
    btn.dataset.act = 'tstart'; btn.className = 'btn huge primary'; btn.disabled = false; btn.textContent = 'Играть';
  }
}
document.addEventListener('input', (e) => { if (e.target.id === 'tBet' && !S.towersSolo) renderTowersSolo(); });

// ───────────────────────── рулетка ─────────────────────────
function renderChips() {
  document.querySelectorAll('#rChips .chip').forEach((c) => c.classList.toggle('on', Number(c.dataset.v) === S.chip && !$('#rChipCustom').value));
}
document.addEventListener('input', (e) => { if (e.target.id === 'rChipCustom') renderChips(); });
socket.on('roulette', (r) => { sync(r.serverNow); S.roul = r; renderRoulette(); });
function buildRoulStrip() {
  const st = $('#rStrip');
  if (st.childElementCount) return;
  let html = '';
  for (let k = 0; k < 8; k++) for (const n of WHEEL) html += `<div class="rnum ${colorOf(n)}">${n}</div>`;
  st.innerHTML = html;
}
function roulOffset(rep, n, jitter = 0) {
  const W = 78, idx = rep * 37 + WHEEL.indexOf(n);
  return $('#rStrip').parentElement.clientWidth / 2 - (idx * W + 36) + jitter;
}
function renderRoulette(force) {
  const r = S.roul;
  if (!r || !$('#rStrip')) return;
  buildRoulStrip();
  const st = $('#rStrip');
  // анимация ленты
  const target = () => `translateX(${roulOffset(6, r.result, (Math.random() - 0.5) * 50)}px)`;
  if (r.phase === 'spinning') {
    if (S.roulAnimRound !== r.round) {
      S.roulAnimRound = r.round;
      st.style.transition = 'none';
      st.style.transform = `translateX(${roulOffset(1, r.history[0] ?? 0)}px)`;
      void st.offsetWidth;
      const left = r.endsAt - sNow() - 200;
      if (S.tab === 'roulette' && left > 400) st.style.transition = `transform ${left}ms cubic-bezier(.1,.7,.12,1)`;
      st.style.transform = target();
    } else if (force) { st.style.transition = 'none'; st.style.transform = target(); }
  } else if (force || S.roulAnimRound !== r.round) {
    st.style.transition = 'none';
    st.style.transform = r.phase === 'result' ? target() : `translateX(${roulOffset(1, r.history[0] ?? 0)}px)`;
  }
  // мои фишки на столе
  const mineBets = r.bets.filter((b) => b.name === me());
  const sums = {};
  for (const b of mineBets) { const k = b.type + ':' + (b.value ?? ''); sums[k] = (sums[k] || 0) + b.amount; }
  document.querySelectorAll('[data-act="rbet"]').forEach((b) => {
    const k = b.dataset.type + ':' + (b.dataset.v ?? '');
    let badge = b.querySelector('.cb');
    if (sums[k]) {
      if (!badge) { badge = document.createElement('span'); badge.className = 'cb'; b.appendChild(badge); }
      badge.textContent = sums[k] >= 1000 ? r2(sums[k] / 1000) + 'к' : sums[k];
    } else if (badge) badge.remove();
    b.disabled = r.phase !== 'betting';
    b.classList.toggle('won', r.phase === 'result' && b.dataset.type === 'num' && Number(b.dataset.v) === r.result);
  });
  $('#rMine').textContent = rub(mineBets.reduce((s, b) => s + b.amount, 0));
  const bt = { red: '🔴 Красное', black: '⚫ Чёрное', even: 'Чёт', odd: 'Нечет', low: '1–18', high: '19–36' };
  const label = (b) => (b.type === 'num' ? `Число ${b.value}` : b.type === 'dozen' ? `Дюжина ${b.value}` : b.type === 'column' ? `Колонка ${b.value}` : bt[b.type]);
  $('#rBets').innerHTML = r.bets.length ? r.bets.slice().reverse().map((b) => `<div><span><b>${esc(b.name)}</b> → ${label(b)}</span><span>${rub(b.amount)}</span></div>`).join('') : '<div class="muted">Ставок пока нет</div>';
  $('#rHist').innerHTML = r.history.slice(0, 14).map((n) => `<span class="rb ${colorOf(n)}">${n}</span>`).join('');
  const myWin = r.wins[me()];
  $('#rMyWin').innerHTML = r.phase === 'result' && mineBets.length ? (myWin ? `<b class="win-t">+${rub(myWin)}</b>` : '<b class="lose-t">мимо</b>') : '';
}
function updateRoulTimer() {
  const r = S.roul;
  if (!r) return;
  const left = Math.max(0, r.endsAt - sNow());
  const pct = Math.min(100, (left / ROUL_DUR[r.phase]) * 100);
  $('#rTimer').style.width = pct + '%';
  const txt = r.phase === 'betting' ? `Принимаем ставки: ${Math.ceil(left / 1000)} с` : r.phase === 'spinning' ? 'Крутим…' : `Выпало: ${r.result} ${colorOf(r.result) === 'red' ? '🔴' : colorOf(r.result) === 'black' ? '⚫' : '🟢'}`;
  const ph = $('#rPhase');
  if (ph.textContent !== txt) ph.textContent = txt;
}

// ───────────────────────── самолётик ─────────────────────────
socket.on('plane', (p) => {
  sync(p.serverNow);
  const old = S.plane;
  S.plane = p;
  const mine = p.bets.find((b) => b.name === me());
  if (old && old.phase === 'flying' && p.phase === 'crashed' && mine && !mine.cashedAt) toast(`✈️ Улетел на ×${p.crash.toFixed(2)} — ставка сгорела`, 'bad');
  if (mine && mine.cashedAt && mine.auto && old && !(old.bets.find((b) => b.name === me()) || {}).cashedAt) toast(`✈️ Автовывод ×${mine.cashedAt.toFixed(2)} → +${rub(mine.win)}`, 'ok');
  renderPlane();
});
function histCls(m) { return m < 2 ? 'lo' : m < 10 ? 'mid' : m < 100 ? 'hi' : 'top'; }
function renderPlane() {
  const p = S.plane;
  if (!p || !$('#pBets')) return;
  $('#pHist').innerHTML = p.history.map((m) => `<span class="${histCls(m)}">×${m.toFixed(2)}</span>`).join('');
  $('#pBets').innerHTML = p.bets.length ? p.bets.map((b) => `<div><span><b>${esc(b.name)}</b> ${rub(b.amount)}${b.auto ? ` <span class="muted">авто ×${b.auto}</span>` : ''}</span>
    <span class="${b.cashedAt ? 'ok' : ''}">${b.cashedAt ? `×${b.cashedAt.toFixed(2)} · +${rub(b.win)}` : p.phase === 'crashed' ? '💥' : '…'}</span></div>`).join('') : '<div class="muted">Пока никто не поставил</div>';
}
function setBtn(btn, act, cls, text, disabled) {
  if (btn.dataset.act !== act) btn.dataset.act = act;
  if (btn.className !== cls) btn.className = cls;
  if (btn.textContent !== text) btn.textContent = text;
  if (btn.disabled !== disabled) btn.disabled = disabled;
}
function drawPlane() {
  const p = S.plane;
  if (!p) return;
  const t = sNow();
  const mine = p.bets.find((b) => b.name === me());
  let elapsed = 0, mult = 1, big = '1.00×', small = '', ov = '';
  if (p.phase === 'betting') {
    small = `Взлёт через ${Math.max(0, Math.ceil((p.betEndsAt - t) / 1000))} с`;
  } else if (p.phase === 'flying') {
    elapsed = Math.max(0, t - p.flyStart); mult = multAt(elapsed); big = mult.toFixed(2) + '×';
  } else {
    mult = p.crash; elapsed = timeFor(p.crash); big = mult.toFixed(2) + '×'; small = 'Улетел!'; ov = 'crashed';
  }
  if (mine && mine.cashedAt) { small = `Твой вывод: ×${mine.cashedAt.toFixed(2)} (+${rub(mine.win)})`; if (p.phase === 'flying') ov = 'cashed'; }
  setOverlay($('#pOv'), big, small, ov);
  drawCurve($('#pCv'), elapsed, mult, {
    emoji: p.phase === 'crashed' ? '💥' : '✈️', crashed: p.phase === 'crashed', color: '#ec4899', idle: p.phase === 'betting',
    marks: p.bets.filter((b) => b.cashedAt).map((b) => ({ m: b.cashedAt, label: b.name })),
  });
  const btn = $('#pBtn');
  if (p.phase === 'betting') {
    if (mine) setBtn(btn, 'pbet', 'btn huge', `Ставка ${rub(mine.amount)} принята`, true);
    else setBtn(btn, 'pbet', 'btn huge primary', 'Поставить', false);
  } else if (p.phase === 'flying' && mine && !mine.cashedAt) {
    setBtn(btn, 'pcash', 'btn huge gold', `Забрать ${rub(mine.amount * mult)}`, false);
  } else if (mine && mine.cashedAt) {
    setBtn(btn, 'pbet', 'btn huge green', `Выведено +${rub(mine.win)}`, true);
  } else {
    setBtn(btn, 'pbet', 'btn huge', p.phase === 'flying' ? 'Ждём следующий раунд…' : 'Раунд окончен', true);
  }
}
function setOverlay(el, big, small, cls) {
  const b = el.querySelector('.big'), s = el.querySelector('.small');
  if (b.textContent !== big) b.textContent = big;
  if (s.textContent !== small) s.textContent = small;
  const c = 'graph-mult ' + cls;
  if (el.className !== c) el.className = c;
}

// ── краш-дуэль: кадр
function drawCrashDuel() {
  const d = viewDuel('crash');
  const cv = $('#crCv');
  if (!d || !cv) return;
  const s = d.state;
  let elapsed = 0, mult = 1, big = '1.00×', small = '', ov = '';
  const cash = s ? s.cash : {};
  if (!s) small = 'Ждём соперника…';
  else if (d.status === 'done') {
    mult = s.crash; elapsed = timeFor(s.crash); big = mult.toFixed(2) + '×'; small = 'Краш!'; ov = 'crashed';
  } else {
    const el = sNow() - s.startAt;
    if (el < 0) small = `Старт через ${Math.ceil(-el / 1000)}…`;
    else { elapsed = el; mult = multAt(el); big = mult.toFixed(2) + '×'; }
  }
  if (cash[me()]) small = `Твой вывод: ×${cash[me()].toFixed(2)}`;
  setOverlay($('#crOv'), big, small, ov || (cash[me()] && d.status !== 'done' ? 'cashed' : ''));
  drawCurve(cv, elapsed, mult, {
    emoji: d.status === 'done' ? '💥' : '🚀', crashed: d.status === 'done', color: '#7c5cff', idle: !s || sNow() < s.startAt,
    marks: Object.entries(cash).map(([n, m]) => ({ m, label: n })),
  });
  const btn = $('#crBtn');
  if (btn && s) {
    const started = sNow() >= s.startAt;
    if (cash[me()]) setBtn(btn, 'cashout', 'btn huge', `Вывод ×${cash[me()].toFixed(2)} — ждём соперника`, true);
    else if (!started) setBtn(btn, 'cashout', 'btn huge', 'Приготовься…', true);
    else setBtn(btn, 'cashout', 'btn huge green', `ЗАБРАТЬ ×${mult.toFixed(2)}`, d.status !== 'playing');
  } else if (btn) setBtn(btn, 'cashout', 'btn huge', 'Ждём соперника…', true);
}

// ── отрисовка графика
function niceStep(x) {
  const p = Math.pow(10, Math.floor(Math.log10(x)));
  const n = x / p;
  return (n < 1.5 ? 1 : n < 3.5 ? 2 : n < 7.5 ? 5 : 10) * p;
}
function drawCurve(cv, elapsed, mult, o = {}) {
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  if (!w || !h) return;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) { cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr); }
  const ctx = cv.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const pl = 48, pb = 26, pt = 20, pr = 30;
  const tMax = Math.max(10000, elapsed * 1.12), mMax = Math.max(2, mult * 1.15);
  const X = (t) => pl + (t / tMax) * (w - pl - pr);
  const Y = (m) => h - pb - ((m - 1) / (mMax - 1)) * (h - pb - pt);
  ctx.font = '12px system-ui'; ctx.fillStyle = '#8a90b5'; ctx.strokeStyle = 'rgba(255,255,255,.06)'; ctx.lineWidth = 1;
  const ms = niceStep((mMax - 1) / 4);
  for (let k = 0; 1 + k * ms <= mMax; k++) {
    const m = 1 + k * ms, y = Y(m);
    ctx.beginPath(); ctx.moveTo(pl, y); ctx.lineTo(w - pr, y); ctx.stroke();
    ctx.fillText((ms < 1 ? m.toFixed(1) : m.toFixed(0)) + '×', 8, y + 4);
  }
  const ts = niceStep(tMax / 1000 / 6);
  for (let k = 0; k * ts * 1000 <= tMax; k++) ctx.fillText(Math.round(k * ts) + 'с', X(k * ts * 1000) - 6, h - 8);
  const col = o.crashed ? '#ef4444' : o.color || '#7c5cff';
  const N = 90;
  ctx.beginPath();
  for (let i = 0; i <= N; i++) { const t = (elapsed * i) / N; const x = X(t), y = Y(Math.exp(GROWTH * t)); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }
  ctx.strokeStyle = col; ctx.lineWidth = 4; ctx.lineCap = 'round'; ctx.stroke();
  ctx.lineTo(X(elapsed), Y(1)); ctx.lineTo(X(0), Y(1)); ctx.closePath();
  const g = ctx.createLinearGradient(0, pt, 0, h - pb);
  g.addColorStop(0, col + '66'); g.addColorStop(1, col + '05');
  ctx.fillStyle = g; ctx.fill();
  for (const mk of o.marks || []) {
    if (mk.m > mult) continue;
    const x = X(timeFor(mk.m)), y = Y(mk.m);
    ctx.fillStyle = '#22c55e'; ctx.beginPath(); ctx.arc(x, y, 6, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = '#e8eaf6'; ctx.font = 'bold 12px system-ui';
    ctx.fillText(`${mk.label} ×${mk.m.toFixed(2)}`, x + 9, y - 8);
  }
  if (o.emoji) {
    const tx = X(elapsed), ty = Y(mult);
    const dX = (w - pl - pr) / tMax, dY = -((GROWTH * mult) / (mMax - 1)) * (h - pb - pt);
    const ang = Math.atan2(dY, dX);
    ctx.save(); ctx.translate(tx, ty);
    if (!o.crashed) ctx.rotate(ang + Math.PI / 4);
    ctx.font = '36px serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    if (o.idle) ctx.globalAlpha = 0.6 + 0.4 * Math.sin(Date.now() / 200);
    ctx.fillText(o.emoji, 0, 0);
    ctx.restore();
  }
}

// ───────────────────────── главный цикл ─────────────────────────
function frame() {
  requestAnimationFrame(frame);
  if (!S.me) return;
  if (S.tab === 'crash') drawCrashDuel();
  else if (S.tab === 'plane') drawPlane();
  else if (S.tab === 'roulette') updateRoulTimer();
  else if (S.tab === 'cases') tickBattle(false);
}
