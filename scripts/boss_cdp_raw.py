#!/usr/bin/env python3
"""
BOSS直聘职位抓取 + 分析 — 纯 CDP raw protocol

功能:
  1. 搜索特定职位 (关键词 + 城市)
  2. 筛选公司规模、融资阶段、薪资范围、经验、学历、行业
  3. 抓取详情页 JD 并分析薪资范围和技能要求
  4. 输出结构化 JSON + CSV + 终端分析报告
  5. 环境检查、Chrome CDP 自动启动、登录状态检测

用法 (双模式):
  # 阶段1 检索列表（多条件筛选）
  uv run python3 scripts/boss_cdp_raw.py --mode search --keyword "agent开发" --city 北京 --pages 3 --scale 305 --salary 406 --stdout
  # 阶段2 精选详情（管道 / 自动加载最新列表 / --job_link 直接传链接）
  uv run python3 scripts/boss_cdp_raw.py --mode detail --job_id id1,id2 --stdout
  uv run python3 scripts/boss_cdp_raw.py --check
  uv run python3 scripts/boss_cdp_raw.py --setup-chrome
  uv run python3 scripts/boss_cdp_raw.py --version
"""

__version__ = "2.9.0"

import json
import time
import random
import sys
import argparse
import os
import re
import hashlib
import csv
import glob
import platform
import subprocess
import shutil
import signal
import logging
import ntpath
import tempfile
from datetime import datetime
from collections import Counter
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl
from urllib.request import Request, urlopen

websocket = None
requests = None


def configure_console_encoding():
    """Keep CLI diagnostics printable on Windows consoles using legacy code pages."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if not reconfigure:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            # Embedded callers and test doubles may expose a non-reconfigurable stream.
            pass


configure_console_encoding()

# ============================================================
# 全局常量
# ============================================================

# CDP 默认端口（可通过 --cdp-port 覆盖）
DEFAULT_CDP_PORT = 9222

# API 基础路径（便于统一修改）
API_JOB_LIST_PATH = "/wapi/zpgeek/search/joblist.json"
DEFAULT_HOMEPAGE_URL = "https://www.zhipin.com/chengdu/?ka=header-home"
DEFAULT_INBOX_URL = "https://www.zhipin.com/web/geek/chat"
INBOX_FRIEND_LIST_PATH = "/wapi/zprelation/friend/getGeekFriendList.json"
HOT_CITY_URL = "https://www.zhipin.com/wapi/zpgeek/search/job/hot/city.json"
CITY_GROUP_URL = "https://www.zhipin.com/wapi/zpCommon/data/cityGroup.json"

# 请求频率保护
MAX_PAGES = 10          # 单次最大页数
MAX_API_REQUESTS = 500  # 单次最大 API 请求数

def get_default_chrome_path():
    system = platform.system()
    if system == "Darwin":
        return "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if system == "Windows":
        candidates = []
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(ntpath.join(local_app_data, "Google", "Chrome", "Application", "chrome.exe"))
        for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(env_name)
            if base:
                candidates.append(ntpath.join(base, "Google", "Chrome", "Application", "chrome.exe"))
        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate
        return candidates[0] if candidates else "chrome.exe"

    candidates = [
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/snap/bin/chromium",
    ]
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return candidates[0]


def get_default_profile_dir():
    system = platform.system()
    if system == "Darwin":
        return os.path.expanduser("~/Library/Application Support/Google/Chrome")
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            base = ntpath.join(os.path.expanduser("~"), "AppData", "Local")
        return ntpath.join(base, "Google", "Chrome", "User Data")
    return os.path.expanduser("~/.config/google-chrome")


DEFAULT_CHROME_PATH = get_default_chrome_path()
DEFAULT_PROFILE_DIR = get_default_profile_dir()

DEFAULT_CDP_DATA_DIR = os.path.expanduser("~/.boss-zhipin-scraper/chrome-profile")
DEFAULT_RESULT_DIR = os.path.expanduser("~/.boss-zhipin-scraper/job-result")
# 结果目录可用环境变量覆盖（boss.ps1 默认指向工作区 job-data，与 AGENTS.md 约定一致）
RESULT_DIR = os.environ.get("BOSS_RESULT_DIR") or DEFAULT_RESULT_DIR
DEFAULT_CITY_INPUT = "上海"
# 全局请求计数器
_request_counter = 0
_live_city_maps_cache = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("boss_cdp")


def default_output_path(kind):
    filename = f"boss_{kind}_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    return os.path.join(RESULT_DIR, filename)


def require_runtime_dependencies(*names):
    global requests, websocket

    missing = []
    if "requests" in names and requests is None:
        try:
            import requests as requests_module
            requests = requests_module
        except ImportError:
            missing.append("requests")
    if "websocket" in names and websocket is None:
        try:
            import websocket as websocket_module
            websocket = websocket_module
        except ImportError:
            missing.append("websocket-client")
    if missing:
        print(f"缺少依赖: {' '.join(missing)}")
        print("请安装（任选其一）:")
        print(f"  uv add {' '.join(missing)}")
        print(f"  pip install {' '.join(missing)}")
        return False
    return True


# ============================================================
# 筛选参数映射
# Source snapshots:
# - 城市: https://www.zhipin.com/wapi/zpgeek/search/job/hot/city.json + cityGroup.json
# - 筛选项: https://www.zhipin.com/wapi/zpgeek/search/job/condition.json
# ============================================================
# 城市码表已外置到 data/city_codes.json（全量城市，覆盖一二三四五线），
# 见 issue #24。resolve_city 查询链：本地静态 → 运行时拉 BOSS 接口 → 9 位裸码兜底。
# 仓库内路径（开发态）与打包后路径（pip install）都在 _city_data_path() 里处理。
CITY_DATA_FILENAME = "city_codes.json"

_local_city_map_cache = None


def _city_data_path():
    """返回 data/city_codes.json 的路径，兼容仓库开发态与 pip 打包态。"""
    # 1. 仓库开发态：脚本在 scripts/，数据在 ../data/
    repo_data = os.path.join(os.path.dirname(__file__), "..", "data", CITY_DATA_FILENAME)
    if os.path.isfile(repo_data):
        return os.path.normpath(repo_data)
    # 2. 打包态：wheel force-include 到包根 data/，用 importlib.resources 兜底
    try:
        from importlib.resources import files  # py3.9+
        pkg_data = files(__package__ or "__main__").joinpath("..", "data", CITY_DATA_FILENAME) \
            if __package__ else None
    except Exception:
        pkg_data = None
    if pkg_data is not None and os.path.isfile(str(pkg_data)):
        return str(pkg_data)
    # 3. 找不到则返回开发态路径（让调用方决定降级）
    return os.path.normpath(repo_data)


def load_local_city_map():
    """读取本地 data/city_codes.json 静态全量城市码表。

    返回 (name_to_code, code_to_name) 两个字典；读取失败返回 ({}, {})。
    结果缓存，重复调用零开销。
    """
    global _local_city_map_cache
    if _local_city_map_cache is not None:
        return _local_city_map_cache
    name_to_code = {}
    try:
        path = _city_data_path()
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for name, code in raw.items():
                if name and code is not None:
                    name_to_code[str(name)] = str(code)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        log.debug(f"读取本地城市码表失败: {e}")
    code_to_name = {code: name for name, code in name_to_code.items()}
    _local_city_map_cache = name_to_code, code_to_name
    return _local_city_map_cache

SCALE_MAP = {
    "0-20人": "301", "20-99人": "302", "100-499人": "303",
    "500-999人": "304", "1000-9999人": "305", "10000人以上": "306",
}

STAGE_MAP = {
    "未融资": "801", "天使轮": "802", "A轮": "803", "B轮": "804",
    "C轮": "805", "D轮及以上": "806", "已上市": "807", "不需要融资": "808",
}

SALARY_MAP = {
    "不限": "0", "3K以下": "402", "3-5K": "403", "5-10K": "404",
    "10-20K": "405", "20-50K": "406", "50K以上": "407",
}

EXPERIENCE_MAP = {
    "不限": "0", "在校生": "108", "应届生": "102", "经验不限": "101",
    "1年以内": "103", "1-3年": "104",
    "3-5年": "105", "5-10年": "106", "10年以上": "107",
}

DEGREE_MAP = {
    "不限": "0", "初中及以下": "209", "中专/中技": "208", "高中": "206",
    "大专": "202", "本科": "203", "硕士": "204", "博士": "205",
}

INDUSTRY_MAP = {
    "互联网": "1001", "电子商务": "1002", "金融": "1003", "游戏": "1004",
    "企业服务": "1005", "教育培训": "1006", "社交网络": "1007",
    "医疗健康": "1008", "生活服务": "1009", "广告营销": "1010",
}


# ============================================================
# 全局请求计数器辅助
# ============================================================
def incr_request():
    """递增全局请求计数，达到上限时抛出异常"""
    global _request_counter
    _request_counter += 1
    if _request_counter > MAX_API_REQUESTS:
        raise RuntimeError(f"已达到单次最大请求数 {MAX_API_REQUESTS}，停止抓取")
    if _request_counter >= MAX_API_REQUESTS * 0.8:
        log.warning(f"⚠️ 请求次数接近上限: {_request_counter}/{MAX_API_REQUESTS}")


# ============================================================
# CDP 连接
# ============================================================
class CDPSession:
    def __init__(self, cdp_port=DEFAULT_CDP_PORT):
        if not require_runtime_dependencies("requests", "websocket"):
            raise RuntimeError("缺少 CDP 运行依赖")
        self.cdp_port = cdp_port
        resp = requests.get(f"http://127.0.0.1:{cdp_port}/json/version", timeout=10)
        ws_url = resp.json()["webSocketDebuggerUrl"]
        self.ws = websocket.create_connection(ws_url, timeout=60)
        self.mid = 0
        self._events = []

    def send(self, method, params=None, sid=None, timeout=30):
        """发送 CDP 命令并等待匹配的响应。

        Args:
            method: CDP 方法名
            params: 参数字典
            sid: Target session ID
            timeout: 等待响应的超时秒数，默认 30s

        Returns:
            CDP 响应字典

        Raises:
            TimeoutError: 超过 max_retries 仍未收到匹配响应
        """
        self.mid += 1
        msg = {"id": self.mid, "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        self.ws.send(json.dumps(msg))

        start_time = time.time()
        max_retries = 1000

        for attempt in range(max_retries):
            # 检查超时
            elapsed = time.time() - start_time
            if elapsed > timeout:
                raise TimeoutError(
                    f"CDP send({method}) 超时 ({timeout}s), "
                    f"已跳过 {attempt} 条不匹配消息"
                )

            try:
                raw = self.ws.recv()
            except websocket.WebSocketTimeoutException:
                raise TimeoutError(f"CDP WebSocket recv 超时, method={method}")

            try:
                r = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                log.debug(f"跳过非 JSON 消息: {raw[:100]}")
                continue

            if r.get("id") == self.mid:
                return r

            # 不匹配的消息：可能是事件通知，记录并跳过
            event_name = r.get("method", "unknown")
            log.debug(f"跳过不匹配消息 (id={r.get('id')}, event={event_name})")
            if r.get("method"):
                self._events.append(r)

        raise TimeoutError(
            f"CDP send({method}) 在 {max_retries} 条消息内未找到匹配响应"
        )

    def pop_event(self, method=None, sid=None):
        """Return and remove the first buffered CDP event matching filters."""
        for index, event in enumerate(self._events):
            if method and event.get("method") != method:
                continue
            if sid and event.get("sessionId") != sid:
                continue
            return self._events.pop(index)
        return None

    def recv_event(self, timeout=1.0, sid=None):
        """Read one CDP event, preserving unrelated events for later callers."""
        event = self.pop_event(sid=sid)
        if event is not None:
            return event

        old_timeout = self.ws.gettimeout()
        self.ws.settimeout(max(0.05, timeout))
        try:
            while True:
                try:
                    raw = self.ws.recv()
                except websocket.WebSocketTimeoutException:
                    return None
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if message.get("method") and (not sid or message.get("sessionId") == sid):
                    return message
                if message.get("method"):
                    self._events.append(message)
        finally:
            self.ws.settimeout(old_timeout)

    def eval_js(self, js, sid):
        r = self.send("Runtime.evaluate", {"expression": js, "returnByValue": True}, sid)
        return r.get("result", {}).get("result", {}).get("value", None)

    def close(self):
        self.ws.close()


def create_page_session(cdp, background=False):
    """Create and attach a normal page target without injecting page scripts."""
    target = cdp.send(
        "Target.createTarget",
        {"url": "about:blank", "background": background},
    )
    target_id = target["result"]["targetId"]
    attached = cdp.send(
        "Target.attachToTarget",
        {"targetId": target_id, "flatten": True},
    )
    session_id = attached["result"]["sessionId"]
    return target_id, session_id


def attach_active_inbox_target(cdp):
    """Attach to the already open dedicated inbox page without navigating it."""
    targets = (cdp.send("Target.getTargets").get("result") or {}).get("targetInfos") or []
    candidates = []
    for target in targets:
        if target.get("type") != "page":
            continue
        parsed = urlparse(target.get("url") or "")
        if parsed.netloc not in {"www.zhipin.com", "zhipin.com"}:
            continue
        if parsed.path.startswith("/web/geek/chat"):
            candidates.append(target)
    if len(candidates) != 1:
        raise RuntimeError(
            "未找到唯一的专用 BOSS 消息页；请只保留并打开目标会话后再读取"
        )
    target = candidates[0]
    attached = cdp.send(
        "Target.attachToTarget",
        {"targetId": target["targetId"], "flatten": True},
    )
    return target["targetId"], attached["result"]["sessionId"]


class BossAPIError(RuntimeError):
    """BOSS returned a non-success business response."""

    def __init__(self, code, message=""):
        self.code = code
        self.message = message
        suffix = f": {message}" if message else ""
        super().__init__(f"BOSS 搜索接口返回 code {code}{suffix}")


def as_string_list(value):
    """Normalize a list-or-string API field for safe display and CSV output."""
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    if value in (None, ""):
        return []
    return [str(value)]


def normalize_api_jobs(data):
    """Normalize a native BOSS joblist response without issuing another request."""
    if not isinstance(data, dict):
        return []
    raw_code = data.get("code")
    try:
        code = int(raw_code) if raw_code is not None else 0
    except (TypeError, ValueError):
        code = -1
    if code != 0:
        message = str(data.get("message") or data.get("msg") or "")
        raise BossAPIError(code, message)

    jobs = (data.get("zpData") or {}).get("jobList") or []
    results = []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        salary = j.get("salaryDesc") or ""
        results.append({
            "title": j.get("jobName") or "",
            "salary": salary,
            "salary_source": "api" if salary else "api_empty",
            "location": "·".join(filter(None, [
                j.get("cityName") or "",
                j.get("areaDistrict") or "",
                j.get("businessDistrict") or "",
            ])),
            "tags": " | ".join(
                t for t in [j.get("jobExperience") or "", j.get("jobDegree") or ""]
                if t and t != "不限"
            ),
            "boss_name": j.get("brandName") or "",
            "boss_title": j.get("bossTitle") or "",
            "boss_active_status": j.get("activeTimeDesc") or ("在线" if j.get("bossOnline") else ""),
            "company_scale": j.get("brandScaleName") or "",
            "company_stage": j.get("brandStageName") or "",
            "company_industry": j.get("brandIndustry") or "",
            "job_labels": " | ".join(as_string_list(j.get("jobLabels"))),
            "skills": " | ".join(as_string_list(j.get("skills"))),
            "security_id": j.get("securityId") or "",
            "lid": j.get("lid") or "",
            "encrypt_job_id": j.get("encryptJobId") or "",
            "encrypt_boss_id": j.get("encryptBossId") or "",
            "encrypt_brand_id": j.get("encryptBrandId") or "",
            "job_link": (
                "https://www.zhipin.com/job_detail/"
                + str(j.get("encryptJobId"))
                + ".html"
                if j.get("encryptJobId") else ""
            ),
            "company_link": (
                "https://www.zhipin.com/gongsi/"
                + str(j.get("encryptBrandId"))
                + ".html"
                if j.get("encryptBrandId") else ""
            ),
            "welfare": " | ".join(as_string_list(j.get("welfareList"))),
        })
    return results


def wait_for_native_joblist_response(cdp, sid, timeout=25):
    """Capture the page's own joblist XHR/fetch response via CDP Network events."""
    pending = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        event = cdp.recv_event(timeout=min(1.0, max(0.05, deadline - time.time())), sid=sid)
        if event is None:
            continue
        method = event.get("method")
        params = event.get("params") or {}
        if method == "Network.responseReceived":
            response = params.get("response") or {}
            url = response.get("url") or ""
            if urlparse(url).path == API_JOB_LIST_PATH:
                pending[params.get("requestId")] = response
        elif method == "Network.loadingFinished":
            request_id = params.get("requestId")
            response = pending.pop(request_id, None)
            if response is None:
                continue
            body_result = cdp.send(
                "Network.getResponseBody",
                {"requestId": request_id},
                sid,
                timeout=10,
            )
            result = body_result.get("result") or {}
            body = result.get("body") or ""
            if result.get("base64Encoded"):
                import base64
                body = base64.b64decode(body).decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
            except (json.JSONDecodeError, ValueError) as exc:
                raise RuntimeError("BOSS 原生搜索响应不是有效 JSON") from exc
            return normalize_api_jobs(data)
    raise TimeoutError(f"等待页面原生 {API_JOB_LIST_PATH} 响应超时")


