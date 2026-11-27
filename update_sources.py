from datetime import datetime, timedelta, timezone
import json
import os
import re
import time
import urllib.error
import urllib.request

from core.builder import (
    GH_RAW,
    arrange_sites_smart_order,
    build_absolute_fallback_config,
    build_and_save_proxy_pools,
    build_douban_site,
    build_lite_config,
    build_pan_only_config,
    ensure_standard_refs,
    filter_and_sort_parses,
    rank_pan_spider_sites,
    strip_internal_meta,
    write_actions_summary,
)
from core.live import process_live_sources_and_build
from core.utils import (
    DEFAULT_DEAD_OR_JUNK_WORDS,
    HEADERS,
    append_cache_buster,
    idn_encode,
    is_pan_site,
    load_configs,
    normalize_site_display_name,
    run_parallel,
    sync_spider_jar,
    verify_and_mirror_assets,
)
from core.vod import (
    collect_vod_and_update_registry,
    discover_new_upstream_sources,
    fetch_ziyuanzu_cms_candidates,
    load_or_init_source_registry,
    save_source_registry,
    sync_famous_vod_urls_from_nav,
    test_and_dedup_cms,
)

CST_TZ = timezone(timedelta(hours=8))


def _parse_iso_ts(ts_str):
    if not ts_str:
        return None
    try:
        s = str(ts_str).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST_TZ)
        return dt.timestamp()
    except Exception:
        return None


def _ensure_raw_branch_url(url, repo_path, branch="main"):
    """
    兜底修正：确保 raw.githubusercontent.com/<owner>/<repo>/ 后包含分支名（如 /main/），
    避免代理转发时因缺少分支名导致 400/404。
    """
    if not url or not repo_path:
        return url
    prefix_marker = f"raw.githubusercontent.com/{repo_path}/"
    branch_marker = f"raw.githubusercontent.com/{repo_path}/{branch}/"
    if prefix_marker in url and branch_marker not in url:
        return url.replace(prefix_marker, branch_marker, 1)
    return url


def _prune_dead_discovery_pages(registry, settings):
    """
    预检 discovery_pages 可用性：
    - 返回 404/410 的非种子页直接拉黑入 purged_seeds；
    - 连续失败达到 max_discovered_fail_streak 的发现页自动剔除并拉黑，避免反复报错。
    """
    pages = registry.get("discovery_pages", [])
    if not isinstance(pages, list) or not pages:
        return []

    reg_cfg = settings.get("registry", {})
    max_fail = int(reg_cfg.get("max_discovered_fail_streak", 3))
    purged_set = set(registry.get("purged_seeds", []))
    fail_counts = registry.get("discovery_fail_counts")
    if not isinstance(fail_counts, dict):
        fail_counts = {}

    def _probe_page(page_u):
        u = str(page_u or "").strip()
        if not u or " " in u or u in purged_set or u.rstrip("/") in purged_set:
            return (u, "purged")
        target = idn_encode(u)
        need_get = False
        try:
            req = urllib.request.Request(target, headers=HEADERS, method="HEAD")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status < 400:
                    return (u, "ok")
                if resp.status in (404, 410):
                    return (u, "gone")
                return (u, "fail")
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return (u, "gone")
            if e.code in (403, 405):
                need_get = True
            else:
                return (u, "fail")
        except Exception:
            return (u, "fail")

        if need_get:
            try:
                req = urllib.request.Request(target, headers=HEADERS, method="GET")
                with urllib.request.urlopen(req, timeout=6) as resp:
                    if resp.status < 400:
                        return (u, "ok")
                    if resp.status in (404, 410):
                        return (u, "gone")
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    return (u, "gone")
            except Exception:
                pass
        return (u, "fail")

    kept, removed_logs = [], []
    for u, status in run_parallel(_probe_page, pages, workers=12):
        if not u or status == "purged":
            continue
        if status == "ok":
            fail_counts.pop(u, None)
            kept.append(u)
        elif status == "gone":
            fail_counts.pop(u, None)
            purged_set.add(u)
            purged_set.add(u.rstrip("/"))
            removed_logs.append(f"discovery_pages: {u} (HTTP 404/410 已自动拉黑)")
        else:
            cnt = int(fail_counts.get(u, 0) or 0) + 1
            if cnt >= max_fail:
                fail_counts.pop(u, None)
                purged_set.add(u)
                purged_set.add(u.rstrip("/"))
                removed_logs.append(f"discovery_pages: {u} (连续失败{cnt}次已自动拉黑)")
            else:
                fail_counts[u] = cnt
                kept.append(u)

    active_set = set(kept)
    registry["discovery_fail_counts"] = {
        k: v for k, v in fail_counts.items() if k in active_set
    }
    registry["discovery_pages"] = kept
    registry["purged_seeds"] = sorted(purged_set)
    if removed_logs:
        print(f"自动清理失效发现页 {len(removed_logs)} 条:")
        for line in removed_logs:
            print(f"  - {line}")
    return removed_logs


