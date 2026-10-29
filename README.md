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

## 操作指南

下面按「从零到机器人能回消息」的顺序走一遍，全部在 AstrBot 的 **扩展功能 → SnowLuma 控制台** 页面完成。

### 1. 安装 SnowLuma

1. 打开 **SnowLuma 控制台**。
2. 点右上角 **配置** → **维护与帮助** → **下载与安装**，选择发行版和版本；不确定就用默认的 `full` + 最新版。
3. 按需选择下载镜像（选择后立即生效，不需要保存），点 **安装 / 更新**，等待日志出现安装完成。

安装日志会显示下载源、校验结果和解压目录。下载完成后会自动检查 Linux 依赖（xvfb / x11vnc / fluxbox / dbus-x11 / scrot）；缺少时会弹窗索取 sudo 密码并自动安装，密码只用于本次操作。安装完成后插件会自动刷新版本状态。

### 2. 启动 SnowLuma

点左侧状态卡下方的 **电源按钮**（启动 / 停止）。

首次启动会自动完成这些事，不需要手动操作：

- 生成并轮换 WebUI 密码
- 自动登录 WebUI
- 自动同意用户协议 / 隐私政策
- 等待 `WebUI 就绪` 后把地址和密码写进状态卡

状态卡里的 **WebUI 密码** 可以「显示 / 复制 / 重置」，需要直接进 SnowLuma 后台时用。

### 3. 启动并登录 QQ

1. 点 **启动 QQ**。
   - Windows / 有桌面的 Linux：直接在你当前桌面弹出 QQ 登录窗口。
   - 无桌面 Linux：自动创建 Xvfb 虚拟显示 + fluxbox + x11vnc，QQ 跑在虚拟屏幕上。
2. 点 **QQ 登录**，弹窗里会显示当前虚拟屏幕的截图（也就是二维码）。
3. 用手机 QQ 扫码并在手机上确认登录。
4. 二维码一直不出现时，点 **刷新二维码**；QQ 卡在旧显示或黑屏时，点 **重启 QQ 并刷新**。

登录成功后：

- 「在线账号」里会出现对应的 QQ 号
- SnowLuma 自动挂载该 QQ 进程，并为每个账号生成 `config/onebot_<uin>.json`
- 多开 QQ 时每个账号都会单独出现在列表里

> 想在浏览器里直接操作 QQ（而不只是看二维码），点 **复制 VNC 地址**，用 VNC 客户端连接即可。

### 4. 同步 AstrBot 连接

点 **同步连接**。插件会把 AstrBot 里 OneBot v11（aiocqhttp）的反向 WS 地址、Token 写入 SnowLuma 并热重载。

- 按钮高亮 / 提示「未同步」时，说明 AstrBot 侧的配置还没写进 SnowLuma。
- 同步完成后给机器人发一条消息，能在日志里看到 WS 连接即表示打通。

### 5. 挂载与重启

- **全部挂载**：把当前可见的所有 QQ 主进程都注入 SnowLuma。
- **重启**：只重启 SnowLuma 服务，不动 QQ。
- **刷新状态**：重新读取进程、账号、连接状态。

### 控制台按钮一览

| 按钮 | 作用 |
| --- | --- |
| 启动 / 停止 | 启动或停止托管的 SnowLuma 进程 |
| 同步连接 | 把 AstrBot 的 OneBot v11 反向 WS 写入 SnowLuma 并热重载 |
| 打开 WebUI | 弹出 WebUI 地址与密码，可复制；地址会自动适配你当前访问 AstrBot 用的主机名 |
| 启动 QQ | 启动 QQNT；Linux 无桌面时自动准备虚拟显示与 VNC |
| QQ 登录 | 截图当前虚拟屏幕，显示二维码 / 登录界面 |
| 全部挂载 | 注入所有发现的 QQ 主进程 |
| 重启 | 重启 SnowLuma 服务 |
| 刷新状态 | 刷新页面状态 |
| 清屏 | 清空实时日志（不影响服务端日志） |

配置弹窗里的维护按钮：

