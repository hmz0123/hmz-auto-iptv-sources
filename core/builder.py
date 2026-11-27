import json
import os
import random
import re
import time
import urllib.parse
import urllib.request
from core.utils import (
    DEFAULT_DEAD_OR_JUNK_WORDS,
    HEADERS,
    OKHTTP_HEADERS,
    append_cache_buster,
    fetch_url_text,
    idn_encode,
    is_adult_or_sensitive_site,
    is_invalid_site_url,
    is_pan_site,
    parse_json_lenient,
    resolve_relative_url,
    run_parallel,
)

GH_RAW = "https://raw.githubusercontent.com"

# 内置外部公共 GitHub 加速代理监控榜单（作为 discovery_pages 的补充猎手源）
BUILTIN_PROXY_RADAR_URLS = [
    "https://ghproxy.link/",
    "https://mirror.ghproxy.com/",
]

# 电视端网盘统一 Token 挂载协议路径（兼容 OK影视PRO / 影视仓 / NEWBOX / TVBox 饭太硬与 OK 系 JAR）
DEFAULT_CLOUD_DRIVE_PATH = "tvfan/Cloud-drive.txt"


def write_pan_token_template(filepath="token_template.json"):
    """生成电视端（OK影视PRO / 影视仓 / NEWBOX / TVBox）统一网盘 Token 配置模板"""
    template = {
        "_说明": [
            "1. 适用播放器：OK影视PRO、影视仓、NEWBOX、TVBox（FongMi/Takagen99 版）。",
            "2. 使用方法（二选一）：",
            "   方法A（推荐）：在 OK影视PRO / 影视仓 设置 -> 网盘配置 中直接扫码登录阿里/夸克/UC，播放器会自动全局注入。",
            "   方法B（通用文件）：将本文件填写对应 Token/Cookie 后，重命名为 Cloud-drive.txt 并放入电视本地目录 /sdcard/TVBox/tvfan/Cloud-drive.txt（或 /sdcard/tvfan/Cloud-drive.txt）。",
            "3. 下面字段已覆盖饭太硬系、PG系、OK系、王二小系等主流网盘爬虫 JAR 的读取键名。"
        ],
        "ali_token": "在此填入阿里云盘32位refresh_token",
        "quark_cookie": "在此填入夸克网盘网页版Cookie",
        "uc_cookie": "在此填入UC网盘网页版Cookie",
        "115_cookie": "在此填入115网盘Cookie（可选）",
        "tianyi_cookie": "在此填入天翼云盘Cookie（可选）",
        "baidu_cookie": "在此填入百度网盘BDUSS等Cookie（可选）",
        "thread": 6,
        "chunk_size": 512,
        "danmu": True
    }
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(template, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"写入网盘 Token 模板失败: {e}")


def _normalize_pan_site_ext(site):
    """
    规范化网盘站点 ext 传参结构：
    - 若 ext 为 JSON 字符串或 dict，统一补充 Cloud-drive 协议路径以便一键联动本地 Token；
    - 若 ext 为纯 http(s) 网址字符串，保持原样以兼容仅接收 URL 的网盘爬虫。
    """
    sc = dict(site)
    ext = sc.get("ext")
    if isinstance(ext, dict):
        new_ext = dict(ext)
        new_ext.setdefault("Cloud-drive", DEFAULT_CLOUD_DRIVE_PATH)
        sc["ext"] = json.dumps(new_ext, ensure_ascii=False)
    elif isinstance(ext, str):
        stripped = ext.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            parsed = parse_json_lenient(stripped)
            if isinstance(parsed, dict):
                parsed.setdefault("Cloud-drive", DEFAULT_CLOUD_DRIVE_PATH)
                sc["ext"] = json.dumps(parsed, ensure_ascii=False)
    return sc


def build_douban_site():
    """增强版豆瓣源：内置标准影视/榜单分类 ext，避免电视端顶部分类栏为空"""
    return {
        "key": "douban_rank",
        "name": "🎬豆瓣热播·评分排行榜",
        "type": 3,
        "api": "csp_Douban",
        "searchable": 1,
        "quickSearch": 1,
        "filterable": 1,
        "ext": json.dumps(
            {
                "class": [
                    {"type_id": "movie", "type_name": "电影"},
                    {"type_id": "tv", "type_name": "电视剧"},
                    {"type_id": "show", "type_name": "综艺"},
                    {"type_id": "anime", "type_name": "动漫"},
                    {"type_id": "top250", "type_name": "豆瓣Top250"},
                    {"type_id": "weekly_movie", "type_name": "一周口碑电影"},
                    {"type_id": "weekly_tv", "type_name": "全球口碑剧集"},
                ]
            },
            ensure_ascii=False,
        ),
    }


def _extract_m3u8_urls(detail_text):
    if not detail_text:
        return []
    found = re.findall(r"(https?://[^\s\"'<>#\\$]+?\.m3u8[^\s\"'<>]*)", detail_text)
    if not found:
        for m in re.findall(r"(https?://[^\s\"'<>#\\$]+)", detail_text):
            if ".m3u8" in m.lower():
                found.append(m)
    return list(dict.fromkeys(found))


