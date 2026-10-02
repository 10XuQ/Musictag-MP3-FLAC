'use strict';

/* musictag 界面脚本。
   所有标签读写都由服务端调用 musictag.core 完成，这里只负责收集表单、
   显示结果和错误。没有引入任何第三方库。 */

const TOKEN = document.querySelector('meta[name="token"]').content;
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const DATE_HINT = '支持 2024-05-01 / 2024/5/1 / 2024年5月1日 / 20240501，都会统一成 年-月-日';

const S = {
  src: null,
  fmt: '',
  original: null,
  cover: { action: 'keep', token: null, dataUrl: null, meta: null },
  lyricsAction: 'keep',
  busy: false,
};

/* ------------------------------------------------------------------ */
/* 小工具                                                              */
/* ------------------------------------------------------------------ */

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function baseName(p) {
  return String(p || '').split(/[\\/]/).pop() || '';
}

function dirName(p) {
  const s = String(p || '');
  const i = Math.max(s.lastIndexOf('\\'), s.lastIndexOf('/'));
  return i > 0 ? s.slice(0, i) : '';
}

function joinPath(dir, name) {
  const sep = dir.includes('\\') ? '\\' : '/';
  return dir.replace(/[\\/]+$/, '') + sep + name;
}

function bytes(n) {
  if (n === null || n === undefined || n === '') return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let v = Number(n);
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${i === 0 ? v : v.toFixed(1)} ${units[i]}`;
}

async function api(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Token': TOKEN },
    body: JSON.stringify(body || {}),
  });
  let data;
  try {
    data = await res.json();
  } catch (err) {
    throw new Error(`服务没有返回有效响应（HTTP ${res.status}）。请确认 musictag 服务还在运行。`);
  }
  if (!data.ok) throw new Error(data.error || '未知错误');
  return data;
}

function showAlert(message, good) {
  const box = $('#alert');
  box.textContent = message;
  box.classList.toggle('ok', !!good);
  box.hidden = false;
  box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function hideAlert() { $('#alert').hidden = true; }

function setBusy(busy) {
  S.busy = busy;
  $$('#editor button').forEach((b) => { b.disabled = busy; });
  $('#write').textContent = busy ? '封装中…' : '开始封装';
}

/* ------------------------------------------------------------------ */
/* 主题                                                                */
/* ------------------------------------------------------------------ */

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $('#theme-icon').textContent = theme === 'dark' ? '☀️' : '🌙';
  try { localStorage.setItem('musictag-theme', theme); } catch (err) { /* 隐私模式下写不了，忽略 */ }
}

function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem('musictag-theme'); } catch (err) { saved = null; }
  if (!saved) {
    saved = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  applyTheme(saved);
}

$('#theme-btn').addEventListener('click', () => {
  applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
});

/* ------------------------------------------------------------------ */
/* 表单 <-> 界面                                                       */
/* ------------------------------------------------------------------ */

function gatherForm() {
  return {
    src: S.src,
    title: $('#title').value,
    artists: $$('#artists input').map((i) => i.value),
    album: $('#album').value,
    date: $('#date').value,
    cover: { action: S.cover.action, token: S.cover.token },
    lyrics: { action: S.lyricsAction, text: $('#lyrics-text').value, kind: lyricsKind() },
    output: $('#output').value,
    overwrite: $('#overwrite').checked,
  };
}

function loadForm(form) {
  S.src = form.src;
  S.fmt = form.fmt || '';
  S.original = form.original || {};

  $('#fmt-chip').textContent = form.fmt || '—';
  $('#file-name').textContent = baseName(form.src);
  $('#file-path').textContent = form.src;
  $('#file-size').textContent = bytes(form.size);
  $('#file-existing').textContent = `原有标签 ${form.existing_count || 0} 项`;
  $('#audio-line').textContent = form.audio_text || '';

  $('#title').value = form.title || '';
  setArtists(form.artists && form.artists.length ? form.artists : ['']);
  $('#album').value = form.album || '';
  $('#date').value = form.date || '';
  resetDateHint();
  setLyrics(form.lyrics);

  const preview = form.cover_preview;
  S.cover = {
    action: 'keep',
    token: preview ? preview.token : null,
    dataUrl: preview ? preview.data_url : null,
    meta: preview || null,
  };
  renderCover();

  $('#output').value = form.output || '';
  $('#overwrite').checked = !!form.overwrite;

  $('#dropzone').hidden = true;
  $('#editor').hidden = false;
  hideAlert();
  $('#preview-card').hidden = true;
  $('#result-card').hidden = true;
  $('#progress').hidden = true;
  setProgress(0, '');
}

/* ---- 作者（可增删） ---- */

function addArtistRow(value) {
  const row = el('div', 'artist-row');
  const input = el('input');
  input.type = 'text';
  input.value = value || '';
  input.placeholder = '作者名，例如：李四';
  input.autocomplete = 'off';
  const remove = el('button', 'btn ghost small', '删除');
  remove.type = 'button';
  remove.addEventListener('click', () => {
    const box = $('#artists');
    row.remove();
    if (!box.children.length) addArtistRow('');
    else box.lastElementChild.querySelector('input').focus();
  });
  row.append(input, remove);
  $('#artists').appendChild(row);
  return input;
}

function setArtists(list) {
  $('#artists').innerHTML = '';
  (list && list.length ? list : ['']).forEach((name) => addArtistRow(name));
}

$('#add-artist').addEventListener('click', () => {
  const input = addArtistRow('');
  input.focus();
});

/* ---- 封面 ---- */

function renderCover() {
  const img = $('#cover-img');
  const empty = $('#cover-empty');
  const info = $('#cover-info');
  const meta = S.cover.meta;

  if (S.cover.dataUrl) {
    img.src = S.cover.dataUrl;
    img.hidden = false;
    empty.hidden = true;
  } else {
    img.removeAttribute('src');
    img.hidden = true;
    empty.hidden = false;
  }

  if (S.cover.action === 'clear') {
    info.textContent = S.original && S.original.has_cover ? '输出文件将不再包含封面' : '该文件原本就没有封面';
  } else if (meta && meta.summary) {
    info.textContent = `${meta.summary} · ${bytes(meta.size)}`;
  } else {
    info.textContent = '支持 JPG 与 PNG 图片';
  }
}

$('#pick-cover').addEventListener('click', async () => {
  try {
    const picked = await api('/api/pick', { kind: 'image', initial: '' });
    if (!picked.path) return;
    const image = await api('/api/image', { path: picked.path });
    S.cover = { action: 'set', token: image.token, dataUrl: image.data_url, meta: image };
    renderCover();
    hideAlert();
  } catch (err) {
    showAlert(`封面图片有问题：\n${err.message}`);
  }
});

$('#clear-cover').addEventListener('click', () => {
  S.cover = { action: 'clear', token: null, dataUrl: null, meta: null };
  renderCover();
});

/* ---- 歌词 ---- */

function lyricsKind() {
  const checked = document.querySelector('input[name="lyrics-kind"]:checked');
  return checked ? checked.value : 'text';
}

function updateLyricsHint() {
  const text = $('#lyrics-text').value;
  const kind = lyricsKind();
  const label = kind === 'lrc' ? 'LRC 时间轴歌词' : '纯文本歌词';
  $('#lyrics-hint').textContent = text.trim()
    ? `当前 ${text.length} 字符 · 将以「${label}」写入`
    : '留空表示不写入歌词；已勾选的格式不会影响其他标签';
}

function setLyrics(lyrics) {
  const data = lyrics || {};
  $('#lyrics-text').value = data.text || '';
  const kind = data.kind === 'lrc' ? 'lrc' : 'text';
  const radio = document.querySelector(`input[name="lyrics-kind"][value="${kind}"]`);
  if (radio) radio.checked = true;
  S.lyricsAction = 'keep';
  updateLyricsHint();
}

$('#lyrics-text').addEventListener('input', () => {
  S.lyricsAction = 'set';
  updateLyricsHint();
});

$$('input[name="lyrics-kind"]').forEach((radio) => {
  radio.addEventListener('change', () => {
    S.lyricsAction = 'set';
    updateLyricsHint();
  });
});

$('#clear-lyrics').addEventListener('click', () => {
  $('#lyrics-text').value = '';
  S.lyricsAction = 'clear';
  updateLyricsHint();
});

$('#pick-lyrics').addEventListener('click', async () => {
  try {
    const picked = await api('/api/pick', { kind: 'lyrics', initial: '' });
    if (!picked.path) return;
    const data = await api('/api/lyrics', { path: picked.path });
    $('#lyrics-text').value = data.text || '';
    const radio = document.querySelector(`input[name="lyrics-kind"][value="${data.kind === 'lrc' ? 'lrc' : 'text'}"]`);
    if (radio) radio.checked = true;
    S.lyricsAction = 'set';
    updateLyricsHint();
    showAlert(`已从 ${baseName(picked.path)} 导入歌词（识别为${data.kind === 'lrc' ? 'LRC 时间轴' : '纯文本'}）。`, true);
  } catch (err) {
    showAlert(`导入歌词失败：\n${err.message}`);
  }
});

/* ---- 发行日期 ---- */

function resetDateHint() {
  const hint = $('#date-hint');
  hint.textContent = DATE_HINT;
  hint.style.color = '';
}

let dateTimer = null;
$('#date').addEventListener('input', () => {
  clearTimeout(dateTimer);
  dateTimer = setTimeout(checkDate, 350);
});

async function checkDate() {
  const hint = $('#date-hint');
  const text = $('#date').value.trim();
  if (!text) { resetDateHint(); return; }
  try {
    const data = await api('/api/normalize-date', { text });
    hint.textContent = `将写为 ${data.date}`;
    hint.style.color = 'var(--ok)';
  } catch (err) {
    hint.textContent = err.message;
    hint.style.color = 'var(--bad)';
  }
}

/* ------------------------------------------------------------------ */
/* 选择文件                                                            */
/* ------------------------------------------------------------------ */

async function openPath(path) {
  hideAlert();
  const data = await api('/api/open', { path });
  loadForm(data.form);
}

async function pickAudio() {
  try {
    const picked = await api('/api/pick', { kind: 'audio', initial: S.src || '' });
    if (!picked.path) return;
    await openPath(picked.path);
  } catch (err) {
    showAlert(err.message);
  }
}

const AUDIO_RE = /\.(mp3|flac)$/i;

async function uploadFile(file) {
  if (!AUDIO_RE.test(file.name)) {
    showAlert(`只支持 MP3 与 FLAC 文件，这个文件是：${file.name}`);
    return;
  }
  setBusy(true);
  hideAlert();
  showAlert(`正在导入 ${file.name}（${bytes(file.size)}）…`, true);
  try {
    const res = await fetch('/api/upload', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/octet-stream',
        'X-Token': TOKEN,
        'X-Filename': encodeURIComponent(file.name),
      },
      body: file,
    });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || '未知错误');
    loadForm(data.form);
  } catch (err) {
    showAlert(err.message);
  } finally {
    setBusy(false);
  }
}

$('#dropzone').addEventListener('click', () => { $('#file-input').click(); });
$('#dropzone').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); $('#file-input').click(); }
});

$('#pick-audio-2').addEventListener('click', (event) => {
  event.stopPropagation();
  pickAudio();
});

$('#file-input').addEventListener('change', (event) => {
  const file = event.target.files && event.target.files[0];
  event.target.value = '';
  if (file) uploadFile(file);
});

$$('[data-pick-audio]').forEach((btn) => btn.addEventListener('click', pickAudio));

/* ---- 拖拽 ---- */

function hasFiles(event) {
  const dt = event.dataTransfer;
  if (!dt) return false;
  return Array.from(dt.types || []).indexOf('Files') >= 0;
}

let dragDepth = 0;

window.addEventListener('dragenter', (event) => {
  if (!hasFiles(event) || currentTab !== 'single') return;
  event.preventDefault();
  dragDepth += 1;
  $('#drop-overlay').hidden = false;
});

window.addEventListener('dragover', (event) => {
  if (!hasFiles(event) || currentTab !== 'single') return;
  event.preventDefault();
  if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
});

window.addEventListener('dragleave', (event) => {
  if (!hasFiles(event) || currentTab !== 'single') return;
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) $('#drop-overlay').hidden = true;
});

window.addEventListener('drop', (event) => {
  if (!hasFiles(event) || currentTab !== 'single') return;
  event.preventDefault();
  dragDepth = 0;
  $('#drop-overlay').hidden = true;
  const file = event.dataTransfer.files && event.dataTransfer.files[0];
  if (file) uploadFile(file);
});

/* ------------------------------------------------------------------ */
/* 输出路径                                                            */
/* ------------------------------------------------------------------ */

function suggestedName() {
  const name = baseName(S.src);
  const dot = name.lastIndexOf('.');
  return dot > 0 ? `${name.slice(0, dot)}_tagged${name.slice(dot)}` : `${name}_tagged`;
}

$('#pick-folder').addEventListener('click', async () => {
  try {
    const picked = await api('/api/pick', { kind: 'folder', initial: dirName($('#output').value) });
    if (!picked.path) return;
    const current = baseName($('#output').value);
    $('#output').value = joinPath(picked.path, current || suggestedName());
    hideAlert();
  } catch (err) {
    showAlert(err.message);
  }
});

/* ------------------------------------------------------------------ */
/* 进度 / 结果                                                         */
/* ------------------------------------------------------------------ */

function setProgress(percent, text) {
  $('#progress-bar').style.width = `${Math.max(0, Math.min(100, percent))}%`;
  if (text !== undefined) $('#stage-text').textContent = text;
}

function renderRows(container, rows) {
  const box = el('div', 'rows');
  if (!rows.length) {
    box.appendChild(el('p', 'note', '没有改动。'));
  }
  rows.forEach((row) => {
    if (row.before !== undefined) {
      const line = el('div', 'change-row');
      line.append(el('span', 'k', row.label));
      const value = el('span', 'v');
      value.append(document.createTextNode(row.before));
      value.append(el('span', 'arrow', '→'));
      value.append(document.createTextNode(row.after));
      line.append(value);
      box.appendChild(line);
    } else {
      const line = el('div', `check-row${row.ok ? '' : ' bad'}`);
      line.append(el('span', 'k', row.label));
      const value = el('span', 'v');
      value.append(el('span', 'mark', row.ok ? '✔' : '✘'));
      if (row.expected) {
        value.append(document.createTextNode(`写入 ${row.expected}`));
        if (row.actual) value.append(el('span', 'detail', ` ，读出 ${row.actual}`));
      } else {
        value.append(document.createTextNode('核验通过'));
      }
      line.append(value);
      box.appendChild(line);
    }
  });
  container.appendChild(box);
}

function renderPreview(data) {
  const card = $('#preview-card');
  const body = $('#preview-body');
  body.innerHTML = '';
  card.hidden = false;

  if (data.errors && data.errors.length) {
    data.errors.forEach((msg) => body.appendChild(el('p', 'note', `⚠ ${msg}`)));
  }
  if (data.nothing) {
    body.appendChild(el('p', 'note', '没有检测到任何改动：所有字段都和文件里已有的一致，点「开始封装」不会写入任何东西。'));
  } else {
    renderRows(body, data.rows || []);
  }
  body.appendChild(el('p', 'note', `输出格式：${data.fmt}（由源文件内容决定，与输出文件扩展名无关）`));
  body.appendChild(el('p', 'mono', data.output || '（未填写输出路径）'));
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function renderResult(result) {
  const card = $('#result-card');
  const head = card.querySelector('.result-head');
  const body = $('#result-body');
  const failed = result.failed || 0;

  card.hidden = false;
  head.className = `result-head ${failed ? 'bad' : 'ok'}`;
  $('#result-icon').textContent = failed ? '✘' : '✔';
  $('#result-title').textContent = failed
    ? `已写出，但有 ${failed} 项校验没通过`
    : '封装完成，全部校验通过';
  $('#result-path').textContent = result.output;
  $('#open-folder').hidden = false;
  $('#open-folder').onclick = async () => {
    try {
      await api('/api/reveal', { path: result.output });
    } catch (err) {
      showAlert(err.message);
    }
  };

  body.innerHTML = '';
  if (result.audio_unchanged) {
    body.appendChild(el('p', 'note', '音频数据：逐字节一致 —— 原音频流没有被重新编码，音质不变。'));
  } else {
    body.appendChild(el('p', 'note', '音频参数一致（本工具不包含编码器，任何时候都不会重新编码）。'));
  }
  body.appendChild(el('p', 'mono', `写入前：${result.audio_before}`));
  body.appendChild(el('p', 'mono', `写入后：${result.audio_after}`));
  if (result.removed && result.removed.length) {
    body.appendChild(el('p', 'note', `已清除字段：${result.removed.join('、')}`));
  }

  body.appendChild(el('h3', 'card-title', '从输出文件重新读出，逐项核对'));
  renderRows(body, result.checks || []);
  body.appendChild(el('h3', 'card-title', '本次改动'));
  renderRows(body, result.rows || []);
  body.appendChild(el('p', 'note', '源文件没有被修改，可以随时重新封装。'));

  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function renderError(message) {
  const card = $('#result-card');
  const head = card.querySelector('.result-head');
  card.hidden = false;
  head.className = 'result-head bad';
  $('#result-icon').textContent = '✘';
  $('#result-title').textContent = '没有写出文件';
  $('#result-path').textContent = '';
  $('#open-folder').hidden = true;
  const body = $('#result-body');
  body.innerHTML = '';
  body.appendChild(el('p', 'note', message));
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

/* ------------------------------------------------------------------ */
/* 试运行 / 开始封装                                                   */
/* ------------------------------------------------------------------ */

$('#dry-run').addEventListener('click', async () => {
  if (S.busy) return;
  setBusy(true);
  hideAlert();
  try {
    const data = await api('/api/preview', { form: gatherForm() });
    renderPreview(data);
  } catch (err) {
    showAlert(err.message);
  } finally {
    setBusy(false);
  }
});

$('#write').addEventListener('click', async () => {
  if (S.busy) return;
  setBusy(true);
  hideAlert();
  $('#result-card').hidden = true;
  $('#preview-card').hidden = true;
  $('#progress').hidden = false;
  setProgress(0, '准备中…');

  let outcome = null;
  const handle = (event) => {
    if (event.type === 'start') {
      setProgress(0, '准备中…');
    } else if (event.type === 'stage') {
      setProgress((event.index / event.total) * 100, event.text);
    } else if (event.type === 'done') {
      outcome = 'done';
      setProgress(100, '完成');
      renderResult(event.result);
    } else if (event.type === 'error') {
      outcome = 'error';
      $('#progress').hidden = true;
      showAlert(event.error);
      renderError(event.error);
    }
  };

  try {
    await streamRequest('/api/write', { form: gatherForm() }, handle);
    if (outcome === null) {
      $('#progress').hidden = true;
      const message = '写入过程意外中断，没有收到结果。请重试；如果反复出现，请检查服务端控制台的输出。';
      showAlert(message);
      renderError(message);
    }
  } catch (err) {
    $('#progress').hidden = true;
    showAlert(err.message);
    renderError(err.message);
  } finally {
    setBusy(false);
  }
});

/* ------------------------------------------------------------------ */
/* 流式请求（逐行 NDJSON）                                             */
/* ------------------------------------------------------------------ */

async function streamRequest(path, body, handle) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Token': TOKEN },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok || !res.body) {
    throw new Error(`服务返回异常（HTTP ${res.status}）。请确认 musictag 服务还在运行。`);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const chunk = await reader.read();
    if (chunk.done) break;
    buffer += decoder.decode(chunk.value, { stream: true });
    let index = buffer.indexOf('\n');
    while (index >= 0) {
      const line = buffer.slice(0, index).trim();
      buffer = buffer.slice(index + 1);
      if (line) handle(JSON.parse(line));
      index = buffer.indexOf('\n');
    }
  }
  if (buffer.trim()) handle(JSON.parse(buffer.trim()));
}

/* ------------------------------------------------------------------ */
/* 页签                                                                */
/* ------------------------------------------------------------------ */

let currentTab = 'single';

function switchTab(name) {
  if (name === currentTab) return;
  currentTab = name;
  $$('.tab').forEach((btn) => {
    const on = btn.dataset.tab === name;
    btn.classList.toggle('is-on', on);
    btn.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  $('#tab-single').hidden = name !== 'single';
  $('#tab-batch').hidden = name !== 'batch';
  window.scrollTo({ top: 0 });
  try { localStorage.setItem('musictag-tab', name); } catch (err) { /* 无所谓 */ }
}

$$('.tab').forEach((btn) => btn.addEventListener('click', () => switchTab(btn.dataset.tab)));

/* ------------------------------------------------------------------ */
/* 批量打包                                                            */
/* ------------------------------------------------------------------ */

const B = {
  plan: null,
  busy: false,
  outDir: '',
};

/* 浏览器出于安全不会告诉网页「拖进来的文件夹在哪」，
   所以拖文件夹只能把里面的文件复制到工作目录再处理。
   超过这个大小就直接劝用户改用系统对话框（那是原地处理、不复制）。 */
const BATCH_DROP_LIMIT = 300 * 1024 * 1024;

const BATCH_AUDIO_RE = /\.(mp3|flac|ncm)$/i;
const BATCH_LRC_RE = /\.lrc$/i;

function batchAlert(message, good) {
  const box = $('#batch-alert');
  box.textContent = message;
  box.classList.toggle('ok', !!good);
  box.hidden = false;
}

function hideBatchAlert() { $('#batch-alert').hidden = true; }

function setBatchBusy(busy) {
  B.busy = busy;
  $('#batch-run').disabled = busy || !B.plan;
  $('#batch-scan').disabled = busy;
  $('#batch-rescan').disabled = busy;
  $('#batch-pick-src').disabled = busy;
  $('#batch-pick-out').disabled = busy;
  $('#batch-src').disabled = busy;
  $('#batch-out').disabled = busy;
}

function batchPayload() {
  return {
    src_dir: $('#batch-src').value.trim(),
    out_dir: $('#batch-out').value.trim(),
    only_with_lyrics: $('#batch-only-lyrics').checked,
    overwrite: $('#batch-overwrite').checked,
    lyrics_mode: (document.querySelector('input[name="batch-lyrics"]:checked') || {}).value || 'lrc',
  };
}

const STATUS_TEXT = { pending: '待处理', done: '完成', skipped: '跳过', failed: '失败' };

function renderBatchTable(items) {
  const body = $('#batch-list');
  body.replaceChildren();
  items.forEach((item) => {
    const tr = document.createElement('tr');
    tr.dataset.name = item.name;

    const tdKind = el('td');
    tdKind.appendChild(el('span', `tag ${item.kind === 'ncm' ? 'ncm' : 'audio'}`,
      item.kind === 'ncm' ? '解密' : (item.fmt || '音频')));

    const tdName = el('td', 'name');
    tdName.appendChild(el('div', null, item.name));
    const sub = [];
    if (item.match === 'order') sub.push('歌词文件名顺序和音频相反');
    if (item.filled && item.filled.length) sub.push(`已按 ncm 补齐：${item.filled.join('、')}`);
    if (item.output) sub.push(`→ ${item.output}`);
    if (item.message) sub.push(item.message);
    if (sub.length) tdName.appendChild(el('div', 'sub', sub.join(' · ')));

    const tdLrc = el('td', 'sub');
    tdLrc.textContent = item.lrc || '—';

    const tdSt = el('td');
    const st = el('span', `st ${item.status}`, STATUS_TEXT[item.status] || item.status);
    tdSt.appendChild(st);

    tr.append(tdKind, tdName, tdLrc, tdSt);
    body.appendChild(tr);
  });
}

function updateBatchRow(item) {
  const items = B.plan ? B.plan.items : [];
  const index = items.findIndex((i) => i.name === item.name);
  if (index >= 0) items[index] = item; else items.push(item);
  renderBatchTable(items);
  const row = $('#batch-list').querySelector(`tr[data-name="${CSS.escape(item.name)}"]`);
  if (row) row.scrollIntoView({ block: 'nearest' });
}

function renderBatchScan(data) {
  B.plan = { items: data.items || [], summary: data.summary || {} };
  B.outDir = data.out_dir || '';
  $('#batch-out').value = B.outDir;
  $('#batch-panel').hidden = false;
  $('#batch-summary-card').hidden = false;
  $('#batch-list-card').hidden = false;
  $('#batch-result-card').hidden = true;

  const s = data.summary || {};
  const text = $('#batch-summary-text');
  text.replaceChildren();
  text.append(
    document.createTextNode('共 '),
    el('strong', null, s.total || 0),
    document.createTextNode(' 个文件：'),
    el('strong', null, s.audio || 0),
    document.createTextNode(' 个音频 + '),
    el('strong', null, s.ncm || 0),
    document.createTextNode(' 个要解密的 ncm，其中 '),
    el('strong', null, s.with_lyrics || 0),
    document.createTextNode(' 个配到了歌词。'),
  );

  const notes = $('#batch-notes');
  notes.replaceChildren();
  (data.notes || []).forEach((n) => notes.appendChild(el('li', null, n)));

  const orphans = data.orphans || [];
  $('#batch-orphans').hidden = orphans.length === 0;
  if (orphans.length) {
    $('#batch-orphan-summary').textContent = `有 ${orphans.length} 个歌词找不到对应的音频（不会被写入）`;
    const list = $('#batch-orphan-list');
    list.replaceChildren();
    list.className = 'notes warn';
    orphans.forEach((n) => list.appendChild(el('li', null, n)));
  }

  $('#batch-count').textContent = `${(data.items || []).length} 项`;
  renderBatchTable(data.items || []);
  setBatchBusy(false);
}

async function batchScan(quiet) {
  if (B.busy) return;
  const payload = batchPayload();
  if (!payload.src_dir) {
    batchAlert('请先选择要打包的目录（源目录）。');
    return;
  }
  setBatchBusy(true);
  if (!quiet) hideBatchAlert();
  try {
    const data = await api('/api/batch/scan', payload);
    renderBatchScan(data);
    if (!quiet) {
      const s = data.summary || {};
      batchAlert(`扫描完成：${s.total || 0} 个文件，${s.with_lyrics || 0} 个配到了歌词。`
        + (s.with_lyrics ? '点「开始打包」即可写出。' : '这个目录里的文件都还没有歌词。'), true);
    }
  } catch (err) {
    $('#batch-panel').hidden = true;
    $('#batch-summary-card').hidden = true;
    $('#batch-list-card').hidden = true;
    B.plan = null;
    $('#batch-run').disabled = true;
    batchAlert(err.message);
  } finally {
    setBatchBusy(false);
  }
}

$('#batch-scan').addEventListener('click', () => batchScan(false));
$('#batch-rescan').addEventListener('click', () => batchScan(false));

$('#batch-run').addEventListener('click', async () => {
  if (B.busy || !B.plan) return;
  setBatchBusy(false);
  setBatchBusy(true);
  hideBatchAlert();
  $('#batch-result-card').hidden = true;
  $('#batch-progress').hidden = false;
  $('#batch-progress-bar').style.width = '0%';
  $('#batch-stage').textContent = '准备中…';

  let outcome = null;
  let finished = 0;
  const handle = (event) => {
    if (event.type === 'start') {
      $('#batch-stage').textContent = `开始处理 ${event.total} 个文件…`;
    } else if (event.type === 'item') {
      if (event.item && Object.keys(event.item).length) updateBatchRow(event.item);
      if (event.status && event.status !== 'start') {
        finished += 1;
        $('#batch-progress-bar').style.width = `${(finished / Math.max(1, event.total)) * 100}%`;
        $('#batch-stage').textContent = `[${finished}/${event.total}] ${event.name} ${event.text || ''}`;
      }
    } else if (event.type === 'done') {
      outcome = 'done';
      $('#batch-progress-bar').style.width = '100%';
      $('#batch-stage').textContent = '完成';
      renderBatchResult(event.result);
    } else if (event.type === 'error') {
      outcome = 'error';
      $('#batch-progress').hidden = true;
      batchAlert(event.error);
      setBatchBusy(false);
    }
  };

  try {
    await streamRequest('/api/batch/run', batchPayload(), handle);
    if (outcome === null) {
      $('#batch-progress').hidden = true;
      batchAlert('打包过程意外中断，没有收到结果。请重试；如果反复出现，请检查服务端控制台的输出。');
    }
  } catch (err) {
    $('#batch-progress').hidden = true;
    batchAlert(err.message);
  } finally {
    setBatchBusy(false);
  }
});

function renderBatchResult(result) {
  const card = $('#batch-result-card');
  card.hidden = false;
  const stats = result.stats || {};
  const failed = stats.failed || 0;
  $('#batch-result-icon').textContent = failed ? '✘' : '✔';
  $('#batch-result-icon').parentElement.className = `result-head ${failed ? 'bad' : 'ok'}`;
  $('#batch-result-title').textContent = failed ? `完成，但有 ${failed} 个失败` : '打包完成';
  $('#batch-result-path').textContent = result.out_dir || '';
  $('#batch-open').hidden = !result.out_dir;
  $('#batch-open').dataset.path = result.out_dir || '';

  const body = $('#batch-result-body');
  body.replaceChildren();
  const rows = el('div', 'rows');
  const line = (k, v) => {
    const row = el('div', 'check-row');
    row.appendChild(el('span', 'k', k));
    const val = el('span', 'v');
    val.textContent = v;
    row.appendChild(val);
    rows.appendChild(row);
  };
  line('写出', `${stats.done || 0} 个`);
  line('跳过', `${stats.skipped || 0} 个`);
  line('失败', `${failed} 个`);
  if (stats.decrypted) line('解密 ncm', `${stats.decrypted} 个`);
  if (stats.lyrics_written) line('写入歌词', `${stats.lyrics_written} 个`);
  if (stats.verify_failed) line('校验未过', `${stats.verify_failed} 个`);
  body.appendChild(rows);

  if ((result.notes || []).length) {
    const notes = el('ul', 'notes');
    result.notes.forEach((n) => notes.appendChild(el('li', null, n)));
    body.appendChild(notes);
  }
  if ((result.orphans || []).length) {
    const notes = el('ul', 'notes warn');
    notes.appendChild(el('li', null,
      `${result.orphans.length} 个歌词没有找到对应的音频：${result.orphans.join('、')}`));
    body.appendChild(notes);
  }
  if (result.out_dir) {
    batchAlert(failed
      ? `打包结束，${stats.done || 0} 个写出，${failed} 个失败。`
      : `打包完成：${stats.done || 0} 个写出，${stats.skipped || 0} 个跳过。输出在 ${result.out_dir}`, !failed);
  }
}

/* ---- 目录选择 ---- */

async function pickBatchDir(which) {
  const input = which === 'src' ? $('#batch-src') : $('#batch-out');
  try {
    const picked = await api('/api/pick', { kind: 'folder', initial: input.value.trim() });
    if (!picked.path) return;
    input.value = picked.path;
    hideBatchAlert();
    if (which === 'src') {
      $('#batch-out').value = '';
      await batchScan(true);
    }
  } catch (err) {
    batchAlert(err.message);
  }
}

$('#batch-pick-src').addEventListener('click', () => pickBatchDir('src'));
$('#batch-pick-src-2').addEventListener('click', (event) => {
  event.stopPropagation();  // 别让外层拖拽区再弹一次对话框
  pickBatchDir('src');
});
$('#batch-pick-out').addEventListener('click', () => pickBatchDir('out'));
$('#batch-drop').addEventListener('click', () => pickBatchDir('src'));

$('#batch-open').addEventListener('click', async () => {
  const path = $('#batch-open').dataset.path;
  if (!path) return;
  try {
    await api('/api/reveal', { path });
  } catch (err) {
    batchAlert(err.message);
  }
});

/* ---- 拖入文件夹 ---- */

function entryFiles(entry, out) {
  return new Promise((resolve) => {
    if (entry.isFile) {
      entry.file((file) => { out.push(file); resolve(); }, () => resolve());
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      const all = [];
      const step = () => reader.readEntries(async (batch) => {
        if (!batch.length) {
          for (const child of all) await entryFiles(child, out);
          resolve();
          return;
        }
        all.push(...batch);
        step();
      }, () => resolve());
      step();
    } else {
      resolve();
    }
  });
}

async function uploadForBatch(file) {
  const res = await fetch('/api/upload', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/octet-stream',
      'X-Token': TOKEN,
      'X-Filename': encodeURIComponent(file.name),
    },
    body: file,
  });
  const data = await res.json();
  if (!data.ok) throw new Error(data.error || `上传 ${file.name} 失败`);
  return data;
}

$('#batch-drop').addEventListener('dragover', (event) => {
  if (!hasFiles(event)) return;
  event.preventDefault();
  event.stopPropagation();
  event.dataTransfer.dropEffect = 'copy';
  $('#batch-drop').classList.add('is-over');
});

$('#batch-drop').addEventListener('dragleave', () => {
  $('#batch-drop').classList.remove('is-over');
});

$('#batch-drop').addEventListener('drop', async (event) => {
  if (!hasFiles(event)) return;
  event.preventDefault();
  event.stopPropagation();
  dragDepth = 0;
  $('#drop-overlay').hidden = true;
  $('#batch-drop').classList.remove('is-over');

  const items = Array.from(event.dataTransfer.items || []);
  const entries = items
    .map((it) => (it.webkitGetAsEntry ? it.webkitGetAsEntry() : null))
    .filter(Boolean);
  const hasFolder = entries.some((e) => e.isDirectory);

  if (!hasFolder) {
    batchAlert('这里要拖的是整个文件夹。也可以直接点「用系统对话框选择文件夹」。');
    return;
  }

  setBatchBusy(true);
  batchAlert('正在读取拖入的文件夹…');
  let copied = null;
  try {
    const files = [];
    for (const entry of entries) await entryFiles(entry, files);
    const wanted = files.filter((f) => BATCH_AUDIO_RE.test(f.name) || BATCH_LRC_RE.test(f.name));
    if (!wanted.length) {
      batchAlert('这个文件夹里没有 mp3 / flac / ncm / lrc 文件。');
      return;
    }
    const total = wanted.reduce((sum, f) => sum + f.size, 0);
    if (total > BATCH_DROP_LIMIT) {
      batchAlert(`这个文件夹有 ${bytes(total)}，拖拽会先复制一份到工作目录，太大了。\n`
        + '请点「用系统对话框选择文件夹」——那是直接在原目录上处理，一个字节都不会复制。');
      return;
    }
    batchAlert(`正在复制 ${wanted.length} 个文件（${bytes(total)}）到工作目录…`);
    let first = null;
    for (const file of wanted) {
      const data = await uploadForBatch(file);
      if (!first) first = data.form && data.form.src;
    }
    if (!first) {
      batchAlert('复制失败：没有得到文件路径。');
      return;
    }
    copied = { count: wanted.length, total };
    $('#batch-src').value = dirName(first);
    $('#batch-out').value = '';
  } catch (err) {
    batchAlert(err.message);
    return;
  } finally {
    setBatchBusy(false);
  }

  await batchScan(true);
  if (copied && B.plan) {
    batchAlert(`已把 ${copied.count} 个文件（${bytes(copied.total)}）复制到工作目录：`
      + `${B.plan.summary.with_lyrics || 0} 个配到了歌词。`
      + '（拖拽只能复制；想原地处理请用「用系统对话框选择文件夹」。）', true);
  }
});

/* ------------------------------------------------------------------ */

initTheme();
try {
  const saved = localStorage.getItem('musictag-tab');
  if (saved === 'batch') switchTab('batch');
} catch (err) { /* 无所谓 */ }
updateLyricsHint();
$('#file-input').setAttribute('accept', '.mp3,.flac,audio/mpeg,audio/flac');
