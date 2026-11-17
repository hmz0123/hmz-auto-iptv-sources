import json
import re
import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ssl._create_default_https_context = ssl._create_unverified_context

# 1. 修正正确的 GitHub / Gist / Pages 域名
GH_RAW = "https://raw.githubusercontent.com"
GH_GIST = "https://gist.githubusercontent.com"
GH_IO = "https://iptv-org.github.io"
API_PATH = "/api.php/provide/vod/"

# 2. 高可用 IPv4 + IPv6 双栈直播源
LIVE_SOURCE_URLS = [
    GH_RAW + "/fanmingming/live/main/tv/m3u/ipv6.m3u",
    GH_RAW + "/vbskycn/iptv/master/tv/iptv4.m3u",
    GH_RAW + "/vbskycn/iptv/master/tv/iptv6.m3u",
    GH_RAW + "/suxuang/myIPTV/main/ipv4.m3u",
    GH_RAW + "/suxuang/myIPTV/main/ipv6.m3u",
    GH_RAW + "/YueChan/Live/main/APTV.m3u",
    GH_RAW + "/Guovin/iptv-api/gd/output/result.m3u",
    GH_RAW + "/Kimentanm/aptv/master/m3u/iptv.m3u",
    GH_RAW + "/YanG-1989/m3u/main/Gather.m3u",
    GH_IO + "/iptv/languages/zho.m3u",
]

# 3. 最新维护的 TVBox 聚合配置源
TVBOX_VOD_URLS = [
    GH_RAW + "/FongMi/CatVodSpider/main/json/config.json",
    GH_RAW + "/yoursmile66/TVBox/main/XC.json",
    GH_RAW + "/25175/tvyuan/main/tvbox.json",
    GH_RAW + "/jinenge/tvbox/main/0.json",
    GH_RAW + "/gaotianliuyun/gao/master/js.json",
    GH_RAW + "/dxawi/0/main/0.json",
]

# 4. 最新 LibreTV 专用点播源
LIBRE_VOD_URLS = [
    GH_RAW + "/wokaotianshi123/jgcj/main/libretv.json",
    GH_GIST + "/Shupian0510/2d02c1377bcec7c4cbb4abdc7cee0baa/raw/libretv-33-sources.json",
    GH_GIST + "/xuyishan8058/fcd88d1a9e84c024cd5bc91531803ed3/raw/libretv-sources.json",
]

# 5. 内置高可用苹果 CMS 采集接口
CMS_API_LIST = [
    {"key": "ffzy", "name": "非凡影视", "api": "http://ffzyapi.com" + API_PATH},
    {"key": "lzzy", "name": "量子资源", "api": "https://lziapi.com" + API_PATH},
    {"key": "ikun", "name": "爱坤资源", "api": "https://ikunzyapi.com" + API_PATH},
    {"key": "bfzy", "name": "暴风资源", "api": "https://bfzyapi.com" + API_PATH},
    {"key": "tyys", "name": "天涯资源", "api": "https://tyyszy.com" + API_PATH},
    {"key": "ryzy", "name": "如意资源", "api": "https://rycjapi.com" + API_PATH},
    {"key": "hwba", "name": "华为吧资源", "api": "https://cjhwba.com" + API_PATH},
    {"key": "wolong", "name": "卧龙资源", "api": "https://wolongzyw.com" + API_PATH},
    {"key": "jisu", "name": "极速资源", "api": "https://jszyapi.com" + API_PATH},
    {"key": "modu", "name": "魔都资源", "api": "https://mdzyapi.com" + API_PATH},
    {"key": "subo", "name": "速博资源", "api": "https://subocaiji.com" + API_PATH},
    {"key": "xinlang", "name": "新浪资源", "api": "https://xinlangapi.com"},
    {"key": "guangsu", "name": "光速资源", "api": "https://guangsuapi.com" + API_PATH},
    {"key": "hongniu", "name": "红牛资源", "api": "https://hongniuzy2.com" + API_PATH},
    {"key": "jinying", "name": "金鹰资源", "api": "https://jinyingzy.com" + API_PATH},
    {"key": "sdzy", "name": "闪电资源", "api": "https://sdzyapi.com" + API_PATH},
    {"key": "yhzy", "name": "樱花资源", "api": "https://apiyhzy.com" + API_PATH},
    {"key": "wujin", "name": "无尽资源", "api": "https://wujinapi.me" + API_PATH},
    {"key": "dbzy", "name": "豆瓣资源", "api": "https://dbzy.tv" + API_PATH},
    {"key": "dytt", "name": "电影天堂", "api": "http://dyttzyapi.com" + API_PATH},
    {"key": "heimuer", "name": "黑木耳资源", "api": "https://heimuer.xyz" + API_PATH},
    {"key": "niuniu", "name": "牛牛资源", "api": "https://niuniuzy.me" + API_PATH},
]

