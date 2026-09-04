/* SLINGOR.IO client
   - server-authoritative, 60fps render with extrapolation + smoothing
   - baked sprites, culling, pooled particles, clamped DPR
   - procedural audio (Web Audio), no audio files
   - all module state declared up top; the sim itself never runs client-side
*/
(() => {
'use strict';

// ---- constants (a mirror of game/const.py for HUD-only math) ----
const WORLD = 4200;
const MAX_SPEED = 1280;          // SLING_MAX_SPEED
const PLAYER_R = 10.5;           // hard-coded render radius (base)
const MAGNET_R = 260;            // MAGNET_RADIUS (server authoritative)
const WRECK_DECAY = 26;          // WRECK_DECAY seconds
const RESPAWN_DELAY = 2.2;       // RESPAWN_DELAY seconds
const SLING_COMBO_MS = 6000;     // SLING_COMBO_WINDOW, for the HUD popup only

const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const hue = (h, s = 100, l = 60) => `hsl(${h},${s}%,${l}%)`;
const $ = (id) => document.getElementById(id);

// ---- canvas / DPR ----
const canvas = $('game');
const ctx = canvas.getContext('2d');
const mm = $('minimap');
const mmCtx = mm.getContext('2d');
const DPR_MIN = 2; // cap device pixel ratio for perf
const dpr = Math.min(window.devicePixelRatio || 1, DPR_MIN);

let W = 0;
let H = 0;

function resize() {
    W = canvas.width = Math.floor(innerWidth * dpr);
    H = canvas.height = Math.floor(innerHeight * dpr);
    canvas.style.width = innerWidth + 'px';
    canvas.style.height = innerHeight + 'px';
}
addEventListener('resize', resize);
resize();

// ---- world state (declared up front, single source of truth) ----
const entities = new Map(); // id -> ent
const cores = new Map();    // id -> {x,y,v}
const wrecks = new Map();   // id -> {x,y,v,left} (id = index)
const bonuses = new Map();  // id -> {x,y,k} (k = bonus kind index)
let bodies = [];
let selfId = null;
let my = null; // own entity + smoothed camera snapshot
let connected = false;
let offsetMs = 0; // server a (epoch ms) minus performance.now()
let pingMs = 0;
let pingSentAt = 0;
let worldMax = { w: WORLD, h: WORLD };

// render clock helpers
const nowMs = () => performance.now() + offsetMs;

// ---- skins / settings ----
const SKIN_DEFS = [
    { id: 'probe', hue: 180, label: 'ЗОНД' },
    { id: 'vortex', hue: 300, label: 'ВИХРЬ' },
    { id: 'hex', hue: 25, label: 'ГЕКС' },
    { id: 'blade', hue: 150, label: 'ЛЕЗВИЕ' },
];
const skinById = new Map(SKIN_DEFS.map(s => [s.id, s]));
const BONUS_META = [
    { c: '#00e5ff', label: 'ЩИТ' },   // shield
    { c: '#ffcc33', label: 'БУСТ' },  // boost
    { c: '#c77dff', label: 'МАГНИТ' },// magnet
];
const BONUS_IDX = { shield: 0, boost: 1, magnet: 2 };
let soundOn = true;
let chosenSkin = 'probe';
// persisted preferences (localStorage can throw — never let it break joining)
try {
    soundOn = localStorage.getItem('slingor_sound') !== '0';
    const persistedSkin = localStorage.getItem('slingor_skin');
    if (persistedSkin && skinById.has(persistedSkin)) chosenSkin = persistedSkin;
} catch (e) { /* storage unavailable */ }

// ---- camera ----
const cam = { x: WORLD / 2, y: WORLD / 2, scale: 0.85 };
let zoomTarget = 0.85;

// ---- particles (pooled, no per-frame allocation) ----
const MAX_PARTS = 420;
const parts = [];
let partHead = 0;

// ---- trails ----
const trails = new Map(); // id -> array of points
const TRAIL_STEPS = 48;

// ---- sprites ----
const spriteCache = new Map();

// ---- input ----
const keys = { up: false, down: false, left: false, right: false };
const pad = { x: 0, y: 0 }; // smoothed analog thrust vector, each axis in [-1,1]
const pressed = {};
let lastSent = '';      // last sent quantized pad signature
let lastPendingAt = 0;  // performance.now() when a pad change was last sent
let joy = null; // {ox,oy,x,y,id}
const GAME_KEYS = ['KeyW', 'KeyA', 'KeyS', 'KeyD', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'];

// ---- websocket / join ----
let ws = null;
let intentionalClose = false;
let joining = false; // a join attempt is in flight (submit guard)
let respawnAt = 0;   // performance.now() based; 0 = no countdown pending

// ---- HUD / fx ----
let lastPingSent = 0;
let lastFpsT = 0, fpsAcc = 0, fpsN = 0, fpsShown = 60;
let bigBoom = null;
let toastTimer = 0;
let slingFxTimer = 0;
let comboFxTimer = 0;
let shake = 0;      // screen shake impulse (0..1), decayed each frame
let flashA = 0;     // fullscreen flash alpha, decayed each frame
const popups = [];  // floating "+N" world-space texts {x,y,text,color,life}
let lastStats = null; // die-event round stats for the results panel
let lastTop = null;   // die-event room leader for the results panel

// ---- loop ----
let rafId = 0;
let lastT = performance.now();
let dtReal = 0.016;
let visible = !document.hidden;

// ------------------------------------------------------------- particles
function spawnParts(x, y, n, color, spd, life, size) {
    for (let i = 0; i < n; i++) {
        const p = parts[partHead] || (parts[partHead] = {});
        partHead = (partHead + 1) % MAX_PARTS;
        p.x = x; p.y = y;
        const a = Math.random() * Math.PI * 2;
        const s = spd * (0.3 + Math.random() * 0.7);
        p.vx = Math.cos(a) * s; p.vy = Math.sin(a) * s;
        p.life = life * (0.5 + Math.random() * 0.5);
        p.max = p.life;
        p.color = Array.isArray(color) ? color[(Math.random() * color.length) | 0] : color;
        p.size = size ? size * (0.6 + Math.random() * 0.8) : 4;
    }
}

function stepParticles(dt) {
    for (const p of parts) {
        if (p.life === undefined) continue;
        p.life -= dt;
        if (p.life <= 0) { p.life = undefined; continue; }
        p.x += p.vx * dt;
        p.y += p.vy * dt;
        p.vx *= (1 - 2.2 * dt);
        p.vy *= (1 - 2.2 * dt);
    }
}

function drawParticles() {
    for (const p of parts) {
        if (p.life === undefined) continue;
        const f = p.life / p.max;
        ctx.globalAlpha = f;
        ctx.fillStyle = p.color;
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.size * (0.4 + 0.6 * f), 0, 6.2832);
        ctx.fill();
    }
    ctx.globalAlpha = 1;
}

// ------------------------------------------------------------- trails
function pushTrail(id, x, y) {
    let t = trails.get(id);
    if (!t) { t = []; trails.set(id, t); }
    t.push({ x, y, a: 1 });
    if (t.length > TRAIL_STEPS) t.shift();
}
function stepTrails(dt) {
    for (const t of trails.values()) {
        for (const pt of t) pt.a -= dt * 3.2;
        while (t.length && t[0].a <= 0) t.shift();
    }
    for (const id of trails.keys()) {
        if (!entities.has(id)) trails.delete(id);
    }
}

// ------------------------------------------------------------- sprites
function makeSprite(kind, hueVal, r) {
    const key = kind + ':' + hueVal + ':' + Math.round(r);
    if (spriteCache.has(key)) return spriteCache.get(key);
    const S = Math.ceil(r * 2 + r * 3.2);
    const cv = document.createElement('canvas');
    cv.width = S; cv.height = S;
    const c = cv.getContext('2d');
    const cx = S / 2, cy = S / 2;
    const g = c.createRadialGradient(cx, cy, r * 0.4, cx, cy, S / 2);
    const hsl = hueVal === 40 ? '255,204,51' : hueVal === 180 ? '0,229,255' : hueVal === 300 ? '177,92,255'
        : hueVal === 25 ? '255,132,51' : hueVal === 340 ? '255,62,165' : hueVal === 150 ? '125,255,155' : hueVal === 260 ? '100,120,255' : '200,210,255';
    if (kind === 'star') {
        g.addColorStop(0, `rgba(${hsl},0.9)`);
        g.addColorStop(0.35, `rgba(${hsl},0.28)`);
        g.addColorStop(1, 'rgba(0,0,0,0)');
    } else {
        g.addColorStop(0, `rgba(${hsl},0.35)`);
        g.addColorStop(1, 'rgba(0,0,0,0)');
    }
    c.fillStyle = g;
    c.fillRect(0, 0, S, S);
    const bg = c.createRadialGradient(cx - r * 0.3, cy - r * 0.3, r * 0.1, cx, cy, r);
    bg.addColorStop(0, `hsl(${hueVal},70%,82%)`);
    bg.addColorStop(0.6, `hsl(${hueVal},75%,55%)`);
    bg.addColorStop(1, `hsl(${hueVal},80%,30%)`);
    c.fillStyle = bg;
    c.beginPath(); c.arc(cx, cy, r, 0, 6.2832); c.fill();
    if (kind === 'planet') {
        c.strokeStyle = `hsla(${hueVal},90%,70%,0.85)`;
        c.lineWidth = Math.max(2, r * 0.07);
        c.beginPath(); c.arc(cx, cy, r * 1.08, 0, 6.2832); c.stroke();
        c.strokeStyle = 'rgba(0,0,0,0.28)';
        c.lineWidth = r * 0.09;
        c.beginPath();
        c.ellipse(cx + r * 0.25, cy + r * 0.55, r * 0.22, r * 0.08, -0.4, 0, 6.2832);
        c.stroke();
    }
    const sp = { cv, s: S, r };
    spriteCache.set(key, sp);
    return sp;
}

// ------------------------------------------------------------- input
function keyCode(e) { return e.code || (e.key.length === 1 ? 'Key' + e.key.toUpperCase() : e.key); }
function keyP(k) { return !!pressed[k]; }
function keyInput() {
    keys.up = keyP('KeyW') || keyP('w') || keyP('ArrowUp');
    keys.down = keyP('KeyS') || keyP('s') || keyP('ArrowDown');
    keys.left = keyP('KeyA') || keyP('a') || keyP('ArrowLeft');
    keys.right = keyP('KeyD') || keyP('d') || keyP('ArrowRight');
}
addEventListener('keydown', (e) => {
    if (!$('menu').classList.contains('visible')) {
        const k = e.code || e.key;
        if (GAME_KEYS.includes(k)) e.preventDefault();
    }
    pressed[keyCode(e)] = true;
});
addEventListener('keyup', (e) => {
    pressed[keyCode(e)] = false;
});

canvas.addEventListener('pointerdown', (e) => {
    if (!e.isPrimary) return;
    if (!$('menu').classList.contains('visible')) {
        joy = { ox: e.clientX, oy: e.clientY, x: e.clientX, y: e.clientY, id: e.pointerId };
    }
});
addEventListener('pointermove', (e) => {
    if (joy && e.pointerId === joy.id) { joy.x = e.clientX; joy.y = e.clientY; }
});
addEventListener('pointerup', (e) => {
    if (joy && e.pointerId === joy.id) { joy = null; }
});
addEventListener('pointercancel', (e) => {
    if (joy && e.pointerId === joy.id) { joy = null; }
});

// desired thrust vector this frame, each axis in [-1..1]:
// joystick uses an eased analog magnitude, keyboard diagonals are capped to
// unit length so diagonal flight is not faster than cardinal.
function inputTarget() {
    if (joy) {
        const dx = joy.x - joy.ox, dy = joy.y - joy.oy;
        const d = Math.hypot(dx, dy);
        const m = Math.min(d / 54, 1);
        const dead = 0.08;
        if (m < dead) return { x: 0, y: 0 };
        const s = (m - dead) / (1 - dead);
        const ux = d ? dx / d : 0, uy = d ? dy / d : 0;
        return { x: ux * s, y: uy * s };
    }
    let kx = (keys.right ? 1 : 0) - (keys.left ? 1 : 0);
    let ky = (keys.down ? 1 : 0) - (keys.up ? 1 : 0);
    const m = Math.hypot(kx, ky);
    if (m < 0.001) return { x: 0, y: 0 };
    if (m > 1) { kx /= m; ky /= m; }
    return { x: kx, y: ky };
}

function pushInput() {
    if (!connected || !selfId) return;
    const t = inputTarget();
    const k = 1 - Math.exp(-11 * dtReal); // exponential smoothing (~0.15s to 90%)
    pad.x += (t.x - pad.x) * k;
    pad.y += (t.y - pad.y) * k;
    const sig = pad.x.toFixed(2) + ',' + pad.y.toFixed(2);
    if (sig === lastSent) return;
    // coalesce to ~20/s so an agitated joystick stays well under the server's
    // message-rate cap; the very first press after an idle gap sends instantly
    const now = performance.now();
    if (now - lastPendingAt >= 45) {
        lastPendingAt = now;
        lastSent = sig;
        const pos = sig.indexOf(',');
        wsSend({ t: 'input', d: [+sig.slice(0, pos), +sig.slice(pos + 1)] });
    }
}

// joystick is drawn in SCREEN space: reset the world transform first, so the
// thumb follows the finger exactly regardless of camera zoom/pan
function drawJoy() {
    if (!joy) return;
    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const dx = joy.x - joy.ox, dy = joy.y - joy.oy;
    const d = Math.hypot(dx, dy);
    const r = clamp(d, 0, 54);
    ctx.strokeStyle = 'rgba(0,229,255,0.28)';
    ctx.lineWidth = 2.5;
    ctx.beginPath(); ctx.arc(joy.ox, joy.oy, 54, 0, 6.2832); ctx.stroke();
    ctx.beginPath(); ctx.arc(joy.ox + clamp(dx, -54, 54), joy.oy + clamp(dy, -54, 54), 20, 0, 6.2832); ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.restore();
}

// ------------------------------------------------------------- websocket
function wsSend(o) {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify(o));
}

function connect(nick) {
    // retire the previous socket so its close event cannot trigger a reconnect
    if (ws) {
        ws._retired = true;
        try { ws.close(); } catch (e) { /* already closed */ }
    }
    intentionalClose = false;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    let sock;
    try {
        const url = `${proto}://${location.host}/ws?name=${encodeURIComponent(nick)}&skin=${encodeURIComponent(chosenSkin)}`;
        sock = new WebSocket(url);
    } catch (err) {
        ws = null;
        failJoin('НЕВОЗМОЖНО ПОДКЛЮЧИТЬСЯ: ' + (err && err.message ? err.message : 'неверный адрес сервера'));
        return;
    }
    ws = sock;

    // while the menu is still up, a stuck join attempt must never spin forever:
    // after 6s we force-close and show the reason under the button
    const handshakeTimer = setTimeout(() => {
        if (!$('menu').classList.contains('visible') || ws !== sock) return;
        sock._retired = true;
        try { sock.close(); } catch (e) { /* already closed */ }
        failJoin('СТАРТ ЗАВИС. СЕРВЕР НЕ ОТВЕТИЛ ЗА 6 СЕК. ' +
            (sock._errorTxt || ('status: ' + sock.readyState + ', он-лайн: ' + navigator.onLine)));
    }, 6000);

    const done = () => clearTimeout(handshakeTimer);

    sock.onopen = () => { connected = true; joinBtn.textContent = 'ВХОД В АРЕНУ...'; };

    sock.onmessage = (ev) => {
        let m;
        try { m = JSON.parse(ev.data); } catch (e) { return; }
        if (m.t === 'hello') {
            try {
                onHello(m);
                done();
            } catch (err) {
                failJoin('СЕРВЕР ОТВЕТИЛ, НО СТАРТ СЛОМАЛСЯ: ' + (err && err.message ? err.message : err));
                sock._retired = true;
                try { sock.close(); } catch (e) { /* already closed */ }
            }
            return;
        }
        try {
            switch (m.t) {
                case 'st': onState(m); break;
                case 'evt': onEvents(m.ev); break;
                case 'pong': onPong(m); break;
                case 'full':
                    toast('СЕРВЕР ПОЛОН. ПОПРОБУЙТЕ ПОЗЖЕ');
                    sock._retired = true;
                    intentionalClose = true;
                    setJoining(false);
                    sock.close();
                    break;
                default: break;
            }
        } catch (err) {
            if ($('menu').classList.contains('visible')) {
                failJoin('ОШИБКА ДАННЫХ АРЕНЫ: ' + (err && err.message ? err.message : err));
                sock._retired = true;
                try { sock.close(); } catch (e) { /* already closed */ }
            }
        }
    };

    sock.onclose = (ev) => {
        if (ws === sock) connected = false;
        done();
        if (sock._retired || intentionalClose) return;
        const why = ev && (ev.code + (ev.reason ? ' ' + ev.reason : ''));
        setTimeout(() => {
            if (ws !== sock) return;
            if ($('menu').classList.contains('visible')) {
                failJoin('НЕ УДАЛОСЬ ВОЙТИ (код ' + why + '). СЕРВЕР ЗАПУЩЕН? ПОПРОБУЙТЕ ЕЩЁ РАЗ.');
                return;
            }
            toast('СВЯЗЬ ПОТЕРЯНА. ПЕРЕПОДКЛЮЧЕНИЕ...');
            connect(nick);
        }, 1600);
    };
    sock.onerror = () => {
        sock._errorTxt = 'ошибка сети' + (navigator.onLine === false ? ' — вы офлайн' : '');
        try { sock.close(); } catch (e) { /* already closed */ }
    };
}

function failJoin(msg) {
    joining = false;
    setJoining(false);
    clearJoinError();
    const el = $('join-error');
    if (el) el.textContent = msg;
    toast(msg);
}

function clearJoinError() {
    const el = $('join-error');
    if (el) el.textContent = '';
}

// graceful close on navigation so the console stays clean
addEventListener('beforeunload', () => { if (ws) { ws._retired = true; try { ws.close(); } catch (e) { /* noop */ } } });
addEventListener('pagehide', () => { if (ws) { ws._retired = true; try { ws.close(); } catch (e) { /* noop */ } } });

function onHello(m) {
    selfId = m.id;
    worldMax = m.world;
    stopMenuAnim();
    bodies = m.bodies;
    offsetMs = m.a ? m.a * 1000 - performance.now() : 0;
    entities.clear();
    cores.clear();
    wrecks.clear();
    bonuses.clear();
    trails.clear();
    respawnAt = 0;
    for (const p of m.players || []) spawnEnt(p, (m.a || 0) * 1000);
    for (let i = 0; i < (m.cores || []).length; i++) {
        const c = m.cores[i];
        cores.set(i, { x: c[0], y: c[1], v: c[2] });
    }
    for (let i = 0; i < (m.bonuses || []).length; i++) {
        const b = m.bonuses[i];
        bonuses.set(i, { x: b[0], y: b[1], k: b[2] });
    }
    my = entities.get(selfId);
    if (my) { cam.x = my.x; cam.y = my.y; }
    joining = false;
    setJoining(false);
    clearJoinError();
    if ($('menu').classList.contains('visible')) {
        startGame();
        toast('В ЭФИРЕ. СОБИРАЙ ЯДРА.');
    }
}

function spawnEnt(p, serverMs) {
    const e = {
        id: p[0], x: p[1], y: p[2], vx: p[3], vy: p[4],
        score: p[5], name: p[6], color: p[7], alive: p[8], kills: p[9],
        skin: p[10] || 'probe', sh: p[11] || 0, bo: p[12] || 0, mg: p[13] || 0,
        cb: p[14] || 0,
        smoothed: { x: p[1], y: p[2] }
    };
    e.s0 = { x: p[1], y: p[2], vx: p[3], vy: p[4], t: serverMs };
    e.s1 = { x: p[1], y: p[2], vx: p[3], vy: p[4], t: serverMs };
    entities.set(e.id, e);
    return e;
}

function onState(m) {
    offsetMs = m.a * 1000 - performance.now();
    const serverMs = m.a * 1000;
    for (const p of m.p) {
        let e = entities.get(p[0]);
        if (!e) {
            e = spawnEnt(p, serverMs);
        } else {
            e.s0 = e.s1 || e.s0;
        }
        e.x = p[1]; e.y = p[2]; e.vx = p[3]; e.vy = p[4];
        e.score = p[5]; e.name = p[6]; e.color = p[7]; e.alive = p[8]; e.kills = p[9];
        e.skin = p[10] || e.skin; e.sh = p[11] || 0; e.bo = p[12] || 0; e.mg = p[13] || 0;
        e.cb = p[14] || 0;
        e.s1 = { x: p[1], y: p[2], vx: p[3], vy: p[4], t: serverMs };
    }
    for (const id of entities.keys()) {
        if (id !== selfId && !m.p.some(pp => pp[0] === id)) entities.delete(id);
    }
    cores.clear();
    for (let i = 0; i < m.c.length; i++) cores.set(i, { x: m.c[i][0], y: m.c[i][1], v: m.c[i][2] });
    wrecks.clear();
    for (let i = 0; i < m.w.length; i++) {
        const w = m.w[i];
        wrecks.set(i, { x: w[0], y: w[1], v: w[2], left: w[3], sx: undefined, sy: undefined });
    }
    bonuses.clear();
    for (let i = 0; i < (m.bo || []).length; i++) {
        const b = m.bo[i];
        bonuses.set(i, { x: b[0], y: b[1], k: b[2] });
    }
    my = entities.get(selfId);
}

function onEvents(evs) {
    for (const e of evs) {
        switch (e.t) {
            case 'spawn': {
                const ent = entities.get(e.id);
                if (ent) {
                    ent.x = e.x; ent.y = e.y; ent.alive = true;
                    ent.vx = e.vx || 0; ent.vy = e.vy || 0;
                    ent.smoothed.x = e.x; ent.smoothed.y = e.y;
                    ent.s0 = ent.s1 = { x: e.x, y: e.y, vx: ent.vx, vy: ent.vy, t: nowMs() };
                    if (trails.has(e.id)) trails.delete(e.id);
                }
                if (e.id === selfId) {
                    respawnAt = 0;
                    lastStats = null;
                    lastTop = null;
                    const res = $('results');
                    if (res) res.classList.add('hidden');
                    toast('В ЭФИРЕ. СОБИРАЙ ЯДРА.');
                }
                break;
            }
            case 'die':
                explodeAt(e.x, e.y, e.score || 0);
                if (e.id === selfId) {
                    shake = 1;
                    flashA = 0.55;
                    respawnAt = performance.now() + RESPAWN_DELAY * 1000;
                    toast(`КОНТЕЙНЕР ${e.score}, ПОТЕРЯН. РЕСПАВН...`);
                    audio.die();
                    if (e.stats) lastStats = e.stats;
                    if (e.top) lastTop = e.top;
                    renderResults();
                } else {
                    const d = Math.hypot(e.x - cam.x, e.y - cam.y);
                    if (d < 900) shake = Math.max(shake, 0.6 * (1 - d / 900));
                }
                if (trails.has(e.id)) trails.delete(e.id);
                break;
            case 'kill':
                killfeed(`${nameOf(e.killer)}  ☠  ${nameOf(e.victim)}`);
                if (e.killer === selfId) { audio.kill(); toast('УДАР ЗАСЧИТАН!'); }
                if (e.victim === selfId) audio.kill();
                break;
            case 'pickup': {
                const ent = entities.get(e.id);
                if (ent && e.id === selfId) {
                    audio.pickup();
                    sparkle(ent.x, ent.y, 5);
                    popupAt(ent.x, ent.y, '+' + (e.value || 1), '#ffcc33');
                }
                break;
            }
            case 'bump':
                flash(e.x, e.y, 'rgba(255,255,255,0.45)');
                if (e.a === selfId || e.b === selfId) audio.pickup();
                break;
            case 'bonus': {
                const ent = entities.get(e.id);
                if (ent && e.id === selfId) {
                    const meta = BONUS_META[BONUS_IDX[e.kind]] || BONUS_META[0];
                    audio.pickup();
                    toast(meta.label + ' АКТИВЕН');
                    spawnParts(ent.x, ent.y, 10, [meta.c, '#ffffff'], 200, 0.5, 5);
                    flash(ent.x, ent.y, meta.c);
                }
                break;
            }
            case 'shield':
                if (e.id === selfId) {
                    audio.sling();
                    toast('ЩИТ ОТРАЗИЛ УДАР');
                }
                flash(e.x, e.y, '#00e5ff');
                break;
            case 'wreck': {
                const ent = entities.get(e.id);
                if (e.id === selfId) {
                    audio.pickup();
                    if (e.value) toast(`+${e.value} груза с контейнера`);
                    if (ent) popupAt(ent.x, ent.y, '+' + e.value, '#ff3ea5');
                    if (e.owner_name && e.owner !== selfId) {
                        killfeed(`ты подобрал контейнер ${esc(e.owner_name)} (+${e.value})`);
                    }
                } else if (e.owner === selfId) {
                    killfeed(`${esc(nameOf(e.id))} утащил твой контейнер (+${e.value})`);
                }
                break;
            }
            case 'sling':
                if (e.id === selfId) {
                    audio.sling();
                    showSlingFx();
                    flash(e.x, e.y, '#ffcc33');
                    if (e.combo > 1) showComboFx(e.combo);
                } else {
                    flash(e.x, e.y, 'rgba(255,204,51,0.5)');
                }
                break;
            default: break;
        }
    }
}

// ------------------------------------------------------------- audio
let actx = null;
const audio = {
    init() {
        if (actx) { if (actx.state === 'suspended') actx.resume(); return; }
        const AC = window.AudioContext || window.webkitAudioContext;
        if (AC) actx = new AC();
    },
    blip(freq, dur, type, gain, slide) {
        if (!soundOn) return;
        if (!actx) return;
        const t = actx.currentTime;
        const o = actx.createOscillator();
        const g = actx.createGain();
        o.type = type; o.frequency.setValueAtTime(freq, t);
        if (slide) o.frequency.exponentialRampToValueAtTime(slide, t + dur);
        g.gain.setValueAtTime(gain || 0.08, t);
        g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
        o.connect(g).connect(actx.destination);
        o.start(t); o.stop(t + dur + 0.02);
    },
    pickup() { this.blip(660, 0.12, 'sine', 0.08, 990); },
    sling() { this.blip(220, 0.4, 'sawtooth', 0.1, 1400); },
    kill() { this.blip(120, 0.5, 'sawtooth', 0.14, 40); },
    die() {
        if (!soundOn) return;
        if (!actx) return;
        const t = actx.currentTime;
        const buf = actx.createBuffer(1, actx.sampleRate * 0.5, actx.sampleRate);
        const d = buf.getChannelData(0);
        for (let i = 0; i < d.length; i++) d[i] = (Math.random() * 2 - 1) * (1 - i / d.length);
        const src = actx.createBufferSource();
        src.buffer = buf;
        const g = actx.createGain();
        g.gain.setValueAtTime(0.2, t);
        g.gain.exponentialRampToValueAtTime(0.001, t + 0.5);
        const flt = actx.createBiquadFilter();
        flt.type = 'lowpass'; flt.frequency.value = 900;
        src.connect(flt); flt.connect(g); g.connect(actx.destination);
        src.start(t);
    }
};

// ------------------------------------------------------------- HUD
function onPong(m) {
    pingMs = Math.round((performance.now() - pingSentAt) / 2);
    $('ping').textContent = pingMs;
}

function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
        ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function updateBoard() {
    const list = $('board-list');
    $('online').textContent = [...entities.values()].filter(e => e.alive).length;
    const arr = [...entities.values()]
        .filter(e => e.alive)
        .sort((a, b) => b.score - a.score)
        .slice(0, 8);
    let html = '';
    for (let i = 0; i < arr.length; i++) {
        const e = arr[i];
        const me = e.id === selfId;
        const cls = me ? 'bp me' : (i === 0 ? 'bp top1' : 'bp');
        html += `<div class="board-player ${cls}"><b>${e.score}</b> <span class="bp-name">${esc(e.name)}</span></div>`;
    }
    list.innerHTML = html;
    if (my) $('score').textContent = my.score;
}

function nameOf(id) {
    const e = entities.get(id);
    return e ? e.name : '??';
}

function killfeed(text) {
    const box = $('events');
    const d = document.createElement('div');
    d.className = 'ev kill';
    d.textContent = text;
    box.prepend(d);
    while (box.children.length > 3) box.lastChild.remove();
    setTimeout(() => { if (d.parentNode) d.remove(); }, 4200);
}

function toast(text) {
    const t = $('toast');
    t.textContent = text;
    t.classList.remove('hidden');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.add('hidden'), 2600);
}

