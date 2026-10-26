# SnowLuma Go

> 把 [SnowLuma](https://github.com/SnowLuma/SnowLuma) 一键装好、跑起来，并自动接进 AstrBot —— 全程通过 SnowLuma **官方 WebUI API** 完成，不再手改 `onebot11_*.json`，也不需要重启进程。

<p align="left">
  <img alt="AstrBot" src="https://img.shields.io/badge/AstrBot-%3E%3D4.24.1-5b8cff" />
  <img alt="Platform" src="https://img.shields.io/badge/platform-Windows%20%7C%20Linux-8b5cf6" />
  <img alt="OneBot" src="https://img.shields.io/badge/protocol-OneBot%20v11-34d399" />
  <img alt="License" src="https://img.shields.io/badge/license-AGPL--3.0-fb7185" />
</p>

SnowLuma Go 是 [NapCat_Go](https://github.com/SATA-F5/NC-Go) 的姊妹插件：保留「一键部署 + 自动接入」的完整操作体验，但把实现换成 SnowLuma 官方推荐的集成路径。所有网络配置都通过 REST API 校验、落盘并**热重载**，返回 `saved / applied / online` 三态，插件会把结果原样告诉你。

> [!NOTE]
> 当前仓库是**内部 beta 构建**：`metadata.yaml` 的插件版本沿用 `7.4.1`，方便与 NapCat_Go 同版本号对比测试；功能与文档会继续迭代，正式发版时再统一升版。

---

## ✨ 功能

| 能力 | 说明 |
| --- | --- |
| 📦 一键安装 / 更新 | 从 SnowLuma 官方 GitHub Releases 解析版本，自动匹配 `win-x64` / `linux-x64` / `linux-arm64`，支持 `full`（自带 Node）与 `lite`（依赖系统 Node）两种发行包 |
| 🚀 托管进程 | 以子进程方式启动 `node index.mjs`，注入 WebUI 端口 / 监听地址 / Hook 自动挂载等环境变量，实时收集 stdout 日志 |
| 🔐 自动登录 WebUI | 首次启动用 `SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD` 播种密码；检测到 `mustChangePassword` 时自动轮换为强密码；密码以机器绑定密钥加密存储 |
| 🔄 官方 API 接入 | `GET/POST /api/config/<uin>` 读取、校验、写入、热重载 `wsClients`，无需重启，也不再猜配置文件结构 |
| 🧩 QQ 进程管理 | 自动发现 QQNT（注册表 / 常见路径 / PATH），按需启动 QQ，列出 SnowLuma 可见进程并逐个「挂载 / 卸载」，也可一键全部挂载 |
| 👥 多账号 | 只要 SnowLuma 里有多个在线 UIN，就能分别查看连接状态；同步目标按 UIN 自动选择 |
| 🖥 内嵌控制台 | 状态总览、账号与进程、AstrBot 接入诊断、版本安装与进度、实时日志、设置面板，支持明暗主题 |
| 🌐 external 模式 | 已经有一个跑着的 SnowLuma？只填地址和密码，插件只做「接入 + 同步」，不碰你的进程和文件 |
| 🛡 安全默认 | 仅连接 `127.0.0.1`、密码加密落盘、只关闭本插件启动的 QQ、不会自动同意任何协议（除非显式开启） |

### 与 NapCat_Go 的差异

| 项目 | NapCat_Go | SnowLuma Go |
| --- | --- | --- |
| 写配置方式 | 直接改写 `onebot11_<uin>.json`，再重启 NapCat | 调用 `POST /api/config/<uin>`，由 SnowLuma 校验并热重载 |
| 生效判断 | 只能看日志 / 重连 | 读取 API 的 `applied`、`online`、`errors` |
| 启动注入 | 改写 QQ 的 `package.json`（`loadNapCat.js`） | 通过 SnowLuma Hook API `POST /api/processes/<pid>/load`，或 `SNOWLUMA_HOOK_AUTOLOAD=1` |
| 进程清理 | 按名字批量结束 NapCat / QQ | 只结束本插件启动的 QQ；SnowLuma 残留用命令行路径匹配 |
| 部署形态 | Shell / Docker | 官方发行包（full / lite）+ external 模式 |
| 版本管理 | GitHub Release 直链 | Release API + 资产名校验 + 多镜像回退 |

---

## 🧠 工作原理

```
┌──────────────────────────── AstrBot ────────────────────────────┐
│  aiocqhttp 平台（反向 WS 监听 0.0.0.0:6199 /ws/）                 │
│        ▲                                     ▲                  │
│        │ ws:// AstrBot                       │ 插件页面 bridge    │
│        │                                     │                  │
│  ┌─────┴─────────────┐        ┌──────────────┴───────────────┐   │
│  │ SnowLuma 进程      │◄──────►│ SnowLuma Go 插件              │   │
│  │ (node index.mjs)  │  REST  │ · 安装/启动/日志              │   │
│  │ · WebUI :5099     │  API   │ · 登录/协议/密码              │   │
│  │ · OneBot v11      │        │ · 写 wsClients + 热重载       │   │
│  └─────┬─────────────┘        └──────────────────────────────┘   │
│        │ Hook（native）                                           │
│  ┌─────▼─────┐                                                    │
│  │  QQNT     │  扫码 / 快速登录后，SnowLuma 才能上报账号          │
│  └───────────┘                                                    │
└──────────────────────────────────────────────────────────────────┘
```

插件只做三件事：

1. **把 SnowLuma 变成可控进程**：解析版本 → 下载 → 校验 → 解压 → 启动 → 等 WebUI 就绪。
2. **把 WebUI 会话变成可编程接口**：`POST /api/login` 拿 `X-SnowLuma-Token`，处理初始改密与协议同意门禁。
3. **把 AstrBot 的监听地址写进 SnowLuma**：读取 `cmd_config.json` 的 aiocqhttp 配置 → 组装 `wsClients` → `POST /api/config/<uin>` → 报告热重载结果。

### 用到的官方接口

| 接口 | 用途 |
| --- | --- |
| `POST /api/login` | 密码（可选 TOTP）登录，返回会话 token |
| `GET /api/auth/state`、`POST /api/auth/change-password` | 初始密码轮换 |
| `GET /api/agreements`、`POST /api/agreements/record-consent` | 协议门禁检测与用户显式同意 |
| `GET /api/qq-list`、`GET /api/connections` | 在线账号与 OneBot 适配器健康度 |
| `GET /api/processes`、`POST /api/processes/<pid>/load\|unload\|refresh` | QQ 进程发现与 Hook |
| `GET/POST /api/config/<uin>` | OneBot v11 网络配置读写 + 热重载 |
| `GET /api/logs` | 服务端日志（面板「服务端」日志源） |
| `GET /api/system`、`GET /api/update/check` | 主机信息与上游更新检查 |

> 认证头是 `X-SnowLuma-Token`（SnowLuma 刻意把 `Authorization` 留给反向代理）。

---

## 📋 前置要求

1. **AstrBot ≥ 4.24.1**，且已创建一个 **OneBot v11（aiocqhttp）** 机器人（反向 WS 端口，例如 6199）。
2. **QQNT 9.9.x**（Windows 安装版 / Linux `linuxqq`）。QQ 登录本身必须由你完成（扫码或快速登录）。
3. **SnowLuma 发行包要求**：
   - `full` 包自带 Node 运行时（解压后约 100 MB），开箱即用；
   - `lite` 包需要系统 Node.js 满足 `^24.22.1 || >=26.12.1`；
   - 插件启动前会检测 Node 版本并给出明确提示。

---

## 🚀 安装

### 通过插件市场 / 仓库

在 AstrBot 插件市场搜索 **Snowluma Go**，或把本仓库放进 `data/plugins/`：

```text
data/plugins/Snowluma-Go/
├── main.py
├── metadata.yaml
├── pages/snowluma_go/...
└── ...
```

重载插件后，打开 **插件 → Snowluma Go → SnowLuma 控制台**。

### 手动安装（开发用）

```bash
cd AstrBot/data/plugins
git clone https://github.com/SATA-F5/Snowluma-Go.git
# 重启 AstrBot 或在 WebUI 重载插件
```

---

## ⚡ 快速开始

1. **创建机器人**：AstrBot → 机器人 → 创建 → OneBot v11，端口填一个空闲端口（如 `6199`），保存。
2. **打开控制台**：插件 → Snowluma Go → 打开页面。
3. **安装 SnowLuma**：在「安装与更新」里选择 `latest` + `full`，点「安装 / 更新」。首次约 40 MB（full 包），进度与镜像会实时显示。
4. **启动并登录**：点「启动」。插件会拉起 SnowLuma、自动登录 WebUI、把日志打进面板。若 SnowLuma 要求同意协议，面板会出现「我同意」按钮。
5. **登录 QQ**：点「启动 QQ」→ 在弹出的 QQ 窗口扫码 / 快速登录；或在 SnowLuma WebUI 里按引导接入进程。账号上线后，插件会自动把 AstrBot 的反向 WS 写入 SnowLuma 并热重载。
6. 回到 AstrBot 机器人页面，确认 OneBot v11 平台显示在线。

> 想手动确认？在面板点「立即同步」，会显示 `已更新反向 WS 连接 → ws://127.0.0.1:6199/ws/（已热重载）` 之类的结果。

---

## ⚙️ 配置

配置同时支持 AstrBot 插件配置页（`_conf_schema.json`）与插件页面右上角「设置」弹窗；密码类字段始终加密落盘。

生效优先级：**在插件页面保存过的键 > AstrBot 插件配置页 > 内置默认值**。也就是说，面板里改过的项不会被 AstrBot 配置页覆盖，而未在面板保存过的项仍可继续从 AstrBot 配置页调整。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `auto_start` | `true` | 插件加载后自动启动 SnowLuma |
| `auto_sync` | `true` | 账号上线后自动同步 OneBot 连接 |
| `auto_hook` | `true` | 启动时设置 `SNOWLUMA_HOOK_AUTOLOAD=1`，自动挂载新发现的 QQ 进程 |
| `auto_start_qq` | `false` | 启动 SnowLuma 后自动拉起 QQ（登录仍需手动） |
| `deploy_mode` | `auto` | `auto` / `native`（本机托管）/ `external`（连接已有实例） |
| `snowluma_port` | `5099` | 本机 WebUI 端口，通过 `SNOWLUMA_WEBUI_PORT` 注入 |
| `snowluma_version` | `latest` | 默认安装版本 |
| `release_flavor` | `full` | `full` 自带 Node；`lite` 需系统 Node |
| `download_mirror` | `https://gh-proxy.com/` | 下载加速前缀；失败会自动尝试内置镜像列表 |
| `webui_password` | 空 | SnowLuma WebUI 密码；留空则自动生成强密码并加密保存 |
| `external_host` | `127.0.0.1` | external 模式下的 SnowLuma 地址 |
| `external_port` | `5099` | external 模式下的 WebUI 端口 |
| `qq_path` | 空 | QQ 可执行文件路径；留空自动探测 |
| `qq_number` | 空 | 指定同步目标 UIN；留空使用 SnowLuma 在线账号 |
| `astrbot_config_path` | 空 | `cmd_config.json` 路径；留空自动探测 |
| `ws_host_override` | 空 | 跨机部署时填写 SnowLuma 能访问到的 AstrBot 主机/IP |
| `auto_accept_agreements` | `false` | 启动时注入 `SNOWLUMA_ACCEPT_EULA=1` / `SNOWLUMA_ACCEPT_PRIVACY=1`；默认关闭，建议在页面手动同意 |
| `sync_connection_name` | `astrbot` | 写入 SnowLuma 的 `wsClients` 条目名 |

### 跨机部署

如果 SnowLuma 跑在另一台机器：

1. `deploy_mode = external`，填 `external_host` / `external_port` / `webui_password`；
2. `ws_host_override` 填 AstrBot 所在机器的 LAN / 公网地址（保证 SnowLuma 能连上）；
3. 确保 AstrBot 的 aiocqhttp 监听 `0.0.0.0` 且防火墙放行对应端口。

---

## 🧩 目录结构

```text
Snowluma-Go/
├── main.py                 # Star 入口、Web API 路由、/snowluma 指令
├── snowluma_manager.py     # 进程生命周期、WebUI 会话、状态聚合、自动流程
├── snowluma_api.py         # 官方 WebUI REST 客户端
├── config_syncer.py        # AstrBot aiocqhttp → SnowLuma wsClients
├── download.py             # Release 解析、镜像下载、校验、解压安装
├── qq_manager.py           # QQ 发现 / 启动 / Hook
├── utils.py                # 平台、加密、类型、进程工具
├── pages/snowluma_go/      # 内嵌控制台（index.html + bridge.js）
├── .astrbot-plugin/i18n/   # 页面标题等本地化
├── logo.png
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
└── README.md
```

插件数据目录（AstrBot `data/plugins_data/snowluma_go/` 或 `context.get_data_dir()` 对应位置）：

```text
config.json                 # 插件配置（密码为密文）
snowluma/                   # SnowLuma 安装目录
├── config/                 # SnowLuma 自己的配置（保留，不覆盖）
├── data/                   # 消息 / 媒体等运行数据（保留）
├── index.mjs / node(.exe) / native/ / client/
backups/                    # 同步前的 OneBot 配置快照
downloads/                  # 下载缓存
```

---

## 🩺 常见问题

**Q：启动后一直「未安装」？**
A：在「安装与更新」里执行一次安装；或把 SnowLuma 发行包解压到插件的 `snowluma/` 数据目录（需包含 `index.mjs`）。也可以切到 `external` 模式接管已有实例。

**Q：日志提示 Node 版本不兼容？**
A：SnowLuma 要求 `^24.22.1 || >=26.12.1`。改用 `full` 包（自带 Node），或升级系统 Node。

**Q：同步提示「请先同意 SnowLuma 用户协议 / 隐私政策」？**
A：SnowLuma 在首次使用时要求显式同意。面板会出现「我同意」按钮（会调用 `POST /api/agreements/record-consent`）；也可以点「打开 WebUI」手动确认。默认不会替你自动同意。

**Q：启动 QQ 后没有账号？**
A：QQ 需要完成登录（扫码 / 快速登录）；并且 SnowLuma 必须已把 Hook 注入该 QQ 进程。点「全部挂载」后等待几秒，账号会出现在面板和 `GET /api/qq-list` 里。

**Q：端口被占用？**
A：SnowLuma 会自动顺延端口并在日志里打印 `port ... is in use, using ... instead`；插件会读回真实端口。也可以改 `snowluma_port` 后重启。

**Q：能用 Docker 吗？**
A：SnowLuma 官方以发行包分发，没有官方镜像。推荐把 SnowLuma 跑在宿主机 / 另一台机器，再用 `external` 模式接入。

**Q：会杀掉我自己开的 QQ 吗？**
A：不会。插件只记录并关闭「由本插件启动」的 QQ 进程；「清理残留」也只匹配命令行里含插件安装目录的 node 进程。

**Q：Linux 无桌面？**
A：插件会在没有 `DISPLAY` 且存在 `xvfb-run` 时用 Xvfb 启动 QQ。Hook 仍然需要 QQ 进程能正常完成登录流程。

---

## 🛠 开发

```bash
# Python 3.10+
python -m compileall .
# 可选：对运行中的 SnowLuma 做接口自检
python tools/selfcheck.py --base http://127.0.0.1:5099 --password '******'
```

- SnowLuma 官方仓库：<https://github.com/SnowLuma/SnowLuma>
- OneBot v11 规范：<https://github.com/botuniverse/onebot-11>
- 上游 WebUI 路由实现：`packages/core/src/webui/server.ts`
- OneBot 配置模型：`packages/onebot/src/config.ts`

插件内部约定：

- 所有对 SnowLuma 的写操作都必须走官方 API；**不要**绕过 API 手改 `config/onebot_<uin>.json`。
- 新增路由统一注册在 `/{PLUGIN_NAME}/...`（`PLUGIN_NAME = snowluma_go`），页面通过 `apiGet` / `apiPost` 调用。
- 任何会结束进程的操作都必须区分「本插件启动」与「用户启动」。

---

## 📝 更新日志

见 [CHANGELOG.md](./CHANGELOG.md)。

---

## 📄 许可证与免责声明

本插件以 **AGPL-3.0** 发布，见 [LICENSE](./LICENSE)。

SnowLuma 是独立的第三方项目，与腾讯 / QQ 无隶属或授权关系；其源码可见但**非 OSI 开源**，商业使用与公开发布修改版需另行获得授权。本插件只调用其公开 WebUI API，不分发 SnowLuma 二进制，也不修改其源码。

使用本项目即表示你理解：QQ 账号自动化存在风控与合规风险，请遵守《QQ 用户协议》与当地法律；一切后果由使用者自行承担。
