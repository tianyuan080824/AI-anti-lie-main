"""威胁情报：`DomainIntelProvider` 接口。

- 本地黑名单（`local`）始终启用，不联网，不会失败；
- 外部情报只在配置了密钥时启用，查询时只发可注册域名（契约第 2 节），不带路径和参数；
- 外部情报调用失败（超时、断网、返回错误）抛 `IntelError`，分析器据此带上 `intel_unavailable`。

verdict 只用两种取值：`malicious`（判为恶意）和 `unknown`（没有恶意结论）。不输出 `clean` 一类的词，
因为「情报里没有」不等于安全。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import httpx

from app.analyzers.link.data import LinkData, host_in
from app.config import Settings

MALICIOUS = "malicious"
UNKNOWN = "unknown"


class IntelError(Exception):
    pass


class DomainIntelProvider(ABC):
    #: 报告里 `intel.provider` 的取值
    name: str
    #: True：调用第三方服务（失败时带 intel_unavailable）；False：本地数据
    external: bool = True

    @abstractmethod
    async def query(self, domain: str, timeout: float) -> str:
        """查询一个可注册域名，返回 verdict。失败时抛 IntelError。"""

    async def aclose(self) -> None:
        pass


class LocalBlacklist(DomainIntelProvider):
    name = "local"
    external = False

    def __init__(self, data: LinkData) -> None:
        self.data = data

    async def query(self, domain: str, timeout: float) -> str:
        return MALICIOUS if host_in(domain, self.data.blacklist) else UNKNOWN


class ThreatBookProvider(DomainIntelProvider):
    """微步在线域名信誉。接口地址和返回格式按公开文档编写，没有用真实密钥验证过（见 NOTES.md）。"""

    name = "threatbook"
    URL = "https://api.threatbook.cn/v3/scene/dns"

    def __init__(self, api_key: str, client: httpx.AsyncClient) -> None:
        self.api_key = api_key
        self.client = client

    async def query(self, domain: str, timeout: float) -> str:
        try:
            response = await self.client.post(
                self.URL,
                data={"apikey": self.api_key, "resource": domain},
                timeout=httpx.Timeout(timeout, connect=min(1.5, timeout)),
            )
            if response.status_code != 200:
                raise IntelError(f"http_{response.status_code}")
            doc = response.json()
        except httpx.HTTPError as exc:
            raise IntelError(type(exc).__name__) from exc
        except ValueError as exc:
            raise IntelError("bad_json") from exc
        if not isinstance(doc, dict) or doc.get("response_code") != 0:
            raise IntelError("response_code")
        data = doc.get("data") or {}
        info = (data.get("domains") or {}).get(domain) if isinstance(data, dict) else None
        if isinstance(info, dict) and info.get("is_malicious") is True:
            return MALICIOUS
        return UNKNOWN


def configured_external(settings: Settings) -> dict[str, bool]:
    """各外部情报是否配置了密钥。"""
    return {
        "threatbook": bool(settings.intel_threatbook_api_key),
        "qianxin": bool(settings.intel_qianxin_api_key),
        "tencent": bool(settings.intel_tencent_secret_id and settings.intel_tencent_secret_key),
    }


#: 配置项已声明、但本版本还没有接入的外部情报（配置了也不会调用，自检会提示）
NOT_IMPLEMENTED = ("qianxin", "tencent")


def build_external(settings: Settings, client: httpx.AsyncClient) -> list[DomainIntelProvider]:
    providers: list[DomainIntelProvider] = []
    if settings.intel_threatbook_api_key:
        providers.append(ThreatBookProvider(settings.intel_threatbook_api_key, client))
    return providers
