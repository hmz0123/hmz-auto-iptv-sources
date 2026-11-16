import json
import re
import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ssl._create_default_https_context = ssl._create_unverified_context

GH_RAW = "https://" + "raw.githubusercontent.com"
GH_GIST = "https://" + "gist.githubusercontent.com"
GH_IO = "https://" + "iptv-org.github.io"

LIVE_SOURCE_URLS = [
    GH_IO + "/iptv/languages/zho.m3u",
    GH_RAW + "/Guovin/iptv-api/gd/output/result.m3u",
    GH_RAW + "/vbskycn/iptv/master/tv/iptv4.m3u",
    GH_RAW + "/Kimentanm/aptv/master/m3u/iptv.m3u",
]

TVBOX_VOD_URLS = [
    GH_RAW + "/25175/tvyuan/main/tvbox.json",
    GH_RAW + "/jinenge/tvbox/main/0.json",
    GH_RAW + "/yoursmile66/TVBox/main/XC.json",
    GH_RAW + "/FongMi/CatVodSpider/main/json/config.json",
    GH_RAW + "/gaotianliuyun/gao/master/0821.json",
]

LIBRE_VOD_URLS = [
    GH_RAW + "/wokaotianshi123/jgcj/main/libretv.json",
    GH_GIST + "/xuyishan8058/fcd88d1a9e84c024cd5bc91531803ed3/raw/libretv-sources.json",
]

CMS_API_LIST = [
    {"key": "ffzy", "name": "非凡影视", "api": "http://" + "://ffzyapi.com"},
    {"key": "lzzy", "name": "量子资源", "api": "https://" + "://lziapi.com"},
    {"key": "ikun", "name": "爱坤资源", "api": "https://" + "://ikunzyapi.com"},
    {"key": "bfzy", "name": "暴风资源", "api": "https://" + "://bfzyapi.com"},
    {"key": "tyys", "name": "天涯资源", "api": "https://" + "://tyyszy.com"},
    {"key": "ryzy", "name": "如意资源", "api": "https://" + "://rycjapi.com"},
    {"key": "hwba", "name": "华为吧资源", "api": "https://" + "cj.hwba.live/api.php/provide/vod/"},
    {"key": "wolong", "name": "卧龙资源", "api": "https://" + "://wolongzyw.com"},
    {"key": "jisu", "name": "极速资源", "api": "https://" + "://jszyapi.com"},
    {"key": "modu", "name": "魔都资源", "api": "https://" + "://mdzyapi.com"},
    {"key": "subo", "name": "速博资源", "api": "https://" + "://subocaiji.com"},
    {"key": "xinlang", "name": "新浪资源", "api": "https://" + "://xinlangapi.com"},
    {"key": "guangsu", "name": "光速资源", "api": "https://" + "://guangsuapi.com"},
    {"key": "hongniu", "name": "红牛资源", "api": "https://" + "://hongniuzy2.com"},
    {"key": "jinying", "name": "金鹰资源", "api": "https://" + "://jinyingzy.com"},
    {"key": "sdzy", "name": "闪电资源", "api": "https://" + "://sdzyapi.com"},
    {"key": "yhzy", "name": "樱花资源", "api": "https://" + "://apiyhzy.com"},
    {"key": "wujin", "name": "无尽资源", "api": "https://" + "api.wujinapi.me/api.php/provide/vod/"},
    {"key": "dbzy", "name": "豆瓣资源", "api": "https://" + "dbzy.tv/api.php/provide/vod/"},
    {"key": "dytt", "name": "电影天堂", "api": "http://" + "://dyttzyapi.com"},
]

TIMEOUT = 6
MAX_WORKERS = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def fetch_url_text(url, timeout=15):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def clean_json_text(raw_text):
    text = re.sub(r"^\s*//.*$", "", raw_text, flags=re.MULTILINE)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return text


def parse_live_content(content):
    lines = content.splitlines()
    parsed = []
    current_info = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            name_match = re.search(r",([^,]+)$", line)
            name = name_match.group(1).strip() if name_match else "未知频道"
            group_match = re.search(r'group-title="([^"]+)"', line)
            group = group_match.group(1).strip() if group_match else "综合频道"
            current_info = {"name": name, "group": group}
        elif line.startswith("http") and current_info:
            current_info["url"] = line
            parsed.append(current_info)
            current_info = {}
        elif "," in line and "http" in line and not line.startswith("#"):
            ch_name, ch_url = line.split(",", 1)
            ch_name = ch_name.strip()
            ch_url = ch_url.strip()
            if ch_url.startswith("http"):
                parsed.append({"name": ch_name, "group": "精选频道", "url": ch_url})
    return parsed


