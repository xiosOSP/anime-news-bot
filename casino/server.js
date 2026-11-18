// РофлКазино — демо-казино для игры с друзьями. Деньги ненастоящие.
const express = require('express');
const http = require('http');
const { Server } = require('socket.io');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const PORT = process.env.PORT || 3000;
const START_BALANCE = 10000;
const REFILL = 10000;
const DATA_FILE = process.env.DATA_FILE || path.join(__dirname, 'data.json');

const app = express();
app.use(express.static(path.join(__dirname, 'public')));
const server = http.createServer(app);
const io = new Server(server);

// ───────────────────────── утилиты ─────────────────────────
const rnd = () => crypto.randomInt(0, 2 ** 47) / 2 ** 47; // [0,1)
const randInt = (n) => crypto.randomInt(0, n);
const r2 = (x) => Math.round(x * 100) / 100;
const rub = (x) => r2(x).toLocaleString('ru-RU') + ' ₽';
const now = () => Date.now();

function pickDistinct(n, k) {
  const arr = [...Array(n).keys()];
  for (let i = n - 1; i > 0; i--) {
    const j = randInt(i + 1);
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
  return arr.slice(0, k);
}
function parseBet(x) {
  const v = r2(Number(x));
  if (!Number.isFinite(v) || v < 1) throw new Error('Минимальная ставка — 1 ₽');
  if (v > 1e9) throw new Error('Слишком большая ставка');
  return v;
}
function clampInt(x, lo, hi, def) {
  const v = Math.floor(Number(x));
  if (!Number.isFinite(v)) return def;
  return Math.max(lo, Math.min(hi, v));
}

// Рост множителя у краша и самолётика: m(t) = e^(k·t)
const GROWTH = 0.00009; // 2x ≈ 7.7 c, 10x ≈ 25.6 c
const multAt = (ms) => (ms <= 0 ? 1 : Math.floor(100 * Math.exp(GROWTH * ms)) / 100);
const timeFor = (m) => Math.log(m) / GROWTH;
// edge = 0.97 → 3% преимущество казино; edge = 1 → честно (для дуэлей)
function genCrash(edge) {
  const c = edge / (1 - rnd());
  return Math.min(1000, Math.max(1, Math.floor(c * 100) / 100));
}

// ───────────────────────── хранилище ─────────────────────────
let db = { users: {} };
try { db = JSON.parse(fs.readFileSync(DATA_FILE, 'utf8')); } catch { /* первый запуск */ }
if (!db.users) db.users = {};
let saveTimer = null;
function save() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    fs.writeFile(DATA_FILE + '.tmp', JSON.stringify(db), (err) => {
      if (!err) fs.rename(DATA_FILE + '.tmp', DATA_FILE, () => {});
    });
  }, 400);
}

const online = new Map(); // name -> кол-во подключений
const publicUser = (u) => ({
  name: u.name, balance: u.balance, refills: u.refills, wagered: u.wagered,
  best: u.best, online: (online.get(u.name) || 0) > 0,
});
function pushUser(name) {
  const u = db.users[name];
  if (u) io.to('u:' + name).emit('me', publicUser(u));
  broadcastPlayers();
}
let playersTimer = null;
function broadcastPlayers() {
  if (playersTimer) return;
  playersTimer = setTimeout(() => {
    playersTimer = null;
    io.emit('players', Object.values(db.users).map(publicUser).sort((a, b) => b.balance - a.balance));
  }, 50);
}
function take(name, amount) {
  const u = db.users[name];
  if (!(amount > 0) || amount > u.balance + 1e-9) throw new Error('Недостаточно средств');
  u.balance = r2(u.balance - amount);
  u.wagered = r2((u.wagered || 0) + amount);
  save(); pushUser(name);
}
function give(name, amount, mult) {
  const u = db.users[name];
  if (!u || !(amount > 0)) return;
  u.balance = r2(u.balance + amount);
  if (mult && mult > 1 && mult > (u.best || 0)) u.best = r2(mult);
  save(); pushUser(name);
}

// лента событий и чат
const feed = [];
function addFeed(text, kind = 'info') {
  const item = { t: now(), text, kind };
  feed.unshift(item); feed.length = Math.min(feed.length, 40);
  io.emit('feed', item);
}
const chat = [];

