'use strict';
/* Nazzel desktop interface. Talks to Python through window.pywebview.api. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const api = () => window.pywebview.api;
const esc = (t) => String(t ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const enc = (key) => key.split('/').map(encodeURIComponent).join('/');

const state = { mode: 'video', folder: '', filter: 'all', items: [], settings: {}, attachment: null, queue: [], current: null };

function fmtBytes(n) {
  if (!n) return '';
  const u = ['ب', 'ك.ب', 'م.ب', 'ج.ب', 'ت.ب'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i ? 1 : 0)} ${u[i]}`;
}
function fmtTime(s) {
  s = Math.max(0, Math.round(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(x).padStart(2, '0')}` : `${m}:${String(x).padStart(2, '0')}`;
}
function toast(text, ms = 2600) {
  const t = $('#toast');
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), ms);
}
function modal(html, onMount) {
  $('#modal-body').innerHTML = html;
  $('#modal').hidden = false;
  if (onMount) onMount($('#modal-body'));
}
function closeModal() { $('#modal').hidden = true; }
$('#modal').addEventListener('click', (e) => { if (e.target.id === 'modal') closeModal(); });
function ask(title, placeholder = '', value = '', secret = false) {
  return new Promise((resolve) => {
    modal(`<h3>${esc(title)}</h3><input id="m-in" type="${secret ? 'password' : 'text'}" style="width:100%" placeholder="${esc(placeholder)}" value="${esc(value)}">
      <div class="row" style="margin-top:14px;justify-content:flex-end"><button class="ghost" id="m-no">إلغاء</button><button class="primary" id="m-ok">تم</button></div>`, (el) => {
      const input = $('#m-in', el);
      input.focus();
      input.select();
      const done = (v) => { closeModal(); resolve(v); };
      $('#m-ok', el).onclick = () => done(input.value);
      $('#m-no', el).onclick = () => done(null);
      input.onkeydown = (e) => { if (e.key === 'Enter') done(input.value); if (e.key === 'Escape') done(null); };
    });
  });
}

/* ------------------------------------------------------------------ navigation */
function show(view) {
  $$('nav button').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
  $$('.view').forEach((v) => v.classList.toggle('active', v.id === `view-${view}`));
  if (view === 'library') loadLibrary();
  if (view === 'assistant') loadChat();
  if (view === 'follow') loadSubs();
  if (view === 'settings') loadSettings();
  if (view === 'browser') loadBrowserView();
  if (view === 'settings') loadMemory();
}
$$('nav button').forEach((b) => (b.onclick = () => show(b.dataset.view)));

/* ------------------------------------------------------------------ download */
$$('#mode button').forEach((b) => (b.onclick = () => {
  state.mode = b.dataset.v;
  $$('#mode button').forEach((x) => x.classList.toggle('on', x === b));
}));
$('#go').onclick = async () => {
  const text = $('#link').value.trim();
  if (!text) return toast('الصق رابط أول');
  const count = await api().add(text, state.mode, $('#quality').value);
  if (!count) return toast('ما لقيت رابط واضح');
  $('#link').value = '';
  $('#stremio-box').hidden = true;
  toast(count > 1 ? `بدأ تحميل ${count} روابط ⬇` : 'بدأ التحميل ⬇');
  refreshJobs();
};
$('#link').addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || !e.shiftKey)) { e.preventDefault(); $('#go').click(); } });
$('#link').addEventListener('input', checkStremio);
$('#paste-btn').onclick = async () => {
  try { $('#link').value = await navigator.clipboard.readText(); checkStremio(); } catch { toast('اضغط Ctrl+V في المربع'); }
};
$('#open-folder').onclick = () => api().open_folder('');
$('#lib-open').onclick = () => api().open_folder(state.folder);
$('#clear-done').onclick = async () => { await api().clear_finished(); refreshJobs(); };

async function checkStremio() {
  const text = $('#link').value.trim();
  const box = $('#stremio-box');
  const target = text.includes('stremio') ? await api().stremio_target(text) : null;
  if (!target) { box.hidden = true; return; }
  const [kind, id] = target;
  box.hidden = false;
  box.innerHTML = kind === 'movie'
    ? `🎬 فيلم من ستريميو: <button class="ghost small" id="st-one">نزّل الفيلم مع الترجمة العربية</button>`
    : `📺 مسلسل من ستريميو: ${id.includes(':') ? '<button class="ghost small" id="st-one">نزّل هالحلقة</button>' : ''}
       <button class="ghost small" id="st-season">نزّل الموسم كامل</button>`;
  const one = $('#st-one');
  if (one) one.onclick = async () => {
    const error = await api().stremio_download(kind, id);
    toast(error || 'بدأ التحميل ⬇ مع الترجمة العربية');
    refreshJobs();
  };
  const season = $('#st-season');
  if (season) season.onclick = () => seasonDialog(id);
}

async function seasonDialog(seriesId) {
  modal('<div class="empty">أجيب الحلقات…</div>');
  const data = await api().stremio_episodes(seriesId);
  const seasons = [...new Set(data.episodes.map((e) => e.season))].filter((s) => s > 0);
  const startSeason = seriesId.includes(':') ? Number(seriesId.split(':')[1]) : seasons[0];
  const render = (season) => {
    const eps = data.episodes.filter((e) => e.season === season);
    modal(`<h3>${esc(data.title)}</h3>
      <div class="row wrap"><select id="ss">${seasons.map((s) => `<option value="${s}" ${s === season ? 'selected' : ''}>الموسم ${s}</option>`).join('')}</select>
      <button class="ghost small" id="all">تحديد الكل</button></div>
      <div style="max-height:46vh;overflow:auto;margin:12px 0">${eps.map((e) => `<label class="row" style="padding:6px 0"><input type="checkbox" value="${esc(e.id)}" checked> ${e.number}. ${esc(e.name)}</label>`).join('')}</div>
      <div class="row" style="justify-content:flex-end"><button class="ghost" id="cancel">إلغاء</button><button class="primary" id="dl">نزّل المحدد</button></div>
      <div id="sres" class="hint"></div>`, (el) => {
      $('#ss', el).onchange = (e) => render(Number(e.target.value));
      $('#all', el).onclick = () => $$('input[type=checkbox]', el).forEach((c) => (c.checked = true));
      $('#cancel', el).onclick = closeModal;
      $('#dl', el).onclick = async () => {
        const chosen = $$('input[type=checkbox]:checked', el).map((c) => c.value);
        const fails = [];
        for (const [i, id] of chosen.entries()) {
          $('#sres', el).textContent = `أدور رابط ${i + 1}/${chosen.length}…`;
          const error = await api().stremio_download('series', id);
          if (error) fails.push(`${id}: ${error}`);
        }
        $('#sres', el).textContent = fails.length ? fails.join(' · ') : '';
        if (!fails.length) { closeModal(); toast(`بدأ تحميل ${chosen.length} حلقة ⬇`); }
        refreshJobs();
      };
    });
  };
  if (!seasons.length) return modal('<div class="empty">ما لقيت حلقات</div>');
  render(seasons.includes(startSeason) ? startSeason : seasons[0]);
}

