import os
import re
import time
import urllib.parse
import urllib.request
from core.utils import fetch_url_text, idn_encode, run_parallel


def normalize_channel_name(raw_name):
    name = str(raw_name or "").strip()
    name = re.sub(r"[\[\(（【][^\]\)）】]*[\]\)）】]", "", name).strip()
    name = re.sub(
        r"[-_\s]*(?:4k|2160p?|1080p?|720p?|uhd|fhd|hd|sd|超清|高清|标清|蓝光|高码|ipv6|ipv4|备用\d*)$",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip()
    upper = name.upper()
    m_cctv = re.match(r"^CCTV[-\s]*(\d+\+?)(?:[\u4e00-\u9fff]*)$", upper)
    if m_cctv:
        return f"CCTV{m_cctv.group(1)}"
    if upper.startswith("CGTN"):
        return re.sub(r"[-\s]+", "", upper)
    if "卫视" in name:
        name = re.sub(r"(?:高清|频道)$", "", name).strip()
    return name or str(raw_name).strip()


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
    if any(
        k in upper
        for k in ("凤凰", "翡翠", "明珠", "无线", "TVB", "香港", "澳门", "台湾", "东森", "中天", "民视", "年代")
    ):
        return "港澳台频道"
    return raw_group if raw_group and raw_group not in ("未分组", "其他") else "精选地方与轮播"


def parse_live_content(content):
    parsed, current_info = [], {}
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            name_match = re.search(r",([^,]+)$", line)
            norm_name = normalize_channel_name(name_match.group(1).strip() if name_match else "未知频道")
            group_match = re.search(r'group-title="([^"]+)"', line)
            raw_group = group_match.group(1).strip() if group_match else ""
            current_info = {"name": norm_name, "group": categorize_channel(norm_name, raw_group)}
        elif line.startswith("http") and current_info:
            if is_chinese_channel(current_info["name"]):
                current_info["url"] = line
                parsed.append(current_info)
            current_info = {}
        elif "," in line and "http" in line and not line.startswith("#"):
            ch_name, ch_url = line.split(",", 1)
            ch_name, ch_url = normalize_channel_name(ch_name), ch_url.strip()
            if ch_url.startswith("http") and is_chinese_channel(ch_name):
                parsed.append({"name": ch_name, "group": categorize_channel(ch_name), "url": ch_url})
    return parsed


def _is_valid_stream_chunk(chunk, content_type=""):
    if not chunk or "text/html" in content_type.lower():
        return False
    head_str = chunk[:256].lstrip().lower()
    if head_str.startswith(b"<!doctype") or head_str.startswith(b"<html"):
        return False
    if b"#extm3u" in head_str or chunk[0:1] == b"\x47" or chunk[:3] == b"FLV" or b"ftyp" in chunk[:32]:
        return True
    return any(t in content_type.lower() for t in ("video/", "audio/", "mpegurl", "octet-stream"))


def _live_quality_bonus(channel):
    text = f"{channel.get('name', '')} {channel.get('url', '')}".lower()
    bonus = 0
    if any(k in text for k in ("4k", "2160", "uhd")):
        bonus += 180
    elif any(k in text for k in ("1080", "fhd", "蓝光", "高码", "超清")):
        bonus += 120
    elif any(k in text for k in ("720", "hd", "高清")):
        bonus += 60
    if re.search(r"https?://\[[0-9a-f:]+\]", text):
        bonus += 50
    return bonus


def _probe_m3u8_slice_speed(m3u8_text: str, m3u8_url: str, timeout: int = 3) -> float:
    lines = [ln.strip() for ln in m3u8_text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        return -1.0
    sample_uri = urllib.parse.urljoin(idn_encode(m3u8_url), lines[-1])
    if ".m3u8" in sample_uri.lower():
        try:
            sub_txt = fetch_url_text(sample_uri, timeout=timeout, prefer_okhttp=True)
            sub_lines = [ln.strip() for ln in sub_txt.splitlines() if ln.strip() and not ln.strip().startswith("#")]
            if not sub_lines:
                return -1.0
            sample_uri = urllib.parse.urljoin(idn_encode(sample_uri), sub_lines[-1])
        except Exception:
            return -1.0

    t0 = time.time()
    try:
        req = urllib.request.Request(sample_uri, headers={"User-Agent": "okhttp/3.15"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status not in (200, 206):
                return -1.0
            chunk = resp.read(262144)
            elapsed = max(0.02, time.time() - t0)
            return round((len(chunk) / 1024.0) / elapsed, 1) if len(chunk) >= 4096 else -1.0
    except Exception:
        return -1.0


def process_live_sources_and_build(registry, new_live_urls, tag, settings):
    """并发抓取、真实切片测速直播源并生成 tvbox_live.txt"""
    th = settings.get("thresholds", {})
    req_timeout = int(th.get("stream_timeout", th.get("live_timeout", th.get("timeout", 6))))
    min_slice_kbs = float(th.get("min_live_slice_speed_kbs", 220))
    drop_slow = bool(th.get("drop_slow_live_streams", True))
    min_valid_ch = int(th.get("min_valid_live_channels", 20))
    max_per_ch = int(th.get("max_streams_per_channel", 5))
    max_live_total = int(th.get("max_live_channels", 1500))
    max_workers = int(th.get("max_workers", 20))
    max_suspend = int(settings.get("registry", {}).get("max_seed_suspend_streak", 4))
    purged_set = set(registry.get("purged_seeds", []))
    now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    live_items = [
        x for x in registry.get("live_sources", [])
        if str(x.get("url", "")).strip() and " " not in str(x.get("url", "")).strip() and str(x.get("url", "")).strip() not in purged_set
    ]
    existing_urls = {x["url"] for x in live_items}
    for u in new_live_urls:
        clean_u = str(u or "").strip()
        if clean_u and " " not in clean_u and clean_u not in purged_set and clean_u not in existing_urls:
            live_items.append({"url": clean_u, "origin": "discovered", "fail_streak": 0, "latency": 999, "valid_items": 0, "last_ok_at": ""})

    candidates_to_fetch = []
    for idx, src in enumerate(live_items):
        if src.get("origin") == "seed" and int(src.get("fail_streak", 0)) >= max_suspend and idx % 2 == 1:
            continue
        candidates_to_fetch.append((idx, src))

    def fetch_one_live_source(idx_src):
        _, src = idx_src
        u = src["url"]
        t0 = time.time()
        try:
            txt = fetch_url_text(u, timeout=10)
            lat = round((time.time() - t0) * 1000)
            items = parse_live_content(txt)
            return (u, True, lat, items, "")
        except Exception as e:
            return (u, False, 999, [], str(e))

    fetch_results = run_parallel(fetch_one_live_source, candidates_to_fetch, workers=min(12, max_workers))
    result_by_url = {res[0]: res for res in fetch_results}

    raw_ch = []
    for _, src in candidates_to_fetch:
        res = result_by_url.get(src["url"])
        if not res:
            continue
        u, ok, lat, items, err = res
        if ok and len(items) >= 5:
            src.update({"fail_streak": 0, "latency": lat, "valid_items": len(items), "last_ok_at": now_str})
            raw_ch.extend(items)
            print(f"直播源有效: {u.split('/')[-1]} -> {len(items)} 条 ({lat}ms)")
        else:
            src["fail_streak"] = int(src.get("fail_streak", 0)) + 1
            src["valid_items"] = len(items)
            if not ok:
                print(f"直播源失败: {u} -> {err}")

    registry["live_sources"] = live_items

    def test_channel(channel):
        url = channel["url"]
        is_hd = any(k in f"{channel.get('name', '')} {url}".lower() for k in ("4k", "2160", "1080", "fhd", "蓝光", "高码", "超清"))
        for attempt in range(2):
            t0 = time.time()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "okhttp/3.15"})
                with urllib.request.urlopen(req, timeout=req_timeout) as resp:
                    chunk = resp.read(16384)
                    if resp.status in (200, 206) and _is_valid_stream_chunk(chunk, resp.headers.get("Content-Type", "")):
                        lat = round((time.time() - t0) * 1000)
                        penalty, slice_kbs = 0, -1.0
                        if b"#extm3u" in chunk.lower():
                            slice_kbs = _probe_m3u8_slice_speed(chunk.decode("utf-8", errors="ignore"), url, timeout=3)
                            if slice_kbs <= 0:
                                return None
                            required_spd = min_slice_kbs if is_hd else max(120.0, min_slice_kbs * 0.65)
                            if slice_kbs < required_spd:
                                if drop_slow:
                                    return None
                                penalty += 320
                        else:
                            direct_kbs = round((len(chunk) / 1024.0) / max(0.02, time.time() - t0), 1)
                            slice_kbs = direct_kbs
                            if drop_slow and direct_kbs < 100.0:
                                return None

                        speed_bonus = min(140, int(slice_kbs / 8.0)) if slice_kbs > 0 else 0
                        channel["latency"] = lat
                        channel["slice_kbs"] = slice_kbs
                        channel["score"] = max(1, lat - _live_quality_bonus(channel) - speed_bonus + penalty)
                        return channel
            except Exception:
                if attempt == 0:
                    time.sleep(0.05)
        return None

    uniq_ch = list({c["url"]: c for c in raw_ch}.values())
    val_ch = sorted(run_parallel(test_channel, uniq_ch, max_workers), key=lambda x: (x.get("score", 999), x.get("latency", 999)))

    if len(val_ch) < min_valid_ch and os.path.exists("tvbox_live.txt"):
        with open("tvbox_live.txt", "r", encoding="utf-8") as f:
            existing_lines = [ln.strip() for ln in f.read().splitlines() if ln.strip() and "," in ln and not ln.strip().endswith(",#genre#")]
        if existing_lines:
            print(f"有效直播流仅 {len(val_ch)} 条，保留历史 tvbox_live.txt（{len(existing_lines)} 条）兜底。")
            return len(existing_lines)

    groups = {g: [] for g in ["央视频道", "卫视频道", "港澳台频道", "精选地方与轮播"]}
    for c in val_ch:
        groups.setdefault(c["group"], []).append(c)

    trimmed_groups, total_channels = {}, 0
    for g, cl in groups.items():
        per_name, kept = {}, []
        for item in cl:
            if total_channels + len(kept) >= max_live_total:
                break
            if per_name.get(item["name"], 0) < max_per_ch:
                per_name[item["name"]] = per_name.get(item["name"], 0) + 1
                kept.append(item)
        if kept:
            trimmed_groups[g] = kept
            total_channels += len(kept)

    with open("tvbox_live.txt", "w", encoding="utf-8") as f:
        f.write(
            f"更新时间: {tag},#genre#\n\n"
            + "".join(f"{g},#genre#\n" + "".join(f"{c['name']},{c['url']}\n" for c in cl) + "\n" for g, cl in trimmed_groups.items())
        )
    return total_channels
