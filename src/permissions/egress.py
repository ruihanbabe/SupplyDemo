"""文件摘要（F11 · 出站白名单，防 SSRF）

spec §5.5"出站网络控制"：datasheet 下载只从白名单域名、只允许 HTTPS、禁止内网 IP、重定向到白名单外即拒绝、限制文件大小、只接受 PDF；
各 MCP Server 只能访问自己的上游域名。
- EgressPolicy：一个组件的出站策略。check_url(url) / check_redirects(urls) / check_download(content_type, size) 返回拒绝原因或 None。
  地址检查先看主机名是否为 IP 字面量，再用解析函数取全部解析地址，任何一个落在内网 / 回环 / 链路本地 / 保留 / 组播段即拒绝。
- egress_policy(name, config)：按 config/permissions.yaml egress 段构造。
依赖：标准库 ipaddress / socket / urllib.parse。本 Feature 只提供检查函数（DNS 解析可注入）；真实下载由 F19 调用。
"""
from __future__ import annotations  # 类型注解延迟求值

import ipaddress  # 地址分类
import socket  # 默认 DNS 解析
from collections.abc import Callable  # 解析函数类型
from urllib.parse import urlsplit  # 拆 URL

Resolver = Callable[[str], list[str]]  # 主机名 → 解析出的地址列表


def system_resolver(host: str) -> list[str]:
    """默认解析：系统 DNS（离线测试一律注入假解析，不走这里）。"""
    return sorted({info[4][0] for info in socket.getaddrinfo(host, 443)})  # 全部地址


def _bad_ip(addr: str) -> bool:
    """是否为不允许出站的地址（内网、回环、链路本地、保留、组播、未指定）。"""
    ip = ipaddress.ip_address(addr.split("%")[0])  # 去掉 IPv6 区域标识
    return not ip.is_global or ip.is_multicast  # 非公网即拒绝


# ---------------------------------------------------------------------------
# EgressPolicy：一个组件的出站策略
# 功能：见文件摘要。
# 依赖：域名白名单（含子域）、可选的大小上限 / 内容类型 / 重定向上限、解析函数。
# 完成：拒绝原因写清楚是哪条规则，便于记事件与 Eval 统计。
# ---------------------------------------------------------------------------
class EgressPolicy:
    def __init__(self, domains: list[str], *, max_bytes: int | None = None,
                 content_types: list[str] | None = None, max_redirects: int = 5,
                 resolver: Resolver = system_resolver):
        self.domains = [d.lower().strip(".") for d in domains]  # 白名单域名
        self.max_bytes = max_bytes  # 大小上限
        self.content_types = [c.lower() for c in content_types] if content_types else None  # 接受的类型
        self.max_redirects = max_redirects  # 重定向跳数
        self.resolver = resolver  # 解析函数

    def _domain_ok(self, host: str) -> bool:
        """主机名是否等于或属于白名单域名的子域。"""
        return any(host == d or host.endswith("." + d) for d in self.domains)  # 精确或子域

    def check_url(self, url: str) -> str | None:
        """单个 URL：HTTPS、无内嵌凭证、域名在白名单、解析地址全部是公网。"""
        parts = urlsplit(url)  # 拆分
        if parts.scheme.lower() != "https":  # 只允许 HTTPS
            return f"只允许 HTTPS：{parts.scheme or '（无协议）'}"  # 拒绝
        if parts.username or parts.password:  # URL 内嵌账号密码
            return "URL 不得内嵌账号密码"  # 拒绝
        host = (parts.hostname or "").lower().strip(".")  # 主机名
        if not host:  # 没有主机
            return "URL 缺少主机名"  # 拒绝
        try:  # 主机名本身是 IP 字面量
            ipaddress.ip_address(host)  # 能解析即为 IP
            return f"不允许直接访问 IP 地址：{host}"  # 白名单只有域名
        except ValueError:  # 不是 IP，继续
            pass  # 按域名处理
        if not self._domain_ok(host):  # 不在白名单
            return f"域名不在白名单：{host}"  # 拒绝
        try:  # 解析
            addrs = self.resolver(host)  # 全部地址
        except OSError as exc:  # 解析失败
            return f"域名解析失败：{host}（{exc}）"  # 拒绝
        bad = [a for a in addrs if _bad_ip(a)]  # 内网等地址
        if not addrs or bad:  # 无地址或有内网地址（防 DNS 指向内网）
            return f"域名解析到不允许的地址：{host} → {', '.join(bad) or '无地址'}"  # 拒绝
        return None  # 通过

    def check_redirects(self, urls: list[str]) -> str | None:
        """重定向链：跳数不超上限，每一跳都过 check_url（跳到白名单外即拒绝）。"""
        if len(urls) - 1 > self.max_redirects:  # 跳数（首个 URL 不算跳）
            return f"重定向次数超过 {self.max_redirects}"  # 拒绝
        for u in urls:  # 每一跳
            reason = self.check_url(u)  # 检查
            if reason:  # 不通过
                return f"重定向目标被拒：{reason}"  # 拒绝
        return None  # 通过

    def check_download(self, content_type: str | None, size: int | None) -> str | None:
        """下载内容：类型在允许列表、大小不超上限（未知大小时由调用方边读边查）。"""
        if self.content_types is not None:  # 限类型
            ctype = (content_type or "").split(";")[0].strip().lower()  # 主类型
            if ctype not in self.content_types:  # 不接受
                return f"内容类型不接受：{ctype or '（未知）'}"  # 拒绝
        if self.max_bytes is not None and size is not None and size > self.max_bytes:  # 超大
            return f"文件超过上限 {self.max_bytes} 字节：{size}"  # 拒绝
        return None  # 通过


def egress_policy(name: str, config: dict, resolver: Resolver = system_resolver) -> EgressPolicy:
    """按权限配置 egress 段构造某组件的出站策略；未登记的组件报错（默认拒绝一切）。"""
    spec = config["egress"].get(name)  # 组件策略
    if spec is None:  # 没登记
        raise KeyError(f"出站策略未登记：{name}")  # 报错
    return EgressPolicy(spec["domains"], max_bytes=spec.get("max_bytes"),  # 域名与大小
                        content_types=spec.get("content_types"), max_redirects=spec.get("max_redirects", 5),
                        resolver=resolver)  # 类型、跳数、解析