function showSlingFx() {
    const fx = $('sling-fx');
    fx.classList.remove('hidden');
    fx.style.animation = 'none';
    void fx.offsetWidth;
    fx.style.animation = '';
    clearTimeout(slingFxTimer);
    slingFxTimer = setTimeout(() => fx.classList.add('hidden'), 900);
}

function showComboFx(combo) {
    const fx = $('combo-fx');
    fx.textContent = 'ЦЕПЬ ПРАЩ ×' + combo;
    fx.classList.remove('hidden');
    fx.style.animation = 'none';
    void fx.offsetWidth;
    fx.style.animation = '';
    clearTimeout(comboFxTimer);
    comboFxTimer = setTimeout(() => fx.classList.add('hidden'), SLING_COMBO_MS);
}

function flash(x, y, color) {
    spawnParts(x, y, 14, [color], 120, 0.5, 5);
}

function sparkle(x, y, n) {
    spawnParts(x, y, n, ['#ffcc33', '#ffe08a', '#fff'], 160, 0.4, 4);
}

function explodeAt(x, y, val) {
    const colors = ['#ff3ea5', '#ff4757', '#ffcc33', '#7d5cff'];
    spawnParts(x, y, 46, colors, 340, 1.1, 6);
    spawnParts(x, y, 16, ['#c9e7ff'], 200, 0.8, 3);
    bigBoom = { x, y, r: 0, t: 0 };
}

