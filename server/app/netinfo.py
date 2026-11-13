"""本机网络信息（启动信息和自检共用）。只查本机网卡，不向外发包。"""

from __future__ import annotations

import socket

ALL_INTERFACES = ("0.0.0.0", "")
#: 只监听 IPv6 时手机用 IPv4 局域网地址连不上（Windows 上 "::" 默认不接受 IPv4）
IPV6_ANY = "::"


def listens_on_all(host: str) -> bool:
    return host in ALL_INTERFACES


def lan_addresses() -> list[str]:
    """本机的局域网 IPv4 地址（不含 127.x）。"""
    addresses: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # UDP 的 connect 不发包，只为让系统选出出口网卡
            addresses.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except (OSError, UnicodeError):  # 主机名含非 ASCII 字符时 getaddrinfo 抛 UnicodeError
        pass
    return sorted(a for a in addresses if not a.startswith("127.") and a != "0.0.0.0")


def base_url(host: str, port: int) -> str:
    return f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"


IPV6_ONLY_NOTE = "监听的是 IPv6 的 \"::\"，手机用 IPv4 局域网地址可能连不上：热点方式请用 --host 0.0.0.0"


def lan_lines(port: int) -> list[str]:
    urls = ", ".join(f"http://{a}:{port}" for a in lan_addresses())
    return [
        f"  局域网地址  {urls or '未找到（笔记本还没连上热点？）'}",
        "  提醒        正在监听所有网卡：请在系统防火墙里放行这个端口（Windows 第一次启动时弹窗选「允许」，"
        "勾选「专用网络」），并只在自己的热点里使用",
    ]