HOMEPAGE_JOB_KEYS = {
    "jobName", "encryptJobId", "salaryDesc", "brandName", "cityName",
}


def _looks_like_job_item(value):
    """Return whether a mapping resembles one native BOSS job item."""
    if not isinstance(value, dict):
        return False
    candidate = value.get("jobInfo") if isinstance(value.get("jobInfo"), dict) else value
    return len(HOMEPAGE_JOB_KEYS.intersection(candidate)) >= 2


def iter_homepage_job_lists(value, path="$"):
    """Yield ``(json_path, list)`` pairs that contain native job objects."""
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if isinstance(child, list) and any(_looks_like_job_item(item) for item in child):
                yield child_path, child
            else:
                yield from iter_homepage_job_lists(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            if isinstance(child, (dict, list)):
                yield from iter_homepage_job_lists(child, f"{path}[{index}]")


def classify_homepage_section(response_url, json_path):
    """Best-effort section label derived from native endpoint/path names."""
    parsed_url = urlparse(response_url)
    query = dict(parse_qsl(parsed_url.query, keep_blank_values=True))
    if query.get("sortType") == "1":
        return "selected"
    if query.get("sortType") == "2":
        return "latest"
    hint = f"{parsed_url.path} {parsed_url.query} {json_path}".lower()
    if any(token in hint for token in ("latest", "newest", "recent", "fresh", "newjob")):
        return "latest"
    if any(token in hint for token in ("recommend", "selected", "choice", "guess", "expect")):
        return "selected"
    return "other"


def normalize_homepage_payload(data, response_url=""):
    """Extract and normalize all native job lists found in one homepage payload."""
    if not isinstance(data, dict):
        return [], []
    raw_code = data.get("code")
    try:
        code = int(raw_code) if raw_code is not None else 0
    except (TypeError, ValueError):
        code = -1
    if code != 0:
        message = str(data.get("message") or data.get("msg") or "")
        raise BossAPIError(code, message)

    jobs = []
    sources = []
    for json_path, raw_jobs in iter_homepage_job_lists(data):
        section = classify_homepage_section(response_url, json_path)
        count_before = len(jobs)
        for raw in raw_jobs:
            if not isinstance(raw, dict):
                continue
            candidate = raw.get("jobInfo") if isinstance(raw.get("jobInfo"), dict) else raw
            normalized = normalize_api_jobs({"code": 0, "zpData": {"jobList": [candidate]}})
            if not normalized:
                continue
            job = normalized[0]
            job["homepage_section"] = section
            job["homepage_source_path"] = json_path
            job["homepage_response_url"] = response_url
            jobs.append(job)
        sources.append({
            "response_url": response_url,
            "json_path": json_path,
            "section": section,
            "job_count": len(jobs) - count_before,
        })
    return jobs, sources


def wait_for_homepage_job_responses(cdp, sid, timeout=15):
    """Capture native JSON responses emitted by the homepage and extract jobs."""
    pending = {}
    all_jobs = []
    sources = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        event = cdp.recv_event(timeout=min(1.0, max(0.05, deadline - time.time())), sid=sid)
        if event is None:
            continue
        method = event.get("method")
        params = event.get("params") or {}
        if method == "Network.responseReceived":
            response = params.get("response") or {}
            url = response.get("url") or ""
            mime_type = str(response.get("mimeType") or "").lower()
            if "zhipin.com" in urlparse(url).netloc and (
                "json" in mime_type or "/wapi/" in urlparse(url).path
            ):
                pending[params.get("requestId")] = response
        elif method == "Network.loadingFinished":
            request_id = params.get("requestId")
            response = pending.pop(request_id, None)
            if response is None:
                continue
            try:
                body_result = cdp.send(
                    "Network.getResponseBody", {"requestId": request_id}, sid, timeout=10,
                )
            except (KeyError, TimeoutError):
                continue
            result = body_result.get("result") or {}
            body = result.get("body") or ""
            if result.get("base64Encoded"):
                import base64
                body = base64.b64decode(body).decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                continue
            response_url = response.get("url") or ""
            response_path = urlparse(response_url).path
            has_job_list = any(True for _ in iter_homepage_job_lists(data))
            if not has_job_list and "/recommend/job/list.json" not in response_path:
                continue
            jobs, response_sources = normalize_homepage_payload(data, response_url)
            all_jobs.extend(jobs)
            sources.extend(response_sources)
            captured_sections = {
                source.get("section") for source in sources if source.get("job_count", 0) > 0
            }
            if {"selected", "latest"}.issubset(captured_sections):
                break
    return all_jobs, sources


def json_list_shapes(value, path="$", limit=30):
    """Describe JSON list schemas without retaining any private values."""
    shapes = []
    if isinstance(value, dict):
        for key, child in value.items():
            if len(shapes) >= limit:
                break
            shapes.extend(json_list_shapes(child, f"{path}.{key}", limit - len(shapes)))
    elif isinstance(value, list):
        item_keys = []
        if value and isinstance(value[0], dict):
            item_keys = sorted(str(key) for key in value[0].keys())[:40]
        shapes.append({"path": path, "length": len(value), "item_keys": item_keys})
        for index, child in enumerate(value[:3]):
            if isinstance(child, (dict, list)):
                if len(shapes) >= limit:
                    break
                shapes.extend(json_list_shapes(child, f"{path}[{index}]", limit - len(shapes)))
    return shapes


def websocket_payload_schema(payload_data):
    """Summarize a WebSocket payload without retaining any message values.

    The inbox discovery mode is allowed to establish which protocol envelopes
    exist, but must never print recruiter names, message text, attachment URLs,
    or opaque identifiers embedded in the frame body.
    """
    if not isinstance(payload_data, str):
        return {"encoding": "unknown", "top_level_type": type(payload_data).__name__}
    try:
        payload = json.loads(payload_data)
    except (json.JSONDecodeError, ValueError):
        return {"encoding": "non_json", "payload_bytes": len(payload_data.encode("utf-8"))}

    summary = {"encoding": "json", "top_level_type": type(payload).__name__}
    if isinstance(payload, dict):
        summary["top_level_keys"] = sorted(str(key) for key in payload.keys())[:40]
        nested = {}
        for key, value in payload.items():
            if isinstance(value, dict):
                nested[str(key)] = sorted(str(child) for child in value.keys())[:30]
            elif isinstance(value, list):
                nested[str(key)] = {
                    "list_length": len(value),
                    "item_keys": sorted(str(child) for child in value[0].keys())[:30]
                    if value and isinstance(value[0], dict) else [],
                }
        if nested:
            summary["nested_keys"] = nested
    elif isinstance(payload, list):
        summary["list_length"] = len(payload)
        if payload and isinstance(payload[0], dict):
            summary["item_keys"] = sorted(str(key) for key in payload[0].keys())[:40]
    return summary


def wait_for_inbox_endpoint_metadata(cdp, sid, timeout=12):
    """Observe inbox JSON and WebSocket schemas without returning message contents."""
    pending = {}
    metadata = []
    seen = set()
    websocket_urls = {}
    websocket_frames = []
    websocket_seen = set()
    deadline = time.time() + timeout
    while time.time() < deadline:
        event = cdp.recv_event(timeout=min(1.0, max(0.05, deadline - time.time())), sid=sid)
        if event is None:
            continue
        method = event.get("method")
        params = event.get("params") or {}
        if method == "Network.webSocketCreated":
            request_id = params.get("requestId")
            if request_id:
                websocket_urls[request_id] = urlparse(params.get("url") or "").path or "(root)"
        elif method in {"Network.webSocketFrameReceived", "Network.webSocketFrameSent"}:
            frame = params.get("response") or params.get("request") or {}
            request_id = params.get("requestId")
            schema = websocket_payload_schema(frame.get("payloadData"))
            key = (
                method,
                websocket_urls.get(request_id, "(unknown)"),
                json.dumps(schema, ensure_ascii=False, sort_keys=True),
            )
            if key not in websocket_seen:
                websocket_seen.add(key)
                websocket_frames.append({
                    "direction": "received" if method.endswith("Received") else "sent",
                    "socket_path": websocket_urls.get(request_id, "(unknown)"),
                    "opcode": frame.get("opcode"),
                    "schema": schema,
                })
        elif method == "Network.responseReceived":
            response = params.get("response") or {}
            parsed_url = urlparse(response.get("url") or "")
            mime_type = str(response.get("mimeType") or "").lower()
            if "zhipin.com" in parsed_url.netloc and (
                "json" in mime_type or "/wapi/" in parsed_url.path
            ):
                pending[params.get("requestId")] = response
        elif method == "Network.loadingFinished":
            request_id = params.get("requestId")
            response = pending.pop(request_id, None)
            if response is None:
                continue
            try:
                body_result = cdp.send(
                    "Network.getResponseBody", {"requestId": request_id}, sid, timeout=10,
                )
            except (KeyError, TimeoutError):
                continue
            result = body_result.get("result") or {}
            body = result.get("body") or ""
            if result.get("base64Encoded"):
                import base64
                body = base64.b64decode(body).decode("utf-8", errors="replace")
            try:
                payload = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                continue
            raw_code = payload.get("code") if isinstance(payload, dict) else None
            try:
                code = int(raw_code) if raw_code is not None else 0
            except (TypeError, ValueError):
                code = -1
            if code in {31, 37}:
                raise BossAPIError(code, "收件箱页面原生接口受限")

            parsed_url = urlparse(response.get("url") or "")
            key = (parsed_url.path, tuple(sorted(key for key, _ in parse_qsl(parsed_url.query))))
            if key in seen:
                continue
            seen.add(key)
            zp_data = payload.get("zpData") if isinstance(payload, dict) else None
            metadata.append({
                "path": parsed_url.path,
                "query_keys": [key for key, _ in parse_qsl(parsed_url.query)],
                "code": code,
                "top_level_keys": sorted(str(key) for key in payload.keys())[:40]
                if isinstance(payload, dict) else [],
                "zp_data_keys": sorted(str(key) for key in zp_data.keys())[:40]
                if isinstance(zp_data, dict) else [],
                "list_shapes": json_list_shapes(zp_data if isinstance(zp_data, (dict, list)) else payload),
            })
    return metadata, websocket_frames


def normalize_inbox_conversations(data):
    """Return non-content conversation metadata from BOSS's native inbox list."""
    if not isinstance(data, dict):
        return []
    raw_code = data.get("code")
    try:
        code = int(raw_code) if raw_code is not None else 0
    except (TypeError, ValueError):
        code = -1
    if code != 0:
        raise BossAPIError(code, str(data.get("message") or data.get("msg") or ""))

    items = (data.get("zpData") or {}).get("result") or []
    conversations = []
    for item in items:
        if not isinstance(item, dict):
            continue
        security_id = item.get("securityId") or ""
        encrypt_job_id = item.get("encryptJobId") or ""
        job_link = ""
        if encrypt_job_id:
            job_link = "https://www.zhipin.com/job_detail/" + str(encrypt_job_id) + ".html"
            if security_id:
                job_link = build_detail_url({
                    "job_link": job_link,
                    "security_id": security_id,
                })
        conversations.append({
            "conversation_id": item.get("encryptUid") or item.get("encryptBossId") or "",
            "job_id": encrypt_job_id,
            "job_title": item.get("sourceTitle") or item.get("jobName") or "",
            "company": item.get("brandName") or "",
            "recruiter_title": item.get("title") or "",
            "chat_status": item.get("chatStatus") or "",
            "unread_count": item.get("unreadMsgCount") or 0,
            "last_time": item.get("lastTime") or "",
            "last_timestamp": item.get("lastTS") or "",
            "is_top": bool(item.get("isTop")),
            "source_type": item.get("sourceType") or "",
            "job_source": item.get("jobSource") or "",
            "job_link": job_link,
        })
    return conversations


def wait_for_native_inbox_list(cdp, sid, timeout=15):
    """Capture the page's native conversation-list response, without DOM scraping."""
    pending = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        event = cdp.recv_event(timeout=min(1.0, max(0.05, deadline - time.time())), sid=sid)
        if event is None:
            continue
        method = event.get("method")
        params = event.get("params") or {}
        if method == "Network.responseReceived":
            response = params.get("response") or {}
            if urlparse(response.get("url") or "").path == INBOX_FRIEND_LIST_PATH:
                pending[params.get("requestId")] = response
        elif method == "Network.loadingFinished":
            request_id = params.get("requestId")
            response = pending.pop(request_id, None)
            if response is None:
                continue
            body_result = cdp.send(
                "Network.getResponseBody", {"requestId": request_id}, sid, timeout=10,
            )
            result = body_result.get("result") or {}
            body = result.get("body") or ""
            if result.get("base64Encoded"):
                import base64
                body = base64.b64decode(body).decode("utf-8", errors="replace")
            try:
                payload = json.loads(body)
            except (json.JSONDecodeError, ValueError) as exc:
                raise RuntimeError("BOSS 原生收件箱响应不是有效 JSON") from exc
            return normalize_inbox_conversations(payload)
    raise TimeoutError(f"等待页面原生 {INBOX_FRIEND_LIST_PATH} 响应超时")

# ============================================================
# Optional DOM fallback for explicit opt-in only. The normal list path never
# injects XHR and never uses this extractor; DOM salary may be obfuscated.
# ============================================================
EXTRACT_LIST_JS = """
(function(){
    var results = [];
    var cards = document.querySelectorAll('li.job-card-box');
    for (var i = 0; i < cards.length; i++) {
        var card = cards[i];
        var nameEl = card.querySelector('.job-name');
        var salaryEl = card.querySelector('.job-salary');
        var locEl = card.querySelector('.company-location');
        var tagEls = card.querySelectorAll('.tag-list li');
        var bossEl = card.querySelector('.boss-name');
        var bossLink = card.querySelector('.boss-info');
        var tags = [];
        for (var j = 0; j < tagEls.length; j++) tags.push(tagEls[j].innerText.trim());
        var jobLink = nameEl ? (nameEl.getAttribute('href') || '') : '';
        if (jobLink && jobLink.charAt(0) === '/') jobLink = 'https://www.zhipin.com' + jobLink;
        var cLink = bossLink ? (bossLink.getAttribute('href') || '') : '';
        if (cLink && cLink.charAt(0) === '/') cLink = 'https://www.zhipin.com' + cLink;
        var t = nameEl ? nameEl.innerText.trim() : '';
        if (t) results.push({
            title: t,
            salary: salaryEl ? salaryEl.innerText.trim() : '',
            salary_source: 'dom_untrusted',
            location: locEl ? locEl.innerText.trim() : '',
            tags: tags.join(' | '),
            boss_name: bossEl ? bossEl.innerText.trim() : '',
            job_link: jobLink,
            company_link: cLink
        });
    }
    return JSON.stringify(results);
})()
"""

# ============================================================
# 详情页提取与校验
# ============================================================
DETAIL_LOGIN_MARKER = "登录查看完整内容"
DETAIL_DESCRIPTION_MARKER = "职位描述"
DETAIL_COMPETITIVENESS_MARKER = "竞争力分析"
DETAIL_SAFETY_MARKER = "BOSS 安全提示"
MIN_DETAIL_TEXT_LENGTH = 120


class DetailExtractionError(ValueError):
    """The rendered page does not contain a usable job description."""


class DetailLoginRequiredError(DetailExtractionError):
    """The detail page is truncated because the BOSS session is not logged in."""


EXTRACT_DETAIL_JS = r"""
(function(){
    var pageText = document.body ? document.body.innerText : '';
    function firstText(selectors) {
        for (var i = 0; i < selectors.length; i++) {
            var el = document.querySelector(selectors[i]);
            if (el && (el.innerText || '').trim()) return el.innerText.trim();
        }
        return '';
    }
    var tags = [];
    var benefitWords = ['五险','补充医疗','定期体检','带薪年假','年终奖','零食','餐补',
        '节日福利','加班补助','股票期权','员工旅游','交通补助','通讯补贴','团建',
        '生日福利','免费班车','全勤奖','包吃','弹性工作','下午茶','租房补贴',
        '体检','健身','文化','充电假','司龄假','红包','能量补贴','社团','三薪',
        '绩效','底薪','保底','活动基金','学习基金','节日礼品','无障碍'];
    var noiseWords = ['BOSS直聘','boss','BOSS','来自BOSS直聘','金','金币'];
    function isBenefit(t) {
        if (t === '...' || t.length > 15 || t.length < 2) return true;
        for (var i = 0; i < benefitWords.length; i++) {
            if (t.includes(benefitWords[i])) return true;
        }
        for (var i = 0; i < noiseWords.length; i++) {
            if (t === noiseWords[i] || t.includes(noiseWords[i])) return true;
        }
        return false;
    }
    function addTagText(value) {
        (value || '').split(/\n+/).forEach(function(part){
            var t = part.trim().replace(/\s+/g, ' ');
            if (t && !isBenefit(t) && tags.indexOf(t) === -1) tags.push(t);
        });
    }
    document.querySelectorAll(
        '.job-tags span, .job-tags .tag-all, .job-keyword-list span, '
        + '.job-limit span, .job-limit .item, .job-primary .info-public span, '
        + '[class*="job-limit"] span, [class*="job-tag"] span'
    ).forEach(function(s){ addTagText(s.innerText); });
    // Some internship constraints are rendered as plain text instead of tag
    // nodes. Keep only exact visible phrases; do not infer requirements from
    // the prose JD.
    [
        /\d+\s*天\s*\/\s*周/g,
        /(?:实习|工作(?:时长)?|持续|连续)\s*\d+\s*个月/g,
        /每周可实习\s*\d+\s*天/g,
        /优秀论文优先/g
    ].forEach(function(pattern){
        var matches = pageText.match(pattern) || [];
        matches.forEach(addTagText);
    });
    var jd = '';
    var sections = document.querySelectorAll('.job-detail-section, .job-sec');
    for (var i = 0; i < sections.length; i++) {
        var text = (sections[i].innerText || '').trim();
        if (text.indexOf('职位描述') !== -1 && text.length > jd.length) {
            jd = text;
        }
    }
    var title = '';
    var h1 = document.querySelector('h1');
    if (h1) title = (h1.innerText || '').trim();
    if (!title) {
        var m = (document.title || '').match(/「(.+?)招聘」/);
        if (m) title = m[1];
    }
    var company = '';
    var sider = document.querySelector('.sider-company');
    if (sider) {
        var lines = (sider.innerText || '').split('\n').map(function(s){ return s.trim(); }).filter(function(s){ return s; });
        var ci = lines.indexOf('公司基本信息');
        if (ci >= 0 && lines[ci + 1]) company = lines[ci + 1];
    }
    if (!company) {
        var m2 = (document.title || '').match(/」_(.+?)招聘-BOSS直聘/);
        if (m2) company = m2[1];
    }
    var salary = firstText([
        '.job-banner .salary', '.job-primary .salary', '.job-salary',
        '[class*="job-salary"]', '[class*="salary"]'
    ]);
    if (!salary) {
        var salaryMatch = pageText.match(/\d+(?:\.\d+)?\s*-\s*\d+(?:\.\d+)?\s*(?:元\/天|元\/月|K(?:·\d+薪)?|千\/月|万\/月)/);
        if (salaryMatch) salary = salaryMatch[0].replace(/\s+/g, '');
    }
    var locationText = firstText([
        '.job-banner .job-address', '.job-primary .job-address',
        '.job-location', '.location-address', '[class*="job-address"]'
    ]);
    var companyLink = '';
    var companyAnchors = document.querySelectorAll('a[href*="/gongsi/"]');
    for (var ai = 0; ai < companyAnchors.length; ai++) {
        var candidateLink = companyAnchors[ai].href || '';
        if (/\/gongsi\/[^/?#]+/.test(candidateLink)) {
            companyLink = candidateLink;
            break;
        }
    }
    return JSON.stringify({
        jd: jd,
        page_text: pageText.substring(0, 12000),
        tags: tags,
        url: location.href,
        title: title,
        company: company,
        company_link: companyLink,
        salary: salary,
        location: locationText
    });
})()
"""


def _normalize_detail_whitespace(text):
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").splitlines()]
    normalized = "\n".join(lines).strip()
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return re.sub(r"[ \t]{2,}", " ", normalized)


def _normalize_detail_location(text):
    """Remove map/UI suffixes from the visible detail-page location."""
    lines = []
    for line in str(text or "").replace("\r\n", "\n").splitlines():
        value = line.strip()
        if not value or value in {"点击查看地图", "查看地图", "地图"}:
            continue
        lines.append(value)
    return "·".join(lines)


def _looks_like_navigation_page(text):
    return (
        DETAIL_DESCRIPTION_MARKER not in text
        and "无障碍专区" in text
        and "首页" in text
        and "职位" in text
        and "公司" in text
    )


def _is_boss_activity_line(text):
    """True for recruiter activity labels like「在线」「今日活跃」."""
    return text == "在线" or text.endswith("活跃")


def map_list_boss_active_status(job):
    """Map list-API job fields to ``boss_active_status``.

    BOSS ``/wapi/zpgeek/search/joblist.json`` typically exposes ``bossOnline``
    but not ``activeTimeDesc``. Prefer ``activeTimeDesc`` when present;
    otherwise map ``bossOnline=True`` to 「在线」. Detailed labels such as
    「刚刚活跃」still come from the detail path.
    """
    if not isinstance(job, dict):
        return ""
    desc = str(job.get("activeTimeDesc") or "").strip()
    if desc:
        return desc
    if job.get("bossOnline"):
        return "在线"
    return ""


def resolve_boss_active_status(list_status="", detail_status=""):
    """Prefer detail activity text; fall back to list mapping result."""
    detail = str(detail_status or "").strip()
    if detail:
        return detail
    return str(list_status or "").strip()


def _recruiter_footer_info(lines):
    """Locate recruiter card footer and optional activity status.

    Returns ``(footer_start, boss_active_status)``. ``footer_start`` is the
    line index where the recruiter card begins (to truncate JD), or ``None``.
    ``boss_active_status`` is e.g. ``今日活跃`` / ``在线``, or ``""``.
    """
    stripped_lines = [line.strip() for line in lines]
    end = len(stripped_lines)
    while end and not stripped_lines[end - 1]:
        end -= 1

    def card_info(card_end):
        while card_end and not stripped_lines[card_end - 1]:
            card_end -= 1
        if card_end < 4 or stripped_lines[card_end - 2] != "·":
            return None, ""
        activity_or_name = stripped_lines[card_end - 4]
        has_activity_line = _is_boss_activity_line(activity_or_name)
        if has_activity_line:
            start = card_end - 5
            status = activity_or_name
        else:
            start = card_end - 4
            status = ""
        if start < 0:
            return None, ""
        return start, status

    for marker in (DETAIL_COMPETITIVENESS_MARKER, DETAIL_SAFETY_MARKER):
        try:
            marker_index = stripped_lines.index(marker)
        except ValueError:
            continue
        start, status = card_info(marker_index)
        if start is not None:
            return start, status
    return card_info(end)


def _recruiter_footer_start(lines):
    start, _status = _recruiter_footer_info(lines)
    return start


def extract_detail_fields(extracted, min_length=MIN_DETAIL_TEXT_LENGTH):
    """Return validated JD and boss activity status as separate fields.

    ``jd`` never includes the recruiter card or activity label.
    ``boss_active_status`` is extracted from that card when present.

    ``page_text`` is diagnostic input only. It is never persisted unless it has
    an explicit job-description section that passes all checks.
    """
    if not isinstance(extracted, dict):
        raise DetailExtractionError("detail extractor returned non-dict")

    raw_jd = str(extracted.get("jd") or "")
    page_text = str(extracted.get("page_text") or "")
    diagnostic_text = "\n".join((raw_jd, page_text))

    if DETAIL_LOGIN_MARKER in diagnostic_text:
        raise DetailLoginRequiredError(
            "detail page is truncated at the login wall; refresh the BOSS login session"
        )
    if _looks_like_navigation_page(diagnostic_text):
        raise DetailExtractionError("detail page rendered navigation chrome without a JD")

    text = raw_jd
    if not text and DETAIL_DESCRIPTION_MARKER in page_text:
        text = page_text
    if DETAIL_DESCRIPTION_MARKER in text:
        text = text.split(DETAIL_DESCRIPTION_MARKER, 1)[1]

    lines = text.replace("\r\n", "\n").splitlines()
    footer_start, boss_active_status = _recruiter_footer_info(lines)
    if footer_start is not None:
        lines = lines[:footer_start]
    else:
        for index, line in enumerate(lines):
            if line.strip() == DETAIL_SAFETY_MARKER:
                lines = lines[:index]
                break

    jd = _normalize_detail_whitespace("\n".join(lines))
    if len(jd) < min_length:
        raise DetailExtractionError(
            f"job description too short after validation: {len(jd)} < {min_length}"
        )
    return {
        "jd": jd,
        "boss_active_status": boss_active_status,
        "title": str(extracted.get("title") or "").strip(),
        "company": str(extracted.get("company") or "").strip(),
        "company_link": str(extracted.get("company_link") or "").strip(),
        "salary": str(extracted.get("salary") or "").strip(),
        "location": _normalize_detail_location(extracted.get("location")),
        "tags": extracted.get("tags") if isinstance(extracted.get("tags"), list) else [],
    }


def extract_job_description(extracted, min_length=MIN_DETAIL_TEXT_LENGTH):
    """Return validated JD text without BOSS page chrome."""
    return extract_detail_fields(extracted, min_length=min_length)["jd"]


# ============================================================
# 解析城市参数（支持中文和代码）
# ============================================================
class CityAPIResponseError(ValueError):
    """BOSS 城市接口返回业务错误或无效响应。"""


class CityResolutionError(ValueError):
    """无法把用户输入解析为有效城市码。"""


def fetch_boss_json(url, timeout=10):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8-sig"))

    if not isinstance(data, dict):
        raise CityAPIResponseError(f"BOSS 城市接口返回非对象响应: {url}")

    code = data.get("code")
    if code != 0:
        message = data.get("message") or "未知错误"
        raise CityAPIResponseError(
            f"BOSS 城市接口返回业务错误 code={code}, message={message}: {url}"
        )
    if not isinstance(data.get("zpData"), dict):
        raise CityAPIResponseError(f"BOSS 城市接口响应缺少有效 zpData: {url}")
    return data


def load_live_city_maps(timeout=10):
    global _live_city_maps_cache
    if _live_city_maps_cache is not None:
        return _live_city_maps_cache

    name_to_code = {}

    try:
        hot_city_data = fetch_boss_json(HOT_CITY_URL, timeout=timeout)
        for item in hot_city_data.get("zpData", {}).get("hotCityList", []):
            name = item.get("name")
            code = item.get("code")
            if name and code is not None:
                name_to_code[name] = str(code)

        city_group_data = fetch_boss_json(CITY_GROUP_URL, timeout=timeout)
        for group in city_group_data.get("zpData", {}).get("cityGroup", []):
            for item in group.get("cityList", []):
                name = item.get("name")
                code = item.get("code")
                if name and code is not None:
                    name_to_code.setdefault(name, str(code))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError,
            CityAPIResponseError) as e:
        log.warning(f"加载 BOSS 在线城市映射失败: {e}")

    code_to_name = {code: name for name, code in name_to_code.items()}
    _live_city_maps_cache = name_to_code, code_to_name
    return _live_city_maps_cache


