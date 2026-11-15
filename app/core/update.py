"""检查有没有新版本。

## 边界

这个模块只做一件事：问 GitHub「最新一个 Release 是哪个版本」，把版本号与下载页
带回来。它**不上报任何东西**——不带设备标识、不带使用数据、不带安装路径，
请求体为空，查询串为空，只有一个 `User-Agent`（GitHub 的 API 强制要求，否则直接
403）。这也是为什么它默认开着不算隐私问题：这是一次纯粹的下行读取。

## 为什么失败要静默

检查更新失败的原因几乎全部与用户无关：公司网络挡了 api.github.com、DNS 抖动、
GitHub 自己 5xx、release 还没发出来。任何一种情况下弹一句「检查更新失败」，
都是在用一件用户不关心也没法处理的事打断他查词。所以失败只留日志，界面上
什么都不发生——**只有在真的发现新版本时才说话**。

## 版本比较

不能用字符串比较：`"1.10.0" < "1.9.0"` 在字典序里成立，而它显然更新。所以拆成
整数元组逐位比，缺的位按 0 补（`1.5` 与 `1.5.0` 相等）。预发布后缀（`-beta.1`）
不进比较——本项目的 tag 目前只有三段数字，为不存在的场景写分支只会多一处
没人走过、也没人验证过的代码。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..version import APP_VERSION, REPO_URL

# 最新 Release 的接口。用 `releases/latest` 而不是 `releases`：后者要翻页、
# 还要自己跳过草稿与预发布，而 GitHub 已经按「最新的正式版」定义了这个端点。
RELEASES_API = f"{REPO_URL.replace('github.com', 'api.github.com')}/releases/latest"

# 检查更新的超时。比查词的 30 秒短得多：这是一件后台的、可以失败的事，
# 让它挂半分钟只会白占一个线程。
CHECK_TIMEOUT = 6.0

_NUMBER_RE = re.compile(r"\d+")


def parse_version(text: str) -> tuple[int, ...]:
    """把 `v1.5.0` / `1.5.0` / `release-1.5.0` 拆成 `(1, 5, 0)`。

    取不到任何数字时返回空元组——调用方据 `bool()` 判断「这个 tag 认不出来」，
    而不是拿 `(0,)` 去参与比较，那会让一个奇怪的 tag 被当成「最新」。
    """
    return tuple(int(part) for part in _NUMBER_RE.findall(text or ""))


def is_newer(latest: str, current: str = APP_VERSION) -> bool:
    """`latest` 是否比 `current` 新。长度不同的版本号按 0 补齐后逐位比。"""
    left, right = parse_version(latest), parse_version(current)
    if not left or not right:
        return False
    width = max(len(left), len(right))
    padded_left = left + (0,) * (width - len(left))
    padded_right = right + (0,) * (width - len(right))
    return padded_left > padded_right


@dataclass(frozen=True)
class ReleaseInfo:
    """一条可展示的「有新版本」信息。"""

    version: str
    url: str
    name: str = ""

    def display(self) -> str:
        """状态栏那一行的文字。版本号带 v，与 Release 页面的写法一致。"""
        return f"有新版本 v{self.version.lstrip('vV')}"


def check(timeout: float = CHECK_TIMEOUT) -> ReleaseInfo | None:
    """问一次 GitHub。**没有新版本、或者任何失败，都返回 None。**

    不抛异常是刻意的：调用方在后台线程里跑它，抛出来的异常会走 worker 的
    失败回调，而那条回调会弹窗——为一次用户没主动要求的检查弹窗，是打扰。
    """
    import httpx

    headers = {
        # GitHub API 要求带 UA，缺失时返回 403（而不是 401）。
        "User-Agent": f"ArcQuill/{APP_VERSION}",
        "Accept": "application/vnd.github+json",
    }
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(RELEASES_API, headers=headers)
            if resp.status_code != 200:
                return None
            data = resp.json()
    except Exception:  # noqa: BLE001 - 网络、解析、超时一律当作「这次没查成」
        return None

    if not isinstance(data, dict):
        return None
    tag = str(data.get("tag_name") or "").strip()
    if not tag or not is_newer(tag):
        return None
    return ReleaseInfo(
        version=tag.lstrip("vV"),
        url=str(data.get("html_url") or REPO_URL),
        name=str(data.get("name") or ""),
    )
