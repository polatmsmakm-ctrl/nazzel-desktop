/* Injected into every page of the Nazzel browser window: ad guard + the docked toolbar (like the phone app). */
(function () {
  if (window.__nazzel) return;
  window.__nazzel = true;
  const API = () => window.pywebview && window.pywebview.api;
  const HOST = location.hostname.replace(/^www\./, '');
  const IS_HOME = location.pathname.endsWith('browser-home.html');
  const AD_HOSTS = ['propellerads.com', 'propellerclick.com', 'onclkds.com', 'onclickads.net', 'onclasrv.com', 'oclasrv.com',
    'popads.net', 'popcash.net', 'adsterra.com', 'highperformanceformat.com', 'profitablegatecpm.com', 'effectivegatecpm.com',
    'highcpmgate.com', 'exoclick.com', 'exosrv.com', 'exdynsrv.com', 'juicyads.com', 'adcash.com', 'clickadu.com',
    'hilltopads.net', 'realsrv.com', 'monetag.com', 'pemsrv.com', 'tsyndicate.com', 'trafficstars.com', 'popunder.net',
    'adskeeper.com', 'a-ads.com', 'bidvertiser.com', 'ad-maven.com', 'admaven.com', 'pushance.com', 'richpush.co',
    'evadav.com', 'galaksion.com', 'rollerads.com', 'doubleclick.net', 'googleadservices.com', 'googlesyndication.com'];
  const isAdHost = (h) => AD_HOSTS.some((a) => h === a || h.endsWith('.' + a));
  const siteOf = (h) => { const p = h.replace(/^www\./, '').split('.'); return p.length > 2 ? p.slice(-(p[p.length - 1].length === 2 && ['co', 'com', 'net', 'org'].includes(p[p.length - 2]) ? 3 : 2)).join('.') : p.join('.'); };
  let blocked = 0;
  let onBlocked = () => {};

  /* ---------- helpers (no innerHTML: some sites enforce Trusted Types) ---------- */
  const sheet = (css, root) => { try { const s = new CSSStyleSheet(); s.replaceSync(css); root.adoptedStyleSheets = [...root.adoptedStyleSheets, s]; } catch (e) {} };
  const h = (tag, props, kids) => {
    const el = document.createElement(tag);
    Object.assign(el, props || {});
    (kids || []).forEach((k) => el.append(k));
    return el;
  };

  /* ---------- ad guard ---------- */
  try { window.open = function () { blocked++; onBlocked(); return null; }; } catch (e) {}
  sheet(`[id*="google_ads"],ins.adsbygoogle,iframe[src*="doubleclick"],iframe[src*="googlesyndication"],iframe[src*="popads"],
    iframe[src*="exoclick"],[class*="popup-ad"],a[href*="1xbet"],a[href*="melbet"],a[href*="adclick"],
    #masthead-ad,ytd-ad-slot-renderer,ytd-promoted-sparkles-web-renderer,ytd-display-ad-renderer,ytd-in-feed-ad-layout-renderer,
    .ytp-ad-overlay-container,.ytp-ad-image-overlay,div[style*="z-index: 2147483647"]{display:none!important}`, document);

  document.addEventListener('click', (e) => {
    const a = e.target.closest && e.target.closest('a[href]');
    if (!a || a.hasAttribute('download')) return;
    let u; try { u = new URL(a.href, location.href); } catch (x) { return; }
    if (!/^https?:$/.test(u.protocol)) return;
    if (isAdHost(u.hostname)) { e.preventDefault(); e.stopPropagation(); blocked++; onBlocked(); return; }
    if (a.target === '_blank') { e.preventDefault(); e.stopPropagation(); location.href = u.href; }   // stay in this window
  }, true);

  // a page that jumps to another site by itself (no tap) right after loading is a redirect ad
  let tapped = false, loadedAt = Date.now();
  document.addEventListener('pointerdown', () => { tapped = true; setTimeout(() => (tapped = false), 1500); }, true);
  document.addEventListener('keydown', () => { tapped = true; setTimeout(() => (tapped = false), 1500); }, true);
  window.addEventListener('beforeunload', (e) => {
    if (!tapped && Date.now() - loadedAt < 4000 && document.readyState !== 'loading' && !IS_HOME) {
      try { e.preventDefault(); e.returnValue = ''; } catch (x) {}
    }
  });
  // injected ad scripts / frames from known ad networks are removed
  new MutationObserver((list) => {
    for (const m of list) for (const n of m.addedNodes) {
      if (n.nodeType !== 1) continue;
      const src = n.src || '';
      if (/^(SCRIPT|IFRAME)$/.test(n.tagName) && src) { try { if (isAdHost(new URL(src, location.href).hostname)) { n.remove(); blocked++; onBlocked(); } } catch (x) {} }
    }
  }).observe(document.documentElement, { childList: true, subtree: true });

  // YouTube: skip video ads, hide sponsored cards
  if (/(^|\.)youtube\.com$/.test(location.hostname)) {
    setInterval(() => {
      const player = document.querySelector('.html5-video-player');
      const video = document.querySelector('video.html5-main-video');
      if (player && video && player.classList.contains('ad-showing')) {
        const skip = document.querySelector('.ytp-ad-skip-button, .ytp-ad-skip-button-modern, .ytp-skip-ad-button');
        if (skip) skip.click();
        else if (isFinite(video.duration) && video.duration > 0) { video.muted = true; video.currentTime = video.duration - 0.1; }
        blocked++; onBlocked();
      }
    }, 400);
  }
  // X / Facebook / Instagram: sponsored posts
  const SPONSORED = /^(ad|promoted|sponsored|إعلان|ممول|برعاية|ممولة|مُموَّل)$/i;
  if (/(x|twitter|facebook|instagram|threads)\.com$/.test(location.hostname)) {
    setInterval(() => {
      document.querySelectorAll('article:not([data-nz]),div[role="article"]:not([data-nz])').forEach((post) => {
        post.dataset.nz = '1';
        for (const s of post.querySelectorAll('span,a')) {
          if (s.children.length === 0 && SPONSORED.test((s.textContent || '').trim())) { post.style.display = 'none'; blocked++; onBlocked(); break; }
        }
      });
    }, 1200);
  }

  /* ---------- finding downloadable things on the page ---------- */
  const FILE_RE = /\.(mp4|m4v|webm|mkv|mov|m3u8|mp3|m4a|flac|wav|ipa|iso|zip|rar|7z|apk|exe|msi|pdf|epub)(\?|#|$)/i;
  const KIND = { mp4: 'video', m4v: 'video', webm: 'video', mkv: 'video', mov: 'video', m3u8: 'video', mp3: 'audio', m4a: 'audio', flac: 'audio', wav: 'audio' };
  function detect() {
    const found = new Map();
    const add = (url, label) => {
      try { url = new URL(url, location.href).href; } catch (e) { return; }
      if (!/^https?:/.test(url) || found.has(url)) return;
      const ext = (url.split('?')[0].split('.').pop() || '').toLowerCase();
      found.set(url, { url, label: label || decodeURIComponent(url.split('?')[0].split('/').pop() || url).slice(0, 60), ext, kind: KIND[ext] || 'file' });
    };
    document.querySelectorAll('video[src],video source[src],audio[src],audio source[src]').forEach((v) => { const s = v.src; if (s && !s.startsWith('blob:')) add(s); });
    document.querySelectorAll('a[href]').forEach((a) => { if (FILE_RE.test(a.href)) add(a.href, (a.textContent || '').trim().slice(0, 60)); });
    const og = document.querySelector('meta[property="og:video"],meta[property="og:video:url"]');
    if (og && og.content) add(og.content);
    return [...found.values()].slice(0, 12);
  }
  const isMediaPage = () => /(youtube\.com\/(watch|shorts)|youtu\.be\/|tiktok\.com\/.+\/video|instagram\.com\/(p|reel|tv)\/|x\.com\/.+\/status|twitter\.com\/.+\/status|facebook\.com\/.+(videos|watch|reel)|vimeo\.com\/\d|twitch\.tv|dailymotion\.com\/video|reddit\.com\/r\/.+\/comments|snapchat\.com\/spotlight|pinterest\.[a-z.]+\/pin\/)/i.test(location.href) || !!document.querySelector('video');

  /* ---------- docked toolbar ---------- */
  const BAR_H = 46;
  const host = h('div');
  host.id = 'nazzel-bar';
  const root = host.attachShadow({ mode: 'open' });
  sheet(`
    :host { all: initial; }
    .bar { position: fixed; top: 0; left: 0; right: 0; height: ${BAR_H}px; z-index: 2147483647; display: flex; gap: 6px; align-items: center;
      padding: 0 8px; background: #0f1416; border-bottom: 1px solid #ffffff22; direction: rtl; font: 600 13px "Segoe UI", Tahoma, sans-serif; color: #fff; box-sizing: border-box; }
    button { border: 0; border-radius: 10px; min-width: 34px; height: 32px; padding: 0 10px; cursor: pointer; background: #1d2629; color: #fff; font: inherit; }
    button:hover { background: #28343a; } button[disabled] { opacity: .35; cursor: default; }
    button.p { background: #2ed3b7; color: #04201b; }
    .addr { flex: 1; position: relative; display: flex; }
    input { width: 100%; height: 32px; border-radius: 10px; border: 1px solid #ffffff22; background: #070b0c; color: #fff; padding: 0 12px 0 34px; font: inherit; outline: none; box-sizing: border-box; direction: ltr; text-align: left; }
    input:focus { border-color: #2ed3b7; }
    .x { position: absolute; left: 4px; top: 4px; width: 24px; min-width: 24px; height: 24px; padding: 0; border-radius: 50%; background: #ffffff22; }
    .badge { background: #e5484d; border-radius: 999px; padding: 1px 6px; margin-inline-start: 4px; font-size: 11px; }
    .menu { position: fixed; top: ${BAR_H + 4}px; right: 8px; width: 300px; max-height: 70vh; overflow: auto; background: #0f1416; border: 1px solid #ffffff2a; border-radius: 14px;
      padding: 6px; direction: rtl; box-shadow: 0 10px 30px #000a; font: 600 13px "Segoe UI", Tahoma, sans-serif; color: #fff; z-index: 2147483647; }
    .menu button { display: block; width: 100%; text-align: right; background: transparent; height: auto; padding: 9px 10px; line-height: 1.4; }
    .menu button:hover { background: #ffffff14; }
    .menu button.p { color: #2ed3b7; }
    .menu hr { border: 0; border-top: 1px solid #ffffff1f; margin: 4px 0; }
    .menu small { display: block; padding: 6px 10px 2px; color: #9fb0b5; font-weight: 500; }
    .toast { position: fixed; top: ${BAR_H + 8}px; left: 50%; transform: translateX(-50%); background: #0f1416f2; border: 1px solid #2ed3b755; color: #2ed3b7;
      border-radius: 999px; padding: 8px 16px; font: 600 13px "Segoe UI", Tahoma, sans-serif; z-index: 2147483647; direction: rtl; }
    .shield { color: #2ed3b7; font-size: 12px; } `, root);

  const back = h('button', { textContent: '◀', title: 'رجوع', onclick: () => history.back() });
  const fwd = h('button', { textContent: '▶', title: 'قدّام', onclick: () => history.forward() });
  const reload = h('button', { textContent: '⟳', title: 'تحديث', onclick: () => location.reload() });
  const home = h('button', { textContent: '🏠', title: 'الرئيسية', onclick: async () => { location.href = await API().home(); } });
  const input = h('input', { placeholder: 'ابحث أو اكتب رابط', spellcheck: false });
  const clear = h('button', { className: 'x', textContent: '✕', title: 'مسح', onclick: () => { input.value = ''; input.focus(); } });
  const shield = h('span', { className: 'shield', title: 'إعلانات انمنعت' });
  const badge = h('span', { className: 'badge', hidden: true });
  const dl = h('button', { className: 'p', textContent: '⬇ تحميل' }, [badge]);
  const bar = h('div', { className: 'bar' }, [home, back, fwd, reload, h('div', { className: 'addr' }, [input, clear]), shield, dl]);
  root.append(bar);
  let menuEl = null, toastEl = null;

  const say = (text) => { toastEl && toastEl.remove(); toastEl = h('div', { className: 'toast', textContent: text }); root.append(toastEl); setTimeout(() => toastEl && toastEl.remove(), 2600); };
  onBlocked = () => { shield.textContent = `🛡 ${blocked}`; };
  const go = async () => {
    const text = input.value.trim();
    if (!text) return;
    const url = await API().go(text);
    if (url) location.href = url;
  };
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); if (e.key === 'Escape') { input.value = IS_HOME ? '' : location.href; input.blur(); } e.stopPropagation(); });
  input.addEventListener('keyup', (e) => e.stopPropagation());
  input.addEventListener('focus', () => input.select());

  const closeMenu = () => { menuEl && menuEl.remove(); menuEl = null; };
  document.addEventListener('pointerdown', (e) => { if (menuEl && !e.composedPath().includes(host)) closeMenu(); }, true);
  const item = (label, fn, cls) => h('button', { textContent: label, className: cls || '', onclick: async () => { closeMenu(); try { await fn(); } catch (e) { say('صار خطأ'); } } });
  const sep = () => h('hr');
  const note = (t) => h('small', { textContent: t });

  dl.onclick = async () => {
    if (menuEl) return closeMenu();
    if (IS_HOME) return say('افتح موقع أول، وبعدين حمّل منه');
    const url = location.href;
    const menu = h('div', { className: 'menu' });
    const media = isMediaPage();
    menu.append(note(media ? 'هالصفحة فيها فيديو' : 'تحميل الصفحة المفتوحة'));
    menu.append(item('▶ شغّل بمشغّل نزّل (بدون إعلانات)', async () => { const r = await API().play(url, false); say(r || 'يفتح في نزّل…'); }, 'p'));
    menu.append(item('🎧 اسمع الصوت بس', async () => { const r = await API().play(url, true); say(r || 'يفتح في نزّل…'); }));
    menu.append(sep());
    menu.append(item('🎬 تحميل الفيديو', async () => { await API().grab(url, 'video'); say('انضاف للتحميلات ⬇'); }));
    menu.append(item('🎵 تحميل الصوت فقط', async () => { await API().grab(url, 'audio'); say('انضاف للتحميلات ⬇'); }));
    menu.append(item('🖼 تحميل الصور / المنشور كامل', async () => { await API().grab(url, 'photos'); say('انضاف للتحميلات ⬇'); }));
    const stremio = /stremio/i.test(location.hostname) ? await API().stremio(url) : null;
    if (stremio) {
      menu.append(sep(), note('ستريميو'));
      menu.append(item(stremio[0] === 'movie' ? '🎬 نزّل الفيلم مع الترجمة العربية' : '📺 نزّل هالحلقة مع الترجمة',
        async () => { const e = await API().stremio_download(stremio[0], stremio[1]); say(e || 'بدأ التحميل مع الترجمة العربية ⬇'); }));
      if (stremio[0] === 'series') menu.append(item('📚 نزّل الموسم كامل', async () => { await API().season(stremio[1]); say('اختر الحلقات في نافذة نزّل'); }));
    }
    const found = detect();
    if (found.length) {
      menu.append(sep(), note('ملفات لقيتها في الصفحة'));
      found.forEach((f) => menu.append(item((f.kind === 'video' ? '🎞 ' : f.kind === 'audio' ? '🎵 ' : '📦 ') + f.label, async () => { await API().grab(f.url, f.kind === 'audio' ? 'audio' : 'video', true); say('انضاف للتحميلات ⬇'); })));
    }
    menu.append(sep(), item('📋 انسخ الرابط', async () => { await navigator.clipboard.writeText(url); say('اننسخ ✓'); }));
    menu.append(item('✨ لخّصه بالمساعد', async () => { await API().summarize(url); say('أرسلته للمساعد في نزّل'); }));
    root.append(menu);
    menuEl = menu;
  };

  /* ---------- keep everything in sync (single-page sites change the address without reloading) ---------- */
  const place = () => {
    if (!document.documentElement.contains(host)) document.documentElement.append(host);
    if (!window.__nzPad) { window.__nzPad = true; sheet(`html{margin-top:${BAR_H}px !important}`, document); }
    if (root.activeElement !== input) input.value = IS_HOME ? '' : location.href;
    clear.hidden = false;
  };
  const start = () => {
    place();
    setInterval(place, 800);
    setInterval(async () => {
      try {
        const n = await API().active_count();
        badge.hidden = !n; badge.textContent = n;
      } catch (e) {}
    }, 2500);
  };
  const ready = () => (API() ? start() : setTimeout(ready, 200));
  ready();
})();