def resolve_city(city_input):
    """把「中文城市名 / 城市码」解析为 (name, code)。

    查询链（逐级降级）:
      1. 本地静态码表 data/city_codes.json（全量、离线可用）
      2. 运行时拉 BOSS 接口 hot/city.json + cityGroup.json（自愈）
      3. 都查不到时接受 9 位裸 city code，其他输入报错
    """
    if not city_input:
        return city_input, city_input

    # 1. 本地静态码表
    local_map, local_reverse = load_local_city_map()
    if city_input in local_map:
        return city_input, local_map[city_input]
    if city_input in local_reverse:
        return local_reverse[city_input], city_input

    # 2. 运行时拉 BOSS 接口
    live_map, live_reverse = load_live_city_maps()
    if city_input in live_map:
        return city_input, live_map[city_input]
    if city_input in live_reverse:
        return live_reverse[city_input], city_input

    # 3. 仍未命中的 9 位纯数字视为用户直接传入的裸 city code
    if re.fullmatch(r"\d{9}", city_input):
        return city_input, city_input

    raise CityResolutionError(
        f"无法解析城市 '{city_input}'：本地城市码表和 BOSS 在线城市接口均未命中。"
        "请传入受支持的中文城市名或 9 位 city code；已停止抓取，"
        "避免将无效城市参数误判为 0 个岗位。"
    )