async function refreshJobs() {
  const jobs = await api().jobs();
  const active = jobs.filter((j) => ['queued', 'running'].includes(j.phase)).length;
  $('#badge-jobs').hidden = !active;
  $('#badge-jobs').textContent = active;
  $('#jobs-empty').hidden = jobs.length > 0;
  $('#jobs').innerHTML = jobs.map((j) => {
    const pct = j.fraction != null ? Math.round(j.fraction * 100) : null;
    let status = j.status;
    if (j.phase === 'running' && j.total) {
      status = `${pct}% · ${fmtBytes(j.downloaded)} من ${fmtBytes(j.total)}` + (j.speed ? ` · ⚡ ${fmtBytes(j.speed)}/ث` : '') + (j.eta ? ` · باقي ${fmtTime(j.eta)}` : '');
    }
    if (j.phase === 'failed') status = j.error;
    const buttons = {
      queued: `<button class="icon-btn" data-a="pause" title="إيقاف">⏸</button><button class="icon-btn" data-a="cancel" title="إلغاء">✕</button>`,
      running: `<button class="icon-btn" data-a="pause" title="إيقاف">⏸</button><button class="icon-btn" data-a="cancel" title="إلغاء">✕</button>`,
      paused: `<button class="icon-btn" data-a="resume" title="كمّل">▶</button><button class="icon-btn" data-a="remove" title="حذف">✕</button>`,
      failed: `<button class="icon-btn" data-a="retry" title="إعادة">↻</button><button class="icon-btn" data-a="remove" title="حذف">✕</button>`,
      cancelled: `<button class="icon-btn" data-a="retry" title="إعادة">↻</button><button class="icon-btn" data-a="remove" title="حذف">✕</button>`,
      done: `<button class="icon-btn" data-a="play" title="شغّل">▶</button><button class="icon-btn" data-a="folder" title="المجلد">📂</button><button class="icon-btn" data-a="remove" title="إخفاء">✕</button>`,
    }[j.phase];
    const icon = { video: '🎬', audio: '🎵', photos: '🖼' }[j.mode] || '⬇';
    return `<div class="job ${j.phase}" data-id="${j.id}">
      <div class="thumb" style="${j.thumbnail ? `background-image:url('${esc(j.thumbnail)}')` : ''}">${j.thumbnail ? '' : icon}</div>
      <div class="body"><div class="title">${esc(j.title)}</div><div class="status">${esc(status)}</div>
      ${j.phase === 'running' ? `<div class="bar ${pct == null ? 'indeterminate' : ''}"><i style="width:${pct ?? 0}%"></i></div>` : ''}</div>
      <div class="row">${buttons}</div></div>`;
  }).join('');
  $$('#jobs .job').forEach((el) => $$('[data-a]', el).forEach((b) => (b.onclick = async () => {
    const id = el.dataset.id, a = b.dataset.a;
    const job = jobs.find((j) => j.id === id);
    if (a === 'play' && job.files[0]) return openFile(job.files[0]);
    if (a === 'folder') return api().open_folder(job.files[0] ? keyOf(job.files[0]) : '');
    await api().job_action(id, a);
    refreshJobs();
  })));
}
function keyOf(path) {
  const root = (state.settings.download_dir || '').replace(/\\/g, '/').replace(/\/$/, '');
  const p = path.replace(/\\/g, '/');
  return p.startsWith(root) ? p.slice(root.length + 1) : p;
}
async function openFile(path) {
  const key = keyOf(path);
  const item = await api().item_details(key);
  openItem(item);
}

/* ------------------------------------------------------------------ library */
$$('#filters button').forEach((b) => (b.onclick = () => {
  state.filter = b.dataset.f;
  $$('#filters button').forEach((x) => x.classList.toggle('on', x === b));
  renderItems();
}));
$('#search').addEventListener('input', renderItems);
$('#new-folder').onclick = async () => {
  const name = await ask('اسم المجلد', 'مثل: أفلام');
  if (name) { await api().new_folder(name, state.folder); loadLibrary(); }
};
$('#import').onclick = async () => {
  const files = await api().choose_files(true);
  if (!files.length) return;
  const n = await api().import_files(files, state.folder);
  toast(`انضاف ${n} ✓`);
  loadLibrary();
};
$('#new-list').onclick = async () => {
  const name = await ask('اسم القائمة', 'مثل: أفلام الويكند');
  if (name) { await api().playlist_create(name); loadLibrary(); }
};
$('#montage-btn').onclick = montageDialog;

async function loadLibrary() {
  const data = await api().library(state.folder);
  state.items = data.items;
  $('#lib-title').textContent = state.folder ? state.folder.split('/').pop() : 'الملفات';
  const parts = state.folder ? state.folder.split('/') : [];
  $('#crumbs').innerHTML = state.folder ? `<a data-k="">الملفات</a>` + parts.map((p, i) => ` / <a data-k="${esc(parts.slice(0, i + 1).join('/'))}">${esc(p)}</a>`).join('') : '';
  $$('#crumbs a').forEach((a) => (a.onclick = () => { state.folder = a.dataset.k; loadLibrary(); }));
  $('#folders').innerHTML = data.folders.map((f) => `<div class="folder" data-k="${esc(f.key)}">📁 <b class="grow">${esc(f.name)}</b><span class="hint">${f.count}</span></div>`).join('');
  $$('#folders .folder').forEach((el) => {
    el.onclick = () => { state.folder = el.dataset.k; loadLibrary(); };
    el.oncontextmenu = (e) => { e.preventDefault(); menu(e, [['📂 فتح في المستكشف', () => api().open_folder(el.dataset.k)], ['—'], ['🗑 حذف المجلد', async () => { if (confirm('تحذف المجلد وكل اللي فيه؟')) { await api().delete(el.dataset.k); loadLibrary(); } }, 'danger']]); };
  });
  const cont = data.continue || [];
  $('#continue-row').hidden = !(cont.length && !state.folder);
  $('#continue').innerHTML = cont.map((i) => card(i, `باقي ${fmtTime(i.remaining)}`)).join('');
  bindCards($('#continue'), cont);
  const lists = data.playlists || [];
  $('#playlists-row').hidden = state.folder !== '';
  $('#playlists').innerHTML = lists.map((p) => `<div class="pl" data-id="${p.id}"><div style="font-size:24px">🎶</div><b>${esc(p.name)}</b><div class="hint">${p.count} ملف</div></div>`).join('') || '<div class="hint">اضغط يمين على أي ملف ← «أضف لقائمة تشغيل»</div>';
  $$('#playlists .pl').forEach((el) => (el.onclick = () => playlistDialog(el.dataset.id)));
  renderItems();
}
function card(i, extra) {
  const icon = { video: '🎬', audio: '🎵', image: '🖼', file: i.ext === 'pdf' ? '📕' : ['ipa', 'tipa'].includes(i.ext) ? '📱' : ['zip', '7z', 'rar', 'tar', 'gz'].includes(i.ext) ? '🗜' : '📄' }[i.kind];
  const cover = i.cover || ['video', 'image'].includes(i.kind) ? `background-image:url('/cover/${enc(i.key)}')` : '';
  return `<div class="item" data-k="${esc(i.key)}"><div class="cover" style="${cover}">${cover ? '' : icon}
    <span class="tag">${esc(i.ext.toUpperCase())}${i.subtitles ? ' · ترجمة' : ''}</span>${i.progress ? `<span class="prog" style="width:${Math.round(i.progress * 100)}%"></span>` : ''}</div>
    <div class="meta"><div class="t">${esc(i.title)}</div><div class="s">${esc(extra || [i.uploader, fmtBytes(i.size)].filter(Boolean).join(' · '))}</div></div></div>`;
}
function renderItems() {
  const q = $('#search').value.trim().toLowerCase();
  const list = state.items.filter((i) => (state.filter === 'all' || i.kind === state.filter) && (!q || (i.title + ' ' + (i.uploader || '')).toLowerCase().includes(q)));
  $('#items').innerHTML = list.map((i) => card(i)).join('');
  $('#lib-empty').hidden = list.length > 0 || $('#folders').children.length > 0;
  bindCards($('#items'), list);
}
function bindCards(root, list) {
  $$('.item', root).forEach((el) => {
    const item = list.find((i) => i.key === el.dataset.k);
    el.onclick = () => openItem(item, list);
    el.oncontextmenu = (e) => { e.preventDefault(); itemMenu(e, item); };
  });
}
function openItem(item, list) {
  if (item.kind === 'video' || item.kind === 'audio') {
    const queue = naturalSort((list || []).filter((i) => i.kind === item.kind));
    return play(item, queue.length ? queue : [item]);
  }
  if (item.kind === 'image' || item.ext === 'pdf') {
    return modal(item.ext === 'pdf'
      ? `<iframe src="/media/${enc(item.key)}" style="width:80vw;height:80vh;border:0;border-radius:10px;background:#fff"></iframe>`
      : `<img src="/media/${enc(item.key)}" style="max-width:80vw;max-height:80vh;border-radius:10px">`,
      (el) => { el.parentElement.style.width = 'auto'; });
  }
  if (['ipa', 'tipa'].includes(item.ext)) return ipaDialog(item);
  if (['zip', '7z', 'tar', 'gz', 'tgz', 'bz2', 'xz', 'cbz'].includes(item.ext)) return runTool('extract', item);
  api().open_external(item.key);
}
function itemMenu(e, item) {
  const media = item.kind === 'video' || item.kind === 'audio';
  const entries = [];
  if (media) entries.push(['▶ تشغيل', () => openItem(item, state.items)]);
  entries.push(['↗ فتح بالبرنامج الافتراضي', () => api().open_external(item.key)]);
  entries.push(['📂 إظهار في المجلد', () => api().open_folder(item.key)]);
  entries.push(['📱 أرسل للجوال (واي فاي)', () => transferDialog('send', [item.key])]);
  entries.push(['—']);
  if (item.kind === 'image') entries.push(['📝 استخرج النص من الصورة', () => ocrDialog(item.key, 0, false)]);
  if (item.kind === 'video' && item.ext !== 'mp4') entries.push(['🔄 حوّل إلى MP4', () => runTool('mp4', item)]);
  if (media && item.ext !== 'mp3') entries.push(['🎵 حوّل إلى MP3', () => runTool('mp3', item)]);
  if (media) entries.push(['🎚 استوديو الصوت (قص، صوت، سرعة…)', () => audioStudioDialog(item)]);
  if (item.kind === 'image') entries.push(['🏷 حوّله لملصق', () => runTool('sticker', item)]);
  if (item.kind === 'video') {
    entries.push(['🗜 ضغط (واتساب…)', () => compressDialog(item)]);
    entries.push(['🖼 صنع GIF', () => gifDialog(item, 0)]);
    entries.push(['🏷 صنع ملصق', () => stickerDialog(item, 0)]);
    entries.push(['💬 جيب ترجمات عربية', () => runTool('arsubs', item)]);
  }
  if (media) {
    entries.push(['✨ ترجمة عربية بالذكاء الاصطناعي', () => runTool('aisubs', item)]);
    entries.push(['📝 تفريغ نصي', () => runTool('transcript', item)]);
    entries.push(['🎶 أضف لقائمة تشغيل', () => addToPlaylist(item)]);
  }
  if (['zip', '7z', 'tar', 'gz', 'tgz', 'ipa', 'tipa', 'cbz'].includes(item.ext)) entries.push(['🗜 فك الضغط', () => runTool('extract', item)]);
  if (['ipa', 'tipa'].includes(item.ext)) entries.push(['📱 معلومات التطبيق', () => ipaDialog(item)]);
  entries.push(['✨ اسأل المساعد عن الملف', () => { state.attachment = item.key; show('assistant'); showAttachment(); }]);
  entries.push(['—']);
  entries.push(['✏ إعادة تسمية', async () => { const n = await ask('الاسم الجديد', '', item.title); if (n) { await api().rename(item.key, n); loadLibrary(); } }]);
  entries.push(['📁 نقل لمجلد', () => moveDialog(item)]);
  entries.push(['🗑 حذف', async () => { if (confirm(`تحذف «${item.title}»؟`)) { await api().delete(item.key); loadLibrary(); } }, 'danger']);
  menu(e, entries);
}
function menu(e, entries) {
  const m = $('#menu');
  m.innerHTML = entries.map(([label, , cls], i) => (label === '—' ? '<hr>' : `<button data-i="${i}" class="${cls || ''}">${esc(label)}</button>`)).join('');
  m.hidden = false;
  const x = Math.min(e.clientX, window.innerWidth - 250), y = Math.min(e.clientY, window.innerHeight - m.offsetHeight - 10);
  m.style.left = `${x}px`;
  m.style.top = `${Math.max(8, y)}px`;
  $$('button', m).forEach((b) => (b.onclick = () => { m.hidden = true; entries[b.dataset.i][1](); }));
}
document.addEventListener('click', (e) => { if (!e.target.closest('#menu')) $('#menu').hidden = true; });