def _extract_ts_segments(media_text, media_url):
    segs = []
    lines = media_text.splitlines()
    for idx, line in enumerate(lines):
        if line.strip().startswith("#EXTINF") and idx + 1 < len(lines):
            nxt = lines[idx + 1].strip()
            if nxt and not nxt.startswith("#"):
                segs.append(resolve_relative_url(media_url, nxt))
    return segs


def _probe_single_cms_segment_speed(site):
    """对单个 CMS 站做轻量 TS 分片测速，返回 (speed_kbs, ttfb_ms)"""
    try:
        api = str(site.get("api", "")).strip()
        if not api.startswith(("http://", "https://")):
            return 0, 9999
        base_api = re.sub(r"[?&]ac=list.*", "", api.rstrip("/"))
        sep = "&" if "?" in base_api else "?"

        try:
            list_txt = fetch_url_text(f"{base_api}{sep}ac=list&pg=1", timeout=3.5)
        except Exception:
            return 0, 9999
        if not list_txt or len(list_txt) < 40:
            return 0, 9999

        vod_ids = []
        if str(site.get("type", 1)) == "0":
            vod_ids = re.findall(r"<id>(\d+)</id>", list_txt)[:2]
        else:
            try:
                parsed_list = parse_json_lenient(list_txt)
            except Exception:
                return 0, 9999
            if isinstance(parsed_list, dict) and isinstance(parsed_list.get("list"), list):
                vod_ids = [
                    str(item["vod_id"])
                    for item in parsed_list["list"][:2]
                    if isinstance(item, dict) and item.get("vod_id") is not None
                ]
        if not vod_ids:
            return 0, 9999

        m3u8_candidates = []
        for vid in vod_ids:
            try:
                detail_txt = fetch_url_text(
                    f"{base_api}{sep}ac=detail&ids={urllib.parse.quote(vid)}", timeout=3.5
                )
                m3u8_candidates = _extract_m3u8_urls(detail_txt)[:2]
                if m3u8_candidates:
                    break
            except Exception:
                continue
        if not m3u8_candidates:
            return 0, 9999

        for play_url in m3u8_candidates:
            t0 = time.time()
            try:
                master_txt = fetch_url_text(play_url, timeout=3.5, prefer_okhttp=True)
                ttfb_ms = round((time.time() - t0) * 1000)
            except Exception:
                continue
            if not master_txt:
                continue

            media_url = ""
            if "#EXT-X-STREAM-INF" in master_txt:
                lines = master_txt.splitlines()
                for idx, line in enumerate(lines):
                    if "STREAM-INF" in line and idx + 1 < len(lines):
                        sub = lines[idx + 1].strip()
                        if sub and not sub.startswith("#"):
                            media_url = resolve_relative_url(play_url, sub)
                            break
            elif "#EXTINF" in master_txt:
                media_url = play_url
            if not media_url:
                continue

            try:
                media_txt = fetch_url_text(media_url, timeout=3.5, prefer_okhttp=True)
            except Exception:
                continue
            segs = _extract_ts_segments(media_txt, media_url)
            if not segs:
                continue

            total_bytes, total_sec, ok_cnt = 0, 0.0, 0
            for seg_url in segs[:3]:
                if ok_cnt >= 2:
                    break
                ts_start = time.time()
                try:
                    req = urllib.request.Request(seg_url, headers=OKHTTP_HEADERS)
                    with urllib.request.urlopen(req, timeout=2.8) as resp:
                        if 200 <= resp.status < 300:
                            chunk = resp.read(131072)
                            elapsed = max(0.02, time.time() - ts_start)
                            if len(chunk) >= 1024:
                                total_bytes += len(chunk)
                                total_sec += elapsed
                                ok_cnt += 1
                except Exception:
                    continue

            if ok_cnt >= 2 and total_sec > 0:
                speed_kbs = round((total_bytes / 1024.0) / total_sec, 1)
                return speed_kbs, ttfb_ms

        return 0, 9999
    except Exception:
        return 0, 9999


def speedtest_cms_segments(cms_sites, top_n=12, workers=16):
    """并发对前 top_n 个 CMS 采集站做 TS 分片真实下载测速"""
    if not cms_sites or top_n <= 0:
        return cms_sites
    effective_top = min(int(top_n), 12)
    head = [dict(s) for s in cms_sites[:effective_top]]
    tail = [dict(s) for s in cms_sites[effective_top:]]

    def run_one(site):
        spd, ttfb = _probe_single_cms_segment_speed(site)
        sc = dict(site)
        sc["_seg_speed_kbs"] = spd
        sc["_seg_ttfb_ms"] = ttfb
        if spd > 0:
            clean_name = re.sub(r"\(\d+ms\)$", "", str(sc.get("name", "")))
            sc["name"] = f"{clean_name}·{int(spd)}KB/s({ttfb}ms)"
        return sc

    probed_head = run_parallel(run_one, head, workers=workers)
    probed_head.sort(
        key=lambda x: (
            x.get("_seg_speed_kbs", 0) <= 0,
            -float(x.get("_seg_speed_kbs", 0)),
            int(x.get("_seg_ttfb_ms", 9999)),
        )
    )
    ok_n = sum(1 for x in probed_head if x.get("_seg_speed_kbs", 0) > 0)
    print(f"CMS 分片真测速完成：抽测 {len(probed_head)} 个 -> 分片通畅 {ok_n} 个")
    return probed_head + tail


