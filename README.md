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

QQNT 需要图形界面才能显示二维码。无桌面的 Ubuntu 服务器建议用 Xvfb + VNC：

```bash
sudo apt update
sudo apt install -y xvfb x11vnc openbox

# 用运行 AstrBot 的同一个用户执行
Xvfb :99 -screen 0 1280x800x24 &
DISPLAY=:99 openbox &
x11vnc -display :99 -forever -shared -rfbport 5900 -passwd '替换成你的VNC密码'
```

然后在插件配置里把「Linux QQ DISPLAY」填为 `:99`，保存后用 VNC 客户端连接 `服务器IP:5900`，再在面板点「启动 QQ」，即可在 VNC 里扫码登录。

注意：

- AstrBot、SnowLuma、QQ 必须使用同一个系统用户运行，不要用 root 启动 QQ
- 如果 QQ 之前用 root 启动过，先停止旧进程，并修正 `~/.config/QQ` 的属主
- VNC 暴露到公网有风险，建议只在内网使用或通过 SSH 隧道连接

## 所需环境

- AstrBot `>= 4.24.1`，并已创建 OneBot v11（aiocqhttp）机器人
- QQNT 9.9.x（Windows / Linux）；QQ 登录需自行完成
- 使用 full 发行包无需额外运行时；使用 lite 发行包需 Node.js `^24.22.1 || >=26.12.1`
- 可访问 GitHub Releases（或配置下载镜像）
