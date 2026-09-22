"""独立可选的 WebView2 原生探针：不启动托盘、不写 widget.json。

以 --output <目录> 运行；关闭贴纸窗口或 Ctrl+C 退出。
本地命令文件 command.txt 接受 wide/compact/snapshot/exit（另有 move-next
跨屏移动）；探针把几何快照 JSON 写进 --output 目录。
"""
import argparse
import datetime as dt
import json
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import webview
from monitor import winchild as wc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    wc.configure_process_dpi()
    state = wc._WindowState()
    state.cfg.update(opacity=.9, passthrough=False, pinned=False, x=160, y=120)
    state.last_widget_data = {
        'generated_at': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'today': {'cost': 3.27, 'requests': 42},
        'plan': {'tier': 'lite', 'window5h_limit': 2000, 'week_limit': 10000},
        'window5h': {'credits': 1780, 'used_pct': 89, 'reset_eta_min': 246},
        'thisweek': {'credits': 2200, 'used_pct': 22},
        'week': [{'d': '09-%02d' % (14+i), 'full': '2026-09-%02d' % (14+i),
                  'req': i+4, 'cost': value}
                 for i, value in enumerate([.82, 1.63, 2.5, .6, 4.8, 2.76, 3.27])],
        'last_error': None,
    }

    class Pipe:
        def send(self, msg):
            with (args.output / 'messages.jsonl').open('a', encoding='utf-8') as f:
                f.write(json.dumps(msg, ensure_ascii=False) + '\n')
            if isinstance(msg, list) and msg[:1] == ['WIDGET_SETTINGS']:
                state.cfg.update(msg[1])
                state.widget.evaluate_js('renderWidgetSettings(%s)' % json.dumps(state.cfg))

    wc._CHILD_PIPE[0] = Pipe()
    state.window = webview.create_window('GAUGE test host', html='<html></html>', hidden=True)
    state.widget = webview.create_window(
        'GAUGE native smoke', wc.WIDGET_HTML_PATH.as_uri(),
        width=380, height=460, x=160, y=120, frameless=True,
        transparent=True, background_color='#0A0A0C', resizable=False,
        easy_drag=False, on_top=True, js_api=wc.WidgetApi(state))
    wc._bind_widget_events(state, state.widget)

    def snapshot():
        widget = state.widget
        if widget is None or not state.widget_ready.wait(10):
            return
        data = widget.evaluate_js('JSON.stringify({width:innerWidth,height:innerHeight,dpr:devicePixelRatio,body:document.body.getBoundingClientRect().toJSON(),shell:document.getElementById("widget").getBoundingClientRect().toJSON(),opacity:getComputedStyle(document.getElementById("widget")).opacity,background:getComputedStyle(document.getElementById("widget")).backgroundColor})')
        data = json.loads(data) if isinstance(data, str) else data
        form = widget.native
        data['native'] = {
            'width': form.Width, 'height': form.Height,
            'clientWidth': form.ClientSize.Width, 'clientHeight': form.ClientSize.Height,
            'webviewWidth': form.webview.Width, 'webviewHeight': form.webview.Height,
            'x': form.Left, 'y': form.Top, 'layout': state.layout,
            'backColor': str(form.BackColor),
        }
        (args.output / ('geometry-' + state.layout + '.json')).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

    def control():
        state.widget_ready.wait(20)
        time.sleep(1)
        snapshot()
        command = args.output / 'command.txt'
        while not state.exiting:
            time.sleep(.25)
            if not command.exists():
                continue
            action = command.read_text(encoding='utf-8-sig').strip()
            command.unlink()
            if action == 'exit':
                state.exiting = True
                if state.widget is not None:
                    state.widget.destroy()
                state.window.destroy()
                return
            if action == 'move-next':
                def move(native):
                    from System.Drawing import Point
                    from System.Windows.Forms import Screen
                    screens = list(Screen.AllScreens)
                    current = Screen.FromControl(native).DeviceName
                    index = next(i for i, screen in enumerate(screens)
                                 if screen.DeviceName == current)
                    area = screens[(index + 1) % len(screens)].WorkingArea
                    native.Location = Point(area.Left + 48, area.Top + 48)
                wc._on_widget_ui(state.widget, move)
                time.sleep(1.5)
            if action in ('wide', 'compact') and action != state.layout:
                wc.WidgetApi(state).toggle_layout()
                state.widget_ready.wait(20)
                time.sleep(1)
            if action in ('wide', 'compact', 'snapshot', 'move-next'):
                snapshot()

    webview.start(control)


if __name__ == '__main__':
    main()
