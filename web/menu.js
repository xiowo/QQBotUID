(function () {
  'use strict';

  window.QQBotMenuEditor = function (ui) {
    var $ = function (id) { return document.getElementById(id); };
    var root = $('menuItems');
    var loaded = false;
    var saved = '';
    var dragging = null;
    var actions = [['send_message', '发送消息'], ['link', 'HTTPS 链接'], ['switch', '开关'], ['menu', '折叠菜单']];
    var esc = ui.escapeHtml;
    function length(value) { return Array.from(value).length; }
    function children(list) { return Array.from(list.children); }
    function field(row, key) { return row.querySelector('[data-menu-field="' + key + '"]'); }
    function text(value, max, label, validate) {
      value = value.trim();
      if (validate && (!value || length(value) > max)) throw new Error(label + '不能为空，且最多 ' + max + ' 个字符');
      return value;
    }
    function readRow(row, validate) {
      var sub = row.dataset.sub === 'true';
      var type = row.querySelector('.menu-type').dataset.value;
      var item = { type: type, name: text(field(row, 'name').value, sub ? 14 : 10, sub ? '子菜单名称' : '一级菜单名称', validate) };
      if (type === 'send_message') item.send_message = text(field(row, 'send_message').value, 2000, '发送内容', validate);
      if (type === 'link') {
        item.link = text(field(row, 'link').value, 2048, '菜单链接', validate);
        if (validate) {
          var url;
          try { url = new URL(item.link); } catch (_) { throw new Error('菜单链接格式错误，请填写 HTTPS 链接'); }
          if (url.protocol !== 'https:') throw new Error('菜单链接必须使用 HTTPS');
        }
      }
      if (type === 'switch') item.switch = { switch_id: text(field(row, 'switch_id').value, 128, '开关 ID', validate), default: field(row, 'default').checked };
      if (type === 'menu') {
        item.sub_menu_items = children(row.querySelector('.menu-list')).map(function (child) { return readRow(child, validate); });
        if (validate && (!item.sub_menu_items.length || item.sub_menu_items.length > 5)) throw new Error('每个折叠菜单需要 1 到 5 个子项');
      }
      return item;
    }
    function payload(validate) {
      var items = children(root).map(function (row) { return readRow(row, validate); });
      if (validate && items.length > 10) throw new Error('全局菜单最多 10 个一级菜单项');
      return { menu: items.length ? { items: items } : null };
    }
    function snapshot() { return JSON.stringify(payload(false)); }
    function dirty() { return saved !== '' && snapshot() !== saved; }
    function updateList(list, sub) {
      var rows = children(list);
      rows.forEach(function (row, index) {
        row.querySelector('.menu-number').textContent = String(index + 1).padStart(2, '0');
        row.querySelector('[data-menu-action="up"]').disabled = index === 0;
        row.querySelector('[data-menu-action="down"]').disabled = index === rows.length - 1;
        var nameLength = length(field(row, 'name').value);
        var counter = row.querySelector('.menu-name-count');
        counter.textContent = nameLength + ' / ' + (sub ? 14 : 10);
        counter.classList.toggle('over-limit', nameLength > (sub ? 14 : 10));
        if (!sub) {
          var subList = row.querySelector('.menu-list');
          row.querySelector('.menu-sub-count').textContent = subList.children.length + ' / 5';
          row.querySelector('[data-menu-action="add-child"]').disabled = subList.children.length >= 5;
          updateList(subList, true);
        }
      });
    }
    function changed() {
      updateList(root, false);
      $('menuCount').textContent = root.children.length + ' / 10';
      $('menuAdd').disabled = root.children.length >= 10;
      $('menuClear').disabled = !root.children.length;
      $('menuDirty').hidden = !dirty();
      $('menuEmpty').hidden = Boolean(root.children.length);
      $('menuEmpty').textContent = loaded ? '暂无全局自定义菜单。点击「添加菜单项」开始配置。' : '请先读取 QQ 端菜单。';
    }
    function makeRow(item, sub) {
      item = item || { type: 'send_message', name: '', send_message: '' };
      var row = document.createElement('article');
      row.className = 'panel-item menu-row' + (sub ? ' menu-subrow' : '');
      row.dataset.sub = String(Boolean(sub));
      row.innerHTML = '<div class="panel-item-head"><div class="panel-actions"><button class="panel-drag menu-drag" type="button" draggable="true" aria-label="拖动菜单排序" title="拖动同级排序">⠿</button><span class="panel-item-number menu-number"></span><div class="menu-type"></div></div><div class="panel-actions"><button class="icon-action" data-menu-action="up" type="button" title="上移" aria-label="上移菜单">↑</button><button class="icon-action" data-menu-action="down" type="button" title="下移" aria-label="下移菜单">↓</button><button class="icon-action" data-menu-action="remove" type="button" title="移除" aria-label="移除菜单">×</button></div></div>'
        + '<label class="field menu-name"><span>' + (sub ? '子菜单名称' : '一级菜单名称') + '<small class="menu-name-count"></small></span><input data-menu-field="name" value="' + esc(item.name) + '" autocomplete="off" /></label>'
        + '<label class="field menu-value" data-menu-section="send_message"><span>发送内容 <small>最多 2000 字符</small></span><textarea data-menu-field="send_message" rows="3" placeholder="例如：#帮助">' + esc(item.send_message || '') + '</textarea></label>'
        + '<label class="field menu-value" data-menu-section="link"><span>HTTPS 链接</span><input data-menu-field="link" type="url" value="' + esc(item.link || '') + '" placeholder="https://example.com" /></label>';
      if (!sub) {
        row.innerHTML += '<div data-menu-section="switch"><label class="field menu-value"><span>开关 ID <small>最多 128 字符</small></span><input data-menu-field="switch_id" value="' + esc(item.switch ? item.switch.switch_id : '') + '" placeholder="唯一开关标识" /></label><div class="panel-admin"><span>默认开启</span><label class="toggle"><input data-menu-field="default" type="checkbox" aria-label="默认开启"' + (item.switch && item.switch.default ? ' checked' : '') + ' /><i></i></label></div></div>'
          + '<section class="menu-children" data-menu-section="menu"><div class="panel-section-head"><h3>子菜单 <small class="menu-sub-count">0 / 5</small></h3><button class="btn" data-menu-action="add-child" type="button">＋ 添加子菜单</button></div><p class="hint">子菜单仅支持发送消息和 HTTPS 链接。</p><div class="menu-list"></div></section>';
        (item.sub_menu_items || []).forEach(function (child) { row.querySelector('.menu-list').append(makeRow(child, true)); });
      }
      function setType(type) {
        ui.select(row.querySelector('.menu-type'), sub ? actions.slice(0, 2) : actions, type, function (value) {
          setType(value);
          if (value === 'menu' && !row.querySelector('.menu-list').children.length) row.querySelector('.menu-list').append(makeRow(null, true));
          changed();
        }, sub ? '子菜单类型' : '菜单类型');
        Array.from(row.children).filter(function (node) { return node.dataset.menuSection; }).forEach(function (section) { section.hidden = section.dataset.menuSection !== type; });
      }
      setType(item.type);
      return row;
    }
    function remoteText(value, label) {
      if (typeof value !== 'string') throw new Error('QQ 返回的' + label + '不是文本，未覆盖编辑器');
      return value;
    }
    function remoteItem(item, sub) {
      if (!item || typeof item !== 'object' || Array.isArray(item)) throw new Error('QQ 返回了无法识别的菜单项');
      var allowed = sub ? ['send_message', 'link'] : ['send_message', 'link', 'switch', 'menu'];
      if (allowed.indexOf(item.type) < 0) throw new Error('QQ 返回了不支持的菜单类型：' + String(item.type));
      var result = { type: item.type, name: remoteText(item.name, '菜单名称') };
      if (item.type === 'send_message') result.send_message = remoteText(item.send_message, '发送内容');
      if (item.type === 'link') result.link = remoteText(item.link, '菜单链接');
      if (item.type === 'switch') {
        if (!item.switch || typeof item.switch !== 'object' || (item.switch.default !== undefined && typeof item.switch.default !== 'boolean')) throw new Error('QQ 返回了无法识别的开关配置');
        result.switch = { switch_id: remoteText(item.switch.switch_id, '开关 ID'), default: item.switch.default === true };
      }
      if (item.type === 'menu') {
        if (!Array.isArray(item.sub_menu_items) || !item.sub_menu_items.length || item.sub_menu_items.length > 5) throw new Error('QQ 返回的折叠菜单子项数量不合法');
        result.sub_menu_items = item.sub_menu_items.map(function (child) { return remoteItem(child, true); });
      }
      return result;
    }
    function load(data) {
      var menu;
      if (data === null) menu = null;
      else if (data && typeof data === 'object' && !Array.isArray(data)) {
        if (Object.prototype.hasOwnProperty.call(data, 'menu')) menu = data.menu;
        else if (Object.keys(data).every(function (key) { return ['code', 'message', 'msg'].indexOf(key) >= 0; })) menu = null;
        else throw new Error('QQ 未返回可识别的全局菜单，未覆盖编辑器');
      } else throw new Error('QQ 未返回可识别的全局菜单，未覆盖编辑器');
      var values = [];
      if (menu !== null) {
        if (!menu || typeof menu !== 'object' || Array.isArray(menu)) throw new Error('QQ 菜单结构不合法，未覆盖编辑器');
        if (!Array.isArray(menu.items) && Object.keys(menu).length) throw new Error('QQ 菜单缺少元素列表，未覆盖编辑器');
        values = menu.items || [];
        if (values.length > 10) throw new Error('QQ 返回了超过 10 个一级菜单，未覆盖编辑器');
      }
      var nodes = values.map(function (item) { return makeRow(remoteItem(item, false), false); });
      root.replaceChildren.apply(root, nodes);
      loaded = true;
      $('menuFields').disabled = false;
      saved = snapshot(); changed();
    }
    function reset() {
      root.replaceChildren(); loaded = false; saved = ''; $('menuFields').disabled = true; changed();
    }
    $('menuAdd').onclick = function () {
      if (root.children.length >= 10) return;
      root.append(makeRow(null, false)); changed();
    };
    root.addEventListener('input', changed);
    root.addEventListener('click', function (event) {
      var button = event.target.closest('[data-menu-action]');
      if (!button || button.disabled) return;
      var row = button.closest('.menu-row');
      var action = button.dataset.menuAction;
      if (action === 'remove') row.remove();
      if (action === 'up' && row.previousElementSibling) row.previousElementSibling.before(row);
      if (action === 'down' && row.nextElementSibling) row.nextElementSibling.after(row);
      if (action === 'add-child') {
        var list = row.querySelector('.menu-list');
        if (list.children.length < 5) list.append(makeRow(null, true));
      }
      changed();
    });
    root.addEventListener('dragstart', function (event) {
      if ($('panelControls').disabled || $('menuFields').disabled || !event.target.closest('.menu-drag')) { event.preventDefault(); return; }
      dragging = event.target.closest('.menu-row');
      dragging.classList.add('dragging');
      event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain', 'menu-item');
    });
    root.addEventListener('dragover', function (event) {
      if (!dragging || $('panelControls').disabled) return;
      var list = event.target.closest('.menu-list');
      if (list !== dragging.parentElement) return;
      event.preventDefault();
      var next = children(list).filter(function (row) { return row !== dragging; }).find(function (row) { var rect = row.getBoundingClientRect(); return event.clientY < rect.top + rect.height / 2; });
      list.insertBefore(dragging, next || null);
    });
    root.addEventListener('drop', function (event) { if (dragging) event.preventDefault(); });
    root.addEventListener('dragend', function () { if (dragging) dragging.classList.remove('dragging'); dragging = null; changed(); });
    reset();
    return {
      load: load,
      reset: reset,
      dirty: dirty,
      payload: function () { if (!loaded) throw new Error('请先成功读取 QQ 端菜单，再提交修改'); return payload(true); },
      markSaved: function () { saved = snapshot(); changed(); },
      clear: function () { root.replaceChildren(); changed(); },
    };
  };
})();