def _match_keywords(site, keywords):
    if not keywords:
        return False
    blob = f"{site.get('key', '')} {site.get('name', '')} {site.get('api', '')} {site.get('ext', '')}".lower()
    return any(str(k).strip().lower() in blob for k in keywords if str(k).strip())


def arrange_sites_smart_order(sites, settings, rules):
    """
    豆瓣置顶 + 分层黄金前排：
    - 第 1 位：增强版豆瓣热播榜
    - 第 2 位起：置顶网盘站 -> 置顶秒播采集站 -> 其余常规站
    """
    pinned_cfg = settings.get("pinned", {})
    home_kws = pinned_cfg.get("home_prefer_cms_keywords", [])
    cms_kws = pinned_cfg.get("cms_keywords", [])
    pan_kws = pinned_cfg.get("pan_keywords", rules.get("pan_keywords", []))
    invalid_kws = tuple(rules.get("junk_keywords", [])) + DEFAULT_DEAD_OR_JUNK_WORDS
    qs_top_n = int(pinned_cfg.get("quick_search_top_n", 12))

    douban = [s for s in sites if s.get("key") == "douban_rank"] or [build_douban_site()]
    others = [s for s in sites if s.get("key") != "douban_rank"]

    cms_candidates = [
        s for s in others if s.get("type") in (0, 1) and not is_pan_site(s, pan_kws, invalid_kws)
    ]
    pan_candidates = [
        _normalize_pan_site_ext(s) for s in others if is_pan_site(s, pan_kws, invalid_kws)
    ]
    spider_other = [
        s
        for s in others
        if s.get("type") not in (0, 1) and not is_pan_site(s, pan_kws, invalid_kws)
    ]

    def cms_rank_tuple(s):
        spd = float(s.get("_seg_speed_kbs", 0) or 0)
        is_home_kw = _match_keywords(s, home_kws)
        is_cms_kw = _match_keywords(s, cms_kws)
        health = float(s.get("_health_score", 60.0) or 60.0)
        m = re.search(r"\((\d+)ms\)", str(s.get("name", "")))
        api_lat = int(m.group(1)) if m else 999
        return (
            not (spd > 0 and is_home_kw),
            not (spd > 0 and is_cms_kw),
            spd <= 0,
            -spd,
            not is_cms_kw,
            -round(health / 10),
            api_lat,
        )

    def pan_rank_tuple(s):
        is_pinned = _match_keywords(s, pan_kws)
        has_local_jar = str(s.get("jar", "")).startswith("./jars/")
        is_prio = bool(s.get("_is_priority", False))
        src_lat = int(s.get("_src_latency", 500) or 500)
        return (not is_pinned, not has_local_jar, not is_prio, src_lat)

    cms_sorted = sorted(cms_candidates, key=cms_rank_tuple)
    pan_sorted = sorted(pan_candidates, key=pan_rank_tuple)

    top_pan = pan_sorted[:4]
    rem_pan = pan_sorted[4:]
    top_cms = cms_sorted[:6]
    tail_cms = cms_sorted[6:]

    ordered = (
        douban[:1]
        + top_pan
        + top_cms
        + spider_other
        + rem_pan
        + tail_cms
    )

    final_ordered = []
    for idx, item in enumerate(ordered):
        sc = dict(item)
        if sc.get("key") != "douban_rank":
            sc["quickSearch"] = 1 if idx < qs_top_n else 0
        final_ordered.append(sc)
    return final_ordered


def ensure_standard_refs(
    tv_obj, settings, spider_md5="", abs_base_prefix=None, version_tag=""
):
    if not isinstance(tv_obj, dict):
        return tv_obj
    urls_cfg = settings.get("urls", {})
    md5_suffix = f";md5;{spider_md5}" if spider_md5 else ""
    if abs_base_prefix:
        base = abs_base_prefix.rstrip("/")
        spider_ref, live_ref = (
            f"{base}/custom_spider.jar{md5_suffix}",
            append_cache_buster(f"{base}/tvbox_live.txt", version_tag),
        )
    else:
        spider_ref, live_ref = (
            f"./custom_spider.jar{md5_suffix}",
            append_cache_buster("./tvbox_live.txt", version_tag),
        )

    tv_obj["spider"] = spider_ref
    tv_obj["lives"] = [
        {
            "name": "自动测速精选直播",
            "type": 0,
            "url": live_ref,
            "playerType": int(settings.get("default_live_player_type", 2)),
            "ua": "okhttp/3.15",
            "epg": urls_cfg.get("live_epg", "https://epg.112114.xyz/"),
            "logo": urls_cfg.get("live_logo", "https://epg.112114.xyz/logo/{name}.png"),
        }
    ]
    tv_obj["flags"] = list(settings.get("default_player_flags", []))
    tv_obj["ijk"] = list(settings.get("default_ijk_config", []))
    return tv_obj


def strip_internal_meta(site_list):
    return [{k: v for k, v in s.items() if not k.startswith("_")} for s in site_list]


def _probe_pan_ext_page(ext_url, timeout=3.5):
    """对网盘站 ext 网页做轻量可用性校验"""
    bad_markers = ("域名出售", "正在维护", "系统维护", "网站维护", "404 not found", "access denied", "error 1020")
    good_markers = ("搜索", "电影", "电视剧", "vod", "网盘", "资源", "片单")
    try:
        txt = fetch_url_text(ext_url, timeout=timeout, prefer_okhttp=True)
        if not txt or len(txt.strip()) < 60:
            return False
        plain = re.sub(r"<[^>]+>", " ", txt).lower()
        if any(b.lower() in plain for b in bad_markers):
            return False
        return any(g.lower() in plain for g in good_markers) or "<title" in txt.lower()
    except Exception:
        return False


