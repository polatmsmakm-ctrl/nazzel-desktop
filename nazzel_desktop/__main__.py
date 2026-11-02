"""Nazzel for Windows: starts the engine, the local server and the window."""
import os
import sys


def main():
    if '--selftest' in sys.argv:
        # before anything opens the real settings: the test uses its own folders
        from .selftest import run
        sys.exit(run())

    from . import core
    core.boot()
    from . import server
    from .api import Api
    from .core.extras import SUBSCRIPTIONS
    from .core.settings import resource

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
    api = Api()
    url = server.start()
    window = webview.create_window('نزّل', url, js_api=api, width=1280, height=820, min_size=(960, 640),
                                   background_color='#0B0F10', text_select=True)
    api.window = window
    SUBSCRIPTIONS.start_timer(api.notify)
    icon = resource('ui', 'icon.ico')
    webview.start(debug='--debug' in sys.argv, icon=icon if os.path.exists(icon) else None,
                  private_mode=False, storage_path=os.path.join(os.environ.get('LOCALAPPDATA', '.'), 'Nazzel', 'webview'))


if __name__ == '__main__':
    main()