def list_cities(keyword=None, use_live=True):
    """打印支持的城市列表。keyword 非空时只打印城市名含该关键词的城市。

    优先用运行时拉取的最新码表（use_live=True），拉取失败回退本地静态码表。
    """
    name_to_code = {}
    if use_live:
        live_map, _ = load_live_city_maps()
        name_to_code.update(live_map)
    if not name_to_code:
        local_map, _ = load_local_city_map()
        name_to_code.update(local_map)
    if not name_to_code:
        print("⚠️ 无法加载城市码表（本地静态文件缺失且网络拉取失败）")
        return

    items = sorted(name_to_code.items(), key=lambda kv: kv[0])
    if keyword:
        keyword = keyword.strip()
        items = [(n, c) for n, c in items if keyword in n]
        if not items:
            print(f"没有匹配「{keyword}」的城市")
            return
    print(f"共 {len(items)} 个城市（支持中文城市名或城市码）：")
    for name, code in items:
        print(f"  {name}\t{code}")


# ============================================================
# CSV 导出
# ============================================================
CSV_COLUMNS = [
    "job_id", "title", "salary", "salary_source", "location", "tags", "boss_name",
    "boss_active_status",
    "company_scale", "company_stage", "company_industry", "skills",
    "job_link", "welfare",
]

DETAIL_CSV_COLUMNS = [
    "job_id", "title", "company", "salary", "salary_source", "location",
    "boss_active_status", "tags_list", "job_link", "company_link", "skill_tags", "jd",
]


def write_csv(csv_path, jobs):
    """将 jobs 列表写入 CSV 文件"""
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for j in jobs:
            # 确保每列都有值
            row = {col: j.get(col, "") for col in CSV_COLUMNS}
            writer.writerow(row)
    print(f"CSV 已保存: {csv_path}")


def write_detail_csv(csv_path, details):
    """将岗位详情列表写入 CSV 文件"""
    os.makedirs(os.path.dirname(csv_path) or ".", exist_ok=True)
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=DETAIL_CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for d in details:
            row = {col: d.get(col, "") for col in DETAIL_CSV_COLUMNS}
            if isinstance(row.get("skill_tags"), list):
                row["skill_tags"] = " | ".join(row["skill_tags"])
            writer.writerow(row)
    print(f"详情 CSV 已保存: {csv_path}")


# ============================================================
# 增量写入 JSON
# ============================================================
def write_json_atomic(path, data):
    """Write JSON beside the target and atomically replace the old file."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".boss-json-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def append_json(path, new_jobs):
    """追加 jobs 到 JSON 文件，每条按 job_id 去重"""
    existing = []
    seen_ids = set()
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            existing = data.get("jobs", [])
            seen_ids = {j.get("job_id", "") for j in existing}
        except (json.JSONDecodeError, OSError, ValueError):
            data = {}
    added = 0
    for j in new_jobs:
        if j.get("job_id") not in seen_ids:
            existing.append(j)
            seen_ids.add(j.get("job_id", ""))
            added += 1
    data["jobs"] = existing
    write_json_atomic(path, data)
    return added


def flush_jobs(path, meta, jobs):
    """每次有新数据就全量刷写（jobs 去重后），保证异常退出也能保留"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    # 合并已有文件
    existing_jobs = []
    seen_ids = set()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                old = json.load(f)
            existing_jobs = old.get("jobs", [])
            seen_ids = {j.get("job_id", "") for j in existing_jobs}
        except (json.JSONDecodeError, OSError, ValueError):
            pass
    for j in jobs:
        if j.get("job_id") not in seen_ids:
            existing_jobs.append(j)
            seen_ids.add(j.get("job_id", ""))
    meta["total"] = len(existing_jobs)
    meta["jobs"] = existing_jobs
    write_json_atomic(path, meta)


# ============================================================
# 构建搜索 URL
# ============================================================
def build_search_url(keyword, city_code, page, filters):
    params = {"query": keyword, "city": city_code, "page": page}
    for key, code in filters.items():
        if code:
            params[key] = code
    return f"https://www.zhipin.com/web/geek/job?{urlencode(params)}"


def should_use_dom_fallback(jobs, allow_dom_fallback=False):
    return allow_dom_fallback and not jobs


def parse_api_jobs_eval_value(value):
    if not value:
        return []
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []

    jobs = []
    for item in parsed:
        if not isinstance(item, dict) or item.get("error"):
            continue
        if item.get("title") or item.get("job_link"):
            jobs.append(item)
    return jobs


def build_detail_url(job):
    """Build the URL used for detail navigation without mutating job_link."""
    link = job.get("job_link", "")
    if not link:
        return ""

    parsed = urlparse(link)
    params = parse_qsl(parsed.query, keep_blank_values=True)
    existing_keys = {key for key, _ in params}
    for query_key, job_key in (("lid", "lid"), ("securityId", "security_id")):
        value = job.get(job_key) or job.get(query_key) or ""
        if value and query_key not in existing_keys:
            params.append((query_key, value))
            existing_keys.add(query_key)

    return urlunparse(parsed._replace(query=urlencode(params)))

# ============================================================
# 抓取列表
# ============================================================
def scrape_list(keyword, city_input, max_pages, filters, output_path,
                cdp_port=DEFAULT_CDP_PORT, fmt="json", allow_dom_fallback=False):
    city_name, city_code = resolve_city(city_input)
    cdp = CDPSession(cdp_port)
    all_jobs = []
    seen = set()
    stream_mode = (output_path == "-")
    if not output_path:
        output_path = default_output_path("jobs")

    # 显示筛选条件
    filter_desc = []
    if filters.get("scale"):
        for k, v in SCALE_MAP.items():
            if v == filters["scale"]:
                filter_desc.append(f"规模={k}")
    if filters.get("stage"):
        for k, v in STAGE_MAP.items():
            if v == filters["stage"]:
                filter_desc.append(f"融资={k}")
    if filters.get("salary"):
        for k, v in SALARY_MAP.items():
            if v == filters["salary"]:
                filter_desc.append(f"薪资={k}")
    if filters.get("experience"):
        for k, v in EXPERIENCE_MAP.items():
            if v == filters["experience"]:
                filter_desc.append(f"经验={k}")
    if filters.get("degree"):
        for k, v in DEGREE_MAP.items():
            if v == filters["degree"]:
                filter_desc.append(f"学历={k}")
    if filters.get("industry"):
        for k, v in INDUSTRY_MAP.items():
            if v == filters["industry"]:
                filter_desc.append(f"行业={k}")

    print(f"=== BOSS直聘抓取 ===")
    print(f"关键词: {keyword} | 城市: {city_name} | 页数: {max_pages}")
    if filter_desc:
        print(f"筛选: {' | '.join(filter_desc)}")
    print()

    tid, sid = create_page_session(cdp)
    cdp.send("Network.enable", {}, sid)

    try:
        for pg in range(1, max_pages + 1):
            print(f"--- [{pg}/{max_pages} 页, {len(all_jobs)} 条已抓] ---")
            incr_request()

            # Navigate to the target search page and capture the page's own
            # joblist XHR/fetch response. Do not inject a second synchronous XHR.
            url = build_search_url(keyword, city_code, pg, filters)
            cdp.send("Page.navigate", {"url": url}, sid)
            jobs = wait_for_native_joblist_response(cdp, sid)

            # DOM 提取的薪资可能是加密字体，默认禁用；只有显式允许时才降级。
            if should_use_dom_fallback(jobs, allow_dom_fallback):
                log.warning("⚠️ API 获取失败，回退到 DOM 提取（此方式已弃用，数据可能不完整）")
                val = cdp.eval_js(EXTRACT_LIST_JS, sid)
                if val:
                    try:
                        jobs = json.loads(val) if isinstance(val, str) else val
                    except (json.JSONDecodeError, ValueError):
                        print(f"  ⚠️ JSON 解析失败")
                        jobs = []
            elif not jobs:
                log.warning("⚠️ API 未返回职位数据，已跳过 DOM fallback；如需强制降级可加 --allow-dom-fallback")

            if not jobs:
                print("  ⚠️ 无数据")
                continue

            new = 0
            for j in jobs:
                key = j.get('job_link') or j['title']
                j['job_id'] = hashlib.md5(key.encode()).hexdigest()[:16]
                if key in seen:
                    continue
                seen.add(key)
                all_jobs.append(j)
                new += 1
                salary = j.get('salary','?')
                scale = j.get('company_scale', '')
                active = j.get('boss_active_status', '')
                extra = f" | {scale}" if scale else ""
                if active:
                    extra += f" | {active}"
                print(f"  ✓ {j['title']} | {salary} | {j.get('location','')} | {j.get('boss_name','')}{extra}")

            print(f"  本页 {len(jobs)} 条, 新增 {new}, 累计 {len(all_jobs)}")

            # 每页抓完就写入文件，异常退出也能保留
            if output_path and not stream_mode:
                flush_jobs(output_path, {
                    "keyword": keyword,
                    "city": city_name,
                    "filters": filters,
                    "filter_desc": filter_desc,
                    "scraped_at": datetime.now().isoformat(),
                }, all_jobs)

            if pg < max_pages:
                d = random.uniform(8, 15)
                print(f"  翻页等待 {d:.0f}s...\n")
                time.sleep(d)

    except KeyboardInterrupt:
        print("\n中断")
    except BossAPIError:
        raise
    except RuntimeError as e:
        print(f"\n⚠️ {e}")
    finally:
        try:
            cdp.send("Target.closeTarget", {"targetId": tid})
        except (KeyError, websocket.WebSocketException, TimeoutError):
            log.debug("关闭搜索 target 失败", exc_info=True)
        try:
            cdp.close()
        except websocket.WebSocketException:
            log.debug("关闭搜索 CDP 连接失败", exc_info=True)

    print(f"\n{'='*60}")
    print(f"完成: {len(all_jobs)} 条")

    # 把 job_link 升级为 detail 可直接用的完整地址（自动补 lid/securityId 参数）
    for job in all_jobs:
        if job.get("job_link"):
            job["job_link"] = build_detail_url(job)

    if all_jobs:
        if not stream_mode:
            # 最终写入（含时间戳更新）
            flush_jobs(output_path, {
                "keyword": keyword,
                "city": city_name,
                "filters": filters,
                "filter_desc": filter_desc,
                "scraped_at": datetime.now().isoformat(),
            }, all_jobs)
            print(f"已保存: {output_path}")

            # CSV 导出
            if fmt == "csv":
                csv_path = output_path.rsplit(".", 1)[0] + ".csv"
                write_csv(csv_path, all_jobs)
    else:
        print("无数据")

    return {"keyword": keyword, "city": city_name, "total": len(all_jobs), "jobs": all_jobs}


