from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import html
import json
import os
import re
import time
import urllib.error
from urllib.parse import urlparse
import urllib.request

from core.builder import (
    GH_RAW,
    arrange_sites_smart_order,
    build_absolute_fallback_config,
    build_and_save_proxy_pools,
    build_douban_site,
    build_lite_config,
    build_pan_only_config,
    ensure_standard_refs,
    filter_and_sort_parses,
    is_douban_home_site,
    rank_pan_spider_sites,
    strip_internal_meta,
    write_actions_summary,
)
from core.live import process_live_sources_and_build
from core.utils import (
    DEFAULT_DEAD_OR_JUNK_WORDS,
    HEADERS,
    append_cache_buster,
    idn_encode,
    is_pan_site,
    load_configs,
    normalize_site_display_name,
    run_parallel,
    sync_spider_jar,
    verify_and_mirror_assets,
)
from core.vod import (
    _initial_cms_health_score,
    collect_vod_and_update_registry,
    discover_new_upstream_sources,
    fetch_ziyuanzu_cms_candidates,
    load_or_init_source_registry,
    save_source_registry,
    sync_famous_vod_urls_from_nav,
    test_and_dedup_cms,
)

CST_TZ = timezone(timedelta(hours=8))

STANDARD_CMS_FILTERS = [
    {
        "key": "class",
        "name": "类型",
        "value": [
            {"n": "全部", "v": ""},
            {"n": "剧情", "v": "剧情"},
            {"n": "喜剧", "v": "喜剧"},
            {"n": "动作", "v": "动作"},
            {"n": "爱情", "v": "爱情"},
            {"n": "科幻", "v": "科幻"},
            {"n": "悬疑", "v": "悬疑"},
            {"n": "惊悚", "v": "惊悚"},
            {"n": "犯罪", "v": "犯罪"},
            {"n": "奇幻", "v": "奇幻"},
            {"n": "战争", "v": "战争"},
            {"n": "动画", "v": "动画"},
            {"n": "纪录片", "v": "纪录片"},
        ],
    },
    {
        "key": "area",
        "name": "地区",
        "value": [
            {"n": "全部", "v": ""},
            {"n": "大陆", "v": "大陆"},
            {"n": "香港", "v": "香港"},
            {"n": "台湾", "v": "台湾"},
            {"n": "美国", "v": "美国"},
            {"n": "韩国", "v": "韩国"},
            {"n": "日本", "v": "日本"},
            {"n": "英国", "v": "英国"},
            {"n": "泰国", "v": "泰国"},
            {"n": "印度", "v": "印度"},
            {"n": "其他", "v": "其他"},
        ],
    },
    {
        "key": "year",
        "name": "年份",
        "value": [
            {"n": "全部", "v": ""},
            {"n": "2026", "v": "2026"},
            {"n": "2025", "v": "2025"},
            {"n": "2024", "v": "2024"},
            {"n": "2023", "v": "2023"},
            {"n": "2022", "v": "2022"},
            {"n": "2021", "v": "2021"},
            {"n": "2020", "v": "2020"},
            {"n": "2010-2019", "v": "2010-2019"},
            {"n": "更早", "v": "更早"},
        ],
    },
    {
        "key": "by",
        "name": "排序",
        "value": [
            {"n": "最新上线", "v": "time"},
            {"n": "最多播放", "v": "hits"},
            {"n": "豆瓣高分", "v": "score"},
        ],
    },
]

PRIMARY_CAT_ALIASES = (
    ("电影", ("电影", "电影片", "大片")),
    ("电视剧", ("电视剧", "连续剧", "剧集")),
    ("综艺", ("综艺", "综艺节目", "真人秀")),
    ("动漫", ("动漫", "动画", "番剧", "少儿")),
    ("短剧", ("短剧", "爽剧", "微短剧")),
)

SPECIAL_SITE_KEYWORDS = (
    "动漫",
    "番剧",
    "短剧",
    "爽剧",
    "体育",
    "球赛",
    "听书",
    "相声",
    "小品",
    "戏曲",
    "少儿",
    "纪录",
    "演唱会",
    "磁力",
)

BLACKLIST_DOMAINS = (
    "agit.ai",
    "www.agit.ai",
    "voflix.me",
    "www.voflix.me",
    "wogg.xxooo.cf",
    "rihou.vip",
)

BLACKLIST_SITE_KEYWORDS = (
    "永远免费",
    "live2vod",
    "yj1211",
)

LITE_PRIVATE_IP_PORT_RE = re.compile(
    r"^https?://(?:\d{1,3}\.){3}\d{1,3}:\d{2,5}(?:/|$)",
    re.IGNORECASE,
)

WOGG_LIKE_KEYWORDS = (
    "玩偶",
    "wogg",
    "mogg",
    "木偶",
)

LIVE_GENRE_ALIAS_MAP = {
    "体育": "体育频道",
    "央视频道": "CCTV央视频道",
    "央视": "CCTV央视频道",
    "中央频道": "CCTV央视频道",
    "卫视频道": "地方卫视频道",
    "卫视": "地方卫视频道",
    "地方卫视": "地方卫视频道",
    "各省卫视": "地方卫视频道",
    "港澳台频道": "港澳台",
    "港澳台直播": "港澳台",
    "春晚": "历年春晚",
    "春晚频道": "历年春晚",
    "4K": "CCTV央视频道",
    "4k": "CCTV央视频道",
    "4K频道": "CCTV央视频道",
    "4k频道": "CCTV央视频道",
}

LIVE_JUNK_CHANNEL_EXACT = {
    "支持作者",
    "公众号",
    "关注公众号",
}

LIVE_JUNK_CHANNEL_SUBSTRINGS = (
    "Kunnamkulam",
)

ALLOWED_LIVE_SCHEMES = (
    "http",
    "https",
    "rtmp",
    "rtsp",
    "udp",
    "rtp",
    "mms",
    "p2p",
    "mitv",
)