def test_live_speed(channel):
    url = channel["url"]
    if "://[" in url:
        return None
    start_time = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "okhttp/3.15"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            if resp.status in (200, 206, 301, 302):
                if len(resp.read(512)) > 0:
                    channel["latency"] = round((time.time() - start_time) * 1000)
                    return channel
    except Exception:
        pass
    return None


def test_cms_api(site):
    start_time = time.time()
    try:
        sep = "&" if "?" in site["api"] else "?"
        test_url = f"{site['api']}{sep}ac=list&pg=1"
        text = fetch_url_text(test_url, timeout=TIMEOUT)
        if '"list"' in text or '"class"' in text or "<rss" in text:
            site["latency"] = round((time.time() - start_time) * 1000)
            return site
    except Exception:
        pass
    return None


def run_parallel(func, items, workers=20):
    results = []
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        for res in executor.map(func, items):
            if res:
                results.append(res)
    finally:
        executor.shutdown(wait=True)
    return results


def collect_all_vod():
    merged_cms = {item["api"]: dict(item) for item in CMS_API_LIST}
    tvbox_base = {}
    all_tvbox_sites = []
    all_parses = []

    for url in LIBRE_VOD_URLS:
        try:
            data = json.loads(clean_json_text(fetch_url_text(url)))
            if isinstance(data.get("sources"), list):
                for s in data["sources"]:
                    api = s.get("api", "").strip()
                    if api and api not in merged_cms:
                        merged_cms[api] = {
                            "key": s.get("id") or f"src_{len(merged_cms)}",
                            "name": s.get("name", "聚合资源"),
                            "api": api,
                            "isAdult": s.get("isAdult", False),
                        }
            elif isinstance(data.get("api_site"), dict):
                for k, v in data["api_site"].items():
                    api = v.get("api", "").strip()
                    if api and api not in merged_cms:
                        merged_cms[api] = {
                            "key": k,
                            "name": v.get("name", k),
                            "api": api,
                        }
            print(f"成功解析 LibreTV 源: {url}")
        except Exception as e:
            print(f"LibreTV 源跳过 {url}: {e}")

    for url in TVBOX_VOD_URLS:
        try:
            data = json.loads(clean_json_text(fetch_url_text(url)))
            sites = data.get("sites", [])
            if sites:
                print(f"成功解析 TVBox 源: {url} ({len(sites)} 个站点)")
                if not tvbox_base and data.get("spider"):
                    tvbox_base = data
                for s in sites:
                    if s.get("type") in (0, 1) and str(s.get("api", "")).startswith("http"):
                        api = s["api"].strip()
                        if api not in merged_cms:
                            merged_cms[api] = {
                                "key": s.get("key") or f"tv_{len(merged_cms)}",
                                "name": s.get("name", "TVBox站点"),
                                "api": api,
                            }
                    else:
                        all_tvbox_sites.append(s)
                all_parses.extend(data.get("parses", []))
        except Exception as e:
            print(f"TVBox 源跳过 {url}: {e}")

    return list(merged_cms.values()), tvbox_base, all_tvbox_sites, all_parses


def main():
    update_tag = time.strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"1. 开始处理直播源 ({update_tag})...")
    raw_channels = []
    for url in LIVE_SOURCE_URLS:
        try:
            raw_channels.extend(parse_live_content(fetch_url_text(url)))
        except Exception as e:
            print(f"直播源失败 {url}: {e}")

    unique_channels = list({ch["url"]: ch for ch in raw_channels}.values())
    valid_channels = run_parallel(test_live_speed, unique_channels, MAX_WORKERS)
    valid_channels.sort(key=lambda x: x.get("latency", 999))

    if len(valid_channels) < 10 and unique_channels:
        valid_channels = [ch for ch in unique_channels if "://[" not in ch["url"]][:200]
        for ch in valid_channels:
            ch.setdefault("latency", 999)

    with open("libre_tv.m3u", "w", encoding="utf-8") as f:
        f.write(f"#EXTM3U # Updated: {update_tag}\n")
        for ch in valid_channels:
            f.write(f'#EXTINF:-1 group-title="{ch["group"]}",{ch["name"]}\n{ch["url"]}\n')

    groups = {}
    for ch in valid_channels:
        groups.setdefault(ch["group"], []).append(ch)
    with open("tvbox_live.txt", "w", encoding="utf-8") as f:
        f.write(f"更新时间: {update_tag},#genre#\n\n")
        for group_name, ch_list in groups.items():
            f.write(f"{group_name},#genre#\n")
            for ch in ch_list:
                f.write(f"{ch['name']},{ch['url']}\n")
            f.write("\n")

    print("2. 开始处理点播源...")
    cms_pool, tvbox_base, extra_tvbox_sites, extra_parses = collect_all_vod()
    print(f"共收集到 {len(cms_pool)} 个 CMS 点播接口，开始并发测速...")

    valid_cms = run_parallel(test_cms_api, cms_pool, 20)