// round-results panel shown while the respawn countdown runs
function renderResults() {
    const el = $('results');
    if (!el || !lastStats) return;
    $('res-cores').textContent = lastStats.cores || 0;
    $('res-wreck').textContent = lastStats.wreck || 0;
    $('res-combo').textContent = lastStats.bestCombo || 0;
    $('res-dist').textContent = Math.round(lastStats.dist || 0) + ' м';
    $('res-top').textContent = lastTop
        ? `${esc(lastTop.name)} — ${lastTop.score}` : '—';
    el.classList.remove('hidden');
}

// floating "+N" world-space popups on pickups
function popupAt(x, y, text, color) {
    popups.push({ x, y, text, color, life: 1.1 });
    if (popups.length > 24) popups.shift();
}

function drawPopups(dt) {
    ctx.font = 'bold 13px "Share Tech Mono"';
    ctx.textAlign = 'center';
    for (let i = popups.length - 1; i >= 0; i--) {
        const p = popups[i];
        p.life -= dt;
        if (p.life <= 0) { popups.splice(i, 1); continue; }
        p.y -= 46 * dt;
        ctx.globalAlpha = Math.min(1, p.life * 2);
        ctx.fillStyle = p.color;
        ctx.fillText(p.text, p.x, p.y);
    }
    ctx.globalAlpha = 1;
}