def scrape_homepage(homepage_url, output_path, cdp_port=DEFAULT_CDP_PORT,
                    capture_seconds=15):
    """Capture jobs from the homepage's own native JSON responses."""
    parsed = urlparse(homepage_url)
    if parsed.scheme != "https" or parsed.netloc not in {"www.zhipin.com", "zhipin.com"}:
        raise ValueError("--homepage-url 必须是 https://www.zhipin.com/ 下的地址")

    cdp = CDPSession(cdp_port)
    stream_mode = (output_path == "-")
    if not output_path:
        output_path = default_output_path("homepage")

    print("=== BOSS直聘首页岗位 ===")
    print(f"首页: {homepage_url}")
    print(f"捕获窗口: {capture_seconds} 秒\n")

    tid, sid = create_page_session(cdp)
    cdp.send("Network.enable", {}, sid)
    try:
        incr_request()
        cdp.send("Page.navigate", {"url": homepage_url}, sid)
        raw_jobs, sources = wait_for_homepage_job_responses(
            cdp, sid, timeout=capture_seconds,
        )
    finally:
        try:
            cdp.send("Target.closeTarget", {"targetId": tid})
        except (KeyError, websocket.WebSocketException, TimeoutError):
            log.debug("关闭首页 target 失败", exc_info=True)
        try:
            cdp.close()
        except websocket.WebSocketException:
            log.debug("关闭首页 CDP 连接失败", exc_info=True)

    deduped = []
    by_key = {}
    for job in raw_jobs:
        key = job.get("encrypt_job_id") or job.get("job_link") or (
            f"{job.get('boss_name', '')}|{job.get('title', '')}|{job.get('location', '')}"
        )
        if not key.strip("|"):
            continue
        section = job.pop("homepage_section", "other")
        source_path = job.pop("homepage_source_path", "")
        response_url = job.pop("homepage_response_url", "")
        if key in by_key:
            existing = by_key[key]
            if section not in existing["homepage_sections"]:
                existing["homepage_sections"].append(section)
            source = {"section": section, "json_path": source_path,
                      "response_url": response_url}
            if source not in existing["homepage_sources"]:
                existing["homepage_sources"].append(source)
            continue
        job["homepage_sections"] = [section]
        job["homepage_sources"] = [{
            "section": section,
            "json_path": source_path,
            "response_url": response_url,
        }]
        job["job_id"] = hashlib.md5(key.encode()).hexdigest()[:16]
        if job.get("job_link"):
            job["job_link"] = build_detail_url(job)
        by_key[key] = job
        deduped.append(job)

    sections = {"selected": [], "latest": [], "other": []}
    for job in deduped:
        for section in job.get("homepage_sections", ["other"]):
            sections.setdefault(section, []).append(job)

    result = {
        "mode": "homepage",
        "homepage_url": homepage_url,
        "scraped_at": datetime.now().isoformat(),
        "total": len(deduped),
        "section_counts": {key: len(value) for key, value in sections.items()},
        "sections": sections,
        "sources": sources,
        "jobs": deduped,
    }

    for section_name in ("selected", "latest", "other"):
        section_jobs = sections.get(section_name) or []
        if not section_jobs:
            continue
        print(f"--- {section_name}: {len(section_jobs)} 条 ---")
        for job in section_jobs:
            print(
                f"  ✓ {job.get('title', '')} | {job.get('salary', '')} | "
                f"{job.get('location', '')} | {job.get('boss_name', '')}"
            )

    print(f"\n完成: {len(deduped)} 条；原生岗位列表来源 {len(sources)} 个")
    if not stream_mode:
        write_json_atomic(output_path, result)
        print(f"已保存: {output_path}")
    return result


def discover_inbox_endpoints(inbox_url, cdp_port=DEFAULT_CDP_PORT,
                             capture_seconds=12):
    """Read-only inbox endpoint discovery; never returns conversation values."""
    parsed = urlparse(inbox_url)
    if parsed.scheme != "https" or parsed.netloc not in {"www.zhipin.com", "zhipin.com"}:
        raise ValueError("--inbox-url 必须是 https://www.zhipin.com/ 下的地址")

    cdp = CDPSession(cdp_port)
    tid, sid = create_page_session(cdp)
    cdp.send("Network.enable", {}, sid)
    try:
        incr_request()
        cdp.send("Page.navigate", {"url": inbox_url}, sid)
        endpoints, websocket_frames = wait_for_inbox_endpoint_metadata(
            cdp, sid, timeout=capture_seconds,
        )
    finally:
        try:
            cdp.send("Target.closeTarget", {"targetId": tid})
        except (KeyError, websocket.WebSocketException, TimeoutError):
            log.debug("关闭收件箱发现 target 失败", exc_info=True)
        try:
            cdp.close()
        except websocket.WebSocketException:
            log.debug("关闭收件箱发现 CDP 连接失败", exc_info=True)

    return {
        "mode": "inbox-discover",
        "inbox_url": inbox_url,
        "scraped_at": datetime.now().isoformat(),
        "endpoint_count": len(endpoints),
        "endpoints": endpoints,
        "websocket_frame_count": len(websocket_frames),
        "websocket_frames": websocket_frames,
        "privacy": "只返回接口路径、查询键名和 JSON/WebSocket 字段结构；不返回联系人、消息预览或正文。",
    }


def scrape_inbox(inbox_url, output_path, cdp_port=DEFAULT_CDP_PORT,
                 capture_seconds=15):
    """Return inbox progress metadata, excluding recruiter name and message content."""
    parsed = urlparse(inbox_url)
    if parsed.scheme != "https" or parsed.netloc not in {"www.zhipin.com", "zhipin.com"}:
        raise ValueError("--inbox-url 必须是 https://www.zhipin.com/ 下的地址")

    cdp = CDPSession(cdp_port)
    stream_mode = (output_path == "-")
    if not output_path:
        output_path = default_output_path("inbox")
    tid, sid = create_page_session(cdp)
    cdp.send("Network.enable", {}, sid)
    try:
        incr_request()
        cdp.send("Page.navigate", {"url": inbox_url}, sid)
        conversations = wait_for_native_inbox_list(
            cdp, sid, timeout=capture_seconds,
        )
    finally:
        try:
            cdp.send("Target.closeTarget", {"targetId": tid})
        except (KeyError, websocket.WebSocketException, TimeoutError):
            log.debug("关闭收件箱 target 失败", exc_info=True)
        try:
            cdp.close()
        except websocket.WebSocketException:
            log.debug("关闭收件箱 CDP 连接失败", exc_info=True)

    unread_total = sum(
        count for count in (item.get("unread_count") for item in conversations)
        if isinstance(count, int)
    )
    result = {
        "mode": "inbox",
        "inbox_url": inbox_url,
        "scraped_at": datetime.now().isoformat(),
        "conversation_total": len(conversations),
        "unread_total": unread_total,
        "conversations": conversations,
        "privacy": "不输出招聘者姓名、头像、消息预览或消息正文。",
    }
    if not stream_mode:
        write_json_atomic(output_path, result)
        print(f"收件箱: {len(conversations)} 个会话，未读 {unread_total} 条")
        print(f"已保存: {output_path}")
    return result


# This extractor is intentionally scoped to the already selected conversation.
# It does not click, scroll, navigate, or listen for future frames.  BOSS uses
# changing CSS-module class names, so it detects common message/bubble tokens
# and reports only the items already rendered in the visible conversation pane.
EXTRACT_ACTIVE_INBOX_JS = r"""
(function(){
  function clean(value, limit) {
    value = String(value || '').replace(/\s+/g, ' ').trim();
    return value.length > limit ? value.slice(0, limit) + '…' : value;
  }
  function visible(el) {
    var style = window.getComputedStyle(el);
    var rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  }
  function classTrail(el) {
    var classes = [];
    for (var node = el; node && node !== document.body; node = node.parentElement) {
      if (typeof node.className === 'string' && node.className) classes.push(node.className);
    }
    return classes.join(' ');
  }
  function messageAncestor(el) {
    for (var node = el; node && node !== document.body; node = node.parentElement) {
      var name = typeof node.className === 'string' ? node.className : '';
      if (/(^|[-_ ])(?:message|msg|bubble|chat-item|conversation-item)(?:[-_ ]|$)/i.test(name)) return node;
    }
    return null;
  }
  var expected = __EXPECTED_CONTACT_JSON__;
  var roots = [];
  var seenRoot = new Set();
  var all = Array.prototype.slice.call(document.querySelectorAll('body *'));
  // A name in the left-side contact list must not authorize reading the active
  // pane. Verify the expected contact occurs in the visible main-header band.
  var headerMatches = [];
  for (var h = 0; h < all.length; h++) {
    var headerNode = all[h];
    if (!visible(headerNode)) continue;
    var headerText = clean(headerNode.innerText, 240);
    if (!expected || headerText.indexOf(expected) < 0) continue;
    var headerRect = headerNode.getBoundingClientRect();
    if (headerRect.left < window.innerWidth * 0.28 || headerRect.top < 0 || headerRect.top > 180) continue;
    headerMatches.push({text: headerText, top: Math.round(headerRect.top), left: Math.round(headerRect.left)});
  }
  headerMatches.sort(function(a, b) {
    return (a.top - b.top) || (a.text.length - b.text.length);
  });
  // BOSS uses `.message-item` for one logical history row. Prefer this exact
  // class token so nested bubbles/cards and the left-side `last-msg` preview
  // cannot be reported as separate messages. Fall back only on older markup.
  for (var i = 0; i < all.length; i++) {
    var candidate = all[i];
    if (!visible(candidate)) continue;
    if (candidate.classList && candidate.classList.contains('message-item') && !seenRoot.has(candidate)) {
      seenRoot.add(candidate);
      roots.push(candidate);
    }
  }
  if (!roots.length) {
    for (var f = 0; f < all.length; f++) {
      var fallback = all[f];
      if (!visible(fallback)) continue;
      var root = messageAncestor(fallback);
      if (root && !(root.classList && root.classList.contains('last-msg')) && !seenRoot.has(root)) {
        seenRoot.add(root);
        roots.push(root);
      }
    }
  }
  var entries = [];
  var seen = new Set();
  for (var j = 0; j < roots.length; j++) {
    var root = roots[j];
    var text = clean(root.innerText, 1000);
    var images = root.querySelectorAll('img').length;
    var links = root.querySelectorAll('a[href]').length;
    var key = text + '|' + images + '|' + links + '|' + (root.className || '');
    if (seen.has(key)) continue;
    seen.add(key);
    var rootClasses = String(root.className || '');
    var type = 'non_text';
    if (/message-card/i.test(root.innerHTML || '')) type = 'platform_card';
    else if (/item-system/i.test(rootClasses)) type = 'system_event';
    else if (text) type = /item-(?:self|myself)/i.test(rootClasses) ? 'outgoing_text' : 'incoming_text';
    else if (images) type = 'image_or_attachment';
    entries.push({
      type: type,
      text: text,
      image_count: images,
      link_count: links,
      class_hint: clean(rootClasses, 180)
    });
  }
  var controls = [];
  for (var k = 0; k < all.length; k++) {
    var control = all[k];
    if (!visible(control)) continue;
    var tag = control.tagName ? control.tagName.toLowerCase() : '';
    if (tag === 'textarea' || control.getAttribute('contenteditable') === 'true') {
      controls.push({tag: tag || 'contenteditable', placeholder: clean(control.getAttribute('placeholder'), 120)});
    }
  }
  return JSON.stringify({
    expected_contact: expected,
    expected_contact_in_active_header: headerMatches.length > 0,
    active_header: headerMatches.length ? headerMatches[0] : null,
    rendered_message_count: entries.length,
    rendered_entries: entries,
    composer_controls: controls
  });
})()
"""


EXTRACT_ACTIVE_INBOX_HEADER_JS = r"""
(function(){
  function clean(value, limit) {
    value = String(value || '').replace(/\s+/g, ' ').trim();
    return value.length > limit ? value.slice(0, limit) + '…' : value;
  }
  function visible(el) {
    var style = window.getComputedStyle(el);
    var rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  }
  var expected = __EXPECTED_CONTACT_JSON__;
  var all = Array.prototype.slice.call(document.querySelectorAll('body *'));
  var matches = [];
  for (var i = 0; i < all.length; i++) {
    var node = all[i];
    if (!visible(node)) continue;
    var text = clean(node.innerText, 240);
    if (!expected || text.indexOf(expected) < 0) continue;
    var rect = node.getBoundingClientRect();
    if (rect.left < window.innerWidth * 0.28 || rect.top < 0 || rect.top > 180) continue;
    matches.push({text: text, top: Math.round(rect.top), left: Math.round(rect.left)});
  }
  matches.sort(function(a, b) { return (a.top - b.top) || (a.text.length - b.text.length); });
  return JSON.stringify({matches: matches});
})()
"""


COUNT_ACTIVE_OUTGOING_TEXT_JS = r"""
(function(){
  var expected = __MESSAGE_JSON__;
  var nodes = Array.prototype.slice.call(document.querySelectorAll('.message-item.item-myself, .message-item.item-self'));
  var count = 0;
  for (var i = 0; i < nodes.length; i++) {
    if ((nodes[i].innerText || '').replace(/\s+/g, ' ').trim() === expected) count++;
  }
  return count;
})()
"""


def verify_active_inbox_header(cdp, sid, expected_contact):
    """Return the visible main-pane header for one explicitly named contact."""
    script = EXTRACT_ACTIVE_INBOX_HEADER_JS.replace(
        "__EXPECTED_CONTACT_JSON__", json.dumps(str(expected_contact), ensure_ascii=False),
    )
    raw = cdp.eval_js(script, sid)
    if not isinstance(raw, str):
        raise RuntimeError("未能读取当前主消息区标题")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError("当前主消息区标题格式异常") from exc
    matches = payload.get("matches") or []
    if not matches:
        raise RuntimeError(
            f"主消息区标题未显示“{expected_contact}”；为避免误操作，已停止"
        )
    return matches[0]


def count_active_outgoing_text(cdp, sid, message):
    script = COUNT_ACTIVE_OUTGOING_TEXT_JS.replace(
        "__MESSAGE_JSON__", json.dumps(str(message), ensure_ascii=False),
    )
    value = cdp.eval_js(script, sid)
    return value if isinstance(value, int) else 0


def send_active_inbox_text(expected_contact, message, confirmed=False,
                           cdp_port=DEFAULT_CDP_PORT):
    """Send one explicitly confirmed text to the manually selected conversation.

    The command deliberately has no recipient search, no queue, no retry, and
    no scheduling. A failed post-send visibility check never triggers a resend.
    """
    if not confirmed:
        raise ValueError("实际发送必须显式传入 --confirm-send")
    if not expected_contact or not str(expected_contact).strip():
        raise ValueError("--expect-contact 必填")
    if not isinstance(message, str) or not message.strip():
        raise ValueError("--message 必须是非空的精确发送文案")
    if len(message) > 500:
        raise ValueError("单条测试消息最多 500 个字符")

    cdp = CDPSession(cdp_port)
    tid = sid = None
    try:
        tid, sid = attach_active_inbox_target(cdp)
        header = verify_active_inbox_header(cdp, sid, expected_contact)
        before_count = count_active_outgoing_text(cdp, sid, message)
        document = cdp.send("DOM.getDocument", {"depth": 1, "pierce": True}, sid)
        root_id = ((document.get("result") or {}).get("root") or {}).get("nodeId")
        if not root_id:
            raise RuntimeError("未能定位当前消息页 DOM 根节点")
        selected = cdp.send(
            "DOM.querySelector", {"nodeId": root_id, "selector": '[contenteditable="true"]'}, sid,
        )
        composer_id = (selected.get("result") or {}).get("nodeId")
        if not composer_id:
            raise RuntimeError("未找到消息输入框；未发送")
        cdp.send("DOM.focus", {"nodeId": composer_id}, sid)
        cdp.send("Input.insertText", {"text": message}, sid)
        cdp.send("Input.dispatchKeyEvent", {
            "type": "keyDown", "key": "Enter", "code": "Enter",
            "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
        }, sid)
        cdp.send("Input.dispatchKeyEvent", {
            "type": "keyUp", "key": "Enter", "code": "Enter",
            "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13,
        }, sid)
        time.sleep(1.0)
        after_count = count_active_outgoing_text(cdp, sid, message)
        return {
            "mode": "inbox-send-active",
            "recipient": str(expected_contact),
            "active_header": header,
            "message": message,
            "submitted": True,
            "post_send_visible": after_count > before_count,
            "scope": "仅当前已选会话；单次显式确认发送；未切换会话、未重试",
            "sent_at": datetime.now().isoformat(),
        }
    finally:
        if tid is not None:
            try:
                cdp.send("Target.detachFromTarget", {"sessionId": sid})
            except (KeyError, websocket.WebSocketException, TimeoutError):
                log.debug("脱离活动收件箱 target 失败", exc_info=True)
        try:
            cdp.close()
        except websocket.WebSocketException:
            log.debug("关闭发送收件箱 CDP 连接失败", exc_info=True)


