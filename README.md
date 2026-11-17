# 📺 自动测速视频源仓库 (LibreTV & TVBox 双端版)

本项目通过 **GitHub Actions** 每天自动从网络搜集公开的电视直播源与影视点播接口，进行多线程连通性测速与去重排序，自动生成适配 **LibreTV** 与 **TVBox** 的订阅文件。

---

## 🔗 订阅地址一览（直接复制填入软件）

#####################################
> [💡 **国内网络提示**：]
> 如果电视或播放器直连 `://githubusercontent.com` 较慢，可在下方链接前加上加速前缀（如 'https://gh-proxy.com'、 `https://ghfast.top` 或 `https://ghproxy.com`）。或者(一、 jsDelivr 全球 CDN 加速链接（主力推荐，国内访问最稳、秒开）

适合直接填入 TVBox、影视仓、OK影视、LibreTV 等客户端：
https://jsdelivr.net

二、 gh-proxy 实时镜像加速链接（无长缓存，更新后几分钟即同步）

如果刚运行完 Actions 更新、希望电视端立刻拉取最新内容而不受 CDN 缓存延迟影响，可以使用这组链接：
https://gh-proxy.com

三、 Fastly 备用 CDN 加速链接（当上述线路偶尔维护时备用）
https://jsdelivr.net   
######################################
>
* **TVBox 完整版配置接口（直播 + 点播二合一）**
  ```text
  https://github.com/hmz0123/hmz-auto-iptv-sources/blob/main/tvbox.json
  ```

* **TVBox 纯直播源接口**
  ```text
  https://github.com/hmz0123/hmz-auto-iptv-sources/blob/main/tvbox_live.txt
  ```

* **LibreTV 点播源订阅（新版 v2 格式）**
  ```text
  https://github.com/hmz0123/hmz-auto-iptv-sources/blob/main/libre_vod.json
  ```

* **LibreTV / 通用播放器直播源订阅（M3U 格式）**
  ```text
  https://github.com/hmz0123/hmz-auto-iptv-sources/blob/main/libre_tv.m3u
  ```

---

## 📂 仓库各文件中文说明

### 1. 自动生成的订阅源（每天自动更新）

| 文件名 | 格式 | 适用软件 | 内容说明 |
| :--- | :--- | :--- | :--- |
| **`tvbox.json`** | JSON | TVBox、影视仓、OK影视等 | **主力推荐**。整合了测速后的点播站点、爬虫接口、解析线路以及直播源引用。 |
| **`tvbox_live.txt`** | TXT | TVBox 及各类电视直播壳 | 按频道分组整理好的电视直播源（`频道名,链接` 格式），按测速延迟从低到高排列。 |
| **`libre_vod.json`** | JSON | LibreTV（网页/客户端版） | 适配新版 LibreTV 数据源订阅标准的影视点播接口列表，已自动剔除失效接口并标注响应延迟。 |
| **`libre_tv.m3u`** | M3U | LibreTV、Kodi、PotPlayer、VLC | 标准 M3U 格式电视直播源，保留完整频道分组信息。 |

### 2. 核心脚本与自动化配置

| 文件路径 | 作用说明 |
| :--- | :--- |
| **`update_sources.py`** | **核心抓取与测速脚本**。负责拉取上游直播与点播源、清理 JSON 注释、去重、并发测速并分别输出双端配置文件。 |
| **`.github/workflows/daily_update.yml`** | **GitHub Actions 定时工作流配置**。控制任务每天自动运行的时间（默认北京时间凌晨 04:00）以及自动提交推送更新后的源文件。 |
| **`README.md`** | 仓库说明文档，记录各文件用途及订阅链接。 |

---

## ⚙️ 使用与维护说明

  所有数据均来自于网络，数据源只为自己技术测试使用，严禁任何人违法使用。任何人自行使用后造成的后果，请使用人自行承担，本人该不负责。
* **自动更新频率**：每天北京时间 **04:00**（UTC 20:00）自动执行一次抓取与测速。
* **手动立即更新**：进入仓库顶部的 **Actions** 页面 -> 点击左侧 **Daily IPTV Source Update & Speedtest** -> 点击右侧 **Run workflow** 即可立即触发更新。
* **自定义上游源**：如需增减直播或点播源，直接编辑 `update_sources.py` 顶部的 `LIVE_SOURCE_URLS`、`TVBOX_VOD_URLS` 或 `CMS_API_LIST` 列表即可。
