import json
import re
import ssl
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# 忽略部分直播源的 SSL 证书过期报错
ssl._create_default_https_context = ssl._create_unverified_context

# 使用字符串拼接防止复制代码时网址被自动截断
GH_RAW = "https://" + "raw.githubusercontent.com"
GH_IO = "https://" + "iptv-org.github.io"

SOURCE_URLS = [
    GH_IO + "/iptv/languages/zho.m3u",
    GH_RAW + "/Guovin/iptv-api/gd/output/result.m3u",
    GH_RAW + "/vbskycn/iptv/master/tv/iptv4.m3u",
    GH_RAW + "/Kimentanm/aptv/master/m3u/iptv.m3u",
]

TIMEOUT = 6  # 测速超时时间（秒）
MAX_WORKERS = 30


def fetch_sources():
    """从上游获取所有频道信息"""
    channels = []
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
    }
    for url in SOURCE_URLS:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode("utf-8", errors="ignore")
                parsed = parse_content(content)
                print(f"成功获取源 {url}，解析出 {len(parsed)} 个频道")
                channels.extend(parsed)
        except Exception as e:
            print(f"获取源失败 {url}: {e}")
    return channels


def parse_content(content):
    """兼容解析 M3U 和 TXT 格式"""
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
            if len(parts) == 2 and parts[1].strip().startswith("http"):
                parsed.append({
                    "name": parts[0].strip(),
                    "group": "精选频道",
                    "url": parts[1].strip(),
                })
    return parsed


def test_speed(channel):
    """测试流媒体链接连通性与响应延迟"""
    url = channel["url"]
    if "://[" in url:
        return None

    start_time = time.time()
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "okhttp/3.15"}
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            if resp.status in (200, 206, 301, 302):
                chunk = resp.read(512)
                if len(chunk) > 0:
                    latency = round((time.time() - start_time) * 1000)
                    channel["latency"] = latency
                    return channel
    except Exception:
        pass
    return None


def save_for_libre(valid_channels, filename="libre_tv.m3u"):
    with open(filename, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for ch in valid_channels:
            f.write(f'#EXTINF:-1 group-title="{ch["group"]}",{ch["name"]}\n')
            f.write(f'{ch["url"]}\n')


def save_for_tvbox(
    valid_channels, txt_filename="tvbox_live.txt", json_filename="tvbox.json"
):
    groups = {}
    for ch in valid_channels:
        groups.setdefault(ch["group"], []).append(ch)

    with open(txt_filename, "w", encoding="utf-8") as f:
        for group_name, ch_list in groups.items():
            f.write(f"{group_name},#genre#\n")
            for ch in ch_list:
                f.write(f"{ch['name']},{ch['url']}\n")
            f.write("\n")

    live_url = (
        GH_RAW + "/hmz0123/hmz-auto-iptv-sources/main/" + txt_filename
    )
    wallpaper_url = "https://" + "tuapi.eees.cc/api.php?category=fengjing&type=302"

    tvbox_config = {
        "spider": "",
        "wallpaper": wallpaper_url,
        "lives": [{
            "name": "自动测速精选直播",
            "type": 0,
            "url": live_url,
            "playerType": 1,
            "ua": "okhttp/3.15",
        }],
        "sites": [],
        "parses": [],
    }
    with open(json_filename, "w", encoding="utf-8") as f:
        json.dump(tvbox_config, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    print("开始抓取上游源...")
    raw_channels = fetch_sources()
    unique_channels = list({ch["url"]: ch for ch in raw_channels}.values())
    print(f"去重后共 {len(unique_channels)} 个频道，开始并发测速...")

    valid_channels = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        results = executor.map(test_speed, unique_channels)
        for res in results:
            if res:
                valid_channels.append(res)

    valid_channels.sort(key=lambda x: x["latency"])
    print(f"测速完成，有效可用频道共 {len(valid_channels)} 个。")

    # 如果海外机房测速通过率过低，保留前 200 个非 IPv6 源作为备用
    if len(valid_channels) < 10 and len(unique_channels) > 0:
        print("警告：海外服务器测速通过率过低，启用备用保留策略...")
        fallback = [ch for ch in unique_channels if "://[" not in ch["url"]][
            :200
        ]
        for ch in fallback:
            ch["latency"] = 999
        valid_channels = fallback

    save_for_libre(valid_channels)
    save_for_tvbox(valid_channels)
    print(f"写入完成，最终保存 {len(valid_channels)} 个频道。")