async function runTool(name, item, options = {}) {
  await api().tool(name, item.key, options);
  toast('بدأ… تقدر تتابعه تحت القائمة الجانبية');
  refreshTasks();
}
async function refreshTasks() {
  const tasks = await api().task_list();
  $('#tasks').innerHTML = tasks.map((t) => `<div class="task"><div>${esc(t.label)}</div>
    ${t.done ? `<div style="color:${t.error ? 'var(--danger)' : 'var(--ok)'}">${esc(t.error || '✓ ' + (typeof t.result === 'number' ? `${t.result} ملف` : 'تم'))}</div>` : `<div class="bar"><i style="width:${Math.round(t.progress * 100)}%"></i></div>`}</div>`).join('');
  for (const t of tasks) {
    if (t.done && !refreshTasks.seen.has(t.id)) {
      refreshTasks.seen.add(t.id);
      if (t.error && t.error.startsWith('🔒') && t.key) {
        const pw = await ask('كلمة سر الملف', '', '', true);
        if (pw) { await api().tool('extract', t.key, { password: pw }); toast('أفك الضغط…'); }
      }
      if (!t.error && t.tool !== 'prepare') toast(`✓ ${t.label}`);
      if ($('#view-library').classList.contains('active')) loadLibrary();
    }
  }
}
refreshTasks.seen = new Set();

