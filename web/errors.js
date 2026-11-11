(function () {
  'use strict';

  window.QQBotErrors = function (options) {
    var $ = function (id) { return document.getElementById(id); };
    var state = { days: 1, appid: '', view: 'recent', page: 1, pages: 1, serial: 0, busy: false, bots: [] };
    var traceTooltip = document.createElement('div');
    traceTooltip.id = 'errorTraceTooltip';
    traceTooltip.className = 'error-trace-tooltip';
    traceTooltip.setAttribute('role', 'tooltip');
    traceTooltip.hidden = true;
    document.body.appendChild(traceTooltip);
    var traceTarget = null;

    function hideTrace() {
      if (traceTarget) traceTarget.removeAttribute('aria-describedby');
      traceTarget = null;
      traceTooltip.hidden = true;
    }

    function showTrace(event) {
      var button = event.target.closest('[data-trace]');
      if (!button) return;
      hideTrace();
      traceTarget = button;
      button.setAttribute('aria-describedby', traceTooltip.id);
      traceTooltip.textContent = button.dataset.trace;
      traceTooltip.hidden = false;
      var rect = button.getBoundingClientRect();
      var width = traceTooltip.offsetWidth;
      var height = traceTooltip.offsetHeight;
      var center = rect.left + rect.width / 2;
      var left = Math.max(16, Math.min(center - width / 2, window.innerWidth - width - 16));
      var below = rect.top < height + 24;
      traceTooltip.classList.toggle('below', below);
      traceTooltip.style.left = left + 'px';
      traceTooltip.style.top = (below ? rect.bottom + 8 : rect.top - height - 8) + 'px';
      traceTooltip.style.setProperty('--trace-arrow', Math.max(12, Math.min(center - left, width - 12)) + 'px');
    }

    function copySelectedTrace(button) {
      var field = document.createElement('textarea');
      field.className = 'error-copy-field';
      field.value = button.dataset.trace;
      field.readOnly = true;
      document.body.appendChild(field);
      field.select();
      var copied = document.execCommand('copy');
      field.remove();
      button.focus({ preventScroll: true });
      return copied;
    }

    function esc(value) {
      return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
      });
    }

    function timeText(value) {
      return value ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '—';
    }

    function code(value) {
      return value ? '<span class="error-code">' + esc(value) + '</span>' : '—';
    }

    function botCell(row) {
      var bot = state.bots.find(function (item) { return item.appid === row.appid; });
      var name = row.bot_name || (bot && bot.bot_name) || row.appid;
      var avatar = bot && /^https?:\/\//i.test(bot.avatar) ? bot.avatar : '';
      return '<div class="error-bot"><span class="bot-avatar"><i>' + esc(name.slice(0, 1)) + '</i>'
        + (avatar ? '<img src="' + esc(avatar) + '" alt="" loading="lazy" referrerpolicy="no-referrer">' : '')
        + '</span><span><strong>' + esc(name) + '</strong><small>' + esc(row.bot_self_id || row.appid)
        + '</small></span></div>';
    }

    function recentRow(row) {
      var trace = row.trace_id ? '<button class="error-trace" type="button" aria-label="复制 trace_id：'
        + esc(row.trace_id) + '" data-trace="' + esc(row.trace_id) + '">'
        + esc(row.trace_id.length > 20 ? row.trace_id.slice(0, 10) + '…' + row.trace_id.slice(-6) : row.trace_id)
        + '</button>' : '—';
      var content = row.content ? '<button class="error-content-preview" type="button" aria-haspopup="dialog"'
        + ' aria-controls="errorContentDialog" aria-label="查看完整消息内容"><span class="error-content-text">'
        + esc(row.content) + '</span><span class="error-content-more">查看全文</span></button>' : '—';
      return '<tr><td class="error-endpoint"><b>' + esc(row.method) + '</b> <code>' + esc(row.path)
        + '</code><small>' + (row.status ? 'HTTP ' + esc(row.status) : '网络请求失败')
        + '</small></td><td>' + code(row.code) + '</td><td>' + code(row.err_code)
        + '</td><td class="error-message">' + esc(row.message) + '</td><td>' + trace + '</td><td>'
        + botCell(row) + '</td><td class="error-time">' + esc(timeText(row.created_at))
        + '</td><td class="error-content">' + content + '</td></tr>';
    }

    function typeRow(row, total) {
      return '<tr><td>' + code(row.code) + '</td><td>' + code(row.err_code)
        + '</td><td class="error-message">' + esc(row.message) + '</td><td class="error-number">'
        + esc(row.count) + '</td><td class="error-number">' + (total ? (row.count / total * 100).toFixed(1) : '0.0')
        + '%</td><td class="error-time">' + esc(timeText(row.last_seen)) + '</td></tr>';
    }

    function renderBots() {
      var root = $('errorBot');
      var focused = root.contains(document.activeElement);
      var choices = [['', '全部机器人']].concat(state.bots.map(function (bot) {
        return [bot.appid, bot.bot_name ? bot.bot_name + ' · ' + bot.appid : bot.appid];
      }));
      if (state.appid && !state.bots.some(function (bot) { return bot.appid === state.appid; })) {
        choices.push([state.appid, state.appid]);
      }
      root.classList.remove('open');
      window.QQBotSelect(root, choices, state.appid, function (appid) {
        state.appid = appid;
        renderBots();
        queryFirstPage();
      }, '机器人', false);
      if (focused) root.querySelector('.select-btn').focus({ preventScroll: true });
    }

    function pager() {
      $('errorPrev').disabled = state.busy || state.page <= 1;
      $('errorNext').disabled = state.busy || state.page >= state.pages;
    }

    function render(data) {
      state.bots = data.bots;
      state.page = data.page;
      state.pages = Math.max(1, Math.ceil(data.total / data.page_size));
      renderBots();
      var bot = state.bots.find(function (item) { return item.appid === data.appid; });
      var label = data.days === 1 ? '今日' : '近 ' + data.days + ' 天';
      label += ' · ' + (data.appid ? (bot && bot.bot_name) || data.appid : '全部机器人');
      $('errorSummary').textContent = label + ' · 共 ' + data.total_errors + ' 条报错'
        + (state.view === 'types' ? ' · ' + data.total + ' 种错误' : '');
      $('errorPageInfo').textContent = data.total ? '第 ' + state.page + ' / ' + state.pages + ' 页' : '';
      if (!data.items.length) {
        $('errorResults').innerHTML = '<div class="empty">所选范围暂无错误记录</div>';
        return;
      }
      var recent = state.view === 'recent';
      var headers = recent ? ['报错接口', 'code', 'err_code', '报错信息', 'trace_id', 'Bot', '时间', '消息摘要']
        : ['code', 'err_code', '报错信息', '次数', '占比', '最近出现'];
      $('errorResults').innerHTML = '<div class="error-table-scroll" tabindex="0" role="region" aria-label="'
        + (recent ? '最近报错明细，可横向滚动' : '按次数降序排列的报错类型，可横向滚动')
        + '"><table class="error-table ' + (recent ? 'error-recent' : 'error-types') + '"><thead><tr>'
        + headers.map(function (name) { return '<th scope="col">' + name + '</th>'; }).join('')
        + '</tr></thead><tbody>' + data.items.map(function (row) {
          return recent ? recentRow(row) : typeRow(row, data.total_errors);
        }).join('') + '</tbody></table></div>';
    }

    async function load() {
      hideTrace();
      var serial = ++state.serial;
      state.busy = true;
      pager();
      $('errorResults').setAttribute('aria-busy', 'true');
      $('errorResults').innerHTML = '<div class="empty">正在读取错误记录…</div>';
      $('errorSummary').textContent = '正在查询…';
      $('errorPageInfo').textContent = '';
      var query = new URLSearchParams({ days: state.days, appid: state.appid, view: state.view, page: state.page, page_size: 20 });
      try {
        var data = await options.api('/errors?' + query.toString());
        if (serial !== state.serial) return;
        render(data);
      } catch (error) {
        if (serial !== state.serial) return;
        state.pages = 1;
        state.page = 1;
        $('errorSummary').textContent = '查询失败';
        $('errorResults').innerHTML = '<div class="empty" role="alert">' + esc(error.message || '读取失败')
          + '。请点击查询重试。</div>';
      } finally {
        if (serial === state.serial) {
          state.busy = false;
          $('errorResults').setAttribute('aria-busy', 'false');
          pager();
        }
      }
    }

    function choose(container, attribute, value) {
      container.querySelectorAll('button').forEach(function (button) {
        var active = button.dataset[attribute] === String(value);
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', String(active));
      });
    }

    function queryFirstPage() {
      state.page = 1;
      load();
    }

    $('errorFilters').addEventListener('submit', function (event) { event.preventDefault(); queryFirstPage(); });
    $('errorDays').addEventListener('click', function (event) {
      var button = event.target.closest('[data-days]');
      if (!button) return;
      state.days = Number(button.dataset.days);
      choose($('errorDays'), 'days', state.days);
      queryFirstPage();
    });
    $('errorViews').addEventListener('click', function (event) {
      var button = event.target.closest('[data-view]');
      if (!button) return;
      state.view = button.dataset.view;
      choose($('errorViews'), 'view', state.view);
      queryFirstPage();
    });
    $('errorPrev').addEventListener('click', function () { if (!state.busy && state.page > 1) { state.page--; load(); } });
    $('errorNext').addEventListener('click', function () { if (!state.busy && state.page < state.pages) { state.page++; load(); } });
    $('errorResults').addEventListener('error', function (event) {
      if (event.target.tagName === 'IMG') event.target.remove();
    }, true);
    $('errorResults').addEventListener('click', function (event) {
      var button = event.target.closest('.error-content-preview');
      if (!button) return;
      hideTrace();
      $('errorContentBody').textContent = button.querySelector('.error-content-text').textContent;
      $('errorContentBody').scrollTop = 0;
      $('errorContentDialog').showModal();
    });
    $('closeErrorContent').addEventListener('click', function () { $('errorContentDialog').close(); });
    $('errorContentDialog').addEventListener('click', function (event) {
      if (event.target !== this) return;
      var rect = this.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) {
        this.close();
      }
    });
    $('errorContentDialog').addEventListener('close', function () { $('errorContentBody').textContent = ''; });
    $('errorResults').addEventListener('pointerover', showTrace);
    $('errorResults').addEventListener('pointerout', hideTrace);
    $('errorResults').addEventListener('focusin', showTrace);
    $('errorResults').addEventListener('focusout', hideTrace);
    document.addEventListener('scroll', hideTrace, true);
    window.addEventListener('resize', hideTrace);
    document.addEventListener('keydown', function (event) { if (event.key === 'Escape') hideTrace(); });
    $('errorResults').addEventListener('click', async function (event) {
      var button = event.target.closest('[data-trace]');
      if (!button) return;
      var copied = false;
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(button.dataset.trace);
          copied = true;
        }
      } catch (error) {
        copied = false;
      }
      if (!copied) copied = copySelectedTrace(button);
      options.toast(copied ? '已复制 trace_id' : '复制失败，请检查浏览器剪贴板权限', !copied);
    });
    renderBots();
    return { open: queryFirstPage };
  };
})();
