# 更新日志

## 1.0.6

- 「QQ 登录 → 页面内操作」刷新间隔改为 **0.5 秒**
- 实时画面改为串行、自调度的异步轮询：上一帧完成后才发下一帧，慢请求不会叠加
- 后端截图增加互斥锁，多个标签页同时查看也不会并发抓屏
- JPEG 质量调整为 65，降低 0.5 秒刷新下的带宽与解码开销

## 1.0.5

- 「QQ 登录」的实时操作改为 **HTTP 截图 + `xdotool`**，不再依赖 iframe / noVNC / WebSocket，在 AstrBot 的受限 iframe 内也能使用
  - 点击画面即点击虚拟屏幕，支持 Enter / Tab / Esc / Backspace 和文字输入
  - 点击前先聚焦鼠标下的窗口，兼容 fluxbox 的 click-to-focus，避免第一次点击被吞
  - 静态截图也可点击进入实时模式；未安装 `xdotool` 时明确提示
- 修复 x11vnc 在 Wayland 会话环境误判退出（`Wayland display server detected`）：清理 `WAYLAND_DISPLAY` 并设置 `XDG_SESSION_TYPE=x11`
- 修复旧窗口管理器占用 `:99` 导致 fluxbox 启动失败：不再因为服务环境存在 `DISPLAY` 而跳过清理
- 「安装 Linux 依赖」加入 `xdotool`
- 移除 1.0.4 的 noVNC / websockify 方案

## 1.0.4

- 尝试用 noVNC + websockify 在「QQ 登录」弹窗内嵌实时画面（iframe）
- 新增 `novnc_port` 配置与 websockify 清理逻辑
- 该方案在 AstrBot 的受限 iframe 中不可用，1.0.5 起已移除

## 1.0.3

- 一次性 sudo 密码：由 WebUI 弹窗输入，RSA 公钥加密后传输，后端用完即弃，不写入配置与日志
- 启动前自动检查并修复：
  - NapCat 注入导致的 QQ 启动入口失效
  - `~/.config/QQ` 不可写（`EACCES ... mkdir .../QQ/log`）
  - `kernel.yama.ptrace_scope` 非 0（`COMPONENT_LOAD_FAILED`）
- 检测其他用户启动的 QQ 进程，输入 sudo 密码后结束再启动
- 启动 SnowLuma 前清理同安装目录下的残留 node 进程，避免旧实例占用 5099、新实例跑到 5100
- 状态区新增「QQ 启动环境需要修复」提示与「修复并启动 QQ」按钮
- 「Linux QQ DISPLAY」默认值改为 `99` 并持久化
- 引导截图改由 `guide/asset` 接口读取，恢复步骤淡入动画；插件版本变化自动刷新页面
- x11vnc 记录完整日志（DISPLAY、端口、错误输出）

## 1.0.2

- 新增独立的「下载与安装」页面：版本、发行包、下载镜像
- 镜像、版本与发行包选择实时保存，无需再点保存配置
- 安装流程在下载完成后检查 Linux 依赖，缺少时弹窗索取 sudo 密码并安装
- Xvfb 虚拟屏幕改为 `800x600`
- 控制台布局优化：三个主按钮一行、日志吸顶、状态卡自适应、操作按钮网格化
- 重新制作首次使用引导，并支持版本化重新展示

## 1.0.1

- 下载镜像与 NapCat_Go 保持一致，默认 `https://gh.zwy.one/`，支持自绘镜像下拉与自定义前缀
- 主界面精简，维护按钮移入配置弹窗；引导高亮与状态卡片补全
- 下载低速自动换源、显示真实进度、复用成功过的镜像
- 「打开 WebUI」改为自定义弹窗，地址按当前访问主机自动适配
- QQ 进程识别过滤 Electron 子进程；支持启动 SnowLuma 时联动启动 QQ、安装后自动启动
- 修复旧 WebUI 凭据导致的密码错误；SnowLuma 启动增加互斥锁
- 新增「清理 QQ 进程」「Linux QQ DISPLAY」「QQ 登录」二维码弹窗
- 异步端口探测；VNC 复用；Xvfb 锁冲突自动回退 `:100` / `:101`
- QQ 强制 X11 与 Chromium 无头参数；优先 fluxbox；ptrace `setcap`；独立 D-Bus 会话
- 二维码截图等待登录页出现，支持黑屏检测与重启 QQ
- QQ 日志写入 `qq.log`，窗口检查失败输出 X11 窗口树与诊断信息
- 支持回滚 QQ 热更新版本，修复登录界面空白

## 1.0.0

- 首个正式版本
- 一键安装 / 更新 SnowLuma（full / lite、镜像回退、下载校验）
- 托管启动、停止、重启，实时收集运行日志
- 自动登录 SnowLuma WebUI，自动处理初始密码与协议确认
- 通过官方 API 写入 OneBot v11 反向 WS 并热重载
- 自动发现 / 启动 QQ，挂载或卸载进程，支持多账号与 external 模式
- 内嵌控制台、开屏引导与 `/snowluma` 管理指令
- WebUI 密码显示 / 复制 / 重置，适配无图形界面的 Ubuntu 服务器
