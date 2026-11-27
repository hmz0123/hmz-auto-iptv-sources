from datetime import datetime, timezone
import hashlib
import html
import json
import os
import random
import re
import time
import urllib.parse
from core.utils import (
    canonical_cache_id,
    fetch_url_text,
    is_invalid_site_url,
    is_junk_or_ad_site,
    is_pan_site,
    normalize_site_display_name,
    parse_json_lenient,
    resolve_relative_url,
    run_parallel,
    sanitize_discovered_url,
)


def _cleanup_alias_cache_files(cache_dir, canonical_ids):
    """清理历史重复别名缓存文件，仅保留规范 canonical_id 快照"""
    if not os.path.isdir(cache_dir):
        return
    keep_names = {f"{canonical_cache_id(k)}.json" for k in canonical_ids} | {"ziyuanzu_cms.json"}
    try:
        for fn in os.listdir(cache_dir):
            if fn.endswith(".json") and fn not in keep_names:
                full = os.path.join(cache_dir, fn)
                if os.path.isfile(full):
                    os.remove(full)
    except Exception:
        pass


def load_or_init_source_registry(settings, seeds, dead_hosts):
    reg_file = settings.get("registry", {}).get("file", "source_registry.json")
    purged_existing = []
    registry = {
        "updated_at": "",
        "purged_seeds": [],
        "live_sources": [],
        "vod_sources": [],
        "cms_apis": [],
        "discovery_pages": list(seeds.get("discovery_pages", [])),
    }
    if os.path.exists(reg_file):
        try:
            with open(reg_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    purged_existing = [
                        str(x).strip()
                        for x in (loaded.get("purged_seeds") or [])
                        if isinstance(x, str) and x.strip()
                    ]
                    registry.update(loaded)
        except Exception as e:
            print(f"读取 {reg_file} 失败，将重新初始化: {e}")

    purged_set = set(purged_existing)
    registry["purged_seeds"] = sorted(purged_set)

    live_map = {}
    for item in registry.get("live_sources", []):
        clean_u = sanitize_discovered_url(item.get("url", ""), dead_hosts)
        if clean_u and clean_u not in purged_set and clean_u not in live_map:
            live_map[clean_u] = {**item, "url": clean_u}
    for u in seeds.get("live_source_urls", []):
        clean_u = sanitize_discovered_url(u, dead_hosts)
        if clean_u and clean_u not in purged_set:
            live_map.setdefault(
                clean_u,
                {
                    "url": clean_u,
                    "origin": "seed",
                    "fail_streak": 0,
                    "latency": 999,
                    "valid_items": 0,
                    "last_ok_at": "",
                },
            )["origin"] = "seed"

    vod_map = {}
    for item in registry.get("vod_sources", []):
        clean_u = sanitize_discovered_url(item.get("url", ""), dead_hosts)
        if clean_u and clean_u not in purged_set and clean_u not in vod_map:
            vod_map[clean_u] = {"yield_rate": 1.0, **item, "url": clean_u}
    seed_vods = [
        g["urls"][0]
        for g in seeds.get("tvbox_vod_groups", [])
        if g.get("urls")
    ] + list(seeds.get("extra_vod_urls", []))
    for u in dict.fromkeys(seed_vods):
        clean_u = sanitize_discovered_url(u, dead_hosts)
        if clean_u and clean_u not in purged_set:
            vod_map.setdefault(
                clean_u,
                {
                    "url": clean_u,
                    "origin": "seed",
                    "fail_streak": 0,
                    "latency": 999,
                    "valid_items": 0,
                    "yield_rate": 1.0,
                    "last_ok_at": "",
                },
            )["origin"] = "seed"

    cms_map = {}
    for it in registry.get("cms_apis", []):
        raw_api = str(it.get("api", "")).strip()
        clean_api = sanitize_discovered_url(raw_api, dead_hosts)
        if clean_api:
            norm = clean_api.rstrip("/")
            if norm not in purged_set and clean_api not in purged_set:
                cms_map[norm] = {
                    "health_score": 80.0 if it.get("origin") == "seed" else 60.0,
                    **it,
                    "api": clean_api,
                }
    for c in seeds.get("cms_api_list", []):
        clean_api = sanitize_discovered_url(c.get("api", ""), dead_hosts)
        if clean_api:
            norm = clean_api.rstrip("/")
            if norm not in purged_set and clean_api not in purged_set:
                cms_map.setdefault(
                    norm,
                    {
                        "key": c["key"],
                        "name": c["name"],
                        "api": clean_api,
                        "origin": "seed",
                        "fail_streak": 0,
                        "latency": 999,
                        "health_score": 85.0,
                        "last_ok_at": "",
                    },
                )["origin"] = "seed"

    registry["live_sources"] = list(live_map.values())
    registry["vod_sources"] = list(vod_map.values())
    registry["cms_apis"] = list(cms_map.values())
    registry["discovery_pages"] = list(
        dict.fromkeys(
            [
                sanitize_discovered_url(p, dead_hosts)
                for p in (registry.get("discovery_pages", []) + seeds.get("discovery_pages", []))
                if sanitize_discovered_url(p, dead_hosts)
            ]
        )
    )
    return registry


def save_source_registry(registry, tag, settings):
    reg_cfg = settings.get("registry", {})
    reg_file = reg_cfg.get("file", "source_registry.json")
    max_disc_fail = int(reg_cfg.get("max_discovered_fail_streak", 3))
    max_seed_suspend = int(reg_cfg.get("max_seed_suspend_streak", 4))
    purge_streak = int(reg_cfg.get("purge_seed_fail_streak", 15))
    max_live_cap = int(reg_cfg.get("max_live_sources_cap", 60))
    max_vod_cap = int(reg_cfg.get("max_vod_sources_cap", 80))
    max_cms_cap = int(reg_cfg.get("max_cms_apis_cap", 150))
    purged_set = set(registry.get("purged_seeds", []))

    def prune_urls(items, cap, check_yield=False):
        kept = []
        for it in items:
            u = str(it.get("url") or it.get("api") or "").strip()
            if not u or " " in u or u in purged_set:
                continue
            origin, streak = it.get("origin", "discovered"), int(it.get("fail_streak", 0))
            if origin == "discovered":
                if streak >= max_disc_fail:
                    continue
                if (
                    check_yield
                    and float(it.get("yield_rate", 1.0)) < 0.15
                    and int(it.get("valid_items", 0)) > 15
                ):
                    continue
            if origin == "seed" and streak >= purge_streak:
                continue
            kept.append(it)
        kept.sort(
            key=lambda x: (
                int(x.get("fail_streak", 0)) >= max_seed_suspend,
                int(x.get("fail_streak", 0)),
                x.get("origin") != "seed",
                -float(x.get("yield_rate", 1.0)),
                int(x.get("latency", 999)),
                -int(x.get("valid_items", 0)),
            )
        )
        return kept[:cap]

    def prune_cms(items, cap):
        kept = []
        for it in items:
            api = str(it.get("api", "")).strip()
            if not api or " " in api or api in purged_set or api.rstrip("/") in purged_set:
                continue
            origin, streak = it.get("origin", "discovered"), int(it.get("fail_streak", 0))
            if origin == "discovered" and streak >= max_disc_fail:
                continue
            if origin == "seed" and streak >= purge_streak:
                continue
            kept.append(it)
        kept.sort(
            key=lambda x: (
                int(x.get("fail_streak", 0)),
                x.get("origin") != "seed",
                -float(x.get("health_score", 60.0)),
                int(x.get("latency", 999)),
            )
        )
        return kept[:cap]

    registry["updated_at"] = tag
    registry["purged_seeds"] = sorted(purged_set)
    registry["live_sources"] = prune_urls(registry.get("live_sources", []), max_live_cap)
    registry["vod_sources"] = prune_urls(registry.get("vod_sources", []), max_vod_cap, check_yield=True)
    registry["cms_apis"] = prune_cms(registry.get("cms_apis", []), max_cms_cap)

    with open(reg_file, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    print(
        f"已更新源仓 {reg_file}：直播源 {len(registry['live_sources'])} / "
        f"点播源 {len(registry['vod_sources'])} / CMS {len(registry['cms_apis'])}"
    )


def sync_famous_vod_urls_from_nav(seeds, dead_hosts):
    corpus_blocks = []
    for nav_url in seeds.get("famous_nav_pages", []):
        try:
            raw_html = html.unescape(fetch_url_text(nav_url, timeout=8))
            for blk in re.split(
                r"(?:</tr>|</li>|</div>|</p>|<br\s*/?>|\r?\n)", raw_html, flags=re.IGNORECASE
            ):
                plain = re.sub(r"<[^>]+>", " ", blk).lower()
                for u in re.findall(r"https?://[^\s\"'<>()（）【】]+", blk):
                    clean_u = sanitize_discovered_url(u, dead_hosts)
                    if (
                        clean_u
                        and not is_invalid_site_url(clean_u)
                        and "clbug.com" not in clean_u.lower()
                        and not clean_u.lower().endswith(
                            (".png", ".jpg", ".css", ".js", ".ico")
                        )
                    ):
                        corpus_blocks.append((plain, clean_u))
        except Exception as e:
            print(f"知名导航页读取跳过: {nav_url} -> {e}")

    for multi_url in seeds.get("multi_repo_urls", []):
        try:
            data = parse_json_lenient(
                fetch_url_text(multi_url, timeout=8, prefer_okhttp=True)
            )
            entries = (
                (data.get("urls") or data.get("订阅") or [])
                if isinstance(data, dict)
                else []
            )
            for item in entries:
                if isinstance(item, dict):
                    clean_u = sanitize_discovered_url(item.get("url", ""), dead_hosts)
                    if item.get("name") and clean_u and not is_invalid_site_url(clean_u):
                        corpus_blocks.append((str(item["name"]).lower(), clean_u))
        except Exception:
            pass

    if not corpus_blocks:
        return
    added_summary = []
    for grp in seeds.get("tvbox_vod_groups", []):
        kws = [k.lower() for k in grp.get("keywords", [grp["name"]])]
        existing_set = set(grp.get("urls", []))
        fresh = [
            u
            for lbl, u in corpus_blocks
            if any(k in lbl for k in kws) and u not in existing_set
        ]
        fresh = list(dict.fromkeys(fresh))
        if fresh:
            grp["urls"] = fresh[:3] + grp.get("urls", [])
            added_summary.append(f"{grp['name']}(+{len(fresh[:3])}条)")
    if added_summary:
        print(f"定向动态寻址完成：已为 {', '.join(added_summary)} 补充最新候选")


def discover_new_upstream_sources(registry, seeds, settings, dead_hosts, extra_texts=None):
    max_disc = int(settings.get("registry", {}).get("max_new_discoveries_per_run", 15))
    purged_set = set(registry.get("purged_seeds", []))
    existing_live = {x["url"] for x in registry.get("live_sources", [])}
    existing_vod = {x["url"] for x in registry.get("vod_sources", [])}
    existing_cms = {x["api"].rstrip("/") for x in registry.get("cms_apis", [])}

    pages = list(dict.fromkeys(registry.get("discovery_pages", [])))[:15]

    def _safe_fetch_discovery_page(page_u):
        try:
            return fetch_url_text(page_u, timeout=7)
        except Exception as e:
            print(f"发现页抓取跳过: {page_u} -> {e}")
            return ""

    fetched_pages = run_parallel(_safe_fetch_discovery_page, pages, workers=10)
    corpus_texts = list(extra_texts or []) + [txt for txt in fetched_pages if txt]

    new_live, new_vod, new_cms, discovered_multi = [], [], [], []
    url_pattern = re.compile(
        r"https?://[A-Za-z0-9.-]+/[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=-]+"
    )

    for text in corpus_texts:
        for raw_u in url_pattern.findall(text):
            u = sanitize_discovered_url(raw_u, dead_hosts)
            if (
                not u
                or u in purged_set
                or u.rstrip("/") in purged_set
                or is_invalid_site_url(u)
                or "hmz0123/hmz-auto-iptv-sources" in u
            ):
                continue
            lower_u = u.lower()
            if any(k in lower_u for k in ("dc.json", "dc.txt", "duocang", "多仓")):
                discovered_multi.append(u)
            elif lower_u.endswith((".m3u", ".m3u8")) and u not in existing_live:
                existing_live.add(u)
                new_live.append(u)
            elif (
                lower_u.endswith(".json")
                and any(
                    h in lower_u
                    for h in ("raw.githubusercontent.com", "gitee.com", "kstore.")
                )
                and u not in existing_vod
            ):
                existing_vod.add(u)
                new_vod.append(u)
            elif "/api.php/provide/vod" in lower_u:
                clean_api = u.split("?")[0].rstrip("/") + "/"
                if clean_api.rstrip("/") not in existing_cms and clean_api.rstrip("/") not in purged_set:
                    existing_cms.add(clean_api.rstrip("/"))
                    new_cms.append(clean_api)

    if extra_texts is None:
        pool = list(seeds.get("multi_repo_urls", [])) + (
            random.sample(discovered_multi, min(2, len(discovered_multi)))
            if discovered_multi
            else []
        )
        for mu in dict.fromkeys(pool):
            try:
                data = parse_json_lenient(fetch_url_text(mu, timeout=8, prefer_okhttp=True))
                entries = (
                    (data.get("urls") or data.get("订阅") or [])
                    if isinstance(data, dict)
                    else []
                )
                for it in entries:
                    if isinstance(it, dict):
                        sub_u = sanitize_discovered_url(it.get("url", ""), dead_hosts)
                        if (
                            sub_u
                            and sub_u not in purged_set
                            and not is_invalid_site_url(sub_u)
                            and "xhztv.top/xhz" not in sub_u
                            and sub_u not in existing_vod
                        ):
                            existing_vod.add(sub_u)
                            new_vod.append(sub_u)
            except Exception:
                pass

    return new_live[:max_disc], new_vod[:max_disc], new_cms[:max_disc]


def fetch_ziyuanzu_cms_candidates(settings):
    """拉取资源组高可用苹果CMS接口（支持本地快照容灾，全量版不做成人限制）"""
    cache_dir = settings.get("cache_dir", "cache")
    cache_file = os.path.join(cache_dir, "ziyuanzu_cms.json")
    raw_data, from_cache = None, False
    try:
        txt = fetch_url_text(settings.get("urls", {}).get("ziyuanzu_cms", ""), timeout=10)
        parsed = parse_json_lenient(txt)
        if parsed:
            raw_data = parsed
            os.makedirs(cache_dir, exist_ok=True)
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(parsed, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"资源组 CMS 接口拉取失败，读取本地缓存快照: {e}")

    if raw_data is None and os.path.exists(cache_file):
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                raw_data, from_cache = json.load(f), True
        except Exception:
            pass

    if not raw_data:
        return []
    entries = (
        raw_data
        if isinstance(raw_data, list)
        else (
            raw_data.get("interfaces")
            or raw_data.get("data")
            or raw_data.get("list")
            or raw_data.get("sites")
            or []
        )
    )
    candidates, seen_apis = [], set()
    for idx, item in enumerate(entries):
        if not isinstance(item, dict) or str(item.get("apiType", "json")).strip().lower() not in (
            "",
            "json",
        ):
            continue
        raw_api = str(item.get("api", "")).strip()
        if not raw_api.startswith(("http://", "https://")) or is_invalid_site_url(raw_api):
            continue
        clean_api = raw_api.split("?")[0].rstrip("/") + "/"
        if clean_api.rstrip("/") in seen_apis:
            continue
        total_res = int(item.get("totalResources", 0) or 0)
        if 0 < total_res < 500:
            continue
        seen_apis.add(clean_api.rstrip("/"))
        host_prefix = re.sub(
            r"[^a-zA-Z0-9]", "", urllib.parse.urlsplit(clean_api).netloc
        )[:14]
        cats = item.get("categories") or []
        candidates.append(
            {
                "key": f"zyz_{host_prefix or idx}",
                "name": str(item.get("name", "")).strip() or f"资源组_{idx + 1}",
                "api": clean_api,
                "categories": cats if isinstance(cats, list) else [str(cats)],
                "origin": "discovered",
                "fail_streak": 0,
                "latency": 999,
                "health_score": 65.0,
                "last_ok_at": "",
            }
        )
    print(
        f"资源组 CMS 接口加载完成（{'本地缓存快照' if from_cache else '在线实时'}）："
        f"有效候选 {len(candidates)} 个"
    )
    return candidates


def collect_vod_and_update_registry(
    registry, new_vod_urls, new_cms_apis, settings, seeds, rules
):
    now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cache_dir = settings.get("cache_dir", "cache")
    reg_cfg = settings.get("registry", {})
    max_seed_suspend = int(reg_cfg.get("max_seed_suspend_streak", 4))
    max_disc_fail = int(reg_cfg.get("max_discovered_fail_streak", 3))
    purged_set = set(registry.get("purged_seeds", []))
    junk_kws = rules.get("junk_keywords", [])
    pan_kws = rules.get("pan_keywords", [])

    vod_items = registry.get("vod_sources", [])
    existing_vod = {x["url"] for x in vod_items}
    for u in new_vod_urls:
        if u not in existing_vod and u not in purged_set:
            vod_items.append(
                {
                    "url": u,
                    "origin": "discovered",
                    "fail_streak": 0,
                    "latency": 999,
                    "valid_items": 0,
                    "yield_rate": 1.0,
                    "last_ok_at": "",
                }
            )

    cms_map = {
        x["api"].rstrip("/"): dict(x)
        for x in registry.get("cms_apis", [])
        if x.get("api")
    }
    for api_u in new_cms_apis:
        norm = api_u.rstrip("/")
        if norm not in purged_set and api_u not in purged_set:
            cms_map.setdefault(
                norm,
                {
                    "key": f"disc_{len(cms_map)}",
                    "name": f"发现站_{len(cms_map)}",
                    "api": api_u,
                    "origin": "discovered",
                    "fail_streak": 0,
                    "latency": 999,
                    "health_score": 60.0,
                    "last_ok_at": "",
                },
            )

    backup_map = {}
    canonical_ids = []
    for g in seeds.get("tvbox_vod_groups", []):
        if g.get("urls"):
            cid = canonical_cache_id(g.get("id") or g.get("name") or "src")
            canonical_ids.append(cid)
            backup_map[g["urls"][0]] = (cid, g.get("name", cid), g["urls"])

    _cleanup_alias_cache_files(cache_dir, canonical_ids)

    tvbox_base, extra_sites, all_parses, raw_texts, source_stats = {}, [], [], [], []
    seen_repo_fps = []

    vod_sorted = sorted(
        vod_items,
        key=lambda x: (x["url"] not in backup_map, x.get("origin") != "seed"),
    )
    for idx, src in enumerate(vod_sorted):
        if (
            src.get("origin") == "seed"
            and int(src.get("fail_streak", 0)) >= max_seed_suspend
            and idx % 2 == 1
        ):
            continue
        url = src["url"]
        is_prio = url in backup_map
        sid, sname, candidates = (
            backup_map[url]
            if is_prio
            else (
                re.sub(
                    r"[^a-zA-Z0-9]", "", urllib.parse.urlsplit(url).netloc
                )[:12]
                or "src",
                url.split("/")[-1],
                [url],
            )
        )

        fetched = None
        for c_idx, cand_u in enumerate(candidates):
            t0 = time.time()
            try:
                raw_txt = fetch_url_text(
                    cand_u,
                    timeout=9,
                    prefer_okhttp=("raw.githubusercontent.com" not in cand_u),
                )
                lat = round((time.time() - t0) * 1000)
                data = parse_json_lenient(raw_txt)
                if (
                    isinstance(data, dict)
                    and isinstance(data.get("sites"), list)
                    and len(data["sites"]) >= 3
                ):
                    if is_prio:
                        os.makedirs(cache_dir, exist_ok=True)
                        with open(
                            os.path.join(cache_dir, f"{sid}.json"), "w", encoding="utf-8"
                        ) as f:
                            json.dump(
                                {"used_url": cand_u, "data": data},
                                f,
                                ensure_ascii=False,
                                indent=2,
                            )
                    fetched = (cand_u, lat, raw_txt, data, False)
                    break
            except Exception:
                if c_idx < len(candidates) - 1:
                    time.sleep(0.25)

        if fetched is None and is_prio:
            snap_path = os.path.join(cache_dir, f"{sid}.json")
            if os.path.exists(snap_path):
                try:
                    with open(snap_path, "r", encoding="utf-8") as f:
                        snap = json.load(f)
                        if isinstance(snap.get("data", {}).get("sites"), list):
                            fetched = (snap.get("used_url", url), 650, "", snap["data"], True)
                except Exception:
                    pass

        if fetched is None:
            src["fail_streak"] = int(src.get("fail_streak", 0)) + 1
            src["valid_items"] = 0
            if is_prio:
                source_stats.append(
                    {
                        "id": sid,
                        "name": sname,
                        "ok": False,
                        "cached": False,
                        "fail_streak": src["fail_streak"],
                        "latency": 999,
                        "total": 0,
                        "pan": 0,
                        "priority": True,
                    }
                )
            continue

        used_url, lat, raw_text, data, from_cache = fetched
        sites = data.get("sites", [])
        cur_sigs = {
            f"{str(s.get('api', '')).strip().rstrip('/')}|"
            f"{str(s.get('ext', '')).strip().rstrip('/')[:64]}"
            for s in sites
            if isinstance(s, dict) and s.get("api")
        }
        if not is_prio and src.get("origin") != "seed" and cur_sigs:
            if any(
                len(cur_sigs & prev) / max(1, len(cur_sigs)) > 0.85 for prev in seen_repo_fps
            ):
                src["fail_streak"] = max_disc_fail
                continue
        if cur_sigs:
            seen_repo_fps.append(cur_sigs)

        src.update(
            {
                "fail_streak": (
                    int(src.get("fail_streak", 0)) + 1 if from_cache else 0
                ),
                "latency": lat,
                "valid_items": len(sites),
                "last_ok_at": now_str,
            }
        )
        if raw_text:
            raw_texts.append(raw_text)
        if not tvbox_base and data.get("wallpaper"):
            tvbox_base = data

        up_spider = resolve_relative_url(used_url, data.get("spider", ""))
        extracted_cnt, pan_cnt = 0, 0
        for s in sites:
            if not isinstance(s, dict) or is_junk_or_ad_site(s, junk_kws):
                continue
            sc = dict(s)
            sc["name"] = normalize_site_display_name(sc.get("name", "TVBox站"))
            if is_junk_or_ad_site(sc, junk_kws):
                continue
            api = str(sc.get("api", "")).strip()
            ext = sc.get("ext", "")
            if is_invalid_site_url(api) or is_invalid_site_url(ext):
                continue
            s_jar = resolve_relative_url(used_url, sc.get("jar", "")) or (
                up_spider if api.startswith("csp_") else ""
            )
            if s_jar:
                sc["jar"] = s_jar
            if isinstance(ext, str):
                sc["ext"] = resolve_relative_url(used_url, ext)
            sc.update(
                {
                    "_src_url": url,
                    "_src_id": sid,
                    "_src_latency": lat,
                    "_is_priority": is_prio,
                }
            )

            norm_api = api.rstrip("/")
            if (
                sc.get("type") in (0, 1)
                and api.startswith("http")
                and norm_api not in cms_map
                and norm_api not in purged_set
            ):
                cms_map[norm_api] = {
                    "key": sc.get("key") or f"t_{len(cms_map)}",
                    "name": sc["name"],
                    "api": api,
                    "origin": "discovered",
                    "fail_streak": 0,
                    "latency": 999,
                    "health_score": 60.0,
                    "last_ok_at": "",
                }
                extracted_cnt += 1
            elif not api.startswith(".") and "./" not in str(sc.get("ext", "")):
                extra_sites.append(sc)
                extracted_cnt += 1
                if is_pan_site(sc, pan_kws, junk_kws):
                    pan_cnt += 1

        src["yield_rate"] = round(extracted_cnt / max(1, len(sites)), 2)
        all_parses.extend(data.get("parses", []))
        if is_prio:
            source_stats.append(
                {
                    "id": sid,
                    "name": sname,
                    "ok": True,
                    "cached": from_cache,
                    "fail_streak": src["fail_streak"],
                    "latency": lat,
                    "total": extracted_cnt,
                    "pan": pan_cnt,
                    "priority": True,
                }
            )

    registry["vod_sources"] = vod_sorted
    return cms_map, tvbox_base, extra_sites, all_parses, raw_texts, source_stats


def _is_recently_healthy_cms(item, now_ts):
    """判断是否属于近 6 小时内已验证通过且高健康度（>=85）的稳定接口"""
    if int(item.get("fail_streak", 0)) > 0:
        return False
    if float(item.get("health_score", 0) or 0) < 85.0:
        return False
    if not item.get("fingerprint"):
        return False
    last_ok = str(item.get("last_ok_at", "")).strip()
    if not last_ok:
        return False
    try:
        dt = datetime.fromisoformat(last_ok.replace("Z", "+00:00"))
        return (now_ts - dt.timestamp()) < 21600
    except Exception:
        return False


def test_and_dedup_cms(cms_map, settings):
    """
    并发测速 CMS 接口 + 历史健康度（EMA）自适应加权 + 增量快速探活 + 指纹合并镜像站
    """
    th = settings.get("thresholds", {})
    timeout = int(th.get("timeout", 5))
    workers = int(th.get("cms_workers", th.get("max_workers", 24)))
    now_dt = datetime.now(timezone.utc)
    now_str = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    now_ts = now_dt.timestamp()

    def check_cms(site):
        sep = "&" if "?" in site["api"] else "?"
        test_url = f"{site['api']}{sep}ac=list&pg=1"
        is_fast_probe = _is_recently_healthy_cms(site, now_ts)
        probe_timeout = min(timeout, 3.5) if is_fast_probe else timeout
        attempts = 1 if is_fast_probe else 2

        for attempt in range(attempts):
            t0 = time.time()
            try:
                text = fetch_url_text(test_url, timeout=probe_timeout)
                lat = round((time.time() - t0) * 1000)
                stripped = text.strip()
                if stripped.startswith("{"):
                    data = parse_json_lenient(stripped)
                    if isinstance(data, dict) and (data.get("list") or data.get("class")):
                        classes = data.get("class") or []
                        live_cats = [
                            str(c.get("type_name", "")).strip()
                            for c in classes
                            if isinstance(c, dict) and c.get("type_name")
                        ]
                        if live_cats:
                            site["categories"] = list(
                                dict.fromkeys(list(site.get("categories") or []) + live_cats)
                            )
                        cls_sig = ",".join(
                            f"{c.get('type_id')}:{c.get('type_name', '')}"
                            for c in classes[:15]
                            if isinstance(c, dict)
                        )
                        titles = "|".join(
                            str(x.get("vod_name", ""))[:16]
                            for x in (data.get("list") or [])[:5]
                            if isinstance(x, dict)
                        )
                        site["latency"] = lat
                        site["fingerprint"] = hashlib.md5(
                            f"json|{cls_sig}|{titles}".encode("utf-8")
                        ).hexdigest()[:16]
                        return site
                elif "<rss" in stripped and "<list" in stripped:
                    site["latency"] = lat
                    site["fingerprint"] = hashlib.md5(
                        stripped[:2048].encode("utf-8")
                    ).hexdigest()[:16]
                    return site
            except Exception:
                if attempt < attempts - 1:
                    time.sleep(0.1)
        return None

    val_list = run_parallel(check_cms, [dict(x) for x in cms_map.values()], workers)
    valid_map = {x["api"].rstrip("/"): x for x in val_list}

    for norm_api, item in cms_map.items():
        prev_score = float(item.get("health_score", 75.0 if item.get("origin") == "seed" else 60.0))
        if norm_api in valid_map:
            cur_lat = int(valid_map[norm_api]["latency"])
            inst_score = max(55.0, min(100.0, 100.0 - (cur_lat / 35.0)))
            new_score = round(prev_score * 0.75 + inst_score * 0.25, 1)
            item.update(
                {
                    "fail_streak": 0,
                    "latency": cur_lat,
                    "health_score": new_score,
                    "fingerprint": valid_map[norm_api].get("fingerprint", item.get("fingerprint", "")),
                    "last_ok_at": now_str,
                }
            )
            valid_map[norm_api]["_health_score"] = new_score
            if valid_map[norm_api].get("categories"):
                item["categories"] = valid_map[norm_api]["categories"]
        else:
            new_score = round(max(0.0, prev_score * 0.80 - 8.0), 1)
            item["fail_streak"] = int(item.get("fail_streak", 0)) + 1
            item["health_score"] = new_score

    best_by_fp, passthrough = {}, []
    for s in val_list:
        fp = s.get("fingerprint")
        eff_score = s.get("latency", 888) - (s.get("_health_score", 60.0) - 60.0) * 3.0
        s["_eff_rank"] = eff_score
        if not fp:
            passthrough.append(s)
        elif fp not in best_by_fp or eff_score < best_by_fp[fp]["_eff_rank"]:
            best_by_fp[fp] = s

    deduped = sorted(
        list(best_by_fp.values()) + passthrough,
        key=lambda x: x.get("_eff_rank", x.get("latency", 888)),
    )
    print(
        f"CMS 测速与指纹去重完成：有效 {len(val_list)} 个 -> "
        f"合并镜像后保留 {len(deduped)} 个"
    )
    return deduped
