"""文件摘要（F19 · datasheet 获取与入库）

spec §6、§6.2、§5.5：某颗料首次进入案子时按需下载解析（不预取）；版本 = 内容哈希；新版本入库保留旧版并触发失效传播；
下载只走出站白名单。
- 来源接口 get(mpn) → (url, 内容字节, 内容类型)：
  - FixtureSource：tests/fixtures/datasheets/<料号>.<标签>.json（合成样例）；use(mpn, 标签) 切换当前版本（模拟改版）。
  - HttpSource：按料号查 URL 表，逐跳用 httpx 发请求（不自动跟随重定向），每一跳过 F11 出站策略 check_url，重定向链过
    check_redirects，响应过 check_download（只收 PDF、大小上限）；客户端可注入（测试用模拟传输，本 Feature 不发真实请求）。
- Ingestor.ensure(mpn, day)：没有任何版本时获取并入库；refresh(mpn, day)：重新获取比对版本（提交前重校验、A5 注入用）。
  入库：解析 → 存版本与页 → 写 datasheet_ingested 事件；已有旧版本时对外部引用 datasheet:<料号> 写失效（F06 传播）。
  原始文件可落 blob_dir（data/datasheets/，不进 git）。
依赖：datasheets.store / parse、permissions.egress、session.log、httpx。被 datasheets.server、F12 门控（refresh）、F04（A5）使用。
"""
from __future__ import annotations  # 类型注解延迟求值

from pathlib import Path  # 文件
from urllib.parse import urljoin  # 重定向地址

import httpx  # HTTP 客户端

from datasheets.parse import FIXTURE_TYPE  # 样例内容类型
from datasheets.store import DatasheetStore, latest  # 存储
from session.log import Session, sha256  # 事件日志与哈希


class FetchRejected(RuntimeError):
    """出站检查不通过或下载失败（原因写明哪条规则）。"""


class FixtureSource:
    """固定样例来源：目录下 <料号>.<标签>.json；缺省用字典序第一个标签。"""

    def __init__(self, directory: Path):
        self.dir = Path(directory)  # 样例目录
        self.current: dict[str, str] = {}  # 料号 → 当前标签

    def use(self, mpn: str, tag: str) -> None:
        """切换某料当前的样例版本（模拟厂商改版）。"""
        self.current[mpn] = tag  # 记下

    def get(self, mpn: str) -> tuple[str, bytes, str]:
        files = sorted(self.dir.glob(f"{mpn}.*.json"))  # 该料的样例
        if not files:  # 没有
            raise FetchRejected(f"没有 {mpn} 的 datasheet 样例")  # 报错
        tag = self.current.get(mpn)  # 指定的标签
        path = next((f for f in files if f.name == f"{mpn}.{tag}.json"), files[0]) if tag else files[0]  # 选文件
        return f"fixture://{path.name}", path.read_bytes(), FIXTURE_TYPE  # URL、内容、类型


class HttpSource:
    """真实下载来源（本 Feature 只用模拟传输测试）：每一跳都过出站白名单。"""

    def __init__(self, urls: dict[str, str], policy, client: httpx.Client | None = None):
        self.urls, self.policy = urls, policy  # 料号 → URL；出站策略
        self.client = client or httpx.Client(follow_redirects=False, timeout=30)  # 不自动跟随重定向

    def get(self, mpn: str) -> tuple[str, bytes, str]:
        url = self.urls.get(mpn)  # 起始 URL
        if url is None:  # 没登记
            raise FetchRejected(f"没有 {mpn} 的下载地址")  # 报错
        chain = [url]  # 跳转链
        while True:  # 逐跳
            reason = self.policy.check_redirects(chain)  # 整条链过白名单（含本跳）
            if reason:  # 不通过
                raise FetchRejected(reason)  # 拒绝
            resp = self.client.get(chain[-1])  # 发请求（只到已检查过的地址）
            if resp.is_redirect:  # 重定向
                chain.append(urljoin(chain[-1], resp.headers["location"]))  # 下一跳
                continue  # 先检查再请求
            if resp.status_code != 200:  # 失败
                raise FetchRejected(f"下载失败：HTTP {resp.status_code}")  # 报错
            reason = self.policy.check_download(resp.headers.get("content-type"), len(resp.content))  # 类型与大小
            if reason:  # 不通过
                raise FetchRejected(reason)  # 拒绝
            return chain[-1], resp.content, resp.headers.get("content-type", "")  # 最终地址、内容、类型


class Ingestor:
    # 功能：见文件摘要。依赖：来源、解析器、存储、事件日志、可选落盘目录。完成：同内容不重复入库。
    def __init__(self, source, parser, store: DatasheetStore, session: Session, *, blob_dir: Path | None = None):
        self.source, self.parser, self.store, self.session = source, parser, store, session  # 依赖
        self.blob_dir = Path(blob_dir) if blob_dir else None  # 原始文件目录（不进 git）

    def ensure(self, mpn: str, *, day: int) -> dict:
        """按需：已有版本直接返回最新，没有才获取入库。"""
        return latest(self.store, mpn) or self.refresh(mpn, day=day)  # 最新或新入库

    def refresh(self, mpn: str, *, day: int) -> dict:
        """重新获取并比对版本；新版本入库（保留旧版）并触发失效传播。返回最新版本元数据。"""
        url, data, ctype = self.source.get(mpn)  # 获取
        version = sha256(data.hex())  # 版本 = 内容哈希
        old = latest(self.store, mpn)  # 现有最新
        if old is not None and old["version"] == version:  # 内容没变
            return old  # 不动
        doc = self.parser.parse(data, ctype)  # 解析
        meta = {"mpn": mpn, "version": version, "source_url": url, "title": doc["title"],  # 元数据
                "toc": doc["toc"], "fetched_day": day}  # 目录与取得日
        self.store.put_version(meta, doc["pages"])  # 入库（旧版保留）
        if self.blob_dir is not None:  # 落盘
            (self.blob_dir / mpn).mkdir(parents=True, exist_ok=True)  # 目录
            (self.blob_dir / mpn / f"{version[:16]}.bin").write_bytes(data)  # 原始文件
        self.session.append("datasheet_ingested", day=day, actor="datasheets",  # 入库事件
                            payload={"mpn": mpn, "version": version, "pages": len(doc["pages"]),  # 料号、版本、页数
                                     "source_url": url, "previous": old and old["version"]},  # 来源与旧版本
                            entity_keys={"mpn": [mpn], "document": [f"datasheet:{mpn}"]})  # 实体键
        if old is not None:  # 改版：依赖旧版本的 Artifact 失效（F06）
            self.session.invalidate_external(f"datasheet:{mpn}", version, day=day, actor="datasheets")  # 传播
        return meta  # 新版本