def _normalize_live_genre(raw_genre):
    """剥离直播分组中的 Emoji 与装饰性符号，并归一化常见同义分组"""
    cleaned = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9\-_/&]+", "", str(raw_genre or "")).strip()
    if cleaned in LIVE_GENRE_ALIAS_MAP:
        return LIVE_GENRE_ALIAS_MAP[cleaned]
    if cleaned.lower() in LIVE_GENRE_ALIAS_MAP:
        return LIVE_GENRE_ALIAS_MAP[cleaned.lower()]
    return cleaned if cleaned else "综合频道"


def _sanitize_live_stream_url(raw_url):
    """清洗直播流地址尾部的引号、HTML实体、字面量转义符、行内#多源及水印参数"""
    if not raw_url:
        return ""
    url = str(raw_url).strip()

    # 1. 解码 HTML 实体（如 &amp; -> &）并剔除残留的 &quot; / &quot
    url = html.unescape(url)
    url = re.sub(r"&quot;?", "", url, flags=re.IGNORECASE)

    # 2. 清除文本中直接残留的字面量转义符（如 \r\n）及首尾残留引号/控制字符
    url = url.replace("\\r", "").replace("\\n", "").replace("\\t", "")
    url = url.strip("\"'`,; \t\r\n\x00-\x1f\x7f")
    if not url:
        return ""

    # 3. 兼容并清理行内 # 拼接的冗余多源后缀（保留第一条合法流地址）
    parts = [p.strip("\"'`,; \t\r\n\x00-\x1f\x7f") for p in url.split("#") if p.strip()]
    for part in parts:
        # 剔除常见的公众号推广尾部参数
        part = re.sub(r"\?%E5%85%B3%E6%B3%A8%E5%BE%AE.*$", "", part)
        part = re.sub(r"[?&]zzhed\??$", "", part, flags=re.IGNORECASE)
        part = part.rstrip("?")
        if any(ch in part for ch in ('"', "'", "<", ">", "\r", "\n")):
            continue
        try:
            parsed = urlparse(part)
            if parsed.scheme.lower() in ALLOWED_LIVE_SCHEMES and parsed.netloc:
                return part
        except Exception:
            continue
    return ""


def _post_sanitize_tvbox_live_txt(filepath="tvbox_live.txt", fallback_count=0):
    """
    对生成的 tvbox_live.txt 执行后置标准化清洗：
      1. 合并带 Emoji 与不带 Emoji 的重复 #genre# 分类（及春晚、4K测试等冗余分组）
      2. 净化频道 URL 尾部的多余双引号、HTML实体、行内#多源拼接与 \\r\\n 脏字符
      3. 过滤推广/广告杂项频道，并在同分组内按 (频道名, 净化后URL) 精确去重
    """
    if not os.path.exists(filepath):
        return fallback_count

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            raw_lines = f.readlines()
    except Exception:
        return fallback_count

    header_comments = []
    grouped_channels = OrderedDict()
    seen_in_group = {}
    current_genre = "综合频道"
    seen_first_genre = False

    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            continue

        # 保留文件顶部的元信息注释行（如更新时间统计）
        if not seen_first_genre and stripped.startswith("#"):
            header_comments.append(stripped)
            continue

        if stripped.endswith(",#genre#"):
            seen_first_genre = True
            raw_g = stripped[: -len(",#genre#")]
            current_genre = _normalize_live_genre(raw_g)
            grouped_channels.setdefault(current_genre, [])
            seen_in_group.setdefault(current_genre, set())
            continue

        if "," in stripped and "://" in stripped:
            seen_first_genre = True
            ch_name, raw_u = stripped.split(",", 1)
            ch_name = ch_name.strip().strip("\"'")
            if (
                not ch_name
                or ch_name in LIVE_JUNK_CHANNEL_EXACT
                or any(sub in ch_name for sub in LIVE_JUNK_CHANNEL_SUBSTRINGS)
            ):
                continue

            clean_u = _sanitize_live_stream_url(raw_u)
            if not clean_u:
                continue

            grouped_channels.setdefault(current_genre, [])
            seen_in_group.setdefault(current_genre, set())
            dedup_key = (ch_name, clean_u)
            if dedup_key not in seen_in_group[current_genre]:
                seen_in_group[current_genre].add(dedup_key)
                grouped_channels[current_genre].append(f"{ch_name},{clean_u}")

    if not grouped_channels:
        return fallback_count

    out_lines = list(header_comments)
    total_channels = 0
    for genre, items in grouped_channels.items():
        if not items:
            continue
        if out_lines:
            out_lines.append("")
        out_lines.append(f"{genre},#genre#")
        out_lines.extend(items)
        total_channels += len(items)

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(out_lines) + "\n")
    except Exception:
        return fallback_count

    return total_channels


def _parse_iso_ts(ts_str):
    if not ts_str:
        return None
    try:
        s = str(ts_str).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST_TZ)
        return dt.timestamp()
    except Exception:
        return None


def _ensure_raw_branch_url(url, repo_path, branch="main"):
    """
    兜底修正：确保 raw.githubusercontent.com/<owner>/<repo>/ 后包含分支名（如 /main/），
    避免代理转发时因缺少分支名导致 400/404。
    """
    if not url or not repo_path:
        return url
    prefix_marker = f"raw.githubusercontent.com/{repo_path}/"
    branch_marker = f"raw.githubusercontent.com/{repo_path}/{branch}/"
    if prefix_marker in url and branch_marker not in url:
        return url.replace(prefix_marker, branch_marker, 1)
    return url