function compressDialog(item) {
  modal(`<h3>ضغط الفيديو</h3><div class="list">
    ${[[16, '💬 واتساب (أقل من ١٦ ميقا)'], [25, '🎮 ديسكورد (٢٥ ميقا)'], [50, '✈ تيليجرام / إيميل (٥٠ ميقا)']].map(([mb, t]) => `<button class="ghost" data-mb="${mb}">${t}</button>`).join('')}
    <div class="row"><input id="mb" type="number" min="2" value="100" style="width:100px"> ميقا <button class="ghost" id="custom">حجم ثاني</button></div></div>`, (el) => {
    $$('[data-mb]', el).forEach((b) => (b.onclick = () => { closeModal(); runTool('compress', item, { mb: Number(b.dataset.mb) }); }));
    $('#custom', el).onclick = () => { closeModal(); runTool('compress', item, { mb: Number($('#mb', el).value) }); };
  });
}
function gifDialog(item, start) {
  modal(`<h3>صنع GIF</h3>
    <label class="row">من الثانية <input id="g-start" type="number" step="0.5" value="${start.toFixed(1)}" style="width:90px"></label>
    <label class="row">المدة <input id="g-len" type="number" step="0.5" min="0.5" max="15" value="3" style="width:90px"> ثانية</label>
    <label class="row">النعومة <select id="g-fps"><option value="8">خفيفة</option><option value="12" selected>ناعمة</option><option value="20">سلسة</option></select>
    المقاس <select id="g-w"><option value="320">صغير</option><option value="480" selected>وسط</option><option value="720">كبير</option></select></label>
    <div class="row" style="justify-content:flex-end;margin-top:12px"><button class="ghost" id="g-no">إلغاء</button><button class="primary" id="g-ok">سوّه</button></div>`, (el) => {
    $('#g-no', el).onclick = closeModal;
    $('#g-ok', el).onclick = () => {
      closeModal();
      runTool('gif', item, { start: Number($('#g-start', el).value), seconds: Number($('#g-len', el).value), fps: Number($('#g-fps', el).value), width: Number($('#g-w', el).value) });
    };
  });
}
async function moveDialog(item) {
  const folders = await api().folders();
  modal(`<h3>نقل إلى</h3><div class="list">${folders.map((f) => `<button class="ghost" data-f="${esc(f)}">📁 ${esc(f || 'الملفات')}</button>`).join('')}</div>`, (el) => {
    $$('[data-f]', el).forEach((b) => (b.onclick = async () => { closeModal(); await api().move(item.key, b.dataset.f); loadLibrary(); }));
  });
}
async function ipaDialog(item) {
  modal('<div class="empty">أقرأ التطبيق…</div>');
  const info = await api().ipa(item.key);
  if (info.failed) return modal(`<h3>ملف IPA</h3><p>${esc(info.error)}</p>`);
  modal(`<div class="row">${info.icon ? `<img src="/file/${encodeURI(info.icon.replace(/\\/g, '/'))}" style="width:64px;height:64px;border-radius:14px">` : '📱'}
    <div><h3 style="margin:0">${esc(info.name)}</h3><div class="hint">${esc(info.bundle)} · ${esc(info.version)} ${info.min_ios ? '· iOS ' + esc(info.min_ios) + '+' : ''}</div></div></div>
    <p class="hint">على الكمبيوتر تقدر تثبّته على جوالك ببرنامج Sideloadly أو AltServer أو 3uTools.</p>
    <div class="row wrap"><button class="primary" id="i-open">افتح بالبرنامج الافتراضي</button><button class="ghost" id="i-folder">📂 إظهار في المجلد</button>
    <button class="ghost" id="i-side">تحميل Sideloadly</button></div>`, (el) => {
    $('#i-open', el).onclick = () => api().open_external(item.key);
    $('#i-folder', el).onclick = () => api().open_folder(item.key);
    $('#i-side', el).onclick = () => api().open_url('https://sideloadly.io');
  });
}
async function addToPlaylist(item) {
  const lists = await api().playlists();
  modal(`<h3>أضف لقائمة تشغيل</h3><div class="list"><button class="ghost" id="pl-new">+ قائمة جديدة</button>
    ${lists.map((p) => `<button class="ghost" data-id="${p.id}">🎶 ${esc(p.name)} (${p.count})</button>`).join('')}</div>`, (el) => {
    $$('[data-id]', el).forEach((b) => (b.onclick = async () => { await api().playlist_add(b.dataset.id, item.key); closeModal(); toast('انضاف ✓'); }));
    $('#pl-new', el).onclick = async () => {
      const name = await ask('اسم القائمة');
      if (name) { const id = await api().playlist_create(name); await api().playlist_add(id, item.key); toast('انضاف ✓'); loadLibrary(); }
    };
  });
}
async function playlistDialog(id) {
  const p = await api().playlist(id);
  modal(`<div class="row between"><h3 style="margin:0">🎶 ${esc(p.name)}</h3><div class="row"><button class="primary small" id="pl-play">▶ تشغيل</button><button class="ghost small" id="pl-shuffle">🔀 عشوائي</button><button class="ghost small" id="pl-del">🗑</button></div></div>
    <div class="list" style="margin-top:12px">${p.items.map((i, n) => `<div class="row sub-item"><b>${n + 1}</b><span class="grow">${esc(i.title)}</span>
      <button class="icon-btn" data-up="${n}">▲</button><button class="icon-btn" data-rm="${n}">✕</button></div>`).join('') || '<div class="hint">فاضية</div>'}</div>`, (el) => {
    const keys = p.items.map((i) => i.key);
    $('#pl-play', el).onclick = () => { closeModal(); if (p.items.length) play(p.items[0], p.items); };
    $('#pl-shuffle', el).onclick = () => { closeModal(); const s = [...p.items].sort(() => Math.random() - 0.5); if (s.length) play(s[0], s); };
    $('#pl-del', el).onclick = async () => { if (confirm('تحذف القائمة؟')) { await api().playlist_delete(id); closeModal(); loadLibrary(); } };
    $$('[data-rm]', el).forEach((b) => (b.onclick = async () => { keys.splice(Number(b.dataset.rm), 1); await api().playlist_set(id, keys); playlistDialog(id); }));
    $$('[data-up]', el).forEach((b) => (b.onclick = async () => { const n = Number(b.dataset.up); if (n) { [keys[n - 1], keys[n]] = [keys[n], keys[n - 1]]; await api().playlist_set(id, keys); playlistDialog(id); } }));
  });
}
async function montageDialog() {
  const all = await api().all_items();
  const clips = all.filter((i) => i.kind === 'video'), songs = all.filter((i) => i.kind === 'audio');
  const chosen = [];
  const render = () => {
    modal(`<h3>مونتاج سريع</h3><div class="hint">اختار المقاطع بالترتيب</div>
      <div style="max-height:36vh;overflow:auto;margin:10px 0">${clips.map((c) => `<label class="row" style="padding:5px 0"><input type="checkbox" data-k="${esc(c.key)}" ${chosen.includes(c.key) ? 'checked' : ''}> ${chosen.includes(c.key) ? `<b>${chosen.indexOf(c.key) + 1}</b>` : ''} ${esc(c.title)}</label>`).join('')}</div>
      <label class="row">🎵 موسيقى <select id="m-music"><option value="">بدون</option>${songs.map((s) => `<option value="${esc(s.key)}">${esc(s.title)}</option>`).join('')}</select></label>
      <label class="row">صوت الموسيقى <input id="m-mv" type="range" min="0" max="1" step="0.05" value="0.6"> صوت المقاطع <input id="m-cv" type="range" min="0" max="1" step="0.05" value="1"></label>
      <div class="row" style="justify-content:flex-end;margin-top:12px"><button class="ghost" id="m-no">إلغاء</button><button class="primary" id="m-ok" ${chosen.length ? '' : 'disabled'}>سوّ الفيديو (${chosen.length})</button></div>`, (el) => {
      $$('input[type=checkbox]', el).forEach((c) => (c.onchange = () => { const k = c.dataset.k; const i = chosen.indexOf(k); if (i >= 0) chosen.splice(i, 1); else chosen.push(k); render(); }));
      $('#m-no', el).onclick = closeModal;
      $('#m-ok', el).onclick = async () => {
        const music = $('#m-music', el).value || null;
        await api().montage(chosen, music, Number($('#m-mv', el).value), Number($('#m-cv', el).value));
        closeModal();
        toast('بدأ المونتاج…');
        refreshTasks();
      };
    });
  };
  render();
}