// ------------------------------------------------------------- render
function drawBodies() {
    for (const b of bodies) {
        const sp = makeSprite(b[5], b[4], b[2]);
        ctx.drawImage(sp.cv, b[0] - sp.s / 2, b[1] - sp.s / 2);
        ctx.font = '11px "Share Tech Mono"';
        ctx.fillStyle = 'rgba(210,230,255,0.5)';
        ctx.textAlign = 'center';
        ctx.fillText(b[3], b[0], b[1] + b[2] + 16);
    }
}

function drawCores() {
    ctx.fillStyle = '#ffcc33';
    for (const c of cores.values()) {
        if (c.v > 1) {
            ctx.globalAlpha = 0.9;
            ctx.fillStyle = '#ffe08a';
        }
        ctx.beginPath();
        ctx.arc(c.x, c.y, c.v > 1 ? 7 : 4.6, 0, 6.2832);
        ctx.fill();
        ctx.globalAlpha = 1;
        ctx.fillStyle = '#ffcc33';
    }
}

function drawBonuses() {
    const pulse = 0.55 + 0.45 * Math.sin(performance.now() / 260);
    for (const b of bonuses.values()) {
        const meta = BONUS_META[b.k] || BONUS_META[0];
        ctx.globalAlpha = 0.22;
        ctx.fillStyle = meta.c;
        ctx.beginPath(); ctx.arc(b.x, b.y, 16 * pulse + 6, 0, 6.2832); ctx.fill();
        ctx.globalAlpha = 1;
        ctx.strokeStyle = meta.c;
        ctx.lineWidth = 1.6;
        ctx.beginPath(); ctx.arc(b.x, b.y, 11, 0, 6.2832); ctx.stroke();
        ctx.fillStyle = meta.c;
        ctx.beginPath();
        const rot = performance.now() / 700;
        for (let i = 0; i < 4; i++) {
            const a = rot + i * (Math.PI / 2);
            const px = b.x + Math.cos(a) * 5, py = b.y + Math.sin(a) * 5;
            if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
        }
        ctx.closePath();
        ctx.fill();
    }
}

