# 更新日志

## 1.0.1

- 下载镜像改为与 NapCat_Go 完全一致的候选列表和尝试顺序，默认使用 `https://gh.zwy.one/`
- 配置弹窗新增自绘下载镜像下拉栏（不使用浏览器原生 `datalist` / `select`），支持内置镜像选择与自定义前缀
- 将「检查更新 / 清理残留 / 使用引导」等维护按钮移入配置弹窗，主界面仅保留启动 QQ、全部挂载、重启、刷新状态
- 修复引导高亮的定位与显示：不再依赖 `requestAnimationFrame`，顶部/底部目标会自动留出边距，卡片不再遮挡高亮区域
- 引导补齐所有状态卡片：版本、运行方式、QQ 客户端、Node 运行时、接入连接名、插件版本、安装目录、AstrBot 配置等
- 下载增强：镜像持续低速（如 3KB/s）时自动放弃并切换下一个源；使用 GitHub 资产大小显示真实进度，不再一直显示 0%；成功过的镜像会优先复用
- 打开 WebUI 改为 NapCat_Go 风格的自定义弹窗，集中显示地址和密码，支持显示 / 复制密码
- WebUI 默认监听 `0.0.0.0`（可在配置中改回 `127.0.0.1`），远程打开时地址自动适配当前访问 AstrBot 的主机名，不再错误指向 `127.0.0.1`
- QQ 进程识别会过滤 Electron 的 renderer / GPU / zygote 等子进程，只显示和挂载真正的主进程，避免看起来像一次启动了很多 QQ
- 启动 SnowLuma 时默认联动启动 QQ（已存在 QQ 主进程时不会重复启动）
- 新增「安装 / 更新完成后自动启动」选项，只在安装成功后触发；原有的插件加载自启动也只会在已安装时执行
- 修复旧 `config/webui.json` 与插件保存密码不一致导致的「密码错误」和反复启动：启动失败时会备份旧凭据、重新生成并重试一次
- SnowLuma 启动增加互斥锁，避免自动启动、安装后启动和手动点击同时拉起多个进程
- 配置区新增「清理 QQ 进程」：结束当前系统用户运行的 QQ 主进程和 Electron 子进程；其他用户的进程会提示需要 sudo
- 新增「Linux QQ DISPLAY」配置，配合 Xvfb / VNC 让无桌面 Ubuntu 服务器也能看到 QQ 二维码并完成登录；README 增加操作步骤
- 新增 WebUI「QQ 登录」弹窗：自动启动 Xvfb / x11vnc 并截取 QQ 登录二维码，可直接在浏览器里扫码
- 新增加密保存的 sudo 密码，以及「修复 QQ 权限」「安装 Linux 依赖」按钮（chown / ptrace_scope / apt）
- 清理 QQ 进程支持通过 sudo 结束其他系统用户启动的旧 QQ
- 修复异步路径中的同步阻塞端口探测：状态、重连、自动流程和部署方式解析改用 `asyncio` / `to_thread`，避免卡住 AstrBot 事件循环
- 安装 Linux 依赖后立即启动虚拟显示并探测 VNC；已有 Xvfb 时也会补启动 x11vnc，VNC 不可用时会明确提示
- DISPLAY 配置兼容 `99` / `:99` 两种写法，避免误判为远程 X11 目标而不启动 Xvfb / VNC
- Xvfb / x11vnc 启动后会检测进程状态并记录 `xvfb.log` / `x11vnc.log`；清理陈旧 socket，截图失败时区分“缺工具”和“DISPLAY 不可用”
- Xvfb 的 `:99` 锁文件被其他用户占用时自动改用 `:100` / `:101`，不再反复报 `Could not create server lock file`
- 修复重复启动 Xvfb 的问题：复用当前进程已启动的显示，不再因为 `:99` 锁失败而反复拉起 `:101` 并报 `Server is already active`
- 启动 QQ 时检测 `kernel.yama.ptrace_scope`，非 0 时提示使用「修复 QQ 权限」
- 「清理残留」现在同时清理本插件使用的 Xvfb / x11vnc 进程，解决旧的 `:101` 显示锁或残留显示服务无法复用的问题
- 不再回退到不可见的 `xvfb-run` 启动 QQ；二维码截图前会检查虚拟显示里是否真的有 QQ 窗口，避免显示纯黑截图
- 增加黑屏检测和「重启 QQ 并刷新」按钮，自动把仍运行在旧 DISPLAY 的 QQ 移到当前虚拟显示
- 强制 QQ 使用 X11（`QT_QPA_PLATFORM=xcb`、清理 `WAYLAND_DISPLAY`），并记录 openbox / xwininfo 状态，修复 Qt 窗口不显示导致的黑屏
- Linux 下启动 QQ 增加 `--no-sandbox --disable-gpu --disable-software-rasterizer --disable-gpu-compositing --disable-dev-shm-usage --ozone-platform=x11`，并补齐 XDG_RUNTIME_DIR / 软件渲染环境，避免进程存在但不映射窗口
- 依赖检查现在要求必须安装 fluxbox；只回退 openbox 时日志会明确警告，避免“看起来启动成功但二维码一直黑屏”
- 对齐 SnowLuma 官方无头部署：优先使用 fluxbox、Xvfb 1920x1080x16，安装 dbus-x11 / libcap2-bin
- 「修复 QQ 权限」会额外 `setcap cap_sys_ptrace=ep` 到 SnowLuma 的 node，兼容 Yama / 容器环境
- 配置 sudo 密码时，启动 QQ 会自动修正 `ptrace_scope`；窗口检查失败会打印当前 X11 窗口树，方便定位黑屏
- 修复服务进程 `PATH` 精简导致 `which fluxbox` 找不到 `/usr/bin/fluxbox`、从而一直回退 openbox 的问题：改为同时搜索 `/usr/bin` 等标准目录，并在日志里打印实际使用的窗口管理器路径
- 启动虚拟显示前自动结束占用同一 DISPLAY 的旧窗口管理器，避免 openbox 与 fluxbox 争夺显示导致窗口不映射
- 窗口管理器异常退出时记录 `window-manager.log` 并自动回退；QQ 启动后主动检查是否已映射窗口
- 为无头 QQ 创建独立 D-Bus 会话（`dbus-x11`），并设置 `NO_AT_BRIDGE=1`，避免 Electron 在无会话总线环境下不显示登录窗口

## 1.0.0

首个正式版本：

- 一键安装 / 更新 SnowLuma（full / lite 发行包、镜像回退、下载校验）
- 托管启动、停止、重启，并实时收集运行日志
- 自动登录 SnowLuma WebUI，自动完成初始密码轮换与协议确认
- 通过官方 WebUI API 写入 OneBot v11 反向 WS 连接并热重载
- 自动发现 / 启动 QQ，挂载或卸载 QQ 进程
- 支持多账号、external 模式、内嵌控制台页面与 `/snowluma` 指令
- 控制台与 NapCat_Go 同款主题：大按钮、状态卡片、实时日志、配置弹窗
- 开屏高亮引导（欢迎页 → 前置步骤 → 界面导览 → 重点提醒），可随时用「使用引导」重看，完成状态持久化
- WebUI 密码显示 / 复制 / 一键重置；「在新页面打开」使用原生链接，并提供复制地址、复制密码，适配无图形界面的 Ubuntu 服务器
- 内置同协议插件页桥接，避免 AstrBot 的 bridge-sdk 在 iframe 内加载失败时页面不可用
