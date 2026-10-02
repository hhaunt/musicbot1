// Компаньон MusicCloud для Discord: показывает в профиле Discord, какой трек сейчас играет
// в мини-приложении и сколько прошло. Работает на компьютере, где запущен Discord:
// раз в 5 секунд спрашивает у сервера бота «что играет» и передаёт это Discord по локальному IPC.
// Сторонних библиотек не нужно — только Node.js 18+.
'use strict';
const net = require('net');
const fs = require('fs');
const path = require('path');
const readline = require('readline');

const CONFIG = path.join(__dirname, 'config.json');
const POLL_MS = 5000;

function ask(question) {
  const rl = readline.createInterface({input: process.stdin, output: process.stdout});
  return new Promise(resolve => rl.question(question, answer => { rl.close(); resolve(answer.trim()); }));
}

/* config.json: код из мини-приложения и Application ID приложения в Discord */
async function loadConfig() {
  let cfg = {};
  try { cfg = JSON.parse(fs.readFileSync(CONFIG, 'utf8')); } catch (e) {}
  if (!cfg.code) cfg.code = await ask('Код из мини-приложения (Профиль → Discord): ');
  if (!cfg.clientId) cfg.clientId = await ask('Application ID приложения Discord: ');
  let code = null;
  try { code = JSON.parse(Buffer.from(cfg.code, 'base64').toString('utf8')); } catch (e) {}
  if (!code || !code.url || !code.u || !code.k) {
    console.log('Код не подходит. Скопируйте его заново в мини-приложении и запустите программу ещё раз.');
    process.exit(1);
  }
  if (!/^\d{15,22}$/.test(cfg.clientId)) {
    console.log('Application ID — это число из 17–20 цифр со страницы приложения на discord.com/developers.');
    process.exit(1);
  }
  fs.writeFileSync(CONFIG, JSON.stringify(cfg, null, 2));
  return {url: code.url.replace(/\/$/, ''), u: code.u, k: code.k, clientId: cfg.clientId};
}

/* Минимальный клиент локального IPC Discord: пакет = код операции (int32) + длина (int32) + JSON */
class DiscordIPC {
  constructor(clientId) { this.clientId = clientId; this.sock = null; this.ready = false; this.buf = Buffer.alloc(0); }

  pipePath(i) {
    if (process.platform === 'win32') return `\\\\?\\pipe\\discord-ipc-${i}`;
    const dir = process.env.XDG_RUNTIME_DIR || process.env.TMPDIR || process.env.TMP || process.env.TEMP || '/tmp';
    return path.join(dir, `discord-ipc-${i}`);
  }

  async connect() {
    for (let i = 0; i < 10; i++) {
      try {
        const sock = await new Promise((resolve, reject) => {
          const c = net.createConnection(this.pipePath(i), () => resolve(c));
          c.once('error', reject);
        });
        this.attach(sock);
        return;
      } catch (e) {}
    }
    throw new Error('Discord не запущен');
  }

  attach(sock) {
    this.sock = sock; this.ready = false; this.buf = Buffer.alloc(0);
    sock.on('data', d => this.onData(d));
    sock.on('close', () => { this.sock = null; this.ready = false; });
    sock.on('error', () => {});
    this.send(0, {v: 1, client_id: this.clientId});  // рукопожатие
  }

  send(op, payload) {
    if (!this.sock) return;
    const data = Buffer.from(JSON.stringify(payload));
    const head = Buffer.alloc(8);
    head.writeInt32LE(op, 0); head.writeInt32LE(data.length, 4);
    this.sock.write(Buffer.concat([head, data]));
  }

  onData(chunk) {
    this.buf = Buffer.concat([this.buf, chunk]);
    while (this.buf.length >= 8) {
      const op = this.buf.readInt32LE(0), len = this.buf.readInt32LE(4);
      if (this.buf.length < 8 + len) break;
      let msg = {};
      try { msg = JSON.parse(this.buf.subarray(8, 8 + len).toString('utf8')); } catch (e) {}
      this.buf = this.buf.subarray(8 + len);
      if (op === 3) this.send(4, msg);  // ping → pong
      else if (op === 2) { console.log('Discord закрыл соединение:', msg.message || ''); this.sock && this.sock.destroy(); }
      else if (msg.evt === 'READY') {
        this.ready = true;
        console.log('Подключено к Discord' + (msg.data && msg.data.user ? ' как ' + msg.data.user.username : ''));
      } else if (msg.evt === 'ERROR') console.log('Discord ответил ошибкой:', msg.data && msg.data.message);
    }
  }

  setActivity(activity) {
    if (!this.ready) return;
    this.send(1, {cmd: 'SET_ACTIVITY', args: {pid: process.pid, activity}, nonce: `${Date.now()}-${Math.random()}`});
  }
}

/* Discord требует от 2 до 128 символов в текстовых полях */
const text = s => { s = String(s || '').trim(); return (s.length < 2 ? s + '  ' : s).slice(0, 128); };

function buildActivity(st, startMs) {
  const activity = {
    type: 2,  // «Слушает»
    details: text(st.title),
    state: text(st.artist || st.brand),
    timestamps: {start: Math.round(startMs)},
    assets: {large_image: st.cover || 'logo', large_text: text(st.brand || 'MusicCloud')},
    instance: false,
  };
  if (st.duration) activity.timestamps.end = Math.round(startMs + st.duration * 1000);  // полоса прогресса
  if (st.link) activity.buttons = [{label: `Слушать в ${st.brand || 'MusicCloud'}`.slice(0, 32), url: st.link}];
  return activity;
}

async function main() {
  const cfg = await loadConfig();
  const ipc = new DiscordIPC(cfg.clientId);
  let last = '', waiting = false;

  async function tick() {
    if (!ipc.sock) {
      try { await ipc.connect(); waiting = false; last = ''; }
      catch (e) { if (!waiting) { console.log('Жду, пока запустится Discord…'); waiting = true; } return; }
    }
    let st;
    try {
      const r = await fetch(`${cfg.url}/api/presence?u=${cfg.u}&k=${cfg.k}`);
      if (r.status === 401) {
        console.log('Код не принят сервером. Удалите config.json и запустите программу заново.');
        process.exit(1);
      }
      st = await r.json();
    } catch (e) { return; }  // сервер недоступен — попробуем в следующий раз
    if (!ipc.ready) return;
    if (!st.playing) {
      if (last !== 'none') { ipc.setActivity(null); last = 'none'; console.log('Ничего не играет — статус убран'); }
      return;
    }
    const startMs = Date.now() - st.position * 1000;
    // обновляем статус только при смене трека или перемотке, а не каждые 5 секунд
    const key = `${st.title}|${st.artist}|${Math.round(startMs / 4000)}`;
    if (key === last) return;
    last = key;
    ipc.setActivity(buildActivity(st, startMs));
    console.log('Сейчас играет:', st.artist ? `${st.artist} — ${st.title}` : st.title);
  }

  console.log('Компаньон MusicCloud для Discord запущен. Закройте окно, чтобы остановить.');
  await tick();
  setInterval(tick, POLL_MS);
}

main();