def rank_pan_spider_sites(pan_sites, verify_ext_top_n=10):
    def check_pan(idx_site):
        idx, site = idx_site
        s = dict(site)
        ext = str(s.get("ext", "")).strip()
        if not ext.startswith(("http://", "https://")):
            return (1, 500, idx, s, True)
        t0 = time.time()
        try:
            req = urllib.request.Request(idn_encode(ext), headers=OKHTTP_HEADERS)
            with urllib.request.urlopen(req, timeout=4) as resp:
                if resp.status < 500:
                    lat = round((time.time() - t0) * 1000)
                    clean_name = re.sub(r"\(\d+ms\)$", "", s.get("name", ""))
                    s["name"] = f"{clean_name}({lat}ms)"
                    return (0, lat, idx, s, True)
        except Exception:
            pass
        return (2, 9999, idx, s, False)

    checked = run_parallel(check_pan, list(enumerate(pan_sites)), workers=10)
    checked.sort(key=lambda x: (x[0], x[1], x[2]))

    probe_targets = [
        (rank_i, item[3])
        for rank_i, item in enumerate(checked)
        if item[0] == 0 and str(item[3].get("ext", "")).strip().startswith(("http://", "https://"))
    ][: max(0, int(verify_ext_top_n))]

    if probe_targets:
        def check_page(pair):
            rank_i, st = pair
            ok = _probe_pan_ext_page(str(st.get("ext", "")).strip(), timeout=3.5)
            return (rank_i, ok)

        page_res = dict(run_parallel(check_page, probe_targets, workers=8))
        kept = []
        dropped_fake = 0
        for rank_i, item in enumerate(checked):
            if rank_i in page_res and not page_res[rank_i]:
                dropped_fake += 1
                continue
            kept.append(item[3])
        if dropped_fake:
            print(f"网盘 ext 首页内容探活：过滤疑似伪活网盘 {dropped_fake} 个")
        return kept

    return [x[3] for x in checked]


def _normalize_parse_entry(p):
    out = dict(p)
    raw_t = out.get("type", 0)
    try:
        out["type"] = int(float(str(raw_t).strip())) if not isinstance(raw_t, bool) else 0
    except Exception:
        out["type"] = 0

    if "playerType" in out:
        raw_pt = out.get("playerType")
        try:
            if isinstance(raw_pt, bool):
                del out["playerType"]
            else:
                out["playerType"] = int(float(str(raw_pt).strip()))
        except Exception:
            del out["playerType"]

    def clean_flags(flist):
        res, seen_f = [], set()
        for item in flist:
            val = re.sub(r"\\[nrt]|[\x00-\x1f\x7f]", "", str(item)).strip()
            if val and val not in seen_f:
                seen_f.add(val)
                res.append(val)
        return res

    if isinstance(out.get("ext"), dict) and isinstance(out["ext"].get("flag"), list):
        new_ext = dict(out["ext"])
        new_ext["flag"] = clean_flags(new_ext["flag"])
        out["ext"] = new_ext
    if isinstance(out.get("flag"), list):
        out["flag"] = clean_flags(out["flag"])
    return out


def _is_real_parse_response(p_type, body_text):
    """
    真实播放解析校验（防失效占位视频与广告劫持）：
    - type=1（JSON 解析）：必须能解析为 JSON 且包含有效的视频播放链接（非 404/error 占位链）
    - type=0 / 2（Web 嗅探解析）：必须返回正常 HTML 页面（非空白或 404/防火墙/广告拦截页）
    """
    if not body_text:
        return False
    txt = body_text.strip()
    bad_play_markers = ("404.mp4", "error.m3u8", "err.mp4", "invalid", "expired", "解析失败", "请联系", "授权过期", "澳门", "博彩")
    if p_type == 1:
        try:
            data = parse_json_lenient(txt)
            if isinstance(data, dict):
                code_val = data.get("code")
                if code_val is not None and str(code_val).strip() not in ("200", "0", "1", "true", "True"):
                    return False
                play_u = str(data.get("url") or data.get("play_url") or "").strip()
                if (
                    play_u.startswith(("http://", "https://"))
                    and not is_invalid_site_url(play_u)
                    and not any(bm in play_u.lower() for bm in bad_play_markers)
                ):
                    return True
        except Exception:
            pass
        return False

    lower_txt = txt.lower()
    if len(txt) < 80:
        return False
    bad_markers = ("404 not found", "502 bad gateway", "access denied", "error 1020", "防火墙", "域名出售", "已过期")
    if any(b in lower_txt for b in bad_markers):
        return False
    return "<html" in lower_txt or "<script" in lower_txt or "player" in lower_txt