function drawWrecks() {
    for (const w of wrecks.values()) {
        // smooth the 16Hz state updates so fast wrecks glide instead of jumping
        if (w.sx === undefined) { w.sx = w.x; w.sy = w.y; }
        const k = 1 - Math.exp(-8 * dtReal);
        w.sx += (w.x - w.sx) * k;
        w.sy += (w.y - w.sy) * k;
        const r = clamp(6 + w.v * 0.5, 8, 40);
        // decay halo: ring shrinks as the container is about to vanish
        const lifeF = Math.max(0, Math.min(1, (w.left || WRECK_DECAY) / WRECK_DECAY));
        ctx.strokeStyle = `rgba(255,62,165,${0.5 * lifeF})`;
        ctx.lineWidth = 1.5;
        ctx.beginPath(); ctx.arc(w.sx, w.sy, r * 1.45 + (1 - lifeF) * 6, 0, 6.2832); ctx.stroke();
        ctx.fillStyle = 'rgba(255,62,165,0.16)';
        ctx.beginPath(); ctx.arc(w.sx, w.sy, r * 1.9, 0, 6.2832); ctx.fill();
        ctx.fillStyle = 'rgba(255,62,165,0.9)';
        ctx.beginPath(); ctx.arc(w.sx, w.sy, r, 0, 6.2832); ctx.fill();
        ctx.fillStyle = '#ffd2e8';
        ctx.font = '10px "Share Tech Mono"';
        ctx.textAlign = 'center';
        ctx.fillText(w.v, w.sx, w.sy + 3);
    }
}

