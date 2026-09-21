"""从上游（new-api）读取某 sk 的**当天**实际消耗。

方案（2026-09 改）：用供应商提供的**访问令牌 (PAT)**，不再用账号密码登录。
  1) GET /api/token/search?token=<完整sk>  → 精确反查该 sk 对应的令牌名；
  2) GET /api/log/self/stat?token_name=..&start_timestamp=..&end_timestamp=..
     → 该令牌在指定日期的 quota 汇总；
  3) 平台用量 = quota / quota_per_unit。

为什么换掉账密登录：新版 new-api 的 `GET /api/token/` 会把 key 过一遍
`buildMaskedTokenResponses()` 脱敏（model/token.go 的 GetMaskedKey），
原先「登录后按 key 可见尾部后缀猜令牌名」的做法因此失效。
而 `/api/token/search?token=` 在 model 层是拿**完整 key**（去掉 sk- 前缀）精确匹配的，
配 PAT 用就能稳定定位，不受脱敏影响。

PAT 从 new-api 的「个人设置 → 生成系统访问令牌」获取，走
`Authorization: Bearer <PAT>` 头。用 UpstreamSession 复用连接，一个 base_url 建一次。
"""
from __future__ import annotations

import logging

import httpx

from app.config import settings
from app.source import day_range

logger = logging.getLogger(__name__)
_UID_CACHE: dict = {}     # (base_url, token) -> uid，进程内复用，免得每个 sk 都重探
TIMEOUT = 12.0       # 认证/令牌列表
STAT_TIMEOUT = 60.0  # stat 聚合可能较慢，单独给更长超时


def _json(resp):
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {}