def filter_and_sort_parses(parses):
    if not parses:
        return []
    seen, unique = set(), []
    for p in parses:
        if not isinstance(p, dict):
            continue
        u = str(p.get("url", "")).strip()
        if not u or u in seen:
            continue
        norm_p = _normalize_parse_entry(p)
        is_pseudo = norm_p.get("type") == 3 and u in ("Demo", "Web")
        if not is_pseudo and is_invalid_site_url(u):
            continue
        seen.add(u)
        unique.append(norm_p)

    def check_parse(idx_p):
        idx, p = idx_p
        u = str(p.get("url", "")).strip()
        if not u.startswith(("http://", "https://")):
            return (2, 500, idx, p)
        p_type = int(p.get("type", 0) or 0)
        t0 = time.time()
        try:
            test_target = u + ("https://v.qq.com/x/cover/mzc00200mp8jaac.html" if u.endswith(("=", "?")) else "")
            req = urllib.request.Request(test_target, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=3.5) as resp:
                if resp.status in (200, 301, 302):
                    lat = round((time.time() - t0) * 1000)
                    body = resp.read(4096).decode("utf-8", errors="ignore")
                    if _is_real_parse_response(p_type, body):
                        # type=1 JSON 直连解析优先于 type=0 Web 嗅探解析
                        type_prio = 0 if p_type == 1 else 1
                        return (type_prio, lat, idx, p)
        except Exception:
            pass
        return None

    valid = run_parallel(check_parse, list(enumerate(unique[:40])), workers=16)
    valid.sort(key=lambda x: (x[0], x[1], x[2]))
    selected = [x[3] for x in valid][:10] if valid else unique[:10]
    http_ok = sum(1 for x in valid if x[0] in (0, 1))
    print(f"官采解析器（parses）探活完成：有效 {http_ok} 个 -> 精选留存 {len(selected)} 个")
    return selected


def build_lite_config(full_tv_with_meta, tag, settings, rules, spider_md5="", version_tag=""):
    """生成精简版 tvbox_lite.json（豆瓣置顶 + 分片测速筛选 + 过滤成人/敏感站点）"""
    ed = settings.get("editions", {})
    pan_kws = settings.get("pinned", {}).get("pan_keywords", rules.get("pan_keywords", []))
    invalid_kws = tuple(rules.get("junk_keywords", [])) + DEFAULT_DEAD_OR_JUNK_WORDS
    adult_kws = rules.get("lite_only_adult_keywords", [])
    sites = [
        s
        for s in full_tv_with_meta.get("sites", [])
        if not is_adult_or_sensitive_site(s, adult_kws)
    ]

    max_src_lat = int(ed.get("lite_max_source_latency", 1200))
    max_pan = int(ed.get("lite_max_pan_sites", 25))
    max_extra = int(ed.get("lite_max_extra_spider_sites", 25))
    max_cms_lat = int(ed.get("lite_max_cms_latency", 900))
    max_cms = int(ed.get("lite_max_cms_sites", 25))

    pan_list = sorted(
        [
            _normalize_pan_site_ext(s)
            for s in sites
            if s.get("key") != "douban_rank" and is_pan_site(s, pan_kws, invalid_kws)
        ],
        key=lambda x: (
            0 if "_src_id" not in x else 1,
            not x.get("_is_priority", False),
            int(x.get("_src_latency", 500) or 500),
        ),
    )
    fast_pan = (
        [
            s
            for s in pan_list
            if int(s.get("_src_latency", 500) or 500) <= max_src_lat
        ][:max_pan]
        or pan_list[:max_pan]
    )

    other_list = sorted(
        [
            s
            for s in sites
            if s.get("key") != "douban_rank"
            and s.get("type") not in (0, 1)
            and not is_pan_site(s, pan_kws, invalid_kws)
        ],
        key=lambda x: (
            not str(x.get("jar", "")).startswith("./jars/"),
            not x.get("_is_priority", False),
            int(x.get("_src_latency", 500) or 500),
        ),
    )
    fast_other = [
        s
        for s in other_list
        if int(s.get("_src_latency", 500) or 500) <= max_src_lat
    ][:max_extra]

    cms_list = [
        s
        for s in sites
        if s.get("type") in (0, 1) and not is_pan_site(s, pan_kws, invalid_kws)
    ]
    fast_cms = []
    for s in cms_list:
        m = re.search(r"\((\d+)ms\)", str(s.get("name", "")))
        if (int(m.group(1)) if m else 999) <= max_cms_lat:
            fast_cms.append(s)
        if len(fast_cms) >= max_cms:
            break
    fast_cms = fast_cms or cms_list[: min(12, len(cms_list))]

    probed_cms = speedtest_cms_segments(
        fast_cms,
        top_n=int(ed.get("lite_cms_speedtest_top_n", 12)),
        workers=16,
    )
    min_spd = float(ed.get("lite_min_seg_speed_kbs", 150))
    speed_passed_cms = [
        s for s in probed_cms if float(s.get("_seg_speed_kbs", 0) or 0) >= min_spd
    ]
    final_cms = speed_passed_cms if len(speed_passed_cms) >= 5 else probed_cms

    ordered_sites = arrange_sites_smart_order(
        [build_douban_site()] + fast_other + final_cms + fast_pan,
        settings,
        rules,
    )

    return ensure_standard_refs(
        {
            "updated_at": tag,
            "spider": "./custom_spider.jar",
            "wallpaper": full_tv_with_meta.get("wallpaper", "https://eees.cc"),
            "lives": full_tv_with_meta.get("lives", []),
            "sites": strip_internal_meta(ordered_sites),
            "parses": full_tv_with_meta.get("parses", [])[:8],
            "flags": full_tv_with_meta.get("flags", []),
            "ijk": full_tv_with_meta.get("ijk", []),
            "ads": full_tv_with_meta.get("ads", []),
        },
        settings,
        spider_md5=spider_md5,
        version_tag=version_tag,
    )