def _sanitize_sites_with_blacklist(sites, mode="full", extra_dead_hosts=()):
    """
    1. 黑名单域名/关键词过滤（含自建豆瓣推荐首页白名单保护）
    2. 精简版（mode='lite'）额外剔除裸连私有 IP:端口源并收敛冗余玩偶系源
    """
    if not isinstance(sites, list):
        return []

    dead_host_set = {
        str(h).strip().lower()
        for h in (tuple(BLACKLIST_DOMAINS) + tuple(extra_dead_hosts or ()))
        if str(h).strip()
    }
    kw_list = [k.lower() for k in BLACKLIST_SITE_KEYWORDS]

    filtered = []
    wogg_kept = 0
    max_wogg_in_lite = 2

    for s in sites:
        if not isinstance(s, dict):
            continue
        if is_douban_home_site(s):
            filtered.append(s)
            continue

        key_str = str(s.get("key", "")).strip().lower()
        name_str = str(s.get("name", "")).strip()
        name_lower = name_str.lower()
        blob = " ".join(
            [
                key_str,
                name_lower,
                str(s.get("api", "")).lower(),
                str(s.get("ext", "")).lower(),
                str(s.get("jar", "")).lower(),
            ]
        )

        if any(kw in blob for kw in kw_list):
            continue

        matched_dead = False
        for field in ("api", "ext", "jar", "url"):
            val = str(s.get(field, "")).strip()
            if not val:
                continue
            try:
                host = (urlparse(val).hostname or "").lower()
            except Exception:
                host = ""
            if host and any(host == dh or host.endswith("." + dh) for dh in dead_host_set):
                matched_dead = True
                break
            if any(dh in val.lower() for dh in dead_host_set):
                matched_dead = True
                break
        if matched_dead:
            continue

        if mode == "lite":
            api_u = str(s.get("api", "")).strip()
            ext_u = str(s.get("ext", "")).strip()
            if LITE_PRIVATE_IP_PORT_RE.match(api_u) or LITE_PRIVATE_IP_PORT_RE.match(ext_u):
                continue

            is_wogg_like = any(w in key_str or w in name_lower for w in WOGG_LIKE_KEYWORDS)
            if is_wogg_like:
                if wogg_kept >= max_wogg_in_lite:
                    continue
                wogg_kept += 1

        filtered.append(s)

    return filtered