/* ------------------------------------------------------------------ player */
const video = $('#video');
let subDelay = 0, recordTimer = null, playToken = 0, nextTimer = null;
const naturalSort = (list) => [...list].sort((a, b) => (a.title || '').localeCompare(b.title || '', undefined, { numeric: true }));
async function playSource(details, token) {
  // formats WebView2 can't play (HEVC, AC-3 sound, AVI…) are quietly converted to a cached MP4 first
  if (details.kind === 'video' && await api().needs_prepare(details.key)) {
    const box = $('#p-prep');
    box.hidden = false;
    box.innerHTML = '<div>أجهّز الفيديو للتشغيل… <b id="prep-pct">0%</b><div class="hint">يتحول مرة وحدة وبعدها يفتح علطول</div></div>';
    const id = await api().prepare_playback(details.key);
    for (;;) {
      await new Promise((r) => setTimeout(r, 700));
      if (token !== playToken) return null;
      const t = await api().task_status(id);
      if (!t) break;
      $('#prep-pct').textContent = `${Math.round((t.progress || 0) * 100)}%`;
      if (t.done) {
        box.hidden = true;
        if (t.error) return `/media/${enc(details.key)}`;
        details.subtitle_tracks = (await api().item_details(details.key)).subtitle_tracks;
        return `/file/${encodeURI(t.result)}`;
      }
    }
    box.hidden = true;
  }
  return `/media/${enc(details.key)}`;
}
async function play(item, queue) {
  const token = ++playToken;
  clearInterval(nextTimer);
  $('#p-next-card').hidden = true;
  $('#p-skip').hidden = true;
  const details = await api().item_details(item.key);
  if (token !== playToken) return;
  if (!queue || queue.length <= 1) {      // one file: the next episodes are its folder neighbours
    try {
      const data = await api().library(details.folder_key || '');
      const same = naturalSort((data.items || []).filter((i) => i.kind === details.kind));
      queue = same.some((i) => i.key === details.key) ? same : [item];
    } catch { queue = [item]; }
  }
  state.current = details;
  state.queue = queue;
  $('#player').hidden = false;
  $('#p-title').textContent = details.title;
  $('#p-error').hidden = true;
  video.hidden = false;
  subDelay = 0;
  $('#p-delay').textContent = '0.0';
  $$('track', video).forEach((t) => t.remove());
  video.removeAttribute('src');
  const src = await playSource(details, token);
  if (!src || token !== playToken) return;
  video.src = src;
  const tracks = details.subtitle_tracks || [];
  $('#p-subs').innerHTML = `<option value="">بدون ترجمة</option>` + tracks.map((t, i) => `<option value="${i}">${esc(langName(t.lang))}</option>`).join('');
  tracks.forEach((t, i) => {
    const track = document.createElement('track');
    track.kind = 'subtitles';
    track.label = langName(t.lang);
    track.srclang = t.lang.slice(0, 2);
    track.src = `/sub/${enc(t.file)}`;
    track.dataset.i = i;
    video.appendChild(track);
  });
  $('#p-subs2').innerHTML = `<option value="">+ ترجمة ثانية</option>` + tracks.map((t, i) => `<option value="${i}">${esc(langName(t.lang))}</option>`).join('');
  $('#p-subs2').hidden = tracks.length < 2;
  if (tracks.length) {
    $('#p-subs').value = '0';
    const ar = tracks.findIndex((t) => t.lang.startsWith('ar')), en = tracks.findIndex((t) => t.lang.startsWith('en'));
    if (state.settings.dual_subs && ar >= 0 && en >= 0) { $('#p-subs').value = String(ar); $('#p-subs2').value = String(en); }
    setTimeout(() => selectSub($('#p-subs').value), 300);
  }
  video.playbackRate = Number($('#p-speed').value);
  video.onloadedmetadata = () => { if (details.position > 5 && details.position < video.duration - 10) video.currentTime = details.position; };
  video.onerror = () => {
    $('#p-error').hidden = false;
    $('#p-error').innerHTML = `<div>المشغّل المدمج ما يقدر يشغّل هالصيغة (${esc(details.ext.toUpperCase())}).<br><br>
      <button class="primary" id="pe-mp4">حوّله إلى MP4</button> <button class="ghost" id="pe-ext">↗ شغّله ببرنامج ثاني (VLC…)</button></div>`;
    $('#pe-mp4').onclick = () => { closePlayer(); runTool('mp4', details); };
    $('#pe-ext').onclick = () => api().open_external(details.key);
  };
  video.onended = () => {
    api().record(details.key, video.duration, video.duration);
    const i = state.queue.findIndex((q) => q.key === details.key);
    if (i >= 0 && i + 1 < state.queue.length) nextCard(state.queue[i + 1]);
  };
  video.ontimeupdate = () => {
    const early = video.currentTime > 3 && video.currentTime < 240 && video.duration > 600 && details.kind === 'video';
    if ($('#p-skip').hidden === early) $('#p-skip').hidden = !early;
  };
  clearInterval(recordTimer);
  recordTimer = setInterval(() => { if (!video.paused && video.duration) api().record(details.key, video.currentTime, video.duration); }, 5000);
}
function nextCard(item) {
  const card = $('#p-next-card');
  let left = 8;
  const draw = () => {
    card.innerHTML = `<div class="hint">الحلقة التالية بعد ${left} ث</div><b>${esc(item.title)}</b>
      <div class="row"><button class="primary small" id="nx-go">شغّلها الحين</button><button class="ghost small" id="nx-no">إلغاء</button></div>`;
    $('#nx-go').onclick = () => { clearInterval(nextTimer); card.hidden = true; play(item, state.queue); };
    $('#nx-no').onclick = () => { clearInterval(nextTimer); card.hidden = true; };
  };
  card.hidden = false;
  draw();
  clearInterval(nextTimer);
  nextTimer = setInterval(() => { left -= 1; if (left <= 0) { clearInterval(nextTimer); card.hidden = true; play(item, state.queue); } else draw(); }, 1000);
}
function step(delta) {
  const i = state.queue.findIndex((q) => q.key === state.current?.key);
  const target = state.queue[i + delta];
  if (i >= 0 && target) play(target, state.queue); else toast(delta > 0 ? 'هذا آخر واحد' : 'هذا أول واحد');
}
$('#p-next').onclick = () => step(1);
$('#p-prev').onclick = () => step(-1);
$('#p-skip').onclick = () => { video.currentTime += 85; $('#p-skip').hidden = true; };
$('#p-sticker').onclick = () => { if (!state.current) return toast('هذا تشغيل مباشر، حمّله أول'); video.pause(); stickerDialog(state.current, video.currentTime); };
$('#p-audio').onclick = () => { if (!state.current) return toast('هذا تشغيل مباشر، حمّله أول'); video.pause(); audioStudioDialog(state.current); };
function langName(code) {
  const base = code.split(/[-_.]/)[0];
  const names = { ar: 'العربية', en: 'English', fr: 'Français', tr: 'Türkçe', es: 'Español', de: 'Deutsch', sub: 'ترجمة' };
  const number = (code.match(/[-_.](\d+)$/) || [])[1];
  return (names[base] || code) + (code.includes('ai') ? ' (ذكاء اصطناعي)' : '') + (number ? ` ${number}` : '');
}
/* two subtitle tracks at once: both are read by the page and drawn as two lines (the browser can't stack tracks) */
function renderDual() {
  const lines = ['#p-subs', '#p-subs2'].map((sel) => {
    const v = $(sel).value;
    const track = v === '' ? null : video.textTracks[Number(v)];
    return track ? [...(track.activeCues || [])].map((c) => c.text.replace(/<[^>]+>/g, '')).join('\n') : '';
  });
  $('#p-dual-a').textContent = lines[0];
  $('#p-dual-b').textContent = lines[1];
}
video.textTracks.addEventListener('addtrack', (e) => e.track.addEventListener('cuechange', renderDual));
$('#p-subs2').onchange = () => selectSub($('#p-subs').value);
function selectSub(value) {
  const second = $('#p-subs2').hidden ? '' : $('#p-subs2').value;
  if (second !== '' && value !== '') {
    [...video.textTracks].forEach((t, i) => (t.mode = String(i) === value || String(i) === second ? 'hidden' : 'disabled'));
    $('#p-dual').hidden = false;
    renderDual();
    return;
  }
  $('#p-dual').hidden = true;
  [...video.textTracks].forEach((t, i) => (t.mode = String(i) === value ? 'showing' : 'disabled'));
}
$('#p-subs').onchange = (e) => selectSub(e.target.value);
$('#p-speed').onchange = (e) => (video.playbackRate = Number(e.target.value));
function shiftSubs(delta) {
  subDelay = Math.round((subDelay + delta) * 10) / 10;
  $('#p-delay').textContent = (subDelay > 0 ? '+' : '') + subDelay.toFixed(1);
  const current = $('#p-subs').value;
  $$('track', video).forEach((t) => {
    const src = t.src.split('?')[0];
    const fresh = document.createElement('track');
    Object.assign(fresh, { kind: 'subtitles', label: t.label, srclang: t.srclang, src: `${src}?delay=${subDelay}` });
    fresh.dataset.i = t.dataset.i;
    t.replaceWith(fresh);
  });
  setTimeout(() => selectSub(current), 200);
}
$('#p-delay-minus').onclick = () => shiftSubs(-0.5);
$('#p-delay-plus').onclick = () => shiftSubs(0.5);
$('#p-pip').onclick = () => video.requestPictureInPicture?.().catch(() => toast('صورة داخل صورة مو متاحة'));
$('#p-external').onclick = () => { if (!state.current) return toast('هذا تشغيل مباشر، حمّله أول'); video.pause(); api().open_external(state.current.key); };
$('#p-frame').onclick = () => state.current ? runTool('frame', state.current, { at: video.currentTime }) : toast('هذا تشغيل مباشر، حمّله أول');
$('#p-gif').onclick = () => { if (!state.current) return toast('هذا تشغيل مباشر، حمّله أول'); video.pause(); gifDialog(state.current, video.currentTime); };
function closePlayer() {
  if (state.current && video.duration) api().record(state.current.key, video.currentTime, video.duration);
  clearInterval(recordTimer);
  clearInterval(nextTimer);
  playToken += 1;
  $('#p-next-card').hidden = true;
  $('#p-prep').hidden = true;
  video.pause();
  video.removeAttribute('src');
  video.load();
  $('#player').hidden = true;
  if ($('#view-library').classList.contains('active')) loadLibrary();
}
$('#p-close').onclick = closePlayer;
document.addEventListener('keydown', (e) => {
  if ($('#player').hidden || ['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) return;
  if (e.key === 'Escape') closePlayer();
  if (e.key === ' ') { e.preventDefault(); video.paused ? video.play() : video.pause(); }
  if (e.key === 'ArrowRight') video.currentTime += 10;
  if (e.key === 'ArrowLeft') video.currentTime -= 10;
  if (e.key === 'ArrowUp') video.volume = Math.min(1, video.volume + 0.1);
  if (e.key === 'ArrowDown') video.volume = Math.max(0, video.volume - 0.1);
  if (e.key.toLowerCase() === 'f') document.fullscreenElement ? document.exitFullscreen() : video.requestFullscreen();
  if (e.key.toLowerCase() === 'm') video.muted = !video.muted;
  if (e.key.toLowerCase() === 'n') step(1);
  if (e.key.toLowerCase() === 'p') step(-1);
});
function applySubtitleLook(s) {
  const root = document.documentElement.style;
  const colors = { white: '#fff', yellow: '#ffe14d', cyan: '#5ee7ff', lime: '#7dff8a', pink: '#ff8ad8' };
  root.setProperty('--sub-color', colors[s.sub_color] || '#fff');
  root.setProperty('--sub-bg', s.sub_background === 'box' ? 'rgba(0,0,0,.62)' : s.sub_background === 'soft' ? 'rgba(0,0,0,.3)' : 'transparent');
  root.setProperty('--sub-shadow', s.sub_background === 'outline' ? '0 0 3px #000, 0 0 3px #000, 1px 1px 2px #000' : 'none');
  root.setProperty('--sub-scale', s.sub_scale || 1);
  root.setProperty('--sub-font', { rounded: '"Segoe UI Variable Display", "Segoe UI", sans-serif', serif: 'Georgia, "Times New Roman", serif', arabic: 'Tahoma, "Segoe UI", sans-serif' }[s.sub_font] || 'var(--font)');
  root.setProperty('--sub-weight', s.sub_font === 'bold' ? 800 : 600);
}

/* ------------------------------------------------------------------ assistant */
async function loadChat() {
  const history = await api().chat_history();
  $('#chat').innerHTML = history.length ? history.map(renderMsg).join('') : `<div class="empty">✦ هلا! اسألني أي شي، أو أرسل ملف وألخصه لك.<br>جرّب: «لخّص لي هالفيديو https://youtu.be/…» أو «نزّل آخر ٣ مقاطع من القناة الفلانية صوت»</div>`;
  $('#chat').scrollTop = 1e9;
}
function renderMsg(m) {
  const acts = (m.actions || []).map((a) => `✓ ${esc(a)}`).join('<br>');
  const srcs = (m.sources || []).slice(0, 5).map((s) => `<a href="#" data-url="${esc(s.url)}">🌐 ${esc(s.title)}</a>`).join('');
  const text = esc(m.text).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
  return `<div class="msg ${m.role}${m.error ? ' error' : ''}">${m.file ? `📎 ${esc(m.file)}<br>` : ''}${text}${acts ? `<div class="acts">${acts}</div>` : ''}${srcs ? `<div class="srcs">${srcs}</div>` : ''}</div>`;
}
function showAttachment() {
  const chip = $('#attach-chip');
  chip.hidden = !state.attachment;
  if (state.attachment) {
    chip.innerHTML = `📎 ${esc(state.attachment.split(/[\\/]/).pop())} <a href="#" id="unattach">✕</a>`;
    $('#unattach').onclick = (e) => { e.preventDefault(); state.attachment = null; showAttachment(); };
  }
}
$('#attach').onclick = async () => {
  const files = await api().choose_files(false);
  if (files.length) { state.attachment = files[0]; showAttachment(); }
};
$('#send').onclick = async () => {
  const text = $('#ask').value.trim();
  if (!text && !state.attachment) return;
  let attachment = state.attachment;
  if (attachment && !/^[a-zA-Z]:|^\//.test(attachment)) attachment = (state.settings.download_dir + '/' + attachment);
  $('#ask').value = '';
  state.attachment = null;
  showAttachment();
  $('#chat').insertAdjacentHTML('beforeend', renderMsg({ role: 'user', text, file: attachment ? attachment.split(/[\\/]/).pop() : null }) + '<div class="typing" id="typing">✦ يفكر…</div>');
  $('#chat').scrollTop = 1e9;
  $('#send').disabled = true;
  const reply = await api().ask(text, attachment);
  $('#send').disabled = false;
  $('#typing')?.remove();
  $('#chat').insertAdjacentHTML('beforeend', renderMsg(reply));
  $('#chat').scrollTop = 1e9;
  if (state.viaVoice) { state.viaVoice = false; if (reply.text && !reply.error) speak(reply.text); }
  if (reply.play) { const item = await api().item_details(reply.play); play(item, [item]); }
  refreshJobs();
};
$('#ask').addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('#send').click(); } });
$('#chat-clear').onclick = async () => { await api().chat_clear(); loadChat(); };
document.addEventListener('click', (e) => {
  const a = e.target.closest('[data-url]');
  if (a) { e.preventDefault(); api().open_url(a.dataset.url); }
});