def build_pan_only_config(full_tv_with_meta, tag, settings, rules, spider_md5="", version_tag=""):
    write_pan_token_template("token_template.json")
    ed = settings.get("editions", {})
    pan_kws = settings.get("pinned", {}).get("pan_keywords", rules.get("pan_keywords", []))
    invalid_kws = tuple(rules.get("junk_keywords", [])) + DEFAULT_DEAD_OR_JUNK_WORDS
    pan_qs_top = int(ed.get("pan_quick_search_top_n", 12))
    pan_max = int(ed.get("pan_edition_max_sites", 80))
    sites = full_tv_with_meta.get("sites", [])
    pan_list = sorted(
        [
            _normalize_pan_site_ext(s)
            for s in sites
            if s.get("key") != "douban_rank" and is_pan_site(s, pan_kws, invalid_kws)
        ],
        key=lambda x: (
            0 if "_src_id" not in x else 1,
            not str(x.get("jar", "")).startswith("./jars/"),
            not x.get("_is_priority", False),
            int(x.get("_src_latency", 500) or 500),
        ),
    )
    graded = [
        {**s, "quickSearch": 1 if idx < pan_qs_top else 0}
        for idx, s in enumerate(pan_list[:pan_max])
    ]

    return ensure_standard_refs(
        {
            "updated_at": tag,
            "spider": "./custom_spider.jar",
            "wallpaper": full_tv_with_meta.get("wallpaper", "https://eees.cc"),
            "lives": full_tv_with_meta.get("lives", []),
            "sites": strip_internal_meta([build_douban_site()] + graded),
            "parses": full_tv_with_meta.get("parses", [])[:8],
            "flags": full_tv_with_meta.get("flags", []),
            "ijk": full_tv_with_meta.get("ijk", []),
            "ads": full_tv_with_meta.get("ads", []),
        },
        settings,
        spider_md5=spider_md5,
        version_tag=version_tag,
    )


def build_absolute_fallback_config(
    base_tv, tag, abs_base_prefix, settings, spider_md5="", version_tag=""
):
    clean_base = abs_base_prefix.rstrip("/")
    abs_sites = []
    for s in base_tv.get("sites", []):
        item = dict(s)
        if str(item.get("jar", "")).startswith("./"):
            item["jar"] = f"{clean_base}/{item['jar'][2:]}"
        abs_sites.append(item)
    return ensure_standard_refs(
        {
            "updated_at": tag,
            "spider": "",
            "wallpaper": base_tv.get("wallpaper", "https://eees.cc"),
            "lives": [],
            "sites": abs_sites,
            "parses": [dict(p) for p in base_tv.get("parses", [])],
            "flags": list(base_tv.get("flags", [])),
            "ijk": list(base_tv.get("ijk", [])),
            "ads": list(base_tv.get("ads", [])),
        },
        settings,
        spider_md5=spider_md5,
        abs_base_prefix=abs_base_prefix,
        version_tag=version_tag,
    )


def _is_dead_proxy_prefix(prefix, dead_hosts):
    if not prefix:
        return True
    low = str(prefix).strip().lower()
    if any(h and str(h).strip().lower() in low for h in (dead_hosts or ())):
        return True
    host = urllib.parse.urlsplit(low).netloc
    return any(h and str(h).strip().lower() == host for h in (dead_hosts or ()))


def _discover_external_gh_proxies(discovery_pages, cf_domains, dead_hosts=()):
    """
    外部代理猎手：自动从 discovery_pages 与内置监控页中提取新的 GitHub 加速前缀
    （自动排除仓库自身的 CF 自定义域名、dead_proxy_hosts 与 raw/github 主域名）
    """
    excluded_hosts = {
        "github.com",
        "raw.githubusercontent.com",
        "gist.githubusercontent.com",
        "api.github.com",
    }
    for d in cf_domains or []:
        h = urllib.parse.urlsplit(str(d)).netloc.lower()
        if h:
            excluded_hosts.add(h)
    for dh in dead_hosts or ():
        h = urllib.parse.urlsplit(str(dh) if "://" in str(dh) else f"https://{dh}").netloc.lower()
        if h:
            excluded_hosts.add(h)

    scan_urls = list(dict.fromkeys(list(BUILTIN_PROXY_RADAR_URLS) + list(discovery_pages or [])[:8]))
    found_prefixes = []
    seen_hosts = set()
    wrapped_pat = re.compile(
        r"(https?://[A-Za-z0-9.-]+(?::\d+)?)/https?://(?:raw\.githubusercontent\.com|github\.com)",
        flags=re.IGNORECASE,
    )
    domain_pat = re.compile(
        r"https?://[A-Za-z0-9.-]*(?:ghproxy|gh-proxy|gitmirror|ghfast)[A-Za-z0-9.-]*/?",
        flags=re.IGNORECASE,
    )

    for page_u in scan_urls:
        try:
            txt = fetch_url_text(page_u, timeout=5)
            for m in wrapped_pat.findall(txt):
                host = urllib.parse.urlsplit(m).netloc.lower()
                if host and host not in excluded_hosts and host not in seen_hosts:
                    seen_hosts.add(host)
                    found_prefixes.append(m.rstrip("/") + "/")
            for m in domain_pat.findall(txt):
                norm = m.rstrip("/") + "/"
                host = urllib.parse.urlsplit(norm).netloc.lower()
                if host and host not in excluded_hosts and host not in seen_hosts:
                    seen_hosts.add(host)
                    found_prefixes.append(norm)
        except Exception:
            continue
    return found_prefixes[:12]


