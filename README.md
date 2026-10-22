# astrbot_plugin_jmcomic

基于 [`jmcomic`](https://github.com/hect0x7/JMComic-Crawler-Python) 库的 AstrBot 插件：查询 JM 漫画信息、下载整本并合并为 PDF、随机推荐。

> [!WARNING]
> 本插件会访问成人向漫画站点，仅限 18 岁以上用户在遵守当地法律法规的前提下自行部署使用。请勿在未成年人可访问的群聊或公开场景中传播相关内容。

## 功能

| 指令 | 说明 |
| --- | --- |
| `/jm <JM号>` | 返回 JM 号、标题、封面、作者、标签、页数、简介 |
| `/jm 下载 <JM号>` | 下载整本，按章节顺序合并成一个 PDF 并发送文件 |
| `/jm 推荐` | 从“最新”分类中随机推荐一部，返回格式与第一条一致 |
| `/jm 帮助` | 显示帮助 |

也支持 `JM123456`、`jm-123456`、`/album/123456` 等写法；指令别名：`jmcomic`、`禁漫`。

示例：

```text
/jm 123456
/jm 下载 123456
/jm 推荐
```

## 安装

### 方式一：WebUI 上传（推荐）

1. 将本目录压缩为 `astrbot_plugin_jmcomic.zip`（压缩包内顶层目录必须是 `astrbot_plugin_jmcomic/`）。
2. 打开 AstrBot WebUI → 插件 → 安装插件 → 上传 zip。
3. 等待依赖安装完成后，重载插件。

### 方式二：手动放入插件目录

```bash
cd AstrBot/data/plugins
# 把本目录复制/解压为 astrbot_plugin_jmcomic
```

然后安装依赖：

```bash
# 进入 AstrBot 使用的 Python 环境
pip install -r data/plugins/astrbot_plugin_jmcomic/requirements.txt
```

重启 AstrBot，或在 WebUI 插件页重载。

## 依赖

- AstrBot `>= 4.10, < 5`（在 4.29 的源码接口上开发）
- [`jmcomic`](https://pypi.org/project/jmcomic/) `>= 2.7.9, < 2.8`（使用其 async 客户端与 `download_album_async`）
- `img2pdf`：把图片无损嵌入 PDF
- `Pillow`：图片格式转换与尺寸处理

`jmcomic` 会自动带上 `curl-cffi`、`commonx`、`pycryptodome`、`PyYAML` 等依赖。

## 工作流程

1. `/jm <ID>`：用 `jmcomic` 的 async 客户端请求详情，下载封面到本地缓存，拼接文字 + 图片消息返回。
2. `/jm 下载 <ID>`：
   - 先查询详情，检查页数上限、屏蔽标签；
   - `download_album_async` 把全部章节下载到 `data/plugin_data/astrbot_plugin_jmcomic/downloads/`；
   - 按章节、页码顺序读取图片，统一转成 JPEG（WebP 原图会让 PDF 膨胀 4～5 倍），再用 `img2pdf` 合并；
   - 通过 `Comp.File` 发送 PDF，默认生成后删除原始图片；
   - 相同本子再次请求时优先发送缓存 PDF。
3. `/jm 推荐`：读取“最新”分类的前 N 页，随机抽一个 JM 号，再按第 1 条格式返回；命中屏蔽标签会重新抽取。

## 配置

配置文件为 `data/config/astrbot_plugin_jmcomic_config.json`，可在 WebUI 插件配置页可视化修改。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `max_concurrent_downloads` | `2` | 同时进行的下载任务数 |
| `download_timeout` | `600` | 单本下载超时（秒） |
| `pdf_timeout` | `300` | PDF 生成超时（秒） |
| `image_concurrency` | `8` | 单本内图片下载并发 |
| `proxy` | `""` | HTTP 代理，如 `http://127.0.0.1:7890` |
| `domain_list` | `[]` | 自定义 JM 域名；留空自动更新 |
| `max_album_pages` | `1000` | 单本页数上限，0 不限制 |
| `pdf_pages_per_file` | `0` | 每个 PDF 的页数，0 表示合并成一个 |
| `pdf_split_size_mb` | `0` | 按大小分卷；QQ 群文件建议 90 |
| `pdf_jpeg_quality` | `90` | PDF 内 JPEG 质量 |
| `cleanup_images` | `true` | 生成 PDF 后删除图片 |
| `cache_pdf` | `true` | 缓存已生成的 PDF |
| `cache_days` | `7` | 缓存保留天数，0 不清理 |
| `cover_enabled` | `true` | 是否发送封面 |
| `title_max_len` | `120` | 标题截断长度 |
| `description_max_len` | `300` | 简介截断长度，0 不截断 |
| `nsfw_warning` | `true` | 附加 18+ 提示 |
| `group_enabled` | `true` | 是否允许群聊使用 |
| `download_admin_only` | `false` | 仅管理员可下载 |
| `allowed_groups` | `[]` | 群白名单，空表示不限制 |
| `blocked_groups` | `[]` | 群黑名单 |
| `blocked_tags` | `["loli", ...]` | 命中标题/标签时拒绝处理 |
| `enforce_blocked_tags` | `true` | 是否启用标签过滤 |
| `download_cooldown` | `30` | 同一用户下载冷却（秒） |
| `debug_log` | `false` | 输出 jmcomic 调试日志 |
| `retry_times` | `3` | 网络重试次数 |
| `http_timeout` | `30` | 单次 HTTP 请求超时 |
| `detail_timeout` | `45` | 查询详情超时 |
| `max_random_page` | `20` | 随机推荐的最大页码 |
| `recommend_attempts` | `8` | 推荐重试次数 |

### 关于群文件发送（OneBot v11 / NapCat）

NapCat 本身**没有“允许目录”白名单**。`upload_group_file` / `upload_private_file` 会把 `file` 当成本机路径或 URL 处理，能不能读取决于运行 NapCat 的进程/容器看到的文件系统。

- **同一台机器，NapCat 非容器**：直接用绝对路径即可，确保运行 NapCat 的用户对 PDF 有读权限。
- **NapCat 在 Docker**：把 AstrBot 的数据目录挂载进 NapCat，并且**容器内路径要与 AstrBot 内路径一致**。官方 AstrBot 模板已经给两个容器都挂了 `./data:/AstrBot/data`，所以插件生成的 `/AstrBot/data/plugin_data/astrbot_plugin_jmcomic/pdf/...` 在 NapCat 里能直接读。

  自己的 compose 给 napcat 加：

  ```yaml
  volumes:
    - ./data:/AstrBot/data
  ```

  只挂 PDF 子目录也可以，但容器内路径必须完全一致：

  ```yaml
  volumes:
    - ./data/plugin_data/astrbot_plugin_jmcomic/pdf:/AstrBot/data/plugin_data/astrbot_plugin_jmcomic/pdf:ro
  ```

- **AstrBot 在宿主机、NapCat 在容器**：把 AstrBot 的数据目录按 AstrBot 看到的绝对路径挂进 NapCat。例如 AstrBot 里路径是 `/opt/AstrBot/data/...`，NapCat 就要挂到 `/opt/AstrBot/data`。
- **AstrBot 和 NapCat 不在同一台机器**：本地路径不可行。给 AstrBot 配置 `callback_api_base`（NapCat 能访问的地址，比如 `http://astrbot:6185`），AstrBot 会把 PDF 注册成 HTTP 文件链接，NapCat 自己下载。
- **验证**：

  ```bash
  docker exec -it napcat ls -l /AstrBot/data/plugin_data/astrbot_plugin_jmcomic/pdf
  docker exec -it napcat head -c 5 /AstrBot/data/plugin_data/astrbot_plugin_jmcomic/pdf/<文件名>.pdf
  # 应显示 %PDF-
  ```

- **权限**：AstrBot 和 NapCat 的 UID/GID 不同时会读不到。设置 `NAPCAT_UID` / `NAPCAT_GID`，或执行 `chmod -R a+rX data/plugin_data/astrbot_plugin_jmcomic/pdf`。
- `enableLocalFile2Url` **不是**目录白名单，开启它也不会让容器外路径变得可读；无需为发送文件打开它。
- **大文件**：NapCat 的 `timeout.maxTimeout` 默认是 1800000ms；PDF 很大且上传慢时，可以增大 `timeout.uploadSpeedKBps` 或 `maxTimeout`。也可以把 `pdf_split_size_mb` 设为 90 自动分卷。

PDF 文件实际位于：

```text
AstrBot/data/plugin_data/astrbot_plugin_jmcomic/pdf/
```


## 常见问题

### 1. 请求失败 / 一直超时

JM 的域名经常变化，插件会通过 `jmcomic` 自动更新域名。若仍然失败：

- 在配置中填写可用的 `proxy`；
- 或把 `domain_list` 设置为当前可访问的域名（如 `18comic.vip`）；
- 把 `debug_log` 设为 `true`，查看具体请求错误。

### 2. 封面发送失败

封面来自图片 CDN，和 API 域名不是同一套。插件会随机尝试 3 个图片域名；全部失败时只返回文字信息。可以检查服务器到 `cdn-msp*.jmapiproxy*.cc` 等域名的连通性。

### 3. PDF 太大

- 把 `pdf_jpeg_quality` 降到 80～85；
- 把 `pdf_split_size_mb` 设为 90，让插件自动分卷；
- 或用 `pdf_pages_per_file` 按页数分卷。

### 4. 图片下载不完整

插件使用 `check_exception=False`，部分图片失败时仍会生成 PDF，并在消息里提示失败数量。若失败过多，通常是网络/域名问题，建议开启代理或降低 `image_concurrency`。

### 5. 插件加载失败，提示缺少 `jmcomic`

在 AstrBot 的 Python 环境中执行：

```bash
pip install -r data/plugins/astrbot_plugin_jmcomic/requirements.txt
```

然后重载插件。AstrBot 通常也会根据 `requirements.txt` 自动安装。

## 数据目录

```text
data/plugin_data/astrbot_plugin_jmcomic/
├── downloads/   # 下载原图（生成 PDF 后默认删除）
├── pdf/         # 生成的 PDF 缓存
├── covers/      # 封面缓存
└── tmp/         # PDF 生成临时目录
```

`cache_days` 到期后，插件会在启动时清理旧文件。

## 开发与测试

```bash
# 代码风格
ruff check .
ruff format --check .

# 单独测试 JM 库连通性
python -c "import jmcomic; print(jmcomic.__version__)"
```

建议在测试机器人或私聊中验证 `/jm 下载`，避免在公开群聊中直接发送成人内容。

## 许可与致谢

- 本插件代码：MIT License，见 [LICENSE](./LICENSE)。
- 核心抓取能力来自 [`hect0x7/JMComic-Crawler-Python`](https://github.com/hect0x7/JMComic-Crawler-Python)（`jmcomic`，MIT License）。本插件**不修改、不内置**其源码，仅通过公开 API 调用。
- 插件框架：[AstrBot](https://github.com/AstrBotDevs/AstrBot)（AGPL-3.0）。
- 本项目与 JM 站点、jmcomic 作者、AstrBot 官方均无隶属关系。

## 免责声明

本插件仅用于个人学习与技术研究。使用者应自行确认所在地区法律法规、站点服务条款以及内容版权；因使用本插件产生的任何后果由使用者自行承担。请勿将本插件用于传播违法或侵权内容。
