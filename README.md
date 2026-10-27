# SnowLuma Go

SnowLuma Go 是一个把 [SnowLuma](https://github.com/SnowLuma/SnowLuma) 一键装好并接入 AstrBot 的插件：下载安装、启动登录、QQ 进程挂载、OneBot 反向 WS 配置全部自动完成，配置通过 SnowLuma 官方 WebUI API 写入并热重载。

## 功能

- 一键安装 / 更新 SnowLuma（官方 Releases，支持 full / lite 与镜像加速）
- 托管启动、停止、重启，实时查看运行日志
- 自动登录 SnowLuma WebUI，自动处理初始密码与协议确认
- 通过官方 API 写入 AstrBot 的 OneBot v11 反向 WS 连接并热重载，无需手改配置文件或重启
- 自动发现 / 启动 QQ，挂载或卸载 QQ 进程
- 支持多账号，以及 external 模式（接管已运行的 SnowLuma）
- 内嵌控制台页面，并提供 `/snowluma` 管理指令

## Ubuntu 无桌面服务器登录 QQ

插件会在无桌面环境自动启动 Xvfb，并允许直接在 WebUI 的「QQ 登录」弹窗里截取二维码，不强制需要 VNC 客户端。

推荐流程：

1. 在配置里填写「sudo 密码」（加密保存，仅用于 Linux 修复命令）
2. 点「配置 → 维护与帮助 → 安装 Linux 依赖」，安装 `xvfb x11vnc openbox scrot`
3. 点「修复 QQ 权限」，自动修正 `~/.config/QQ` 属主并设置 `kernel.yama.ptrace_scope=0`
4. 点「清理 QQ 进程」，再点「启动 QQ」
5. 点「QQ 登录」，在弹出的窗口里直接查看二维码并扫码

如果需要 VNC：

- 配置「VNC 端口」默认 `5900`
- 「VNC 密码」留空时 x11vnc 只监听 `127.0.0.1`，可通过 SSH 隧道访问
- 填写密码后允许局域网 VNC 连接；公网暴露有风险，建议只在内网使用

注意：

- AstrBot、SnowLuma、QQ 必须使用同一个系统用户运行，不要用 root 启动 QQ
- 如果 QQ 之前用 root 启动过，配置 sudo 密码后「清理 QQ 进程」会尝试通过 sudo 结束它
- 如果 QQ 之前用 root 启动过，先停止旧进程，并修正 `~/.config/QQ` 的属主

## 所需环境

- AstrBot `>= 4.24.1`，并已创建 OneBot v11（aiocqhttp）机器人
- QQNT 9.9.x（Windows / Linux）；QQ 登录需自行完成
- 使用 full 发行包无需额外运行时；使用 lite 发行包需 Node.js `^24.22.1 || >=26.12.1`
- 可访问 GitHub Releases（或配置下载镜像）
