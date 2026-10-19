/* Offline, browser-native resumable multi-file uploader. No external dependencies. */
(() => {
  'use strict';
  const el = (id) => document.getElementById(id);
  const token = new URLSearchParams(location.search).get('token') || '';
  const jobs = [];
  let config = null;
  let parallel = 4;
  let active = 0;
  let cursor = 0;
  let lastBytes = 0;
  let lastTick = performance.now();
  let speed = 0;
  const phrases = {queued:'در صف', preparing:'در حال آماده‌سازی', uploading:'در حال ارسال', paused:'متوقف', failed:'خطا در اتصال', finalizing:'ثبت نهایی', done:'کامل شد', cancelled:'لغو شد'};
  const mb = (bytes) => `${(bytes / 1048576).toFixed(1)} مگابایت`;
  const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));
  function toast(message) {
    const box = el('toast'); box.textContent = message; box.classList.add('show');
    clearTimeout(toast.timer); toast.timer = setTimeout(() => box.classList.remove('show'), 7000);
  }
  async function api(path, options = {}) {
    const res = await fetch(path, {
      ...options,
      cache: 'no-store',
      headers: {'X-Session-Token':token, ...(options.body instanceof Blob ? {} : options.body ? {'Content-Type':'application/json'} : {}), ...(options.headers || {})}
    });
    const payload = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(payload.error || `خطای ارتباط ${res.status}`);
    return payload;
  }
  function fingerprint(file) {
    return [file.name, file.size, file.lastModified, file.webkitRelativePath || ''].join('::');
  }
  function countChunks(job) { return Math.max(1, Math.ceil(job.file.size / config.chunkSize)); }
  function bytesDone(job) {
    let total = 0;
    for (const n of job.received) total += Math.min(config.chunkSize, job.file.size - n * config.chunkSize);
    return total;
  }
  function addFiles(fileList) {
    if (!config) { toast('لطفاً اتصال به رایانه برقرار شود.'); return; }
    for (const file of Array.from(fileList)) {
      if (file.size > config.maxFileBytes) { toast(`حجم «${file.name}» بیش از محدودیت است.`); continue; }
      const key = fingerprint(file);
      const old = jobs.find(j => j.key === key && j.state !== 'cancelled');
      if (old) { if (old.state === 'failed' || old.state === 'paused') resume(old); continue; }
      const job = {file, key, id:null, received:new Set(), inflight:new Map(), retries:new Map(), next:0, retryQueue:[], state:'queued', error:'', finishing:false};
      jobs.push(job);
      prepare(job);
    }
    render();
  }
  async function prepare(job) {
    if (job.state === 'cancelled') return;
    job.state = 'preparing'; render();
    try {
      const info = await api('/api/files', {method:'POST', body:JSON.stringify({name:job.file.name,size:job.file.size,key:job.key})});
      job.id = info.id; job.received = new Set(info.received || []);
      if (job.state === 'cancelled') { await api(`/api/files/${job.id}`, {method:'DELETE'}); return; }
      if (info.status === 'complete') {job.state = 'done'; return;}
      if (job.state !== 'paused') job.state = 'uploading';
    } catch (e) { job.state = 'failed'; job.error = e.message; toast(e.message); }
    finally {render();}
  }
  function nextChunk(job) {
    while (job.retryQueue.length) {
      const index = job.retryQueue.shift();
      if (!job.received.has(index) && !job.inflight.has(index)) return index;
    }
    const count = countChunks(job);
    while (job.next < count) {
      const n = job.next++;
      if (!job.received.has(n) && !job.inflight.has(n)) return n;
    }
    return null;
  }
  async function sendChunk(job, index, controller) {
    const start = index * config.chunkSize;
    const blob = job.file.slice(start, Math.min(job.file.size, start + config.chunkSize));
    for (let attempt = 0; attempt < 4; attempt++) {
      if (job.state !== 'uploading') break;
      try {
        await api(`/api/files/${job.id}/chunks/${index}`, {method:'PUT', body:blob, signal:controller.signal});
        job.received.add(index);
        job.retries.delete(index);
        return;
      } catch (error) {
        if (controller.signal.aborted || job.state !== 'uploading') break;
        if (attempt === 3) {
          job.state = 'failed'; job.error = error.message;
          toast(`ارسال «${job.file.name}» متوقف شد: ${error.message}`);
        } else await sleep((attempt + 1) * 650);
      }
    }
    if (!job.received.has(index)) job.retryQueue.push(index);
  }
  async function finish(job) {
    if (job.finishing || job.state !== 'uploading') return;
    job.finishing = true; job.state = 'finalizing'; render();
    try {
      await api(`/api/files/${job.id}/complete`, {method:'POST'});
      job.state = 'done';
    } catch (error) { job.state = 'failed'; job.error = error.message; toast(error.message); }
    finally {job.finishing = false; render();}
  }
  function schedule() {
    if (!config || !jobs.length) return;
    for (let i = 0; active < parallel && i < jobs.length * 3; i++) {
      const job = jobs[cursor++ % jobs.length];
      if (job.state !== 'uploading' || !job.id) continue;
      const index = nextChunk(job);
      if (index === null) {
        if (!job.inflight.size && job.received.size === countChunks(job)) finish(job);
        continue;
      }
      const controller = new AbortController();
      job.inflight.set(index, controller); active++;
      sendChunk(job, index, controller).finally(() => {
        job.inflight.delete(index); active = Math.max(0, active - 1); render();
      });
    }
  }
  function pause(job) {
    if (!['uploading','preparing'].includes(job.state)) return;
    job.state = 'paused';
    for (const controller of job.inflight.values()) controller.abort();
    render();
  }
  async function resume(job) {
    if (!['paused','failed'].includes(job.state)) return;
    if (!job.id) {prepare(job); return;}
    job.state = 'preparing'; render();
    try {
      const info = await api(`/api/files/${job.id}`);
      job.received = new Set(info.received || []); job.next = 0; job.retryQueue = [];
      job.retries.clear();
      if (info.status === 'complete') job.state = 'done';
      else if (info.status === 'cancelled') throw new Error('این انتقال قبلاً لغو شده است.');
      else if (job.state === 'preparing') job.state = 'uploading';
    } catch (error) {job.state = 'failed'; job.error = error.message; toast(error.message);}
    render();
  }
  async function cancel(job) {
    if (['done','cancelled'].includes(job.state)) return;
    job.state = 'cancelled';
    for (const controller of job.inflight.values()) controller.abort();
    if (job.id) {
      try { await api(`/api/files/${job.id}`, {method:'DELETE'}); }
      catch (error) { toast('درخواست لغو به رایانه نرسید؛ نوبت را در رایانه پایان دهید.'); }
    }
    render();
  }
  function makeButton(text, handler, danger = false) {
    const b = document.createElement('button'); b.type = 'button'; b.textContent = text;
    if (danger) b.className = 'danger';
    b.addEventListener('click', handler); return b;
  }
  function render() {
    if (!config) return;
    let totalSize = 0, totalDone = 0;
    const box = el('jobs'); box.replaceChildren();
    for (const job of jobs) {
      const completed = bytesDone(job);
      if (job.state !== 'cancelled') {totalSize += job.file.size; totalDone += completed;}
      const fraction = job.file.size ? completed / job.file.size : (job.state === 'done' ? 1 : 0);
      const card = document.createElement('div'); card.className = `job ${job.state === 'done' ? 'done' : job.state === 'failed' ? 'failed' : job.state === 'paused' ? 'paused' : ''}`;
      const top = document.createElement('div'); top.className = 'jobtop';
      const title = document.createElement('div'); title.className = 'filename'; title.textContent = job.file.name;
      const actions = document.createElement('div'); actions.className = 'jobactions';
      if (['uploading','preparing'].includes(job.state)) actions.append(makeButton('⏸ توقف', () => pause(job)));
      if (['paused','failed'].includes(job.state)) actions.append(makeButton('▶ ادامه / تلاش مجدد', () => resume(job)));
      if (!['done','cancelled'].includes(job.state)) actions.append(makeButton('✕ لغو', () => cancel(job), true));
      top.append(title, actions);
      const meta = document.createElement('div'); meta.className = 'meta';
      meta.textContent = `${phrases[job.state] || job.state} • ${mb(completed)} از ${mb(job.file.size)} • ${Math.round(fraction * 100)}٪` + (job.error ? ` • ${job.error}` : '');
      const track = document.createElement('div'); track.className = 'track';
      const bar = document.createElement('div'); bar.style.width = `${Math.round(fraction * 100)}%`; track.append(bar);
      card.append(top, meta, track); box.append(card);
    }
    if (!jobs.length) {const empty = document.createElement('div'); empty.className = 'empty'; empty.textContent='فایل‌های انتخاب‌شده اینجا نمایش داده می‌شوند.'; box.append(empty);}
    const percent = totalSize ? Math.round(totalDone / totalSize * 100) : 0;
    el('overallBar').style.width = `${percent}%`;
    el('total').textContent = `${jobs.length} فایل`;
    el('overallText').textContent = `${mb(totalDone)} از ${mb(totalSize)} • ${percent}٪ • سرعت تقریبی ${speed.toFixed(1)} مگابایت بر ثانیه`;
  }
  async function init() {
    if (!token) {el('session').textContent = 'لینک نوبت نامعتبر است؛ QR را دوباره اسکن کنید.'; return;}
    try {
      config = await api('/api/session');
      el('session').textContent = `نوبت فعال: ${config.code}`;
      el('limits').textContent = `حداکثر هر فایل: ${Math.round(config.maxFileBytes / 1073741824)} گیگابایت | حداکثر هر نوبت: ${Math.round(config.maxSessionBytes / 1073741824)} گیگابایت`;
      render();
      setInterval(schedule, 180);
      setInterval(() => {
        const now = performance.now();
        let bytes = 0;
        for (const job of jobs) bytes += bytesDone(job);
        speed = Math.max(0, (bytes - lastBytes) / 1048576 / ((now - lastTick) / 1000));
        lastBytes = bytes; lastTick = now; render();
      }, 1100);
    } catch (error) {
      el('session').textContent = `ارتباط برقرار نشد: ${error.message}`;
      toast('گوشی و رایانه باید در یک وای‌فای باشند و برنامه باز باشد.');
    }
  }
  for (const id of ['allFiles','photos']) {
    el(id).addEventListener('change', (e) => {addFiles(e.target.files); e.target.value = '';});
  }
  el('pauseAll').addEventListener('click', () => jobs.forEach(pause));
  el('resumeAll').addEventListener('click', () => jobs.forEach(job => {if (job.state === 'paused' || job.state === 'failed') resume(job);}));
  el('concurrency').addEventListener('change', (e) => {parallel = Number(e.target.value) || 4;});
  init();
})();