// ───────────────────────── кейсы ─────────────────────────
const CASES = [
  {
    id: 'bomzh', name: 'Бомж-кейс', emoji: '🗑️', price: 100, items: [
      { name: 'Семечки', emoji: '🌻', value: 10, w: 300 },
      { name: 'Доширак', emoji: '🍜', value: 30, w: 260 },
      { name: 'Шаурма', emoji: '🌯', value: 70, w: 200 },
      { name: 'Пиво', emoji: '🍺', value: 110, w: 120 },
      { name: 'Пицца', emoji: '🍕', value: 180, w: 70 },
      { name: 'Кроссы с рынка', emoji: '👟', value: 450, w: 30 },
      { name: 'Самокат', emoji: '🛴', value: 1200, w: 12 },
      { name: 'Айфон', emoji: '📱', value: 6000, w: 3 },
    ],
  },
  {
    id: 'student', name: 'Студент', emoji: '🎒', price: 500, items: [
      { name: 'Проездной', emoji: '🚌', value: 80, w: 300 },
      { name: 'Энергетик ящик', emoji: '⚡', value: 200, w: 250 },
      { name: 'Стипендия', emoji: '💵', value: 450, w: 200 },
      { name: 'Наушники', emoji: '🎧', value: 900, w: 120 },
      { name: 'Видеокарта б/у', emoji: '🖥️', value: 2000, w: 45 },
      { name: 'Ноутбук', emoji: '💻', value: 5000, w: 10 },
      { name: 'Автомат по сессии', emoji: '🎓', value: 15000, w: 2 },
    ],
  },
  {
    id: 'major', name: 'Мажор', emoji: '💎', price: 2500, items: [
      { name: 'Кальян', emoji: '💨', value: 400, w: 300 },
      { name: 'Кроссовки Jordan', emoji: '👟', value: 1200, w: 250 },
      { name: 'Пятёрка', emoji: '💸', value: 2500, w: 200 },
      { name: 'PlayStation', emoji: '🎮', value: 5000, w: 120 },
      { name: 'Часы', emoji: '⌚', value: 12000, w: 30 },
      { name: 'Байк', emoji: '🏍️', value: 30000, w: 7 },
      { name: 'Гелик', emoji: '🚙', value: 100000, w: 1 },
    ],
  },
  {
    id: 'oligarch', name: 'Олигарх', emoji: '👑', price: 10000, items: [
      { name: 'Ужин в ресторане', emoji: '🍾', value: 2000, w: 450 },
      { name: 'Шуба', emoji: '🧥', value: 5000, w: 250 },
      { name: 'Котлета налички', emoji: '💰', value: 10000, w: 200 },
      { name: 'Ролекс', emoji: '⌚', value: 25000, w: 90 },
      { name: 'Порш', emoji: '🏎️', value: 60000, w: 30 },
      { name: 'Яхта', emoji: '🛥️', value: 200000, w: 5 },
      { name: 'Остров', emoji: '🏝️', value: 500000, w: 1 },
    ],
  },
];
for (const c of CASES) {
  const W = c.items.reduce((s, i) => s + i.w, 0);
  c.ev = r2(c.items.reduce((s, i) => s + i.value * i.w, 0) / W);
  for (const it of c.items) {
    it.chance = Math.round((it.w / W) * 10000) / 100;
    const k = it.value / c.price;
    it.rarity = k < 0.5 ? 'common' : k < 1.2 ? 'rare' : k < 4 ? 'epic' : k < 20 ? 'legend' : 'gold';
  }
}
const caseById = (id) => CASES.find((c) => c.id === id);
function rollCase(c) {
  const W = c.items.reduce((s, i) => s + i.w, 0);
  let x = rnd() * W;
  for (const it of c.items) { if ((x -= it.w) < 0) return it; }
  return c.items[0];
}
const itemView = (it) => ({ name: it.name, emoji: it.emoji, value: it.value, rarity: it.rarity });
const CASE_SPIN_MS = 5200;

// ───────────────────────── мины (соло) ─────────────────────────
const MINES_CELLS = 25;
function minesMult(mines, n) {
  if (n <= 0) return 1;
  let m = 0.97;
  for (let i = 0; i < n; i++) m *= (MINES_CELLS - i) / (MINES_CELLS - mines - i);
  return Math.floor(m * 100) / 100;
}
const minesSolo = {}; // name -> {bet, mines, field:Set, opened:[]}
function minesSoloView(name) {
  const s = minesSolo[name];
  if (!s) return null;
  return {
    bet: s.bet, mines: s.mines, opened: s.opened,
    mult: minesMult(s.mines, s.opened.length),
    next: minesMult(s.mines, s.opened.length + 1),
  };
}