def read_active_inbox_conversation(expected_contact, cdp_port=DEFAULT_CDP_PORT,
                                   max_entries=80):
    """Read only the currently rendered, user-selected inbox conversation.

    This is an explicit, non-default operation. It attaches to the open inbox
    page rather than creating/navigating a target, verifies the expected name
    is visible, and never triggers UI input or a network action.
    """
    if not expected_contact or not str(expected_contact).strip():
        raise ValueError("--expect-contact 必填，用于确认当前选中的会话")
    max_entries = max(1, min(200, int(max_entries)))
    cdp = CDPSession(cdp_port)
    tid = sid = None
    try:
        tid, sid = attach_active_inbox_target(cdp)
        script = EXTRACT_ACTIVE_INBOX_JS.replace(
            "__EXPECTED_CONTACT_JSON__", json.dumps(str(expected_contact), ensure_ascii=False),
        )
        raw = cdp.eval_js(script, sid)
        if not isinstance(raw, str):
            raise RuntimeError("未能从当前消息页读取可见会话内容")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise RuntimeError("当前消息页返回的会话内容格式异常") from exc
        if not payload.get("expected_contact_in_active_header"):
            raise RuntimeError(
                f"主消息区标题未显示“{expected_contact}”；为避免读取错误对象，已停止"
            )
        entries = payload.get("rendered_entries") or []
        if not isinstance(entries, list):
            entries = []
        entries = entries[:max_entries]
        type_counts = Counter(
            str(entry.get("type") or "unknown")
            for entry in entries if isinstance(entry, dict)
        )
        return {
            "mode": "inbox-read-active",
            "expected_contact": str(expected_contact),
            "scope": "仅当前已选会话、仅页面已渲染内容；未切换、未滚动、未发送",
            "active_header": payload.get("active_header"),
            "rendered_message_count": len(entries),
            "message_type_counts": dict(type_counts),
            "messages": entries,
            "composer_controls": payload.get("composer_controls") or [],
            "scraped_at": datetime.now().isoformat(),
        }
    finally:
        if tid is not None:
            try:
                cdp.send("Target.detachFromTarget", {"sessionId": sid})
            except (KeyError, websocket.WebSocketException, TimeoutError):
                log.debug("脱离活动收件箱 target 失败", exc_info=True)
        try:
            cdp.close()
        except websocket.WebSocketException:
            log.debug("关闭活动收件箱 CDP 连接失败", exc_info=True)


# ============================================================
# 抓取详情
# ============================================================
def build_detail_record(job, extracted):
    link = job.get("job_link", "")
    boss_active_status = resolve_boss_active_status(
        list_status=job.get("boss_active_status", ""),
        detail_status=extracted.get("boss_active_status", ""),
    )
    return {
        "job_id": job.get("job_id", ""),
        "title": job.get("title") or extracted.get("title", ""),
        "company": job.get("boss_name") or extracted.get("company", ""),
        "salary": job.get("salary") or extracted.get("salary", ""),
        "salary_source": job.get("salary_source") or (
            "detail_dom" if extracted.get("salary") else ""
        ),
        "location": job.get("location") or extracted.get("location", ""),
        "boss_active_status": boss_active_status,
        "tags_list": job.get("tags") or " | ".join(extracted.get("tags", [])),
        "job_link": link,
        "link": link,
        "skill_tags": extracted.get("tags", []),
        "company_link": job.get("company_link") or extracted.get("company_link", ""),
        "jd": extracted.get("jd", ""),
    }


def scrape_details(list_data, max_details=None, output_path=None,
                   cdp_port=DEFAULT_CDP_PORT, fmt="json", on_detail=None):
    jobs = list_data.get("jobs", [])
    if max_details:
        jobs = jobs[:max_details]
    stream_mode = (output_path == "-")
    if not output_path:
        output_path = default_output_path("details")

    print(f"\n=== 抓取岗位详情 ({len(jobs)} 个) ===\n")
    results = []
    seen_links = set()

    for idx, job in enumerate(jobs):
        link = job.get("job_link", "")
        title = job.get("title", "")
        company = job.get("boss_name", "")
        if not link:
            continue

        # 按 link 去重
        if link in seen_links:
            print(f"[{idx+1}/{len(jobs)}] 跳过重复: {company} - {title}")
            continue
        seen_links.add(link)

        t0 = time.time()
        print(f"[{idx+1}/{len(jobs)}] {company} - {title}")

        incr_request()

        # 每个详情页用新 session 避免检测；自动化 target 默认后台创建。
        ws = CDPSession(cdp_port)
        tid, sid = create_page_session(ws)

        detail_url = build_detail_url(job)
        record_job = dict(job)
        record_job["job_link"] = detail_url
        ws.send("Page.navigate", {"url": detail_url}, sid)
        print(f"  加载页面...")
        time.sleep(random.uniform(5, 8))

        print(f"  提取 JD...")
        d = None
        for _attempt in range(3):
            val = ws.eval_js(EXTRACT_DETAIL_JS, sid)
            try:
                candidate = json.loads(val) if isinstance(val, str) else None
            except (json.JSONDecodeError, ValueError, TypeError):
                candidate = None
            page_text = candidate.get("page_text", "") if candidate else ""
            if candidate and (candidate.get("jd") or "职位描述" in page_text):
                d = candidate
                break
            if _attempt < 2:
                # 仅在首次提取未拿到职位描述时补一次短滚动，避免每个
                # 详情页固定执行 3–7 次 Runtime.evaluate。
                delta = random.randint(250, 500)
                ws.eval_js(f"window.scrollBy(0,{delta})", sid)
                time.sleep(random.uniform(0.8, 1.5))
        if d is None:
            d = {"jd": "", "tags": []}

        try:
            fields = extract_detail_fields(d)
            for field in ("jd", "title", "company", "salary", "location", "company_link", "tags"):
                d[field] = fields[field]
            d["boss_active_status"] = resolve_boss_active_status(
                list_status=job.get("boss_active_status", ""),
                detail_status=fields["boss_active_status"],
            )
        except DetailLoginRequiredError as exc:
            ws.send("Target.closeTarget", {"targetId": tid})
            ws.close()
            raise RuntimeError(
                "BOSS detail login expired; stopped before writing truncated JD data"
            ) from exc
        except DetailExtractionError as exc:
            print(f"  跳过无效详情页: {exc}")
            ws.send("Target.closeTarget", {"targetId": tid})
            ws.close()
            continue

        detail = build_detail_record(record_job, d)
        results.append(detail)

        if d.get("tags"):
            print(f"  技能: {', '.join(d['tags'])}")
        if d.get("boss_active_status"):
            print(f"  活跃: {d['boss_active_status']}")
        print(f"  JD: {len(d.get('jd',''))} 字 ({time.time()-t0:.0f}s)")

        # 每抓完一个详情就写入，异常退出也能保留
        if output_path and not stream_mode:
            write_json_atomic(output_path, results)
        if on_detail is not None:
            on_detail(detail)

        ws.send("Target.closeTarget", {"targetId": tid})
        ws.close()
        remaining_jobs = any(
            item.get("job_link") and item.get("job_link") not in seen_links
            for item in jobs[idx + 1:]
        )
        if remaining_jobs:
            gap = random.uniform(8, 15)
            print(f"  等待 {gap:.0f}s 后抓下一个...\n")
            time.sleep(gap)

    if not stream_mode:
        # 最终保存（dirname 为空时回退到当前目录，与循环内/其它写文件处保持一致）
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n详情已保存: {output_path}")

        if fmt == "csv":
            csv_path = output_path.rsplit(".", 1)[0] + ".csv"
            write_detail_csv(csv_path, results)
    return results


# ============================================================
# 动态技术术语提取（供 job_summary.py 复用）
# ============================================================
def extract_tech_terms_from_jds(details, search_keyword=""):
    """从 JD 文本中提取基础、搜索词和高频技术术语。"""
    base_tech_terms = [
        "Java", "Spring", "Redis", "MySQL", "Kafka", "Flink", "Spark",
        "Go", "Python", "微服务", "分布式", "高并发",
        "AI", "LLM", "RAG", "Agent", "SQL", "Linux",
    ]
    keyword_terms = [
        word.strip()
        for word in re.split(r"[\s,，、]+", search_keyword)
        if len(word.strip()) >= 2
    ]
    word_freq = Counter()
    stop_words = {
        "任职", "要求", "岗位", "职责", "描述", "优先", "具有",
        "负责", "相关", "经验", "能力", "以上", "及其", "工作",
        "开发", "团队", "项目", "公司", "业务", "熟悉", "熟练",
        "了解", "掌握", "参与", "完成", "进行", "能够", "学历",
        "专业", "提供", "福利", "加入", "我们", "我们只", "是通过",
        "就是", "已经", "可以", "这个", "那个", "什么", "怎么",
        "欢迎", "期待", "为你", "为你提供",
    }
    for detail in details:
        jd_text = str(detail.get("jd") or "") if isinstance(detail, dict) else ""
        if not jd_text:
            continue
        for word in re.findall(r"\b[A-Za-z][A-Za-z0-9._-]+\b", jd_text):
            if 2 <= len(word) <= 30:
                word_freq[word] += 1
        for word in re.findall(r"[\u4e00-\u9fff]{2,6}", jd_text):
            if word not in stop_words:
                word_freq[word] += 1

    dynamic_terms = [word for word, count in word_freq.most_common(60) if count >= 2]
    return list(dict.fromkeys(base_tech_terms + keyword_terms + dynamic_terms))