| 按钮 | 作用 |
| --- | --- |
| 下载与安装 | 打开独立下载页：选择版本 / 发行包 / 下载镜像，安装或卸载 |
| 检查更新 | 查询 SnowLuma 新版本 |
| 清理残留 | 结束本插件托管的 Xvfb / x11vnc / fluxbox / openbox 进程，清理显示锁 |
| 清理 QQ 进程 | 结束当前用户的 QQ 进程；存在其他用户的旧进程时会再询问 sudo 密码 |
| 修复 QQ 权限 | 修正 `~/.config/QQ` 属主、设置 `kernel.yama.ptrace_scope=0`、给 Node 授予 ptrace 能力 |
| 修复 QQ 登录界面 | 恢复被 NapCat 补丁改掉的 QQ 启动入口，并回滚 QQ 热更新版本 |
| 安装 Linux 依赖 | 安装 xvfb / x11vnc / fluxbox / dbus-x11 / scrot 等无头运行依赖 |
| 使用引导 | 重新播放首次使用引导 |

### 管理指令

在 AstrBot 里发送 `/snowluma`（需要管理员权限）可以查看当前状态：

```text
SnowLuma Go
------------------------
运行方式: native
SnowLuma: 已运行（1.10.0）
WebUI: http://127.0.0.1:5099
登录状态: 已登录
账号: 123456789
QQ 进程: 1 个可挂载
AstrBot 连接: ws://127.0.0.1:6199/ws
```

## QQ 登录说明

### Windows / 有桌面的 Linux

「启动 QQ」会直接显示 QQ 登录窗口，按正常扫码流程登录即可；「QQ 登录」会提示无需截图。

### 无桌面 Linux

插件在 `:99` 上创建虚拟屏幕（被占用时自动改用 `:100` / `:101`），并启动：

- `fluxbox`：窗口管理器。**必须有**，否则 QQ 登录窗口不会正常映射（只有 `openbox` 时插件会打印警告）
- `x11vnc`：把虚拟屏幕投到 VNC
- 独立 D-Bus 会话：避免 Electron 在无会话总线时异常

VNC 相关：

- 默认端口 `5900`
- **VNC 密码留空时 x11vnc 只监听 `127.0.0.1`**，远程请用 SSH 隧道：`ssh -L 5900:127.0.0.1:5900 用户@服务器`
- 填写 VNC 密码后允许局域网连接；公网暴露有风险，建议只在内网使用

> 直接看弹窗里的二维码通常就够了，VNC 只是备用手段。

## 常见问题

### 二维码弹窗是黑的 / 只看到 Ubuntu 壁纸

说明 QQ 进程在，但**没有创建登录窗口**。看日志里的 `[qq]` 行，按顺序排查：

1. 是否出现 `[qq] 已启动窗口管理器 fluxbox: :99（/usr/bin/fluxbox）`？
   - 如果是 `openbox`，说明 fluxbox 没装或找不到，先装 `fluxbox`，再点 **清理残留** 并重新启动。
2. 是否出现 `[qq] [warn] QQ 主进程已启动，但 :99 上仍未出现窗口`？
   - 后面会带 `qq.log` 摘要，直接看第一行错误。
3. 点 **修复 QQ 登录界面**，然后重新 **启动 QQ**。

弹窗里的诊断信息会列出 X11 窗口树、QQ 进程命令行、`DISPLAY`、`qq.log` 摘要，复制下来就能定位。

### 日志出现 `ERR_MODULE_NOT_FOUND ... napcat.mjs`

旧版 NapCat 插件改过 QQ 的启动入口：

```text
/opt/QQ/resources/app/package.json   "main": "./loadNapCat.js"
```

NapCat 的数据目录被删除或插件改名后，这个加载器找不到 `napcat.mjs`，QQ 会直接抛异常，**永远不创建登录窗口**。

处理：

1. **启动 SnowLuma / 启动 QQ 时会自动检测并修复**；需要 sudo 时 WebUI 会弹窗索取密码（RSA 加密、仅本次生效，不保存）。
2. 也可以手动点 **配置 → 维护与帮助 → 修复 QQ 登录界面**。
3. 或在服务器手动恢复：

```bash
sudo cp /opt/QQ/resources/app/package.json /opt/QQ/resources/app/package.json.napcat-bak
sudo python3 - <<'PY'
import json
p = '/opt/QQ/resources/app/package.json'
d = json.load(open(p))
d['main'] = './application.asar/app_launcher/index.js'
json.dump(d, open(p, 'w'), ensure_ascii=False, indent=2)
PY
sudo mv /opt/QQ/resources/app/loadNapCat.js /opt/QQ/resources/app/loadNapCat.js.disabled
```