/* ------------------------------------------------------------------ follow */
async function loadSubs() {
  const subs = await api().subscriptions();
  $('#subs').innerHTML = subs.map((s) => `<div class="sub-item"><div style="font-size:22px">${s.mode === 'audio' ? '🎵' : '🎬'}</div>
    <div class="body"><b>${esc(s.title)}</b><div class="hint">${s.last_check ? 'آخر فحص ' + new Date(s.last_check * 1000).toLocaleString('ar') : ''}${s.new ? ` · ${s.new} جديد` : ''}</div>
    ${s.error ? `<div class="hint" style="color:var(--warn)">${esc(s.error)}</div>` : ''}</div>
    <button class="icon-btn" data-u="${esc(s.url)}">✕</button></div>`).join('') || '<div class="empty">ما تتابع شي للحين</div>';
  $$('#subs [data-u]').forEach((b) => (b.onclick = async () => { await api().unsubscribe(b.dataset.u); loadSubs(); }));
}
$('#sub-add').onclick = async () => {
  const url = $('#sub-url').value.trim();
  if (!url) return;
  $('#sub-msg').textContent = 'أقرأ القناة…';
  const error = await api().subscribe(url, $('#sub-mode').value, $('#sub-latest').value);
  $('#sub-msg').textContent = error || 'تمام ✓ أي مقطع جديد ينزل لحاله';
  if (!error) { $('#sub-url').value = ''; loadSubs(); refreshJobs(); }
};
$('#sub-check').onclick = async () => {
  toast('أشيك على القنوات…');
  const n = await api().check_subscriptions();
  toast(n ? `بدأ تحميل ${n} مقطع جديد` : 'ما فيه جديد');
  loadSubs();
  refreshJobs();
};

/* ------------------------------------------------------------------ settings */
async function loadMemory() {
  const m = await api().memory_list();
  $('#mem-list').innerHTML = m.items.map((i) => `<div class="row between"><span>${esc(i.text)}</span><button class="icon-btn" data-m="${i.id}">✕</button></div>`).join('')
    || '<div class="hint">ما فيه شي محفوظ للحين</div>';
  $$('#mem-list [data-m]').forEach((b) => (b.onclick = async () => { await api().memory_delete(b.dataset.m); loadMemory(); }));
}
$('#mem-clear').onclick = async () => { if (confirm('تمسح كل اللي يتذكره المساعد؟')) { await api().memory_clear(); loadMemory(); } };
async function loadSettings() {
  const s = await api().get_settings();
  state.settings = s;
  $('#s-folder').textContent = s.download_dir;
  $('#s-key').value = s.gemini_key || '';
  $('#s-addon').value = s.stremio_addon || '';
  $$('[data-s]').forEach((el) => {
    const v = s[el.dataset.s];
    if (el.type === 'checkbox') el.checked = !!v; else el.value = String(v);
  });
  const info = await api().info();
  const e = info.engine || {};
  $('#s-engine').textContent = `yt-dlp ${e.yt_dlp || '?'} · gallery-dl ${e.gallery_dl || '?'} · ffmpeg ${info.ffmpeg ? '✓' : '✗'} · deno ${info.deno ? '✓' : '✗'}`;
  $('#version').textContent = `نزّل للكمبيوتر ${info.version}`;
}
$$('[data-s]').forEach((el) => el.addEventListener('change', async () => {
  let v = el.type === 'checkbox' ? el.checked : el.value;
  if (el.hasAttribute('data-num')) v = Number(v);
  const s = await api().save_settings({ [el.dataset.s]: v });
  state.settings = { ...state.settings, ...s };
  applySubtitleLook(state.settings);
}));
$('#s-choose').onclick = async () => { const f = await api().choose_folder(); if (f) loadSettings(); };
$('#s-key').addEventListener('change', () => api().save_settings({ gemini_key: $('#s-key').value.trim() }));
$('#s-addon').addEventListener('change', () => api().save_settings({ stremio_addon: $('#s-addon').value.trim() }));
$('#s-key-test').onclick = async () => {
  await api().save_settings({ gemini_key: $('#s-key').value.trim() });
  $('#s-key-msg').textContent = 'أختبر…';
  const r = await api().test_key($('#s-key').value);
  $('#s-key-msg').textContent = r.ok ? `✅ شغّال، يقدر يستخدم ${r.count} نموذج` : '❌ Google ما قبل المفتاح';
};
$('#s-addon-test').onclick = async () => {
  await api().save_settings({ stremio_addon: $('#s-addon').value.trim() });
  const name = await api().stremio_test();
  $('#s-addon-msg').textContent = name ? `✅ شغّالة: ${name}` : '❌ ما قدرت أفتح الإضافة (لازم الرابط كامل وينتهي بـ manifest.json)';
};
$('#s-update').onclick = async () => {
  toast('أحدّث المحرك…', 6000);
  const r = await api().update_engine();
  toast(r && !r.failed ? '✓ المحرك محدّث' : 'ما قدرت أحدّث المحرك');
  loadSettings();
};