def build_and_save_proxy_pools(discovery_pages, settings, seeds, dead_hosts=()):
    px_cfg = settings.get("proxy", {})
    list_file = px_cfg.get("list_file", "active_proxies.json")
    lite_file = px_cfg.get("lite_file", "active_proxies_lite.json")
    target_count = int(px_cfg.get("target_count", 10))
    lite_count = int(px_cfg.get("lite_count", 3))
    max_fail_streak = int(px_cfg.get("max_fail_streak", 3))
    min_speed_kbs = float(px_cfg.get("min_speed_kbs", 50.0))

    repo_owner_path = settings.get("repo_owner_path", "hmz0123/hmz-auto-iptv-sources").strip("/")
    repo_branch = settings.get("repo_branch", "main").strip("/")
    raw_base = f"{GH_RAW}/{repo_owner_path}/{repo_branch}/"

    memory = {}
    if os.path.exists(list_file):
        try:
            with open(list_file, "r", encoding="utf-8") as f:
                for it in json.load(f):
                    if isinstance(it, dict) and it.get("prefix") and not _is_dead_proxy_prefix(it["prefix"], dead_hosts):
                        memory[it["prefix"]] = it
        except Exception:
            pass

    ordered, seen = [], set()
    for rec in sorted(
        [r for r in memory.values() if int(r.get("fail_streak", 0)) < max_fail_streak],
        key=lambda x: x.get("discovery_order", 999),
    ):
        if rec["prefix"] not in seen and not _is_dead_proxy_prefix(rec["prefix"], dead_hosts):
            seen.add(rec["prefix"])
            ordered.append(rec["prefix"])
    for p in seeds.get("proxy_candidates", []):
        p_norm = p if p.endswith("/") else p + "/"
        if p_norm not in seen and not _is_dead_proxy_prefix(p_norm, dead_hosts):
            seen.add(p_norm)
            ordered.append(p_norm)

    hunted = _discover_external_gh_proxies(
        discovery_pages, settings.get("urls", {}).get("cf_domains", []), dead_hosts=dead_hosts
    )
    hunted_added = 0
    for hp in hunted:
        if hp not in seen and not _is_dead_proxy_prefix(hp, dead_hosts):
            seen.add(hp)
            ordered.append(hp)
            hunted_added += 1
    if hunted_added:
        print(f"外部代理猎手：新捕获 {hunted_added} 个候选加速节点纳入测速")

    degraded_prev = [
        r for r in memory.values() if int(r.get("fail_streak", 0)) >= max_fail_streak and not _is_dead_proxy_prefix(r.get("prefix", ""), dead_hosts)
    ]
    for rec in random.sample(degraded_prev, min(3, len(degraded_prev))):
        if rec["prefix"] not in seen:
            seen.add(rec["prefix"])
            ordered.append(rec["prefix"])

    now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def check_proxy(idx_p):
        marker, prefix = idx_p
        json_u, jar_u = (
            f"{prefix}{raw_base}tvbox.json",
            f"{prefix}{raw_base}custom_spider.jar",
        )
        t0, ok, lat, spd = time.time(), False, 999, 0.0
        try:
            with urllib.request.urlopen(
                urllib.request.Request(json_u, headers=HEADERS), timeout=5
            ) as r1:
                if r1.status == 200 and b'"spider"' in r1.read(512):
                    t_jar = time.time()
                    with urllib.request.urlopen(
                        urllib.request.Request(jar_u, headers=HEADERS), timeout=6
                    ) as r2:
                        jb = r2.read(524288)
                        if r2.status == 200 and len(jb) > 50 * 1024 and jb[:4] == b"PK\x03\x04":
                            spd = round(
                                (len(jb) / 1024.0) / max(0.01, time.time() - t_jar), 1
                            )
                            lat = round((time.time() - t0) * 1000)
                            ok = spd >= min_speed_kbs
        except Exception:
            pass
        prev = memory.get(prefix, {})
        streak = 0 if ok else int(prev.get("fail_streak", 0)) + 1
        return {
            "discovery_order": marker,
            "prefix": prefix,
            "url": json_u,
            "lite_url": f"{prefix}{raw_base}tvbox_lite.json",
            "pan_url": f"{prefix}{raw_base}tvbox_pan.json",
            "latency": lat if ok else 999,
            "speed_kbs": spd,
            "ok": ok,
            "fail_streak": streak,
            "degraded": streak >= max_fail_streak,
            "last_ok_at": now_str if ok else prev.get("last_ok_at", ""),
        }

    ranked = sorted(
        run_parallel(check_proxy, list(enumerate(ordered, 1)), workers=16),
        key=lambda x: (
            x["degraded"],
            not x["ok"],
            x["discovery_order"],
            x["latency"],
            -x["speed_kbs"],
        ),
    )
    selected = (
        [x for x in ranked if x["ok"] and not x["degraded"]]
        + [x for x in ranked if x["ok"] and x["degraded"]]
    )[:target_count]
    if len(selected) < target_count:
        used = {x["prefix"] for x in selected}
        selected.extend(
            [x for x in ranked if x["prefix"] not in used and not x["degraded"]][
                : target_count - len(selected)
            ]
        )

    full_records = []
    for idx, it in enumerate(selected, 1):
        st = "✅" if it["ok"] else ("⚠️降权" if it["degraded"] else "⏳")
        spd_txt = f"·{int(it['speed_kbs'])}KB/s" if it["speed_kbs"] > 0 else ""
        full_records.append(
            {
                "order": idx,
                "discovery_order": it["discovery_order"],
                "name": f"🚀代理全量{idx} {st}{it['latency']}ms{spd_txt}",
                "lite_name": f"⚡代理精简{idx} {st}{it['latency']}ms{spd_txt}",
                "pan_name": f"💎代理网盘{idx} {st}{it['latency']}ms{spd_txt}",
                "prefix": it["prefix"],
                "url": it["url"],
                "lite_url": it["lite_url"],
                "pan_url": it["pan_url"],
                "latency": it["latency"],
                "speed_kbs": it["speed_kbs"],
                "verified": it["ok"],
                "fail_streak": it["fail_streak"],
                "degraded": it["degraded"],
                "last_ok_at": it["last_ok_at"],
            }
        )

    lite_records = (
        sorted(
            [r for r in full_records if r["verified"]],
            key=lambda x: (int(x.get("fail_streak", 0)) * 120 + x["latency"], -x["speed_kbs"]),
        )
        or full_records
    )[:lite_count]
    with open(list_file, "w", encoding="utf-8") as f:
        json.dump(full_records, f, ensure_ascii=False, indent=2)
    with open(lite_file, "w", encoding="utf-8") as f:
        json.dump(lite_records, f, ensure_ascii=False, indent=2)
    return full_records, lite_records