# ============================================================
# 解析 JS eval 返回值
def parse_jobs_eval_value(value):
    if not value:
        return []
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (json.JSONDecodeError, ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


def has_usable_smoke_jobs(jobs):
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if (
            job.get("title")
            and job.get("salary")
            and job.get("salary_source") == "api"
            and job.get("job_link")
        ):
            return True
    return False


def run_smoke_test(cdp_port=DEFAULT_CDP_PORT):
    """Run one native page-network smoke test without writing result files."""
    if not require_runtime_dependencies("requests", "websocket"):
        return 1

    cdp = None
    tid = None
    try:
        cdp = CDPSession(cdp_port)
        city_name, city_code = resolve_city(DEFAULT_CITY_INPUT)
        smoke_query = "AI Agent"
        search_url = build_search_url(smoke_query, city_code, 1, {})
        tid, sid = create_page_session(cdp)

        print(f"打开 BOSS 搜索页: {smoke_query} @ {city_name}")
        cdp.send("Network.enable", {}, sid)
        cdp.send("Page.navigate", {"url": search_url}, sid)
        jobs = wait_for_native_joblist_response(cdp, sid)

        if has_usable_smoke_jobs(jobs):
            sample = next(job for job in jobs if job.get("salary") and job.get("job_link"))
            print(f"✅ Smoke test 通过: {sample.get('title')} | {sample.get('salary')}")
            return 0
        print("❌ Smoke test 未拿到可用职位；请检查登录态或 BOSS API 返回")
        return 1
    except (BossAPIError, requests.ConnectionError, requests.Timeout, KeyError,
            json.JSONDecodeError, websocket.WebSocketException, TimeoutError) as e:
        print(f"❌ Smoke test 失败: {e}")
        return 1
    finally:
        if cdp is not None:
            if tid is not None:
                try:
                    cdp.send("Target.closeTarget", {"targetId": tid})
                except (KeyError, websocket.WebSocketException, TimeoutError):
                    log.debug("关闭 smoke test target 失败", exc_info=True)
            try:
                cdp.close()
            except websocket.WebSocketException:
                log.debug("关闭 smoke test CDP 连接失败", exc_info=True)


# ============================================================
# --check 环境检查
# ============================================================
def run_check(cdp_port=DEFAULT_CDP_PORT):
    """运行环境诊断检查"""
    print("=" * 50)
    print("  BOSS直聘 CDP 环境检查")
    print("=" * 50)
    print()

    all_pass = True

    # 检查 1: Python 依赖
    print("[1/3] Python 依赖...")
    deps_ok = require_runtime_dependencies("websocket", "requests")
    if requests is not None:
        print(f"  ✅ requests 可导入")
    if websocket is not None:
        print(f"  ✅ websocket 可导入")
    if deps_ok:
        print(f"  ✅ 依赖完整")
    else:
        all_pass = False

    # 检查 2: CDP 端口连通性
    print("[2/3] CDP 端口连通性...")
    if requests is None:
        print(f"  ❌ 跳过 — 缺少 requests")
        all_pass = False
    else:
        try:
            resp = requests.get(f"http://127.0.0.1:{cdp_port}/json/version", timeout=5)
            data = resp.json()
            browser = data.get("Browser", "未知")
            print(f"  ✅ 通过 — Chrome {browser}")
        except (requests.ConnectionError, requests.Timeout):
            print(f"  ❌ 失败 — 无法连接 127.0.0.1:{cdp_port}")
            print(f"     请先启动 Chrome CDP: python3 {__file__} --setup-chrome")
            all_pass = False
        except (json.JSONDecodeError, KeyError) as e:
            print(f"  ❌ 失败 — CDP 响应异常: {e}")
            all_pass = False

    # 检查 3: avoid a BOSS request here. The actual target search validates
    # login and API availability using the page's native network response.
    print("[3/3] BOSS 登录态...")
    print("  ℹ️  未主动探测（避免额外搜索请求）；请直接运行目标搜索")

    print()
    if all_pass:
        print("✅ 所有检查通过，可以开始抓取")
    else:
        print("❌ 部分检查未通过，请修复后重试")
    print()

    return 0 if all_pass else 1


# ============================================================
# --setup-chrome 自动启动
# ============================================================
def prepare_cdp_profile(copy_login_state=False, reset=False):
    """Prepare an isolated persistent Chrome profile for CDP."""
    cdp_data_dir = DEFAULT_CDP_DATA_DIR
    cdp_default = os.path.join(cdp_data_dir, "Default")

    if reset and os.path.exists(cdp_data_dir):
        shutil.rmtree(cdp_data_dir)

    os.makedirs(cdp_default, exist_ok=True)

    copied = 0
    if copy_login_state:
        default_profile = DEFAULT_PROFILE_DIR
        default_default = os.path.join(default_profile, "Default")
        cookie_files = []
        for rel_dir in ("", "Network"):
            for name in ("Cookies", "Cookies-journal", "Cookies-wal", "Cookies-shm"):
                rel_path = os.path.join(rel_dir, name) if rel_dir else name
                cookie_files.append((os.path.join(default_default, rel_path), os.path.join(cdp_default, rel_path)))

        copy_files = [(os.path.join(default_profile, "Local State"), os.path.join(cdp_data_dir, "Local State"))]
        copy_files.extend(cookie_files)
        for src, dst in copy_files:
            if os.path.exists(src):
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                    copied += 1
                except Exception as e:
                    print(f"  ⚠️  复制 {os.path.basename(src)} 失败: {e}")

    return {
        "path": cdp_data_dir,
        "copied": copied,
        "reset": reset,
        "copy_login_state": copy_login_state,
    }


def is_cdp_ready(cdp_port):
    try:
        resp = requests.get(f"http://127.0.0.1:{cdp_port}/json/version", timeout=2)
        return resp.status_code == 200
    except Exception:
        return False


def is_chrome_command(command):
    lower = (command or "").lower()
    return any(token in lower for token in (
        "google chrome",
        "google-chrome",
        "chromium",
        "chrome.exe",
    ))


def normalize_profile_path(path):
    clean = (path or "").strip("\"'")
    if platform.system() == "Windows":
        return ntpath.normcase(ntpath.normpath(clean))
    return os.path.realpath(os.path.expanduser(clean))


def extract_user_data_dir(command):
    match = re.search(r"--user-data-dir=(\"[^\"]+\"|'[^']+'|\S+)", command or "")
    if not match:
        return None
    return match.group(1).strip("\"'")


def iter_chrome_process_commands():
    """Return (pid, command line) tuples for Chrome-like browser processes."""
    if platform.system() == "Windows":
        ps_script = (
            "Get-CimInstance Win32_Process -Filter \"name = 'chrome.exe'\" | "
            "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
        )
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_script],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            )
        except Exception:
            return []
        if not r.stdout.strip():
            return []
        try:
            data = json.loads(r.stdout)
        except (json.JSONDecodeError, ValueError):
            # Keep a small fallback for mocked/legacy process providers that
            # return the Unix-style "pid command" format on Windows.
            processes = []
            for line in r.stdout.splitlines():
                try:
                    pid_text, command = line.strip().split(None, 1)
                    pid = int(pid_text)
                except (ValueError, TypeError):
                    continue
                if is_chrome_command(command):
                    processes.append((pid, command))
            return processes
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return []
        processes = []
        for item in data:
            command = item.get("CommandLine") or ""
            if not is_chrome_command(command):
                continue
            try:
                processes.append((int(item.get("ProcessId")), command))
            except (TypeError, ValueError):
                continue
        return processes

    try:
        r = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
        )
    except Exception:
        return []

    processes = []
    for line in r.stdout.splitlines():
        if not is_chrome_command(line):
            continue
        try:
            pid_text, command = line.strip().split(None, 1)
            pid = int(pid_text)
        except ValueError:
            continue
        processes.append((pid, command))
    return processes


def chrome_pids_for_user_data_dir(user_data_dir):
    """Return Chrome PIDs using the given user-data-dir."""
    pids = []
    real_dir = normalize_profile_path(user_data_dir)
    for pid, command in iter_chrome_process_commands():
        if "--user-data-dir=" not in command:
            continue
        path = extract_user_data_dir(command)
        if path and normalize_profile_path(path) == real_dir:
            pids.append(pid)
    return pids


def chrome_user_data_dirs_for_cdp_port(cdp_port):
    """Return user-data-dir paths for Chrome processes using the given CDP port."""
    dirs = []
    port_arg = f"--remote-debugging-port={cdp_port}"
    for _pid, command in iter_chrome_process_commands():
        if port_arg not in command:
            continue
        path = extract_user_data_dir(command)
        if path:
            dirs.append(path)
    return dirs


def cdp_port_uses_profile(cdp_port, cdp_data_dir):
    expected = normalize_profile_path(cdp_data_dir)
    return any(normalize_profile_path(path) == expected for path in chrome_user_data_dirs_for_cdp_port(cdp_port))


def terminate_process(pid, force=False):
    if platform.system() == "Windows":
        cmd = ["taskkill", "/PID", str(pid), "/T"]
        if force:
            cmd.append("/F")
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        return
    os.kill(pid, signal.SIGKILL if force else signal.SIGTERM)


def stop_cdp_chrome(cdp_data_dir):
    """Stop only Chrome processes that use the scraper's isolated profile."""
    pids = chrome_pids_for_user_data_dir(cdp_data_dir)
    if not pids:
        return 0

    for pid in pids:
        try:
            terminate_process(pid, force=False)
        except ProcessLookupError:
            pass
    for _ in range(10):
        time.sleep(0.5)
        if not chrome_pids_for_user_data_dir(cdp_data_dir):
            return len(pids)

    for pid in chrome_pids_for_user_data_dir(cdp_data_dir):
        try:
            terminate_process(pid, force=True)
        except ProcessLookupError:
            pass
    time.sleep(0.5)
    return len(pids)


def wait_for_cdp(cdp_port, timeout=30):
    print("等待 CDP 可用", end="")
    for _ in range(timeout):
        time.sleep(1)
        print(".", end="", flush=True)
        if is_cdp_ready(cdp_port):
            print(f"\n✅ CDP 已就绪 (端口 {cdp_port})")
            return True
    print(f"\n❌ 等待超时 ({timeout}s)，CDP 未就绪")
    print(f"   请手动检查 Chrome 是否启动，端口 {cdp_port} 是否开放")
    return False