// ───────────────────────── башни ─────────────────────────
const TOWER_ROWS = 8;
const TOWER_DIFF = {
  easy: { cols: 4, bombs: 1, label: 'Лёгкая' },
  medium: { cols: 3, bombs: 1, label: 'Средняя' },
  hard: { cols: 2, bombs: 1, label: 'Сложная' },
  extreme: { cols: 3, bombs: 2, label: 'Экстрим' },
};
function towerMult(diff, level) {
  if (level <= 0) return 1;
  const d = TOWER_DIFF[diff];
  return Math.floor(0.97 * Math.pow(d.cols / (d.cols - d.bombs), level) * 100) / 100;
}
const genTower = (diff) => {
  const d = TOWER_DIFF[diff];
  return Array.from({ length: TOWER_ROWS }, () => pickDistinct(d.cols, d.bombs));
};
const towersSolo = {}; // name -> {bet, diff, layout, picks:[]}
function towersSoloView(name) {
  const s = towersSolo[name];
  if (!s) return null;
  return {
    bet: s.bet, diff: s.diff, picks: s.picks, level: s.picks.length,
    mult: towerMult(s.diff, s.picks.length), next: towerMult(s.diff, s.picks.length + 1),
  };
}

// ───────────────────────── самолётик (общий раунд) ─────────────────────────
const PLANE_BET_MS = 8000;
const PLANE_AFTER_MS = 4000;
const plane = { phase: 'betting', round: 0, betEndsAt: 0, flyStart: 0, crash: 1, bets: {}, history: [] };
let planeTick = null;
function planeView() {
  return {
    phase: plane.phase, round: plane.round, betEndsAt: plane.betEndsAt, flyStart: plane.flyStart,
    crash: plane.phase === 'crashed' ? plane.crash : null,
    bets: Object.entries(plane.bets).map(([name, b]) => ({ name, amount: b.amount, auto: b.auto, cashedAt: b.cashedAt, win: b.win })),
    history: plane.history, serverNow: now(),
  };
}
const emitPlane = () => io.emit('plane', planeView());
function planeCashout(name, m) {
  const b = plane.bets[name];
  b.cashedAt = m; b.win = r2(b.amount * m);
  give(name, b.win, m);
  if (m >= 10) addFeed(`✈️ ${name}: ×${m.toFixed(2)} в самолётике → +${rub(b.win)}`, 'win');
}
function planeBetting() {
  plane.phase = 'betting'; plane.round++; plane.bets = {};
  plane.betEndsAt = now() + PLANE_BET_MS;
  emitPlane();
  setTimeout(planeFly, PLANE_BET_MS);
}
function planeFly() {
  plane.phase = 'flying'; plane.crash = genCrash(0.97); plane.flyStart = now();
  const crashAt = plane.flyStart + timeFor(plane.crash);
  emitPlane();
  planeTick = setInterval(() => {
    const t = now();
    let changed = false;
    for (const [name, b] of Object.entries(plane.bets)) {
      if (b.cashedAt || !b.auto || b.auto > plane.crash) continue;
      if (t >= plane.flyStart + timeFor(b.auto)) { planeCashout(name, b.auto); changed = true; }
    }
    if (t >= crashAt) { clearInterval(planeTick); planeCrash(); return; }
    if (changed) emitPlane();
  }, 50);
}
function planeCrash() {
  plane.phase = 'crashed';
  plane.history.unshift(plane.crash); plane.history.length = Math.min(plane.history.length, 25);
  emitPlane();
  setTimeout(planeBetting, PLANE_AFTER_MS);
}

