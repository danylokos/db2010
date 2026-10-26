import {PCMQueue} from './audio-buffer.js';
class DB2010PCM extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = new PCMQueue(sequence => this.transport?.postMessage({type: 'retired', sequence, epoch: this.queue.epoch}));
    this.since = 0;
    this.statsPending = false;
    this.ready = false;
    const command = event => {
      try {
        if (event.data.type === 'reset') this.queue.reset();
        else if (event.data.type === 'pcm') this.queue.push(event.data);
        else throw new Error('Invalid audio transport command');
      } catch (error) {
        this.queue.reset();
        this.transport.postMessage({type:'error', message:error.message});
      }
    };
    this.port.onmessage = event => {
      try {
        if (event.data.type === 'stats-ack') this.statsPending = false;
        else if (event.data.type === 'transport') {
          this.transport = event.data.port;
          this.transport.onmessage = command;
          if (this.ready) this.transport.postMessage({type:'ready'});
        }
        else throw new Error('Invalid audio worklet command');
      } catch (error) {
        this.queue.reset();
        this.transport?.postMessage({type: 'error', message: error.message});
      }
    };
  }
  process(inputs, outputs) {
    const channels = outputs[0];
    if (channels?.length >= 2) {
      if (!this.ready) {
        this.ready = true;
        this.transport?.postMessage({type: 'ready'});
      }
      this.queue.render(channels[0], channels[1]);
      this.since += channels[0].length;
      if (this.since >= sampleRate && !this.statsPending) {
        this.since %= sampleRate;
        this.statsPending = true;
        this.port.postMessage({type: 'stats', ...this.queue.stats()});
      }
    }
    return true;
  }
}
registerProcessor('db2010-pcm', DB2010PCM);
