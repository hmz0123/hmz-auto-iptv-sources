import hashlib
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ssl._create_default_https_context = ssl._create_unverified_context

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
}
OKHTTP_HEADERS = {"User-Agent": "okhttp/4.12.0", "Accept": "application/json,text/plain,*/*"}

CACHE_ALIAS_MAP = {
    "feimao": "fm",
    "laoliubei": "llb",
    "moyu": "my",
    "wangerxiao": "wex",
    "xiaoma": "xm",
}

DEFAULT_DEAD_OR_JUNK_WORDS = (
    "购物", "演示", "测试站", "公告", "免责", "说明", "使用须知", "公众号",
    "防失联", "备用网址", "推送", "本地", "请勿相信", "验证码", "口令",
    "验证", "解锁", "扫码", "广告", "赌博", "博彩", "澳门", "威尼斯", "彩票", "棋牌",
    "接口失效", "已失效", "失效", "维护中", "暂停服务", "已停用", "下线",
)


def canonical_cache_id(sid: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_-]", "", str(sid or "").strip()).lower()
    return CACHE_ALIAS_MAP.get(clean, clean or "src")


def load_configs():
    """加载 config/settings.json、seeds.json 与 rules.json，并支持环境变量覆写"""
    with open("config/settings.json", "r", encoding="utf-8") as f:
        settings = json.load(f)
    with open("config/seeds.json", "r", encoding="utf-8") as f:
        seeds = json.load(f)
    with open("config/rules.json", "r", encoding="utf-8") as f:
        rules = json.load(f)

    settings.setdefault("thresholds", {})
    settings.setdefault("editions", {})
    settings.setdefault("pinned", {})
    settings.setdefault("registry", {})
    settings.setdefault("proxy", {})
    settings.setdefault("urls", {})

    if os.environ.get("LITE_MAX_CMS_SITES"):
        settings["editions"]["lite_max_cms_sites"] = int(os.environ["LITE_MAX_CMS_SITES"])
    if os.environ.get("MAX_WORKERS"):
        settings["thresholds"]["max_workers"] = int(os.environ["MAX_WORKERS"])
    return settings, seeds, rules


def append_cache_buster(url: str, version_tag: str) -> str:
    if not url or not version_tag:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}v={version_tag}"