def _fmt_delta(cur, prev):
    if prev is None:
        return "（首次统计）"
    diff = cur - prev
    if diff > 0:
        return f"（较上期 **+{diff}** 📈）"
    if diff < 0:
        return f"（较上期 **{diff}** 📉）"
    return "（较上期 **持平** ➖）"


def write_actions_summary(
    tag,
    full_cnt,
    lite_cnt,
    pan_only_cnt,
    pan_cnt,
    live_cnt,
    dc_cnt,
    jar_stats,
    source_stats,
    full_proxies,
    lite_proxies,
    registry,
    alerts,
    prev_counts=None,
):
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    prev = prev_counts or {}
    try:
        top_proxies = "\n".join(
            f"- `{p['prefix']}` — **{p['latency']}ms** · `{int(p.get('speed_kbs', 0))}KB/s`"
            for p in lite_proxies
        )
        contrib = "\n".join(
            f"- {'📦' if s.get('cached') else ('✅' if s['ok'] else '❌')} "
            f"**{s['name']}**：提取 **{s['total']}** 站（网盘 **{s['pan']}**）"
            for s in source_stats
            if s.get("priority")
        )
        alert_md = (
            "\n".join(f"- ⚠️ {a}" for a in alerts)
            if alerts
            else "- ✅ 全线健康运行，无异常告警。"
        )
        md = (
            f"### 📺 TVBox 自动更新健康简报 ({tag})\n\n"
            f"| 指标 | 数值 |\n| :--- | :--- |\n"
            f"| 全量版 (`tvbox.json`) | **{full_cnt}** 站{_fmt_delta(full_cnt, prev.get('full'))}（含网盘 **{pan_cnt}** 站{_fmt_delta(pan_cnt, prev.get('pan_in_full'))}） |\n"
            f"| 精简版 (`tvbox_lite.json`) | **{lite_cnt}** 站{_fmt_delta(lite_cnt, prev.get('lite'))}（豆瓣置顶 + 分片测速 + 过滤成人/敏感站点） |\n"
            f"| 网盘专版 (`tvbox_pan.json`) | **{pan_only_cnt}** 站{_fmt_delta(pan_only_cnt, prev.get('pan_only'))}（豆瓣置顶） |\n"
            f"| 精选直播 (`tvbox_live.txt`) | **{live_cnt}** 条{_fmt_delta(live_cnt, prev.get('live'))} |\n"
            f"| JAR 本地镜像 | **{jar_stats[2]}** 个（总存活 {jar_stats[0]}，剔除死链 {jar_stats[1]}） |\n"
            f"| 多仓入口 (`dc.json`) | **{dc_cnt}** 条 |\n"
            f"| 源仓规模 (`source_registry.json`) | 直播 **{len(registry['live_sources'])}**{_fmt_delta(len(registry['live_sources']), prev.get('reg_live'))} / "
            f"点播 **{len(registry['vod_sources'])}**{_fmt_delta(len(registry['vod_sources']), prev.get('reg_vod'))} / "
            f"CMS **{len(registry['cms_apis'])}**{_fmt_delta(len(registry['cms_apis']), prev.get('reg_cms'))} |\n\n"
            f"#### 📦 主力单仓贡献\n{contrib}\n\n"
            f"#### ⚡ 精简代理节点\n{top_proxies}\n\n"
            f"#### 🚨 状态自检\n{alert_md}\n"
        )
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(md)
    except Exception as e:
        print(f"写入 Summary 失败: {e}")
