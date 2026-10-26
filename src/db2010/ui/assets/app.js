'use strict';
const el = id => document.getElementById(id);
const narrowWorkspace = matchMedia('(max-width: 800px)');
const workspace = document.querySelector('.workspace');
const workspaceTabs = [el('tab-phone'), el('tab-files')];
function selectWorkspace(name, focus = false) {
  releaseLocal();
  workspace.dataset.mobileView = name;
  for (const tab of workspaceTabs) {
    const selected = tab.id === 'tab-'+name;
    tab.setAttribute('aria-selected', String(selected));
    tab.tabIndex = selected ? 0 : -1;
    if (selected && focus) tab.focus();
  }
}
for (const tab of workspaceTabs) {
  tab.addEventListener('click', () => selectWorkspace(tab.id.slice(4)));
  tab.addEventListener('keydown', event => {
    const target = event.key === 'ArrowLeft' || event.key === 'ArrowRight'
      ? (tab.id === 'tab-phone' ? 'files' : 'phone')
      : {Home:'phone', End:'files'}[event.key];
    if (target) { event.preventDefault(); selectWorkspace(target, true); }
  });
}
function workspaceLayout() {
  for (const name of ['phone', 'files']) {
    const panel = el(name+'-panel');
    if (narrowWorkspace.matches) {
      panel.setAttribute('role', 'tabpanel');
      panel.setAttribute('aria-labelledby', 'tab-'+name);
    } else {
      panel.removeAttribute('role');
      if (name === 'files') panel.setAttribute('aria-labelledby', 'files-title');
      else panel.removeAttribute('aria-labelledby');
    }
  }
}
narrowWorkspace.addEventListener('change', workspaceLayout);
workspaceLayout();
const compactCount = new Intl.NumberFormat('en', {notation:'compact', minimumFractionDigits:1, maximumFractionDigits:1});
const tails = Array.from({length:4}, () => ({data:'', total:0, truncated:false}));
let pendingDisplay = null, drawingScheduled = false;
let handset3D = null, focusedContact = null;
function uartText(encoded) {
  const raw = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
  return new TextDecoder('utf-8').decode(raw).replace(/\r\n?/g, '\n')
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, c => '\\x'+c.charCodeAt(0).toString(16).padStart(2,'0'));
}
function uartHexdump(tail) {
  const raw = Uint8Array.from(atob(tail.data), c => c.charCodeAt(0));
  const start = tail.total-raw.length, lines = [];
  for (let i = 0; i < raw.length; i += 16) {
    const row = raw.subarray(i, i+16);
    const hex = Array.from(row, byte => byte.toString(16).padStart(2, '0'));
    const left = hex.slice(0, 8).join(' ').padEnd(23);
    const right = hex.slice(8).join(' ').padEnd(23);
    const ascii = Array.from(row, byte => byte >= 0x20 && byte <= 0x7e ? String.fromCharCode(byte) : '.').join('');
    lines.push((start+i).toString(16).padStart(8, '0')+'  '+left+'  '+right+'  |'+ascii+'|');
  }
  return lines.join('\n');
}
function renderUART() {
  const port = Number(el('port').value), tail = tails[port], box = el('uart');
  const atEnd = box.scrollHeight-box.scrollTop-box.clientHeight < 30;
  box.classList.toggle('hexdump', port === 2);
  box.textContent = port === 2 ? uartHexdump(tail) : uartText(tail.data);
  el('counts').textContent = tail.total.toLocaleString()+' '+(tail.total === 1 ? 'byte' : 'bytes');
  el('truncated').textContent = tail.truncated ? 'Earlier bytes omitted · latest 64 KiB' : '';
  if (atEnd) box.scrollTop = box.scrollHeight;
}
el('port').addEventListener('change', renderUART);
function receiveStatus(data) {
  setInputEnabled(data.input_enabled === true);
  const s = data.status;
  el('elapsed').textContent = s.elapsed.toFixed(1)+' s';
  el('observations').title = s.counter_kind === 'qemu-icount' ? 'QEMU native instruction count' : 'Research instruction observations';
  el('observations').textContent = s.observations === undefined ? '—' : compactCount.format(s.observations).replace(/([KMBT])$/, ' $1').replace('K', 'k');
  el('rate').textContent = (s.rate/1e6).toFixed(2)+' M/s';
  el('pc').textContent = s.pc === undefined ? '—' : s.pc.toString(16).toUpperCase().padStart(8,'0');
  el('outcome').textContent = s.error ? s.error.type+': '+s.error.message : (s.stop ? s.stop.reason : '');
}
function receiveUART(data) {
  for (const tail of data.uart) tails[tail.port] = tail;
  renderUART();
}
function decodeBinary(buffer) {
  if (buffer.byteLength < 4) throw new Error('Missing display header');
  const length = new DataView(buffer).getUint32(0, false);
  if (length > buffer.byteLength-4) throw new Error('Incomplete display header');
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 4, length)));
  if (typeof header.type !== 'string') throw new Error('Missing binary message type');
  return {header, pixels: new Uint8Array(buffer, 4+length)};
}
function decodeDisplay(message) {
  const panel = message.header.panel, pixels = message.pixels;
  const known = panel?.width === 176 && panel.height === 220 &&
    panel.pixel_format === 'rgb565-be' && panel.known_pixels === 176*220;
  if (pixels.length !== (known ? 176*220*2 : 0)) throw new Error('Invalid display pixels');
  return {panel, pixels, known};
}
function drawDisplay(frame) {
  const ctx = el('lcd').getContext('2d');
  if (frame.known) {
    const image = ctx.createImageData(176, 220), rgba = image.data, raw = frame.pixels;
    for (let offset = 0, out = 0; offset < raw.length; offset += 2, out += 4) {
      const word = (raw[offset] << 8) | raw[offset+1];
      rgba[out] = Math.floor(((word >> 11) & 31)*255/31);
      rgba[out+1] = Math.floor(((word >> 5) & 63)*255/63);
      rgba[out+2] = Math.floor((word & 31)*255/31);
      rgba[out+3] = 255;
    }
    ctx.putImageData(image, 0, 0);
  } else {
    ctx.clearRect(0, 0, 176, 220);
  }
  el('unknown').hidden = frame.known;
  handset3D?.display(!frame.known);
}
function receiveDisplay(frame) {
  pendingDisplay = frame;
  if (drawingScheduled) return;
  drawingScheduled = true;
  requestAnimationFrame(() => {
    drawingScheduled = false;
    const latest = pendingDisplay;
    pendingDisplay = null;
    drawDisplay(latest);
  });
}
// Subsystems dispatch independently. Continuous media would supply its own
// timed chunks and playback buffer rather than the LCD's latest-frame policy.
const receivers = {status: receiveStatus, uart: receiveUART};
const binaryReceivers = {display: message => receiveDisplay(decodeDisplay(message))};
function receive(data) {
  if (data instanceof ArrayBuffer) {
    const message = decodeBinary(data);
    binaryReceivers[message.header.type]?.(message);
  } else {
    const message = JSON.parse(data);
    receivers[message.type]?.(message);
  }
}
function connectionState(state, label) {
  const indicator = el('connection');
  indicator.className = 'connection-dot '+state;
  indicator.setAttribute('aria-label', label);
  indicator.title = label;
}
function connect() {
  connectionState('connecting', 'Connecting');
  let socket;
  try {
    socket = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://')+location.host+'/ws');
    socket.binaryType = 'arraybuffer';
  } catch (error) {
    // Firefox can throw synchronously for a blocked connection. In that case
    // no open/error/close event arrives and the handshake watchdog cannot run.
    inputSocket = null;
    setInputEnabled(false);
    connectionState('disconnected', 'Connection failed: '+(error.name || 'WebSocket')+'; reconnecting');
    setTimeout(connect,1000);
    return;
  }
  inputSocket = socket;
  // A forwarded TCP connection can accept the request without completing the
  // upgrade. Do not wait for the browser's much longer network timeout, or for
  // a close event from an abandoned attempt, before retrying.
  let deadline;
  const retry = label => {
    if (inputSocket !== socket) return;
    clearTimeout(deadline);
    releaseLocal();
    inputSocket = null;
    setInputEnabled(false);
    connectionState('disconnected', label);
    socket.onopen = socket.onmessage = socket.onclose = socket.onerror = null;
    socket.close();
    setTimeout(connect,1000);
  };
  const armDeadline = () => {
    clearTimeout(deadline);
    deadline = setTimeout(() => retry('Connection timed out; reconnecting'),5000);
  };
  armDeadline();
  socket.onopen = () => {
    connectionState('connected', 'Connected');
    // The server sends its cached state immediately after upgrading. Also
    // recover if the upgrade succeeds but the first update never arrives.
    armDeadline();
  };
  socket.onmessage = event => {
    receive(event.data);
    clearTimeout(deadline);
  };
  socket.onclose = () => retry('Disconnected; reconnecting');
  socket.onerror = () => retry('Connection error; reconnecting');
}
const handset = el('handset');
const keyButtons = new Map(Array.from(handset.querySelectorAll('[data-key]'), b => [b.dataset.key, b]));
// Physical keyboard/pointer sources share contacts. The selected action lives
// until keyup; modifier release order cannot turn a soft key into a direction.
const sources = new Map();
let inputSocket = null, inputEnabled = false;
function sendContact(button, pressed) {
  if (inputSocket?.readyState === WebSocket.OPEN && inputEnabled) {
    inputSocket.send(JSON.stringify({type:'keypad', button, pressed}));
  }
}
function contact(source, action, pressed) {
  if (pressed) {
    if (!inputEnabled || sources.has(source)) return;
    const held = Array.from(sources.values()).includes(action);
    sources.set(source, action);
    if (!held) sendContact(action, true);
  } else {
    action = sources.get(source);
    if (action === undefined) return;
    sources.delete(source);
    if (!Array.from(sources.values()).includes(action)) sendContact(action, false);
  }
  for (const [key, button] of keyButtons) {
    const down = Array.from(sources.values()).includes(key);
    button.classList.toggle('pressed', down);
    button.setAttribute('aria-pressed', String(down));
  }
  handset3D?.contacts(sources.values(), focusedContact);
}
function releaseLocal() {
  for (const source of Array.from(sources.keys())) contact(source, null, false);
  handset3D?.release();
}
function setInputEnabled(enabled) {
  if (enabled === inputEnabled) return;
  if (!enabled) releaseLocal();
  inputEnabled = enabled;
  for (const button of keyButtons.values()) button.disabled = !enabled;
  if (handset3D) handset3D.enabled = enabled;
}
function binding(event) {
  if (event.altKey || event.ctrlKey || event.metaKey) return null;
  if (event.shiftKey) {
    if (event.code === 'ArrowLeft') return 'soft-left';
    if (event.code === 'ArrowRight') return 'soft-right';
    if (event.code === 'Enter' || event.code === 'NumpadEnter') return 'walkman';
    if (event.code === 'KeyC') return 'camera-focus';
  }
  if (event.key === '#') return 'hash';
  if (event.key === '*' || event.code === 'NumpadMultiply') return 'star';
  if (/^Numpad[0-9]$/.test(event.code)) return event.code.slice(-1);
  if (/^[0-9]$/.test(event.key)) return event.key;
  return ({ArrowUp:'up', ArrowRight:'right', ArrowDown:'down', ArrowLeft:'left',
           Enter:'select', NumpadEnter:'select', Escape:'back', Backspace:'clear',
           PageUp:'volume-up', PageDown:'volume-down', Space:'play-pause',
           KeyC:'camera', KeyP:'power'})[event.code] ?? null;
}
handset.addEventListener('keydown', event => {
  const semantic = event.target.closest('[data-key]');
  const activationKey = ['Enter', 'NumpadEnter', 'Space'].includes(event.code);
  if (semantic && activationKey && (event.altKey || event.ctrlKey || event.metaKey)) {
    // Suppress the native button click too; it otherwise looks identical to
    // an assistive activation after the physical shortcut was ignored.
    event.preventDefault();
    return;
  }
  const activation = semantic && activationKey && (!event.shiftKey || event.code === 'Space');
  const source = 'key:'+event.code;
  const action = sources.get(source) ?? (activation ? semantic.dataset.key : binding(event));
  if (action === null) return;
  event.preventDefault();
  if (!event.repeat) contact(source, action, true);
});
window.addEventListener('keyup', event => {
  const source = 'key:'+event.code;
  if (sources.has(source)) { event.preventDefault(); contact(source, null, false); }
});
for (const [action, button] of keyButtons) {
  button.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    event.preventDefault();
    handset.focus({preventScroll:true});
    button.setPointerCapture(event.pointerId);
    contact('pointer:'+event.pointerId, action, true);
  });
  for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) {
    button.addEventListener(type, event => { event.preventDefault(); contact('pointer:'+event.pointerId, null, false); });
  }
  button.addEventListener('click', event => {
    event.preventDefault();
    // Assistive-technology activation need not emit physical key/pointer
    // events. Keep a brief physical hold, then release through the same path.
    if (event.detail === 0 && !event.altKey && !event.ctrlKey && !event.metaKey) {
      const source = 'activate:'+action;
      contact(source, action, true);
      setTimeout(() => contact(source, null, false), 100);
    }
  });
}
handset.addEventListener('focusin', event => {
  focusedContact = event.target.closest('[data-key]')?.dataset.key ?? null;
  if (focusedContact) handset3D?.focusContact(focusedContact);
  handset3D?.contacts(sources.values(), focusedContact);
});
handset.addEventListener('focusout', event => {
  focusedContact = null;
  handset3D?.contacts(sources.values());
  if (!handset.contains(event.relatedTarget)) releaseLocal();
});
window.addEventListener('blur', releaseLocal);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) releaseLocal();
  else handset3D?.invalidate();
});
window.addEventListener('pagehide', releaseLocal);

function fail3D(error) {
  releaseLocal();
  handset3D?.dispose();
  handset3D = null;
  el('three-stage').hidden = true;
  el('front-view').hidden = true;
  el('view-state').textContent = '3D handset unavailable. Reload to try again.';
  el('view-state').title = error.message;
}
async function loadHandset() {
  try {
    const module = await import('./handset3d.js');
    handset3D = await module.createHandset({
      host: el('three-stage'), lcd: el('lcd'), contact,
      focus: () => handset.focus({preventScroll: true}), failure: fail3D,
      tooltip: action => keyButtons.get(action)?.title ?? '',
      viewChanged: rotated => { el('front-view').hidden = !rotated; }
    });
    handset3D.enabled = inputEnabled;
    handset3D.contacts(sources.values(), focusedContact);
    handset3D.display(!el('unknown').hidden);
    handset3D.setActive(true);
    el('view-state').textContent = '';
  } catch (error) {
    fail3D(error);
  }
}
el('front-view').addEventListener('click', () => { releaseLocal(); handset3D?.front(); });
handset.focus({preventScroll:true});
connect();
loadHandset();