// ───────────────────────── рулетка (общий стол) ─────────────────────────
const RED = new Set([1, 3, 5, 7, 9, 12, 14, 16, 18, 19, 21, 23, 25, 27, 30, 32, 34, 36]);
const colorOf = (n) => (n === 0 ? 'green' : RED.has(n) ? 'red' : 'black');
const ROUL_BET_MS = 15000, ROUL_SPIN_MS = 7000, ROUL_RESULT_MS = 4000;
const roul = { phase: 'betting', endsAt: 0, bets: [], result: null, history: [], wins: {}, round: 0 };
const ROUL_TYPES = {
  num: { mult: 36, test: (n, v) => n === v },
  red: { mult: 2, test: (n) => colorOf(n) === 'red' },
  black: { mult: 2, test: (n) => colorOf(n) === 'black' },
  even: { mult: 2, test: (n) => n !== 0 && n % 2 === 0 },
  odd: { mult: 2, test: (n) => n % 2 === 1 },
  low: { mult: 2, test: (n) => n >= 1 && n <= 18 },
  high: { mult: 2, test: (n) => n >= 19 },
  dozen: { mult: 3, test: (n, v) => n !== 0 && Math.ceil(n / 12) === v },
  column: { mult: 3, test: (n, v) => n !== 0 && ((n - 1) % 3) + 1 === v },
};
function roulView() {
  return {
    phase: roul.phase, endsAt: roul.endsAt, round: roul.round,
    result: roul.phase === 'betting' ? null : roul.result,
    bets: roul.bets.map((b) => ({ name: b.name, type: b.type, value: b.value, amount: b.amount })),
    wins: roul.phase === 'result' ? roul.wins : {},
    history: roul.history, serverNow: now(),
  };
}
const emitRoul = () => io.emit('roulette', roulView());
function roulBetting() {
  roul.phase = 'betting'; roul.round++; roul.bets = []; roul.wins = {}; roul.result = null;
  roul.endsAt = now() + ROUL_BET_MS;
  emitRoul();
  setTimeout(roulSpin, ROUL_BET_MS);
}
function roulSpin() {
  roul.phase = 'spinning'; roul.result = randInt(37); roul.endsAt = now() + ROUL_SPIN_MS;
  emitRoul();
  setTimeout(roulSettle, ROUL_SPIN_MS);
}
function roulSettle() {
  const n = roul.result;
  const wins = {};
  for (const b of roul.bets) {
    const T = ROUL_TYPES[b.type];
    if (T.test(n, b.value)) wins[b.name] = r2((wins[b.name] || 0) + b.amount * T.mult);
  }
  for (const [name, w] of Object.entries(wins)) {
    give(name, w);
    if (w >= 5000) addFeed(`🎡 ${name}: +${rub(w)} в рулетке (выпало ${n})`, 'win');
  }
  roul.wins = wins; roul.phase = 'result'; roul.endsAt = now() + ROUL_RESULT_MS;
  roul.history.unshift(n); roul.history.length = Math.min(roul.history.length, 30);
  emitRoul();
  setTimeout(roulBetting, ROUL_RESULT_MS);
}

// ───────────────────────── дуэли 1 на 1 ─────────────────────────
const GAME_NAMES = { crash: 'краш-дуэль', mines: 'мины-дуэль', towers: 'башни-дуэль', cases: 'кейс-батл' };
const duels = new Map();
let duelSeq = 1;
const activeDuelOf = (name) => [...duels.values()].find((d) => d.status !== 'done' && d.players.includes(name));

function duelView(d, viewer) {
  const done = d.status === 'done';
  const base = {
    id: d.id, game: d.game, stake: d.stake, config: d.config, creator: d.creator,
    players: d.players, status: d.status, createdAt: d.createdAt, result: d.result,
  };
  const s = d.state;
  if (!s) return base;
  if (d.game === 'crash') {
    base.state = { startAt: s.startAt, cash: s.cash, crash: done ? s.crash : null };
  } else if (d.game === 'mines') {
    const p = {};
    for (const [n, ps] of Object.entries(s.p)) p[n] = { count: ps.opened.length, status: ps.status, opened: n === viewer || done ? ps.opened : null, hit: ps.hit };
    base.state = { p, field: done ? s.field : null };
  } else if (d.game === 'towers') {
    const p = {};
    for (const [n, ps] of Object.entries(s.p)) p[n] = { level: ps.picks.length, status: ps.status, picks: n === viewer || done ? ps.picks : null, hit: ps.hit };
    base.state = { p, layout: done ? s.layout : null };
  } else if (d.game === 'cases') {
    base.state = { rolls: s.rolls, startAt: s.startAt };
  }
  return base;
}
function broadcastDuels() {
  const list = [...duels.values()].sort((a, b) => b.createdAt - a.createdAt);
  for (const name of online.keys()) {
    if (!online.get(name)) continue;
    io.to('u:' + name).emit('duels', { list: list.map((d) => duelView(d, name)), serverNow: now() });
  }
}
function cleanupDuels() {
  const done = [...duels.values()].filter((d) => d.status === 'done').sort((a, b) => b.createdAt - a.createdAt);
  for (const d of done.slice(12)) duels.delete(d.id);
}