def _normalize_cms_domain(api_url):
    """提取 CMS 接口归一化主域名，用于合并同一采集站的 XML/JSON 或多子域重复项"""
    raw = str(api_url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").lower().strip(".")
        path = (parsed.path or "").lower()
    except Exception:
        return raw.lower()

    if not host:
        return raw.lower()

    host = re.sub(
        r"^(?:www|m|api|cj|caiji|collect|cms|vod|vip|json|xml|v\d+)[.-]+",
        "",
        host,
    )
    host = re.sub(
        r"(?:zyapi|api|caiji|采集|资源|影视|vod)\.(?:com|net|org|tv|cc|me|xyz|top|live|vip|cn)$",
        "",
        host,
    )
    host = re.sub(
        r"\.(?:com\.cn|net\.cn|org\.cn|com|net|org|tv|cc|me|xyz|top|live|vip|cn|io|gg|sh|la|pro)$",
        "",
        host,
    )
    path_tag = "xml" if ("xml" in path or "at/xml" in path) else "json"
    return f"{host or parsed.hostname}::{path_tag}"


def _dedup_cms_sites_normalized(sites, strict_host_only=False):
    """
    CMS 采集源接口归一化去重：
    对 type == 1 的标准 CMS 采集站按归一化主域名归并，优先保留延迟最低、健康分最高的条目。
    """
    if not isinstance(sites, list) or not sites:
        return sites

    best_by_norm = {}
    for idx, s in enumerate(sites):
        if not isinstance(s, dict) or s.get("type") != 1 or is_douban_home_site(s):
            continue

        norm_key = _normalize_cms_domain(s.get("api", ""))
        if strict_host_only and "::" in norm_key:
            norm_key = norm_key.split("::", 1)[0]
        if not norm_key:
            continue

        lat = _extract_site_latency_ms(s)
        health = float(s.get("_health_score") or s.get("health_score") or 60.0)
        score = (lat, -health, idx)

        prev = best_by_norm.get(norm_key)
        if prev is None or score < prev[0]:
            best_by_norm[norm_key] = (score, idx, s)

    kept_cms = {item[1]: item[2] for item in best_by_norm.values()}
    result = []
    for idx, s in enumerate(sites):
        if isinstance(s, dict) and s.get("type") == 1 and not is_douban_home_site(s):
            if idx in kept_cms:
                result.append(kept_cms[idx])
        else:
            result.append(s)
    return result


def _resolve_preferred_gh_proxy_prefix(full_px, lite_px):
    """从已测速的代理池中提取最优 GitHub 加速前缀（确保以 '/' 结尾）"""
    for pool in (lite_px, full_px):
        if isinstance(pool, list) and pool:
            top = pool[0]
            if isinstance(top, dict):
                pref = str(top.get("prefix") or "").strip()
                if pref.startswith("http"):
                    return pref if pref.endswith("/") else f"{pref}/"
    return "https://ghfast.top/"


def _rewrite_gh_url_with_proxy(val, proxy_prefix, repo_owner_path="", repo_branch="main"):
    """
    将裸连的 GitHub Raw / GitHub 链接改写为走优选加速代理前缀；
    若已包含代理前缀或为本地相对路径（./ 或 /），则保持不变。
    """
    if not isinstance(val, str):
        return val
    s = val.strip()
    if not s or not s.startswith("http"):
        return val

    if proxy_prefix and s.startswith(proxy_prefix):
        return s
    known_proxies = (
        "https://ghfast.top/",
        "https://ghproxy.net/",
        "https://gh-proxy.com/",
        "https://cdn.gh-proxy.org/",
        "https://gh.api.99988866.xyz/",
    )
    if any(s.startswith(kp) for kp in known_proxies) and "raw.githubusercontent.com" in s:
        if repo_owner_path:
            s = _ensure_raw_branch_url(s, repo_owner_path, repo_branch)
        return s

    if "raw.githubusercontent.com/" in s:
        if repo_owner_path:
            s = _ensure_raw_branch_url(s, repo_owner_path, repo_branch)
        if s.startswith(("https://raw.githubusercontent.com/", "http://raw.githubusercontent.com/")):
            return f"{proxy_prefix}{s}"
        idx = s.find("raw.githubusercontent.com/")
        if idx != -1:
            return f"{proxy_prefix}https://{s[idx:]}"

    if s.startswith("https://github.com/") and "/raw/" in s:
        return f"{proxy_prefix}{s}"

    return s


def _apply_preferred_github_proxy(config_obj, proxy_prefix, repo_owner_path="", repo_branch="main"):
    """递归遍历配置中的 sites / parses，统一为 GitHub 链接加上优选代理"""
    if not isinstance(config_obj, dict) or not proxy_prefix:
        return config_obj

    target_fields = ("jar", "ext", "api", "url")

    def _walk(node):
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if k in target_fields and isinstance(v, str):
                    node[k] = _rewrite_gh_url_with_proxy(
                        v, proxy_prefix, repo_owner_path, repo_branch
                    )
                elif isinstance(v, (dict, list)):
                    _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    if isinstance(config_obj.get("sites"), list):
        _walk(config_obj["sites"])
    if isinstance(config_obj.get("parses"), list):
        _walk(config_obj["parses"])
    return config_obj


def _prune_dead_discovery_pages(registry, settings):
    """
    预检 discovery_pages 可用性：
    - 返回 404/410 的非种子页直接拉黑入 purged_seeds；
    - 连续失败达到 max_discovered_fail_streak 的发现页自动剔除并拉黑，避免反复报错。
    """
    pages = registry.get("discovery_pages", [])
    if not isinstance(pages, list) or not pages:
        return []

    reg_cfg = settings.get("registry", {})
    max_fail = int(reg_cfg.get("max_discovered_fail_streak", 3))
    purged_set = set(registry.get("purged_seeds", []))
    fail_counts = registry.get("discovery_fail_counts")
    if not isinstance(fail_counts, dict):
        fail_counts = {}

    def _probe_page(page_u):
        u = str(page_u or "").strip()
        if not u or " " in u or u in purged_set or u.rstrip("/") in purged_set:
            return (u, "purged")
        target = idn_encode(u)
        need_get = False
        try:
            req = urllib.request.Request(target, headers=HEADERS, method="HEAD")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status < 400:
                    return (u, "ok")
                if resp.status in (404, 410):
                    return (u, "gone")
                return (u, "fail")
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return (u, "gone")
            if e.code in (403, 405):
                need_get = True
            else:
                return (u, "fail")
        except Exception:
            return (u, "fail")

        if need_get:
            try:
                req = urllib.request.Request(target, headers=HEADERS, method="GET")
                with urllib.request.urlopen(req, timeout=6) as resp:
                    if resp.status < 400:
                        return (u, "ok")
                    if resp.status in (404, 410):
                        return (u, "gone")
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    return (u, "gone")
            except Exception:
                pass
        return (u, "fail")

    kept, removed_logs = [], []
    for u, status in run_parallel(_probe_page, pages, workers=12):
        if not u or status == "purged":
            continue
        if status == "ok":
            fail_counts.pop(u, None)
            kept.append(u)
        elif status == "gone":
            fail_counts.pop(u, None)
            purged_set.add(u)
            purged_set.add(u.rstrip("/"))
            removed_logs.append(f"discovery_pages: {u} (HTTP 404/410 已自动拉黑)")
        else:
            cnt = int(fail_counts.get(u, 0) or 0) + 1
            if cnt >= max_fail:
                fail_counts.pop(u, None)
                purged_set.add(u)
                purged_set.add(u.rstrip("/"))
                removed_logs.append(f"discovery_pages: {u} (连续失败{cnt}次已自动拉黑)")
            else:
                fail_counts[u] = cnt
                kept.append(u)

    active_set = set(kept)
    registry["discovery_fail_counts"] = {
        k: v for k, v in fail_counts.items() if k in active_set
    }
    registry["discovery_pages"] = kept
    registry["purged_seeds"] = sorted(purged_set)
    if removed_logs:
        print(f"自动清理失效发现页 {len(removed_logs)} 条:")
        for line in removed_logs:
            print(f"  - {line}")
    return removed_logs


def _purge_dead_seeds_and_update_tracker(registry, settings, now_iso):
    """
    维护每个源的 first_fail_at 连续失败起点：
    - 若 fail_streak == 0，清空 first_fail_at；
    - 若 fail_streak > 0 且无 first_fail_at，记录当前时间；
    - 若 fail_streak >= 15 且首次失败距今 >= 15 天，彻底物理删除，
      并把 URL 写入 registry['purged_seeds'] 防止下次从 seeds.json 回流。
    """
    reg_cfg = settings.get("registry", {})
    max_streak = int(reg_cfg.get("purge_seed_fail_streak", 15))
    max_days = float(reg_cfg.get("purge_seed_fail_days", 15))
    now_ts = time.time()
    purged_set = set(registry.get("purged_seeds", []))
    purged_items = []

    for bucket in ("live_sources", "vod_sources", "cms_apis"):
        items = registry.get(bucket, [])
        if not isinstance(items, list):
            continue
        kept = []
        for it in items:
            if not isinstance(it, dict):
                continue
            u = str(it.get("url") or it.get("api") or "").strip()
            if not u or " " in u or u in purged_set or u.rstrip("/") in purged_set:
                continue
            streak = int(it.get("fail_streak", 0) or 0)
            if streak <= 0:
                it.pop("first_fail_at", None)
                kept.append(it)
                continue

            if not it.get("first_fail_at"):
                it["first_fail_at"] = now_iso
            first_ts = _parse_iso_ts(it.get("first_fail_at")) or now_ts
            fail_days = (now_ts - first_ts) / 86400.0

            if streak >= max_streak and fail_days >= max_days:
                purged_set.add(u)
                purged_set.add(u.rstrip("/"))
                purged_items.append(f"{bucket}: {u} (连续失败{streak}次/{fail_days:.1f}天)")
                continue
            kept.append(it)
        registry[bucket] = kept

    registry["purged_seeds"] = sorted(purged_set)
    if purged_items:
        print(f"自动物理清除长期失效源 {len(purged_items)} 条:")
        for line in purged_items:
            print(f"  - {line}")
    return purged_items


def _filter_purged_seeds(seeds, registry):
    """过滤掉已打入 purged_seeds 黑名单或含空格的脏种子链接"""
    purged = set(registry.get("purged_seeds", []))
    cleaned = dict(seeds)
    for list_key in ("discovery_pages", "live_source_urls", "extra_vod_urls"):
        if isinstance(cleaned.get(list_key), list):
            cleaned[list_key] = [
                u.strip()
                for u in cleaned[list_key]
                if isinstance(u, str)
                and u.strip()
                and " " not in u.strip()
                and u.strip() not in purged
                and u.strip().rstrip("/") not in purged
            ]
    if isinstance(cleaned.get("tvbox_vod_groups"), list):
        new_groups = []
        for g in cleaned["tvbox_vod_groups"]:
            gc = dict(g)
            gc["urls"] = [
                u.strip()
                for u in gc.get("urls", [])
                if isinstance(u, str)
                and u.strip()
                and " " not in u.strip()
                and u.strip() not in purged
                and u.strip().rstrip("/") not in purged
            ]
            if gc["urls"]:
                new_groups.append(gc)
        cleaned["tvbox_vod_groups"] = new_groups
    return cleaned


def _snapshot_prev_counts(settings, pan_keywords, invalid_keywords):
    """在覆盖写出新文件前，快照上一版成品与源仓数量以便简报对比增减"""
    prev = {}
    try:
        if os.path.exists("tvbox.json"):
            with open("tvbox.json", "r", encoding="utf-8") as f:
                sites = json.load(f).get("sites", [])
                if isinstance(sites, list):
                    prev["full"] = len(sites)
                    prev["pan_in_full"] = sum(
                        1 for s in sites if is_pan_site(s, pan_keywords, invalid_keywords)
                    )
    except Exception:
        pass

    for key, fname in (("lite", "tvbox_lite.json"), ("pan_only", "tvbox_pan.json")):
        try:
            if os.path.exists(fname):
                with open(fname, "r", encoding="utf-8") as f:
                    sites = json.load(f).get("sites", [])
                    if isinstance(sites, list):
                        prev[key] = len(sites)
        except Exception:
            pass

    try:
        if os.path.exists("tvbox_live.txt"):
            with open("tvbox_live.txt", "r", encoding="utf-8") as f:
                prev["live"] = sum(
                    1
                    for ln in f
                    if "," in ln
                    and not ln.strip().endswith("#genre#")
                    and "://" in ln
                )
    except Exception:
        pass

    reg_file = settings.get("registry", {}).get("file", "source_registry.json")
    try:
        if os.path.exists(reg_file):
            with open(reg_file, "r", encoding="utf-8") as f:
                reg = json.load(f)
                prev["reg_live"] = len(reg.get("live_sources", []))
                prev["reg_vod"] = len(reg.get("vod_sources", []))
                prev["reg_cms"] = len(reg.get("cms_apis", []))
    except Exception:
        pass

    return prev


def _extract_site_latency_ms(site):
    """从站点元数据或名称括号中解析响应延迟(ms)"""
    for k in ("_latency", "_src_latency", "latency"):
        v = site.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return int(v)
    m = re.search(r"\((\d{1,4})\s*ms\)", str(site.get("name", "")))
    if m:
        return int(m.group(1))
    return 600


def _enrich_top_cms_categories_and_filters(sites, top_n=25):
    """
    复用 test_and_dedup_cms 探活阶段缓存的 _raw_classes 原始分类列表，
    为前 top_n 个标准 CMS(type=1) 站点零网络请求映射一级分类与四维筛选。
    """
    applied = 0
    for s in sites:
        if applied >= top_n:
            break
        if s.get("type") != 1 or is_douban_home_site(s):
            continue
        raw_classes = s.get("_raw_classes")
        if not isinstance(raw_classes, list) or not raw_classes:
            continue

        matched_cats = []
        matched_ids = []
        used_targets = set()
        for item in raw_classes:
            if not isinstance(item, dict):
                continue
            t_name = str(item.get("type_name", "")).strip()
            t_id = str(item.get("type_id", "")).strip()
            if not t_id:
                continue
            for target_cat, aliases in PRIMARY_CAT_ALIASES:
                if target_cat not in used_targets and any(
                    alias == t_name or alias in t_name for alias in aliases
                ):
                    matched_cats.append(target_cat)
                    matched_ids.append(t_id)
                    used_targets.add(target_cat)
                    break

        if len(matched_cats) >= 2:
            filters_obj = {
                tid: json.loads(json.dumps(STANDARD_CMS_FILTERS, ensure_ascii=False))
                for tid in matched_ids
            }
            s["categories"] = matched_cats
            s["_categories"] = matched_cats
            s["filters"] = filters_obj
            s["filterable"] = 1
            applied += 1


def _rebalance_tv_site_tiers(sites, settings, pan_kws, invalid_kws, enrich_top_n=25):
    """
    电视端黄金梯队重排 + 统一聚搜(quickSearch)配额分配：
      T1: 唯一自建豆瓣推荐首页置顶
      T2: 优质快速直连站（按延迟/速度与健康分综合排序）
      T3: 优质网盘站（玩偶、木偶、蜡笔、至臻、欧歌等，紧接直连站方便看4K原盘）
      T4: 普通存活直连站
      T5: 动漫 / 短剧 / 体育 / 听书等垂直专项站（放列表尾部避免干扰日常选片）
    重排后统一为前 quick_search_top_n 个站点分配 quickSearch=1，避免顺序与聚搜错位。
    """
    if not isinstance(sites, list) or not sites:
        return sites

    qs_top_n = int(settings.get("pinned", {}).get("quick_search_top_n", 12))
    indexed = list(enumerate(sites))

    def _tier_sort_key(pair):
        idx, s = pair
        name_str = str(s.get("name", ""))
        lat = _extract_site_latency_ms(s)
        health = float(s.get("_health_score") or s.get("health_score") or 60.0)
        is_pan = is_pan_site(s, pan_kws, invalid_kws)
        is_special = any(kw in name_str for kw in SPECIAL_SITE_KEYWORDS)

        if is_douban_home_site(s):
            tier = 1
        elif is_pan:
            tier = 3
        elif is_special:
            tier = 5
        elif s.get("type") == 1 or s.get("_is_priority") or lat <= 450:
            tier = 2
        else:
            tier = 4

        return (tier, lat, -health, idx)

    reordered = [s for _, s in sorted(indexed, key=_tier_sort_key)]
    _enrich_top_cms_categories_and_filters(reordered, top_n=enrich_top_n)

    for idx, s in enumerate(reordered):
        if not is_douban_home_site(s):
            s["quickSearch"] = 1 if idx < qs_top_n else 0
    return reordered


def main():
    settings, seeds, rules = load_configs()
    th = settings.get("thresholds", {})
    dead_hosts = tuple(rules.get("dead_proxy_hosts", []))
    pan_kws = rules.get("pan_keywords", [])
    cms_kws = settings.get("pinned", {}).get("cms_keywords", [])
    invalid_kws = tuple(rules.get("junk_keywords", [])) + DEFAULT_DEAD_OR_JUNK_WORDS
    prev_counts = _snapshot_prev_counts(settings, pan_kws, invalid_kws)

    repo_owner_path = settings.get("repo_owner_path", "hmz0123/hmz-auto-iptv-sources").strip("/")
    repo_branch = settings.get("repo_branch", "main").strip("/")

    now_cst = datetime.now(CST_TZ)
    tag = now_cst.strftime("%Y-%m-%d %H:%M:%S 北京时间")
    now_iso = now_cst.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    version_tag = now_cst.strftime("%Y%m%d")
    alerts = []

    spider_md5 = sync_spider_jar(settings.get("urls", {}).get("upstream_spider", ""))
    sync_famous_vod_urls_from_nav(seeds, dead_hosts)

    registry = load_or_init_source_registry(settings, seeds, dead_hosts)
    _purge_dead_seeds_and_update_tracker(registry, settings, now_iso)
    _prune_dead_discovery_pages(registry, settings)
    seeds = _filter_purged_seeds(seeds, registry)

    new_live, new_vod, new_cms = discover_new_upstream_sources(
        registry, seeds, settings, dead_hosts
    )
    raw_live_cnt = process_live_sources_and_build(registry, new_live, tag, settings)
    live_cnt = _post_sanitize_tvbox_live_txt("tvbox_live.txt", fallback_count=raw_live_cnt)

    (
        cms_map,
        tv_base,
        ex_sites,
        ex_parses,
        vod_texts,
        source_stats,
    ) = collect_vod_and_update_registry(
        registry, new_vod, new_cms, settings, seeds, rules
    )
    _, _, more_cms = discover_new_upstream_sources(
        registry, seeds, settings, dead_hosts, extra_texts=vod_texts
    )
    purged_set = set(registry.get("purged_seeds", []))
    for api_u in more_cms:
        if api_u.rstrip("/") in purged_set or api_u in purged_set:
            continue
        cms_map.setdefault(
            api_u.rstrip("/"),
            {
                "key": f"disc_{len(cms_map)}",
                "name": f"发现站_{len(cms_map)}",
                "api": api_u,
                "origin": "discovered",
                "fail_streak": 0,
                "latency": 999,
                "health_score": _initial_cms_health_score("", api_u, cms_kws),
                "last_ok_at": "",
            },
        )
    for zyz_item in fetch_ziyuanzu_cms_candidates(settings):
        if zyz_item["api"].rstrip("/") not in purged_set and zyz_item["api"] not in purged_set:
            cms_map.setdefault(zyz_item["api"].rstrip("/"), zyz_item)

    val_cms = test_and_dedup_cms(cms_map, settings)
    max_cms_sites = int(th.get("max_cms_sites", 90))
    if max_cms_sites > 0 and len(val_cms) > max_cms_sites:
        val_cms = val_cms[:max_cms_sites]

    registry["cms_apis"] = list(cms_map.values())
    purged_now = _purge_dead_seeds_and_update_tracker(registry, settings, now_iso)
    if purged_now:
        alerts.append(f"已自动彻底清除长期失效死链 {len(purged_now)} 条（连续失败≥15次且满15天）。")
    save_source_registry(registry, tag, settings)

    min_valid_cms = int(th.get("min_valid_cms_sites", 5))
    min_full_sites = int(th.get("min_quality_gate_full_sites", 30))
    min_pan_sites = int(th.get("min_quality_gate_pan_sites", 3))
    max_extra_spider_full = int(th.get("max_full_extra_spider_sites", 250))

    jar_stats, quality_gate_triggered = (0, 0, 0), False
    if len(val_cms) < min_valid_cms and os.path.exists("tvbox.json"):
        quality_gate_triggered = True
        alerts.append(
            f"有效 CMS 接口仅 {len(val_cms)} 个，触发熔断保留上一版 tvbox.json。"
        )
        with open("tvbox.json", "r", encoding="utf-8") as f:
            final_tv_meta = ensure_standard_refs(
                json.load(f),
                settings,
                spider_md5=spider_md5,
                version_tag=version_tag,
            )
        final_tv = dict(final_tv_meta)
    else:
        douban = build_douban_site()
        cms_sites = [
            {
                "key": i["key"],
                "name": f"⚡{normalize_site_display_name(i['name'])}({i.get('latency', 888)}ms)",
                "type": 1,
                "api": i["api"],
                "searchable": 1,
                "quickSearch": 1,
                "filterable": 1,
                "_latency": i.get("latency", 888),
                "_health_score": i.get("health_score", 60),
                "_categories": i.get("categories", []),
                "_raw_classes": i.get("_raw_classes", []),
            }
            for i in val_cms
        ]

        seen_keys, seen_sigs, base_sites = set(), set(), []

        def reg_site(s, allow_rename=True):
            if is_douban_home_site(s) and s.get("key") != "douban_rank":
                return False
            k = str(s.get("key", "")).strip()
            ext_sig = (
                re.sub(r"^https?://", "", str(s.get("ext", "")).strip().lower())
                .rstrip("/")
            )
            sig = (s.get("type"), str(s.get("api", "")).strip().rstrip("/"), ext_sig)
            if not k or sig in seen_sigs:
                return False
            if k in seen_keys:
                if not allow_rename:
                    return False
                k = f"{s.get('_src_id', 'dup')}_{k}"
                if k in seen_keys:
                    return False
            s["key"] = k
            seen_keys.add(k)
            seen_sigs.add(sig)
            return True

        if reg_site(douban, allow_rename=False):
            base_sites.append(douban)
        for s in cms_sites + rank_pan_spider_sites(
            seeds.get("pan_spider_sites", []), verify_ext_top_n=10
        ):
            if reg_site(s, allow_rename=True):
                base_sites.append(s)

        ex_alive, jar_stats = verify_and_mirror_assets(
            ex_sites, settings, pan_kws
        )
        ex_alive.sort(
            key=lambda x: (
                not x.get("_is_priority", False),
                not str(x.get("jar", "")).startswith("./jars/"),
                int(x.get("_src_latency", 500) or 500),
            )
        )
        dedup_ex = [s for s in ex_alive if reg_site(s, allow_rename=True)]
        if max_extra_spider_full > 0 and len(dedup_ex) > max_extra_spider_full:
            dedup_ex = dedup_ex[:max_extra_spider_full]

        merged_raw = (
            base_sites
            + [s for s in dedup_ex if not is_pan_site(s, pan_kws, invalid_kws)]
            + [s for s in dedup_ex if is_pan_site(s, pan_kws, invalid_kws)]
        )
        merged_raw = _sanitize_sites_with_blacklist(
            merged_raw, mode="full", extra_dead_hosts=dead_hosts
        )
        merged_raw = _dedup_cms_sites_normalized(merged_raw, strict_host_only=False)
        merged = arrange_sites_smart_order(merged_raw, settings, rules)
        merged = _rebalance_tv_site_tiers(
            merged,
            settings,
            pan_kws,
            invalid_kws,
            enrich_top_n=int(th.get("enrich_cms_filters_top_n", 25)),
        )

        pan_cnt = sum(1 for s in merged if is_pan_site(s, pan_kws, invalid_kws))
        if (
            len(merged) < min_full_sites
            or pan_cnt < min_pan_sites
        ) and os.path.exists("tvbox.json"):
            quality_gate_triggered = True
            alerts.append(
                f"黄金门禁触发（全量 {len(merged)} / 网盘 {pan_cnt}），保留上一版配置。"
            )
            with open("tvbox.json", "r", encoding="utf-8") as f:
                final_tv_meta = ensure_standard_refs(
                    json.load(f),
                    settings,
                    spider_md5=spider_md5,
                    version_tag=version_tag,
                )
            final_tv = dict(final_tv_meta)
        else:
            final_tv_meta = ensure_standard_refs(
                {
                    "updated_at": tag,
                    "spider": "./custom_spider.jar",
                    "wallpaper": "https://eees.cc",
                    "lives": [],
                    "sites": merged,
                    "parses": filter_and_sort_parses(
                        ex_parses or tv_base.get("parses", [])
                    ),
                    "flags": tv_base.get("flags", []),
                    "ijk": tv_base.get("ijk", []),
                    "ads": tv_base.get("ads", []),
                },
                settings,
                spider_md5=spider_md5,
                version_tag=version_tag,
            )
            final_tv = {**final_tv_meta, "sites": strip_internal_meta(merged)}

    if not quality_gate_triggered or not os.path.exists("tvbox_lite.json"):
        lite_tv = build_lite_config(
            final_tv_meta, tag, settings, rules, spider_md5, version_tag=version_tag
        )
        if isinstance(lite_tv.get("sites"), list):
            lite_sites = _sanitize_sites_with_blacklist(
                lite_tv["sites"], mode="lite", extra_dead_hosts=dead_hosts
            )
            lite_sites = _dedup_cms_sites_normalized(lite_sites, strict_host_only=True)
            lite_tv["sites"] = strip_internal_meta(
                _rebalance_tv_site_tiers(
                    lite_sites,
                    settings,
                    pan_kws,
                    invalid_kws,
                    enrich_top_n=int(th.get("enrich_cms_filters_top_n", 25)),
                )
            )
    else:
        with open("tvbox_lite.json", "r", encoding="utf-8") as f:
            lite_tv = json.load(f)

    if not quality_gate_triggered or not os.path.exists("tvbox_pan.json"):
        pan_tv = build_pan_only_config(
            final_tv_meta, tag, settings, rules, spider_md5, version_tag=version_tag
        )
        if isinstance(pan_tv.get("sites"), list):
            pan_tv["sites"] = _sanitize_sites_with_blacklist(
                pan_tv["sites"], mode="full", extra_dead_hosts=dead_hosts
            )
    else:
        with open("tvbox_pan.json", "r", encoding="utf-8") as f:
            pan_tv = json.load(f)

    def check_cf(domain):
        d = domain.rstrip("/")
        host = d.replace("https://", "").replace("http://", "")
        t0 = time.time()
        try:
            with urllib.request.urlopen(
                urllib.request.Request(f"{d}/tvbox.json", headers=HEADERS), timeout=6
            ) as r:
                if r.status == 200 and b'"spider"' in r.read(512):
                    return {
                        "base": d,
                        "host": host,
                        "lat": round((time.time() - t0) * 1000),
                        "ok": True,
                    }
        except Exception:
            pass
        return {"base": d, "host": host, "lat": 999, "ok": False}

    cf_sorted = sorted(
        run_parallel(check_cf, settings.get("urls", {}).get("cf_domains", []), workers=4),
        key=lambda x: (not x["ok"], x["lat"]),
    )

    cf_primary_entries, cf_backup_entries, cf_failed_entries = [], [], []
    healthy_idx = 0
    for c in cf_sorted:
        if c["ok"]:
            healthy_idx += 1
            trio = [
                {
                    "url": append_cache_buster(f"{c['base']}/tvbox_lite.json", version_tag),
                    "name": f"⚡CF精简{healthy_idx} [{c['host']}] ({c['lat']}ms)",
                },
                {
                    "url": append_cache_buster(f"{c['base']}/tvbox_pan.json", version_tag),
                    "name": f"💎CF网盘{healthy_idx} [{c['host']}] ({c['lat']}ms)",
                },
                {
                    "url": append_cache_buster(f"{c['base']}/tvbox.json", version_tag),
                    "name": f"👑CF全量{healthy_idx} [{c['host']}] ({c['lat']}ms)",
                },
            ]
            if healthy_idx == 1:
                cf_primary_entries.extend(trio)
            else:
                cf_backup_entries.extend(trio)
        else:
            cf_failed_entries.append(
                {
                    "url": append_cache_buster(f"{c['base']}/tvbox.json", version_tag),
                    "name": f"👑CF冷备 [{c['host']}]",
                }
            )

    full_px, lite_px = build_and_save_proxy_pools(
        registry.get("discovery_pages", []), settings, seeds, dead_hosts=dead_hosts
    )

    for px_list in (full_px, lite_px):
        for p in px_list:
            for k in ("url", "lite_url", "pan_url"):
                if p.get(k):
                    p[k] = _ensure_raw_branch_url(p[k], repo_owner_path, repo_branch)

    # 统一为配置中的裸连 GitHub 链接加上测速优选代理前缀
    preferred_gh_proxy = _resolve_preferred_gh_proxy_prefix(full_px, lite_px)
    for cfg_obj in (final_tv, lite_tv, pan_tv):
        _apply_preferred_github_proxy(
            cfg_obj, preferred_gh_proxy, repo_owner_path, repo_branch
        )

    if not quality_gate_triggered:
        with open("tvbox.json", "w", encoding="utf-8") as f:
            json.dump(final_tv, f, ensure_ascii=False, indent=2)
        with open("tvbox_lite.json", "w", encoding="utf-8") as f:
            json.dump(lite_tv, f, ensure_ascii=False, indent=2)
        with open("tvbox_pan.json", "w", encoding="utf-8") as f:
            json.dump(pan_tv, f, ensure_ascii=False, indent=2)

    # 第一屏直达双代理（最速代理精简 + 最速代理网盘）与其余灾备代理分离
    proxy_top_entries, proxy_rest_entries = [], []
    if lite_px:
        top1 = lite_px[0]
        if top1.get("lite_url"):
            proxy_top_entries.append(
                {
                    "url": append_cache_buster(top1["lite_url"], version_tag),
                    "name": top1["lite_name"],
                }
            )
        if top1.get("pan_url"):
            proxy_top_entries.append(
                {
                    "url": append_cache_buster(top1["pan_url"], version_tag),
                    "name": top1["pan_name"],
                }
            )

    top_seen_urls = {x["url"] for x in proxy_top_entries}
    for item in (
        [
            {
                "url": append_cache_buster(p["lite_url"], version_tag),
                "name": p["lite_name"],
            }
            for p in lite_px
            if p.get("lite_url")
        ]
        + [
            {
                "url": append_cache_buster(p["pan_url"], version_tag),
                "name": p["pan_name"],
            }
            for p in lite_px
            if p.get("pan_url")
        ]
        + [
            {
                "url": append_cache_buster(p["url"], version_tag),
                "name": p["name"],
            }
            for p in full_px
            if p.get("url")
        ]
    ):
        if item["url"] not in top_seen_urls:
            top_seen_urls.add(item["url"])
            proxy_rest_entries.append(item)

    fallback_entries = []
    if cf_sorted and cf_sorted[0]["ok"]:
        fastest_cf = cf_sorted[0]
        with open("tvbox_cf_abs.json", "w", encoding="utf-8") as f:
            json.dump(
                build_absolute_fallback_config(
                    lite_tv, tag, fastest_cf["base"], settings, spider_md5, version_tag
                ),
                f,
                ensure_ascii=False,
                indent=2,
            )
        fallback_entries.append(
            {
                "url": append_cache_buster(
                    f"{fastest_cf['base']}/tvbox_cf_abs.json", version_tag
                ),
                "name": f"🛡️CF应急绝对路径 [{fastest_cf['host']}] ({fastest_cf['lat']}ms)",
            }
        )

    proxy_pool_for_abs = lite_px or full_px
    for idx_abs, (fp, out_fname, label_prefix) in enumerate(
        zip(
            proxy_pool_for_abs[:2],
            ("tvbox_proxy_abs.json", "tvbox_proxy_abs2.json"),
            ("🛡️代理应急主线", "🛡️代理应急备线"),
        ),
        1,
    ):
        prefix = fp["prefix"] if fp["prefix"].endswith("/") else f"{fp['prefix']}/"
        raw_base = f"{prefix}{GH_RAW}/{repo_owner_path}/{repo_branch}"
        with open(out_fname, "w", encoding="utf-8") as f:
            json.dump(
                build_absolute_fallback_config(
                    lite_tv, tag, raw_base, settings, spider_md5, version_tag
                ),
                f,
                ensure_ascii=False,
                indent=2,
            )
        fallback_entries.append(
            {
                "url": append_cache_buster(
                    f"{raw_base}/{out_fname}", version_tag
                ),
                "name": f"{label_prefix}{idx_abs} [标{fp['discovery_order']}] ({fp['latency']}ms)",
            }
        )

    # 电视遥控器第一屏黄金顺序：主推CF1(精简/网盘/全量) -> 最速双代理 -> 应急绝对路径 -> 备用CF2~4 -> 其余灾备代理
    dc_urls = (
        cf_primary_entries
        + proxy_top_entries
        + fallback_entries
        + cf_backup_entries
        + proxy_rest_entries
        + cf_failed_entries
    )

    if dc_urls:
        with open("dc.json", "w", encoding="utf-8") as f:
            json.dump({"urls": dc_urls}, f, ensure_ascii=False, indent=2)

    pan_total = sum(
        1 for s in final_tv.get("sites", []) if is_pan_site(s, pan_kws, invalid_kws)
    )
    write_actions_summary(
        tag,
        len(final_tv["sites"]),
        len(lite_tv["sites"]),
        len(pan_tv["sites"]),
        pan_total,
        live_cnt,
        len(dc_urls),
        jar_stats,
        source_stats,
        full_px,
        lite_px,
        registry,
        alerts,
        prev_counts=prev_counts,
    )
    print(
        f"完成！全量 {len(final_tv['sites'])} 站 / 精简 {len(lite_tv['sites'])} 站 / "
        f"网盘 {len(pan_tv['sites'])} 站 / 直播 {live_cnt} 条"
    )


if __name__ == "__main__":
    main()
