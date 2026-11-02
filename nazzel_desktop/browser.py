"""A small built-in browser window: pages open here, ads/pop-ups are blocked, and one click sends the page to the downloader."""
import json
import urllib.parse

# Hides the usual ad containers, blocks pop-ups and "open in new tab" tricks, and adds a toolbar.
GUARD_JS = r"""
(function () {
  if (window.__nazzel) return; window.__nazzel = true;
  try { window.open = function () { return null; }; } catch (e) {}
  const css = document.createElement('style');
  css.textContent = `
    [id*="google_ads"],[class*="adsbygoogle"],ins.adsbygoogle,iframe[src*="doubleclick"],iframe[src*="googlesyndication"],
    iframe[src*="adservice"],iframe[src*="popads"],iframe[src*="exoclick"],[id^="ad-"],[id^="ads-"],[class^="ad-"],
    [class*=" ad-"],[class*="advert"],[id*="advert"],[class*="popup-ad"],[class*="sponsor"],a[href*="bet365"],
    a[href*="1xbet"],a[href*="melbet"],a[href*="adclick"],div[style*="z-index: 2147483647"] { display: none !important; }`;
  document.documentElement.appendChild(css);
  const here = location.hostname.replace(/^www\./, '');
  document.addEventListener('click', function (e) {
    const a = e.target.closest && e.target.closest('a[href]');
    if (!a) return;
    let u; try { u = new URL(a.href, location.href); } catch (x) { return; }
    const same = u.hostname.replace(/^www\./, '').endsWith(here) || here.endsWith(u.hostname.replace(/^www\./, ''));
    if (a.target === '_blank' || a.hasAttribute('download')) {
      if (a.hasAttribute('download')) return;
      e.preventDefault(); e.stopPropagation();
      if (same || e.isTrusted) location.href = u.href;
    }
  }, true);
  // a script that jumps to another site right after load (redirect ad) is stopped
  let armed = true; setTimeout(function () { armed = false; }, 2500);
  window.addEventListener('beforeunload', function (e) {
    if (armed && !window.__nazzelClicked) { e.preventDefault(); e.returnValue = ''; }
  });
  document.addEventListener('pointerdown', function () { window.__nazzelClicked = true; }, true);

  const bar = document.createElement('div');
  bar.style.cssText = 'position:fixed;top:10px;left:10px;z-index:2147483646;display:flex;gap:6px;direction:rtl;font:600 13px Segoe UI,Tahoma,sans-serif';
  const mk = function (label, fn, primary) {
    const b = document.createElement('button');
    b.textContent = label; b.onclick = fn;
    b.style.cssText = 'border:0;border-radius:999px;padding:8px 14px;cursor:pointer;box-shadow:0 2px 10px #0008;' +
      (primary ? 'background:#2ed3b7;color:#04201b' : 'background:#1b2326;color:#fff');
    return b;
  };
  const say = function (t) { const old = bar.lastChild; const s = document.createElement('span');
    s.textContent = t; s.style.cssText = 'background:#1b2326;color:#2ed3b7;border-radius:999px;padding:8px 12px';
    bar.appendChild(s); setTimeout(function () { s.remove(); }, 2500); };
  bar.appendChild(mk('⬇ نزّل هالصفحة', function () {
    window.pywebview.api.grab(location.href).then(function (n) { say(n ? 'انضاف للتحميلات ✓' : 'ما لقيت رابط'); });
  }, true));
  bar.appendChild(mk('🎞 نزّل الفيديو', function () {
    const v = document.querySelector('video'); const src = v && (v.currentSrc || v.src);
    const target = src && !src.startsWith('blob:') ? src : location.href;
    window.pywebview.api.grab(target).then(function () { say('انضاف للتحميلات ✓'); });
  }));
  bar.appendChild(mk('◀', function () { history.back(); }));
  bar.appendChild(mk('▶', function () { history.forward(); }));
  document.documentElement.appendChild(bar);
})();
"""


def normalize(text):
    """What was typed in the address bar → a URL (or a Google search)."""
    text = (text or '').strip()
    if not text:
        return ''
    if '://' in text:
        return text
    if ' ' not in text and '.' in text:
        return 'https://' + text
    return 'https://www.google.com/search?q=' + urllib.parse.quote(text)


class BrowserApi:
    """What the pages inside the browser window can call."""

    def __init__(self, main):
        self.main = main

    def grab(self, url):
        return self.main.add(url)


def open_window(main, text):
    import webview
    url = normalize(text)
    if not url:
        return False
    window = webview.create_window('نزّل — متصفح', url, js_api=BrowserApi(main), width=1180, height=800, text_select=True,
                                   background_color='#0B0F10')
    window.events.loaded += lambda: _inject(window)
    return True


def _inject(window):
    try:
        window.evaluate_js(GUARD_JS)
    except Exception:
        pass
