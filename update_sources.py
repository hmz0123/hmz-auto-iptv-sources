import json
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# 1. 配置上游源列表（你可以随时在此增减公开的 m3u 或 txt 源链接）
SOURCE_URLS = [
    "https://github.io",
    "https://githubusercontent.com",
]

TIMEOUT = 3  # 测速超时时间（秒）
MAX_WORKERS = 30  # 并发测速线程数


def fetch_sources():
    """从上游获取所有频道信息"""
    channels = []
    for url in SOURCE_URLS:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                content = resp.read().decode("utf-8", errors="ignore")
                channels.extend(parse_m3u(content))
        except Exception as e:
            print(f"获取源失败 {url}: {e}")
    return channels


def parse_m3u(content):
    """解析 M3U 内容提取频道名、分组和链接"""
    lines = content.splitlines()
    parsed = []
    current_info = {}

    for line in lines:
        line = line.strip()
        if line.startswith("#EXTINF:"):
            # 提取频道名称
            name_match = re.search(r",([^,]+)$", line)
            name = name_match.group(1).strip() if name_match else "未知频道"
            # 提取分组名称
            group_match = re.search(r'group-title="([^"]+)"', line)
            group = group_match.group(1).strip() if group_match else "综合频道"
            current_info = {"name": name, "group": group}
        elif line.startswith("http") and current_info:
            current_info["url"] = line
            parsed.append(current_info)
            current_info = {}
    return parsed


def test_speed(channel):
    """测试流媒体链接连通性与响应延迟"""
    url = channel["url"]
    start_time = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            if resp.status == 200:
                # 尝试读取前 1KB 数据确保流有效
                chunk = resp.read(1024)
                if len(chunk) > 0:
                    latency = round((time.time() - start_time) * 1000)
                    channel["latency"] = latency
                    return channel
    except Exception:
        pass
    return None


def save_for_libre(valid_channels, filename="libre_tv.m3u"):
    """生成标准 M3U 格式，适配 LibreTV / Kodi / PotPlayer 等"""
    with open(filename, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for ch in valid_channels:
            f.write(f'#EXTINF:-1 group-title="{ch["group"]}",{ch["name"]}\n')
            f.write(f'{ch["url"]}\n')


def save_for_tvbox(
    valid_channels, txt_filename="tvbox_live.txt", json_filename="tvbox.json"
):
    """生成 TVBox 支持的 TXT 直播源及标准 JSON 配置文件"""
    # 1. 按分组整理 TXT 格式（TVBox 直播默认格式：频道名,链接）
    groups = {}
    for ch in valid_channels:
        groups.setdefault(ch["group"], []).append(ch)

    with open(txt_filename, "w", encoding="utf-8") as f:
        for group_name, ch_list in groups.items():
            f.write(f"{group_name},#genre#\n")
            for ch in ch_list:
                f.write(f"{ch['name']},{ch['url']}\n")
            f.write("\n")

    # 2. 生成完整的 TVBox 订阅 JSON 框架，引用同目录下的直播源文件
    tvbox_config = {
        "spider": "",
        "wallpaper": "https://eees.cc",
        "lives": [
            {
                "name": "自动测速精选直播",
                "type": 0,
                "url": f"./{txt_filename}",
                "playerType": 1,
                "ua": "okhttp/3.15",
            }
        ],
        "sites": [],
        "parses": [],
    }
    with open(json_filename, "w", encoding="utf-8") as f:
        json.dump(tvbox_config, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    print("开始抓取上游源...")
    raw_channels = fetch_sources()
    # 根据 URL 去重
    unique_channels = list({ch["url"]: ch for ch in raw_channels}.values())
    print(f"去重后共 {len(unique_channels)} 个频道，开始并发测速...")

    valid_channels = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        results = executor.map(test_speed, unique_channels)
        for res in results:
            if res:
                valid_channels.append(res)

    # 按延迟从低到高排序（速度越快越靠前）
    valid_channels.sort(key=lambda x: x["latency"])
    print(f"测速完成，有效可用频道共 {len(valid_channels)} 个。")

    save_for_libre(valid_channels)
    save_for_tvbox(valid_channels)
    print("文件已生成：libre_tv.m3u、tvbox_live.txt、tvbox.json")
