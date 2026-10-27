/* SnowLuma Go — plugin page controller (NapCat_Go console style). */
(function () {
    "use strict";

    const $ = (id) => document.getElementById(id);
    const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

    const CHANNEL = "astrbot-plugin-page";
    const ONBOARD_KEY = "snowluma_go_onboarded_v1";
    const LOCAL_HOSTS = ["127.0.0.1", "localhost", "0.0.0.0", "[::]", "::"];
    const LOOPBACK_BIND_HOSTS = ["127.0.0.1", "localhost", "::1", "[::1]"];
    const STATUS_INTERVAL = 3000;
    const FAST_INTERVAL = 900;
    const LOG_INTERVAL = 2500;
    const MAX_LOG_LINES = 1200;

    const S = {
        bridge: null,
        status: null,
        releases: [],
        mirrors: [],
        logs: [],
        logOffset: 0,
        logSource: "process",
        paused: false,
        busy: new Set(),
        statusTimer: null,
        offlineWarned: false,
        revealedPassword: "",
        onboarding: { steps: [], idx: 0 },
        context: {},
    };

    // ------------------------------------------------------------------
    // Bridge: use AstrBot's SDK when injected, otherwise speak the same
    // postMessage protocol ourselves. AstrBot injects the SDK at the end of
    // <body>, i.e. *after* this script, and some builds fail to serve it at
    // all; depending on it alone left the page without a backend.
    // ------------------------------------------------------------------

    function createFallbackBridge() {
        const origin = window.location.origin;
        const pending = new Map();
        let seq = 0;
        let context = null;
        let resolveReady;
        const ready = new Promise((resolve) => { resolveReady = resolve; });

        window.addEventListener("message", (event) => {
            if (event.source !== window.parent) return;
            if (origin !== "null" && event.origin !== origin && event.origin !== "null") return;
            const message = event.data;
            if (!message || message.channel !== CHANNEL) return;

            if (message.kind === "context") {
                context = message.context || {};
                if (resolveReady) { resolveReady(context); resolveReady = null; }
                return;
            }
            if (message.kind === "response") {
                const waiter = pending.get(message.requestId);
                if (!waiter) return;
                pending.delete(message.requestId);
                if (message.ok) waiter.resolve(message.data);
                else waiter.reject(new Error(message.error || "插件后台请求失败"));
            }
        });

        function send(kind, payload) {
            try {
                window.parent.postMessage(
                    { channel: CHANNEL, kind, ...(payload || {}) },
                    origin === "null" ? "*" : origin
                );
            } catch (error) { /* iframe detached */ }
        }

        function request(action, payload) {
            return new Promise((resolve, reject) => {
                const requestId = "slgo_" + (++seq);
                const timer = setTimeout(() => {
                    pending.delete(requestId);
                    reject(new Error("请求超时：仪表盘没有响应"));
                }, 15000);
                pending.set(requestId, {
                    resolve: (value) => { clearTimeout(timer); resolve(value); },
                    reject: (error) => { clearTimeout(timer); reject(error); },
                });
                send("request", { requestId, action, ...payload });
            });
        }

        send("ready");
        // Never block the UI forever when the page is opened outside AstrBot.
        setTimeout(() => { if (resolveReady) { resolveReady(context || {}); resolveReady = null; } }, 3500);

        return {
            ready: () => ready,
            getContext: () => context,
            apiGet: (endpoint, params) => request("api:get", { endpoint, params }),
            apiPost: (endpoint, body) => request("api:post", { endpoint, body }),
            onContext: (handler) => { if (typeof handler === "function" && context) handler(context); },
        };
    }

    async function resolveBridge() {
        const existing = window.AstrBotPluginPage || window.AstrBotPluginView;
        if (existing) return existing;
        await sleep(400);
        const late = window.AstrBotPluginPage || window.AstrBotPluginView;
        if (late) return late;
        console.warn("[snowluma] AstrBot bridge SDK not injected; using built-in bridge");
        const fallback = createFallbackBridge();
        if (!window.AstrBotPluginPage) window.AstrBotPluginPage = fallback;
        return fallback;
    }

    const apiGet = (endpoint, params) => S.bridge.apiGet(endpoint, params);
    const apiPost = (endpoint, body) => S.bridge.apiPost(endpoint, body || {});

    // ------------------------------------------------------------------
    // Helpers
    // ------------------------------------------------------------------

    function esc(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }

    // Remote AstrBot visitors must not be sent to their own 127.0.0.1 when
    // SnowLuma actually runs on the server. Mirror NapCat_Go's behaviour:
    // replace only loopback hosts with the hostname used to open this page.
    function rewriteUrlForCurrentHost(url) {
        if (!url) return url;
        const currentHost = window.location.hostname;
        if (!currentHost) return url;
        try {
            const parsed = new URL(url);
            if (LOCAL_HOSTS.includes(parsed.hostname)) parsed.hostname = currentHost;
            return parsed.toString();
        } catch (error) {
            return String(url).replace(/127\.0\.0\.1|localhost|0\.0\.0\.0|\[::\]/g, currentHost);
        }
    }

    function currentWebUIUrl() {
        const status = S.status || {};
        const raw = status.webui_url || "";
        if (!raw) return "";
        const bindHost = String(status.webui_bind_host || "").trim();
        // If the user explicitly keeps SnowLuma on loopback, don't advertise a
        // remote URL that cannot work. 0.0.0.0 means all interfaces, so it is
        // *not* local-only even though it appears in LOCAL_HOSTS.
        if (LOOPBACK_BIND_HOSTS.includes(bindHost)) return raw;
        return rewriteUrlForCurrentHost(raw);
    }

    function toast(message, kind, duration) {
        const node = $("toast");
        node.textContent = message;
        node.className = "toast visible" + (kind ? " " + kind : "");
        clearTimeout(toast._timer);
        toast._timer = setTimeout(() => { node.className = "toast" + (kind ? " " + kind : ""); }, duration || 3000);
    }

    function confirmDialog(message, okLabel) {
        return new Promise((resolve) => {
            $("confirm-msg").textContent = message;
            $("confirm-ok").textContent = okLabel || "确定";
            $("confirm-mask").classList.add("visible");
            const done = (value) => {
                $("confirm-mask").classList.remove("visible");
                $("confirm-ok").removeEventListener("click", onOk);
                $("confirm-cancel").removeEventListener("click", onCancel);
                resolve(value);
            };
            const onOk = () => done(true);
            const onCancel = () => done(false);
            $("confirm-ok").addEventListener("click", onOk);
            $("confirm-cancel").addEventListener("click", onCancel);
        });
    }

    function shorten(text, limit) {
        const raw = String(text == null ? "" : text);
        return raw.length > (limit || 240) ? raw.slice(0, limit || 240) + "…" : raw;
    }

    function setBusy(action, busy) {
        if (busy) S.busy.add(action); else S.busy.delete(action);
        document.querySelectorAll('[data-action="' + action + '"]').forEach((node) => {
            if (node.tagName === "BUTTON") node.disabled = !!busy;
        });
    }

    async function run(action, fn, options) {
        if (S.busy.has(action)) return null;
        setBusy(action, true);
        try {
            return await fn();
        } catch (error) {
            toast((options && options.errorPrefix ? options.errorPrefix : "") + (error.message || error), "err", 5000);
            return null;
        } finally {
            setBusy(action, false);
        }
    }

    function setValue(id, text, cls) {
        const node = $(id);
        if (!node) return;
        node.textContent = text == null ? "-" : String(text);
        node.className = "value" + (cls ? " " + cls : "");
    }

    // ------------------------------------------------------------------
    // Status rendering
    // ------------------------------------------------------------------

    function isDownloading(status) {
        const d = (status && status.download) || {};
        if (d.downloading) return true;
        return ["resolving", "downloading", "verifying", "extracting", "installing"].includes(d.phase);
    }

    function render(status) {
        renderHeader(status);
        renderStatusGrid(status);
        renderAccounts(status);
        renderProcesses(status);
        renderGuide(status);
        renderDownload(status);
        renderAuxButtons(status);
    }

    function renderHeader(status) {
        const version = status.installed_version || "";
        const flavor = status.release_flavor || "";
        const node = (status.node && status.node.version) || "";
        const mode = status.external ? "external" : status.deploy_mode || "";
        $("subtitle").textContent = status.installed
            ? ["SnowLuma " + (version || "?"), flavor ? flavor + " 包" : "", mode, node ? "Node " + node : ""]
                .filter(Boolean).join(" · ")
            : "尚未安装 SnowLuma";

        const power = $("btn-toggle-power");
        const label = $("power-label");
        power.classList.remove("power-on", "power-off", "power-downloading");
        power.disabled = false;

        if (isDownloading(status)) {
            power.classList.add("power-downloading");
            label.textContent = "安装中...";
            power.disabled = true;
        } else if (status.running) {
            power.classList.add("power-off");
            label.textContent = "停止 SnowLuma";
        } else {
            power.classList.add("power-on");
            label.textContent = status.installed ? "启动 SnowLuma" : "安装 SnowLuma";
        }
    }

    function renderStatusGrid(status) {
        const syncer = status.syncer || {};
        const accounts = status.accounts || [];
        const connections = status.connections || [];
        const qq = status.qq || {};
        const node = status.node || {};
        const first = accounts[0] || {};

        if (!status.installed) {
            setValue("s-run", "未安装", "warn");
            setValue("s-version", "未安装", "warn");
            setValue("s-webui", "未启动", "warn");
        } else if (status.running) {
            setValue("s-run", status.pid ? "运行中 · " + status.pid : "运行中", "ok");
            setValue("s-version", status.installed_version || "-", status.installed_version ? "" : "warn");
            setValue("s-webui", currentWebUIUrl() || "-", "ok");
        } else {
            setValue("s-run", "已停止", "err");
            setValue("s-version", status.installed_version || "-", status.installed_version ? "" : "warn");
            setValue("s-webui", currentWebUIUrl() || "-", "warn");
        }

        setValue("s-mode", status.deploy_mode || "-");
        setValue(
            "s-login",
            status.auth_ok
                ? "已登录" + (status.consent_required ? " · 待同意协议" : "")
                : status.running ? "未登录" : "未运行",
            status.auth_ok ? "ok" : "warn"
        );
        setValue(
            "s-uin",
            first.uin
                ? first.uin + (first.nickname ? " · " + first.nickname : "")
                : (status.consent_required ? "待同意协议" : "无在线账号"),
            first.uin ? "ok" : "warn"
        );

        const bot = syncer.astrbot_bot_found
            ? (syncer.astrbot_bot_name || syncer.astrbot_bot_id || "已找到")
            : "未找到";
        setValue("s-bot", bot, syncer.astrbot_bot_found ? "ok" : "err");
        setValue(
            "s-sync",
            syncer.last_sync_time
                ? syncer.last_sync_time.replace(/^\d{4}-/, "")
                : "尚未同步",
            syncer.last_sync_ok ? "ok" : "warn"
        );

        const runningCount = ((qq.running_pids || []).length) || 0;
        setValue(
            "s-qq",
            qq.installed
                ? (qq.version || "已安装") + (runningCount ? " · " + runningCount + " 进程" : " · 未运行")
                : "未检测到 QQ",
            qq.installed ? (runningCount ? "ok" : "warn") : "err"
        );
        setValue(
            "s-node",
            node.version ? node.version + (node.bundled ? " · 内置" : " · 系统") : "未找到",
            node.supported ? "ok" : "err"
        );
        setValue("s-conn", syncer.connection_name || "astrbot");
        setValue("s-plugin", (status.plugin && status.plugin.code_version) || "-");
        setValue("s-install-dir", status.install_dir || "-");
        setValue("s-astrbot-path", syncer.astrbot_config_path || "未找到", syncer.astrbot_config_path ? "" : "warn");

        renderPassword(status);

        // keep the sync button meaningful
        const syncBtn = $("btn-sync");
        syncBtn.disabled = !status.running || !syncer.astrbot_bot_found || S.busy.has("sync");
    }

    function renderPassword(status) {
        const node = $("s-password");
        if (!node) return;
        if (S.revealedPassword) {
            node.textContent = S.revealedPassword;
            node.classList.add("revealed");
            return;
        }
        node.classList.remove("revealed");
        node.textContent = (status && status.webui_password_masked)
            || (status && status.password_managed ? "已保存（点击显示）" : "未设置（首次启动自动生成）");
    }

    function renderAccounts(status) {
        const host = $("account-list");
        const accounts = status.accounts || [];
        const connections = status.connections || [];
        if (!accounts.length) {
            host.innerHTML = '<div class="empty-box">还没有账号上线。请启动 QQ 并完成登录' +
                (status.consent_required ? "；并先在 SnowLuma 同意用户协议。" : "。") + "</div>";
            return;
        }
        host.innerHTML = accounts.map((account) => {
            const uin = String(account.uin || "");
            const conn = connections.find((item) => String(item.uin || "") === uin) || {};
            const adapters = conn.adapters || conn.clients || conn.connections || [];
            const online = conn.online === true || conn.connected === true || (Array.isArray(adapters) && adapters.length > 0);
            const parts = [];
            if (account.nickname) parts.push(account.nickname);
            parts.push(online ? "OneBot 适配器在线" : "OneBot 适配器未连接");
            if (Array.isArray(adapters) && adapters.length) parts.push(adapters.length + " 个连接");
            return '<div class="proc-row">' +
                '<div class="who"><b>' + esc(uin) + "</b><span>" + esc(parts.join(" · ")) + "</span></div>" +
                '<div class="acts"><span class="tag ' + (online ? "ok" : "warn") + '">' +
                (online ? "在线" : "离线") + "</span>" +
                '<button class="btn btn-sm" data-action="sync">同步</button></div></div>';
        }).join("");
    }

    function statusClass(value) {
        const text = String(value || "").toLowerCase();
        if (text === "online" || text === "loaded") return "ok";
        if (text === "error" || text === "disconnected") return "err";
        if (text === "loading" || text === "connecting") return "warn";
        return "";
    }

    function renderProcesses(status) {
        const host = $("qq-process-list");
        const processes = status.qq_processes || [];
        if (!processes.length) {
            host.innerHTML = '<div class="empty-box">' + (status.qq && status.qq.installed
                ? "QQ 已安装但尚未运行，或 SnowLuma 还没有发现进程。"
                : "未检测到 QQNT，请先安装 QQ 或在配置里填写路径。") + "</div>";
            return;
        }
        host.innerHTML = processes.map((info) => {
            const pid = Number(info.pid || 0);
            const state = String(info.status || "available");
            const account = info.account && info.account.uin ? String(info.account.uin) : String(info.uin || "");
            return '<div class="proc-row">' +
                '<div class="who"><b>PID ' + pid + (account ? " · " + esc(account) : "") + "</b>" +
                "<span>" + esc(shorten(info.path || info.name || "", 90)) + "</span></div>" +
                '<div class="acts"><span class="tag ' + statusClass(state) + '">' + esc(state) + "</span>" +
                (info.injected
                    ? '<button class="btn btn-sm" data-action="hook-unload" data-pid="' + pid + '">卸载</button>'
                    : '<button class="btn btn-sm" data-action="hook-load" data-pid="' + pid + '">挂载</button>') +
                "</div></div>";
        }).join("");
    }

    function renderGuide(status) {
        const guide = $("guide");
        const parts = [];
        let kind = "";

        if (status.consent_required) {
            kind = "warn";
            parts.push("<strong>需要同意 SnowLuma 协议</strong><br>SnowLuma 要求先确认用户协议与隐私政策，之后才能读写 OneBot 配置。");
            parts.push('<div class="guide-actions">' +
                '<button class="btn btn-sm" data-action="open-webui">打开 WebUI</button>' +
                '<button class="btn btn-sm btn-primary" data-action="accept-agreements">我同意</button></div>');
        } else if (!status.installed) {
            kind = "warn";
            parts.push("<strong>尚未安装 SnowLuma</strong><br>打开「配置」→「版本与安装」，点「安装 / 更新」选择 full 发行包即可自动下载并部署。");
        } else if (status.last_error) {
            kind = "error";
            parts.push("<strong>最近错误</strong><br>" + esc(shorten(status.last_error, 300)));
        } else if (!status.running) {
            kind = "";
            parts.push("<strong>SnowLuma 已停止</strong><br>点击左上角「启动 SnowLuma」，插件会自动登录 WebUI 并加载账号。");
        } else if (!(status.accounts || []).length) {
            kind = "";
            parts.push("<strong>等待 QQ 登录</strong><br>启动 QQ 并完成登录后，账号与 OneBot 连接会自动就绪。");
        } else if (!(status.syncer && status.syncer.last_sync_ok)) {
            kind = "warn";
            parts.push("<strong>账号已在线，等待接入</strong><br>点击「立即同步」把 AstrBot 的反向 WS 写入 SnowLuma。");
        }

        if (!parts.length) {
            guide.className = "guide";
            guide.innerHTML = "";
            return;
        }
        guide.className = "guide visible" + (kind ? " " + kind : "");
        guide.innerHTML = parts.join("");
    }

    function renderDownload(status) {
        const panel = $("download-panel");
        const data = status.download || {};
        const phase = data.phase || "idle";
        const active = isDownloading(status) || phase === "done" || phase === "failed";
        if (!active) {
            panel.className = "download-panel";
            return;
        }
        panel.className = "download-panel visible" + (phase === "done" ? " done" : phase === "failed" ? " failed" : "");
        $("dl-title-text").textContent = data.message || (phase === "done" ? "安装完成" : phase === "failed" ? "安装失败" : "正在安装 SnowLuma");
        $("dl-mirror").textContent = data.current_mirror || "";
        const percent = Math.max(0, Math.min(100, Number(data.percent) || 0));
        $("dl-progress-fill").style.width = percent + "%";
        $("dl-percent").textContent = phase === "failed" ? "失败" : percent + "%";
        $("dl-size").textContent = data.downloaded_mb
            ? data.downloaded_mb + "MB" + (data.total_mb ? " / " + data.total_mb + "MB" : "")
            : "";
        $("dl-speed").textContent = data.speed_kbps ? Math.round(data.speed_kbps) + "KB/s" : "";
    }

    function renderAuxButtons(status) {
        const installed = !!status.installed;
        const running = !!status.running;
        $("btn-qq-launch").disabled = !(status.qq && status.qq.installed) || S.busy.has("qq-launch");
        $("btn-hook-all").disabled = !running || S.busy.has("hook-all");
        $("btn-restart").disabled = !installed || S.busy.has("restart");
        $("btn-cleanup").disabled = S.busy.has("cleanup");
        $("btn-qq-cleanup").disabled = S.busy.has("qq-cleanup");
        $("btn-install").disabled = isDownloading(status);
        $("btn-open-webui").disabled = !(running && status.webui_url);
    }

    // ------------------------------------------------------------------
    // Onboarding (first-run spotlight tour, same style as NapCat_Go)
    // ------------------------------------------------------------------

    function buildOnboardingSteps() {
        const steps = [
            {
                type: "backdrop",
                html: '<span class="ob-step-badge">WELCOME</span>' +
                    '<div class="ob-welcome-title">感谢您使用<br>SnowLuma Go</div>' +
                    '<div class="ob-welcome-sub">接下来会快速认识界面，<br>并完成第一次 AstrBot 接入。</div>',
            },
            {
                type: "backdrop",
                html: '<span class="ob-step-badge">前置 1 / 3</span>' +
                    "<h3>在 AstrBot 中创建机器人</h3>" +
                    "<p>打开 <b>AstrBot</b> 后台 → <code>机器人</code>：</p>" +
                    "<ul>" +
                    "<li>新建机器人，平台选择 <code>OneBot v11</code> 并启用；</li>" +
                    "<li>反向 Websocket 主机填 <code>0.0.0.0</code>，端口如 <code>6199</code>；</li>" +
                    "<li>填写一个随机 Token，稍后由插件自动写入 SnowLuma。</li>" +
                    "</ul>" +
                    '<img class="ob-shot" src="./assets/astbot-create-bot.png" alt="AstrBot 创建机器人">' +
                    '<div class="ob-shot-cap">AstrBot 新建机器人界面（红框为关键填写项）</div>' +
                    '<div class="ob-warn-box">没有可用的 OneBot v11 机器人时，「立即同步」会保持不可用。</div>',
            },
            {
                type: "backdrop",
                html: '<span class="ob-step-badge">前置 2 / 3</span>' +
                    "<h3>安装 SnowLuma</h3>" +
                    "<p>点击右上角 <b>配置</b>，在「版本与安装」里点 <b>安装 / 更新</b>：</p>" +
                    "<ul>" +
                    "<li>优先选 <code>full</code> 发行包，自带 Node，无需额外环境；</li>" +
                    "<li>下载走 GitHub，失败会自动切换镜像；</li>" +
                    "<li>安装会保留 <code>config/</code>、<code>data/</code> 等用户数据。</li>" +
                    "</ul>" +
                    '<div class="ob-hint">国内网络建议保留默认下载镜像。</div>',
            },
            {
                type: "backdrop",
                html: '<span class="ob-step-badge">前置 3 / 3</span>' +
                    "<h3>首次启动还需要</h3>" +
                    "<ul>" +
                    "<li>SnowLuma 首次使用会要求同意用户协议，点页面里的 <b>我同意</b>；</li>" +
                    "<li>WebUI 密码由插件自动生成并加密保存，可在「WebUI 密码」处显示 / 复制，无需自己设置；</li>" +
                    "<li>QQ 登录必须由你在 QQ 客户端里扫码或快速登录。</li>" +
                    "</ul>" +
                    '<div class="ob-warn-box">没有同意协议之前，同步与密码重置都会被 SnowLuma 拒绝。</div>',
            },
        ];

        const tour = [
            ["btn-toggle-power", "启动 / 停止", "一键启动或停止 SnowLuma。启动后插件会自动登录 WebUI 并加载账号。"],
            ["btn-sync", "立即同步", "把 AstrBot 的 OneBot v11 反向 WS 写入 SnowLuma。需要账号在线且已同意协议；按钮灰色表示条件未满足。"],
            ["btn-open-webui", "打开 WebUI", "弹出自定义面板，可打开 WebUI、复制自动适配后的地址与密码。"],
            ["s-run", "运行状态", "SnowLuma 是否在运行以及进程 PID。"],
            ["s-version", "版本", "当前安装的 SnowLuma 版本号；显示「未安装」时先到配置里安装。"],
            ["s-mode", "运行方式", "auto / native / external；external 表示接管已有 SnowLuma，不下载也不拉起进程。"],
            ["s-webui", "WebUI 地址", "当前 SnowLuma WebUI 的访问地址。"],
            ["s-login", "登录状态", "插件与 SnowLuma WebUI 的会话状态；显示「待同意协议」时先点页面里的我同意。"],
            ["s-uin", "QQ 账号", "SnowLuma 内已登录的 QQ 账号。"],
            ["s-bot", "AstrBot 机器人", "插件从 cmd_config.json 探测到的 OneBot v11 机器人。"],
            ["s-sync", "上次同步", "最近一次同步的时间与结果。"],
            ["s-qq", "QQ 客户端", "QQNT 的安装情况与运行进程数。"],
            ["s-node", "Node 运行时", "SnowLuma 使用的 Node 来源与版本；full 包自带运行时。"],
            ["s-conn", "接入连接名", "写入 SnowLuma 的 OneBot 连接名，便于在 WebUI 里辨认。"],
            ["s-plugin", "插件版本", "当前 SnowLuma Go 插件代码版本。"],
            ["s-password", "WebUI 密码", "自动生成并加密保存的密码，可以显示、复制或重置。"],
            ["s-install-dir", "安装目录", "本插件托管的 SnowLuma 程序目录；config/、data/ 等用户数据会保留。"],
            ["s-astrbot-path", "AstrBot 配置", "插件探测到的 AstrBot cmd_config.json 路径。"],
            ["btn-qq-launch", "启动 QQ", "启动 QQNT；Linux 无桌面时会尝试使用 Xvfb。"],
            ["btn-hook-all", "全部挂载", "让 SnowLuma 挂载所有可用的 QQ 进程。"],
            ["btn-open-config", "配置", "安装、更新或卸载 SnowLuma，修改下载镜像与自动化选项。"],
            ["log-container", "实时日志", "插件与 SnowLuma 的运行日志，出错时先看这里。"],
        ];
        tour.forEach(([target, title, desc], index) => {
            steps.push({
                type: "spotlight",
                target,
                html: '<span class="ob-step-badge">界面导览 ' + (index + 1) + " / " + tour.length + "</span>" +
                    "<h3>" + title + "</h3><p>" + desc + "</p>",
            });
        });

        steps.push({
            type: "spotlight",
            target: "btn-sync",
            urgent: true,
            html: '<span class="ob-step-badge">FINAL 重点提醒</span>' +
                '<h3 style="color:#ffd98a;">先同意协议，再同步</h3>' +
                "<ul>" +
                "<li>先去同意 SnowLuma 的用户协议 / 隐私政策；</li>" +
                "<li>确认 QQ 账号已经在线，再点「立即同步」；</li>" +
                "</ul>" +
                '<div class="ob-danger-box">同步成功后 AstrBot 的 OneBot v11 平台会显示在线；失败原因会写在日志区。</div>',
        });
        return steps;
    }

    function startOnboarding() {
        const ob = $("onboarding");
        if (!ob) return;
        S.onboarding.steps = buildOnboardingSteps();
        S.onboarding.idx = 0;
        ob.classList.add("visible");
        renderOnboarding();
    }

    function renderOnboarding() {
        const ob = $("onboarding");
        const card = $("ob-card");
        const steps = S.onboarding.steps;
        const index = S.onboarding.idx;
        const step = steps[index];
        if (!ob || !card || !step) return;

        const first = index === 0;
        const last = index === steps.length - 1;
        card.innerHTML = step.html +
            '<div class="ob-dots"><span style="font-size:11px;color:var(--text-muted);font-family:\'Consolas\',monospace;">' +
            (index + 1) + " / " + steps.length + "</span></div>" +
            '<div class="ob-actions">' +
            '<button class="ob-btn skip" data-ob="skip">' + (first ? "跳过引导" : "不再提示") + "</button>" +
            '<div class="ob-right">' +
            (!first ? '<button class="ob-btn" data-ob="prev">上一步</button>' : "") +
            '<button class="ob-btn primary" data-ob="' + (last ? "done" : "next") + '">' +
            (last ? "开始使用" : "下一步") + "</button>" +
            "</div></div>";

        if (step.type === "backdrop") {
            ob.classList.remove("with-spotlight");
            ob.classList.add("with-backdrop");
            card.style.left = "50%";
            card.style.top = "50%";
            card.style.transform = "translate(-50%, -50%)";
            card.style.width = "";
        } else {
            ob.classList.remove("with-backdrop");
            // Position synchronously first: requestAnimationFrame is throttled
            // or paused in background tabs / embedded iframes, which used to
            // leave most tour steps without a spotlight. A second pass on the
            // next frame keeps the ring aligned after scrolling / layout shifts.
            positionOnboarding();
            requestAnimationFrame(positionOnboarding);
        }
    }

    function isElementVisible(el) {
        if (!el) return false;
        if (el.offsetParent === null && el.getClientRects().length === 0) return false;
        const style = window.getComputedStyle(el);
        return style.display !== "none" && style.visibility !== "hidden";
    }

    function positionOnboarding() {
        const ob = $("onboarding");
        const card = $("ob-card");
        const spot = $("ob-spotlight");
        const step = S.onboarding.steps[S.onboarding.idx];
        if (!ob || !card || !spot || !step || step.type !== "spotlight") {
            if (ob) ob.classList.remove("with-spotlight");
            return;
        }
        const target = step.target ? document.getElementById(step.target) : null;
        if (!target || !isElementVisible(target)) {
            if (S.onboarding.idx < S.onboarding.steps.length - 1) {
                S.onboarding.idx += 1;
                renderOnboarding();
            } else {
                ob.classList.remove("with-spotlight");
            }
            return;
        }

        const pad = 6;
        const margin = 16;
        target.scrollIntoView({ block: "nearest", inline: "nearest" });
        let rect = target.getBoundingClientRect();
        // `nearest` stops as soon as the element touches the viewport edge, so
        // the 6px spotlight frame could be clipped on targets at the top or
        // bottom (e.g. the header config button after scrolling). Pull them
        // fully inside with a little breathing room.
        if (rect.top - pad < margin) {
            window.scrollBy(0, rect.top - pad - margin);
            rect = target.getBoundingClientRect();
        } else if (rect.bottom + pad > window.innerHeight - margin) {
            window.scrollBy(0, rect.bottom + pad - window.innerHeight + margin);
            rect = target.getBoundingClientRect();
        }
        spot.style.left = (rect.left - pad) + "px";
        spot.style.top = (rect.top - pad) + "px";
        spot.style.width = (rect.width + pad * 2) + "px";
        spot.style.height = (rect.height + pad * 2) + "px";
        spot.classList.toggle("urgent", !!step.urgent);

        const cardWidth = Math.min(420, window.innerWidth - 32);
        const cardHeight = card.offsetHeight || 280;
        const clampLeft = (x) => Math.max(16, Math.min(window.innerWidth - cardWidth - 16, x));
        const clampTop = (y) => Math.max(16, Math.min(window.innerHeight - cardHeight - 16, y));
        let left = clampLeft(rect.left + rect.width / 2 - cardWidth / 2);
        let top;
        if (rect.bottom + 18 + cardHeight <= window.innerHeight - 8) {
            top = rect.bottom + 18;
        } else if (rect.top - 18 - cardHeight >= 8) {
            top = rect.top - cardHeight - 18;
        } else {
            // No vertical room (e.g. the tall log panel): dock the card beside
            // the target instead of covering the spotlight.
            const leftX = rect.left - cardWidth - 18;
            const rightX = rect.right + 18;
            if (leftX >= 8) {
                left = leftX;
                top = clampTop(rect.top + rect.height / 2 - cardHeight / 2);
            } else if (rightX + cardWidth <= window.innerWidth - 8) {
                left = rightX;
                top = clampTop(rect.top + rect.height / 2 - cardHeight / 2);
            } else {
                top = Math.max(16, (window.innerHeight - cardHeight) / 2);
            }
        }

        card.style.transform = "none";
        card.style.left = left + "px";
        card.style.top = top + "px";
        card.style.width = cardWidth + "px";
        ob.classList.add("with-spotlight");
    }

    function finishOnboarding() {
        const ob = $("onboarding");
        if (ob) ob.classList.remove("visible", "with-backdrop", "with-spotlight");
        try { localStorage.setItem(ONBOARD_KEY, "1"); } catch (error) { /* ignore */ }
        try { apiPost("onboarding/done").catch(() => {}); } catch (error) { /* ignore */ }
    }

    async function maybeShowOnboarding() {
        let locallyDone = false;
        try { locallyDone = localStorage.getItem(ONBOARD_KEY) === "1"; } catch (error) { /* ignore */ }
        if (locallyDone) return;

        let config = null;
        try { config = await apiGet("config"); } catch (error) { /* ignore */ }
        if (config && config.onboarding_done) {
            try { localStorage.setItem(ONBOARD_KEY, "1"); } catch (error) { /* ignore */ }
            return;
        }
        if (!S.status) return;
        startOnboarding();
    }

    // ------------------------------------------------------------------
    // Polling
    // ------------------------------------------------------------------

    async function refreshStatus() {
        let delay = STATUS_INTERVAL;
        try {
            const data = await apiGet("status");
            if (data && typeof data === "object") {
                S.status = data;
                S.offlineWarned = false;
                render(data);
                if (isDownloading(data)) delay = FAST_INTERVAL;
            }
        } catch (error) {
            if (!S.offlineWarned) {
                S.offlineWarned = true;
                $("subtitle").textContent = "无法连接插件后台";
                $("guide").className = "guide visible error";
                $("guide").innerHTML = "<strong>插件后台未响应</strong><br>" + esc(error.message || String(error)) +
                    "<br>请确认插件已在 AstrBot 中启用，并从「插件 → Snowluma Go → 打开页面」进入。";
                toast("无法连接插件后台：" + (error.message || error), "err", 5000);
            }
            delay = 5000;
        }
        clearTimeout(S.statusTimer);
        S.statusTimer = setTimeout(refreshStatus, delay);
    }

    function classifyLog(line) {
        if (/\[err\]|✘|\berror\b|失败|异常|Traceback/i.test(line)) return "log-err";
        if (/\[warn\]|⚠|warn|警告/i.test(line)) return "log-warn";
        if (/\[ok\]|✔|成功|就绪|已启动|已连接|ready/i.test(line)) return "log-ok";
        return "";
    }

    function logLine(text) {
        const raw = String(text);
        const match = raw.match(/^\[(\d{2}:\d{2}:\d{2})\]\s*/);
        const stamp = match ? '<span class="log-ts">' + esc(match[1]) + "</span> " : "";
        const body = match ? raw.slice(match[0].length) : raw;
        return '<div class="' + classifyLog(raw) + '">' + stamp + esc(body) + "</div>";
    }

    function renderLogs() {
        const view = $("log-container");
        view.innerHTML = S.logs.map(logLine).join("");
        $("log-count").textContent = S.logs.length + " 行";
        view.scrollTop = view.scrollHeight;
    }

    async function refreshLogs() {
        if (S.paused) return;
        if (S.logSource === "server") {
            try {
                const data = await apiGet("server-logs", { limit: 200 });
                const list = (data && data.list) || [];
                S.logs = list.map((entry) => {
                    const level = String(entry.level || entry.levelName || "info").toLowerCase();
                    const time = String(entry.time || entry.timestamp || "");
                    const message = entry.message || entry.msg || JSON.stringify(entry);
                    return (time ? "[" + time.slice(11, 19) + "] " : "") +
                        (level === "error" ? "[err] " : level === "warn" ? "[warn] " : "") + message;
                });
                renderLogs();
            } catch (error) { /* silent while polling */ }
            return;
        }
        try {
            const data = await apiGet("logs", { since: S.logOffset });
            const lines = (data && data.lines) || [];
            if (lines.length) {
                S.logs = S.logs.concat(lines);
                if (S.logs.length > MAX_LOG_LINES) S.logs = S.logs.slice(-MAX_LOG_LINES);
                renderLogs();
            }
            if (data && typeof data.total === "number") S.logOffset = data.total;
        } catch (error) { /* silent while polling */ }
    }

    // ------------------------------------------------------------------
    // Actions
    // ------------------------------------------------------------------

    async function actionPower() {
        if (S.busy.has("power")) return;
        S.busy.add("power");
        $("btn-toggle-power").disabled = true;
        try {
            const status = S.status || {};
            if (!status.installed) {
                openConfig(true);
                return;
            }
            if (status.running) {
                if (!(await confirmDialog("停止后 QQ 会断开与 AstrBot 的连接，确定停止 SnowLuma？", "停止"))) return;
                const result = await run("stop", () => apiPost("stop"));
                if (result) toast(result.message || "已停止", "ok");
            } else {
                const result = await run("start", () => apiPost("start"));
                if (result) toast(result.message || (result.ok ? "已启动" : "启动失败"), result.ok ? "ok" : "err", 4000);
            }
            refreshStatus();
        } finally {
            S.busy.delete("power");
            if (S.status) renderHeader(S.status);
        }
    }

    async function actionSync() {
        const result = await run("sync", () => apiPost("sync"));
        if (result) toast(result.message || (result.ok ? "同步完成" : "同步失败"), result.ok ? "ok" : "err", 5000);
        refreshStatus();
    }

    async function actionStartQQ() {
        const result = await run("qq-launch", () => apiPost("qq/launch"));
        if (result) toast(result.message || "已发出启动指令", result.ok ? "ok" : "err", 4000);
        refreshStatus();
    }

    async function actionHookAll() {
        const result = await run("hook-all", () => apiPost("hook/all"));
        if (result) toast(result.message || (result.ok ? "已挂载" : "挂载失败"), result.ok ? "ok" : "err", 4000);
        refreshStatus();
    }

    async function actionHook(pid, mode) {
        const action = mode === "unload" ? "hook-unload" : "hook-load";
        const result = await run(action, () => apiPost(mode === "unload" ? "hook/unload" : "hook/load", { pid: pid }));
        if (result) toast(result.message || (result.ok ? "操作完成" : "操作失败"), result.ok ? "ok" : "err");
        refreshStatus();
    }

    async function actionRestart() {
        if (!(await confirmDialog("重启会短暂中断 QQ 消息收发，继续？", "重启"))) return;
        const result = await run("restart", () => apiPost("restart"));
        if (result) toast(result.message || (result.ok ? "已重启" : "重启失败"), result.ok ? "ok" : "err", 4000);
        refreshStatus();
    }

    async function actionCleanup() {
        if (!(await confirmDialog("将停止本插件启动的 SnowLuma 并清理安装目录内的残留 node 进程。继续？", "清理"))) return;
        const result = await run("cleanup", () => apiPost("cleanup"));
        if (result) toast(result.message || "已清理", result.ok ? "ok" : "err");
        refreshStatus();
    }

    async function actionCleanupQQ() {
        if (!(await confirmDialog(
            "将强制结束当前系统用户运行的 QQ 进程（包括正在登录的 QQ）。" +
            "其他用户启动的 QQ 需要 sudo 手动结束。继续？",
            "清理 QQ"
        ))) return;
        const result = await run("qq-cleanup", () => apiPost("qq/cleanup"));
        if (result) {
            toast(result.message || (result.ok ? "已清理 QQ 进程" : "清理 QQ 失败"),
                result.ok ? "ok" : "err", 6000);
        }
        refreshStatus();
    }

    async function actionCheckUpdate() {
        const result = await run("check-update", () => apiGet("update/check"));
        if (!result) return;
        if (!result.ok) { toast(result.message || "检查更新失败", "err", 4000); return; }
        const info = result.info || {};
        const latest = info.latest || info.latestVersion || info.version || "";
        const current = result.current || (S.status && S.status.installed_version) || "";
        if (latest && current && String(latest) !== String(current)) {
            toast("SnowLuma 有新版本：" + latest + "（当前 " + current + "）", "warn", 6000);
        } else {
            toast("SnowLuma 已是最新版本" + (latest ? "（" + latest + "）" : ""), "ok", 4000);
        }
    }

    async function actionAcceptAgreements() {
        const result = await run("accept-agreements", () => apiPost("accept-agreements"));
        if (result) toast(result.message || (result.ok ? "已同意协议" : "操作失败"), result.ok ? "ok" : "err", 4000);
        refreshStatus();
    }

    function openWebUIModal() {
        const status = S.status || {};
        if (!status.running || !status.webui_url) {
            toast(status.installed ? "请先启动 SnowLuma" : "请先安装并启动 SnowLuma", "warn", 3000);
            return;
        }
        const url = currentWebUIUrl();
        $("webui-url-field").value = url;
        $("webui-open-link").href = url;
        const bindHost = String(status.webui_bind_host || "").trim();
        const localOnly = LOOPBACK_BIND_HOSTS.includes(bindHost);
        $("webui-ip-hint").textContent = localOnly
            ? "当前 WebUI 仅监听本机（127.0.0.1），其他设备无法访问；可在配置里把监听地址改为 0.0.0.0。"
            : "地址已按当前访问的 IP 自动适配；远程访问 AstrBot 时不会再指向 127.0.0.1。";

        const passwordField = $("webui-password-field");
        passwordField.value = S.revealedPassword || "";
        passwordField.type = "password";
        passwordField.placeholder = status.password_managed ? "已保存，点击显示" : "首次启动自动生成";
        $("btn-toggle-webui-password").textContent = "显示";
        $("webui-modal").classList.add("visible");
    }

    async function toggleWebUIPassword() {
        const field = $("webui-password-field");
        const button = $("btn-toggle-webui-password");
        if (field.type === "text") {
            field.type = "password";
            button.textContent = "显示";
            return;
        }
        try {
            const password = await getPasswordPlain();
            if (!password) {
                toast("当前还没有受管密码", "warn");
                return;
            }
            field.value = password;
            field.type = "text";
            button.textContent = "隐藏";
        } catch (error) {
            toast("读取密码失败：" + error.message, "err", 4000);
        }
    }

    async function copyText(text, okMessage) {
        if (!text) { toast("没有可复制的内容", "warn", 2500); return false; }
        try {
            await navigator.clipboard.writeText(text);
            toast(okMessage || "已复制", "ok", 1600);
            return true;
        } catch (error) {
            const area = document.createElement("textarea");
            area.value = text;
            area.style.position = "fixed";
            area.style.opacity = "0";
            document.body.appendChild(area);
            area.select();
            try {
                document.execCommand("copy");
                toast(okMessage || "已复制", "ok", 1600);
                return true;
            } catch (inner) {
                toast("复制失败，请手动选择文本", "err", 3000);
                return false;
            } finally {
                area.remove();
            }
        }
    }

    async function getPasswordPlain() {
        if (S.revealedPassword) return S.revealedPassword;
        const data = await apiGet("password/reveal");
        const password = (data && data.password) || "";
        if (password) {
            S.revealedPassword = password;
            renderPassword(S.status);
        }
        return password;
    }

    async function actionRevealPassword() {
        if (S.revealedPassword) {
            S.revealedPassword = "";
            renderPassword(S.status);
            return;
        }
        try {
            const password = await getPasswordPlain();
            if (!password) toast("当前还没有受管密码", "warn");
        } catch (error) {
            toast("读取密码失败：" + error.message, "err", 4000);
        }
    }

    async function actionCopyPassword() {
        try {
            const password = await getPasswordPlain();
            await copyText(password, "WebUI 密码已复制");
        } catch (error) {
            toast("读取密码失败：" + error.message, "err", 4000);
        }
    }

    async function actionResetPassword() {
        if (!(await confirmDialog("将生成新的随机 WebUI 密码并立即生效（当前会话会失效）。继续？", "重置密码"))) return;
        const result = await run("reset-password", () => apiPost("password/reset"));
        if (!result) return;
        if (result.password) {
            S.revealedPassword = result.password;
            renderPassword(S.status);
            await copyText(result.password, "新密码已复制");
        }
        toast(result.message || (result.ok ? "密码已重置" : "重置失败"), result.ok ? "ok" : "err", result.ok ? 4000 : 6000);
        refreshStatus();
    }

    async function actionCopyWebUIUrl() {
        const url = currentWebUIUrl();
        if (!url) { toast("WebUI 地址不可用", "warn", 2500); return; }
        await copyText(url, "WebUI 地址已复制");
    }

    // ------------------------------------------------------------------
    // Install
    // ------------------------------------------------------------------

    async function loadReleases(force) {
        try {
            const data = await apiGet("releases", force ? { force: 1 } : undefined);
            S.releases = (data && data.releases) || [];
            const select = $("cfg-snowluma_version");
            const previous = select.value;
            const options = ['<option value="latest">latest（最新版）</option>'];
            S.releases.forEach((release) => {
                const flags = [];
                if (release.prerelease) flags.push("预发布");
                if (release.has_full === false && release.has_lite === false) flags.push("无本平台包");
                options.push('<option value="' + esc(release.tag) + '">' + esc(release.tag) +
                    (flags.length ? " [" + flags.join("/") + "]" : "") + "</option>");
            });
            select.innerHTML = options.join("");
            if (previous && Array.from(select.options).some((option) => option.value === previous)) {
                select.value = previous;
            }
        } catch (error) {
            toast("获取版本列表失败：" + error.message, "err", 5000);
        }
    }

    async function actionInstall() {
        const version = $("cfg-snowluma_version").value || "latest";
        const flavor = $("cfg-release_flavor").value || "full";
        const message = S.status && S.status.installed
            ? "将安装 " + version + "（" + flavor + "）包，程序文件会被替换，用户数据保留。继续？"
            : "将下载并安装 SnowLuma " + version + "（" + flavor + "）包。继续？";
        if (!(await confirmDialog(message, "开始安装"))) return;
        closeConfig();
        const result = await run("install", () => apiPost("install", { version: version, flavor: flavor }));
        if (result) toast(result.message || (result.ok ? "安装完成" : "安装失败"), result.ok ? "ok" : "err", 5000);
        refreshStatus();
    }

    async function actionUninstall() {
        if (!(await confirmDialog("将删除本插件管理的 SnowLuma 安装目录（含 config/ 与账号数据），不可撤销。继续？", "确认卸载"))) return;
        const result = await run("uninstall", () => apiPost("uninstall", { purge: true }));
        if (result) toast(result.message || (result.ok ? "已卸载" : "卸载失败"), result.ok ? "ok" : "err", 5000);
        refreshStatus();
    }

    // ------------------------------------------------------------------
    // Config
    // ------------------------------------------------------------------

    const CONFIG_FIELDS = [
        ["deploy_mode", "value"],
        ["snowluma_port", "number"],
        ["webui_bind_host", "value"],
        ["external_host", "value"],
        ["external_port", "number"],
        ["snowluma_version", "value"],
        ["release_flavor", "value"],
        ["download_mirror", "value"],
        ["qq_number", "value"],
        ["qq_path", "value"],
        ["qq_display", "value"],
        ["astrbot_config_path", "value"],
        ["ws_host_override", "value"],
        ["sync_connection_name", "value"],
        ["auto_start", "check"],
        ["auto_start_after_install", "check"],
        ["auto_sync", "check"],
        ["auto_hook", "check"],
        ["auto_start_qq", "check"],
        ["auto_accept_agreements", "check"],
    ];

    function renderMirrorMenu() {
        const menu = $("cfg-mirror-menu");
        const input = $("cfg-download_mirror");
        if (!menu || !input) return;
        const current = input.value.trim();
        const options = [{ value: "", label: "官方 GitHub（不使用镜像）", tag: "官方" }];
        S.mirrors.forEach((mirror, index) => {
            options.push({ value: mirror, label: mirror, tag: index === 0 ? "默认" : "" });
        });
        menu.innerHTML = options.map((option) => {
            const selected = option.value === current;
            const meta = [option.tag ? '<span class="mirror-tag">' + esc(option.tag) + "</span>" : ""]
                .concat(selected ? ['<span class="mirror-check">✓</span>'] : [])
                .join("");
            return '<button class="mirror-option' + (selected ? " selected" : "") + '" type="button" role="option"' +
                ' aria-selected="' + (selected ? "true" : "false") + '"' +
                ' data-mirror="' + esc(option.value) + '">' +
                '<span class="mirror-url">' + esc(option.label) + "</span>" +
                (meta ? '<span class="mirror-meta">' + meta + "</span>" : "") +
                "</button>";
        }).join("");
    }

    function setMirrorMenu(open) {
        const picker = $("cfg-mirror-picker");
        const menu = $("cfg-mirror-menu");
        const toggle = $("cfg-mirror-toggle");
        if (!picker || !menu || !toggle) return;
        picker.classList.toggle("open", open);
        menu.classList.toggle("visible", open);
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
        if (!open) return;
        const selected = menu.querySelector(".mirror-option.selected");
        if (selected) selected.scrollIntoView({ block: "nearest" });
    }

    async function openConfig(scrollToInstall) {
        try {
            const config = await apiGet("config");
            CONFIG_FIELDS.forEach(([key, kind]) => {
                const input = $("cfg-" + key);
                if (!input) return;
                const value = config[key];
                if (kind === "check") input.checked = !!value;
                else input.value = value == null ? "" : value;
            });
            $("cfg-webui_password").value = "";
            $("cfg-webui_password").type = "password";
            S.mirrors = Array.isArray(config.download_mirrors) ? config.download_mirrors.slice() : [];
            renderMirrorMenu();
            setMirrorMenu(false);
            $("config-modal").classList.add("visible");
            if (!S.releases.length) await loadReleases(false);
            if (S.status) {
                // keep the version picker aligned with the running config
                const select = $("cfg-snowluma_version");
                const configured = config.snowluma_version || "latest";
                if (Array.from(select.options).some((option) => option.value === configured)) select.value = configured;
                $("cfg-release_flavor").value = config.release_flavor || (S.status.release_flavor || "full");
                $("btn-uninstall").style.display = S.status.installed ? "" : "none";
            }
            if (scrollToInstall) {
                $("install-section").scrollIntoView({ behavior: "smooth", block: "start" });
            }
        } catch (error) {
            toast("读取配置失败：" + error.message, "err", 5000);
        }
    }

    function closeConfig() {
        setMirrorMenu(false);
        $("config-modal").classList.remove("visible");
    }

    async function saveConfig() {
        const payload = {};
        CONFIG_FIELDS.forEach(([key, kind]) => {
            const input = $("cfg-" + key);
            if (!input) return;
            if (kind === "check") payload[key] = input.checked;
            else if (kind === "number") payload[key] = Number(input.value) || 0;
            else payload[key] = input.value;
        });
        const password = $("cfg-webui_password").value.trim();
        if (password) payload.webui_password = password;

        const result = await run("config-save", () => apiPost("config/save", payload));
        if (!result) return;
        if (result.ok) {
            toast("配置已保存", "ok");
            closeConfig();
            if (result.status) {
                S.status = result.status;
                render(result.status);
            }
            refreshStatus();
        } else {
            toast(result.message || "保存失败", "err", 5000);
        }
    }

    async function revealPassword() {
        try {
            const password = await getPasswordPlain();
            const input = $("cfg-webui_password");
            if (password) {
                input.value = password;
                input.type = "text";
                renderPassword(S.status);
                toast("已显示当前受管密码", "ok", 2000);
            } else {
                toast("当前没有受管密码", "warn");
            }
        } catch (error) {
            toast("读取密码失败：" + error.message, "err");
        }
    }

    // ------------------------------------------------------------------
    // Wiring
    // ------------------------------------------------------------------

    function bindEvents() {
        $("btn-toggle-power").addEventListener("click", actionPower);
        $("btn-sync").addEventListener("click", actionSync);
        $("btn-qq-launch").addEventListener("click", actionStartQQ);
        $("btn-hook-all").addEventListener("click", actionHookAll);
        $("btn-restart").addEventListener("click", actionRestart);
        $("btn-cleanup").addEventListener("click", actionCleanup);
        $("btn-refresh").addEventListener("click", () => { refreshStatus(); toast("已刷新", "ok", 1200); });
        $("btn-check-update").addEventListener("click", actionCheckUpdate);
        $("btn-qq-cleanup").addEventListener("click", actionCleanupQQ);
        $("btn-replay-guide").addEventListener("click", () => {
            closeConfig();
            startOnboarding();
        });
        $("btn-open-config").addEventListener("click", () => openConfig(false));
        $("cfg-mirror-toggle").addEventListener("click", () => {
            setMirrorMenu(!$("cfg-mirror-menu").classList.contains("visible"));
        });
        $("cfg-download_mirror").addEventListener("focus", () => setMirrorMenu(true));
        $("cfg-download_mirror").addEventListener("input", renderMirrorMenu);
        $("cfg-download_mirror").addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                setMirrorMenu(false);
                $("cfg-download_mirror").blur();
            } else if (event.key === "ArrowDown") {
                event.preventDefault();
                setMirrorMenu(true);
            }
        });
        $("cfg-mirror-menu").addEventListener("click", (event) => {
            const option = event.target.closest(".mirror-option");
            if (!option) return;
            $("cfg-download_mirror").value = option.dataset.mirror || "";
            renderMirrorMenu();
            setMirrorMenu(false);
        });
        document.addEventListener("click", (event) => {
            if (!event.target.closest("#cfg-mirror-picker")) setMirrorMenu(false);
        });
        $("btn-config-cancel").addEventListener("click", closeConfig);
        $("btn-config-save").addEventListener("click", saveConfig);
        $("btn-reveal-password").addEventListener("click", revealPassword);
        $("btn-copy-password-modal").addEventListener("click", actionCopyPassword);
        $("btn-install").addEventListener("click", actionInstall);
        $("btn-uninstall").addEventListener("click", actionUninstall);
        $("btn-open-webui").addEventListener("click", openWebUIModal);
        $("btn-copy-webui-url").addEventListener("click", actionCopyWebUIUrl);
        $("btn-copy-webui-password").addEventListener("click", actionCopyPassword);
        $("btn-toggle-webui-password").addEventListener("click", toggleWebUIPassword);
        $("btn-close-webui").addEventListener("click", () => $("webui-modal").classList.remove("visible"));
        $("webui-open-link").addEventListener("click", (event) => {
            const status = S.status || {};
            if (!(status.running && status.webui_url)) {
                event.preventDefault();
                toast(status.installed ? "请先启动 SnowLuma" : "请先安装并启动 SnowLuma", "warn", 3000);
            }
        });
        $("btn-clear-log").addEventListener("click", () => {
            S.logs = [];
            S.logOffset = S.status ? (S.status.log_count || 0) : 0;
            renderLogs();
        });
        $("log-pause").addEventListener("change", (event) => {
            S.paused = event.target.checked;
            if (!S.paused) refreshLogs();
        });
        $("log-source").addEventListener("change", (event) => {
            S.logSource = event.target.value;
            S.logs = [];
            renderLogs();
            refreshLogs();
        });

        $("ob-card").addEventListener("click", (event) => {
            const button = event.target.closest("button[data-ob]");
            if (!button) return;
            const act = button.dataset.ob;
            if (act === "next") {
                S.onboarding.idx = Math.min(S.onboarding.idx + 1, S.onboarding.steps.length - 1);
                renderOnboarding();
            } else if (act === "prev") {
                S.onboarding.idx = Math.max(0, S.onboarding.idx - 1);
                renderOnboarding();
            } else {
                finishOnboarding();
            }
        });
        window.addEventListener("resize", () => {
            if ($("onboarding").classList.contains("visible")) {
                const step = S.onboarding.steps[S.onboarding.idx];
                if (step && step.type === "spotlight") positionOnboarding();
            }
        });
        window.addEventListener("scroll", () => {
            if ($("onboarding").classList.contains("with-spotlight")) positionOnboarding();
        }, { passive: true });

        document.addEventListener("click", (event) => {
            const target = event.target.closest("[data-action]");
            if (!target) return;
            const action = target.dataset.action;
            if (action === "sync") { actionSync(); return; }
            if (action === "hook-load") { actionHook(Number(target.dataset.pid || 0), "load"); return; }
            if (action === "hook-unload") { actionHook(Number(target.dataset.pid || 0), "unload"); return; }
            if (action === "open-webui") { openWebUIModal(); return; }
            if (action === "reveal-password") { actionRevealPassword(); return; }
            if (action === "copy-password") { actionCopyPassword(); return; }
            if (action === "reset-password") { actionResetPassword(); return; }
            if (action === "accept-agreements") { actionAcceptAgreements(); }
        });

        document.querySelectorAll(".modal").forEach((modal) => {
            modal.addEventListener("click", (event) => {
                if (event.target === modal) modal.classList.remove("visible");
            });
        });
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape") {
                document.querySelectorAll(".modal.visible").forEach((modal) => modal.classList.remove("visible"));
            }
        });
    }

    function applyContext(context) {
        S.context = context || {};
        if (S.context.pageTitle) document.title = S.context.pageTitle + " · SnowLuma Go";
        document.documentElement.setAttribute("data-theme", "dark");
    }

    async function init() {
        S.bridge = await resolveBridge();
        try {
            const context = await Promise.race([S.bridge.ready(), sleep(4000).then(() => ({}))]);
            applyContext(context);
        } catch (error) {
            applyContext({});
        }
        if (S.bridge.onContext) {
            S.bridge.onContext((next) => applyContext(next));
        }
        bindEvents();
        await refreshStatus();
        await refreshLogs();
        loadReleases(false);
        void maybeShowOnboarding();
        setInterval(refreshLogs, LOG_INTERVAL);
        document.addEventListener("visibilitychange", () => {
            if (!document.hidden) { refreshStatus(); refreshLogs(); }
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
