(function () {
  'use strict';

  var scenes = { c2c: '单聊', group: '群聊', channel: '文字子频道', dm: '频道私信' };
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  };
  function width(text) {
    return Array.from(text).reduce(function (sum, ch) { return sum + (ch.codePointAt(0) > 127 ? 2 : 1); }, 0);
  }
  function unwrap(data) {
    if (data && !Array.isArray(data) && data.data && !data.panel && !data.items && !data.records && !data.panels) return data.data;
    return data;
  }
  function listOf(data) {
    data = unwrap(data);
    if (Array.isArray(data)) return data;
    if (data && typeof data === 'object') {
      var listKeys = ['records', 'panels', 'items', 'list'];
      for (var key of listKeys) {
        if (Array.isArray(data[key])) return data[key];
      }
      if (data.is_end === true && !listKeys.some(function (key) {
        return Object.prototype.hasOwnProperty.call(data, key);
      })) return [];
    }
    throw new Error('QQ 未返回可识别的面板列表，请刷新后重试');
  }
  function idOf(data) { return data ? String(data.panel_id || data.id || '') : ''; }
  function nextCursor(data) {
    data = unwrap(data);
    if (data && data.is_end === true) return '';
    return data && !Array.isArray(data) ? String(data.next_cursor || '') : '';
  }
  function splitIds(text) {
    var ids = Array.from(new Set(text.split(/[\s,，]+/).map(function (s) { return s.trim(); }).filter(Boolean)));
    if (!ids.length || ids.length > 20) throw new Error('请填写 1 到 20 个 OpenID');
    return ids;
  }

  function select(root, choices, value, change, label, disabled) {
    root.classList.add('select', 'cp-select');
    root.dataset.value = value;
    var selected = choices.find(function (choice) { return choice[0] === value; });
    root.innerHTML = '<button type="button" class="select-btn" aria-haspopup="listbox" aria-expanded="false" aria-label="' + esc(label) + '"' + (disabled ? ' disabled' : '') + '><span class="select-value">' + esc(selected ? selected[1] : '请选择') + '</span><span class="select-caret" aria-hidden="true"></span></button>'
      + '<ul class="select-menu" role="listbox" aria-label="' + esc(label) + '">' + choices.map(function (choice) {
        return '<li role="presentation"><button type="button" role="option" aria-selected="' + (value === choice[0]) + '" data-value="' + esc(choice[0]) + '">' + esc(choice[1]) + '</button></li>';
      }).join('') + '</ul>';
    root.onclick = function (event) {
      var button = event.target.closest('button');
      if (!button || button.disabled) return;
      if (button.classList.contains('select-btn')) {
        var open = !root.classList.contains('open');
        closeSelects();
        root.classList.toggle('open', open);
        button.setAttribute('aria-expanded', String(open));
      } else if (button.dataset.value !== undefined) {
        closeSelects();
        change(button.dataset.value);
        root.querySelector('.select-btn').focus();
      }
    };
    root.onkeydown = function (event) {
      if (event.key === 'Escape') { closeSelects(); root.querySelector('.select-btn').focus(); }
      if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
      event.preventDefault();
      root.classList.add('open');
      root.querySelector('.select-btn').setAttribute('aria-expanded', 'true');
      var options = Array.from(root.querySelectorAll('[role="option"]'));
      var index = options.indexOf(document.activeElement);
      if (options.length) options[(index + (event.key === 'ArrowDown' ? 1 : options.length - 1)) % options.length].focus();
    };
  }
  function closeSelects() {
    document.querySelectorAll('.cp-select.open').forEach(function (root) {
      root.classList.remove('open');
      root.querySelector('.select-btn').setAttribute('aria-expanded', 'false');
    });
  }

  window.QQBotPanels = function (ui) {
    var state = { bots: [], appid: '', scope: 'c2c', list: [], cursor: '', id: '', version: null, target: 'all', saved: '', busy: false, loaded: false, needsReload: false };
    var copyScope = 'c2c';
    var dragRow = null;
    var menu = window.QQBotMenuEditor({ select: select, escapeHtml: esc });
    function isMenu() { return state.scope === 'menu'; }
    function scopeName() { return isMenu() ? '全局自定义菜单' : scenes[state.scope]; }
    function menuPath() { return '/bots/' + encodeURIComponent(state.appid) + '/menu'; }
    function notice(text, error) {
      if (text) ui.toast(text, error);
    }
    function base() { return '/bots/' + encodeURIComponent(state.appid) + '/panels'; }
    function itemPath(id) { return base() + '/' + encodeURIComponent(id); }
    async function work(task) {
      if (state.busy) return;
      state.busy = true;
      $('panelControls').disabled = true;
      closeSelects();
      try { await task(); }
      catch (error) { notice(error.message || '请求失败', true); }
      finally { state.busy = false; $('panelControls').disabled = false; syncButtons(); }
    }
    function rows() { return Array.from($('panelItems').children); }
    function items() {
      return rows().map(function (row) {
        var type = row.querySelector('.cp-select').dataset.value;
        var item = { type: type, name: row.querySelector('[data-field="name"]').value.trim(), desc: row.querySelector('[data-field="desc"]').value.trim(), only_admin: row.querySelector('[data-field="only_admin"]').checked };
        if (type === 'link') item.link = row.querySelector('[data-field="link"]').value.trim();
        return item;
      });
    }
    function snapshot() { return JSON.stringify({ items: items(), remark: $('panelRemark').value, target: state.target, ids: $('panelTargetIds').value }); }
    function dirty() { return isMenu() ? menu.dirty() : state.saved !== '' && state.saved !== snapshot(); }
    function syncButtons() {
      $('panelSave').disabled = !state.appid || state.needsReload;
      $('panelNew').disabled = !state.appid;
      $('panelAddItem').disabled = rows().length >= 20 || !state.appid;
      $('panelRefresh').disabled = !state.appid;
      $('panelCopyToggle').disabled = !state.appid;
    }
    function changed() {
      var hasChanges = dirty();
      $('panelDirty').hidden = !hasChanges;
      $('panelSaveDock').hidden = !hasChanges;
      $('panelRemarkCount').textContent = width($('panelRemark').value) + ' / 255';
      $('panelRemarkCount').classList.toggle('over-limit', width($('panelRemark').value) > 255);
      $('panelItemCount').textContent = rows().length + ' / 20';
      rows().forEach(function (row, index) {
        row.querySelector('.panel-item-number').textContent = String(index + 1).padStart(2, '0');
        row.querySelector('[data-action="up"]').disabled = index === 0;
        row.querySelector('[data-action="down"]').disabled = index === rows().length - 1;
        ['name', 'desc'].forEach(function (field) {
          var input = row.querySelector('[data-field="' + field + '"]');
          var count = row.querySelector('[data-count="' + field + '"]');
          var limit = field === 'name' ? 15 : 30;
          count.textContent = width(input.value) + ' / ' + limit;
          count.classList.toggle('over-limit', width(input.value) > limit);
        });
      });
      syncButtons();
    }
    function guard(task) {
      if (state.busy) return;
      if (!dirty() && !$('panelManageIds').value.trim()) { task(); return; }
      ui.confirm({ title: '放弃未提交的修改？', text: '当前配置有未提交的修改或关联对象输入。继续后这些内容会丢失，QQ 端配置不会改变。', okText: '放弃修改', onOk: task });
    }
    function reset() {
      menu.reset(); state.loaded = false;
      $('panelWorkspace').hidden = isMenu();
      $('globalMenuEditor').hidden = !isMenu();
      $('panelLimits').hidden = isMenu();
      $('panelRefresh').textContent = isMenu() ? '刷新菜单' : '刷新列表';
      state.id = ''; state.version = null; state.target = 'all'; state.needsReload = false;
      $('panelRemark').value = ''; $('panelTargetIds').value = ''; $('panelManageIds').value = '';
      $('panelItems').replaceChildren(); addItem();
      $('panelIdentity').textContent = '新建于「' + scopeName() + '」场景';
      $('panelEditorTitle').textContent = '新建面板';
      $('panelDelete').hidden = true; $('panelTargetsManager').hidden = true;
      closeCopy(); renderTarget(); state.saved = snapshot(); changed(); renderList();
    }
    function renderBots() {
      var options = state.bots.filter(function (bot) { return bot.secret_set; }).map(function (bot) { return [bot.appid, (bot.bot_name || '未命名 Bot') + ' · ' + bot.appid]; });
      select($('panelBotSelect'), options.length ? options : [['', '没有已绑定的机器人']], state.appid, function (appid) {
        if (appid === state.appid) return;
        guard(function () { state.appid = appid; state.list = []; state.cursor = ''; renderBots(); reset(); return refresh(); });
      }, '机器人', !options.length);
    }
    function renderTarget() {
      var specific = state.scope === 'c2c' || state.scope === 'group';
      select($('panelTargetSelect'), [['all', '全局生效'], ['specific', state.scope === 'group' ? '指定群' : '指定用户']], state.target, function (value) { state.target = value; renderTarget(); changed(); }, '作用范围', Boolean(state.id) || !specific);
      $('panelCreateTargets').hidden = Boolean(state.id) || state.target !== 'specific';
      $('panelCreateTargetsLabel').textContent = state.scope === 'group' ? '群 OpenID' : '用户 OpenID';
      $('panelManageTargetsLabel').textContent = state.scope === 'group' ? '本次操作的群 OpenID' : '本次操作的用户 OpenID';
    }
    function renderList() {
      $('panelList').innerHTML = state.list.length ? state.list.map(function (entry) {
        var panel = entry.panel || entry;
        var id = idOf(entry);
        return '<button type="button" class="panel-list-item' + (state.id === id ? ' active' : '') + '" data-id="' + esc(id) + '"><strong>' + esc(panel.remark || id || '未命名面板') + '</strong><small>' + esc(id) + '</small><span class="tag">' + (entry.target_type === 'specific' ? '指定对象' : '全局') + '</span>' + (Array.isArray(panel.items) ? '<small>' + panel.items.length + ' 个元素</small>' : '') + '</button>';
      }).join('') : '<div class="empty">' + (state.appid ? '这个场景还没有面板，点击「新建」开始配置。' : '请先在 Bot 管理中扫码绑定机器人。') + '</div>';
      $('panelMore').hidden = !state.cursor;
    }
    async function loadList(more) {
      var cursor = more ? state.cursor : '';
      var data = await ui.api(base() + '?scope=' + state.scope + '&limit=20' + (cursor ? '&cursor=' + encodeURIComponent(cursor) : ''));
      var list = listOf(data);
      var merged = more ? state.list.concat(list) : list;
      state.list = merged.filter(function (entry, index) { return idOf(entry) && merged.findIndex(function (other) { return idOf(entry) === idOf(other); }) === index; });
      state.cursor = nextCursor(data);
      if (state.cursor === cursor) state.cursor = '';
      state.loaded = true;
      renderList();
    }
    async function loadMenu() {
      var appid = state.appid;
      var data = unwrap(await ui.api(menuPath()));
      if (state.appid !== appid || !isMenu()) throw new Error('机器人或页面已切换，请重新读取菜单');
      menu.load(data);
      state.loaded = true;
    }
    function refresh() {
      return work(async function () {
        if (!state.appid) return;
        if (isMenu()) { await loadMenu(); notice('已从 QQ 刷新全局菜单'); }
        else { await loadList(false); notice('已从 QQ 刷新面板列表'); }
      });
    }
    async function readDetail(id) {
      if (!id) throw new Error('QQ 未返回面板 ID');
      var data = unwrap(await ui.api(itemPath(id)));
      var panel = data && (data.panel || data);
      if (!panel || !Array.isArray(panel.items)) throw new Error('QQ 未返回可编辑的面板详情');
      var summary = state.list.find(function (entry) { return idOf(entry) === id; });
      state.id = id;
      state.version = Number.isInteger(panel.version) ? panel.version : null;
      state.target = data.target_type || (summary && summary.target_type) || 'all';
      state.needsReload = false;
      $('panelRemark').value = panel.remark || '';
      $('panelItems').replaceChildren();
      panel.items.forEach(addItem);
      $('panelTargetIds').value = ''; $('panelManageIds').value = '';
      $('panelEditorTitle').textContent = '编辑面板';
      $('panelIdentity').textContent = scenes[state.scope] + ' · ' + id + (state.version !== null ? ' · 版本 ' + state.version : '');
      $('panelDelete').hidden = false;
      $('panelTargetsManager').hidden = state.target !== 'specific';
      var targets = state.scope === 'group' ? data.group_openids : data.user_openids;
      $('panelCurrentTargets').textContent = Array.isArray(targets) && targets.length ? targets.join('\n') : '接口未返回关联对象，请以 QQ 端实际配置为准。';
      closeCopy(); renderTarget(); state.saved = snapshot(); changed(); renderList();
    }
    function closeCopy() { $('panelCopyBox').hidden = true; $('panelCopyToggle').setAttribute('aria-expanded', 'false'); }
    function addItem(item) {
      if (rows().length >= 20) return;
      item = item || { type: 'command', name: '', desc: '', only_admin: false };
      var row = document.createElement('article');
      row.className = 'panel-item';
      row.innerHTML = '<div class="panel-item-head"><div class="panel-actions"><button class="panel-drag" type="button" draggable="true" title="拖动排序，也可使用右侧上下按钮" aria-label="拖动排序">⠿</button><span class="panel-item-number"></span><div class="panel-type-select"></div></div><div class="panel-actions"><button class="icon-action" data-action="up" type="button" aria-label="上移" title="上移">↑</button><button class="icon-action" data-action="down" type="button" aria-label="下移" title="下移">↓</button><button class="icon-action" data-action="remove" type="button" aria-label="移除元素" title="移除元素">×</button></div></div>'
        + '<div class="panel-form-grid"><label class="field"><span>名称 <small data-count="name"></small></span><input data-field="name" value="' + esc(item.name) + '" placeholder="如：帮助" autocomplete="off" /></label><label class="field"><span>描述 <small data-count="desc"></small></span><input data-field="desc" value="' + esc(item.desc) + '" placeholder="可选，说明这个指令的用途" autocomplete="off" /></label></div>'
        + '<label class="field panel-link"><span>HTTPS 链接</span><input data-field="link" type="url" value="' + esc(item.link || '') + '" placeholder="https://example.com" /></label>'
        + '<div class="panel-admin"><span>仅管理员可用</span><label class="toggle"><input data-field="only_admin" type="checkbox" aria-label="仅管理员可用"' + (item.only_admin ? ' checked' : '') + ' /><i></i></label></div>';
      function renderType(type) {
        select(row.querySelector('.panel-type-select'), [['command', '指令'], ['link', '链接']], type, function (value) { renderType(value); changed(); }, '元素类型');
        row.querySelector('.panel-link').hidden = type !== 'link';
      }
      renderType(item.type);
      $('panelItems').append(row);
      changed();
    }
    function payload() {
      var values = items();
      if (!values.length) throw new Error('请至少添加一个面板元素');
      values.forEach(function (item, index) {
        if (!item.name || width(item.name) > 15 || width(item.desc) > 30) throw new Error('第 ' + (index + 1) + ' 个元素：名称必填且最多 15 字符，描述最多 30 字符（非 ASCII 计 2 个）');
        if (item.type === 'link') {
          var url;
          try { url = new URL(item.link); } catch (_) { throw new Error('第 ' + (index + 1) + ' 个元素的链接格式错误'); }
          if (url.protocol !== 'https:' || item.link.length > 2048) throw new Error('链接必须使用 HTTPS，且不超过 2048 个字符');
        }
      });
      var remark = $('panelRemark').value.trim();
      if (width(remark) > 255) throw new Error('面板备注最多 255 字符（非 ASCII 计 2 个）');
      var panel = { items: values, remark: remark };
      if (state.id && Number.isInteger(state.version)) panel.version = state.version;
      var body = { panel: panel };
      if (!state.id) {
        body.scope = state.scope; body.target_type = state.target;
        if (state.target === 'specific') body[state.scope === 'group' ? 'group_openids' : 'user_openids'] = splitIds($('panelTargetIds').value);
      }
      return body;
    }
    function confirmRemote(title, text, task) {
      if (state.busy || !state.appid) return;
      var bot = state.bots.find(function (entry) { return entry.appid === state.appid; });
      ui.confirm({ title: title, subtitle: (bot && bot.bot_name ? bot.bot_name + ' · ' : '') + state.appid + ' · ' + scopeName(), text: text, hint: '确认后将直接修改 QQ 端配置，不需要重启核心。', okText: '确认提交', onOk: function () { return work(task); } });
    }
    function save() {
      var body;
      try { body = payload(); } catch (error) { notice(error.message, true); return; }
      var id = state.id;
      confirmRemote(id ? '更新指令面板？' : '创建指令面板？', '将提交当前 ' + body.panel.items.length + ' 个元素。' + (id ? 'QQ 端现有内容将被替换。' : '新面板会按所选作用范围生效。'), async function () {
        var data = unwrap(await ui.api(id ? itemPath(id) : base(), { method: id ? 'PUT' : 'POST', body: JSON.stringify(body) }));
        state.saved = snapshot(); changed();
        notice(id ? '面板已更新到 QQ' : '面板已创建到 QQ');
        var savedId = id || idOf(data);
        if (!savedId) reset();
        else { state.id = savedId; state.needsReload = true; }
        try {
          await loadList(false);
          if (savedId) await readDetail(savedId);
        } catch (error) { notice('已提交到 QQ，但回读失败：' + error.message + '。请重新打开远端面板，勿重复创建。', true); }
      });
    }
    function targets(op) {
      var ids;
      try { ids = splitIds($('panelManageIds').value); } catch (error) { notice(error.message, true); return; }
      var body = { op: op };
      body[state.scope === 'group' ? 'group_openids' : 'user_openids'] = ids;
      confirmRemote(op === 'add' ? '添加关联对象？' : '移除关联对象？', '将' + (op === 'add' ? '添加' : '移除') + ' ' + ids.length + ' 个关联对象。当前元素修改不会提交。', async function () {
        await ui.api(itemPath(state.id) + '/target', { method: 'PUT', body: JSON.stringify(body) });
        $('panelManageIds').value = '';
        notice('关联对象已' + (op === 'add' ? '添加' : '移除'));
        try {
          var data = unwrap(await ui.api(itemPath(state.id)));
          var values = state.scope === 'group' ? data.group_openids : data.user_openids;
          $('panelCurrentTargets').textContent = Array.isArray(values) && values.length ? values.join('\n') : '接口未返回关联对象，请以 QQ 端实际配置为准。';
          var panel = data.panel || data;
          if (Number.isInteger(panel.version)) state.version = panel.version;
        } catch (error) { notice('关联操作已提交，但回读失败：' + error.message, true); }
      });
    }

    $('menuSave').onclick = function () {
      var body;
      try { body = menu.payload(); } catch (error) { notice(error.message, true); return; }
      confirmRemote(body.menu ? '保存全局自定义菜单？' : '移除全局自定义菜单？', body.menu
        ? '将用当前 ' + body.menu.items.length + ' 个一级菜单项替换 QQ 端整份菜单。'
        : '当前编辑器为空，确认后将从 QQ 移除这个机器人的全局自定义菜单。', async function () {
        await ui.api(menuPath(), { method: 'PUT', body: JSON.stringify(body) });
        menu.markSaved(); notice(body.menu ? '全局菜单已保存到 QQ' : '全局菜单已从 QQ 移除');
        try { await loadMenu(); }
        catch (error) { notice('菜单已提交到 QQ，但回读失败：' + error.message, true); }
      });
    };
    $('menuClear').onclick = function () {
      if (state.busy) return;
      ui.confirm({ title: '清空菜单编辑器？', text: '只清空当前编辑内容，QQ 端菜单暂不改变。保存空菜单并确认后，才会从 QQ 移除菜单。', okText: '清空编辑器', onOk: function () { menu.clear(); } });
    };
    $('panelRemark').addEventListener('input', changed);
    $('panelTargetIds').addEventListener('input', changed);
    $('panelItems').addEventListener('input', changed);
    $('panelAddItem').onclick = function () { addItem(); };
    $('panelNew').onclick = function () { guard(reset); };
    $('panelRefresh').onclick = function () { if (isMenu()) guard(refresh); else refresh(); };
    $('panelMore').onclick = function () { work(function () { return loadList(true); }); };
    $('panelSave').onclick = save;
    $('panelTargetAdd').onclick = function () { targets('add'); };
    $('panelTargetRemove').onclick = function () { targets('del'); };
    $('panelDelete').onclick = function () {
      confirmRemote('删除指令面板？', '面板「' + ($('panelRemark').value || state.id) + '」及其关联关系将从 QQ 删除，此操作不可撤销。', async function () {
        await ui.api(itemPath(state.id), { method: 'DELETE' });
        state.list = state.list.filter(function (entry) { return idOf(entry) !== state.id; });
        reset(); notice('面板已从 QQ 删除'); await loadList(false);
      });
    };
    $('panelList').onclick = function (event) {
      var button = event.target.closest('[data-id]');
      if (button) guard(function () { return work(function () { return readDetail(button.dataset.id); }); });
    };
    $('panelScopes').onclick = function (event) {
      var button = event.target.closest('[data-scope]');
      if (!button || button.dataset.scope === state.scope) return;
      guard(function () {
        state.scope = button.dataset.scope; state.list = []; state.cursor = '';
        $('panelScopes').querySelectorAll('button').forEach(function (tab) { var active = tab === button; tab.classList.toggle('active', active); tab.setAttribute('aria-pressed', String(active)); });
        reset(); return refresh();
      });
    };
    $('panelItems').onclick = function (event) {
      var button = event.target.closest('[data-action]');
      if (!button) return;
      var row = button.closest('.panel-item');
      if (button.dataset.action === 'remove') row.remove();
      if (button.dataset.action === 'up' && row.previousElementSibling) row.previousElementSibling.before(row);
      if (button.dataset.action === 'down' && row.nextElementSibling) row.nextElementSibling.after(row);
      changed();
    };
    $('panelItems').addEventListener('dragstart', function (event) {
      if (state.busy || !event.target.closest('.panel-drag')) { event.preventDefault(); return; }
      dragRow = event.target.closest('.panel-item');
      dragRow.classList.add('dragging');
      event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain', 'panel-item');
    });
    $('panelItems').addEventListener('dragover', function (event) {
      if (!dragRow || state.busy) return;
      event.preventDefault();
      var next = rows().filter(function (row) { return row !== dragRow; }).find(function (row) { var rect = row.getBoundingClientRect(); return event.clientY < rect.top + rect.height / 2; });
      $('panelItems').insertBefore(dragRow, next || null);
    });
    $('panelItems').addEventListener('drop', function (event) { if (dragRow) event.preventDefault(); });
    $('panelItems').addEventListener('dragend', function () { if (dragRow) dragRow.classList.remove('dragging'); dragRow = null; changed(); });
    $('panelCopyToggle').onclick = function () {
      var open = $('panelCopyBox').hidden;
      $('panelCopyBox').hidden = !open; $('panelCopyToggle').setAttribute('aria-expanded', String(open));
    };
    function renderCopyScope() {
      select($('panelCopyScopeSelect'), Object.entries(scenes), copyScope, function (value) { copyScope = value; renderCopyScope(); $('panelCopyList').replaceChildren(); }, '来源场景');
    }
    $('panelCopyLoad').onclick = function () {
      work(async function () {
        var data = await ui.api(base() + '?scope=' + copyScope + '&limit=50');
        var list = listOf(data).filter(function (entry) { return idOf(entry) !== state.id; });
        $('panelCopyList').innerHTML = list.length ? list.map(function (entry) { var panel = entry.panel || entry; return '<button type="button" class="btn" data-copy="' + esc(idOf(entry)) + '">' + esc(panel.remark || idOf(entry)) + '</button>'; }).join('') : '<p class="hint">这个场景没有其他面板。</p>';
      });
    };
    $('panelCopyList').onclick = function (event) {
      var button = event.target.closest('[data-copy]');
      if (!button) return;
      ui.confirm({ title: '复制面板元素？', text: '将用「' + button.textContent + '」的元素替换当前全部元素。备注和关联对象不会改变；保存前不会修改 QQ 端配置。', okText: '复制元素', onOk: function () {
        return work(async function () {
          var data = unwrap(await ui.api(itemPath(button.dataset.copy)));
          var source = data && (data.panel || data);
          if (!source || !Array.isArray(source.items) || !source.items.length || source.items.length > 20) throw new Error('来源面板的元素数量不合法');
          $('panelItems').replaceChildren(); source.items.forEach(addItem); closeCopy(); changed(); notice('元素已复制，保存后才会提交到 QQ');
        });
      } });
    };
    document.addEventListener('click', function (event) { if (!event.target.closest('.cp-select')) closeSelects(); });
    window.addEventListener('beforeunload', function (event) { if (dirty() || $('panelManageIds').value.trim()) { event.preventDefault(); event.returnValue = ''; } });
    renderCopyScope(); reset(); renderBots();
    return {
      setBots: function (bots) {
        state.bots = bots;
        var available = bots.filter(function (bot) { return bot.secret_set; });
        if (!available.some(function (bot) { return bot.appid === state.appid; })) {
          state.appid = available.length ? available[0].appid : ''; state.list = []; state.cursor = ''; state.loaded = false; reset();
        }
        renderBots(); syncButtons();
      },
      open: function () { if (!state.loaded && !state.busy && state.appid) return refresh(); },
      busy: function () { return state.busy; },
    };
  };
})();