DEFAULT_SPIDER = GH_RAW + "/FongMi/CatVodSpider/main/jar/custom_spider.jar"
TIMEOUT = 6
MAX_WORKERS = 30
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
}


def fetch_url_text(url, timeout=15):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def clean_json_text(raw_text):
    text = re.sub(r"^\s*//.*$", "", raw_text, flags=re.MULTILINE)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return text


def is_chinese_channel(name):
    if re.search(r"[\u4e00-\u9fff]", name):
        return True
    upper = name.upper()
    return "CCTV" in upper or "CGTN" in upper or "TVB" in upper


def categorize_channel(name, raw_group=""):
    upper = name.upper()
    if "CCTV" in upper or "CGTN" in upper or "央视" in name or "中央" in name:
        return "央视频道"
    if "卫视" in name:
        return "卫视频道"
    if any(k in upper for k in ("凤凰", "翡翠", "明珠", "无线", "TVB", "香港", "澳门", "台湾", "东森", "中天", "民视", "年代")):
        return "港澳台频道"
    return raw_group if raw_group and raw_group not in ("未分组", "其他") else "精选地方与轮播"


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
            raw_group = group_match.group(1).strip() if group_match else ""
            current_info = {"name": name, "group": categorize_channel(name, raw_group)}
        elif line.startswith("http") and current_info:
            if is_chinese_channel(current_info["name"]):
                current_info["url"] = line
                parsed.append(current_info)
            current_info = {}
        elif "," in line and "http" in line and not line.startswith("#"):
            ch_name, ch_url = line.split(",", 1)
            ch_name, ch_url = ch_name.strip(), ch_url.strip()
            if ch_url.startswith("http") and is_chinese_channel(ch_name):
                parsed.append({"name": ch_name, "group": categorize_channel(ch_name), "url": ch_url})
    return parsed


def test_live_speed(channel):
    url = channel["url"]
    start_time = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "okhttp/3.15"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            status, chunk = resp.status, resp.read(512)
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
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for res in executor.map(func, items):
            if res:
                results.append(res)
    return results


def collect_all_vod():
    merged_cms = {item["api"].rstrip("/"): dict(item) for item in CMS_API_LIST}
    tvbox_base, extra_sites, all_parses = {}, [], []

    for url in LIBRE_VOD_URLS:
        try:
            data = json.loads(clean_json_text(fetch_url_text(url, timeout=15)))
            items = data if isinstance(data, list) else (
                data.get("sources") or [{"id": k, **v} for k, v in data.get("api_site", {}).items()]
            )
            for s in items:
                api = (s.get("api") or s.get("url") or "").strip()
                norm_api = api.rstrip("/")
                if api.startswith("http") and norm_api not in merged_cms:
                    merged_cms[norm_api] = {
                        "key": s.get("id") or f"s_{len(merged_cms)}",
                        "name": s.get("name", "聚合源"),
                        "api": api
                    }
            print(f"LibreTV 源抓取成功: {url.split('/')[-1]}")
        except Exception as e:
            print(f"抓取 LibreTV 源失败: {url} -> {e}")

    for url in TVBOX_VOD_URLS:
        try:
            data = json.loads(clean_json_text(fetch_url_text(url, timeout=15)))
            if not tvbox_base and data.get("spider"):
                tvbox_base = data
            for s in data.get("sites", []):
                api = str(s.get("api", "")).strip()
                norm_api = api.rstrip("/")
                if s.get("type") in (0, 1) and api.startswith("http") and norm_api not in merged_cms:
                    merged_cms[norm_api] = {
                        "key": s.get("key") or f"t_{len(merged_cms)}",
                        "name": s.get("name", "TVBox站"),
                        "api": api
                    }
                elif not api.startswith("http"):
                    extra_sites.append(s)
            all_parses.extend(data.get("parses", []))
            print(f"TVBox 源抓取成功: {url.split('/')[-1]}")
        except Exception as e:
            print(f"抓取 TVBox 源失败: {url} -> {e}")

    return list(merged_cms.values()), tvbox_base, extra_sites, all_parses

