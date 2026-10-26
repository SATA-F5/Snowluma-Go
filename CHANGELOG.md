# 更新日志

本项目的版本号与 [NapCat_Go](https://github.com/SATA-F5/NC-Go) 保持同步，方便同版本对比测试。
当前为**内部 beta** 构建，`metadata.yaml` 版本固定为 `7.4.1`，未做正式发版递增。

## 1.0.0

首个 SnowLuma Go 插件

### 新增

- **一键安装 / 更新 SnowLuma**：解析 GitHub Releases，自动匹配 `win-x64` / `linux-x64` / `linux-arm64`，支持 `full` 与 `lite` 发行包，支持自定义镜像前缀与内置镜像回退，解压前做 zip/tar 完整性与体积校验，升级时只替换发行包内的顶层文件（保留 `config/`、`data/`、`logs/`）。
- **托管运行**：以子进程启动 `node index.mjs`，注入 `SNOWLUMA_WEBUI_PORT`、`SNOWLUMA_WEBUI_HOST`、`SNOWLUMA_HOOK_AUTOLOAD`、`SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD`、`SNOWLUMA_ACCEPT_EULA/PRIVACY` 等环境变量；实时解析日志中的 `listening`、端口顺延、初始密码与版本信息。
- **WebUI 会话自动化**：`POST /api/login` 获取 `X-SnowLuma-Token`；识别 `mustChangePassword` 并自动轮换强密码；识别 `needsTotp` 并给出明确提示；密码使用机器绑定密钥加密存储。
- **协议门禁**：识别 `403 consentRequired`，面板展示「我同意」（调用 `POST /api/agreements/record-consent`）与「打开 WebUI」两个入口；默认不自动同意。
- **官方 API 接入 OneBot**：读取 AstrBot `cmd_config.json` 中的 aiocqhttp 监听信息，`GET /api/config/<uin>` → upsert `wsClients` → `POST /api/config/<uin>`，读取并展示 `saved / applied / online / errors`；同步前自动备份旧配置到 `backups/`，配置未变化时跳过写入。
- **QQ 进程管理**：注册表 / 常见路径 / PATH 探测 QQNT，按需启动（Linux 无桌面时尝试 Xvfb），列出 SnowLuma 可见进程，支持单个「挂载 / 卸载」与「全部挂载」；只关闭本插件启动的 QQ。
- **多账号**：账号列表、连接健康度、按 UIN 选择同步目标。
- **内嵌控制台**：状态总览、账号与进程、AstrBot 接入诊断、版本安装进度、实时日志（插件侧 / SnowLuma 服务端双来源）、设置弹窗，明暗主题跟随 AstrBot。
- **external 模式**：连接一个已在运行的 SnowLuma，只做登录与 OneBot 同步，不下载、不启动、不改文件。
- **`/snowluma` 管理指令**：管理员可快速查看运行、登录、账号与同步状态。
- **诊断与清理**：清理安装目录内的残留 node 进程（按可执行文件路径 / 工作目录匹配，不误伤其他 node 进程）、重新探测版本、检查上游更新。
- **配置优先级**：面板保存过的键 > AstrBot 插件配置页 > 内置默认值，并在 `config.json` 中记录 `__overrides__`。
- **性能**：Node / QQ 探测结果带短缓存，避免面板轮询时反复拉起 `node --version` 与进程枚举；自动同步连续失败 3 次后停止重试并给出提示。

### 安全

- WebUI 密码、协议相关敏感值仅以密文落盘；接口返回时打码。
- 默认只连接 `127.0.0.1`，WebUI 监听地址固定为本机回环。
- 所有子进程均以参数列表方式启动，不使用 shell 拼接。
- 清理与停止逻辑区分「插件启动」与「用户启动」的进程。

### 已知限制

- SnowLuma 未提供官方校验和清单，插件只做压缩包完整性 / 结构校验，并在日志中输出实际 SHA256 供审计。
- 二次验证（TOTP）需要用户在面板输入验证码；不支持自动生成。
- 无官方 Docker 镜像，容器化场景请使用 `external` 模式。
- Linux 无桌面环境下 QQ 登录交互仍受 Xvfb 限制。
