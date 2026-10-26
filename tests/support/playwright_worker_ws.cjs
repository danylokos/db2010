// Playwright1.61 Firefox receives worker WebSocket-open events without the
// page's HTTP request metadata. Its inspector asserts and kills the driver.
// Test-only: ignore missing inspection metadata; never alter browser traffic,
// AudioWorklet execution, product JavaScript or emulator state.
const Module = require('node:module');
const fs = require('node:fs');
const original = Module._extensions['.js'];
Module._extensions['.js'] = function(module, filename) {
  if (filename.endsWith('/package/lib/coreBundle.js')) {
    let source = fs.readFileSync(filename, 'utf8');
    const before = `_onWebSocketOpened(event) {
        const request2 = this._webSocketRequests.get(event.requestId);
        assert(request2);
        const response2 = this._webSocketResponses.get(event.requestId);
        assert(response2);`;
    const after = `_onWebSocketOpened(event) {
        const request2 = this._webSocketRequests.get(event.requestId);
        const response2 = this._webSocketResponses.get(event.requestId);
        if (!request2 || !response2) return;`;
    if (!source.includes(before) || source.indexOf(before) !== source.lastIndexOf(before))
      throw new Error('Review Firefox worker-WebSocket inspector workaround for this Playwright version');
    module._compile(source.replace(before, after), filename);
  } else original(module, filename);
};
