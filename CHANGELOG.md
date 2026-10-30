# 更新日志

## 1.0.5

- 「QQ 登录」的实时操作改为 **HTTP 截图 + `xdotool`**：不再依赖 iframe / noVNC / WebSocket，在 AstrBot 的受限 iframe 内也能正常工作
  - 点击画面即发送鼠标点击；支持 Enter / Tab / Esc / Backspace 与文字输入
  - 约每秒刷新一次 JPEG 截图，不需要安装 novnc / websockify
- 「安装 Linux 依赖」改为安装 `xdotool`
- 修复 x11vnc 在 Wayland 会话环境下直接退出（`Wayland display server detected ... Exiting`）：启动 x11vnc 时清理 `WAYLAND_DISPLAY` 并设置 `XDG_SESSION_TYPE=x11`
- 修复旧窗口管理器占用 `:99` 导致 fluxbox 启动失败：不再因为服务环境里存在 `DISPLAY` 就跳过清理

## 1.0.4

- 新增 noVNC 实时画面：插件用 `websockify` 把 `:99` 的 x11vnc 转成网页，并在「QQ 登录」弹窗里用 iframe 嵌入，可直接点登录 / 切换扫码
- 新增 `novnc_port` 配置（默认 6081）；「安装 Linux 依赖」自动安装 `novnc` 与 `websockify`，清理残留时一并清理 websockify
- 「QQ 登录」弹窗新增「页面内操作 / 切回截图」切换；VNC 未设密码时仅本机可用，设置密码后局域网可用

## 1.0.3

- 启动流程（SnowLuma 启动 / 重启 / 安装后启动）与每次启动 QQ 前自动检测 NapCat 注入：当 `resources/app/package.json` 指向 `loadNapCat.js` 且 `napcat/napcat.mjs` 缺失时，自动恢复 QQ 原始入口（需 sudo），避免 QQ 不创建登录窗口
- 无法自动修复（未配置 sudo 密码）时，启动 QQ 会直接返回错误并说明原因，不再启动一个必然黑屏的进程
- 移除持久化的 sudo 密码配置项：改为每次需要时由 WebUI 弹窗索取，不再写入 `config.json` / AstrBot 配置
- sudo 密码在前端用后端下发的 RSA 公钥加密（PKCS#1 v1.5）后再发送，后端只在进程内用临时私钥解密，用完即弃，不落盘、不入日志
- 新增 `sudo/key`（公钥）与 `deps/status`（依赖检查）接口；安装依赖、修复权限、修复 QQ 登录界面、清理其他用户 QQ、启动/重启/登录 QQ 时都会按需弹出 sudo 输入框
- 启动 QQ 前检测 `~/.config/QQ` 是否可写：目录被 root 占用时 QQ 会以 `EACCES ... mkdir .../QQ/log` 退出，现在会向 WebUI 索取 sudo 密码并自动 `chown/chmod` 修复
- 启动前同时检查 `kernel.yama.ptrace_scope`：非 0 时会在启动 SnowLuma / QQ 前索取 sudo 密码，自动执行 chown、`ptrace_scope=0` 与 `setcap cap_sys_ptrace=ep`，避免 Hook 报 `COMPONENT_LOAD_FAILED`
- 启动 SnowLuma 前会清理同一安装目录下遗留的 node 进程，避免旧实例占着 5099 导致新实例改用 5100、同时存在两个 hook
- 状态区新增「QQ 启动环境需要修复」提示与「修复并启动 QQ」按钮；插件版本变化时页面会自动带版本参数重载，避免浏览器缓存旧脚本
- `bridge.js` / `style.css` / `extra.css` 引用加版本参数；sudo 弹窗缺失时回退到浏览器原生输入框
- 检测其他用户启动的 QQ 进程（常见是以前用 root 启动的旧实例）：状态区会提示，点击「修复并启动 QQ」输入 sudo 密码后会先结束这些进程再启动，避免 `COMPONENT_LOAD_FAILED`
- 「Linux QQ DISPLAY」默认值改为 `99`（等价 `:99`）并持久化保存：WebUI 不会再显示空值，配置页与插件配置保持一致
- x11vnc 不再使用 `-quiet`，改用 `-o` 记录完整日志；启动失败时会打印 DISPLAY、端口和错误输出
- 修复引导里的 AstrBot 截图无法显示：改为通过 `guide/asset` 接口按插件目录读取并转成 data URI，避免依赖插件页的静态资源路由与路径分隔符
- 恢复引导动画：每一步内容淡入 + 上移，遮罩淡入

## 1.0.2