function settleDuel(d, scores) {
  if (d.status === 'done') return;
  clearTimeout(d.timer);
  d.status = 'done';
  const [a, b] = d.players;
  const pot = d.game === 'cases' ? null : d.stake * 2;
  let winner = null;
  if (scores[a] > scores[b]) winner = a; else if (scores[b] > scores[a]) winner = b;
  const game = GAME_NAMES[d.game];
  if (d.game === 'cases') {
    const ta = scores[a], tb = scores[b];
    if (winner) {
      give(winner, r2(ta + tb));
      addFeed(`🏆 ${winner} побеждает ${winner === a ? b : a} (${game}) → +${rub(ta + tb)}`, 'duel');
    } else {
      give(a, ta); give(b, tb);
      addFeed(`🤝 Ничья в кейс-батле ${a} vs ${b}`, 'duel');
    }
    d.result = { winner, scores, prize: winner ? r2(ta + tb) : null };
  } else {
    if (winner) {
      give(winner, pot);
      addFeed(`🏆 ${winner} побеждает ${winner === a ? b : a} (${game}) → +${rub(pot)}`, 'duel');
    } else {
      give(a, d.stake); give(b, d.stake);
      addFeed(`🤝 Ничья: ${a} vs ${b} (${game}) — ставки вернулись`, 'duel');
    }
    d.result = { winner, scores, prize: winner ? pot : null };
  }
  cleanupDuels();
  broadcastDuels();
}

function startDuel(d) {
  d.status = 'playing';
  const [a, b] = d.players;
  if (d.game === 'crash') {
    const crash = genCrash(1);
    d.state = { crash, startAt: now() + 4000, cash: {} };
    d.timer = setTimeout(() => settleDuel(d, { [a]: d.state.cash[a] || 0, [b]: d.state.cash[b] || 0 }),
      d.state.startAt - now() + timeFor(crash) + 30);
  } else if (d.game === 'mines') {
    const fresh = () => ({ opened: [], status: 'playing', hit: null });
    d.state = { field: pickDistinct(MINES_CELLS, d.config.mines), p: { [a]: fresh(), [b]: fresh() } };
  } else if (d.game === 'towers') {
    const fresh = () => ({ picks: [], status: 'playing', hit: null });
    d.state = { layout: genTower(d.config.diff), p: { [a]: fresh(), [b]: fresh() } };
  } else if (d.game === 'cases') {
    const c = caseById(d.config.caseId);
    const rolls = { [a]: [], [b]: [] };
    for (let i = 0; i < d.config.count; i++) { rolls[a].push(itemView(rollCase(c))); rolls[b].push(itemView(rollCase(c))); }
    d.state = { rolls, startAt: now() + 1500 };
    const sum = (arr) => r2(arr.reduce((s, it) => s + it.value, 0));
    d.timer = setTimeout(() => settleDuel(d, { [a]: sum(rolls[a]), [b]: sum(rolls[b]) }),
      1500 + d.config.count * (CASE_SPIN_MS + 800));
  }
}
function scoreDuelBoard(d) {
  // для мин и башен: остановился → счёт, подорвался/сдался → 0
  const s = d.state.p;
  if (Object.values(s).some((p) => p.status === 'playing')) return;
  const scores = {};
  for (const [n, p] of Object.entries(s)) scores[n] = p.status === 'stopped' ? (d.game === 'mines' ? p.opened.length : p.picks.length) : 0;
  settleDuel(d, scores);
}
function myDuel(name, id, game) {
  const d = duels.get(Number(id));
  if (!d || !d.players.includes(name)) throw new Error('Дуэль не найдена');
  if (d.status !== 'playing') throw new Error('Дуэль не идёт');
  if (game && d.game !== game) throw new Error('Не та игра');
  return d;
}

