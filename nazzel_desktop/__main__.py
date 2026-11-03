"""Nazzel for Windows: starts the engine, the local server and the window."""
import os
import sys


def main():
    if '--selftest' in sys.argv:
        # before anything opens the real settings: the test uses its own folders
        from .selftest import run
        sys.exit(run())

    from . import tray
    incoming = tray.parse_protocol(sys.argv)          # a link from the browser extension
    if '--browser' not in sys.argv:
        if incoming and tray.send_to_running_copy(*incoming):
            return                  # the open copy took the link
        if not incoming and tray.show_running_copy():
            return                  # Nazzel is already running (maybe in the tray): its window was shown

    from . import core
    core.boot()
    from . import server
    from .api import Api
    from .core.extras import SUBSCRIPTIONS
    from .core.settings import resource, settings

    if '--browser' in sys.argv:
        # no window: open the interface in the default browser (development)
        import time
        import webbrowser
        api = Api()
        url = server.start(dev_api=api, port=8790)
        webbrowser.open(url)
        while True:
            time.sleep(3600)

    import webview
    background = '--background' in sys.argv
    api = Api()
    url = server.start()
    import secrets
    token = secrets.token_urlsafe(16)
    server.Handler.add_token = token
    tray.remember_port(url, token)
    tray.register_protocol()
    window = webview.create_window('نزّل', url, js_api=api, width=1280, height=820, min_size=(960, 640),
                                   background_color='#0B0F10', text_select=True, hidden=background)
    api.window = window
    api.base_url = url
    SUBSCRIPTIONS.start_timer(api.notify)

    state = {'quit': False, 'icon': None}

    def show_window():
        try:
            window.show()
            window.restore()
        except Exception:
            pass

    server.Handler.on_show = show_window

    def add_from_browser(link, mode):
        if api.add(link, mode if mode in ('video', 'audio', 'photos') else 'video'):
            api.notify('انضاف للتحميلات من المتصفح ⬇')
            show_window()
            try:
                window.evaluate_js("show('download'); refreshJobs()")
            except Exception:
                pass

    server.Handler.on_add = add_from_browser
    if incoming:
        import threading
        threading.Timer(3.0, lambda: add_from_browser(*incoming)).start()

    def quit_app(icon=None):
        state['quit'] = True
        try:
            if icon:
                icon.stop()
            window.destroy()
        except Exception:
            pass
        os._exit(0)

    def on_closing():
        # "close to the tray" (Settings): the window hides, downloads and followed channels keep going
        if not state['quit'] and state['icon'] is not None and settings().get('close_to_tray'):
            window.hide()
            return False
        return True

    def ready(win):
        # runs once the window exists: the tray icon, the drag & drop of files
        if settings().get('close_to_tray') or settings().get('run_at_startup') or background:
            state['icon'] = tray.start_tray(win, lambda: SUBSCRIPTIONS.check_all(), quit_app)
        try:
            win.events.closing += on_closing
        except Exception:
            pass
        try:
            from webview.dom import DOMEventHandler

            def on_drop(event):
                files = (event.get('dataTransfer') or {}).get('files') or []
                paths = [f.get('pywebviewFullPath') for f in files if f.get('pywebviewFullPath')]
                if paths:
                    api.import_dropped(paths)

            win.dom.document.events.drop += DOMEventHandler(on_drop, True, False)
        except Exception:
            pass          # an older pywebview: only dropped links work

    icon = resource('ui', 'icon.ico')
    webview.start(ready, window, debug='--debug' in sys.argv, icon=icon if os.path.exists(icon) else None,
                  private_mode=False, storage_path=os.path.join(os.environ.get('LOCALAPPDATA', '.'), 'Nazzel', 'webview'))


if __name__ == '__main__':
    main()