function drawHullPath(ang, skin) {
    ctx.beginPath();
    if (skin === 'vortex') {
        ctx.moveTo(Math.cos(ang) * 1.5, Math.sin(ang) * 1.5);
        ctx.lineTo(Math.cos(ang + 2.4) * 1.0, Math.sin(ang + 2.4) * 1.0);
        ctx.lineTo(Math.cos(ang - 2.4) * 1.0, Math.sin(ang - 2.4) * 1.0);
    } else if (skin === 'hex') {
        for (let i = 0; i < 6; i++) {
            const a = ang + i * (Math.PI / 3);
            const px = Math.cos(a) * 1.08, py = Math.sin(a) * 1.08;
            if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
        }
    } else if (skin === 'blade') {
        ctx.moveTo(Math.cos(ang) * 1.5, Math.sin(ang) * 1.5);
        ctx.lineTo(Math.cos(ang + 2.4) * 0.85, Math.sin(ang + 2.4) * 0.85);
        ctx.lineTo(Math.cos(ang + Math.PI) * 0.4, Math.sin(ang + Math.PI) * 0.4);
        ctx.lineTo(Math.cos(ang - 2.4) * 0.85, Math.sin(ang - 2.4) * 0.85);
    } else {
        ctx.arc(0, 0, 1, 0, 6.2832);
    }
    ctx.closePath();
}

function drawPlayers(now) {
    if (!my) return;
    // magnet field: only for the local ship, purely informational
    if (my.mg > 0) {
        const pulse = 0.5 + 0.5 * Math.sin(performance.now() / 500);
        ctx.strokeStyle = `rgba(199,125,255,${0.16 + 0.1 * pulse})`;
        ctx.lineWidth = 1.6;
        ctx.beginPath(); ctx.arc(my.smoothed.x, my.smoothed.y, MAGNET_R, 0, 6.2832); ctx.stroke();
    }
    for (const e of entities.values()) {
        const isMe = e.id === selfId;
        // render position: own ship is predicted ahead of the packet stream,
        // everyone else is interpolated between the two last server states
        let tx, ty;
        if (isMe && e.s1) {
            const age = clamp((now - e.s1.t) / 1000, 0, 0.12);
            tx = e.x + e.vx * age;
            ty = e.y + e.vy * age;
        } else if (e.s0 && e.s1 && e.s1.t > e.s0.t) {
            const span = Math.max(e.s1.t - e.s0.t, 40);
            const f = clamp((now - e.s1.t) / span, 0, 1);
            tx = e.s0.x + (e.s1.x - e.s0.x) * f;
            ty = e.s0.y + (e.s1.y - e.s0.y) * f;
        } else {
            tx = e.x; ty = e.y;
        }
        const k = 1 - Math.exp(-18 * dtReal);
        e.smoothed.x += (tx - e.smoothed.x) * k;
        e.smoothed.y += (ty - e.smoothed.y) * k;
        const x = e.smoothed.x, y = e.smoothed.y;

        if (!e.alive) continue;

        if (Math.hypot(e.vx, e.vy) > 80) {
            pushTrail(e.id, x, y);
        }
        const tr = trails.get(e.id);
        if (tr && tr.length > 2) {
            const n = tr.length;
            const head = n - 1;
            // chained slings shift the trail toward gold and thicken it
            const combo = e.cb || 0;
            const trHue = combo > 1 ? 42 : e.color;
            const trBoost = 1 + combo * 0.14;
            const passes = [[0.10, 0.45], [0.14, 0.75], [0.09, 1.00]];
            ctx.lineCap = 'round';
            for (const [a, frac] of passes) {
                const i0 = Math.max(0, Math.floor(n * (1 - frac)));
                ctx.strokeStyle = `hsla(${trHue},90%,${64 + 24 * frac}%,${a * trBoost})`;
                ctx.lineWidth = (isMe ? 3.8 : 3.2) * (0.55 + 0.45 * frac) * trBoost;
                ctx.beginPath();
                ctx.moveTo(tr[i0].x, tr[i0].y);
                for (let i = i0 + 1; i <= head; i++) ctx.lineTo(tr[i].x, tr[i].y);
                ctx.stroke();
            }
            ctx.lineCap = 'butt';
        }

        const r = PLAYER_R * (1 + Math.min(e.score, 20) * 0.06);
        ctx.fillStyle = `hsla(${e.color},95%,65%,0.12)`;
        ctx.beginPath(); ctx.arc(x, y, r * 2.1, 0, 6.2832); ctx.fill();
        ctx.fillStyle = hue(e.color, 85, 62);
        const ang = Math.atan2(e.vy, e.vx);
        ctx.save();
        ctx.translate(x, y);
        ctx.scale(r, r);
        drawHullPath(ang, e.skin);
        ctx.fill();
        ctx.restore();
        if (isMe && Math.hypot(pad.x, pad.y) > 0.2) {
            ctx.fillStyle = 'rgba(255,204,51,0.85)';
            ctx.beginPath();
            ctx.arc(x - e.vx * 0.012, y - e.vy * 0.012, r * 0.42, 0, 6.2832);
            ctx.fill();
        }
        if (e.sh > 0) {
            ctx.strokeStyle = `rgba(0,229,255,${0.35 + 0.25 * Math.sin(performance.now() / 120)})`;
            ctx.lineWidth = 1.6;
            ctx.beginPath(); ctx.arc(x, y, r * 1.75, 0, 6.2832); ctx.stroke();
        }
        ctx.globalAlpha = 1;
        ctx.font = isMe ? 'bold 11px "Share Tech Mono"' : '10px "Share Tech Mono"';
        ctx.textAlign = 'center';
        ctx.fillStyle = isMe ? '#eaffff' : 'rgba(210,230,255,0.85)';
        ctx.fillText(e.name, x, y - r - 8);
        ctx.fillStyle = '#ffcc33';
        ctx.fillText(e.score, x, y - r + 16);
        if (e.bo > 0) buffPip(x, y - r + 24, 0, '#ffcc33');
        if (e.sh > 0) buffPip(x, y - r + 24, 1, '#00e5ff');
        if (e.mg > 0) buffPip(x, y - r + 24, 2, '#c77dff');
    }
}

function buffPip(cx, cy, idx, color) {
    const off = (idx - 1) * 7;
    ctx.fillStyle = color;
    ctx.beginPath(); ctx.arc(cx + off, cy, 2.4, 0, 6.2832); ctx.fill();
}

function updateBuffHUD() {
    const sh = $('buff-shield'), bo = $('buff-boost'), mg = $('buff-magnet');
    sh.textContent = my && my.sh > 0 ? my.sh : '';
    bo.textContent = my && my.bo > 0 ? my.bo : '';
    mg.textContent = my && my.mg > 0 ? my.mg : '';
    sh.classList.toggle('on', !!(my && my.sh > 0));
    bo.classList.toggle('on', !!(my && my.bo > 0));
    mg.classList.toggle('on', !!(my && my.mg > 0));
}

let bgPattern = null;
function drawBackground() {
    // subtle parallax starfield baked once
    if (!bgPattern) {
        bgPattern = document.createElement('canvas');
        bgPattern.width = 256; bgPattern.height = 256;
        const c = bgPattern.getContext('2d');
        for (let i = 0; i < 120; i++) {
            const x = Math.random() * 256, y = Math.random() * 256;
            c.globalAlpha = Math.random() * 0.5 + 0.1;
            c.fillStyle = '#bfe9ff';
            c.fillRect(x, y, Math.random() < 0.85 ? 1 : 2, 1);
        }
        bgPattern.globalAlpha = 1;
    }
    // the base fill MUST run under the identity transform: it is the only
    // full-canvas erase each frame. In the leftover world transform it only
    // clears the region under the previous camera, leaving ghosting trails
    // (and earlier menu decorations) on the rest of the screen.
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = '#04060f';
    ctx.fillRect(0, 0, W, H);
    ctx.globalAlpha = 0.7;
    ctx.fillStyle = ctx.createPattern(bgPattern, 'repeat');
    ctx.fillRect(0, 0, W, H);
    ctx.globalAlpha = 1;
    ctx.restore();
}