3. 卸载 / 禁用旧的 NapCat 插件，否则它可能再次接管入口。

### 登录界面空白，或只有背景

Linux QQ 的已知问题，常见原因是热更新版本与系统 `libcurl` / `libssh2` 不匹配。

- 插件默认会预加载系统 `libssh2`，日志中可看到 `[qq] 已预加载系统 libssh2: ...`
- 仍然空白时点 **修复 QQ 登录界面**，把 `~/.config/QQ/versions` 下的热更新版本备份掉，让 QQ 回退到自带版本
- 系统缺少 libssh2 时：`sudo apt-get install -y libssh2-1`
- 也可以手动把 QQ 自带的 `libssh2.so.1` 改名（可恢复），再重启 QQ

### 日志出现 `EACCES ... mkdir '.../.config/QQ/log'`

QQ 配置目录被其他用户（通常是 root）占用，QQ 会在创建日志目录时直接退出，因此不会有登录窗口。

现在启动 QQ / 启动 SnowLuma 前会自动检测 `~/.config/QQ` 是否可写，需要时弹窗索取 sudo 密码并自动 `chown/chmod`。也可以手动点 **修复 QQ 权限**。

### 日志出现 `EACCES ... /proc/<pid>/environ`

SnowLuma 通过 ptrace 注入 QQ，被 Yama 或权限策略拦住了。

1. 点 **修复 QQ 权限**，按弹窗输入 **sudo 密码**（仅本次使用）
2. 该操作会修正 QQ 配置属主、设置 `kernel.yama.ptrace_scope=0`、给 Node 授予 `cap_sys_ptrace`
3. **重启 SnowLuma**，再重新挂载 QQ

### VNC 连不上

- 空密码时只监听本机，用 SSH 隧道或设置 VNC 密码
- 连接一次后仍黑屏，先确认弹窗里的二维码是否正常；弹窗正常说明是 VNC 显示问题而不是 QQ 问题

### WebUI 打不开

- 远程访问需要在配置里把「WebUI 监听地址」改为 `0.0.0.0`，保存后重启 SnowLuma
- 地址和密码在状态卡里，点「打开 WebUI」可以直接复制
- 密码被改乱时点「重置」

### `Could not create server lock file: /tmp/.X99-lock` / `Server is already active`

`:99` 被其他用户或上一次运行占用。点 **清理残留**；插件也会自动回退到 `:100` / `:101`。

### QQ 已经登录，但 AstrBot 没反应

1. 确认「在线账号」里有该 QQ
2. 点 **同步连接**
3. 检查 AstrBot 里 OneBot v11（aiocqhttp）机器人是否启用、端口是否与 SnowLuma 中一致
4. 看 SnowLuma 服务端日志里是否有 WS 连接记录

## 升级与迁移

- 插件升级后建议重启 AstrBot，让新代码生效
- 升级 SnowLuma 版本在 **下载与安装** 里选择新版本，用户数据（`config/`、`data/`）会保留
- 从 NapCat 系插件迁移过来时，先按上面的方法恢复 QQ 启动入口，再卸载旧插件
- external 模式只连接已有 SnowLuma 实例，不会下载或启动新的实例

## 所需环境

- AstrBot `>= 4.24.1`，并已创建 OneBot v11（aiocqhttp）机器人
- QQNT 9.9.x（Windows / Linux）；QQ 登录需自行完成
- 使用 full 发行包无需额外运行时；使用 lite 发行包需 Node.js `^24.22.1 || >=26.12.1`
- 可访问 GitHub Releases（或配置下载镜像）

无桌面 Linux 还需要 `xvfb`、`x11vnc`、`fluxbox`、`dbus-x11`、`scrot`、`x11-utils`，可以直接用 **安装 Linux 依赖** 一键安装。

注意：

- AstrBot、SnowLuma、QQ 必须使用同一个系统用户运行，不要用 root 启动 QQ
- 如果 QQ 之前用 root 启动过，点「清理 QQ 进程」时会弹窗索取 sudo 密码，用于结束旧进程并修正 `~/.config/QQ` 的属主