/* ------------------------------------------------------------------ clipboard & background */
async function checkClipboard() {
  const link = await api().clipboard_link();
  if (!link || document.hidden === false && $('#link') === document.activeElement) return;
  const banner = $('#clip-banner');
  banner.innerHTML = `📋 <span>نسخت رابط:</span><span class="url">${esc(link)}</span>
    <button class="primary small" data-m="video">🎬 فيديو</button><button class="ghost small" data-m="audio">🎵 صوت</button><button class="ghost small" data-m="x">✕</button>`;
  banner.hidden = false;
  clearTimeout(checkClipboard.timer);
  checkClipboard.timer = setTimeout(() => (banner.hidden = true), 15000);
  $$('[data-m]', banner).forEach((b) => (b.onclick = async () => {
    banner.hidden = true;
    if (b.dataset.m === 'x') return;
    await api().add(link, b.dataset.m);
    toast('بدأ التحميل ⬇');
    refreshJobs();
  }));
}

window.addEventListener('pywebviewready', async () => {
  initStartupBox();
  const s = await api().get_settings();
  state.settings = s;
  state.mode = s.mode || 'video';
  $$('#mode button').forEach((b) => b.classList.toggle('on', b.dataset.v === state.mode));
  $('#quality').value = s.quality || 'best';
  applySubtitleLook(s);
  loadSettings();
  refreshJobs();
  setInterval(refreshJobs, 800);
  setInterval(refreshTasks, 1000);
  setInterval(checkClipboard, 1500);
  setInterval(async () => { for (const n of await api().notices_pop()) toast(n, 5000); }, 3000);
});

/* outside the app window (tests, --browser): reach the API over HTTP */
setTimeout(() => {
  if (window.pywebview) return;
  window.pywebview = { api: new Proxy({}, { get: (_, name) => (...args) => fetch(`/__api/${name}`, { method: 'POST', body: JSON.stringify(args) }).then((r) => r.json()) }) };
  window.dispatchEvent(new Event('pywebviewready'));
}, 400);


/* ------------------------------------------------------------------ audio studio / stickers */
function audioStudioDialog(item) {
  const row = (id, label, min, max, step, value, unit = '') =>
    `<label><span>${label}</span><input type="range" id="${id}" min="${min}" max="${max}" step="${step}" value="${value}"><output id="${id}-o">${value}${unit}</output></label>`;
  modal(`<h3>🎚 استوديو الصوت</h3><div class="hint">${esc(item.title)}</div><div class="studio">
    <label><span>من ثانية</span><input id="as-start" type="number" min="0" step="0.5" value="0"><span></span></label>
    <label><span>إلى ثانية</span><input id="as-end" type="number" min="0" step="0.5" value="" placeholder="للنهاية"><span></span></label>
    ${row('as-volume', 'مستوى الصوت', 20, 300, 5, 100, '%')}
    ${row('as-speed', 'السرعة', 50, 200, 5, 100, '%')}
    ${row('as-bass', 'الجهير', -10, 10, 1, 0, 'dB')}
    ${row('as-treble', 'الحاد', -10, 10, 1, 0, 'dB')}
    ${row('as-fi', 'تدرّج دخول', 0, 10, 0.5, 0, 'ث')}
    ${row('as-fo', 'تدرّج خروج', 0, 10, 0.5, 0, 'ث')}
    <label class="check"><input type="checkbox" id="as-norm"> وحّد مستوى الصوت (Normalize)</label>
    <label class="check"><input type="checkbox" id="as-rev"> اعكس الصوت</label>
    <label><span>الصيغة</span><select id="as-fmt"><option value="mp3">MP3</option><option value="m4a">M4A</option></select><span></span></label>
    </div><div class="row" style="justify-content:flex-end;margin-top:12px"><button class="ghost" id="as-no">إلغاء</button><button class="primary" id="as-ok">طبّق</button></div>`, (el) => {
    const units = { 'as-volume': '%', 'as-speed': '%', 'as-bass': 'dB', 'as-treble': 'dB', 'as-fi': 'ث', 'as-fo': 'ث' };
    Object.keys(units).forEach((id) => { const r = $('#' + id, el); r.oninput = () => ($('#' + id + '-o', el).textContent = r.value + units[id]); });
    $('#as-no', el).onclick = closeModal;
    $('#as-ok', el).onclick = () => {
      const v = (id) => Number($('#' + id, el).value);
      closeModal();
      runTool('audio_studio', item, {
        start: v('as-start'), end: v('as-end') || 0, volume: v('as-volume') / 100, speed: v('as-speed') / 100,
        bass: v('as-bass'), treble: v('as-treble'), fade_in: v('as-fi'), fade_out: v('as-fo'),
        normalize: $('#as-norm', el).checked, reverse: $('#as-rev', el).checked, fmt: $('#as-fmt', el).value,
      });
    };
  });
}
function stickerDialog(item, start) {
  if (item.kind === 'image') return runTool('sticker', item);
  modal(`<h3>🏷 صنع ملصق (WebP 512×512)</h3>
    <label class="row">النوع <select id="st-type"><option value="0">ثابت (لقطة)</option><option value="4" selected>متحرك</option></select></label>
    <label class="row">من الثانية <input id="st-start" type="number" step="0.5" value="${(start || 0).toFixed(1)}" style="width:90px"></label>
    <label class="row">مدة المتحرك <input id="st-len" type="number" step="0.5" min="1" max="8" value="4" style="width:90px"> ثانية</label>
    <div class="hint">الملصق المتحرك يتقلّل حجمه لين يصير تحت ١ ميقا (يناسب واتساب).</div>
    <div class="row" style="justify-content:flex-end;margin-top:12px"><button class="ghost" id="st-no">إلغاء</button><button class="primary" id="st-ok">سوّه</button></div>`, (el) => {
    $('#st-no', el).onclick = closeModal;
    $('#st-ok', el).onclick = () => {
      closeModal();
      runTool('sticker', item, { start: Number($('#st-start', el).value), seconds: $('#st-type', el).value === '0' ? 0 : Number($('#st-len', el).value) });
    };
  });
}

/* ------------------------------------------------------------------ built-in browser */
const SITES = [['▶️', 'يوتيوب', 'https://www.youtube.com/'], ['📸', 'إنستقرام', 'https://www.instagram.com/'], ['🎵', 'تيك توك', 'https://www.tiktok.com/'],
  ['✖️', 'إكس', 'https://x.com/'], ['👥', 'فيسبوك', 'https://www.facebook.com/'], ['👻', 'سناب', 'https://www.snapchat.com/spotlight'],
  ['💬', 'ريديت', 'https://www.reddit.com/'], ['📌', 'بنترست', 'https://www.pinterest.com/'], ['🧵', 'ثريدز', 'https://www.threads.com/'], ['🎬', 'ستريميو', 'https://web.stremio.com/']];
$('#sites').innerHTML = SITES.map(([i, n, u]) => `<button data-u="${u}"><span class="ic">${i}</span>${n}</button>`).join('');
$$('#sites button').forEach((b) => (b.onclick = () => api().browse(b.dataset.u)));
async function loadBrowserView() {
  const list = await api().browse_history();
  $('#browse-hist').innerHTML = list.slice(0, 12).map((t) => `<div class="hist-row" data-t="${esc(t)}">${esc(t)}</div>`).join('') || '<div class="empty">ما فتحت شي للحين</div>';
  $$('#browse-hist [data-t]').forEach((el) => (el.onclick = () => api().browse(el.dataset.t)));
}
$('#browse-hist-clear').onclick = async () => { await api().browse_history_clear(); loadBrowserView(); };
function playExternal(url, title, audioOnly) {
  $('#player').hidden = false;
  $('#p-title').textContent = title || '';
  $('#p-error').hidden = true;
  $('#p-prep').hidden = true;
  state.current = null; state.queue = [];
  playToken += 1; clearInterval(recordTimer); clearInterval(nextTimer);
  video.onended = video.ontimeupdate = video.onerror = video.onloadedmetadata = null;
  $('#p-skip').hidden = true; $('#p-next-card').hidden = true;
  $$('track', video).forEach((t) => t.remove());
  video.hidden = false;
  video.src = url;
  video.play().catch(() => {});
}
function askAssistant(text) { show('assistant'); $('#ask').value = text; $('#send').click(); }
const browseInput = $('#browse-url');
browseInput.oninput = () => ($('#browse-clear').hidden = !browseInput.value);
browseInput.onfocus = () => browseInput.select();
$('#browse-clear').onclick = () => { browseInput.value = ''; $('#browse-clear').hidden = true; browseInput.focus(); };
$('#browse-go').onclick = async () => { await api().browse(browseInput.value.trim()); browseInput.value = ''; $('#browse-clear').hidden = true; loadBrowserView(); };
browseInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') $('#browse-go').click(); });