def _purge_dead_seeds_and_update_tracker(registry, settings, now_iso):
    """
    维护每个源的 first_fail_at 连续失败起点：
    - 若 fail_streak == 0，清空 first_fail_at；
    - 若 fail_streak > 0 且无 first_fail_at，记录当前时间；
    - 若 fail_streak >= 15 且首次失败距今 >= 15 天，彻底物理删除，
      并把 URL 写入 registry['purged_seeds'] 防止下次从 seeds.json 回流。
    """
    reg_cfg = settings.get("registry", {})
    max_streak = int(reg_cfg.get("purge_seed_fail_streak", 15))
    max_days = float(reg_cfg.get("purge_seed_fail_days", 15))
    now_ts = time.time()
    purged_set = set(registry.get("purged_seeds", []))
    purged_items = []

    for bucket in ("live_sources", "vod_sources", "cms_apis"):
        items = registry.get(bucket, [])
        if not isinstance(items, list):
            continue
        kept = []
        for it in items:
            if not isinstance(it, dict):
                continue
            u = str(it.get("url") or it.get("api") or "").strip()
            if not u or " " in u or u in purged_set or u.rstrip("/") in purged_set:
                continue
            streak = int(it.get("fail_streak", 0) or 0)
            if streak <= 0:
                it.pop("first_fail_at", None)
                kept.append(it)
                continue

            if not it.get("first_fail_at"):
                it["first_fail_at"] = now_iso
            first_ts = _parse_iso_ts(it.get("first_fail_at")) or now_ts
            fail_days = (now_ts - first_ts) / 86400.0

            if streak >= max_streak and fail_days >= max_days:
                purged_set.add(u)
                purged_set.add(u.rstrip("/"))
                purged_items.append(f"{bucket}: {u} (连续失败{streak}次/{fail_days:.1f}天)")
                continue
            kept.append(it)
        registry[bucket] = kept

    registry["purged_seeds"] = sorted(purged_set)
    if purged_items:
        print(f"自动物理清除长期失效源 {len(purged_items)} 条:")
        for line in purged_items:
            print(f"  - {line}")
    return purged_items


def _filter_purged_seeds(seeds, registry):
    """过滤掉已打入 purged_seeds 黑名单或含空格的脏种子链接"""
    purged = set(registry.get("purged_seeds", []))
    cleaned = dict(seeds)
    for list_key in ("discovery_pages", "live_source_urls", "extra_vod_urls"):
        if isinstance(cleaned.get(list_key), list):
            cleaned[list_key] = [
                u.strip()
                for u in cleaned[list_key]
                if isinstance(u, str)
                and u.strip()
                and " " not in u.strip()
                and u.strip() not in purged
                and u.strip().rstrip("/") not in purged
            ]
    if isinstance(cleaned.get("tvbox_vod_groups"), list):
        new_groups = []
        for g in cleaned["tvbox_vod_groups"]:
            gc = dict(g)
            gc["urls"] = [
                u.strip()
                for u in gc.get("urls", [])
                if isinstance(u, str)
                and u.strip()
                and " " not in u.strip()
                and u.strip() not in purged
                and u.strip().rstrip("/") not in purged
            ]
            if gc["urls"]:
                new_groups.append(gc)
        cleaned["tvbox_vod_groups"] = new_groups
    return cleaned


