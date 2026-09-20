# -*- coding: utf-8 -*-
"""singleinst — 单实例锁（127.0.0.1:59321，批次3a 自 app.py 拆分）。

持有锁的一侧监听 SHOW 指令并转发给窗口子进程；后启动的一侧 bind 失败，
向已运行实例发送 SHOW 唤出窗口后静默退出。SHOW 的转发回调经参数注入，
使本模块只依赖 appenv（包内依赖无环）。
"""
import socket
import threading
from typing import Callable, Optional

from monitor.appenv import SINGLE_PORT, log


def bind_single_instance(on_show: Callable[[str], bool]) -> Optional[socket.socket]:
    """绑定 127.0.0.1:59321 并监听 SHOW 指令；绑定失败返回 None。

    on_show：SHOW 指令的转发回调（托盘侧传入 traycore.child_send），
    返回值语义与其一致（是否发送成功）。
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        srv.bind(("127.0.0.1", SINGLE_PORT))
        srv.listen(4)
    except OSError:
        try:
            srv.close()
        except OSError:
            pass
        return None

    def accept_loop() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            try:
                data = conn.recv(16)
                conn.close()
            except OSError:
                continue
            if data.strip().upper().startswith(b"SHOW"):
                log("收到 SHOW 指令 → 转发给窗口子进程")
                on_show("SHOW")

    threading.Thread(target=accept_loop, daemon=True, name="single-instance").start()
    return srv


def notify_existing_instance() -> bool:
    """单实例协议：通知已运行实例唤出窗口。返回是否发送成功。"""
    try:
        with socket.create_connection(("127.0.0.1", SINGLE_PORT), timeout=2) as c:
            c.sendall(b"SHOW")
        return True
    except OSError:
        return False