/* ------------------------------------------------------------------ voice */
let recorder = null;
const blobToB64 = (blob) => new Promise((resolve) => { const r = new FileReader(); r.onload = () => resolve(String(r.result).split(',')[1]); r.readAsDataURL(blob); });
$('#mic').onclick = async () => {
  if (recorder) return recorder.stop();
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); } catch { return toast('ما قدرت أوصل للمايك. فعّله من خصوصية ويندوز ← الميكروفون', 5000); }
  const chunks = [];
  const rec = (recorder = new MediaRecorder(stream));
  rec.ondataavailable = (e) => chunks.push(e.data);
  rec.onstop = async () => {
    stream.getTracks().forEach((t) => t.stop());
    $('#mic').classList.remove('rec');
    $('#mic').textContent = '🎤';
    recorder = null;
    const blob = new Blob(chunks, { type: rec.mimeType });
    if (blob.size < 1500) return toast('ما سمعت شي');
    toast('أسمعك…');
    const reply = await api().voice(await blobToB64(blob), blob.type);
    if (reply.error) return toast(reply.error, 5000);
    if (!reply.text) return toast('ما فهمت الكلام');
    $('#ask').value = reply.text;
    state.viaVoice = true;
    $('#send').click();
  };
  rec.start();
  $('#mic').classList.add('rec');
  $('#mic').textContent = '⏹';
  setTimeout(() => { if (recorder === rec) rec.stop(); }, 30000);
};
function speak(text) {
  try {
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text.replace(/[*_`#]/g, ''));
    u.lang = /[؀-ۿ]/.test(text) ? 'ar-SA' : 'en-US';
    speechSynthesis.speak(u);
  } catch {}
}


/* ------------------------------------------------------------------ phone <-> PC */
let transferTimer = null;
async function transferDialog(mode, keys) {
  clearInterval(transferTimer);
  if (!mode) {
    return modal(`<h3>📱 جوال ⇄ كمبيوتر</h3><p class="hint">الجوال والكمبيوتر على نفس الواي فاي. يطلع رمز QR تصوّره بكاميرا الجوال (أو بماسح QR داخل تطبيق نزّل على الآيفون).</p>
      <div class="list"><button class="primary" id="tr-recv">📥 استقبل ملفات من الجوال</button>
      <button class="ghost" id="tr-send">📤 أرسل ملفات للجوال</button></div>`, (el) => {
      $('#tr-recv', el).onclick = () => transferDialog('receive');
      $('#tr-send', el).onclick = pickForTransfer;
    });
  }
  modal('<div class="empty">أجهّز…</div>');
  const r = await api().transfer_start(mode, keys || []);
  if (r.error) return modal(`<h3>📱</h3><p>${esc(r.error)}</p>`);
  modal(`<h3>${mode === 'receive' ? '📥 استقبال من الجوال' : '📤 إرسال للجوال'}</h3>
    <div style="display:flex;justify-content:center;margin:10px 0"><div style="background:#fff;border-radius:14px;padding:8px;width:260px;height:260px">${r.qr || ''}</div></div>
    <div class="mono" style="word-break:break-all;text-align:center;direction:ltr">${esc(r.url)}</div>
    <p class="hint">${mode === 'receive' ? 'صوّر الرمز بالجوال، اضغط «اختر ملفات» وهي توصل هنا علطول.' : 'صوّر الرمز بالجوال وحمّل الملفات: ' + esc(r.files.join('، '))}
    <br>أول مرة ويندوز يسألك عن السماح بالشبكة: اختار «السماح» (شبكة خاصة).</p>
    <div id="tr-got"></div>
    <div class="row" style="justify-content:flex-end;margin-top:10px"><button class="ghost" id="tr-stop">إيقاف</button></div>`, (el) => {
    $('#tr-stop', el).onclick = async () => { clearInterval(transferTimer); await api().transfer_stop(); closeModal(); loadLibrary(); };
    let seen = 0;
    transferTimer = setInterval(async () => {
      const st = await api().transfer_status();
      if (!st.active) { clearInterval(transferTimer); return; }
      if (st.received.length !== seen) {
        seen = st.received.length;
        $('#tr-got', el).innerHTML = st.received.map((n) => `<div style="color:var(--ok)">✓ وصل: ${esc(n)}</div>`).join('');
        toast('وصل ملف من الجوال ✓');
      }
    }, 1500);
  });
}
async function pickForTransfer() {
  const items = (await api().all_items()).filter((i) => i.kind !== 'folder').slice(0, 80);
  modal(`<h3>اختار الملفات</h3><div style="max-height:46vh;overflow:auto;margin:10px 0">${items.map((i) =>
    `<label class="row" style="padding:6px 0"><input type="checkbox" value="${esc(i.key)}"> ${esc(i.title)} <span class="hint">${fmtBytes(i.size)}</span></label>`).join('') || '<div class="empty">ما فيه ملفات</div>'}</div>
    <div class="row" style="justify-content:flex-end"><button class="ghost" id="tp-no">إلغاء</button><button class="primary" id="tp-ok">اعرض الرمز</button></div>`, (el) => {
    $('#tp-no', el).onclick = closeModal;
    $('#tp-ok', el).onclick = () => {
      const keys = $$('input:checked', el).map((c) => c.value);
      if (keys.length) transferDialog('send', keys); else toast('اختار ملف');
    };
  });
}
$('#transfer-btn').onclick = () => transferDialog();


/* ------------------------------------------------------------------ text from pictures */
async function ocrDialog(key, at, translate) {
  modal('<div class="empty">أقرأ النص…</div>');
  const r = await api().ocr(key, at, translate);
  if (r.error) return modal(`<h3>📝 النص</h3><p>${esc(r.error)}</p>`);
  modal(`<h3>📝 النص${translate ? ' + الترجمة' : ''}</h3>
    <textarea id="ocr-text" rows="12" style="width:100%">${esc(r.text)}</textarea>
    <div class="row wrap" style="justify-content:flex-end;margin-top:10px">
      <button class="ghost" id="ocr-close">إغلاق</button>
      ${translate ? '' : '<button class="ghost" id="ocr-tr">🌐 ترجمه للعربي</button>'}
      <button class="primary" id="ocr-copy">نسخ</button></div>`, (el) => {
    $('#ocr-close', el).onclick = closeModal;
    $('#ocr-copy', el).onclick = async () => { await navigator.clipboard.writeText($('#ocr-text', el).value); toast('اننسخ ✓'); };
    const tr = $('#ocr-tr', el);
    if (tr) tr.onclick = () => ocrDialog(key, at, true);
  });
}
$('#p-ocr').onclick = () => {
  if (!state.current) return toast('هذا تشغيل مباشر، حمّله أول');
  video.pause();
  ocrDialog(state.current.key, video.currentTime, false);
};


/* ------------------------------------------------------------------ drag & drop (links here, files through the app) */
(() => {
  const zone = $('#dropzone');
  let depth = 0;
  const hasStuff = (e) => [...(e.dataTransfer?.types || [])].some((t) => ['Files', 'text/uri-list', 'text/plain', 'text/x-moz-url'].includes(t));
  document.addEventListener('dragenter', (e) => { if (hasStuff(e)) { depth += 1; zone.hidden = false; } });
  document.addEventListener('dragleave', () => { depth = Math.max(0, depth - 1); if (!depth) zone.hidden = true; });
  document.addEventListener('dragover', (e) => { if (hasStuff(e)) e.preventDefault(); });
  document.addEventListener('drop', async (e) => {
    e.preventDefault();
    depth = 0;
    zone.hidden = true;
    const text = e.dataTransfer.getData('text/uri-list') || e.dataTransfer.getData('text/plain') || '';
    if (/https?:\/\//i.test(text)) {
      const count = await api().add(text, state.mode, $('#quality').value);
      if (count) { toast(count > 1 ? `بدأ تحميل ${count} روابط ⬇` : 'بدأ التحميل ⬇'); show('download'); refreshJobs(); }
      else toast('ما لقيت رابط واضح');
    }
    // dropped files are picked up by the app itself (it knows their paths) and land in the library
  });
})();

/* ------------------------------------------------------------------ start with Windows */
async function initStartupBox() {
  const box = $('#s-startup');
  if (!box) return;
  if (!(await api().startup_available())) { box.disabled = true; box.parentElement.title = 'يشتغل بعد التثبيت (مو بالنسخة التجريبية)'; return; }
  box.checked = !!(await api().get_settings()).run_at_startup;
  box.onchange = async () => { const ok = await api().set_startup(box.checked); if (!ok) { box.checked = false; toast('ما قدرت أفعّلها'); } };
}
