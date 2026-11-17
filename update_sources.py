import json
import re
import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ssl._create_default_https_context = ssl._create_unverified_context

GH_RAW = "https://githubusercontent.com"
GH_GIST = "https://githubusercontent.com"
GH_IO = "https://github.io"
API_PATH = "/api.php/provide/vod/"

LIVE_SOURCE_URLS = [
    GH_RAW + "/Guovin/iptv-api/gd/output/result.m3u",
    GH_RAW + "/vbskycn/iptv/master/tv/iptv4.m3u",
    GH_RAW + "/Kimentanm/aptv/master/m3u/iptv.m3u",
    GH_IO + "/iptv/languages/zho.m3u",
]

TVBOX_VOD_URLS = [
    GH_RAW + "/FongMi/CatVodSpider/main/json/config.json",
    GH_RAW + "/yoursmile66/TVBox/main/XC.json",
    GH_RAW + "/25175/tvyuan/main/tvbox.json",
    GH_RAW + "/jinenge/tvbox/main/0.json",
]

LIBRE_VOD_URLS = [
    GH_RAW + "/wokaotianshi123/jgcj/main/libretv.json",
    GH_GIST + "/xuyishan8058/fcd88d1a9e84c024cd5bc91531803ed3/raw/libretv-sources.json",
]

# 修复了原有的双冒号拼写错误
CMS_API_LIST = [
    {"key": "ffzy", "name": "非凡影视", "api": "http://ffzyapi.com" + API_PATH},
    {"key": "lzzy", "name": "量子资源", "api": "https://lziapi.com" + API_PATH},
    {"key": "ikun", "name": "爱坤资源", "api": "https://ikunzyapi.com" + API_PATH},
    {"key": "bfzy", "name": "暴风资源", "api": "https://bfzyapi.com" + API_PATH},
    {"key": "tyys", "name": "天涯资源", "api": "https://tyyszy.com" + API_PATH},
    {"key": "ryzy", "name": "如意资源", "api": "https://rycjapi.com" + API_PATH},
    {"key": "hwba", "name": "华为吧资源", "api": "https://hwba.live" + API_PATH},
    {"key": "wolong", "name": "卧龙资源", "api": "https://wolongzyw.com" + API_PATH},
    {"key": "jisu", "name": "极速资源", "api": "https://jszyapi.com" + API_PATH},
    {"key": "modu", "name": "魔都资源", "api": "https://mdzyapi.com" + API_PATH},
    {"key": "subo", "name": "速博资源", "api": "https://subocaiji.com" + API_PATH},
    {"key": "xinlang", "name": "新浪资源", "api": "https://xinlangapi.com" + API_PATH},
    {"key": "guangsu", "name": "光速资源", "api": "https://guangsuapi.com" + API_PATH},
    {"key": "hongniu", "name": "红牛资源", "api": "https://hongniuzy2.com" + API_PATH},
    {"key": "jinying", "name": "金鹰资源", "api": "https://jinyingzy.com" + API_PATH},
    {"key": "sdzy", "name": "闪电资源", "api": "https://sdzyapi.com" + API_PATH},
    {"key": "yhzy", "name": "樱花资源", "api": "https://apiyhzy.com" + API_PATH},
    {"key": "wujin", "name": "无尽资源", "api": "https://wujinapi.me" + API_PATH},
    {"key": "dbzy", "name": "豆瓣资源", "api": "https://dbzy.tv" + API_PATH},
    {"key": "dytt", "name": "电影天堂", "api": "http://dyttzyapi.com" + API_PATH},
]

# 备用爬虫 Jar（当外部源全部抓取失败时，保障豆瓣首页推荐与榜单正常工作）
DEFAULT_SPIDER = GH_RAW + "/FongMi/CatVodSpider/main/jar/custom_spider.jar"

TIMEOUT = 6
MAX_WORKERS = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def fetch_url_text(url, timeout=15):
    req = urllib.request.Request(url, headers=HEADERS)
    resp = urllib.request.urlopen(req, timeout=timeout)
    data = resp.read().decode("utf-8", errors="ignore")
    resp.close()
    return data


def clean_json_text(raw_text):
    text = re.sub(r"^\s*//.*$", "", raw_text, flags=re.MULTILINE)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return text


def is_chinese_channel(name):
    if re.search(r"[\u4e00-\u9fff]", name):
        return True
    upper = name.upper()
    return "CCTV" in upper or "CGTN" in upper


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
            if is_chinese_channel(current_info["name"]):
                current_info["url"] = line
                parsed.append(current_info)
            current_info = {}
        elif "," in line and "http" in line and not line.startswith("#"):
            ch_name, ch_url = line.split(",", 1)
            ch_name, ch_url = ch_name.strip(), ch_url.strip()
            if ch_url.startswith("http") and is_chinese_channel(ch_name):
                parsed.append({"name": ch_name, "group": "精选中文频道", "url": ch_url})
    return parsed


def test_live_speed(channel):
    url = channel["url"]
    if "://[" in url:
        return None
    start_time = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "okhttp/3.15"})
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)
        status, chunk = resp.status, resp.read(512)
        resp.close()
        if status in (200, 206, 301, 302) and len(chunk) > 0:
            channel["latency"] = round((time.time() - start_time) * 1000)
            return channel
    except Exception:
        pass
    return None


