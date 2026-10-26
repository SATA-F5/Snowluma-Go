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

## 所需环境

- AstrBot `>= 4.24.1`，并已创建 OneBot v11（aiocqhttp）机器人
- QQNT 9.9.x（Windows / Linux）；QQ 登录需自行完成
- 使用 full 发行包无需额外运行时；使用 lite 发行包需 Node.js `^24.22.1 || >=26.12.1`
- 可访问 GitHub Releases（或配置下载镜像）