// ───────────────────────── сокеты ─────────────────────────
io.on('connection', (socket) => {
  let me = null;
  const on = (ev, fn) => socket.on(ev, (data, ack) => {
    if (typeof ack !== 'function') ack = () => {};
    try {
      if (ev !== 'login' && !me) throw new Error('Сначала войди');
      ack({ ok: true, ...(fn(data || {}) || {}) });
    } catch (e) {
      ack({ error: e.message || String(e) });
    }
  });

  on('login', ({ name, token }) => {
    name = String(name || '').trim().replace(/\s+/g, ' ');
    token = String(token || '');
    if (name.length < 2 || name.length > 16) throw new Error('Ник: от 2 до 16 символов');
    if (!/^[\p{L}\p{N} _.\-]+$/u.test(name)) throw new Error('Ник: только буквы, цифры, пробел, _ . -');
    if (token.length < 10) throw new Error('Плохой токен');
    let u = db.users[name];
    if (u && u.token !== token) throw new Error('Этот ник уже занят');
    const isNew = !u;
    if (!u) {
      u = db.users[name] = { name, token, balance: START_BALANCE, refills: 0, wagered: 0, best: 0, createdAt: now() };
      save();
    }
    if (me) return { me: publicUser(u) };
    me = name;
    socket.join('u:' + me);
    online.set(me, (online.get(me) || 0) + 1);
    if (isNew) addFeed(`👋 Новый игрок: ${me} (стартовые ${rub(START_BALANCE)})`);
    broadcastPlayers();
    setTimeout(broadcastDuels, 0);
    return {
      me: publicUser(u), feed, chat, cases: CASES, towerDiff: TOWER_DIFF, towerRows: TOWER_ROWS,
      plane: planeView(), roulette: roulView(),
      mines: minesSoloView(me), towers: towersSoloView(me), serverNow: now(),
    };
  });

  socket.on('disconnect', () => {
    if (!me) return;
    online.set(me, Math.max(0, (online.get(me) || 1) - 1));
    broadcastPlayers();
  });

  on('chat', ({ text }) => {
    text = String(text || '').trim().slice(0, 200);
    if (!text) return;
    const m = { name: me, text, t: now() };
    chat.push(m); if (chat.length > 60) chat.shift();
    io.emit('chat', m);
  });

  on('refill', () => {
    const u = db.users[me];
    u.balance = r2(u.balance + REFILL); u.refills = (u.refills || 0) + 1;
    save(); pushUser(me);
    addFeed(`💸 ${me}: ещё ${rub(REFILL)} в долг у мамы (займ №${u.refills})`, 'refill');
  });

  on('transfer', ({ to, amount }) => {
    amount = parseBet(amount);
    if (!db.users[to] || to === me) throw new Error('Кому переводим?');
    take(me, amount); db.users[me].wagered = r2(db.users[me].wagered - amount);
    give(to, amount);
    addFeed(`🤝 Перевод ${me} → ${to}: ${rub(amount)}`);
  });

  // ── мины соло
  on('mines:start', ({ bet, mines }) => {
    if (minesSolo[me]) throw new Error('Игра уже идёт');
    bet = parseBet(bet); mines = clampInt(mines, 1, 24, 3);
    take(me, bet);
    minesSolo[me] = { bet, mines, field: new Set(pickDistinct(MINES_CELLS, mines)), opened: [] };
    return { state: minesSoloView(me) };
  });
  on('mines:open', ({ cell }) => {
    const s = minesSolo[me];
    if (!s) throw new Error('Игра не начата');
    cell = clampInt(cell, 0, MINES_CELLS - 1, -1);
    if (s.opened.includes(cell)) throw new Error('Уже открыта');
    if (s.field.has(cell)) {
      delete minesSolo[me];
      return { boom: true, cell, field: [...s.field], opened: s.opened };
    }
    s.opened.push(cell);
    if (s.opened.length === MINES_CELLS - s.mines) return finishMines();
    return { state: minesSoloView(me) };
  });
  function finishMines() {
    const s = minesSolo[me];
    if (!s) throw new Error('Игра не начата');
    if (!s.opened.length) throw new Error('Открой хотя бы одну клетку');
    const mult = minesMult(s.mines, s.opened.length);
    const win = r2(s.bet * mult);
    delete minesSolo[me];
    give(me, win, mult);
    if (mult >= 5) addFeed(`💣 ${me}: +${rub(win)} в минах (×${mult})`, 'win');
    return { cashout: true, win, mult, field: [...s.field], opened: s.opened };
  }
  on('mines:cashout', finishMines);

  // ── башни соло
  on('towers:start', ({ bet, diff }) => {
    if (towersSolo[me]) throw new Error('Игра уже идёт');
    if (!TOWER_DIFF[diff]) throw new Error('Неизвестная сложность');
    bet = parseBet(bet);
    take(me, bet);
    towersSolo[me] = { bet, diff, layout: genTower(diff), picks: [] };
    return { state: towersSoloView(me) };
  });
  on('towers:pick', ({ col }) => {
    const s = towersSolo[me];
    if (!s) throw new Error('Игра не начата');
    col = clampInt(col, 0, TOWER_DIFF[s.diff].cols - 1, 0);
    const row = s.picks.length;
    if (s.layout[row].includes(col)) {
      delete towersSolo[me];
      return { boom: true, row, col, layout: s.layout, picks: s.picks };
    }
    s.picks.push(col);
    if (s.picks.length === TOWER_ROWS) return finishTowers();
    return { state: towersSoloView(me) };
  });
  function finishTowers() {
    const s = towersSolo[me];
    if (!s) throw new Error('Игра не начата');
    if (!s.picks.length) throw new Error('Пройди хотя бы один этаж');
    const mult = towerMult(s.diff, s.picks.length);
    const win = r2(s.bet * mult);
    delete towersSolo[me];
    give(me, win, mult);
    if (mult >= 5) addFeed(`🗼 ${me}: ${s.picks.length} этаж в башнях → +${rub(win)} (×${mult})`, 'win');
    return { cashout: true, win, mult, layout: s.layout, picks: s.picks };
  }
  on('towers:cashout', finishTowers);

  // ── кейсы
  on('case:open', ({ id }) => {
    const c = caseById(id);
    if (!c) throw new Error('Нет такого кейса');
    take(me, c.price);
    const it = rollCase(c);
    const name = me;
    setTimeout(() => {
      give(name, it.value, it.value / c.price);
      if (it.value >= c.price * 5) addFeed(`📦 ${name}: ${it.emoji} ${it.name} (${rub(it.value)}) из кейса «${c.name}»`, 'win');
    }, CASE_SPIN_MS);
    return { item: itemView(it), spinMs: CASE_SPIN_MS };
  });

  // ── самолётик
  on('plane:bet', ({ amount, auto }) => {
    if (plane.phase !== 'betting') throw new Error('Ставки закрыты, жди следующий раунд');
    if (plane.bets[me]) throw new Error('Ставка уже сделана');
    amount = parseBet(amount);
    auto = Number(auto) >= 1.01 ? Math.floor(Number(auto) * 100) / 100 : null;
    take(me, amount);
    plane.bets[me] = { amount, auto, cashedAt: null, win: 0 };
    emitPlane();
  });
  on('plane:cashout', () => {
    const b = plane.bets[me];
    if (plane.phase !== 'flying' || !b || b.cashedAt) throw new Error('Нечего забирать');
    const m = multAt(now() - plane.flyStart);
    if (m >= plane.crash) throw new Error('Поздно — улетел!');
    planeCashout(me, m);
    emitPlane();
    return { mult: m, win: b.win };
  });

  // ── рулетка
  on('roulette:bet', ({ type, value, amount }) => {
    if (roul.phase !== 'betting') throw new Error('Ставки закрыты');
    const T = ROUL_TYPES[type];
    if (!T) throw new Error('Неизвестная ставка');
    amount = parseBet(amount);
    if (type === 'num') value = clampInt(value, 0, 36, 0);
    else if (type === 'dozen' || type === 'column') value = clampInt(value, 1, 3, 1);
    else value = null;
    if (roul.bets.filter((b) => b.name === me).length >= 60) throw new Error('Слишком много ставок');
    take(me, amount);
    roul.bets.push({ name: me, type, value, amount });
    emitRoul();
  });
  on('roulette:clear', () => {
    if (roul.phase !== 'betting') throw new Error('Ставки закрыты');
    const mine = roul.bets.filter((b) => b.name === me);
    if (!mine.length) return;
    roul.bets = roul.bets.filter((b) => b.name !== me);
    const sum = r2(mine.reduce((s, b) => s + b.amount, 0));
    give(me, sum); db.users[me].wagered = r2(db.users[me].wagered - sum);
    emitRoul();
  });

  // ── дуэли
  on('duel:create', ({ game, stake, config }) => {
    if (!GAME_NAMES[game]) throw new Error('Неизвестная игра');
    if (activeDuelOf(me)) throw new Error('У тебя уже есть активная дуэль');
    config = config || {};
    let cfg = {};
    if (game === 'mines') cfg = { mines: clampInt(config.mines, 1, 24, 5) };
    if (game === 'towers') { if (!TOWER_DIFF[config.diff]) throw new Error('Неизвестная сложность'); cfg = { diff: config.diff }; }
    if (game === 'cases') {
      const c = caseById(config.caseId);
      if (!c) throw new Error('Нет такого кейса');
      cfg = { caseId: c.id, count: clampInt(config.count, 1, 5, 1) };
      stake = c.price * cfg.count;
    }
    stake = parseBet(stake);
    take(me, stake);
    const d = { id: duelSeq++, game, stake, config: cfg, creator: me, players: [me], status: 'open', createdAt: now(), state: null, result: null };
    duels.set(d.id, d);
    addFeed(`⚔️ ${me} ищет соперника: ${GAME_NAMES[game]} на ${rub(stake)}`, 'duel');
    broadcastDuels();
    return { id: d.id };
  });
  on('duel:cancel', ({ id }) => {
    const d = duels.get(Number(id));
    if (!d || d.creator !== me || d.status !== 'open') throw new Error('Нельзя отменить');
    duels.delete(d.id);
    give(me, d.stake); db.users[me].wagered = r2(db.users[me].wagered - d.stake);
    broadcastDuels();
  });
  on('duel:join', ({ id }) => {
    const d = duels.get(Number(id));
    if (!d || d.status !== 'open') throw new Error('Дуэль уже недоступна');
    if (d.creator === me) throw new Error('Нельзя играть с самим собой 🙂');
    if (activeDuelOf(me)) throw new Error('У тебя уже есть активная дуэль');
    take(me, d.stake);
    d.players.push(me);
    startDuel(d);
    broadcastDuels();
  });
  on('duel:cashout', ({ id }) => {
    const d = myDuel(me, id, 'crash');
    const s = d.state;
    if (s.cash[me]) throw new Error('Уже выведено');
    const el = now() - s.startAt;
    if (el < 0) throw new Error('Раунд ещё не начался');
    const m = multAt(el);
    if (m >= s.crash) throw new Error('Поздно — краш!');
    s.cash[me] = m;
    const [a, b] = d.players;
    if (s.cash[a] && s.cash[b]) settleDuel(d, { [a]: s.cash[a], [b]: s.cash[b] });
    else broadcastDuels();
    return { mult: m };
  });
  on('duel:mines:open', ({ id, cell }) => {
    const d = myDuel(me, id, 'mines');
    const p = d.state.p[me];
    if (p.status !== 'playing') throw new Error('Твоя игра уже закончена');
    cell = clampInt(cell, 0, MINES_CELLS - 1, -1);
    if (p.opened.includes(cell)) throw new Error('Уже открыта');
    if (d.state.field.includes(cell)) { p.status = 'boom'; p.hit = cell; }
    else {
      p.opened.push(cell);
      if (p.opened.length === MINES_CELLS - d.config.mines) p.status = 'stopped';
    }
    broadcastDuels();
    scoreDuelBoard(d);
    return { boom: p.status === 'boom' };
  });
  on('duel:towers:pick', ({ id, col }) => {
    const d = myDuel(me, id, 'towers');
    const p = d.state.p[me];
    if (p.status !== 'playing') throw new Error('Твоя игра уже закончена');
    col = clampInt(col, 0, TOWER_DIFF[d.config.diff].cols - 1, 0);
    const row = p.picks.length;
    if (d.state.layout[row].includes(col)) { p.status = 'boom'; p.hit = { row, col }; }
    else {
      p.picks.push(col);
      if (p.picks.length === TOWER_ROWS) p.status = 'stopped';
    }
    broadcastDuels();
    scoreDuelBoard(d);
    return { boom: p.status === 'boom' };
  });
  on('duel:stop', ({ id }) => {
    const d = myDuel(me, id);
    if (d.game !== 'mines' && d.game !== 'towers') throw new Error('Тут нельзя остановиться');
    const p = d.state.p[me];
    if (p.status !== 'playing') throw new Error('Твоя игра уже закончена');
    p.status = 'stopped';
    broadcastDuels();
    scoreDuelBoard(d);
  });
  on('duel:surrender', ({ id }) => {
    const d = myDuel(me, id);
    if (d.game !== 'mines' && d.game !== 'towers') throw new Error('Тут нельзя сдаться');
    const p = d.state.p[me];
    if (p.status !== 'playing') throw new Error('Твоя игра уже закончена');
    p.status = 'boom';
    broadcastDuels();
    scoreDuelBoard(d);
  });
});

planeBetting();
roulBetting();
server.listen(PORT, () => console.log(`🎰 РофлКазино запущено: http://localhost:${PORT}`));