def test_cms_api(site):
    start_time = time.time()
    try:
        sep = "&" if "?" in site["api"] else "?"
        text = fetch_url_text(f"{site['api']}{sep}ac=list&pg=1", timeout=TIMEOUT)
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
    tvbox_base, extra_sites, all_parses = {}, [], []
    for url in LIBRE_VOD_URLS:
        try:
            data = json.loads(clean_json_text(fetch_url_text(url)))
            items = data.get("sources") or [{"id": k, **v} for k, v in data.get("api_site", {}).items()]
            for s in items:
                api = s.get("api", "").strip()
                if api and api not in merged_cms:
                    merged_cms[api] = {"key": s.get("id") or f"s_{len(merged_cms)}", "name": s.get("name", "聚合源"), "api": api}
        except Exception:
            pass
    for url in TVBOX_VOD_URLS:
        try:
            data = json.loads(clean_json_text(fetch_url_text(url)))
            if not tvbox_base and data.get("spider"):
                tvbox_base = data
            for s in data.get("sites", []):
                api = str(s.get("api", "")).strip()
                if s.get("type") in (0, 1) and api.startswith("http") and api not in merged_cms:
                    merged_cms[api] = {"key": s.get("key") or f"t_{len(merged_cms)}", "name": s.get("name", "TVBox站"), "api": api}
                elif not api.startswith("http"):
                    extra_sites.append(s)
            all_parses.extend(data.get("parses", []))
        except Exception:
            pass
    return list(merged_cms.values()), tvbox_base, extra_sites, all_parses


def main():
    tag = time.strftime("%Y-%m-%d %H:%M:%S UTC")
    raw_ch = []
    for u in LIVE_SOURCE_URLS:
        try:
            raw_ch.extend(parse_live_content(fetch_url_text(u)))
        except Exception:
            pass
    uniq_ch = list({c["url"]: c for c in raw_ch}.values())
    val_ch = sorted(run_parallel(test_live_speed, uniq_ch, MAX_WORKERS), key=lambda x: x.get("latency", 999))
    if len(val_ch) < 10 and uniq_ch:
        val_ch = [c for c in uniq_ch if "://[" not in c["url"]][:200]

    with open("libre_tv.m3u", "w", encoding="utf-8") as f:
        f.write(f"#EXTM3U # Updated: {tag}\n" + "".join(f'#EXTINF:-1 group-title="{c["group"]}",{c["name"]}\n{c["url"]}\n' for c in val_ch))

    groups = {}
    for c in val_ch:
        groups.setdefault(c["group"], []).append(c)
    with open("tvbox_live.txt", "w", encoding="utf-8") as f:
        f.write(f"更新时间: {tag},#genre#\n\n" + "".join(f"{g},#genre#\n" + "".join(f"{c['name']},{c['url']}\n" for c in cl) + "\n" for g, cl in groups.items()))

    cms_pool, tv_base, ex_sites, ex_parses = collect_all_vod()
    val_cms = sorted(run_parallel(test_cms_api, cms_pool, 20), key=lambda x: x.get("latency", 888))
    if len(val_cms) < 5:
        for i in cms_pool[:30]:
            i.setdefault("latency", 888)
        val_cms = cms_pool[:30]

    # 1. 输出 LibreTV 点播源（仅支持标准 CMS 接口，配合 LibreTV 前端内置豆瓣榜单使用）
    with open("libre_vod.json", "w", encoding="utf-8") as f:
        json.dump({
            "version": 2,
            "updated_at": tag,
            "cache_time": 7200,
            "sources": [{"id": i["key"], "name": f"{i['name']} ({i.get('latency', 888)}ms)", "api": i["api"], "type": "cms"} for i in val_cms]
        }, f, ensure_ascii=False, indent=2)

    # 2. 构建 TVBox 点播站点，置顶添加【豆瓣评分与排行榜】专属页面
    douban_site = {
        "key": "douban_rank",
        "name": "🎬豆瓣热播·评分排行榜",
        "type": 3,
        "api": "csp_Douban",
        "searchable": 0,
        "quickSearch": 0,
        "filterable": 1
    }

    cms_sites = [{
        "key": i["key"],
        "name": f"⚡{i['name']}({i.get('latency', 888)}ms)",
        "type": 1,
        "api": i["api"],
        "searchable": 1,
        "quickSearch": 1,
        "filterable": 1
    } for i in val_cms]

    seen = {"douban_rank", "Douban", "douban"} | {s["key"] for s in cms_sites}
    dedup_sites = [s for s in ex_sites if s.get("key") and s["key"] not in seen and not seen.add(s["key"])]

    live_url = GH_RAW + "/hmz0123/hmz-auto-iptv-sources/main/tvbox_live.txt"
    wp_url = "https://eees.cc"

    final_tv = {
        "updated_at": tag,
        "spider": tv_base.get("spider") or DEFAULT_SPIDER,
        "wallpaper": tv_base.get("wallpaper", wp_url),
        "lives": [{"name": "自动测速精选直播", "type": 0, "url": live_url, "playerType": 1, "ua": "okhttp/3.15"}],
        "sites": [douban_site] + cms_sites + dedup_sites,
        "parses": ex_parses[:20] if ex_parses else tv_base.get("parses", []),
        "flags": tv_base.get("flags", []),
        "ijk": tv_base.get("ijk", []),
        "ads": tv_base.get("ads", [])
    }

    with open("tvbox.json", "w", encoding="utf-8") as f:
        json.dump(final_tv, f, ensure_ascii=False, indent=2)

    print(f"完成！TVBox {len(final_tv['sites'])} 站（含豆瓣榜单），LibreTV {len(val_cms)} 源，直播 {len(val_ch)} 台。")


if __name__ == "__main__":
    main()