def launch_chrome(cmd):
    kwargs = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if platform.system() == "Windows":
        creationflags = 0
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        if creationflags:
            kwargs["creationflags"] = creationflags
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def run_setup_chrome(cdp_port=DEFAULT_CDP_PORT, copy_login_state=False,
                     reset_profile=False):
    """自动配置并启动 Chrome CDP 模式"""
    if not require_runtime_dependencies("requests"):
        return 1

    print("=" * 50)
    print("  设置 Chrome CDP 调试模式")
    print("=" * 50)
    print()

    profile = prepare_cdp_profile(copy_login_state=copy_login_state, reset=reset_profile)
    cdp_data_dir = profile["path"]
    print(f"✅ 使用独立 Chrome profile: {cdp_data_dir}")
    if reset_profile:
        print("   已按 --reset-chrome-profile 重建 profile")
    if copy_login_state:
        print(f"   已复制 {profile['copied']} 个登录态文件（Local State + Cookie 相关文件）")
    else:
        print("   默认、首次启动、重复启动都不复制主 Chrome Cookie；首次使用请在此专用 Chrome 中登录 zhipin.com")

    if is_cdp_ready(cdp_port):
        if cdp_port_uses_profile(cdp_port, cdp_data_dir):
            print(f"\n✅ CDP 已就绪 (端口 {cdp_port})")
            return 0
        print(f"\n❌ 端口 {cdp_port} 已被其他 Chrome CDP profile 占用")
        print(f"   请关闭旧 CDP Chrome，或改用 --cdp-port 指定其他端口")
        return 1

    stopped = stop_cdp_chrome(cdp_data_dir)
    if stopped:
        print(f"\n已关闭 {stopped} 个旧的 BOSS CDP Chrome 进程")

    print(f"\n启动 Chrome (CDP 端口: {cdp_port})...")
    cmd = [
        DEFAULT_CHROME_PATH,
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={cdp_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-allow-origins=*",
    ]
    launch_chrome(cmd)

    if not wait_for_cdp(cdp_port):
        return 1

    print()
    print("Chrome 已启动。请在这个专用浏览器中登录 zhipin.com。")
    print("程序不会发送登录探测请求；登录完成后请直接运行目标搜索命令。")
    print()
    print(f"示例:")
    print(f"  uv run python3 scripts/boss_cdp_raw.py --keyword \"AI Agent\" --city 上海 --pages 3")
    print(f"  uv run python3 scripts/boss_cdp_raw.py --check")
    print(f"  uv run python3 scripts/boss_cdp_raw.py --stop-chrome   # 抓完关闭专用 Chrome")
    print()
    return 0


def run_stop_chrome():
    """关闭 BOSS 专用 CDP Chrome（按隔离 user-data-dir 精准匹配，不碰主 Chrome）。"""
    if not require_runtime_dependencies("requests"):
        return 1

    print("=" * 50)
    print("  关闭 BOSS 专用 CDP Chrome")
    print("=" * 50)
    print()

    # 只定位 scraper 专用 profile 目录，不复制、不重置
    profile = prepare_cdp_profile(copy_login_state=False, reset=False)
    cdp_data_dir = profile["path"]

    stopped = stop_cdp_chrome(cdp_data_dir)
    if stopped:
        print(f"\n✅ 已关闭 {stopped} 个 BOSS 专用 Chrome 进程 (profile: {cdp_data_dir})")
    else:
        print(f"\nℹ️  没有找到运行中的 BOSS 专用 Chrome 进程 (profile: {cdp_data_dir})")
    print()
    print("提示：仅关闭 scraper 隔离 profile 的 Chrome，不影响你的主 Chrome。")
    print()
    return 0


# ============================================================
# main
# ============================================================

def find_latest_list_file(result_dir=RESULT_DIR):
    """返回默认结果目录下最新的列表 JSON 路径，没有则返回 None"""
    candidates = sorted(
        glob.glob(os.path.join(result_dir, "boss_jobs_*.json")),
        key=os.path.getmtime, reverse=True,
    )
    return candidates[0] if candidates else None


def job_id_from_link(link):
    """从完整 job_link 提取 job_id（/job_detail/xxx.html → xxx），失败返回空串"""
    try:
        path = urlparse(link).path
        base = path.rstrip("/").rsplit("/", 1)[-1]
        if base.endswith(".html"):
            base = base[: -len(".html")]
        return base
    except (ValueError, AttributeError):
        return ""


def filter_jobs_by_ids(list_data, detail_ids):
    """按 job_id 过滤列表，返回 (筛选后的岗位列表, 未匹配的 id 集合)"""
    if isinstance(detail_ids, str):
        wanted = {s.strip() for s in detail_ids.split(",") if s.strip()}
    else:
        wanted = {str(s).strip() for s in detail_ids if str(s).strip()}
    detail_jobs = [
        j for j in list_data.get("jobs", [])
        if str(j.get("job_id", "")) in wanted
    ]
    missing = wanted - {str(j.get("job_id", "")) for j in detail_jobs}
    return detail_jobs, missing

def main():
    p = argparse.ArgumentParser(
        description=f"BOSS直聘抓取 + 分析 (CDP Raw) v{__version__}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
筛选参数示例:
  --scale 305          公司规模 (301=0-20人 302=20-99 303=100-499 304=500-999 305=1000-9999 306=10000+)
  --stage 807          融资阶段 (801=未融资 ... 807=已上市 808=不需要融资)
  --salary 406         薪资范围 (402=3K以下 403=3-5K 404=5-10K 405=10-20K 406=20-50K 407=50K+)
  --experience 105     经验要求 (108=在校生 102=应届生 101=经验不限 103=1年以内 104=1-3年 105=3-5年 106=5-10年 107=10年+)
  --degree 203         学历要求 (209=初中及以下 208=中专/中技 206=高中 202=大专 203=本科 204=硕士 205=博士)
  --industry 1001      行业 (1001=互联网 1002=电商 1003=金融 ...)

城市支持中文: --city 上海  或代码: --city 101020100

示例:
  # 阶段1 检索列表（多条件筛选，--stdout 输出 JSON 便于管道）
  %(prog)s --mode search --keyword "agent开发" --city 北京 --pages 3 --stdout
  %(prog)s --mode search --keyword "agent开发" --city 北京 --pages 3 --scale 305 --salary 406

  # 阶段2 精选详情（管道喂列表 / 指定文件 / 自动加载最新列表）
  %(prog)s --mode detail --job_id id1,id2 --stdout

  # 首页个性化推荐与最新职位（捕获页面原生响应）
  %(prog)s --mode homepage --homepage-url "https://www.zhipin.com/chengdu/?ka=header-home" --stdout

  # 只读发现收件箱数据接口（不输出联系人或聊天内容）
  %(prog)s --mode inbox-discover --stdout

  # 收件箱沟通进度（仅公司/岗位/未读/时间，不读取正文）
  %(prog)s --mode inbox --stdout

  # 显式读取专用 Chrome 当前已选会话的已渲染内容（不切换/不滚动/不发送）
  %(prog)s --mode inbox-read-active --expect-contact "刘姗" --stdout

  # 仅在用户当次精确确认后，向当前已选会话发送一条文本
  %(prog)s --mode inbox-send-active --expect-contact "杨先生" --message "你好" --confirm-send --stdout

  # 文件模式 + CSV 导出（不配 --stdout 时写文件）
  %(prog)s --mode search --keyword "agent开发" --city 北京 --pages 3 --format csv

  # 环境检查 / 启动 Chrome / smoke test
  %(prog)s --check

  # 浏览器/API smoke test
  %(prog)s --smoke-test

  # 启动 Chrome CDP
  %(prog)s --setup-chrome
        """)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--keyword", default="AI Agent", help="搜索关键词")
    p.add_argument("--city", default=DEFAULT_CITY_INPUT, help=f"城市 (中文名或代码，默认 {DEFAULT_CITY_INPUT})")
    p.add_argument("--pages", type=int, default=3, help=f"抓取页数 (最大 {MAX_PAGES})")
    p.add_argument("--output", default=None, help="列表数据输出路径")
    p.add_argument("--detail-output", default=None, help="详情数据输出路径")
    p.add_argument("--homepage-url", default=DEFAULT_HOMEPAGE_URL,
                   help=f"homepage 模式目标地址（默认 {DEFAULT_HOMEPAGE_URL}）")
    p.add_argument("--inbox-url", default=DEFAULT_INBOX_URL,
                   help=f"inbox/inbox-discover 模式目标地址（默认 {DEFAULT_INBOX_URL}）")
    p.add_argument("--capture-seconds", type=int, default=15,
                   help="homepage/inbox 模式捕获原生响应的秒数（5-30，默认 15）")
    p.add_argument("--expect-contact", default=None,
                   help="inbox-read-active 的当前会话校验联系人姓名（必填）")
    p.add_argument("--max-chat-items", type=int, default=80,
                   help="inbox-read-active 最多输出的当前已渲染消息项（1-200，默认 80）")
    p.add_argument("--message", default=None,
                   help="inbox-send-active 的精确发送文本")
    p.add_argument("--confirm-send", action="store_true",
                   help="确认执行 inbox-send-active 的单次外部发送；缺失则拒绝发送")
    p.add_argument("--cdp-port", type=int, default=DEFAULT_CDP_PORT,
                   help=f"CDP 调试端口 (默认 {DEFAULT_CDP_PORT})")
    p.add_argument("--format", default="json", choices=["json", "csv"],
                   help="输出格式 (默认 json)")
    # 筛选参数
    p.add_argument("--scale", default=None, help="公司规模代码")
    p.add_argument("--stage", default=None, help="融资阶段代码")
    p.add_argument("--salary", default=None, help="薪资范围代码")
    p.add_argument("--experience", default=None, help="经验要求代码")
    p.add_argument("--degree", default=None, help="学历要求代码")
    p.add_argument("--industry", default=None, help="行业代码")

    # 功能开关
    p.add_argument("--max-details", type=int, default=None, help="detail 模式最多抓几个详情")
    p.add_argument("--job_id", dest="job_ids", default=None,
                   help="按 job_id 精选详情（逗号分隔；需列表来源：管道/自动加载最新）")
    p.add_argument("--job_link", dest="job_links", default=None,
                   help="按完整 job_link 精选详情（逗号分隔；含 lid/securityId，无需列表文件）")
    p.add_argument("--mode", choices=["search", "detail", "homepage", "inbox", "inbox-discover", "inbox-read-active", "inbox-send-active"], default="search",
                   help="功能模式：search=多条件检索；detail=精选详情；homepage=首页推荐/最新职位；inbox=收件箱进度；inbox-discover=只读发现接口；inbox-read-active=读取当前已选会话；inbox-send-active=单次确认发送")
    p.add_argument("--stdout", action="store_true",
                   help="结果 JSON 输出到 stdout（不写文件；日志走 stderr，可用 2>log.txt 分离）")
    p.add_argument("--stream-json", action="store_true",
                   help="detail 模式每完成一个岗位向 stdout 输出一行 JSON（NDJSON）")
    p.add_argument("--allow-dom-fallback", action="store_true",
                   help="API 无数据时允许降级 DOM 提取（薪资可能受字体反爬影响，默认关闭）")

    # 工具命令
    p.add_argument("--check", action="store_true", help="运行环境诊断检查")
    p.add_argument("--smoke-test", action="store_true",
                   help="用真实 Chrome/CDP 跑一次 BOSS 搜索 API smoke test（不写结果文件）")
    p.add_argument("--list-cities", nargs="?", const="", default=None,
                   metavar="关键词",
                   help="打印支持的城市列表（可选关键词过滤，如 --list-cities 江）；"
                        "支持全国城市，码表见 data/city_codes.json，运行时自动从 BOSS 同步")
    p.add_argument("--setup-chrome", action="store_true",
                   help="自动启动 Chrome CDP 调试模式")
    p.add_argument("--copy-login-state", action="store_true",
                   help="手动从主 Chrome 导入 Local State + Cookie 相关文件到独立 profile（默认、首次启动、重复启动都不复制）")
    p.add_argument("--reset-chrome-profile", action="store_true",
                   help="重建 BOSS 专用 Chrome profile，会清除此专用浏览器内的登录态")
    p.add_argument("--stop-chrome", action="store_true",
                   help="关闭 BOSS 专用 CDP Chrome（按隔离 profile 精准匹配，不影响主 Chrome）")
    p.add_argument("--close-chrome", action="store_true",
                   help="抓取正常结束后自动关闭专用 Chrome（默认不关；异常退出不触发，保留登录态）")

    args = p.parse_args()
    if args.stream_json and args.mode != "detail":
        p.error("--stream-json 仅支持 --mode detail")
    real_stdout = sys.stdout
    if args.stdout or args.stream_json:
        sys.stdout = sys.stderr

    # --check 模式
    if args.check:
        sys.exit(run_check(args.cdp_port))

    if args.smoke_test:
        sys.exit(run_smoke_test(args.cdp_port))

    # --list-cities 模式（无需 Chrome/网络依赖，本地静态码表兜底）
    if args.list_cities is not None:
        list_cities(keyword=args.list_cities or None)
        sys.exit(0)

    # --setup-chrome 模式
    if args.setup_chrome:
        sys.exit(run_setup_chrome(
            args.cdp_port,
            copy_login_state=args.copy_login_state,
            reset_profile=args.reset_chrome_profile,
        ))

    # --stop-chrome 模式（关闭 BOSS 专用 CDP Chrome，独立命令）
    if args.stop_chrome:
        sys.exit(run_stop_chrome())

    if not require_runtime_dependencies("requests", "websocket"):
        sys.exit(1)

    if args.mode == "homepage":
        args.capture_seconds = max(5, min(30, args.capture_seconds))
        try:
            homepage_data = scrape_homepage(
                args.homepage_url,
                "-" if args.stdout else args.output,
                cdp_port=args.cdp_port,
                capture_seconds=args.capture_seconds,
            )
        except (BossAPIError, TimeoutError, RuntimeError, ValueError) as exc:
            print(f"❌ 首页岗位抓取失败: {exc}")
            sys.exit(2)
        if args.stdout:
            json.dump(homepage_data, real_stdout, ensure_ascii=False, indent=2)
            real_stdout.write("\n")
            real_stdout.flush()
        sys.exit(0)

    if args.mode == "inbox-discover":
        args.capture_seconds = max(5, min(30, args.capture_seconds))
        try:
            inbox_data = discover_inbox_endpoints(
                args.inbox_url,
                cdp_port=args.cdp_port,
                capture_seconds=args.capture_seconds,
            )
        except (BossAPIError, TimeoutError, RuntimeError, ValueError) as exc:
            print(f"❌ 收件箱接口发现失败: {exc}")
            sys.exit(2)
        if args.stdout:
            json.dump(inbox_data, real_stdout, ensure_ascii=False, indent=2)
            real_stdout.write("\n")
            real_stdout.flush()
        else:
            print(f"收件箱接口: {inbox_data['endpoint_count']} 个（仅字段结构，未输出任何私聊内容）")
            for endpoint in inbox_data["endpoints"]:
                print(f"  {endpoint['path']} | keys: {', '.join(endpoint['zp_data_keys'][:8])}")
        sys.exit(0)

    if args.mode == "inbox":
        args.capture_seconds = max(5, min(30, args.capture_seconds))
        try:
            inbox_data = scrape_inbox(
                args.inbox_url,
                "-" if args.stdout else args.output,
                cdp_port=args.cdp_port,
                capture_seconds=args.capture_seconds,
            )
        except (BossAPIError, TimeoutError, RuntimeError, ValueError) as exc:
            print(f"❌ 收件箱进度读取失败: {exc}")
            sys.exit(2)
        if args.stdout:
            json.dump(inbox_data, real_stdout, ensure_ascii=False, indent=2)
            real_stdout.write("\n")
            real_stdout.flush()
        sys.exit(0)

    if args.mode == "inbox-read-active":
        if not args.stdout:
            p.error("inbox-read-active 只允许配合 --stdout 使用，避免将私聊正文写入默认文件")
        try:
            inbox_data = read_active_inbox_conversation(
                args.expect_contact,
                cdp_port=args.cdp_port,
                max_entries=args.max_chat_items,
            )
        except (TimeoutError, RuntimeError, ValueError) as exc:
            print(f"❌ 当前会话读取失败: {exc}")
            sys.exit(2)
        json.dump(inbox_data, real_stdout, ensure_ascii=False, indent=2)
        real_stdout.write("\n")
        real_stdout.flush()
        sys.exit(0)

    if args.mode == "inbox-send-active":
        if not args.stdout:
            p.error("inbox-send-active 只允许配合 --stdout 使用，避免将发送记录写入默认文件")
        try:
            inbox_data = send_active_inbox_text(
                args.expect_contact,
                args.message,
                confirmed=args.confirm_send,
                cdp_port=args.cdp_port,
            )
        except (TimeoutError, RuntimeError, ValueError) as exc:
            print(f"❌ 当前会话发送失败: {exc}")
            sys.exit(2)
        json.dump(inbox_data, real_stdout, ensure_ascii=False, indent=2)
        real_stdout.write("\n")
        real_stdout.flush()
        sys.exit(0)

    # --mode detail: 列表来源 = 管道stdin / 自动加载最新 / --job_link 直接传链接
    if args.mode == "detail":
        id_tokens = [t.strip() for t in (args.job_ids or "").split(",") if t.strip()]
        link_tokens = [t.strip() for t in (args.job_links or "").split(",") if t.strip()]

        link_jobs = []
        if link_tokens:
            for t in link_tokens:
                link_jobs.append({
                    "job_id": job_id_from_link(t),
                    "title": "",
                    "boss_name": "",
                    "salary": "",
                    "location": "",
                    "tags": "",
                    "job_link": t,
                })
            print(f"按 job_link 直接加载 {len(link_jobs)} 条（无需列表文件）")

        need_list = bool(id_tokens) or not (link_tokens or id_tokens)
        if need_list:
            if os.environ.get("BOSS_LIST_STDIN"):
                raw = sys.stdin.buffer.read().decode("utf-8-sig")
                if not raw.strip():
                    p.error("--mode detail 检测到管道但没有数据（stdin 为空）")
                list_data = json.loads(raw)
                print(f"从 stdin 加载 {len(list_data.get('jobs', []))} 条")
            else:
                latest = find_latest_list_file()
                if latest is None:
                    p.error("--mode detail 缺少列表：请先 --mode search 抓列表，或通过管道传入列表，或直接用 --job_link 传链接")
                if not (link_tokens or id_tokens):
                    p.error("--mode detail 未指定 --job_id/--job_link，且列表来自自动加载；为避免误抓全部岗位，请指定其一，或通过管道传入精选列表")
                with open(latest, encoding="utf-8-sig") as f:
                    list_data = json.load(f)
                print(f"自动加载最新列表 {len(list_data.get('jobs', []))} 条: {latest}")
            if id_tokens:
                id_jobs, missing = filter_jobs_by_ids(list_data, id_tokens)
                print(f"按 job_id 精选详情：选中 {len(id_jobs)} 条"
                      + (f"，未匹配 {len(missing)} 个 id" if missing else ""))
                if missing:
                    print("  未匹配 job_id: " + ", ".join(sorted(missing)))
                detail_jobs = link_jobs + id_jobs
            else:
                detail_jobs = list_data.get("jobs", [])
                print(f"未指定 --job_id，将对列表内 {len(detail_jobs)} 条全部抓详情（建议精选 <=3 条）")
        else:
            detail_jobs = link_jobs
        if detail_jobs:
            stream_callback = None
            if args.stream_json:
                def stream_callback(detail):
                    json.dump(detail, real_stdout, ensure_ascii=False)
                    real_stdout.write("\n")
                    real_stdout.flush()
            try:
                details = scrape_details(
                    {"jobs": detail_jobs}, args.max_details,
                    "-" if args.stdout else args.detail_output,
                    cdp_port=args.cdp_port, fmt=args.format,
                    on_detail=stream_callback,
                )
            except (BossAPIError, DetailExtractionError, TimeoutError, RuntimeError) as exc:
                print(f"❌ 详情抓取失败: {exc}")
                sys.exit(2)
            if args.stdout and not args.stream_json:
                json.dump(details, real_stdout, ensure_ascii=False, indent=2)
                real_stdout.write("\n")
                real_stdout.flush()
        else:
            print("没有匹配的岗位，跳过详情抓取")
        sys.exit(0)

    # 抓取前校验城市，避免无效中文名被原样作为 city 参数继续请求。
    try:
        resolve_city(args.city)
    except CityResolutionError as e:
        print(f"❌ {e}")
        sys.exit(1)

    # 页数限制
    if args.pages > MAX_PAGES:
        print(f"⚠️ 页数 {args.pages} 超过上限 {MAX_PAGES}，已自动调整为 {MAX_PAGES}")
        args.pages = MAX_PAGES

    # 收集筛选条件
    filters = {}
    for key in ["scale", "stage", "salary", "experience", "degree", "industry"]:
        val = getattr(args, key)
        if val:
            filters[key] = val

    # Do not send a fixed Java/Shanghai probe before the real search. The
    # target page's native joblist response is the single source of truth.
    print("将通过目标搜索页的原生网络响应判断登录态并获取职位数据。\n")

    try:
        list_data = scrape_list(
            args.keyword, args.city, args.pages, filters,
            "-" if args.stdout else args.output,
            cdp_port=args.cdp_port, fmt=args.format,
            allow_dom_fallback=args.allow_dom_fallback,
        )
    except BossAPIError as exc:
        print(f"❌ {exc}")
        sys.exit(2)
    except TimeoutError as exc:
        print(f"❌ 抓取超时: {exc}")
        sys.exit(2)

    # stdout 模式：结果 JSON 直接输出到 stdout（不写文件）
    if args.stdout:
        json.dump(list_data, real_stdout, ensure_ascii=False, indent=2)
        real_stdout.write("\n")
        real_stdout.flush()

    # 抓取正常结束后按需收尾（仅成功路径；异常/登录失败走 sys.exit，不会触发，保留登录态）
    if args.close_chrome:
        profile = prepare_cdp_profile(copy_login_state=False, reset=False)
        stopped = stop_cdp_chrome(profile["path"])
        if stopped:
            print(f"\n🧹 已按 --close-chrome 关闭 BOSS 专用 Chrome 进程：{stopped} 个")
        else:
            print(f"\nℹ️  --close-chrome 未发现运行中的 BOSS 专用 Chrome 进程")


if __name__ == "__main__":
    main()