def main():
    tag = time.strftime("%Y-%m-%d %H:%M:%S UTC")

    # 1. 抓取并测速直播频道
    raw_ch = []
    for u in LIVE_SOURCE_URLS:
        try:
            text = fetch_url_text(u, timeout=20)
            items = parse_live_content(text)
            print(f"直播源抓取成功: {u.split('/')[-1]} -> {len(items)} 条")
            raw_ch.extend(items)
        except Exception as e:
            print(f"直播源抓取失败: {u} -> {e}")

    uniq_ch = list({c["url"]: c for c in raw_ch}.values())
    val_ch = sorted(run_parallel(test_live_speed, uniq_ch, MAX_WORKERS), key=lambda x: x.get("latency", 999))

    # 兜底机制：如果海外服务器测速通过少于 50 条，补充未测通的国内双栈源
    if len(val_ch) < 50 and uniq_ch:
        tested_urls = {c["url"] for c in val_ch}
        ipv4_pool = [c for c in uniq_ch if "://[" not in c["url"] and c["url"] not in tested_urls]
        ipv6_pool = [c for c in uniq_ch if "://[" in c["url"] and c["url"] not in tested_urls]
        val_ch.extend(ipv4_pool[:300] + ipv6_pool[:300])
        print(f"启用双栈兜底：IPv4 补充 {min(len(ipv4_pool), 300)} 条，IPv6 补充 {min(len(ipv6_pool), 300)} 条")

    group_order = ["央视频道", "卫视频道", "港澳台频道", "精选地方与轮播"]
    groups = {g: [] for g in group_order}
    for c in val_ch:
        groups.setdefault(c["group"], []).append(c)
    groups = {k: v for k, v in groups.items() if v}

    ordered_ch = [c for cl in groups.values() for c in cl]

    with open("libre_tv.m3u", "w", encoding="utf-8") as f:
        f.write(f"#EXTM3U # Updated: {tag}\n" + "".join(
            f'#EXTINF:-1 group-title="{c["group"]}",{c["name"]}\n{c["url"]}\n' for c in ordered_ch
        ))

    with open("tvbox_live.txt", "w", encoding="utf-8") as f:
        f.write(f"更新时间: {tag},#genre#\n\n" + "".join(
            f"{g},#genre#\n" + "".join(f"{c['name']},{c['url']}\n" for c in cl) + "\n" for g, cl in groups.items()
        ))

    # 2. 抓取并测速点播源
    cms_pool, tv_base, ex_sites, ex_parses = collect_all_vod()
    print(f"共收集到去重后点播接口 {len(cms_pool)} 个，开始测速...")
    val_cms = sorted(run_parallel(test_cms_api, cms_pool, 25), key=lambda x: x.get("latency", 888))

    if len(val_cms) < 10:
        for i in cms_pool[:40]:
            i.setdefault("latency", 888)
        val_cms = cms_pool[:40]

    # 3. 生成 libre_vod.json（内置豆瓣榜单 + 完整 CMS 源）
    douban_libre_source = {
        "id": "douban",
        "name": "🎬豆瓣热播与评分推荐",
        "api": "https://douban.com",
        "url": "https://douban.com",
        "type": "douban"
    }

    libre_sources = [douban_libre_source] + [
        {
            "id": i["key"],
            "name": f"{i['name']} ({i.get('latency', 888)}ms)",
            "api": i["api"],
            "url": i["api"],
            "type": "cms"
        }
        for i in val_cms
    ]

    # 同时兼容 api_site 字典格式（适配不同版本的 LibreTV 前端）
    api_site_dict = {
        s["id"]: {"name": s["name"], "api": s["api"], "detail": ""}
        for s in libre_sources
    }

    with open("libre_vod.json", "w", encoding="utf-8") as f:
        json.dump({
            "version": 2,
            "updated_at": tag,
            "cache_time": 7200,
            "douban": {
                "enabled": True,
                "api": "https://douban.com"
            },
            "sources": libre_sources,
            "api_site": api_site_dict
        }, f, ensure_ascii=False, indent=2)

    # 4. 生成 tvbox.json
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

    print(f"完成！TVBox {len(final_tv['sites'])} 站（含豆瓣榜单），LibreTV {len(libre_sources)} 源（含豆瓣），直播 {len(ordered_ch)} 台。")


if __name__ == "__main__":
    main()
