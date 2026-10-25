(function () {
  'use strict';

  var SDK = window.GsHubPlugin;
  var API_BASE = '/api/qqbotuid';
  var POLL_INTERVAL_MS = 2000;
  var state = {
    bots: [],
    saved: '',
    expanded: new Set(),
  };
  var bind = {
    taskId: '',
    timer: null,
    busy: false,
  };
  var detail = {
    appid: '',
  };

  var $ = function (id) { return document.getElementById(id); };
  function on(id, type, handler, capture) {
    var el = $(id);
    if (el) el.addEventListener(type, handler, capture === true);
  }
  var botList = $('botList');
  var saveDock = $('saveDock');
  var toastEl = $('toast');

  function escapeHtml(value) {
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function token() {
    if (SDK && SDK.token) return SDK.token;
    try {
      return localStorage.getItem('auth_token') || localStorage.getItem('token') || '';
    } catch (err) {
      return '';
    }
  }

  async function request(path, options) {
    var opts = options || {};
    var headers = Object.assign({}, opts.headers || {});
    if (opts.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';
    var auth = token();
    if (auth && !headers.Authorization) headers.Authorization = 'Bearer ' + auth;
    var response = await fetch(API_BASE + path, Object.assign({}, opts, { headers: headers, credentials: 'same-origin' }));
    var body = await response.json().catch(function () { return {}; });
    if (response.status === 401) throw new Error('未授权，请先在 WebConsole 登录');
    if (!response.ok || body.status !== 0) throw new Error(body.msg || '请求失败（' + response.status + '）');
    return body.data;
  }

  function call(path, options) {
    var opts = Object.assign({}, options || {});
    if (opts.body) {
      opts.headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    }
    return SDK && SDK.api ? SDK.api(API_BASE + path, opts) : request(path, opts);
  }

  var api = call;

  function showToast(text, isError) {
    toastEl.textContent = text || '';
    toastEl.classList.toggle('error', Boolean(isError));
    toastEl.classList.add('show');
    clearTimeout(showToast.timer);
    showToast.timer = setTimeout(function () { toastEl.classList.remove('show'); }, 2400);
  }

  function snapshot() {
    return JSON.stringify(state.bots.map(function (bot) {
      return {
        appid: bot.appid || '',
        enabled: Boolean(bot.enabled),
        mode: bot.mode || 'WebSocket',
      };
    }));
  }

  function updateDirty() {
    var dirty = state.saved !== '' && snapshot() !== state.saved;
    saveDock.classList.toggle('hidden', !dirty);
  }

  function renderCount() {
    var enabled = state.bots.filter(function (bot) { return bot.enabled; }).length;
    $('botCount').textContent = enabled + ' 个已启用';
    $('footerStatus').textContent = state.bots.length
      ? '已启用 ' + enabled + ' / ' + state.bots.length + ' 个 Bot'
      : '还没有配置 Bot';
  }

  function modeTag(mode) {
    return '<span class="tag">' + escapeHtml(mode || 'WebSocket') + '</span>';
  }

  var MODES = ['WebSocket', 'Webhook'];

  function modeSelect(mode) {
    var current = MODES.indexOf(mode) >= 0 ? mode : MODES[0];
    var items = MODES.map(function (value) {
      return '<li role="option" data-value="' + value + '" aria-selected="' + (value === current) + '">'
        + value + '</li>';
    }).join('');
    return '<div class="select" data-select="mode">' +
      '  <button class="select-btn" type="button" aria-haspopup="listbox" aria-expanded="false">' +
      '    <span class="select-value">' + escapeHtml(current) + '</span>' +
      '    <span class="select-caret" aria-hidden="true"></span>' +
      '  </button>' +
      '  <ul class="select-menu" role="listbox">' + items + '</ul>' +
      '</div>';
  }

  function renderBots() {
    if (!state.bots.length) {
      botList.innerHTML = '<div class="empty">还没有 Bot。点右上角「扫码添加」用手机 QQ 授权，'
        + '扫完自动写入 AppID / Secret 并接入核心。</div>';
      renderCount();
      updateDirty();
      return;
    }

    botList.innerHTML = state.bots.map(function (bot, index) {
      var name = bot.bot_name || bot.appid || '未命名 Bot';
      var initial = String(name).trim().slice(0, 1).toUpperCase() || 'Q';
      var expanded = state.expanded.has(bot.appid || index);
      var avatar = '<span class="bot-avatar">' +
        (bot.avatar ? '<img src="' + escapeHtml(bot.avatar) + '" alt="" loading="lazy" referrerpolicy="no-referrer" />' : '') +
        '<i>' + escapeHtml(initial) + '</i></span>';
      return '' +
        '<article class="bot-card' + (expanded ? ' expanded' : '') + '" data-index="' + index + '">' +
        avatar +
        '  <div>' +
        '    <div class="bot-title">' + escapeHtml(name) +
        '      <span class="tag ' + (bot.enabled ? 'enabled' : 'disabled') + '">' + (bot.enabled ? '已启用' : '已停用') + '</span>' +
        modeTag(bot.mode) +
        '    </div>' +
        '    <div class="bot-meta">' +
        '      <span>AppID: <code>' + escapeHtml(bot.appid || '--') + '</code></span>' +
        (bot.uin
          ? '      <span>QQ: <code>' + escapeHtml(bot.uin) + '</code></span>'
          : '') +
        '    </div>' +
        '  </div>' +
        '  <div class="bot-actions">' +
        '    <button class="settings-btn' + (expanded ? ' active' : '') + '" type="button" data-action="settings" title="连接设置" aria-label="连接设置">⚙</button>' +
        '    <label class="toggle"><input type="checkbox" data-field="enabled"' + (bot.enabled ? ' checked' : '') + ' /><i></i></label>' +
        '  </div>' +
        '  <div class="bot-options">' +
        '    <div class="field"><span>运行模式</span>' + modeSelect(bot.mode) + '</div>' +
        '  </div>' +
        '</article>';
    }).join('');

    renderCount();
    updateDirty();
  }

  async function load(silent) {
    var button = $('refreshBtn');
    button.disabled = true;
    try {
      var data = await api('/bots');
      state.bots = (data && data.bots) || [];
      state.saved = snapshot();
      renderBots();
      if (!silent) showToast('已刷新');
    } catch (err) {
      showToast(err.message || '读取失败', true);
    } finally {
      button.disabled = false;
    }
  }

  function closeSelects(except) {
    Array.prototype.forEach.call(botList.querySelectorAll('.select.open'), function (el) {
      if (el === except) return;
      el.classList.remove('open');
      var btn = el.querySelector('.select-btn');
      if (btn) btn.setAttribute('aria-expanded', 'false');
    });
  }

  function handleSelectClick(event) {
    var box = event.target.closest('.select');
    if (!box) return false;
    var btn = event.target.closest('.select-btn');
    if (btn) {
      var open = !box.classList.contains('open');
      closeSelects(box);
      box.classList.toggle('open', open);
      btn.setAttribute('aria-expanded', String(open));
      return true;
    }
    var option = event.target.closest('li[data-value]');
    if (!option) return true;
    var card = box.closest('.bot-card');
    var bot = card && state.bots[Number(card.dataset.index)];
    if (bot) {
      bot.mode = option.dataset.value;
      box.querySelector('.select-value').textContent = bot.mode;
      Array.prototype.forEach.call(box.querySelectorAll('li'), function (li) {
        li.setAttribute('aria-selected', String(li === option));
      });
      updateDirty();
    }
    closeSelects(null);
    return true;
  }

  on('botList', 'click', function (event) {
    var card = event.target.closest('.bot-card');
    if (!card) return;
    var bot = state.bots[Number(card.dataset.index)];
    if (handleSelectClick(event)) return;
    if (event.target.closest('[data-action="settings"]')) {
      var key = bot && (bot.appid || card.dataset.index);
      if (state.expanded.has(key)) state.expanded.delete(key);
      else state.expanded.add(key);
      card.classList.toggle('expanded');
      event.target.closest('button').classList.toggle('active', card.classList.contains('expanded'));
      return;
    }
    if (event.target.closest('input, label, button, a, .field')) return;
    if (bot) openDetail(bot.appid);
  });

  on('botList', 'input', function (event) {
    var field = event.target.dataset.field;
    if (!field) return;
    var card = event.target.closest('.bot-card');
    if (!card) return;
    var bot = state.bots[Number(card.dataset.index)];
    if (!bot) return;
    bot[field] = event.target.type === 'checkbox' ? event.target.checked : event.target.value;
    if (field === 'enabled') renderBots();
    else updateDirty();
  });

  on('botList', 'error', function (event) {
    var img = event.target;
    if (img && img.tagName === 'IMG') img.remove();
  }, true);

  on('refreshBtn', 'click', function () { load(false); });

  on('saveBtn', 'click', async function () {
    var button = $('saveBtn');
    button.disabled = true;
    try {
      var data = await api('/bots', { method: 'POST', body: JSON.stringify({ bots: state.bots }) });
      state.bots = (data && data.bots) || state.bots;
      state.saved = snapshot();
      renderBots();
      showToast(data && data.reloaded === false ? '已保存，重启核心后生效' : '已保存并生效');
    } catch (err) {
      showToast(err.message || '保存失败', true);
    } finally {
      button.disabled = false;
    }
  });

  function detailLine(label, value) {
    if (!value) return '';
    return '<span class="detail-line"><i>' + escapeHtml(label) + '</i><code>' + escapeHtml(value) + '</code></span>';
  }

  function renderDetail(bot) {
    var name = bot.bot_name || bot.appid || '未命名 Bot';
    var initial = String(name).trim().slice(0, 1).toUpperCase() || 'Q';
    var lines = detailLine('AppID', bot.appid)
      + detailLine('QQ', bot.uin)
      + detailLine('Bot ID', bot.bot_id)
      + detailLine('Union OpenID', bot.union_openid);
    var qrs = (bot.qrcodes || []).filter(function (item) { return item.qr; }).map(function (item) {
      return '<div class="detail-qr-item">' +
        '<span class="detail-qr-label">' + escapeHtml(item.label) + '</span>' +
        '<img class="detail-qr-img" src="' + escapeHtml(item.qr) + '" alt="' + escapeHtml(item.label) + '二维码" />' +
        '<a class="detail-qr-link" href="' + escapeHtml(item.url) + '" target="_blank" rel="noreferrer">打开链接</a>' +
        '</div>';
    }).join('');
    return '' +
      '<div class="detail-head">' +
      '  <span class="detail-avatar">' +
      (bot.avatar
        ? '<img class="detail-avatar-img" src="' + escapeHtml(bot.avatar) + '" alt="" referrerpolicy="no-referrer" />'
        : '') +
      '    <i>' + escapeHtml(initial) + '</i>' +
      '  </span>' +
      '  <div class="detail-ident">' +
      '    <h3>' + escapeHtml(name) + '</h3>' +
      lines +
      '  </div>' +
      '</div>' +
      (bot.description ? '<p class="detail-desc">' + escapeHtml(bot.description) + '</p>' : '') +
      (qrs
        ? '<section class="detail-qr"><h4>二维码</h4><div class="detail-qr-grid">' + qrs + '</div></section>'
        : '');
  }

  function setDetailBody(html) {
    $('detailBody').innerHTML = html;
  }

  async function openDetail(appid) {
    if (!appid) return;
    detail.appid = appid;
    $('detailModal').hidden = false;
    document.body.classList.add('modal-open');
    setDetailBody('<div class="detail-loading">正在读取机器人资料…</div>');
    try {
      var data = await api('/bots/' + encodeURIComponent(appid));
      if (detail.appid !== appid) return;
      setDetailBody(renderDetail(data || {}));
    } catch (err) {
      if (detail.appid !== appid) return;
      setDetailBody('<div class="detail-loading">读取失败：' + escapeHtml(err.message || '未知错误') + '</div>');
    }
  }

  function closeDetail() {
    detail.appid = '';
    $('detailModal').hidden = true;
    document.body.classList.remove('modal-open');
  }
  function setBindStatus(text, tone) {
    var el = $('bindStatus');
    el.textContent = text || '';
    el.className = 'bind-status' + (tone ? ' ' + tone : '');
  }

  function stopPolling() {
    if (bind.timer) {
      clearInterval(bind.timer);
      bind.timer = null;
    }
  }

  function resetBindView() {
    bind.taskId = '';
    $('qrImg').hidden = true;
    $('qrImg').removeAttribute('src');
    $('qrImg').classList.remove('stale');
    $('bindPlaceholder').hidden = false;
    $('bindPlaceholder').textContent = '正在生成二维码…';
    $('bindLink').hidden = true;
    $('bindLink').removeAttribute('href');
    $('regenBind').disabled = true;
  }

  async function pollBind() {
    if (!bind.taskId || bind.busy) return;
    bind.busy = true;
    try {
      var data = await api('/bind/poll', { method: 'POST', body: JSON.stringify({ task_id: bind.taskId }) });
      if (data.status === 'completed') {
        stopPolling();
        bind.taskId = '';
        var extra = data.reloaded === false ? '，请重启核心后生效' : '，已在核心中生效';
        setBindStatus('绑定成功：' + data.appid + extra, 'done');
        showToast((data.created ? '已添加机器人 ' : '已更新机器人 ') + data.appid);
        await load(true);
        setTimeout(function () {
          closeBind();
          load(true);
        }, 1600);
      } else if (data.status === 'expired' || data.status === 'not_found') {
        stopPolling();
        bind.taskId = '';
        $('qrImg').classList.add('stale');
        setBindStatus(data.message || '二维码已过期，请重新生成', 'error');
        $('regenBind').disabled = false;
      } else if (data.status === 'error') {
        stopPolling();
        bind.taskId = '';
        setBindStatus(data.message || '查询绑定结果失败', 'error');
        $('regenBind').disabled = false;
      }
    } catch (err) {
      setBindStatus(err.message || '查询失败，稍后自动重试', 'error');
    } finally {
      bind.busy = false;
    }
  }

  async function startBind() {
    stopPolling();
    resetBindView();
    setBindStatus('正在向 QQ 申请绑定二维码…');
    try {
      var data = await api('/bind/create', { method: 'POST' });
      bind.taskId = data.task_id;
      $('qrImg').src = data.qr_data_url;
      $('qrImg').hidden = false;
      $('bindPlaceholder').hidden = true;
      $('bindLink').href = data.url;
      $('bindLink').hidden = false;
      $('regenBind').disabled = false;
      setBindStatus('等待扫码：用手机 QQ 扫描上面的二维码并在手机上确认', 'waiting');
      bind.timer = setInterval(pollBind, POLL_INTERVAL_MS);
    } catch (err) {
      $('bindPlaceholder').textContent = '二维码生成失败';
      setBindStatus(err.message || '生成二维码失败', 'error');
      $('regenBind').disabled = false;
    }
  }

  function openBind() {
    $('bindModal').hidden = false;
    document.body.classList.add('modal-open');
    startBind();
  }

  function closeBind() {
    stopPolling();
    bind.taskId = '';
    $('bindModal').hidden = true;
    document.body.classList.remove('modal-open');
  }

  on('openBindBtn', 'click', openBind);
  on('regenBind', 'click', startBind);
  on('closeBind', 'click', closeBind);
  on('bindModal', 'click', function (event) {
    if (event.target.dataset.close) closeBind();
  });
  on('closeDetail', 'click', closeDetail);
  on('detailModal', 'click', function (event) {
    if (event.target.dataset.close) closeDetail();
  });
  on('detailBody', 'error', function (event) {
    var img = event.target;
    if (img && img.tagName === 'IMG' && img.classList.contains('detail-avatar-img')) img.remove();
  }, true);
  document.addEventListener('click', function (event) {
    if (!event.target.closest('.select')) closeSelects(null);
  });
  document.addEventListener('keydown', function (event) {
    if (event.key !== 'Escape') return;
    if (!$('bindModal').hidden) closeBind();
    else if (!$('detailModal').hidden) closeDetail();
    else closeSelects(null);
  });

  function boot() {
    load(true);
  }

  if (SDK && SDK.ready) SDK.ready.then(boot, boot);
  else boot();
})();