def _snapshot_prev_counts(settings, pan_keywords, invalid_keywords):
    """在覆盖写出新文件前，快照上一版成品与源仓数量以便简报对比增减"""
    prev = {}
    try:
        if os.path.exists("tvbox.json"):
            with open("tvbox.json", "r", encoding="utf-8") as f:
                sites = json.load(f).get("sites", [])
                if isinstance(sites, list):
                    prev["full"] = len(sites)
                    prev["pan_in_full"] = sum(
                        1 for s in sites if is_pan_site(s, pan_keywords, invalid_keywords)
                    )
    except Exception:
        pass

    for key, fname in (("lite", "tvbox_lite.json"), ("pan_only", "tvbox_pan.json")):
        try:
            if os.path.exists(fname):
                with open(fname, "r", encoding="utf-8") as f:
                    sites = json.load(f).get("sites", [])
                    if isinstance(sites, list):
                        prev[key] = len(sites)
        except Exception:
            pass

    try:
        if os.path.exists("tvbox_live.txt"):
            with open("tvbox_live.txt", "r", encoding="utf-8") as f:
                prev["live"] = sum(
                    1
                    for ln in f
                    if "," in ln
                    and not ln.strip().endswith("#genre#")
                    and "://" in ln
                )
    except Exception:
        pass

    reg_file = settings.get("registry", {}).get("file", "source_registry.json")
    try:
        if os.path.exists(reg_file):
            with open(reg_file, "r", encoding="utf-8") as f:
                reg = json.load(f)
                prev["reg_live"] = len(reg.get("live_sources", []))
                prev["reg_vod"] = len(reg.get("vod_sources", []))
                prev["reg_cms"] = len(reg.get("cms_apis", []))
    except Exception:
        pass

    return prev


