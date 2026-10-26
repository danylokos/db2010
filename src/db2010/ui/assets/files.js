'use strict';
(() => {
  const $ = id => document.getElementById(id);
  let epoch = null, state = {}, path = [], entries = [], localBusy = false;
  let jobId = null, jobDone = false, xhr = null, polling = false, navigation = 0, loaded = false;
  let uploadTarget = null;
  const bytes = n => n === null || n === undefined ? '—' : n < 1024 ? `${n} B` : n < 1048576 ? `${(n/1024).toFixed(1)} KiB` : `${(n/1048576).toFixed(1)} MiB`;
  const terminal = phase => ['done','error','cancelled'].includes(phase);
  function error(message = '') {
    $('files-error').textContent = message;
    $('files-error').hidden = !message;
  }
  async function api(url, options = {}) {
    const response = await fetch(url, {cache:'no-store', ...options});
    const data = await response.json();
    if (!response.ok) {
      const problem = new Error(data.error || 'File operation failed');
      problem.remote = data.remote_status;
      throw problem;
    }
    return data;
  }
  const post = (url, data) => api(url, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  function controls() {
    const busy = localBusy || state.busy;
    $('files-connect').hidden = !!state.connected;
    $('files-connect').disabled = !state.available || busy;
    $('files-disconnect').hidden = !state.connected;
    $('files-disconnect').disabled = busy;
    $('files-up').disabled = !state.connected || busy || !path.length;
    $('files-refresh').disabled = !state.connected || busy;
    $('files-upload').disabled = !state.connected || busy || !path.length;
    for (const button of $('files-entries').querySelectorAll('button')) button.disabled = !state.connected || busy;
    $('files-state').textContent = state.connected ? 'Connected to phone' : !state.enabled ? 'File transfer unavailable in this session' : !state.running ? 'Phone session stopped' : !state.available ? 'Waiting for phone…' : 'Ready to connect';
  }
  function render() {
    const crumbs = $('files-path'); crumbs.replaceChildren();
    for (let index = 0; index <= path.length; index++) {
      if (index) crumbs.append(document.createTextNode(' / '));
      const button = document.createElement('button'); button.type = 'button';
      button.textContent = index ? path[index-1] : 'Phone';
      button.onclick = () => browse(path.slice(0,index)); crumbs.append(button);
    }
    const body = $('files-entries'); body.replaceChildren();
    if (!entries.length) {
      const row = body.insertRow(), cell = row.insertCell(); cell.colSpan = 3;
      cell.textContent = state.connected ? 'This folder is empty.' : 'Connect to browse the phone.';
    }
    for (const entry of [...entries].sort((a,b) => Number(b.directory)-Number(a.directory) || a.name.localeCompare(b.name))) {
      const row = body.insertRow();
      const name = row.insertCell(), size = row.insertCell(), action = row.insertCell();
      size.textContent = entry.directory ? 'Folder' : bytes(entry.size);
      if (entry.modified) name.title = `Modified: ${entry.modified}`;
      if (entry.directory) {
        const button = document.createElement('button'); button.type = 'button';
        button.className = 'folder-name'; button.textContent = `▸ ${entry.name}`;
        button.onclick = () => browse([...path,entry.name]); name.append(button);
      } else {
        name.textContent = entry.name;
        const button = document.createElement('button'); button.type = 'button'; button.textContent = 'Download';
        button.setAttribute('aria-label', `Download ${entry.name}`);
        button.onclick = () => download(entry); action.append(button);
      }
    }
    controls();
  }
  async function browse(target) {
    if (localBusy) return;
    const mine = ++navigation, session = epoch;
    localBusy = true; controls(); error();
    try {
      const result = await api(`/api/files/list?epoch=${encodeURIComponent(session)}&path=${encodeURIComponent(JSON.stringify(target))}`);
      if (epoch !== session || mine !== navigation) return;
      path = result.path; entries = result.entries; loaded = true; render();
    } catch (problem) { if (epoch === session) error(problem.message); }
    finally { if (mine === navigation) { localBusy = false; controls(); } }
  }
  function renderJob(job) {
    $('file-transfer').hidden = false;
    $('file-transfer-name').textContent = job.path[job.path.length-1];
    const labels = {receiving:'Waiting for upload', 'upload-body':'Receiving upload',queued:'Waiting',uploading:'Uploading to phone',verifying:'Verifying file',downloading:'Downloading from phone',done:'Complete',cancelled:'Cancelled',error:'Failed'};
    $('file-transfer-state').textContent = `${job.cancel_requested && !terminal(job.phase) ? 'Cancelling' : labels[job.phase] || job.phase} · ${bytes(job.bytes)}${job.total !== null ? ' / '+bytes(job.total) : ''}${job.error ? ' · '+job.error : ''}`;
    const progress = $('file-progress');
    if (job.phase === 'done') progress.value = 1;
    else if (job.total > 0) progress.value = Math.min(1,job.bytes/job.total);
    else progress.removeAttribute('value');
    $('file-cancel').hidden = terminal(job.phase);
    $('file-cancel').disabled = !!job.cancel_requested;
    $('file-phone-approval').hidden = job.kind !== 'upload' || job.phase !== 'uploading' || job.bytes > 0;
    $('file-phone-approval').textContent = job.cancel_requested ? 'Decline the pending transfer on the phone to finish cancellation.' : 'Confirm the transfer on the phone if prompted.';
    $('file-hash').textContent = job.phase === 'done' && job.sha256 ? `SHA-256 ${job.sha256}` : '';
    if (job.phase === 'done' && job.kind === 'download') {
      const link = $('file-save'); link.href = `/api/files/jobs/${job.id}/content?epoch=${encodeURIComponent(job.epoch)}`;
      link.download = job.path[job.path.length-1]; link.hidden = false;
      if (!jobDone) link.click();
    }
    if (terminal(job.phase) && !jobDone) {
      jobDone = true;
      if (job.phase === 'done' && job.kind === 'upload') browse(path);
    }
  }
  function begin(job) {
    jobId = job.id; jobDone = false;
    $('file-save').hidden = true; $('file-save').removeAttribute('href');
    renderJob(job);
  }
  $('files-connect').onclick = async () => {
    localBusy = true; controls(); error();
    try { state = await post('/api/files/connect',{epoch}); }
    catch (problem) { error(problem.message+(problem.remote === 0xc3 ? '. Finish any setup shown on the phone and try again.' : '')); }
    finally { localBusy = false; controls(); }
    if (state.connected) browse([]);
  };
  $('files-disconnect').onclick = async () => {
    localBusy = true; controls(); error();
    try { await post('/api/files/disconnect',{epoch}); state.connected = false; path=[];entries=[];render(); }
    catch (problem) { error(problem.message); }
    finally { localBusy = false; controls(); }
  };
  $('files-up').onclick = () => browse(path.slice(0,-1));
  $('files-refresh').onclick = () => browse(path);
  $('files-upload').onclick = () => { uploadTarget = {epoch,path:[...path]}; $('files-input').click(); };
  $('files-input').onchange = async () => {
    const file = $('files-input').files[0]; $('files-input').value = '';
    if (!file) return;
    const target = uploadTarget; uploadTarget = null;
    if (!target || target.epoch !== epoch || JSON.stringify(target.path) !== JSON.stringify(path)) {
      error('Phone folder changed. Choose the file again.'); return;
    }
    if (file.size > 32*1024*1024) { error('File exceeds32 MiB transfer limit'); return; }
    const replace = entries.some(entry => entry.name === file.name);
    if (replace && !window.confirm(`Replace “${file.name}” on the phone?`)) return;
    const session = epoch; localBusy = true; controls(); error();
    try {
      const job = await post('/api/files/jobs',{epoch:session,kind:'upload',path:[...target.path,file.name],size:file.size,replace});
      if (epoch !== session) return;
      begin(job);
      await new Promise((resolve,reject) => {
        const transfer = new XMLHttpRequest(); xhr = transfer;
        transfer.open('PUT',`/api/files/jobs/${job.id}/body?epoch=${encodeURIComponent(session)}`);
        transfer.setRequestHeader('Content-Type','application/octet-stream'); transfer.timeout = 310000;
        transfer.onload = () => transfer.status >= 200 && transfer.status < 300 ? resolve() : reject(new Error(JSON.parse(transfer.responseText).error || 'Upload failed'));
        transfer.onerror = () => reject(new Error('Upload connection lost'));
        transfer.onabort = () => reject(new Error('Upload cancelled'));
        transfer.ontimeout = () => reject(new Error('Upload deadline exceeded'));
        transfer.send(file);
      });
    } catch (problem) { if (epoch === session) error(problem.message); }
    finally { xhr = null; localBusy = false; controls(); }
  };
  async function download(entry) {
    localBusy = true; controls(); error();
    try { begin(await post('/api/files/jobs',{epoch,kind:'download',path:[...path,entry.name]})); }
    catch (problem) { error(problem.message); }
    finally { localBusy = false; controls(); }
  }
  $('file-cancel').onclick = async () => {
    if (!jobId) return;
    try { await post(`/api/files/jobs/${jobId}/cancel`,{epoch}); xhr?.abort(); }
    catch (problem) { error(problem.message); }
  };
  async function poll() {
    if (polling) return;
    polling = true;
    try {
      const update = await api('/api/files/state');
      if (epoch !== update.epoch) {
        xhr?.abort(); epoch = update.epoch; jobId = null; jobDone = false;
        path=[];entries=[];navigation++;localBusy=false;loaded=false;
        $('file-transfer').hidden=true; $('file-save').hidden=true; $('file-save').removeAttribute('href');
        render();
      }
      state = update; controls();
      if (state.connected && !state.busy && !localBusy && !loaded) browse([]);
      const job = update.jobs.find(item => item.id === jobId);
      if (job) renderJob(job);
    } catch { $('files-state').textContent = 'File connection unavailable'; }
    finally { polling = false; }
  }
  poll(); window.setInterval(poll,500);
})();
