// The page owns browser permission/lifecycle; PCM and ACKs bypass its thread.
let context, worklet, transport, pendingStats;
let enabled = true;
const stats = {received: 0, played: 0, underrun: 0, dropped: 0, buffered: 0};
window.db2010Audio = {stats, get context() { return context; }, get enabled() { return enabled; }};
function update() {
  transport?.postMessage({type: 'state', active: enabled && context?.state === 'running' && !document.hidden});
}
async function resume() {
  if (!enabled || !context || document.hidden) return;
  try {
    await context.resume();
    update();
  } catch (error) { stats.error = error.message; }
}
async function initialize() {
  try {
    context = new AudioContext({sampleRate: 48000, latencyHint: 'interactive'});
    if (context.sampleRate !== 48000) throw new Error('48 kHz audio unavailable');
    context.onstatechange = update;
    await context.audioWorklet.addModule('/audio-worklet.js');
    worklet = new AudioWorkletNode(context, 'db2010-pcm', {numberOfInputs: 0, numberOfOutputs: 1, outputChannelCount: [2]});
    transport = new Worker('/audio-transport.js', {type:'module'});
    const channel = new MessageChannel();
    worklet.port.postMessage({type:'transport', port:channel.port1}, [channel.port1]);
    transport.postMessage({type:'initialize', port:channel.port2,
      url:location.origin.replace(/^http/, 'ws') + '/audio'}, [channel.port2]);
    worklet.port.onmessage = event => {
      if (event.data.type === 'stats') {
        pendingStats = event.data;
        transport.postMessage({type:'stats'});
      }
    };
    transport.onmessage = event => {
      if (event.data.type === 'stats' && pendingStats) {
        Object.assign(stats, pendingStats, event.data);
        pendingStats = null;
        worklet.port.postMessage({type:'stats-ack'});
      }
    };
    transport.onerror = event => { stats.error = event.message; };
    worklet.connect(context.destination);
    void resume();
  } catch (error) {
    stats.error = error.message;
    transport?.terminate();
    if (context) await context.close();
    console.warn('Audio unavailable:', error.message);
  }
}
document.addEventListener('pointerdown', () => { void resume(); }, {capture:true, passive:true});
document.addEventListener('keydown', () => { void resume(); }, {capture:true});
document.addEventListener('visibilitychange', () => {
  if (document.hidden) update(); else void resume();
});
window.addEventListener('pagehide', event => {
  enabled = false;
  update();
  if (event.persisted) void context?.suspend();
  else {
    transport?.terminate();
    worklet?.disconnect();
    void context?.close();
  }
});
window.addEventListener('pageshow', event => {
  if (event.persisted) { enabled = true; void resume(); }
});
void initialize();
