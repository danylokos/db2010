// Audio transport runs independently of page rendering. Queues stay bounded.
let port, url, socket, reconnect;
let enabled = false, workletReady = false;
let currentEpoch = null, last = null, retired = null;
const inflight = new Map();
const stats = {received: 0};
function number(value) {
  if (typeof value !== 'string' || !/^(0|[1-9][0-9]{0,19})$/.test(value) || BigInt(value) > 18446744073709551615n)
    throw new Error('Invalid audio counter');
  return BigInt(value);
}
function reset() {
  inflight.clear(); last = null; retired = null; currentEpoch = null;
  port?.postMessage({type: 'reset'});
}
function connect() {
  if (!enabled || !workletReady || socket) return;
  reset();
  const ws = socket = new WebSocket(url);
  ws.binaryType = 'arraybuffer';
  ws.onmessage = event => {
    if (socket !== ws) return;
    try {
      if (typeof event.data === 'string') {
        const hello = JSON.parse(event.data);
        if (hello.type !== 'audio-ready' || hello.sample_rate !== 48000) throw new Error('Invalid audio stream');
        number(hello.epoch);
        if (currentEpoch !== null && hello.epoch !== currentEpoch) reset();
        currentEpoch = hello.epoch; return;
      }
      const raw = event.data;
      if (!(raw instanceof ArrayBuffer) || raw.byteLength < 4) throw new Error('Missing audio header');
      const size = new DataView(raw).getUint32(0, false);
      if (size < 1 || size > 4096 || size + 4 > raw.byteLength) throw new Error('Invalid audio header size');
      const h = JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(new Uint8Array(raw, 4, size)));
      if (h.type !== 'audio' || h.version !== 1 || h.sample_rate !== 48000 || h.channels !== 2 ||
          h.format !== 's16le' || h.epoch !== currentEpoch || typeof h.stream_id !== 'string' ||
          typeof h.eos !== 'boolean' || typeof h.discontinuity !== 'boolean' ||
          !Number.isInteger(h.frame_count) || h.frame_count < 0 || h.frame_count > 960 ||
          (!h.frame_count && !h.eos) || raw.byteLength !== size + 4 + h.frame_count * 4)
        throw new Error('Invalid PCM audio packet');
      const sequence = number(h.sequence), first = number(h.first_frame);
      number(h.virtual_start_ns);
      if (last && (sequence <= last.sequence || first < last.end || last.eos ||
          ((sequence !== last.sequence + 1n || first !== last.end) && !h.discontinuity)))
        throw new Error('Stale or discontinuous audio packet');
      if (inflight.size >= 10) throw new Error('Audio worklet stalled');
      const pcm = raw.slice(size + 4);
      inflight.set(h.sequence, h.frame_count);
      // A virtual source-clock gap can still carry consecutive samples. The
      // jitter buffer preserves those samples; actual loss/new epochs reset it.
      const resetSamples = h.discontinuity && (!last || first !== last.end || sequence !== last.sequence + 1n);
      port.postMessage({type: 'pcm', pcm, frames: h.frame_count, sequence: h.sequence,
        epoch: currentEpoch, eos: h.eos, discontinuity: resetSamples}, [pcm]);
      last = {sequence, end: first + BigInt(h.frame_count), eos: h.eos};
      stats.received += h.frame_count;
    } catch (error) {
      stats.error = error.message;
      ws.close(1008, 'Invalid or stalled audio stream');
    }
  };
  ws.onclose = () => {
    if (socket !== ws) return;
    reset(); socket = null;
    if (enabled) reconnect = setTimeout(connect, 150);
  };
}
function disconnect() {
  clearTimeout(reconnect);
  if (socket) { const old = socket; socket = null; old.close(); }
  reset();
}
function acknowledge() {
  if (socket?.bufferedAmount > 4096) { socket.close(); return; }
  if (socket?.readyState === WebSocket.OPEN && currentEpoch !== null)
    socket.send(JSON.stringify({type: 'audio-ack', epoch: currentEpoch, sequence: retired}));
}

self.onmessage = event => {
  const data = event.data;
  if (data.type === 'initialize') {
    port = data.port; url = data.url;
    port.onmessage = event => {
      const data = event.data;
      if (data.type === 'ready') { workletReady = true; connect(); }
      else if (data.type === 'retired' && data.epoch === currentEpoch && inflight.has(data.sequence)) {
        inflight.delete(data.sequence); retired = data.sequence;
      } else if (data.type === 'error') { stats.error = data.message; socket?.close(); }
    };
  } else if (data.type === 'state') {
    enabled = data.active;
    if (enabled) connect(); else disconnect();
  } else if (data.type === 'stats') {
    self.postMessage({type:'stats', ...stats});
  }
};
setInterval(acknowledge, 100);