def get_local_spider_md5():
    if not os.path.exists("custom_spider.jar"):
        return ""
    try:
        with open("custom_spider.jar", "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception:
        return ""


def sync_spider_jar(upstream_url: str):
    """同步最新 custom_spider.jar 并返回 32 位 MD5"""
    try:
        req = urllib.request.Request(upstream_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
            if len(data) > 100 * 1024 and data[:4] == b"PK\x03\x04":
                with open("custom_spider.jar", "wb") as f:
                    f.write(data)
                md5_hex = hashlib.md5(data).hexdigest()
                print(f"成功同步 custom_spider.jar ({len(data) // 1024} KB, md5={md5_hex[:8]})")
                return md5_hex
    except Exception as e:
        print(f"同步 custom_spider.jar 失败（将沿用仓库已有文件）: {e}")
    return get_local_spider_md5()


def idn_encode(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
        if not host:
            return url
        encoded_host = host.encode("idna").decode("ascii")
        netloc = f"{encoded_host}:{parsed.port}" if parsed.port else encoded_host
        return urllib.parse.urlunsplit(
            (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
        )
    except Exception:
        return url


def fetch_url_text(url, timeout=10, prefer_okhttp=False):
    target = idn_encode(url)
    header_chain = [OKHTTP_HEADERS, HEADERS] if prefer_okhttp else [HEADERS, OKHTTP_HEADERS]
    last_err = None
    for idx, hdr in enumerate(header_chain):
        try:
            req = urllib.request.Request(target, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
                if raw.strip():
                    return raw
        except Exception as e:
            last_err = e
            if idx == 0:
                time.sleep(0.15)
    raise last_err if last_err else RuntimeError("空响应")


def clean_json_text(raw_text):
    text = str(raw_text or "").lstrip("\ufeff").strip()
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("//")]
    text = "\n".join(lines)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return text.strip()


def parse_json_lenient(raw_text):
    cleaned = clean_json_text(raw_text)
    if not cleaned or cleaned[0] not in ("{", "["):
        raise ValueError("非 JSON 响应或空内容")
    try:
        return json.loads(cleaned, strict=False)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder(strict=False)
        obj, _ = decoder.raw_decode(cleaned)
        return obj


def resolve_relative_url(base_url, maybe_rel):
    val = str(maybe_rel or "").strip()
    if not val or val.startswith(("http://", "https://")):
        return val
    parts = val.split(";", 1)
    rel_path = parts[0]
    suffix = f";{parts[1]}" if len(parts) > 1 else ""
    if rel_path.startswith("./") or rel_path.startswith("../") or "/" in rel_path:
        return urllib.parse.urljoin(idn_encode(base_url), rel_path) + suffix
    return val


def run_parallel(func, items, workers=20):
    results = []
    if not items:
        return results

    def _guarded(item):
        try:
            return func(item)
        except Exception as e:
            print(f"并发子任务异常已跳过: {e}")
            return None

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        for res in executor.map(_guarded, items):
            if res is not None:
                results.append(res)
    return results


def is_invalid_site_url(val):
    s = str(val or "").strip().lower()
    return any(bad in s for bad in ("127.0.0.1", "localhost", "0.0.0.0", "example.com", "your-domain"))


def sanitize_discovered_url(raw_url: str, dead_proxy_hosts=()) -> str:
    s = str(raw_url or "").strip()
    if not s or any(ch in s for ch in (" ", "](", "[", "]")):
        return ""
    if re.search(r"https?://.+https?://", s, flags=re.IGNORECASE):
        return ""
    if re.search(
        r"[?&](?:url|vurl|src|target|link)=(?:https?://|https?%3a)",
        s,
        flags=re.IGNORECASE,
    ):
        return ""

    u = s.rstrip(").,;\"'`<>")
    if not u.startswith(("http://", "https://")):
        return ""

    m = re.search(
        r"(?:raw\.githubusercontent\.com|gist\.githubusercontent\.com)/(.+)$",
        u,
        flags=re.IGNORECASE,
    )
    if m:
        host_part = (
            "raw.githubusercontent.com"
            if "raw.githubusercontent.com" in u.lower()
            else "gist.githubusercontent.com"
        )
        clean_path = m.group(1).replace("refs/heads/", "")
        u = f"https://{host_part}/{clean_path}"

    if any(h in u.lower() for h in (dead_proxy_hosts or ())):
        return ""
    return u


def is_pan_site(site, pan_keywords, invalid_keywords=DEFAULT_DEAD_OR_JUNK_WORDS):
    if not isinstance(site, dict):
        return False
    name = str(site.get("name", "")).strip().lower()
    if invalid_keywords and any(str(w).strip().lower() in name for w in invalid_keywords if str(w).strip()):
        return False
    text = (
        str(site.get("name", "")) + str(site.get("api", "")) + str(site.get("key", ""))
    ).lower()
    return any(k in text for k in pan_keywords)


def is_adult_or_sensitive_site(site, adult_keywords):
    """仅用于精简版过滤成人/敏感站点，全量版不调用"""
    if not isinstance(site, dict):
        return False
    name = str(site.get("name", "")).strip()
    key = str(site.get("key", "")).strip()
    api = str(site.get("api", "")).strip()
    cats = site.get("_categories") or []
    cat_str = " ".join(str(c) for c in cats) if isinstance(cats, list) else str(cats)
    combined = f"{name} {key} {api} {cat_str}".lower()
    return any(k in combined for k in adult_keywords)


def is_junk_or_ad_site(site, junk_keywords):
    """过滤广告/赌博/公告/失效/占位垃圾站（不限制成人内容）"""
    if not isinstance(site, dict):
        return True
    if str(site.get("key", "")).strip() in ("douban_rank", "Douban", "douban"):
        return False
    if site.get("searchable") == 0 and site.get("quickSearch") == 0:
        return True
    name = str(site.get("name", "")).strip()
    key = str(site.get("key", "")).strip()
    api = str(site.get("api", "")).strip()
    ext = (
        str(site.get("ext", "")).strip()
        if isinstance(site.get("ext"), str)
        else json.dumps(site.get("ext", ""), ensure_ascii=False)
    )
    combined = f"{name} {key} {api}".lower()
    if any(k in combined for k in junk_keywords):
        return True
    if "alist" in combined and any(p in ext.lower() for p in ("alist.example", "127.0.0.1", "你的", "填写")):
        return True
    return False


def normalize_site_display_name(raw_name):
    name = str(raw_name or "").strip()
    if not name:
        return "精选片源"
    lat_match = re.search(r"(\(\d+ms\))$", name)
    lat_suffix = lat_match.group(1) if lat_match else ""
    core = name[: -len(lat_suffix)].strip() if lat_suffix else name
    core = re.sub(r"[┃|｜]\s*(?:秒播|无广|高清|超清|专线|备用|测试|推荐|自用)\d*", "", core)
    core = re.sub(r"[\[\(（【](?:秒播|无广|专线|备用|采集|切片|官采)[\]\)）】]", "", core)
    core = re.sub(r"\s+", " ", core).strip()
    return f"{core}{lat_suffix}" if core else name


def _load_dead_assets_cache(cache_dir, ttl_seconds=604800):
    path = os.path.join(cache_dir, "dead_assets.json")
    now_ts = time.time()
    valid_map = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
                if isinstance(raw, dict):
                    for u, ts in raw.items():
                        if isinstance(ts, (int, float)) and (now_ts - ts) < ttl_seconds:
                            valid_map[u] = float(ts)
        except Exception:
            pass
    return path, valid_map


def _save_dead_assets_cache(path, dead_map):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        trimmed = dict(sorted(dead_map.items(), key=lambda x: x[1], reverse=True)[:800])
        with open(path, "w", encoding="utf-8") as f:
            json.dump(trimmed, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def verify_and_mirror_assets(sites, settings, pan_keywords):
    """核心 JAR 本地化镜像（仅保留当前存活站点引用的 JAR）与外部依赖存活预检"""
    cache_dir = settings.get("cache_dir", "cache")
    jars_dir = settings.get("jars_dir", "jars")
    max_bytes = int(settings.get("max_mirror_jar_bytes", 2621440))
    max_kept_jars = int(settings.get("max_mirrored_jars", 15))
    os.makedirs(jars_dir, exist_ok=True)

    dead_cache_path, dead_assets_map = _load_dead_assets_cache(cache_dir)
    cached_dead_urls = set(dead_assets_map.keys())

    existing_local_jars = {
        fn: os.path.join(jars_dir, fn)
        for fn in os.listdir(jars_dir)
        if fn.endswith(".jar") and os.path.isfile(os.path.join(jars_dir, fn))
    }

    priority_jar_urls, all_jar_urls, ext_rule_urls = set(), set(), set()
    for s in sites:
        raw_jar = str(s.get("jar", "")).strip()
        if raw_jar and not raw_jar.startswith("./"):
            clean_jar = raw_jar.split(";", 1)[0].strip()
            if clean_jar.startswith(("http://", "https://")) and clean_jar not in cached_dead_urls:
                all_jar_urls.add(clean_jar)
                if is_pan_site(s, pan_keywords) or s.get("_is_priority") or s.get("quickSearch") == 1:
                    priority_jar_urls.add(clean_jar)
        ext_val = s.get("ext")
        if isinstance(ext_val, str):
            clean_ext = ext_val.split("?", 1)[0].strip()
            full_ext = ext_val.strip()
            if (
                clean_ext.startswith(("http://", "https://"))
                and clean_ext.lower().endswith((".js", ".json"))
                and full_ext not in cached_dead_urls
            ):
                ext_rule_urls.add(full_ext)

    mirrored_map = {}

    def download_mirror_jar(jar_url):
        try:
            req = urllib.request.Request(idn_encode(jar_url), headers=OKHTTP_HEADERS)
            with urllib.request.urlopen(req, timeout=7) as resp:
                if resp.status not in (200, 206):
                    return None
                content_len = resp.headers.get("Content-Length")
                if content_len and content_len.isdigit() and int(content_len) > max_bytes:
                    return None
                data = resp.read(max_bytes + 1)
                if len(data) > max_bytes or len(data) < 20 * 1024 or data[:4] != b"PK\x03\x04":
                    return None
                md5_hex = hashlib.md5(data).hexdigest()
                short_id = md5_hex[:12]
                rel_file = f"{jars_dir}/{short_id}.jar"
                with open(rel_file, "wb") as f:
                    f.write(data)
                existing_local_jars[f"{short_id}.jar"] = rel_file
                return (jar_url, f"./{rel_file};md5;{md5_hex}")
        except Exception:
            return None

    if priority_jar_urls:
        for item in run_parallel(download_mirror_jar, list(priority_jar_urls)[:max_kept_jars], workers=10):
            if item:
                mirrored_map[item[0]] = item[1]

    remaining_jars = [u for u in all_jar_urls if u not in mirrored_map]

    def check_asset(item):
        asset_type, u = item
        target = idn_encode(u)
        need_get = False
        try:
            req = urllib.request.Request(target, headers=OKHTTP_HEADERS, method="HEAD")
            with urllib.request.urlopen(req, timeout=3.5) as resp:
                if resp.status in (200, 206, 301, 302):
                    return (asset_type, u, True)
        except urllib.error.HTTPError as e:
            if e.code in (403, 405):
                need_get = True
            else:
                return (asset_type, u, False)
        except Exception:
            return (asset_type, u, False)

        if need_get:
            try:
                req = urllib.request.Request(target, headers=OKHTTP_HEADERS, method="GET")
                with urllib.request.urlopen(req, timeout=3.5) as resp:
                    if resp.status in (200, 206, 301, 302):
                        chunk = resp.read(32)
                        if chunk and not chunk.lstrip().lower().startswith((b"<!doctype", b"<html")):
                            return (asset_type, u, True)
            except Exception:
                pass
        return (asset_type, u, False)

    tasks = [("jar", u) for u in remaining_jars[:30]] + [("rule", u) for u in list(ext_rule_urls)[:40]]
    alive_extra_jars, dead_jars, dead_rules = set(), set(), set()
    if tasks:
        results = run_parallel(check_asset, tasks, workers=20)
        alive_extra_jars = {u for t, u, ok in results if t == "jar" and ok}
        dead_jars = {u for t, u, ok in results if t == "jar" and not ok}
        dead_rules = {u for t, u, ok in results if t == "rule" and not ok}

    now_ts = time.time()
    for u in dead_jars | dead_rules:
        dead_assets_map[u] = now_ts
    _save_dead_assets_cache(dead_cache_path, dead_assets_map)

    all_dead_jars = dead_jars | cached_dead_urls
    all_dead_rules = dead_rules | cached_dead_urls

    filtered, dropped_sites, rewritten_cnt = [], 0, 0
    active_local_jar_names = set()
    for s in sites:
        item = dict(s)
        raw_jar = str(item.get("jar", "")).strip()
        if raw_jar:
            if raw_jar.startswith("./"):
                clean_ref = raw_jar.split(";", 1)[0].strip()
                prefix = f"./{jars_dir}/"
                if clean_ref.startswith(prefix):
                    active_local_jar_names.add(clean_ref[len(prefix):])
            else:
                clean_jar = raw_jar.split(";", 1)[0].strip()
                if clean_jar in mirrored_map:
                    item["jar"] = mirrored_map[clean_jar]
                    rewritten_cnt += 1
                    mirrored_fn = mirrored_map[clean_jar].split(";", 1)[0].replace(f"./{jars_dir}/", "")
                    active_local_jar_names.add(mirrored_fn)
                elif clean_jar in all_dead_jars:
                    dropped_sites += 1
                    continue
        ext_val = item.get("ext")
        if isinstance(ext_val, str) and ext_val.strip() in all_dead_rules:
            dropped_sites += 1
            continue
        filtered.append(item)

    removed_unused = 0
    for fn, full_path in list(existing_local_jars.items()):
        if fn not in active_local_jar_names:
            try:
                os.remove(full_path)
                existing_local_jars.pop(fn, None)
                removed_unused += 1
            except Exception:
                pass

    print(
        f"外部依赖镜像与预检完成：本地镜像 JAR {len(mirrored_map)} 个（重写 {rewritten_cnt} 处，清理未引用 {removed_unused} 个），"
        f"外部存活 JAR {len(alive_extra_jars)} 个，剔除死链 {dropped_sites} 站（死链缓存 {len(dead_assets_map)} 条）"
    )
    return filtered, (len(mirrored_map) + len(alive_extra_jars), dropped_sites, len(existing_local_jars))