function drawMinimap() {
    mmCtx.clearRect(0, 0, mm.width, mm.height);
    const s = mm.width / worldMax.w;
    mmCtx.strokeStyle = 'rgba(0,229,255,0.25)';
    mmCtx.setLineDash([3, 3]);
    mmCtx.strokeRect(1, 1, mm.width - 2, mm.height - 2);
    mmCtx.setLineDash([]);
    for (const b of bodies) {
        mmCtx.fillStyle = b[5] === 'star' ? '#ffcc33' : '#ff3ea5';
        mmCtx.beginPath();
        mmCtx.arc(b[0] * s, b[1] * s, Math.max(1.5, b[2] * s * 0.22), 0, 6.2832);
        mmCtx.fill();
    }
    for (const e of entities.values()) {
        if (!e.alive) continue;
        const me = e.id === selfId;
        mmCtx.fillStyle = me ? '#00e5ff' : 'rgba(201,231,255,0.65)';
        mmCtx.beginPath(); mmCtx.arc(e.smoothed.x * s, e.smoothed.y * s, me ? 3 : 1.6, 0, 6.2832); mmCtx.fill();
    }
    for (const b of bonuses.values()) {
        const meta = BONUS_META[b.k] || BONUS_META[0];
        mmCtx.fillStyle = meta.c;
        mmCtx.beginPath(); mmCtx.arc(b.x * s, b.y * s, 1.8, 0, 6.2832); mmCtx.fill();
    }
    for (const w of wrecks.values()) {
        mmCtx.fillStyle = 'rgba(255,62,165,0.9)';
        mmCtx.beginPath(); mmCtx.arc(w.x * s, w.y * s, 1.5, 0, 6.2832); mmCtx.fill();
    }
    const vw = W / dpr / cam.scale;
    const vh = H / dpr / cam.scale;
    mmCtx.strokeStyle = 'rgba(0,229,255,0.5)';
    mmCtx.strokeRect((cam.x - vw / 2) * s, (cam.y - vh / 2) * s, vw * s, vh * s);
}

function updateRespawnHUD() {
    const el = $('respawn');
    if (!respawnAt) { el.classList.add('hidden'); return; }
    const left = (respawnAt - performance.now()) / 1000;
    if (left <= 0) return; // the server decides when to spawn; just wait here
    el.classList.remove('hidden');
    const num = $('respawn-num');
    if (num) num.textContent = Math.max(1, Math.ceil(left));
}

// ------------------------------------------------------------- loop
function frame(t) {
    rafId = requestAnimationFrame(frame);
    if (!visible || !connected || !selfId) { lastT = t; return; }
    dtReal = clamp((t - lastT) / 1000, 0, 0.1);
    lastT = t;

    fpsAcc += dtReal; fpsN++;
    if (fpsAcc >= 0.5) {
        fpsShown = Math.round(fpsN / fpsAcc);
        fpsAcc = fpsN = 0;
        $('fps').textContent = fpsShown;
    }

    keyInput();
    pushInput();

    const now = t + offsetMs; // extrapolation clock (ms)

    if (my && my.alive) {
        zoomTarget = clamp(zoomTarget, 0.28, 1.7);
        cam.scale += (zoomTarget - cam.scale) * (1 - Math.pow(1 - 0.4, dtReal * 60));
        cam.x += (my.smoothed.x - cam.x) * Math.min(1, dtReal * 8);
        cam.y += (my.smoothed.y - cam.y) * Math.min(1, dtReal * 8);
    }

    stepParticles(dtReal);
    stepTrails(dtReal);
    if (bigBoom) {
        bigBoom.t += dtReal;
        bigBoom.r += 40 * dtReal;
        if (bigBoom.t > 0.9) bigBoom = null;
    }
    updateBoard();
    updateBuffHUD();
    updateRespawnHUD();

    const vscale = cam.scale;
    const vw = W / dpr / vscale;
    const vh = H / dpr / vscale;

    // screen shake: random world-space offset that fades each frame
    const sh = shake;
    shake = Math.max(0, shake - dtReal * 2.4);
    const shX = (Math.random() - 0.5) * 70 * sh;
    const shY = (Math.random() - 0.5) * 70 * sh;

    drawBackground();
    ctx.setTransform(dpr * vscale, 0, 0, dpr * vscale,
        dpr * (W / dpr / 2 - (cam.x + shX) * vscale),
        dpr * (H / dpr / 2 - (cam.y + shY) * vscale));

    const x0 = cam.x + shX - vw / 2, y0 = cam.y + shY - vh / 2;
    ctx.font = '10px "Share Tech Mono"';
    ctx.strokeStyle = 'rgba(0,229,255,0.18)';
    ctx.lineWidth = 3;
    ctx.strokeRect(0, 0, worldMax.w, worldMax.h);

    const bVisible = (b) => b[0] + b[2] * 2.6 > x0 && b[0] - b[2] * 2.6 < x0 + vw &&
        b[1] + b[2] * 2.6 > y0 && b[1] - b[2] * 2.6 < y0 + vh;
    if (bodies.some(bVisible)) drawBodies();

    if (cores.size) drawCores();
    if (bonuses.size) drawBonuses();
    if (wrecks.size) drawWrecks();
    drawPlayers(now);
    drawParticles();
    drawPopups(dtReal);

    if (bigBoom) {
        ctx.strokeStyle = 'rgba(255,62,165,0.7)';
        ctx.lineWidth = 3;
        ctx.beginPath(); ctx.arc(bigBoom.x, bigBoom.y, bigBoom.r, 0, 6.2832); ctx.stroke();
    }

    drawJoy();
    drawMinimap();

    // fullscreen flash (own death) — drawn last, in screen space
    if (flashA > 0) {
        ctx.save();
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.fillStyle = `rgba(255,120,190,${Math.min(0.65, flashA)})`;
        ctx.fillRect(0, 0, W, H);
        ctx.restore();
        flashA = Math.max(0, flashA - dtReal * 1.9);
    }

    if (my) {
        const sp = Math.hypot(my.vx, my.vy);
        $('speed-fill').style.width = clamp(sp / MAX_SPEED * 100, 0, 100) + '%';
        $('speed-val').textContent = Math.round(sp);
    }
}

// keep the socket alive even when the tab is hidden: rAF throttles there,
// and the server frees idle slots after its recv timeout
setInterval(() => {
    if (connected && selfId && performance.now() - lastPingSent > 3000) {
        lastPingSent = performance.now();
        pingSentAt = performance.now();
        wsSend({ t: 'ping' });
    }
}, 1250);

document.addEventListener('visibilitychange', () => {
    visible = !document.hidden;
    if (visible) {
        lastT = performance.now();
    } else if (connected && selfId && ws && ws.readyState === 1) {
        keys.up = keys.down = keys.left = keys.right = false;
        pad.x = pad.y = 0;
        lastSent = '';
        lastPendingAt = 0;
        wsSend({ t: 'input', d: [0, 0] });
    }
});