def _items(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("items", "records", "data", "list"):
            if isinstance(data.get(k), list):
                return data[k]
    return []


class UpstreamSession:
    """一个上游(base_url)的会话。用访问令牌(PAT)认证，可复用查询多个 sk。"""

    def __init__(self, base_url: str, access_token: str = "", user_id: str = ""):
        self.base = (base_url or "").rstrip("/")
        self.token = (access_token or "").strip()
        self.hint_uid = (str(user_id).strip() or None)   # 页面填的用户ID，有就直连免探测
        self.client: httpx.Client | None = None
        self.headers: dict = {}
        self.logged_in = False
        self.uid = None
        self.error = ""
        self._name_cache: dict = {}          # sk -> (令牌名, 分组)，同一上游多 sk 时复用

    # ---------------- 认证 ----------------

    def open(self) -> tuple[bool, str]:
        """建立会话并校验访问令牌。成功返回 (True, 'ok')。

        新旧版本的 UserAuth 认证要求不同，这里逐一尝试直到走通：
          A. 只发裸令牌（最新版接受 len(parts)==1 的 Authorization）；
          B. 发裸令牌 + New-Api-User=<uid>（旧版 v0.6~v0.9 强制要这个头，
             且必须等于令牌所属用户的 id）。uid 未知时先探测。
        """
        if not self.base:
            self.error = "缺少上游地址"
            return False, self.error
        if not self.token:
            self.error = "未配置访问令牌（在上游「个人设置 → 生成系统访问令牌」获取）"
            return False, self.error
        try:
            self.client = httpx.Client(
                base_url=self.base, timeout=TIMEOUT, follow_redirects=True,
                headers={"Authorization": self.token},
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=20))

            # A. 先试不带 New-Api-User（最新版）
            r = self.client.get("/api/user/self")
            j = _json(r)
            if j.get("success"):
                self._finish_auth((j.get("data") or {}).get("id"))
                return True, "ok"

            need_user = "New-Api-User" in (j.get("message") or "")
            if not need_user:
                # 也许是要 Bearer 前缀的改分支
                self.client.headers["Authorization"] = f"Bearer {self.token}"
                r = self.client.get("/api/user/self")
                j = _json(r)
                if j.get("success"):
                    self._finish_auth((j.get("data") or {}).get("id"))
                    return True, "ok"
                need_user = "New-Api-User" in (j.get("message") or "")
                self.client.headers["Authorization"] = self.token   # 复位成裸令牌

            # B. 版本要求 New-Api-User —— 优先用页面填的用户ID，其次缓存，最后才探测
            if need_user:
                if self.hint_uid and self.hint_uid.isdigit():
                    r = self.client.get("/api/user/self",
                                        headers={"New-Api-User": self.hint_uid})
                    if _json(r).get("success"):
                        self.client.headers["New-Api-User"] = self.hint_uid
                        self.uid = int(self.hint_uid)
                        self.logged_in = True
                        return True, "ok"
                    # 填错了：明确报错，不要静默去遍历（那会掩盖用户的输入错误）
                    self.error = (f"用户ID {self.hint_uid} 与该令牌不匹配 —— "
                                  "请在上游「个人设置」确认你的用户ID，或清空此项由系统自动探测")
                    return False, self.error
                ck = (self.base, self.token)
                uid = _UID_CACHE.get(ck)
                if uid is not None:
                    # 缓存命中，直接带上验证一次
                    r = self.client.get("/api/user/self",
                                        headers={"New-Api-User": str(uid)})
                    if _json(r).get("success"):
                        self.client.headers["New-Api-User"] = str(uid)
                        self.uid = uid
                        self.logged_in = True
                        return True, "ok"
                    _UID_CACHE.pop(ck, None)      # 失效了重探
                uid, data = self._probe_uid()
                if uid is not None:
                    _UID_CACHE[ck] = uid
                if uid is not None:
                    self.client.headers["New-Api-User"] = str(uid)
                    self.uid = uid
                    self.logged_in = True
                    return True, "ok"
                self.error = ("该版本要求 New-Api-User 头，但未能自动定位令牌对应的用户 ID。"
                              "请确认令牌有效，或在“对账 → 调试”查看详情。")
                return False, self.error

            msg = (j.get("message") or "").strip()
            self.error = (f"访问令牌校验失败（HTTP {r.status_code}）"
                          + (f"：{msg}" if msg else
                             "。请确认填的是上游「个人设置 → 系统访问令牌」，不是 sk- 开头的 API 密钥"))
            return False, self.error
        except Exception as exc:  # noqa: BLE001
            self.error = f"请求失败: {exc}"
            return False, self.error

    def _finish_auth(self, uid) -> None:
        if uid is not None:
            self.client.headers["New-Api-User"] = str(uid)
            self.uid = uid
        self.logged_in = True

    def _probe_uid(self) -> tuple:
        """旧版要求 New-Api-User 且值必须等于令牌用户 id，但拿 id 又得先过认证 ——
        用令牌自身并发遍历候选 id，命中即是。

        令牌无效会立刻在第一次响应里体现（access token invalid），据此提前放弃，
        不会白扫 1000 次。命中后其余请求由 as_completed 自然丢弃。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def probe(uid):
            try:
                r = self.client.get("/api/user/self", headers={"New-Api-User": str(uid)})
            except Exception:  # noqa: BLE001
                return uid, None, "err"
            j = _json(r)
            if j.get("success"):
                return uid, (j.get("data") or {}), "ok"
            msg = (j.get("message") or "").lower()
            bad = ("access token" in msg and "invalid" in msg) or "无效" in msg
            return uid, None, ("badtoken" if bad else "mismatch")

        # 分批探，小 id 优先、命中即停。绝大多数自建站账号 id 都很小，
        # 一两批就能命中；令牌无效则第一批就发现，不会白扫。
        batches = [range(1, 33), range(33, 129), range(129, 513), range(513, 1001)]
        with ThreadPoolExecutor(max_workers=24) as ex:
            for batch in batches:
                results = list(ex.map(probe, batch))
                for uid, data, st in results:
                    if st == "ok":
                        return uid, data
                if any(st == "badtoken" for _, _, st in results):
                    return None, None
        return None, None

    # 兼容旧调用签名    # 兼容旧调用签名：login(username, password) → 忽略账密，直接用 PAT
    def login(self, username=None, password=None) -> tuple[bool, str]:
        return self.open()

    # ---------------- sk → 令牌名 ----------------

    def token_name_for(self, sk: str) -> tuple[str, str]:
        """兼容旧调用：只要令牌名。"""
        name, _group, err = self.token_info_for(sk)
        return name, err

    def token_info_for(self, sk: str) -> tuple[str, str, str]:
        """用完整 sk 反查 (令牌名, 分组, 错误信息)。

        主路径：/api/token/search?token=<sk 去前缀>，model 层用完整 key 精确匹配，
        不受令牌列表脱敏影响（v0.9 起就有这个参数）。
        兜底：万一 search 不可用/不认 token 参数，退回遍历令牌列表按可见后缀匹配 ——
        那是旧方案，脱敏严时会失败，仅作最后尝试。
        """
        sk = (sk or "").strip()
        if not sk:
            return "", "", "sk 为空"
        if sk in self._name_cache:
            n, g = self._name_cache[sk]
            return n, g, ""
        bare = sk[3:] if sk.startswith("sk-") else sk

        for finder in (self._by_search, self._by_list):
            name, group = finder(bare)
            if name:
                self._name_cache[sk] = (name, group)
                return name, group, ""
        return "", "", ("没能在该访问令牌下找到这个 sk 对应的令牌 —— "
                        "请确认访问令牌与渠道里的 sk 属于同一个上游账号")

    @staticmethod
    def _ng(t: dict) -> tuple[str, str]:
        return (t.get("name") or ""), (t.get("group") or "")

    def _by_search(self, bare: str) -> tuple[str, str]:
        """精确搜索。返回 (令牌名, 分组)，失败返回 ('','')。"""
        try:
            r = self.client.get("/api/token/search",
                                params={"token": bare, "p": 1, "size": 20})
            items = _items(_json(r).get("data"))
        except Exception:  # noqa: BLE001
            return "", ""
        # 精确搜索通常只回一条；多条时用可见后缀二次确认
        if len(items) == 1:
            return self._ng(items[0])
        for t in items:
            k = str(t.get("key") or "")
            suf = self._visible_suffix(k)
            if k == bare or (suf and len(suf) >= 4 and bare.endswith(suf)):
                return self._ng(t)
        return "", ""

    def _by_list(self, bare: str) -> tuple[str, str]:
        """兜底：遍历令牌列表按可见后缀匹配（脱敏严时会失配）。"""
        for path in ("/api/token/?p=1&size=999", "/api/token/?p=0&page_size=999", "/api/token/"):
            try:
                items = _items(_json(self.client.get(path)).get("data"))
            except Exception:  # noqa: BLE001
                continue
            if not items:
                continue
            best, best_len = ("", ""), 0
            for t in items:
                suf = self._visible_suffix(str(t.get("key") or ""))
                if suf and len(suf) >= 4 and bare.endswith(suf) and len(suf) > best_len:
                    best, best_len = self._ng(t), len(suf)
            if best[0]:
                return best
        return "", ""

    @staticmethod
    def _visible_suffix(masked_key: str) -> str:
        """取脱敏 key 的可见尾部：最后一个 '*' 之后的部分；未脱敏则整串。"""
        if "*" in masked_key:
            return masked_key.rsplit("*", 1)[-1]
        return masked_key

    # ---------------- 用量 ----------------

    def usage_for_sk(self, sk: str, d) -> tuple[bool, float, str]:
        """取该 sk 在某天的用量。返回 (成功, 平台用量, 说明)。"""
        if not self.logged_in:
            return False, 0.0, self.error or "未建立会话"
        qpu = settings.quota_per_unit or 1
        name, group, err = self.token_info_for(sk)
        if err:
            return False, 0.0, err
        start_ts, end_ts = day_range(d)
        params = {
            "type": 0, "token_name": name, "model_name": "",
            "start_timestamp": start_ts,
            "end_timestamp": end_ts - 1,      # 当天 23:59:59
            # 带上令牌所属分组缩小统计范围 —— 同名令牌跨分组时不带会把用量算多。
            # 空串在各版本都是「不过滤」，所以拿不到分组也安全。
            "group": group or "",
        }
        try:
            r = self.client.get("/api/log/self/stat", params=params, timeout=STAT_TIMEOUT)
            j = _json(r)
            if not j.get("success"):
                return False, 0.0, f"读取用量失败: {j.get('message') or r.status_code}"
            data = j.get("data")
            quota = 0
            if isinstance(data, dict):
                quota = int(data.get("quota", 0) or 0)
            elif isinstance(data, (int, float)):
                quota = int(data)
            return True, quota / qpu, "ok"
        except Exception as exc:  # noqa: BLE001
            return False, 0.0, f"读取失败: {exc}"

    def debug_sk(self, sk: str, d) -> dict:
        """诊断：把每一步的原始结果都摊开，方便定位是认证、反查还是取数出了问题。"""
        if not self.logged_in:
            return {"error": self.error or "未建立会话", "auth_header_sent": "裸令牌 + Bearer 均已尝试"}
        bare = sk[3:] if sk.startswith("sk-") else sk
        start_ts, end_ts = day_range(d)

        def _get(path, params=None, timeout=TIMEOUT):
            try:
                r = self.client.get(path, params=params or {}, timeout=timeout)
                return {"http": r.status_code, "body": _json(r)}
            except Exception as exc:  # noqa: BLE001
                return {"error": str(exc)}

        self_info = _get("/api/user/self")
        search = _get("/api/token/search", {"token": bare, "p": 1, "size": 20})
        listed = _get("/api/token/?p=1&size=20")
        name, group, match_err = self.token_info_for(sk)

        def _stat(token_name, grp=""):
            return _get("/api/log/self/stat",
                        {"type": 0, "token_name": token_name, "model_name": "",
                         "start_timestamp": start_ts, "end_timestamp": end_ts - 1,
                         "group": grp},
                        STAT_TIMEOUT)

        return {
            "auth": f"ok（uid={self.uid}，New-Api-User 已{'带上' if self.uid else '未带'}）",
            "auth_headers": {k: (v[:8] + "…" if k.lower() == "authorization" else v)
                             for k, v in dict(self.client.headers).items()
                             if k.lower() in ("authorization", "new-api-user")},
            "user_self": self_info,
            "sk_tail": sk[-8:],
            "token_search": search,
            "token_list_sample": {
                "http": listed.get("http"),
                "names": [t.get("name") for t in _items((listed.get("body") or {}).get("data"))][:10],
                "keys_masked": [str(t.get("key") or "")[-10:]
                                for t in _items((listed.get("body") or {}).get("data"))][:10],
            },
            "matched_name": name or "(未匹配到)",
            "matched_group": group or "(无)",
            "match_error": match_err or None,
            # 实际取数用的组合（带分组）
            "stat_with_name_group": _stat(name, group) if name else None,
            # 不带分组对照：两者不一致说明有同名跨分组令牌
            "stat_with_name_only": _stat(name) if name else None,
            "stat_account_total": _stat(""),
            "day_range": [start_ts, end_ts - 1],
        }

    def close(self):
        if self.client:
            self.client.close()


def fetch_usage(base_url: str, access_token: str, sk: str, d,
                user_id: str = "") -> tuple[bool, float, str]:
    """单个 sk 读取（内部建会话）。用访问令牌认证。"""
    s = UpstreamSession(base_url, access_token, user_id)
    ok, msg = s.open()
    if not ok:
        s.close()
        return False, 0.0, msg
    try:
        return s.usage_for_sk(sk, d)
    finally:
        s.close()


def verify_token(base_url: str, access_token: str, user_id: str = "") -> tuple[bool, str]:
    """测试访问令牌是否可用（供应商令牌页的「测试」按钮用）。"""
    s = UpstreamSession(base_url, access_token, user_id)
    try:
        return s.open()
    finally:
        s.close()
