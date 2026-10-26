/* SnowLuma Go — plugin page controller.
 *
 * Talks to the plugin through AstrBot's plugin-page bridge
 * (window.AstrBotPluginPage.apiGet/apiPost) which proxies to the routes the
 * Python side registered under /<PLUGIN_NAME>/*.
 */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const bridge = window.AstrBotPluginPage || window.AstrBotPluginView || null;

  const state = {
    status: null,
    logs: [],
    logOffset: 0,
    logSource: 'process',
    paused: false,
    releasesLoaded: false,
    currentLogHtml: [],
    busy: new Set(),
    statusTimer: null,
    logTimer: null,
    context: null,
    serverLogEntries: [],
  };

  const STATUS_INTERVAL = 3000;
  const LOG_INTERVAL = 2500;
  const FAST_INTERVAL = 900;
  const MAX_LOG_LINES = 1200;

  // ------------------------------------------------------------------ //
  // Utils
  // ------------------------------------------------------------------ //

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function toast(message, kind, duration) {
    const host = $('toasts');
    if (!host) return;
    const node = document.createElement('div');
    node.className = 'toast' + (kind ? ' ' + kind : '');
    node.textContent = message;
    host.appendChild(node);
    setTimeout(() => {
      node.style.opacity = '0';
      node.style.transition = 'opacity .25s ease';
      setTimeout(() => node.remove(), 260);
    }, duration || 3200);
  }

  function confirmDialog(title, message, okLabel) {
    return new Promise((resolve) => {
      const mask = $('confirm-mask');
      $('confirm-title').textContent = title || '确认操作';
      $('confirm-text').textContent = message || '';
      $('confirm-ok').textContent = okLabel || '确认';
      mask.classList.add('show');
      const done = (value) => {
        mask.classList.remove('show');
        $('confirm-ok').removeEventListener('click', onOk);
        $('confirm-cancel').removeEventListener('click', onCancel);
        mask.removeEventListener('click', onMask);
        resolve(value);
      };
      const onOk = () => done(true);
      const onCancel = () => done(false);
      const onMask = (event) => { if (event.target === mask) done(false); };
      $('confirm-ok').addEventListener('click', onOk);
      $('confirm-cancel').addEventListener('click', onCancel);
      mask.addEventListener('click', onMask);
    });
  }

  function showUrlDialog(url) {
    $('url-field').value = url;
    $('url-open').href = url;
    $('url-mask').classList.add('show');
  }

  function fmtDate(value) {
    if (!value) return '-';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString();
  }

  function shortText(text, limit) {
    const raw = String(text == null ? '' : text);
    return raw.length > (limit || 240) ? raw.slice(0, limit || 240) + '…' : raw;
  }

  function setBusy(action, busy) {
    if (busy) state.busy.add(action);
    else state.busy.delete(action);
    document.querySelectorAll(`[data-act="${action}"]`).forEach((btn) => {
      if (btn.tagName === 'BUTTON') btn.disabled = !!busy;
    });
  }

  async function call(action, fn) {
    if (state.busy.has(action)) return null;
    setBusy(action, true);
    try {
      return await fn();
    } catch (error) {
      toast(error && error.message ? error.message : String(error), 'err', 5000);
      return null;
    } finally {
      setBusy(action, false);
    }
  }

  const apiGet = (endpoint, params) => bridge.apiGet(endpoint, params);
  const apiPost = (endpoint, body) => bridge.apiPost(endpoint, body || {});

  // ------------------------------------------------------------------ //
  // Status polling / rendering
  // ------------------------------------------------------------------ //

  function scheduleStatus(delay) {
    if (state.statusTimer) clearTimeout(state.statusTimer);
    state.statusTimer = setTimeout(refreshStatus, delay);
  }

  async function refreshStatus() {
    let delay = STATUS_INTERVAL;
    try {
      const data = await apiGet('status');
      if (data && typeof data === 'object') {
        state.status = data;
        renderAll(data);
        if (data.download && data.download.downloading) delay = FAST_INTERVAL;
      }
    } catch (error) {
      renderOffline(error);
      delay = 5000;
    }
    scheduleStatus(delay);
  }

  function renderOffline(error) {
    $('run-pill').className = 'pill err';
    $('run-pill').lastElementChild.textContent = '离线';
    $('subtitle').textContent = '无法连接插件后台：' + (error && error.message ? error.message : error);
  }

  function renderAll(data) {
    renderHeader(data);
    renderBanners(data);
    renderStatusKv(data);
    renderAccounts(data);
    renderQQ(data);
    renderSync(data);
    renderInstall(data);
  }

  function renderHeader(data) {
    const installed = !!data.installed;
    const running = !!data.running;
    const version = data.installed_version || '';
    const flavor = data.release_flavor || '';
    const pill = $('run-pill');
    if (!installed) {
      pill.className = 'pill warn';
      pill.lastElementChild.textContent = '未安装';
    } else if (running) {
      pill.className = 'pill ok';
      pill.lastElementChild.textContent = data.external ? '已连接' : '运行中';
    } else {
      pill.className = 'pill err';
      pill.lastElementChild.textContent = '已停止';
    }
    $('subtitle').textContent = [
      version ? 'SnowLuma ' + version : 'SnowLuma',
      flavor ? flavor + ' 包' : '',
      data.external ? 'external 模式' : '本机托管',
      data.node && data.node.version ? 'Node ' + data.node.version : '',
    ].filter(Boolean).join(' · ');
    setBusy('start', running);
    setBusy('stop', !running);
    setBusy('restart', !installed);
  }

  function renderBanners(data) {
    const host = $('banners');
    const items = [];

    if (!data.installed && !data.external) {
      items.push({
        kind: 'warn',
        html: '<span class="grow">尚未安装 SnowLuma。选择版本后点击「安装 / 更新」；安装包来自 SnowLuma 官方 GitHub Releases。</span>' +
          '<button class="btn sm primary" data-act="scroll-install">去安装</button>',
      });
    }
    if (data.node && data.installed && !data.node.supported) {
      items.push({
        kind: 'warn',
        html: '<span class="grow">Node.js 版本不满足 SnowLuma 要求（' + esc(data.node.required_range) + '），' +
          '当前：' + esc(data.node.version || '未找到') + '。建议改用 full 发行包。</span>',
      });
    }
    if (data.last_error) {
      items.push({
        kind: 'err',
        html: '<span class="grow">最近错误：' + esc(shortText(data.last_error, 320)) + '</span>' +
          (data.last_error_at ? '<span class="muted mono">' + esc(data.last_error_at) + '</span>' : ''),
      });
    }
    if (data.consent_required) {
      items.push({
        kind: 'warn',
        html: '<span class="grow">SnowLuma 要求先同意《用户协议》与《隐私政策》才能读写配置。' +
          '请在确认条款后点击同意，或打开 WebUI 手动确认。</span>' +
          '<button class="btn sm ghost" data-act="open-webui">打开 WebUI</button>' +
          '<button class="btn sm primary" data-act="accept-agreements">我同意</button>',
      });
    }
    if (data.must_change_password) {
      items.push({
        kind: 'err',
        html: '<span class="grow">SnowLuma 要求先修改初始 WebUI 密码。插件会尝试自动完成；若失败，请在设置里填写新密码。</span>',
      });
    }

    host.innerHTML = items.map((item) => '<div class="banner ' + item.kind + '">' + item.html + '</div>').join('');
  }

  function renderStatusKv(data) {
    const rows = [];
    const kv = (label, value, cls) =>
      rows.push('<dt>' + esc(label) + '</dt><dd' + (cls ? ' class="' + cls + '"' : '') + '>' + value + '</dd>');

    kv('状态', data.running
      ? '<span class="ok-text">运行中</span>' + (data.pid ? ' <span class="muted mono">pid ' + esc(data.pid) + '</span>' : '')
      : '<span class="' + (data.installed ? 'warn-text' : 'muted') + '">' + (data.installed ? '未运行' : '尚未安装') + '</span>');
    kv('版本', data.installed_version
      ? esc(data.installed_version)
      : '<span class="muted">-</span>');
    kv('运行方式', esc(data.deploy_mode));
    kv('WebUI', data.webui_url
      ? '<span class="mono">' + esc(data.webui_url) + '</span>'
      : '<span class="muted">-</span>');
    kv('登录状态', data.auth_ok
      ? '<span class="ok-text">已登录</span>' + (data.consent_required ? '<span class="tag warn">待同意协议</span>' : '')
      : '<span class="warn-text">未登录</span>' + (data.password_managed ? '' : '<span class="tag warn">缺少密码</span>'));
    kv('WebUI 密码', data.webui_password_masked
      ? '<span class="mono">' + esc(data.webui_password_masked) + '</span><span class="tag">已加密保存</span>'
      : '<span class="muted">未设置</span>');
    kv('Node', data.node
      ? esc(data.node.version || '未找到') +
        (data.node.bundled ? '<span class="tag ok">内置</span>' : '<span class="tag">系统</span>') +
        (data.node.supported ? '' : '<span class="tag err">版本不兼容</span>')
      : '-');
    kv('安装目录', '<span class="mono">' + esc(data.install_dir || '-') + '</span>');
    kv('账号', (data.accounts || []).length
      ? esc((data.accounts || []).map((a) => a.uin + (a.nickname ? ' (' + a.nickname + ')' : '')).join(', '))
      : '<span class="muted">尚无在线账号</span>');
    kv('AstrBot 平台', esc(data.plugin && data.plugin.astrbot_platform ? data.plugin.astrbot_platform : '-'));
    kv('插件版本', esc(data.plugin && data.plugin.code_version ? data.plugin.code_version : '-'));

    $('status-kv').innerHTML = rows.join('');
  }

  function renderAccounts(data) {
    const host = $('account-list');
    const accounts = data.accounts || [];
    const connections = data.connections || [];
    if (!accounts.length) {
      host.innerHTML = '<div class="empty">还没有账号上线。请先启动 QQ 并在 QQ 客户端内完成登录' +
        (data.qq_processes && data.qq_processes.length ? '，然后点击「全部挂载」。' : '。') + '</div>';
      return;
    }
    host.innerHTML = accounts.map((account) => {
      const uin = String(account.uin || '');
      const conn = connections.find((item) => String(item.uin || '') === uin) || {};
      const adapters = conn.adapters || conn.clients || conn.connections || [];
      const online = conn.online === true || conn.connected === true || (Array.isArray(adapters) && adapters.length > 0);
      const nickname = account.nickname ? String(account.nickname) : '';
      const detailParts = [];
      if (nickname) detailParts.push(nickname);
      detailParts.push(online ? 'OneBot 适配器在线' : 'OneBot 适配器未连接');
      if (Array.isArray(adapters) && adapters.length) detailParts.push(adapters.length + ' 个连接');
      return '<div class="row">' +
        '<div class="who"><strong>' + esc(uin) + '</strong><span>' + esc(detailParts.join(' · ')) + '</span></div>' +
        '<div class="actions">' +
        '<span class="tag ' + (online ? 'ok' : 'warn') + '">' + (online ? '在线' : '离线') + '</span>' +
        '<button class="btn sm ghost" data-act="sync">同步</button>' +
        '</div></div>';
    }).join('');
  }

  function statusTagClass(status) {
    const value = String(status || '').toLowerCase();
    if (value === 'online' || value === 'loaded') return 'ok';
    if (value === 'error' || value === 'disconnected') return 'err';
    if (value === 'loading' || value === 'connecting') return 'warn';
    return '';
  }

  function renderQQ(data) {
    const host = $('qq-process-list');
    const processes = data.qq_processes || [];
    const qq = data.qq || {};
    $('qq-hint').textContent = qq.installed
      ? 'QQ ' + (qq.version || '已安装') + (qq.running_pids && qq.running_pids.length ? ' · ' + qq.running_pids.length + ' 个进程' : '')
      : '未检测到 QQ';

    if (!processes.length) {
      host.innerHTML = '<div class="empty">' + (qq.installed
        ? 'QQ 已安装但尚未运行，或 SnowLuma 还没发现进程。'
        : '未检测到 QQNT，请先安装 QQ 或在设置里填写路径。') + '</div>';
      return;
    }
    host.innerHTML = processes.map((info) => {
      const pid = Number(info.pid || 0);
      const status = String(info.status || 'available');
      const account = info.account && info.account.uin ? String(info.account.uin) : String(info.uin || '');
      const injected = !!info.injected;
      const method = info.method ? String(info.method) : '';
      return '<div class="row">' +
        '<div class="who"><strong>PID ' + pid + (account ? ' · ' + esc(account) : '') + '</strong>' +
        '<span>' + esc(shortText(info.path || info.name || '', 90)) + (method ? ' · ' + esc(method) : '') + '</span></div>' +
        '<div class="actions">' +
        '<span class="tag ' + statusTagClass(status) + '">' + esc(status) + '</span>' +
        (injected
          ? '<button class="btn sm ghost" data-act="hook-unload" data-pid="' + pid + '">卸载</button>'
          : '<button class="btn sm primary" data-act="hook-load" data-pid="' + pid + '">挂载</button>') +
        '</div></div>';
    }).join('');
  }

  function renderSync(data) {
    const syncer = data.syncer || {};
    const rows = [];
    const kv = (label, value) => rows.push('<dt>' + esc(label) + '</dt><dd>' + value + '</dd>');

    kv('AstrBot 机器人', syncer.astrbot_bot_found
      ? esc(syncer.astrbot_bot_name || syncer.astrbot_bot_id || '已找到') +
        ' <span class="tag ok">aiocqhttp</span>'
      : '<span class="err-text">未找到</span>');
    kv('监听地址', syncer.astrbot_bot_port
      ? '<span class="mono">' + esc((syncer.astrbot_bot_host || '') + ':' + syncer.astrbot_bot_port + (syncer.astrbot_bot_path || '')) + '</span>'
      : '<span class="muted">-</span>');
    kv('Access Token', syncer.astrbot_bot_token
      ? '<span class="mono">' + esc(syncer.astrbot_bot_token) + '</span>'
      : '<span class="muted">(空)</span>');
    kv('反向 WS', syncer.ws_url
      ? '<span class="mono">' + esc(syncer.ws_url) + '</span>'
      : '<span class="muted">尚未生成</span>');
    kv('目标账号', esc(syncer.uin || '-'));
    kv('最近同步', (syncer.last_sync_ok ? '<span class="ok-text">✔ </span>' : '<span class="warn-text">⚠ </span>') +
      esc(syncer.last_sync_msg || '尚未同步') +
      (syncer.last_sync_time ? '<div class="muted" style="font-size:11.5px">' + esc(syncer.last_sync_time) + '</div>' : '') +
      (syncer.last_applied ? '<span class="tag ok">已热重载</span>' : '') +
      (syncer.last_online === false ? '<span class="tag warn">待账号上线</span>' : ''));
    kv('配置文件', syncer.astrbot_config_path
      ? '<span class="mono">' + esc(syncer.astrbot_config_path) + '</span>'
      : '<span class="muted">未找到</span>');

    $('sync-kv').innerHTML = rows.join('');

    const scan = data.scan || [];
    $('scan-list').innerHTML = scan.length
      ? scan.map((item) => '<div class="row"><div class="who"><strong>' + esc(item.file) + '</strong>' +
          '<span>' + esc(item.dir) + '</span></div><span class="tag">' + esc(item.size) + ' B</span></div>').join('')
      : '<div class="empty">安装目录下还没有 OneBot 配置文件（首次同步后由 SnowLuma 创建）</div>';
  }

  function renderInstall(data) {
    const hint = [];
    if (data.installed_version) hint.push('已安装 ' + data.installed_version);
    if (data.installed_target) hint.push(data.installed_target);
    hint.push('镜像优先：' + (data.config && data.config.download_mirror ? data.config.download_mirror : '官方'));
    $('install-hint').textContent = hint.join(' · ');

    const progress = data.download || {};
    const box = $('download-progress');
    if (progress.downloading || (progress.phase && !['idle', 'done'].includes(progress.phase) && progress.phase !== 'failed')) {
      box.style.display = 'flex';
      const percent = Math.max(0, Math.min(100, Number(progress.percent) || 0));
      $('download-bar').style.width = percent + '%';
      $('download-phase').textContent = (progress.message || progress.phase || '') +
        (progress.phase === 'downloading' ? '（' + percent + '%）' : '');
      const pieces = [];
      if (progress.downloaded_mb) {
        pieces.push(progress.downloaded_mb + ' MB' + (progress.total_mb ? ' / ' + progress.total_mb + ' MB' : ''));
      }
      if (progress.speed_kbps) pieces.push(Math.round(progress.speed_kbps) + ' KB/s');
      if (progress.current_mirror) pieces.push('源: ' + progress.current_mirror);
      if (progress.current_index) pieces.push(progress.current_index + '/' + (progress.total_mirrors || '?'));
      $('download-detail').textContent = pieces.join(' · ');
      $('btn-install').disabled = true;
      $('btn-uninstall').disabled = true;
    } else if (progress.phase === 'failed') {
      box.style.display = 'flex';
      $('download-bar').style.width = '100%';
      $('download-bar').style.background = 'var(--danger)';
      $('download-phase').textContent = '安装失败：' + (progress.message || '未知错误');
      $('download-detail').textContent = '';
      $('btn-install').disabled = false;
      $('btn-uninstall').disabled = false;
    } else {
      box.style.display = 'none';
      $('download-bar').style.background = '';
      $('btn-install').disabled = false;
      $('btn-uninstall').disabled = false;
    }

    if (data.installed_version) {
      const option = '（当前 ' + data.installed_version + '）';
      const select = $('release-select');
      if (select.options.length && select.options[0].value === 'latest') {
        select.options[0].textContent = 'latest（最新版）' + option;
      }
    }

    const flavor = $('flavor-select');
    if (data.release_flavor && flavor.value !== data.release_flavor && !flavor.dataset.touched) {
      flavor.value = data.release_flavor;
    }
  }

  // ------------------------------------------------------------------ //
  // Releases
  // ------------------------------------------------------------------ //

  async function loadReleases(force) {
    try {
      const data = await apiGet('releases', force ? { force: 1 } : undefined);
      const select = $('release-select');
      const previous = select.value;
      const releases = (data && data.releases) || [];
      const options = ['<option value="latest">latest（最新版）</option>'];
      releases.forEach((release) => {
        const tags = [];
        if (release.prerelease) tags.push('预发布');
        if (release.has_full === false && release.has_lite === false) tags.push('无本平台包');
        const label = release.tag + (tags.length ? ' [' + tags.join('/') + ']' : '');
        options.push('<option value="' + esc(release.tag) + '">' + esc(label) + '</option>');
      });
      select.innerHTML = options.join('');
      if (previous && Array.from(select.options).some((opt) => opt.value === previous)) {
        select.value = previous;
      }
      state.releasesLoaded = true;
    } catch (error) {
      toast('获取版本列表失败：' + error.message, 'err', 5000);
    }
  }

  // ------------------------------------------------------------------ //
  // Logs
  // ------------------------------------------------------------------ //

  function classifyLine(line) {
    if (/\[err\]|✘|\berror\b|失败|异常|Traceback/i.test(line)) return 'err';
    if (/\[warn\]|⚠|warn|警告/i.test(line)) return 'warn';
    if (/\[ok\]|✔|成功|ready|已启动|已连接/i.test(line)) return 'ok';
    return '';
  }

  function lineHtml(index, line) {
    const text = String(line);
    const ts = text.match(/^\[(\d{2}:\d{2}:\d{2})\]\s*/);
    const body = ts ? text.slice(ts[0].length) : text;
    const cls = classifyLine(text);
    return '<div class="log-line ' + cls + '" data-i="' + index + '">' +
      (ts ? '<span class="ts">' + esc(ts[1]) + '</span> ' : '') + esc(body) + '</div>';
  }

  function renderLogs() {
    const view = $('log-view');
    view.innerHTML = state.currentLogHtml.join('');
    const auto = $('log-autoscroll');
    if (auto && auto.checked) view.scrollTop = view.scrollHeight;
    $('log-meta').textContent = state.logSource === 'process'
      ? state.logs.length + ' 行 · 来自插件进程管道'
      : state.serverLogEntries.length + ' 条 · 来自 SnowLuma /api/logs';
  }

  async function refreshLogs() {
    if (state.paused) return;
    if (state.logSource === 'server') {
      try {
        const data = await apiGet('server-logs', { limit: 200 });
        const list = (data && data.list) || [];
        state.serverLogEntries = list;
        state.currentLogHtml = list.map((entry, index) => {
          const level = String(entry.level || entry.levelName || 'info').toLowerCase();
          const time = entry.time || entry.timestamp || entry.ts || '';
          const message = entry.message || entry.msg || JSON.stringify(entry);
          const cls = level === 'error' ? 'err' : level === 'warn' ? 'warn' : '';
          return '<div class="log-line ' + cls + '">' +
            (time ? '<span class="ts">' + esc(String(time).slice(11, 19)) + '</span> ' : '') +
            esc(shortText(message, 2000)) + '</div>';
        });
        renderLogs();
      } catch (error) {
        /* keep quiet while polling */
      }
      return;
    }
    try {
      const data = await apiGet('logs', { since: state.logOffset });
      const lines = (data && data.lines) || [];
      if (lines.length) {
        lines.forEach((line) => state.logs.push(line));
        if (state.logs.length > MAX_LOG_LINES) state.logs = state.logs.slice(-MAX_LOG_LINES);
        state.currentLogHtml = state.logs.map((line, index) => lineHtml(index, line));
      }
      if (data && typeof data.total === 'number') state.logOffset = data.total;
      renderLogs();
    } catch (error) {
      /* keep quiet while polling */
    }
  }

  // ------------------------------------------------------------------ //
  // Settings
  // ------------------------------------------------------------------ //

  const SETTINGS_FIELDS = [
    ['auto_start', 'checkbox'],
    ['auto_sync', 'checkbox'],
    ['auto_hook', 'checkbox'],
    ['auto_start_qq', 'checkbox'],
    ['auto_accept_agreements', 'checkbox'],
    ['deploy_mode', 'value'],
    ['snowluma_port', 'number'],
    ['external_host', 'value'],
    ['external_port', 'number'],
    ['snowluma_version', 'value'],
    ['download_mirror', 'value'],
    ['qq_number', 'value'],
    ['qq_path', 'value'],
    ['astrbot_config_path', 'value'],
    ['ws_host_override', 'value'],
    ['sync_connection_name', 'value'],
  ];

  async function openSettings() {
    try {
      const config = await apiGet('config');
      SETTINGS_FIELDS.forEach(([key, kind]) => {
        const input = $('cfg-' + key);
        if (!input) return;
        const value = config[key];
        if (kind === 'checkbox') input.checked = !!value;
        else input.value = value == null ? '' : value;
      });
      const password = $('cfg-webui_password');
      password.value = '';
      password.type = 'password';
      $('settings-mask').classList.add('show');
    } catch (error) {
      toast('读取配置失败：' + error.message, 'err', 5000);
    }
  }

  async function saveSettings() {
    const payload = {};
    SETTINGS_FIELDS.forEach(([key, kind]) => {
      const input = $('cfg-' + key);
      if (!input) return;
      if (kind === 'checkbox') payload[key] = input.checked;
      else if (kind === 'number') payload[key] = Number(input.value) || 0;
      else payload[key] = input.value;
    });
    const password = $('cfg-webui_password').value.trim();
    if (password) payload.webui_password = password;

    const result = await call('save-settings', () => apiPost('config/save', payload));
    if (result && result.ok) {
      toast('设置已保存', 'ok');
      $('settings-mask').classList.remove('show');
      if (result.status) {
        state.status = result.status;
        renderAll(result.status);
      }
      scheduleStatus(600);
    } else if (result) {
      toast(result.message || '保存失败', 'err', 5000);
    }
  }

  async function revealPassword() {
    try {
      const data = await apiGet('password/reveal');
      const input = $('cfg-webui_password');
      if (data && data.password) {
        input.value = data.password;
        input.type = 'text';
        toast('已显示当前受管密码', 'ok');
      } else {
        toast('当前没有受管密码', 'warn');
      }
    } catch (error) {
      toast('读取密码失败：' + error.message, 'err');
    }
  }

  // ------------------------------------------------------------------ //
  // Actions
  // ------------------------------------------------------------------ //

  const ACTIONS = {
    async refresh() {
      await refreshStatus();
      toast('已刷新', 'ok', 1200);
    },
    async start() {
      const result = await call('start', () => apiPost('start'));
      if (!result) return;
      toast(result.message || (result.ok ? '已启动' : '启动失败'), result.ok ? 'ok' : 'err', 4000);
      await refreshStatus();
    },
    async stop() {
      if (!(await confirmDialog('停止 SnowLuma', '停止后 QQ 将断开与 AstrBot 的连接。确定停止？', '停止'))) return;
      const result = await call('stop', () => apiPost('stop'));
      if (result) toast(result.message || '已停止', 'ok');
      await refreshStatus();
    },
    async restart() {
      if (!(await confirmDialog('重启 SnowLuma', '重启会短暂中断 QQ 的消息收发。继续？', '重启'))) return;
      const result = await call('restart', () => apiPost('restart'));
      if (!result) return;
      toast(result.message || (result.ok ? '已重启' : '重启失败'), result.ok ? 'ok' : 'err', 4000);
      await refreshStatus();
    },
    async cleanup() {
      if (!(await confirmDialog('清理残留进程', '将停止由本插件启动的 SnowLuma 并清理安装目录内的残留 node 进程。继续？', '清理'))) return;
      const result = await call('cleanup', () => apiPost('cleanup'));
      if (result) toast(result.message || '已清理', result.ok ? 'ok' : 'err');
      await refreshStatus();
    },
    async sync() {
      const result = await call('sync', () => apiPost('sync'));
      if (!result) return;
      toast(result.message || (result.ok ? '同步完成' : '同步失败'), result.ok ? 'ok' : 'err', 5000);
      await refreshStatus();
    },
    async disconnect() {
      if (!(await confirmDialog('断开 AstrBot 接入', '将从 SnowLuma 移除本插件创建的 wsClients 连接（其他连接不受影响）。继续？', '断开'))) return;
      const result = await call('disconnect', () => apiPost('disconnect'));
      if (result) toast(result.message || '已断开', result.ok ? 'ok' : 'err');
      await refreshStatus();
    },
    async 'qq-launch'() {
      const result = await call('qq-launch', () => apiPost('qq/launch'));
      if (result) toast(result.message || '已发出启动指令', result.ok ? 'ok' : 'err', 4000);
      await refreshStatus();
    },
    async 'qq-stop'() {
      if (!(await confirmDialog('关闭 QQ', '只会关闭由本插件启动的 QQ 进程，不影响你自己启动的 QQ。继续？', '关闭'))) return;
      const result = await call('qq-stop', () => apiPost('qq/stop'));
      if (result) toast(result.message || '', result.ok ? 'ok' : 'warn');
      await refreshStatus();
    },
    async 'hook-all'() {
      const result = await call('hook-all', () => apiPost('hook/all'));
      if (result) toast(result.message || (result.ok ? '已挂载' : '挂载失败'), result.ok ? 'ok' : 'err', 4000);
      await refreshStatus();
    },
    async 'accept-agreements'() {
      const result = await call('accept-agreements', () => apiPost('accept-agreements'));
      if (result) toast(result.message || '', result.ok ? 'ok' : 'err', 4000);
      await refreshStatus();
    },
    async 'open-webui'() {
      const result = await call('open-webui', () => apiPost('open-webui'));
      if (!result || !result.url) return;
      showUrlDialog(result.url);
      try { window.open(result.url, '_blank', 'noopener,noreferrer'); } catch (error) { /* ignored */ }
    },
    async 'reload-releases'() {
      state.releasesLoaded = false;
      await loadReleases(true);
      toast('版本列表已刷新', 'ok', 1600);
    },
    async install() {
      const version = $('release-select').value;
      const flavor = $('flavor-select').value;
      if (!(await confirmDialog('安装 / 更新 SnowLuma',
        '将安装 ' + version + '（' + flavor + '）包。安装会替换程序文件，但保留 config/、data/、logs/ 等用户数据。继续？', '开始安装'))) return;
      const result = await call('install', () => apiPost('install', { version: version, flavor: flavor }));
      if (!result) return;
      toast(result.message || (result.ok ? '安装完成' : '安装失败'), result.ok ? 'ok' : 'err', 5000);
      scheduleStatus(500);
    },
    async uninstall() {
      if (!(await confirmDialog('卸载 SnowLuma', '将删除本插件管理的 SnowLuma 安装目录（含 config/ 与账号数据）。此操作不可撤销，继续？', '确认卸载'))) return;
      const result = await call('uninstall', () => apiPost('uninstall', { purge: true }));
      if (result) toast(result.message || (result.ok ? '已卸载' : '卸载失败'), result.ok ? 'ok' : 'err', 5000);
      await refreshStatus();
    },
    async 'update-check'() {
      const result = await call('update-check', () => apiGet('update/check'));
      if (!result) return;
      if (result.ok) {
        const info = result.info || {};
        const latest = info.latest || info.latestVersion || info.version || '';
        const current = result.current || '';
        if (latest && current && String(latest) !== String(current)) {
          toast('SnowLuma 有新版本：' + latest + '（当前 ' + current + '）', 'warn', 6000);
        } else {
          toast('SnowLuma 已是最新版本' + (latest ? '（' + latest + '）' : ''), 'ok', 4000);
        }
      } else {
        toast(result.message || '检查更新失败', 'err', 5000);
      }
    },
    async settings() { await openSettings(); },
    async 'close-settings'() { $('settings-mask').classList.remove('show'); },
    async 'save-settings'() { await saveSettings(); },
    async 'close-url'() { $('url-mask').classList.remove('show'); },
    async 'scroll-install'() {
      const card = $('release-select');
      if (card) card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    },
    async 'clear-logs'() {
      state.logs = [];
      state.currentLogHtml = [];
      state.logOffset = state.status ? (state.status.log_count || 0) : 0;
      renderLogs();
    },
  };

  async function hookAction(pid, action) {
    if (!pid) {
      toast('缺少 pid', 'err');
      return;
    }
    const result = await call(action === 'load' ? 'hook-load' : 'hook-unload', () =>
      apiPost(action === 'load' ? 'hook/load' : 'hook/unload', { pid: pid }));
    if (result) toast(result.message || (result.ok ? '操作完成' : '操作失败'), result.ok ? 'ok' : 'err');
    await refreshStatus();
  }

  // ------------------------------------------------------------------ //
  // Wiring
  // ------------------------------------------------------------------ //

  function bindEvents() {
    document.addEventListener('click', (event) => {
      const target = event.target.closest('[data-act]');
      if (!target) return;
      const action = target.dataset.act;
      if (!action) return;
      event.preventDefault();

      if (action === 'hook-load' || action === 'hook-unload') {
        hookAction(Number(target.dataset.pid || 0), action === 'hook-load' ? 'load' : 'unload');
        return;
      }
      const handler = ACTIONS[action];
      if (!handler) return;
      Promise.resolve(handler()).catch((error) => toast(String(error), 'err'));
    });

    document.querySelectorAll('.mask').forEach((mask) => {
      mask.addEventListener('click', (event) => {
        if (event.target === mask && mask.id !== 'confirm-mask') mask.classList.remove('show');
      });
    });

    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        document.querySelectorAll('.mask.show').forEach((mask) => {
          if (mask.id !== 'confirm-mask') mask.classList.remove('show');
        });
      }
    });

    $('btn-reveal-password').addEventListener('click', revealPassword);
    $('log-pause').addEventListener('change', (event) => {
      state.paused = event.target.checked;
      if (!state.paused) refreshLogs();
    });
    $('log-source').addEventListener('change', (event) => {
      state.logSource = event.target.value;
      state.currentLogHtml = [];
      if (state.logSource === 'process') {
        state.currentLogHtml = state.logs.map((line, index) => lineHtml(index, line));
        renderLogs();
      } else {
        refreshLogs();
      }
    });
    $('flavor-select').addEventListener('change', () => {
      $('flavor-select').dataset.touched = '1';
    });
    $('url-copy').addEventListener('click', async () => {
      const value = $('url-field').value;
      try {
        await navigator.clipboard.writeText(value);
        toast('已复制', 'ok', 1400);
      } catch (error) {
        $('url-field').select();
        document.execCommand('copy');
        toast('已复制', 'ok', 1400);
      }
    });
  }

  async function init() {
    bindEvents();

    if (!bridge) {
      $('subtitle').textContent = '请从 AstrBot 仪表盘的插件页面打开本页面';
      document.getElementById('banners').innerHTML =
        '<div class="banner err"><span class="grow">检测不到 AstrBot 插件桥接（AstrBotPluginPage）。' +
        '直接双击 index.html 打开无法调用后台接口，请从 AstrBot → 插件 → Snowluma Go → 打开页面。</span></div>';
      return;
    }

    try {
      state.context = await bridge.ready();
    } catch (error) {
      console.warn('[snowluma] bridge ready failed', error);
    }
    const context = state.context || {};
    if (context.pageTitle) document.title = context.pageTitle + ' · SnowLuma Go';
    if (typeof context.isDark === 'boolean') {
      document.documentElement.setAttribute('data-theme', context.isDark ? 'dark' : 'light');
    }
    if (context.displayName) {
      const subtitle = $('subtitle');
      if (!subtitle.textContent || subtitle.textContent === 'Loader…') subtitle.textContent = context.displayName;
    }
    if (bridge.onContext) {
      bridge.onContext((next) => {
        if (next && typeof next.isDark === 'boolean') {
          document.documentElement.setAttribute('data-theme', next.isDark ? 'dark' : 'light');
        }
      });
    }

    await refreshStatus();
    await refreshLogs();
    loadReleases(false);

    state.logTimer = setInterval(refreshLogs, LOG_INTERVAL);
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) {
        refreshStatus();
        refreshLogs();
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