// ------------------------------------------------------------- menu flow
const nickInput = $('nick');
const joinBtn = $('join-btn');
const saved = (() => { try { return localStorage.getItem('slingor_nick'); } catch (e) { return ''; } })();
nickInput.value = saved || ('PROBE-' + ((Math.random() * 9000 + 1000) | 0));

// ---- skin picker + sound toggle (persisted) ----
const skinPicker = $('skin-picker');
function renderSkinPicker() {
    skinPicker.innerHTML = '';
    for (const s of SKIN_DEFS) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'skin-btn' + (s.id === chosenSkin ? ' selected' : '');
        b.dataset.skin = s.id;
        b.title = s.label;
        b.setAttribute('aria-label', 'скин ' + s.label);
        const dot = document.createElement('span');
        dot.className = 'skin-dot';
        dot.style.background = `hsl(${s.hue}, 85%, 60%)`;
        b.appendChild(dot);
        const nm = document.createElement('span');
        nm.className = 'skin-name';
        nm.textContent = s.label;
        b.appendChild(nm);
        b.addEventListener('click', () => {
            chosenSkin = s.id;
            try { localStorage.setItem('slingor_skin', s.id); } catch (err) { /* storage full */ }
            renderSkinPicker();
        });
        skinPicker.appendChild(b);
    }
}
renderSkinPicker();

const soundToggle = $('sound-toggle');
function renderSoundToggle() {
    soundToggle.textContent = soundOn ? 'ЗВУК: ВКЛ' : 'ЗВУК: ВЫКЛ';
    soundToggle.classList.toggle('off', !soundOn);
}
soundToggle.addEventListener('click', () => {
    soundOn = !soundOn;
    try { localStorage.setItem('slingor_sound', soundOn ? '1' : '0'); } catch (err) { /* storage full */ }
    renderSoundToggle();
    if (soundOn) {
        try { audio.init(); } catch (err) { /* audio is optional */ }
        audio.pickup();
    }
});
renderSoundToggle();

function stopMenuAnim() {
    menuDone = true;
    cancelAnimationFrame(menuRaf);
}

function startGame() {
    joining = false;
    clearTimeout(window.__joinDeadline);
    stopMenuAnim();
    $('menu').classList.remove('visible');
    $('hud').classList.remove('hidden');
    if (!rafId) rafId = requestAnimationFrame(frame);
}

function armDeadline() {
    clearTimeout(window.__joinDeadline);
    window.__joinDeadline = setTimeout(() => {
        if (!$('menu').classList.contains('visible')) return;
        failJoin('СОЕДИНЕНИЕ ЗАВИСЛО. САМОЕ ВЕРОЯТНОЕ — ВАШ VPN/ПРОКСИ/РАСШИРЕНИЯ РЕЖУТ WS. ПОПРОБУЙТЕ ОКНО INCOGNITO, ОТКЛЮЧИТЕ VPN И НАЖМИТЕ ЕЩЁ РАЗ.');
    }, 8000);
}

function setJoining(on) {
    joinBtn.disabled = on;
    joinBtn.textContent = on ? 'ПОДКЛЮЧЕНИЕ...' : 'ВОЙТИ В ОРБИТУ';
}

$('join-form').addEventListener('submit', (e) => {
    e.preventDefault();
    if (joining) return;         // already trying to join
    joining = true;
    try { audio.init(); } catch (err) { /* audio is optional */ }
    const nick = nickInput.value.trim() || 'PROBE';
    try { localStorage.setItem('slingor_nick', nick); } catch (err) { /* storage full */ }
    clearJoinError();
    setJoining(true);
    toast('ПОДКЛЮЧЕНИЕ К АРЕНЕ...');
    armDeadline();
    connect(nick);
});

addEventListener('wheel', (e) => {
    if ($('menu').classList.contains('visible')) return;
    zoomTarget *= e.deltaY > 0 ? 0.92 : 1.08;
}, { passive: true });

// leaderboard in menu (decorative; failure is not fatal)
fetch('/api/leaderboard')
    .then(r => r.json())
    .then(d => {
        const el = $('menu-lb');
        if (!d.top || !d.top.length) { el.textContent = '— арена ждёт первых рекордов —'; return; }
        el.innerHTML = d.top.map((r, i) =>
            `<div class="lb-row ${i === 0 ? 'lb-1' : ''}">${i + 1}. <b>${esc(r.name)}</b> — ${r.score}</div>`
        ).join('');
    })
    .catch((err) => console.warn('leaderboard fetch failed:', err && err.message || err));

// version label (single source: the server's __version__)
fetch('/api/version')
    .then(r => r.json())
    .then(d => {
        if (d && d.version) {
            const v = 'v' + d.version;
            $('menu-ver').textContent = v;
            $('hud-ver').textContent = v;
        }
    })
    .catch(() => { $('menu-ver').textContent = ''; });

// start background anim (menu visible, world floating behind)
resize();
let menuRaf = 0;
let menuDone = false;
let decoBodies = null, decoCores = null;
(function menuFrame() {
    if (menuDone || (connected && selfId)) return;
    menuRaf = requestAnimationFrame(menuFrame);
    lastT = performance.now();
    cam.scale += (0.3 - cam.scale) * 0.02;
    cam.x += (WORLD / 2 - cam.x) * 0.005;
    cam.y += (WORLD / 2 - cam.y) * 0.005;
    drawBackground();
    const vscale = cam.scale;
    ctx.setTransform(dpr * vscale, 0, 0, dpr * vscale,
        dpr * (W / dpr / 2 - cam.x * vscale),
        dpr * (H / dpr / 2 - cam.y * vscale));
    if (!decoBodies) {
        // mirrors game/physics.py::make_system() so the menu preview matches the real arena
        decoBodies = [
            [WORLD / 2, WORLD / 2, 290, 'PR-7', 40, 'star'],
            [WORLD / 2 + 1250, WORLD / 2, 120, 'A-1', 180, 'planet'],
            [WORLD / 2 - 1250, WORLD / 2 + 320, 105, 'B-2', 300, 'planet'],
            [WORLD / 2 - 850, WORLD / 2 - 900, 95, 'C-3', 25, 'planet'],
            [WORLD / 2 + 1150, WORLD / 2 - 600, 85, 'D-4', 340, 'planet'],
            [WORLD / 2 - 1500, WORLD / 2 + 1200, 75, 'E-5', 150, 'planet'],
            [WORLD / 2 + 500, WORLD / 2 + 1250, 70, 'F-6', 260, 'planet'],
        ];
        decoCores = [];
        for (let i = 0; i < 60; i++) {
            const a = Math.random() * Math.PI * 2;
            const rr = 700 + Math.random() * 700;
            decoCores.push({ x: WORLD / 2 + Math.cos(a) * rr, y: WORLD / 2 + Math.sin(a) * rr });
        }
    }
    bodies = decoBodies;
    drawBodies();
    ctx.fillStyle = 'rgba(255,204,51,0.7)';
    for (const c of decoCores) {
        ctx.globalAlpha = 0.6;
        ctx.beginPath(); ctx.arc(c.x, c.y, 4, 0, 6.2832); ctx.fill();
    }
    ctx.globalAlpha = 1;
})();

})();