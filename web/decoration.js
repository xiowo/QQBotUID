(function () {
  'use strict';

  var DEFAULT_FOOTER = '> 点击[支持一下](mqqapi://aio/inlinecmd?command=支持沫狐&enter=false&reply=false)';
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (value) {
    return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  };
  function clone(value) { return JSON.parse(JSON.stringify(value)); }
  function defaults() {
    return {
      auto_buttons_enabled: false,
      auto_buttons_override: false,
      buttons: [],
      footer_enabled: false,
      footer_text: DEFAULT_FOOTER,
    };
  }
  function normalize(raw) {
    raw = raw && typeof raw === 'object' ? raw : {};
    var buttons = Array.isArray(raw.buttons) ? raw.buttons.map(function (row) {
      if (row && !Array.isArray(row) && Array.isArray(row.buttons)) row = row.buttons;
      if (!Array.isArray(row)) return [];
      return row.filter(function (button) { return button && typeof button === 'object'; }).map(function (button) {
        var out = {};
        ['text', 'data', 'pressed_text', 'id', 'group_id', 'content', 'confirm_text', 'cancel_text'].forEach(function (key) {
          out[key] = button[key] === null || button[key] === undefined ? '' : String(button[key]);
        });
        ['action', 'style', 'permisson'].forEach(function (key) {
          var value = Number.isInteger(button[key]) ? button[key] : (key === 'style' ? 1 : 2);
          if (key === 'permisson' && value !== 1 && value !== 2) value = 2;
          if (key === 'style' && value === 2) value = 0;
          out[key] = value;
        });
        ['smallbtn', 'enter', 'reply'].forEach(function (key) { out[key] = Boolean(button[key]); });
        if (Array.isArray(button.specify_role_ids)) out.specify_role_ids = button.specify_role_ids;
        if (Array.isArray(button.specify_user_ids)) out.specify_user_ids = button.specify_user_ids;
        return out;
      });
    }).filter(function (row) { return row.length; }) : [];
    return {
      auto_buttons_enabled: Boolean(raw.auto_buttons_enabled),
      auto_buttons_override: Boolean(raw.auto_buttons_override),
      buttons: buttons,
      footer_enabled: Boolean(raw.footer_enabled),
      footer_text: typeof raw.footer_text === 'string' ? raw.footer_text : DEFAULT_FOOTER,
    };
  }
  function tone(style) {
    var value = Number(style);
    if (value === 0) return 'seg-btn--plain';
    if (value === 3) return 'seg-btn--danger';
    if (value === 4) return 'seg-btn--solid';
    return 'seg-btn--primary';
  }
  function label(button) { return button.text || button.data || '按钮'; }
  function newButton() {
    return { text: '按钮', data: '', pressed_text: '', action: 2, style: 1, permisson: 2, smallbtn: false, enter: false, reply: false, id: '', group_id: '', content: '', confirm_text: '', cancel_text: '' };
  }
  var SELECTS = {
    action: [[2, '指令'], [1, '回调'], [0, '链接']],
    style: [[1, '蓝框蓝字'], [0, '灰框黑字'], [3, '灰框红字'], [4, '蓝底白字']],
    permisson: [[2, '所有人'], [1, '仅管理员']],
  };
  function textField(key, title, value, placeholder) {
    return '<label class="field"><span>' + esc(title) + '</span><input data-field="' + key + '" value="' + esc(value) + '" placeholder="' + esc(placeholder || '') + '" autocomplete="off" /></label>';
  }
  function selectField(key, title) {
    return '<label class="field"><span>' + esc(title) + '</span><div data-select-field="' + key + '"></div></label>';
  }
  function checkField(key, title, checked) {
    return '<label class="decor-mini-check"><input type="checkbox" data-field="' + key + '"' + (checked ? ' checked' : '') + ' /><i></i>' + esc(title) + '</label>';
  }

  window.QQBotDecoration = function (ui) {
    var state = {
      data: defaults(), saved: '', loaded: false, busy: false, active: null,
      appid: '', bots: [], hasOwn: false, defaultData: null,
    };
    var dragRow = null;

    function snapshot() { return JSON.stringify(state.data); }
    function rows() { return Array.from($('decorRows').children).filter(function (el) { return el.classList.contains('decor-row'); }); }
    function changed() {
      var dirty = state.loaded && snapshot() !== state.saved;
      $('decorDirty').hidden = !dirty;
      var count = state.data.buttons.reduce(function (sum, row) { return sum + row.length; }, 0);
      $('decorButtonCount').textContent = state.data.buttons.length + ' 行 / ' + count + ' 个按钮';
      $('decorSave').disabled = state.busy || !dirty;
      $('decorScopeTag').hidden = !state.appid || !state.hasOwn;
      $('decorInherit').hidden = !state.appid || !state.hasOwn || state.busy;
      var name = state.appid ? botName(state.appid) : '全局默认';
      $('decorScopeHint').textContent = state.appid
        ? (state.hasOwn
          ? '正在编辑「' + name + '」的单独配置，保存后只影响这个机器人。'
          : '「' + name + '」目前继承全局默认；改动并保存后会生成它自己的配置。')
        : '正在编辑全局默认，作用于所有没有单独配置的机器人。';
    }
    function botName(appid) {
      var bot = state.bots.find(function (item) { return item.appid === appid; });
      return (bot && (bot.bot_name || bot.appid)) || appid;
    }
    function syncSwitches() {
      $('decorButtonsEnabled').checked = state.data.auto_buttons_enabled;
      $('decorButtonsOverride').checked = state.data.auto_buttons_override;
      $('decorFooterEnabled').checked = state.data.footer_enabled;
      $('decorFooterText').value = state.data.footer_text;
    }
    function setBusy(busy) {
      state.busy = busy;
      ['decorButtonsEnabled', 'decorButtonsOverride', 'decorFooterEnabled', 'decorFooterText', 'decorAddRow', 'decorReset', 'decorSave', 'decorInherit'].forEach(function (id) {
        $(id).disabled = busy;
      });
      changed();
    }
    function renderPreview(rowEl, rowIndex) {
      var row = state.data.buttons[rowIndex] || [];
      var html = row.map(function (button, btnIndex) {
        var active = state.active && state.active.row === rowIndex && state.active.button === btnIndex;
        var link = Number(button.action) === 0 ? '<span class="seg-btn__link" aria-hidden="true">↗</span>' : '';
        return '<button type="button" class="seg-btn ' + tone(button.style) + (active ? ' seg-btn--active' : '') + '" data-btn="' + btnIndex + '" title="点击编辑">'
          + link + '<span>' + esc(label(button)) + '</span></button>';
      }).join('');
      html += '<button type="button" class="btn seg-btn-add" data-add-btn="1" title="给这一行添加按钮">＋</button>';
      rowEl.querySelector('.seg-btn-row').innerHTML = html;
    }
    function renderEditor(host, rowIndex, btnIndex) {
      var button = state.data.buttons[rowIndex][btnIndex];
      host.innerHTML = '<div class="decor-grid">'
        + textField('text', '显示文字', button.text, '如：支持沫狐')
        + textField('data', '指令 / 回调数据', button.data, '如：支持沫狐')
        + textField('pressed_text', '点击后文字', button.pressed_text, '可选')
        + selectField('action', '动作类型')
        + selectField('style', '样式')
        + textField('id', '按钮 ID', button.id, '留空自动编号')
        + checkField('smallbtn', '小按钮', button.smallbtn)
        + '</div>'
        + '<details class="decor-advanced"><summary>高级：权限 / 弹窗 / enter / reply</summary><div class="decor-grid">'
        + selectField('permisson', '权限')
        + textField('group_id', '按钮组 ID', button.group_id, '可选')
        + textField('content', '弹窗内容', button.content, '填写后点击先弹窗')
        + textField('confirm_text', '确认文案', button.confirm_text, '是')
        + textField('cancel_text', '取消文案', button.cancel_text, '否')
        + checkField('enter', '指令自动发送', button.enter)
        + checkField('reply', '带引用回复', button.reply)
        + '</div></details>'
        + '<div class="decor-actions"><button class="btn danger" type="button" data-remove-btn="1">删除按钮</button><button class="btn" type="button" data-close-editor="1">收起</button></div>';
      host.querySelectorAll('[data-select-field]').forEach(function (box) {
        var key = box.dataset.selectField;
        if (!window.QQBotSelect || !SELECTS[key]) return;
        window.QQBotSelect(box, SELECTS[key], button[key], function (value) {
          var rowEl = host.closest('.decor-row');
          if (!rowEl || !state.data.buttons[rowIndex] || !state.data.buttons[rowIndex][btnIndex]) return;
          state.data.buttons[rowIndex][btnIndex][key] = parseInt(value, 10);
          renderPreview(rowEl, rowIndex);
          changed();
        }, key);
      });
    }
    function closeEditor() {
      state.active = null;
      rows().forEach(function (rowEl) {
        rowEl.querySelector('.decor-btn-editor').hidden = true;
        rowEl.querySelector('.decor-btn-editor').innerHTML = '';
        renderPreview(rowEl, Number(rowEl.dataset.row));
      });
    }
    function openEditor(rowIndex, btnIndex) {
      var rowEl = rows()[rowIndex];
      if (!rowEl || !state.data.buttons[rowIndex][btnIndex]) return;
      if (state.active) closeEditor();
      state.active = { row: rowIndex, button: btnIndex };
      var host = rowEl.querySelector('.decor-btn-editor');
      renderEditor(host, rowIndex, btnIndex);
      host.hidden = false;
      renderPreview(rowEl, rowIndex);
    }
    function renderAll() {
      $('decorRows').innerHTML = state.data.buttons.map(function (row, rowIndex) {
        return '<article class="decor-row" data-row="' + rowIndex + '">'
          + '<div class="panel-item-head decor-row-head"><div class="panel-actions">'
          + '<button class="panel-drag" type="button" draggable="true" title="拖动排序，也可使用右侧上下按钮" aria-label="拖动排序">⠿</button>'
          + '<span class="panel-item-number">第 ' + (rowIndex + 1) + ' 行</span></div>'
          + '<div class="panel-actions"><button class="icon-action" type="button" data-row-up="1" aria-label="上移">↑</button>'
          + '<button class="icon-action" type="button" data-row-down="1" aria-label="下移">↓</button>'
          + '<button class="icon-action" type="button" data-row-remove="1" aria-label="删除这一行">×</button></div></div>'
          + '<div class="seg-btn-row"></div><div class="decor-btn-editor" hidden></div></article>';
      }).join('') || '<p class="decor-empty">还没有按钮行。点「＋ 添加一行」开始配置。</p>';
      rows().forEach(function (rowEl, index) {
        renderPreview(rowEl, index);
        rowEl.querySelector('[data-row-up]').disabled = index === 0;
        rowEl.querySelector('[data-row-down]').disabled = index === rows().length - 1;
      });
      if (state.active && state.data.buttons[state.active.row] && state.data.buttons[state.active.row][state.active.button]) {
        var active = state.active;
        state.active = null;
        openEditor(active.row, active.button);
      } else {
        state.active = null;
      }
      changed();
    }
    function syncEditor(rowEl) {
      var host = rowEl.querySelector('.decor-btn-editor');
      if (!state.active || host.hidden) return;
      var rowIndex = Number(rowEl.dataset.row);
      var button = state.data.buttons[rowIndex] && state.data.buttons[rowIndex][state.active.button];
      if (!button) return;
      host.querySelectorAll('[data-field]').forEach(function (input) {
        var key = input.dataset.field;
        button[key] = input.type === 'checkbox' ? input.checked : input.value;
      });
      renderPreview(rowEl, rowIndex);
      changed();
    }
    function addRow() {
      if (state.busy || state.data.buttons.length >= 10) return;
      closeEditor();
      state.data.buttons.push([newButton()]);
      renderAll();
    }
    function addButton(rowIndex) {
      var row = state.data.buttons[rowIndex];
      if (!row || row.length >= 5) return;
      closeEditor();
      row.push(newButton());
      renderAll();
    }
    function moveRow(rowEl, offset) {
      var index = Number(rowEl.dataset.row);
      var target = index + offset;
      if (target < 0 || target >= state.data.buttons.length) return;
      var list = state.data.buttons;
      list.splice(target, 0, list.splice(index, 1)[0]);
      closeEditor();
      renderAll();
    }
    function query() { return state.appid ? '?appid=' + encodeURIComponent(state.appid) : ''; }
    function applyPayload(result) {
      state.data = normalize(result.config);
      state.defaultData = normalize(result.default);
      state.hasOwn = Boolean(result.has_own);
      state.saved = snapshot();
      state.loaded = true;
      syncSwitches();
      closeEditor();
      renderAll();
    }
    function renderBotSelect() {
      var root = $('decorBotSelect');
      var choices = [['', '全局默认（所有未单独配置的机器人）']].concat(state.bots.map(function (bot) {
        return [bot.appid, (bot.bot_name || bot.appid) + '（' + bot.appid + '）'];
      }));
      if (!window.QQBotSelect) return;
      window.QQBotSelect(root, choices, state.appid, function (appid) { switchBot(appid); }, '配置对象');
    }
    function setBots(bots) {
      state.bots = Array.isArray(bots) ? bots : [];
      renderBotSelect();
    }
    function switchBot(appid) {
      appid = String(appid || '');
      if (appid === state.appid) return;
      function proceed() {
        state.appid = appid;
        renderBotSelect();
        load(true);
      }
      if (state.loaded && snapshot() !== state.saved) {
        ui.confirm({
          title: '放弃未提交的修改？',
          text: '当前配置有未保存的修改，切换配置对象后这些修改会丢失。',
          okText: '放弃修改',
          onOk: proceed,
        });
        return;
      }
      proceed();
    }
    async function load(force) {
      if ((state.loaded && !force) || state.busy) return;
      setBusy(true);
      try {
        applyPayload(await ui.api('/decoration' + query()));
      } catch (error) {
        state.data = defaults();
        state.defaultData = defaults();
        state.hasOwn = false;
        state.saved = snapshot();
        state.loaded = true;
        syncSwitches();
        closeEditor();
        renderAll();
        ui.toast('读取消息追加配置失败：' + (error.message || '请求失败'), true);
      } finally {
        setBusy(false);
      }
    }
    async function save() {
      if (state.busy) return;
      setBusy(true);
      try {
        applyPayload(await ui.api('/decoration' + query(), {
          method: 'POST',
          body: JSON.stringify({ appid: state.appid, config: state.data }),
        }));
        ui.toast(state.appid ? '已保存「' + botName(state.appid) + '」的配置' : '已保存全局默认配置');
      } catch (error) {
        ui.toast('保存失败：' + (error.message || '请求失败'), true);
      } finally {
        setBusy(false);
      }
    }
    function reset() {
      ui.confirm({
        title: '恢复默认消息追加配置？',
        text: '按钮会清空，三个开关都会变回关闭，底部内容恢复为默认值。保存后才会生效，QQ 端指令面板不受影响。',
        okText: '恢复默认',
        onOk: function () {
          closeEditor();
          state.data = defaults();
          syncSwitches();
          renderAll();
        },
      });
    }
    function inheritDefault() {
      if (!state.appid || state.busy) return;
      ui.confirm({
        title: '恢复继承全局默认？',
        text: '将删除「' + botName(state.appid) + '」的单独配置，之后它会跟随全局默认。',
        okText: '恢复继承',
        onOk: async function () {
          setBusy(true);
          try {
            applyPayload(await ui.api('/decoration/' + encodeURIComponent(state.appid), { method: 'DELETE' }));
            ui.toast('已恢复继承全局默认');
          } catch (error) {
            ui.toast('操作失败：' + (error.message || '请求失败'), true);
          } finally {
            setBusy(false);
          }
        },
      });
    }

    $('decorButtonsEnabled').addEventListener('change', function () { state.data.auto_buttons_enabled = this.checked; changed(); });
    $('decorButtonsOverride').addEventListener('change', function () { state.data.auto_buttons_override = this.checked; changed(); });
    $('decorFooterEnabled').addEventListener('change', function () { state.data.footer_enabled = this.checked; changed(); });
    $('decorFooterText').addEventListener('input', function () { state.data.footer_text = this.value; changed(); });
    $('decorAddRow').addEventListener('click', addRow);
    $('decorReset').addEventListener('click', reset);
    $('decorSave').addEventListener('click', save);
    $('decorInherit').addEventListener('click', inheritDefault);
    $('decorRows').addEventListener('click', function (event) {
      var button = event.target.closest('button');
      if (!button) return;
      var rowEl = button.closest('.decor-row');
      var rowIndex = rowEl ? Number(rowEl.dataset.row) : -1;
      if (button.dataset.addBtn !== undefined) { addButton(rowIndex); return; }
      if (button.dataset.btn !== undefined) { openEditor(rowIndex, Number(button.dataset.btn)); return; }
      if (button.dataset.removeBtn !== undefined) {
        state.data.buttons[rowIndex].splice(state.active.button, 1);
        closeEditor(); renderAll(); return;
      }
      if (button.dataset.closeEditor !== undefined) { closeEditor(); return; }
      if (button.dataset.rowRemove !== undefined) {
        state.data.buttons.splice(rowIndex, 1);
        closeEditor(); renderAll(); return;
      }
      if (button.dataset.rowUp !== undefined) { moveRow(rowEl, -1); return; }
      if (button.dataset.rowDown !== undefined) { moveRow(rowEl, 1); }
    });
    $('decorRows').addEventListener('input', function (event) {
      if (event.target.closest('.decor-btn-editor')) syncEditor(event.target.closest('.decor-row'));
    });
    $('decorRows').addEventListener('change', function (event) {
      if (event.target.closest('.decor-btn-editor')) syncEditor(event.target.closest('.decor-row'));
    });
    $('decorRows').addEventListener('dragstart', function (event) {
      if (state.busy || !event.target.closest('.panel-drag')) { event.preventDefault(); return; }
      dragRow = event.target.closest('.decor-row');
      dragRow.classList.add('dragging');
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', 'decor-row');
    });
    $('decorRows').addEventListener('dragover', function (event) {
      if (!dragRow || state.busy) return;
      event.preventDefault();
      var next = rows().filter(function (row) { return row !== dragRow; }).find(function (row) {
        var rect = row.getBoundingClientRect();
        return event.clientY < rect.top + rect.height / 2;
      });
      $('decorRows').insertBefore(dragRow, next || null);
    });
    $('decorRows').addEventListener('drop', function (event) { if (dragRow) event.preventDefault(); });
    $('decorRows').addEventListener('dragend', function () {
      if (!dragRow) return;
      dragRow.classList.remove('dragging');
      var ordered = rows().map(function (rowEl) { return clone(state.data.buttons[Number(rowEl.dataset.row)]); });
      dragRow = null;
      closeEditor();
      state.data.buttons = ordered;
      renderAll();
    });
    window.addEventListener('beforeunload', function (event) {
      if (state.loaded && snapshot() !== state.saved) { event.preventDefault(); event.returnValue = ''; }
    });

    renderBotSelect();
    syncSwitches();
    renderAll();
    return {
      open: load,
      setBots: setBots,
      busy: function () { return state.busy; },
      dirty: function () { return state.loaded && snapshot() !== state.saved; },
    };
  };
})();