- 新增独立的「下载与安装」页面：版本、发行包、下载镜像都移到这里，从「配置 → 维护与帮助」进入，不再挤在配置弹窗里
- 下载镜像、版本与发行包的选择**实时保存**（专用接口 `download/options`），不需要再点保存配置
- 安装流程在下载完成后自动检测 Linux 依赖（Xvfb / x11vnc / fluxbox / xwininfo / scrot）；缺少时弹窗索取 sudo 密码并自动安装，密码只用于本次操作
- Xvfb 虚拟屏幕改为 `800x600`，截图与 VNC 更贴近 QQ 登录窗口尺寸；ffmpeg 兜底截图同步使用 `800x600`
- 优化控制台布局：三个主按钮等宽一行、左右分栏改为 1.5:1、日志列吸顶、状态卡自适应网格、QQ 操作按钮与维护按钮网格化、操作提示移到主按钮下方
- 重新制作首次使用引导：补充「下载与安装」入口、镜像实时生效、依赖检查与 sudo 提示，并新增对操作提示区的介绍
- 引导使用独立版本号，升级到 1.0.2 后会重新展示一次更新后的引导

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
- 对齐 SnowLuma 官方无头部署：优先使用 fluxbox、Xvfb 800x600x16，安装 dbus-x11 / libcap2-bin
- 「修复 QQ 权限」会额外 `setcap cap_sys_ptrace=ep` 到 SnowLuma 的 node，兼容 Yama / 容器环境
- 配置 sudo 密码时，启动 QQ 会自动修正 `ptrace_scope`；窗口检查失败会打印当前 X11 窗口树，方便定位黑屏
- 修复服务进程 `PATH` 精简导致 `which fluxbox` 找不到 `/usr/bin/fluxbox`、从而一直回退 openbox 的问题：改为同时搜索 `/usr/bin` 等标准目录，并在日志里打印实际使用的窗口管理器路径
- 启动虚拟显示前自动结束占用同一 DISPLAY 的旧窗口管理器，避免 openbox 与 fluxbox 争夺显示导致窗口不映射
- 窗口管理器异常退出时记录 `window-manager.log` 并自动回退；QQ 启动后主动检查是否已映射窗口
- 为无头 QQ 创建独立 D-Bus 会话（`dbus-x11`），并设置 `NO_AT_BRIDGE=1`，避免 Electron 在无会话总线环境下不显示登录窗口
- 二维码截图会自动等待登录页真正出现（识别 QQ 启动画面并按中心白色区域判定），不再把黑色启动画面当成“已截取”，前端会自动重试最多 3 次
- QQ 的 stdout/stderr 写入 `qq.log`（含 `--enable-logging=stderr`）；QQ 启动后立即退出会记录返回码和日志尾部，不再静默失败
- 窗口检查要求存在 ≥120px 的可见 QQ 窗口，避免把残留窗口误判为登录窗口
- 黑屏 / 无窗口 / 仍在加载时，登录弹窗直接显示诊断信息：X11 窗口树、QQ 进程命令行与 `DISPLAY`、`qq.log` 尾部
- 新增「修复 QQ 登录界面」维护按钮：把 `~/.config/QQ/versions` 下的热更新版本改名备份，让 QQ 回退到安装包自带版本（QQ 热更新自带损坏的 `libssh2.so.1` 时登录界面会空白或不出现）
- 窗口探测失败只在启动结束时记录一次完整窗口树，避免每秒刷屏
- `qq.log` 摘要会过滤 Chromium/Electron 的 `[pid:date:WARNING]` 噪声，只保留 QQ 自身的 Node 报错，便于定位登录窗口不出现的原因
- 摘要改为只读取本次启动后新增的日志，并从第一处 `UnhandledPromiseRejection / Error` 开始截取，确保能看到真正的错误头而不是栈尾
- 启动 QQ 时自动预加载系统 `libssh2`：QQ 自带 `libssh2.so.1` 与系统 `libcurl` 不匹配时（`undefined symbol: libssh2_session_set_read_timeout`）会导致登录窗口完全不创建
- 诊断信息增加 libssh2 状态与 QQ 自带库路径；「修复 QQ 登录界面」在缺少系统 libssh2 时给出安装提示
- 检测并修复被 NapCat 补丁接管的 QQ 启动入口（`resources/app/package.json` 的 `main` 被改成 `loadNapCat.js`）：NapCat 数据目录被删除后，该加载器会抛 `ERR_MODULE_NOT_FOUND`，QQ 因此完全不创建登录窗口
- 「修复 QQ 登录界面」会通过 sudo 备份 `package.json` 并恢复原始入口，同时禁用 `loadNapCat.js`
- 启动 QQ 时清除继承的 `NAPCAT_BOOTMAIN` 环境变量，诊断信息中显示当前 `main` 入口

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