def main():
    settings, seeds, rules = load_configs()
    th = settings.get("thresholds", {})
    dead_hosts = tuple(rules.get("dead_proxy_hosts", []))
    pan_kws = rules.get("pan_keywords", [])
    invalid_kws = tuple(rules.get("junk_keywords", [])) + DEFAULT_DEAD_OR_JUNK_WORDS
    prev_counts = _snapshot_prev_counts(settings, pan_kws, invalid_kws)

    repo_owner_path = settings.get("repo_owner_path", "hmz0123/hmz-auto-iptv-sources").strip("/")
    repo_branch = settings.get("repo_branch", "main").strip("/")

    now_cst = datetime.now(CST_TZ)
    tag = now_cst.strftime("%Y-%m-%d %H:%M:%S 北京时间")
    now_iso = now_cst.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    version_tag = now_cst.strftime("%Y%m%d")
    alerts = []

    spider_md5 = sync_spider_jar(settings.get("urls", {}).get("upstream_spider", ""))
    sync_famous_vod_urls_from_nav(seeds, dead_hosts)

    registry = load_or_init_source_registry(settings, seeds, dead_hosts)
    _purge_dead_seeds_and_update_tracker(registry, settings, now_iso)
    _prune_dead_discovery_pages(registry, settings)
    seeds = _filter_purged_seeds(seeds, registry)

    new_live, new_vod, new_cms = discover_new_upstream_sources(
        registry, seeds, settings, dead_hosts
    )
    live_cnt = process_live_sources_and_build(registry, new_live, tag, settings)

    (
        cms_map,
        tv_base,
        ex_sites,
        ex_parses,
        vod_texts,
        source_stats,
    ) = collect_vod_and_update_registry(
        registry, new_vod, new_cms, settings, seeds, rules
    )
    _, _, more_cms = discover_new_upstream_sources(
        registry, seeds, settings, dead_hosts, extra_texts=vod_texts
    )
    purged_set = set(registry.get("purged_seeds", []))
    for api_u in more_cms:
        if api_u.rstrip("/") in purged_set or api_u in purged_set:
            continue
        cms_map.setdefault(
            api_u.rstrip("/"),
            {
                "key": f"disc_{len(cms_map)}",
                "name": f"发现站_{len(cms_map)}",
                "api": api_u,
                "origin": "discovered",
                "fail_streak": 0,
                "latency": 999,
                "last_ok_at": "",
            },
        )
    for zyz_item in fetch_ziyuanzu_cms_candidates(settings):
        if zyz_item["api"].rstrip("/") not in purged_set and zyz_item["api"] not in purged_set:
            cms_map.setdefault(zyz_item["api"].rstrip("/"), zyz_item)

    val_cms = test_and_dedup_cms(cms_map, settings)
    max_cms_sites = int(th.get("max_cms_sites", 90))
    if max_cms_sites > 0 and len(val_cms) > max_cms_sites:
        val_cms = val_cms[:max_cms_sites]

    registry["cms_apis"] = list(cms_map.values())
    purged_now = _purge_dead_seeds_and_update_tracker(registry, settings, now_iso)
    if purged_now:
        alerts.append(f"已自动彻底清除长期失效死链 {len(purged_now)} 条（连续失败≥15次且满15天）。")
    save_source_registry(registry, tag, settings)

    min_valid_cms = int(th.get("min_valid_cms_sites", 5))
    min_full_sites = int(th.get("min_quality_gate_full_sites", 30))
    min_pan_sites = int(th.get("min_quality_gate_pan_sites", 3))
    max_extra_spider_full = int(th.get("max_full_extra_spider_sites", 250))

    jar_stats, quality_gate_triggered = (0, 0, 0), False
    if len(val_cms) < min_valid_cms and os.path.exists("tvbox.json"):
        quality_gate_triggered = True
        alerts.append(
            f"有效 CMS 接口仅 {len(val_cms)} 个，触发熔断保留上一版 tvbox.json。"
        )
        with open("tvbox.json", "r", encoding="utf-8") as f:
            final_tv_meta = ensure_standard_refs(
                json.load(f),
                settings,
                spider_md5=spider_md5,
                version_tag=version_tag,
            )
        final_tv = dict(final_tv_meta)
    else:
        douban = build_douban_site()
        cms_sites = [
            {
                "key": i["key"],
                "name": f"⚡{normalize_site_display_name(i['name'])}({i.get('latency', 888)}ms)",
                "type": 1,
                "api": i["api"],
                "searchable": 1,
                "quickSearch": 1,
                "filterable": 1,
                "_categories": i.get("categories", []),
            }
            for i in val_cms
        ]

        seen_keys, seen_sigs, base_sites = set(), set(), []

        def reg_site(s, allow_rename=True):
            k = str(s.get("key", "")).strip()
            ext_sig = (
                re.sub(r"^https?://", "", str(s.get("ext", "")).strip().lower())
                .rstrip("/")
            )
            sig = (s.get("type"), str(s.get("api", "")).strip().rstrip("/"), ext_sig)
            if not k or sig in seen_sigs:
                return False
            if k in seen_keys:
                if not allow_rename:
                    return False
                k = f"{s.get('_src_id', 'dup')}_{k}"
                if k in seen_keys:
                    return False
            s["key"] = k
            seen_keys.add(k)
            seen_sigs.add(sig)
            return True

        if reg_site(douban, allow_rename=False):
            base_sites.append(douban)
        for s in rank_pan_spider_sites(seeds.get("pan_spider_sites", []), verify_ext_top_n=10) + cms_sites:
            if reg_site(s, allow_rename=True):
                base_sites.append(s)

        ex_alive, jar_stats = verify_and_mirror_assets(
            ex_sites, settings, pan_kws
        )
        ex_alive.sort(
            key=lambda x: (
                not x.get("_is_priority", False),
                not str(x.get("jar", "")).startswith("./jars/"),
                int(x.get("_src_latency", 500) or 500),
            )
        )
        dedup_ex = [s for s in ex_alive if reg_site(s, allow_rename=True)]
        if max_extra_spider_full > 0 and len(dedup_ex) > max_extra_spider_full:
            dedup_ex = dedup_ex[:max_extra_spider_full]

        merged_raw = (
            base_sites
            + [s for s in dedup_ex if not is_pan_site(s, pan_kws, invalid_kws)]
            + [s for s in dedup_ex if is_pan_site(s, pan_kws, invalid_kws)]
        )
        merged = arrange_sites_smart_order(merged_raw, settings, rules)

        pan_cnt = sum(1 for s in merged if is_pan_site(s, pan_kws, invalid_kws))
        if (
            len(merged) < min_full_sites
            or pan_cnt < min_pan_sites
        ) and os.path.exists("tvbox.json"):
            quality_gate_triggered = True
            alerts.append(
                f"黄金门禁触发（全量 {len(merged)} / 网盘 {pan_cnt}），保留上一版配置。"
            )
            with open("tvbox.json", "r", encoding="utf-8") as f:
                final_tv_meta = ensure_standard_refs(
                    json.load(f),
                    settings,
                    spider_md5=spider_md5,
                    version_tag=version_tag,
                )
            final_tv = dict(final_tv_meta)
        else:
            final_tv_meta = ensure_standard_refs(
                {
                    "updated_at": tag,
                    "spider": "./custom_spider.jar",
                    "wallpaper": "https://eees.cc",
                    "lives": [],
                    "sites": merged,
                    "parses": filter_and_sort_parses(
                        ex_parses or tv_base.get("parses", [])
                    ),
                    "flags": tv_base.get("flags", []),
                    "ijk": tv_base.get("ijk", []),
                    "ads": tv_base.get("ads", []),
                },
                settings,
                spider_md5=spider_md5,
                version_tag=version_tag,
            )
            final_tv = {**final_tv_meta, "sites": strip_internal_meta(merged)}
            with open("tvbox.json", "w", encoding="utf-8") as f:
                json.dump(final_tv, f, ensure_ascii=False, indent=2)

    if not quality_gate_triggered or not os.path.exists("tvbox_lite.json"):
        lite_tv = build_lite_config(
            final_tv_meta, tag, settings, rules, spider_md5, version_tag=version_tag
        )
    else:
        with open("tvbox_lite.json", "r", encoding="utf-8") as f:
            lite_tv = json.load(f)

    if not quality_gate_triggered or not os.path.exists("tvbox_pan.json"):
        pan_tv = build_pan_only_config(
            final_tv_meta, tag, settings, rules, spider_md5, version_tag=version_tag
        )
    else:
        with open("tvbox_pan.json", "r", encoding="utf-8") as f:
            pan_tv = json.load(f)

    if not quality_gate_triggered:
        with open("tvbox_lite.json", "w", encoding="utf-8") as f:
            json.dump(lite_tv, f, ensure_ascii=False, indent=2)
        with open("tvbox_pan.json", "w", encoding="utf-8") as f:
            json.dump(pan_tv, f, ensure_ascii=False, indent=2)

    def check_cf(domain):
        d = domain.rstrip("/")
        host = d.replace("https://", "").replace("http://", "")
        t0 = time.time()
        try:
            with urllib.request.urlopen(
                urllib.request.Request(f"{d}/tvbox.json", headers=HEADERS), timeout=6
            ) as r:
                if r.status == 200 and b'"spider"' in r.read(512):
                    return {
                        "base": d,
                        "host": host,
                        "lat": round((time.time() - t0) * 1000),
                        "ok": True,
                    }
        except Exception:
            pass
        return {"base": d, "host": host, "lat": 999, "ok": False}

    cf_sorted = sorted(
        run_parallel(check_cf, settings.get("urls", {}).get("cf_domains", []), workers=4),
        key=lambda x: (not x["ok"], x["lat"]),
    )

    cf_healthy_entries, cf_failed_entries = [], []
    for i, c in enumerate(cf_sorted):
        if c["ok"]:
            cf_healthy_entries.append(
                {
                    "url": append_cache_buster(f"{c['base']}/tvbox_lite.json", version_tag),
                    "name": f"⚡CF精简{i+1} [{c['host']}] ({c['lat']}ms)",
                }
            )
            cf_healthy_entries.append(
                {
                    "url": append_cache_buster(f"{c['base']}/tvbox_pan.json", version_tag),
                    "name": f"💎CF网盘{i+1} [{c['host']}] ({c['lat']}ms)",
                }
            )
            cf_healthy_entries.append(
                {
                    "url": append_cache_buster(f"{c['base']}/tvbox.json", version_tag),
                    "name": f"👑CF全量{i+1} [{c['host']}] ({c['lat']}ms)",
                }
            )
        else:
            cf_failed_entries.append(
                {
                    "url": append_cache_buster(f"{c['base']}/tvbox.json", version_tag),
                    "name": f"👑CF冷备{i+1} [{c['host']}]",
                }
            )

    full_px, lite_px = build_and_save_proxy_pools(
        registry.get("discovery_pages", []), settings, seeds, dead_hosts=dead_hosts
    )

    for px_list in (full_px, lite_px):
        for p in px_list:
            for k in ("url", "lite_url", "pan_url"):
                if p.get(k):
                    p[k] = _ensure_raw_branch_url(p[k], repo_owner_path, repo_branch)

    proxy_entries = (
        [
            {
                "url": append_cache_buster(p["lite_url"], version_tag),
                "name": p["lite_name"],
            }
            for p in lite_px
            if p.get("lite_url")
        ]
        + [
            {
                "url": append_cache_buster(p["pan_url"], version_tag),
                "name": p["pan_name"],
            }
            for p in lite_px
            if p.get("pan_url")
        ]
        + [
            {
                "url": append_cache_buster(p["url"], version_tag),
                "name": p["name"],
            }
            for p in full_px
            if p.get("url")
        ]
    )

    fallback_entries = []
    if cf_sorted and cf_sorted[0]["ok"]:
        fastest_cf = cf_sorted[0]
        with open("tvbox_cf_abs.json", "w", encoding="utf-8") as f:
            json.dump(
                build_absolute_fallback_config(
                    lite_tv, tag, fastest_cf["base"], settings, spider_md5, version_tag
                ),
                f,
                ensure_ascii=False,
                indent=2,
            )
        fallback_entries.append(
            {
                "url": append_cache_buster(
                    f"{fastest_cf['base']}/tvbox_cf_abs.json", version_tag
                ),
                "name": f"🛡️CF应急绝对路径 [{fastest_cf['host']}] ({fastest_cf['lat']}ms)",
            }
        )

    proxy_pool_for_abs = lite_px or full_px
    for idx_abs, (fp, out_fname, label_prefix) in enumerate(
        zip(
            proxy_pool_for_abs[:2],
            ("tvbox_proxy_abs.json", "tvbox_proxy_abs2.json"),
            ("🛡️代理应急主线", "🛡️代理应急备线"),
        ),
        1,
    ):
        prefix = fp["prefix"] if fp["prefix"].endswith("/") else f"{fp['prefix']}/"
        raw_base = f"{prefix}{GH_RAW}/{repo_owner_path}/{repo_branch}"
        with open(out_fname, "w", encoding="utf-8") as f:
            json.dump(
                build_absolute_fallback_config(
                    lite_tv, tag, raw_base, settings, spider_md5, version_tag
                ),
                f,
                ensure_ascii=False,
                indent=2,
            )
        fallback_entries.append(
            {
                "url": append_cache_buster(
                    f"{raw_base}/{out_fname}", version_tag
                ),
                "name": f"{label_prefix}{idx_abs} [标{fp['discovery_order']}] ({fp['latency']}ms)",
            }
        )

    dc_urls = cf_healthy_entries + proxy_entries + fallback_entries + cf_failed_entries

    if dc_urls:
        with open("dc.json", "w", encoding="utf-8") as f:
            json.dump({"urls": dc_urls}, f, ensure_ascii=False, indent=2)

    pan_total = sum(
        1 for s in final_tv.get("sites", []) if is_pan_site(s, pan_kws, invalid_kws)
    )
    write_actions_summary(
        tag,
        len(final_tv["sites"]),
        len(lite_tv["sites"]),
        len(pan_tv["sites"]),
        pan_total,
        live_cnt,
        len(dc_urls),
        jar_stats,
        source_stats,
        full_px,
        lite_px,
        registry,
        alerts,
        prev_counts=prev_counts,
    )
    print(
        f"完成！全量 {len(final_tv['sites'])} 站 / 精简 {len(lite_tv['sites'])} 站 / "
        f"网盘 {len(pan_tv['sites'])} 站 / 直播 {live_cnt} 条"
    )


if __name__ == "__main__":
    main()
