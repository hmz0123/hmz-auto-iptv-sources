import json
import re
import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ssl._create_default_https_context = ssl._create_unverified_context

GH_RAW = "https://" + "://githubusercontent.com"
GH_IO = "https://" + "iptv-org.github.io"

# 1. 上游直播源列表
LIVE_SOURCE_URLS = [
    GH_IO + "/iptv/languages/zho.m3u",
    GH_RAW + "/Guovin/iptv-api/gd/output/result.m3u",
    GH_RAW + "/vbskycn/iptv/master/tv/iptv4.m3u",
    GH_RAW + "/Kimentanm/aptv/master/m3u/iptv.m3u",
]

# 2. 上游点播源列表（公开维护的 TVBox 仓库配置）
VOD_SOURCE_URLS = [
    GH_RAW + "/gaotianliuyun/gao/master/js.json",
    GH_RAW + "/FongMi/CatVodSpider/main/json/config.json",
    GH_RAW + "/yoursmile66/TVBox/main/XC.json",
]

# 3. 公开 CMS 资源站采集接口（适配 LibreTV 与 TVBox 通用点播）
CMS_API_LIST = [
    {"key": "ffzy", "name": "非凡影视", "api": "http://" + "://ffzyapi.com"},
    {"key": "lzzy", "name": "量子资源", "api": "https://" + "://lziapi.com"},
    {"key": "ikun", "name": "爱坤资源", "api": "https://" + "://ikunzyapi.com"},
    {"key": "bfzy", "name": "暴风资源", "api": "https://" + "://bfzyapi.com"},
    {"key": "tyys", "name": "天涯资源", "api": "https://" + "://tyyszy.com"},
]

TIMEOUT = 6
MAX_WORKERS = 30
HEADERS = {"User-Agent": "okhttp/3.15"}


def fetch_url_text(url, timeout=15):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


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
            parts = line.split(",", 1)
            if len(parts) == 2 and parts.strip().startswith("http"):
                parsed.append({
                    "name": parts.strip(),
                    "group": "精选频道",
                    "url": parts.strip(),
                })
    return parsed


def test_live_speed(channel):
    url = channel["url"]
    if "://[" in url:
        return None
    start_time = time.time()
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            if resp.status in (200, 206, 301, 302):
                if len(resp.read(512)) > 0:
                    channel["latency"] = round((time.time() - start_time) * 1000)
                    return channel
    except Exception:
        pass
    return None


def test_cms_api(site):
    """测试点播 CMS 接口响应速度与可用性"""
    start_time = time.time()
    try:
        test_url = site["api"] + "?ac=list&pg=1"
        text = fetch_url_text(test_url, timeout=TIMEOUT)
        data = json.loads(text)
        if "list" in data or "class" in data:
            site["latency"] = round((time.time() - start_time) * 1000)
            return site
    except Exception:
        pass
    return None


def fetch_tvbox_vod():
    """抓取上游 TVBox 点播配置（爬虫、站点、解析）"""
    for url in VOD_SOURCE_URLS:
        try:
            raw_text = fetch_url_text(url, timeout=15)
            # 去除 JSON 中的单行注释
            clean_text = re.sub(r"^\s*//.*$", "", raw_text, flags=re.MULTILINE)
            data = json.loads(clean_text)
            if "sites" in data and len(data["sites"]) > 0:
                print(f"成功获取 TVBox 点播源: {url}，包含 {len(data['sites'])} 个点播站点")
                return data
        except Exception as e:
            print(f"获取点播源失败 {url}: {e}")
    return {}


def main():
    # --- 阶段一：处理直播源 ---
    print("1. 开始抓取并测速直播源...")
    raw_channels = []
    for url in LIVE_SOURCE_URLS:
        try:
            content = fetch_url_text(url)
            raw_channels.extend(parse_live_content(content))
        except Exception as e:
            print(f"直播源获取失败 {url}: {e}")

    unique_channels = list({ch["url"]: ch for ch in raw_channels}.values())
    valid_channels = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        for res in executor.map(test_live_speed, unique_channels):
            if res:
                valid_channels.append(res)

    valid_channels.sort(key=lambda x: x["latency"])
    if len(valid_channels) < 10 and len(unique_channels) > 0:
        valid_channels = [ch for ch in unique_channels if "://[" not in ch["url"]][:200]

    # 保存 LibreTV 直播 M3U
    with open("libre_tv.m3u", "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for ch in valid_channels:
            f.write(f'#EXTINF:-1 group-title="{ch["group"]}",{ch["name"]}\n{ch["url"]}\n')

    # 保存 TVBox 直播 TXT
    groups = {}
    for ch in valid_channels:
        groups.setdefault(ch["group"], []).append(ch)
    with open("tvbox_live.txt", "w", encoding="utf-8") as f:
        for group_name, ch_list in groups.items():
            f.write(f"{group_name},#genre#\n")
            for ch in ch_list:
                f.write(f"{ch['name']},{ch['url']}\n")
            f.write("\n")

    # --- 阶段二：处理点播源 ---
    print("2. 开始测速 CMS 点播接口并合并 TVBox 点播源...")
    valid_cms = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        for res in executor.map(test_cms_api, CMS_API_LIST):
            if res:
                valid_cms.append(res)
    valid_cms.sort(key=lambda x: x["latency"])
    print(f"有效 CMS 点播接口共 {len(valid_cms)} 个")

    # 生成 LibreTV 专用点播配置 (libre_vod.json)
    libre_vod_data = {
        "cache_time": 7200,
        "api_site": {
            item["key"]: {"api": item["api"], "name": item["name"]}
            for item in valid_cms
        },
    }
    with open("libre_vod.json", "w", encoding="utf-8") as f:
        json.dump(libre_vod_data, f, ensure_ascii=False, indent=2)

    # 合并生成 TVBox 完整版配置（点播 + 测速直播）
    vod_config = fetch_tvbox_vod()
    cms_sites_for_tvbox = [
        {
            "key": item["key"],
            "name": f"⚡{item['name']}({item['latency']}ms)",
            "type": 1,
            "api": item["api"],
            "searchable": 1,
            "quickSearch": 1,
            "filterable": 1,
        }
        for item in valid_cms
    ]

    live_url = GH_RAW + "/hmz0123/hmz-auto-iptv-sources/main/tvbox_live.txt"
    wallpaper_url = "https://" + "tuapi.eees.cc/api.php?category=fengjing&type=302"

    final_tvbox = {
        "spider": vod_config.get("spider", ""),
        "wallpaper": vod_config.get("wallpaper", wallpaper_url),
        "lives": [
            {
                "name": "自动测速精选直播",
                "type": 0,
                "url": live_url,
                "playerType": 1,
                "ua": "okhttp/3.15",
            }
        ],
        "sites": cms_sites_for_tvbox + vod_config.get("sites", []),
        "parses": vod_config.get("parses", []),
        "flags": vod_config.get("flags", []),
        "ijk": vod_config.get("ijk", []),
        "ads": vod_config.get("ads", []),
    }

    with open("tvbox.json", "w", encoding="utf-8") as f:
        json.dump(final_tvbox, f, ensure_ascii=False, indent=2)

    print("全部更新完成！")


if __name__ == "__main__":
    main()
