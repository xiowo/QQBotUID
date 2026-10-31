# 🦊 适用于早柚核心的QQ官方机器人适配器 (QQBotUID)

这是一个适用于 [早柚核心 (gsuid_core)](https://github.com/Genshin-bots/gsuid_core) 的 QQ 官方机器人适配器插件。它把 [QQ 开放平台](https://q.qq.com) 的官方机器人接进早柚核心：通过 WebSocket 长连接或 Webhook 回调收消息，转成早柚协议交给核心处理，再把核心下发的消息转成 QQ 官方接口调用发出去。


## ✨ 主要功能

- **🔌 两种接入方式**: `WebSocket`（插件主动连官方网关，最省事，默认）与 `Webhook`（插件自己开 HTTP 端口等官方回调，需要核心公网可达，所有机器人共用一个端口）都支持，每个机器人单独选。
- **🤖 多机器人**: Bot 管理页里加几个就是几个官方机器人，一个插件实例能同时挂多个 AppID，共用一条核心连接。
- **📱 扫码绑定**: 用手机 QQ 扫码授权，自动写入 AppID / Secret 并拉取机器人资料，不用手抄密钥。
- **♻️ 保存即生效**: 改完启用状态或接入方式点保存，插件自己重载运行时，不用重启核心。
- **📝 Markdown 图片**: MD 里的图片会先换成公网链接（核心图床优先，失败回落 QQ 素材管理）
- **🔘 按钮**: 核心下发按钮时按官方 keyboard 结构带上去。
- **🎙️ 富媒体**: 语音、视频、文件都会先上传到官方富媒体接口再发；其中语音 `silk` / `amr` / `mp3` / `wav` / `ogg` 格式直接发，其他格式自动用 ffmpeg + pilk 转成 silk。
- **🗂️ 卡片**: 核心 `card` 段原样透传——`data["type"]` 是 `ark` 时按 `msg_type=3` 发 ark 模板消息（`content` 就是 ark 对象）
- **⌨️ 流式与输入状态**: 私聊可开流式分片下发（内容短时自动对半分，失败自动回退普通消息）；可开收到私聊后先发「正在输入」状态。
- **↩️ 引用与撤回**: 入站解析被引用消息的正文与图片；出站支持引用回复、主动撤回，并按 `echo` 回传消息 ID。
- **🛡️ 禁言 / 解禁**: 支持核心下发的 `excute_ban_user`，秒数为 0 即解禁。
- **👮 群管与菜单**: 踢人、禁言状态、机器人身份、群列表、入群申请审批、群黑名单、自定义菜单、指令面板都封装好了，通过控制帧 `gscore_qqbot_admin_request` 按 `action` 调用（见下文）。
- **🔗 分享链接**: 通过控制帧 `gscore_qqbot_share_link_request` 生成「加机器人好友」的链接，`callback_data` 会随好友添加事件回传，用来区分渠道 / 邀请码（仅 Webhook 模式能收到回调）。
- **📡 元事件上报**: 进群、退群、机器人被拉进群 / 被踢出群、入群申请、好友增删（带来源场景与 `callback_data`）、群消息开关、频道与论坛事件都转成核心的 meta 事件。
- **🏛️ 频道**: 频道消息走 `/channels`，支持收发消息与频道事件；**频道私信（`/dms`）不支持**。
- **🔄 断线重连**: 间隔可配（毫秒），0 表示断开立刻重连；连续快速失败会退避到 1 秒，连上并稳定 5 秒才清零失败计数。
- **📮 主动消息队列**: 被动回复窗口（5 分钟）之外的发送自动转成主动消息，群聊和私聊各走一条独立车道，按可配置间隔放行限速（间隔填 `0` 则不限速，立刻放行）；订阅推送和其他主动推送共用同一条队列，先到先发。
- **🧩 可选独立进程**: 打开后适配器跑在单独进程里，通过核心的 `/ws/{bot_id}` 接入。
- **🚧 断连维护提示**: 独立进程模式下，核心重启 / 重载插件导致断开时，daemon 自己回维护消息而不是丢消息；开启后核心退出也不再结束 daemon，核心回来时自动接管（默认关闭）。
- **👑 主人维护**: 手动挂维护时打开，只放行主人消息，其他人一律收到维护提示（默认关闭，同进程 / 独立进程都生效）。

## 🛠️ 安装说明

1. 把插件目录放进早柚核心的 `gsuid_core/gsuid_core/plugins/` 下，目录名保持 `QQBotUID`：

   ```bash
   cd gsuid_core/gsuid_core/plugins
   ```

   ```bash
   git clone https://github.com/xiowo/QQBotUID.git QQBotUID
   ```

2. 重启核心，插件会在 `on_core_start` 阶段读取 Bot 列表并启动。
3. 网页控制台 → 插件页面 → **Bot 管理** → 扫码添加，或手动填 AppID / Secret。
4. 网页控制台 → 插件配置 → **QQBotUID配置**，确认 `启用 QQBotUID` 已打开。

## 📝 配置指南

配置分三个地方，互不重叠：

| 位置 | 管什么 |
| :--- | :--- |
| 插件页面 → **Bot 管理** | 机器人列表：启用状态、接入方式、扫码添加、群 / 频道二维码 |
| 插件配置 → **QQBotUID配置** | 全局开关、独立进程模式、Webhook 监听参数 |
| 插件配置 → **高级配置** | 接口地址、客户端名称、主动消息队列、消息类型 |

### 页签：QQBotUID配置

**〔运行控制〕**

| 配置项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| **启用 QQBotUID** | 总开关，关掉后所有机器人都不会启动 | `true` |
| **启用 Webhook** | Webhook 总开关，只有打开时 Webhook 模式的机器人才会监听端口 | `false` |
| **忽略其他机器人消息** | 发送者是机器人的消息直接丢弃，不交给核心处理 | `true` |
| **重连间隔 (ms)** | 断线后等多少毫秒重连，`0` 表示断开立刻重连 | `0` |
| **获取群名称** | 开启后调 `/v2/groups/{group_openid}/info` 查群名并缓存 24 小时；关闭则群名统一用 `群聊-xxxxxxxx` 兜底，不请求接口、也不建缓存表 | `true` |

**〔Webhook〕**

| 配置项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| **Webhook 监听地址** | 监听哪个网卡，`0.0.0.0` 表示全部；核心在 Docker 里时留 `0.0.0.0` 并映射端口 | `0.0.0.0` |
| **Webhook 监听端口** | 所有 Webhook 机器人共用这一个端口，按回调路径里的 AppID 分发 | `8766` |
| **Webhook 路径** | 官方后台填的回调路径，插件会在后面追加每个机器人的 AppID | `/qqbotuid/webhook` |

### 页签：高级配置

**〔接口地址〕**

| 配置项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| **QQ API Base** | 官方开放平台接口地址，一般不用改 | `https://api.bot.qq.com` |
| **QQ Token Base** | 获取 `access_token` 的地址，一般不用改 | `https://api.bot.qq.com` |

**〔连接细节〕**

| 配置项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| **固定 Gateway URL** | 留空时由 `/gateway/bot` 自动获取；只在自建网关时才需要填 | `空` |
| **客户端名称** | 写进官方 WebSocket User-Agent 的客户端名 | `QQBotUID` |

**〔主动消息队列〕**

被动消息只能在收到消息后 5 分钟内带 `msg_id` 回复。超过窗口的发送（订阅推送、定时任务等）只能走主动消息，官方对这类消息有频率限制，所以统一排队。

群聊和私聊是两条独立车道，互不阻塞；同一车道内按提交顺序**放行**，相邻两条之间至少隔下面的毫秒数。


| 配置项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| **私聊主动间隔 (ms)** | 私聊车道两条主动消息之间的放行间隔；填 `0` 立刻放行 | `110` |
| **群聊主动间隔 (ms)** | 群聊车道两条主动消息之间的放行间隔；填 `0` 立刻放行 | `1010` |

**〔消息类型〕**

| 配置项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| **流式发送** | 私聊的纯文本 / markdown 回复按分片下发，像打字机一样逐段出现；失败自动回退普通消息。群聊没有流式接口，不受影响；带图片或按钮的回复也不走流式 | `false` |
| **流式分片字符数** | 每次下发的字符数，越小越像打字机，请求次数也越多。内容比这个值短时会自动对半分，保证至少出两片 | `200` |
| **流式分片间隔 (ms)** | 两次分片之间等多久 | `100` |
| **私聊正在输入** | 收到私聊后先发一条「正在输入」状态；会占 1 次被动回复额度，剩最后 1 次时不发 | `false` |
| **正在输入时长 (s)** | 状态持续秒数 | `30` |

**〔维护提示〕**

*断连维护*——**必须先打开〔独立进程模式〕里的 `启用独立进程`**，否则适配器跑在核心进程里，核心一停就全停了，没有进程能回提示（启动时会打一条 warning 提醒）。开了之后：核心重启、重载插件的时候 daemon 和核心会短暂断开，这段时间入站消息本来会被直接丢掉；打开这个开关后，daemon 自己回一句维护提示，用户不会以为机器人死了。核心退出**也不再结束 daemon 进程**，让它继续值守；核心再起来时按 `daemon.lock` 里记的 pid 把它接管回来，不会重复拉起。

*主人维护*——手动挂维护用。开启后**只有主人的消息会转给核心**，其他人不管发什么都只收到提示。不看核心连接状态，同进程 / 独立进程都生效。主人名单就是核心配置里的 `masters`

| 配置项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| **启用断连维护提示** | 总开关；**需要先开〔独立进程模式〕的 `启用独立进程`**，否则不生效；关掉则断连期间照旧丢弃消息，核心退出照旧结束 daemon | `false` |
| **断连次数** | 连续这么多次没连上核心才判定断连、开始回提示；`1` 表示一断开就提示 | `3` |
| **触发的插件** | 从已安装插件里**多选**（候选列表自动读 `plugins/` 和 `buildin_plugins/` 下的目录）；选了以后只有这些插件下的命令才回提示 | `空` |
| **启用主人维护** | 只放行主人消息，其他人一律回提示；不依赖核心连接状态 | `false` |
| **提示内容** | 两种维护共用这条文案，留空用默认文案 | `机器人维护中~\n正在更新框架(꒪⌓꒪)` |


### Bot 管理页（每条记录）

每条记录对应一个官方机器人，可以随时新增 / 删除 / 单独禁用。

| 项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| **启用** | 单独开关，关掉只停这一个 | `true` |
| **接入方式** | `WebSocket` 或 `Webhook`，改完保存就重载运行时 | `WebSocket` |
| **AppID** | 开放平台的应用 ID，扫码绑定会自动写入 | `空` |
| **Secret** | 应用密钥，列表里只显示「已设置」，不回显明文 | `空` |
| **群聊添加 / 频道添加** | 详情页里的两个二维码，扫码把机器人拉进群或频道 | — |

> 机器人昵称、头像、`uin`、`bot_id` 在扫码绑定或首次拉取时从官方接口取一次，之后存在 `data/QQBotUID/bots.json`。


## 🗂️ 群成员名单

插件从收到的事件里推「哪个群有哪些人」，只存 openid。不用开开关，也不用手动建表——启动时自己补。

| 表 | 一行是什么 | 什么时候写 |
| :--- | :--- | :--- |
| `QQBotUID_User` | 一个群里的一个 openid | 收到消息 / 入群事件加，退群事件删；内存攒 5 秒批量落库 |
| `QQBotUID_GroupInfo` | 一个群：群名、成员数、更新时间 | 收到群消息时只读缓存，没命中就丢后台查一次，24 小时内不重复请求 |

`QQBotUID_User` 只有 `bot_id`、`group_openid`、`user_openid` 三列（外加自增 `id`）。私聊的 `group_openid` 是空串，频道存 `channel_id`。

| 事件 | 动作 |
| :--- | :--- |
| 用户发消息（群 / 私聊 / 频道） | 加这个人 |
| `GROUP_MEMBER_ADD` | 加这个人 |
| `GROUP_MEMBER_REMOVE` | 删这个人 |

同一个人重复出现不会产生重复行；同一批里「退了又进」以最后一次为准。

`QQBotUID_GroupInfo` 的列是 `bot_id`、`group_openid`、`group_name`、`member_num`、`updated_at`。群名和成员数来自 `/v2/groups/{group_openid}/info`，拉取失败留空串并按 24 小时冷却，不会冲掉已经拿到的名字。


### 调用方式

控制帧沿插件与核心之间那条 ws 走：核心下发一帧 `MessageSend`，插件查完把结果塞进 `recall_message_id` 回执的 `id` 字段返回，不经过 HTTP、不占端口。

```python
import json
from gsuid_core.models import Message

receipts = await bot.send(
    Message(
        type="gscore_group_members_request",
        data={"request_id": "req-1", "group_id": "1A790910D2..."},
    ),
    wait_recall=True,
)
payload = json.loads(receipts[0]) if receipts else {}
```

| 帧 `type` | `data` 参数 | 回执内容 |
| :--- | :--- | :--- |
| `gscore_group_members_request` | `request_id`、`group_id`（或 `group_openid`）、`bot_id` | `group_openid` + `count` + `member_ids` |
| `gscore_qqbot_active_test` | `target_type`（`group` / `direct`）、`target_id`、`bot_self_id` | `ok` + `message_id` + `error` |
| `gscore_qqbot_admin_request` | `request_id`、`action`，其余参数随 action | `request_id` + `action` + `ok` + `data` / `error` |
| `gscore_qqbot_share_link_request` | `request_id`、`callback_data`（可选） | `request_id` + `ok` + `data`（`url` + `callback_data`）/ `error` |



回执长这样，`request_id` 原样带回，用来对账：

```json
{
  "request_id": "req-1",
  "ok": true,
  "group_openid": "1A790910D2...",
  "count": 2,
  "member_ids": ["9E5C1F3A4B2D...", "B41D77C0E19A..."]
}
```

几个必须注意的点：

- **`wait_recall=True` 不能省。** 不带它核心不会登记等待，`receipts` 永远是 `None`。
- **`request_id` 必填。** 缺了会回 `{"ok": false, "error": "missing request_id"}`，不会去查库。
- **等待上限 10 秒**（核心 `RECALL_WAIT_TIMEOUT`），超时 `receipts` 为空列表。查库是本地 SQLite，正常毫秒级。
- **别把这条连接的回执能力打坏。** 核心对「连续 3 次整轮零回执」的连接会关掉 recall，之后撤回和查询都拿不到回执。插件这边每帧都会回执（查库异常也转成 `ok: false`），但如果你在别处乱发 `wait_recall` 请求，要留意这个计数。

### 主动消息权限测试

`gscore_qqbot_active_test` 用来确认「机器人能不能在这个群里主动发言」。插件会**真的**往目标发一条「✅主动权限确认成功」，所以调用方每次都会在群里看到一条消息。

```json
{"ok": true, "message_id": "xxxx", "error": ""}
```

失败时 `ok` 为 `false`，`error` 是平台原话（带 `code`，比如没有主动消息权限时是 `code=40034106`）。

- **它绕开被动回复。** 发送时 `msg_id` 传空串，走的就是纯主动通道；如果拿入站消息的 `msg_id` 去发，平台会当成被动回复，测出来永远是成功。
- **它不进主动消息队列。** 和普通主动推送不同，这条是同步直发、立即返回结果，不受 `主动消息间隔` 限制。
- **回执同样走 `wait_recall=True`**，没有 `receipts` 就当失败处理。

## 🎙️ 语音转码

官方富媒体语音接口只稳定认 `silk`，其他格式经常被拒。所以发 `record` 段时：

| 格式 | 处理 |
| :--- | :--- |
| `silk` / `amr` / `mp3` / `wav` / `ogg` | 直接上传，不转码 |
| 其他（`flac`、`m4a`、`aac`、`opus`…） | `ffmpeg` 解成 24k 单声道 pcm，再用 `pilk` 编成 silk |
| 认不出格式 | 同上，交给 ffmpeg 自己探测 |

转码是**自动**的，不需要配置开关。

### 依赖

| 依赖 | 作用 | 缺了会怎样 |
| :--- | :--- | :--- |
| `ffmpeg` | 把源音频解成 pcm | 非直发格式的语音**打一条警告后跳过不发**，其余功能不受影响 |
| `pilk` | 把 pcm 编成 silk | 同上 |

```bash
# pilk 是 pip 包
pip install pilk

# ffmpeg 装到 PATH 里就行，插件只认 PATH，不会自己去翻目录
```

插件只做一件事：在 `PATH` 里找 `ffmpeg`（`shutil.which`）。找不到就当没有，不会去猜安装目录、也不读额外的环境变量 —— 装的时候确保 `ffmpeg -version` 在命令行能跑通即可。

找不到时的日志长这样，不会静默丢消息：

```
[QQBotUID] 语音是 flac，需要转成 silk 才能发；未检测到 ffmpeg，已跳过。
装好 ffmpeg 后重启即可自动转码
```

**探测结果在进程内缓存**，装好 ffmpeg 后要重启核心（独立进程模式下等 daemon 自己重载）才会重新识别。

## 🔗 分享链接与回调

生成一条「加机器人好友」的分享链接，用户通过它加好友时，你生成时传的 `callback_data` 会原样回到好友添加事件里 —— 用来区分渠道、邀请码、绑定关系。

### 生成链接

走独立的控制帧 `gscore_qqbot_share_link_request`（跟群管那套分开，因为它是机器人级别的操作，不属于某个群）：

```python
receipts = await bot.send(
    Message(
        type="gscore_qqbot_share_link_request",
        data={"request_id": "req-1", "callback_data": "invite-001"},
    ),
    wait_recall=True,
)
payload = json.loads(receipts[0])
url = payload["data"]["url"]
```

回执：

```json
{
  "request_id": "req-1",
  "ok": true,
  "data": {
    "url": "https://q.qq.com/qqbot/openchat_business?bot_appid=...&callback_data=invite-001",
    "callback_data": "invite-001"
  }
}
```

- `callback_data` **最长 32 字符**，超了直接回错，不会去请求平台。
- 不传就自动生成一个 32 位 hex（`uuid4().hex`），回执里带回来方便你存。
- 同一个 `callback_data` 可以重复用，平台不校验唯一性。
- **这条帧也在 `runner` 里处理，不在 `ws_api`** —— 它要 `sender` 去请求平台，`ws_api.CONTROL_TYPES` 里没有登记它。

### 收回调

用户加好友后，核心会收到 `meta-friend_add` 事件，`data` 长这样：

```json
{
  "user_id": "USER_OPENID",
  "scene": 2003,
  "scene_type": "开发者分享链接（站内）",
  "callback_data": "invite-001",
  "event_id": "EVT_..."
}
```

`scene` 是官方原始值，`scene_type` 是它对应的中文场景名：

| `scene` | 含义 |
| :--- | :--- |
| 1000 | 缺省默认值 |
| 1001 / 1002 | 网络搜索（全部 tab / 机器人 tab） |
| 1003 / 1004 | 群场景 / 空间场景 |
| 2001 / 2002 | 站内 / 站外分享资料页 |
| **2003 / 2004** | **开发者分享链接（站内 / 站外）** |

所以判断「是不是从我的分享链接来的」，看 `scene` 是不是 2003 或 2004，再看 `callback_data` 对不对。

### 限制

**只有 Webhook 模式能收到 `callback_data`**，WebSocket 模式下平台不会下发 `scene_param`（字段为空串）。如果你主要用 WS，这个功能只能用来生成链接，回调拿不到。

## 👮 群管与菜单接口

和群成员列表一样，走一条控制帧 `gscore_qqbot_admin_request`：核心发帧，插件代调官方 API，结果原样回执。

```python
import json
from gsuid_core.models import Message

receipts = await bot.send(
    Message(
        type="gscore_qqbot_admin_request",
        data={
            "request_id": "req-1",
            "action": "kick",
            "group_openid": "1A790910D2...",
            "member_openids": ["9E5C1F3A4B2D..."],
            "add_to_blacklist": False,
        },
    ),
    wait_recall=True,
)
payload = json.loads(receipts[0]) if receipts else {}
if payload.get("ok"):
    ...
```

回执统一是 `{"request_id", "action", "ok", "data" 或 "error"}`，`request_id` 原样带回：

```json
{"request_id": "req-1", "action": "kick", "ok": true}
{"request_id": "req-2", "action": "group_list", "ok": true, "data": [{"group_openid": "..."}]}
{"request_id": "req-3", "action": "kick", "ok": false, "error": "缺少 group_openid 或 member_openids"}
```

### action 一览

| `action` | 额外参数 | 回执 `data` |
| :--- | :--- | :--- |
| `kick` | `group_openid`、`member_openids`、`add_to_blacklist` | 无 |
| `mute` | `group_openid`、`member_openid`、`seconds`（0 为解禁） | 无 |
| `mute_state` | `group_openid` | 群内禁言状态 |
| `bot_state` | `group_openid` | 机器人在群里的身份（是否管理员） |
| `group_list` | 无 | 机器人所在的群列表，已自动翻页 |
| `join_requests` | `group_openid` | 入群申请列表 |
| `approve` | `group_openid`、`member_openid`、`join_request_id` | 无 |
| `decline` | `group_openid`、`member_openid`、`join_request_id`、`reason`、`add_to_blacklist` | 无 |
| `blacklist` | `group_openid` | 群黑名单 |
| `blacklist_add` / `blacklist_del` | `group_openid`、`member_openids` | 无 |
| `menu_get` | 无 | 全局自定义菜单 |
| `menu_set` | `menu`（非空 dict） | 无 |
| `panel_list` | `scope`（`group` / `c2c` / `channel` / `dm`，默认 `group`） | 指令面板列表 |
| `panel_create` | `scope`、`target_type`、`panel`、`user_openids`、`group_openids` | 新建的面板（含 `panel_id`） |
| `panel_delete` | `panel_id` | 无 |

`group_openid` 也可以写成 `group_id`，`member_openid` 也可以写成 `user_id`；列表参数写单个字符串会当成一项。

### 几个必须注意的点

- **`wait_recall=True` 不能省。** 不带它核心不会登记等待，`receipts` 永远是 `None`。
- **`request_id` 必填。** 缺了回 `{"ok": false, "error": "missing request_id"}`，不会去调平台。
- **未知 `action` 回 `ok: false`**，并在日志里打一条 `未知的群管 action`。
- **`seconds` 只认秒数**（`int` 或纯数字字符串），和核心 `bot.ban()` 的 `duration` 同一套解析；写 `"10m"` 这种会被判为参数不完整。
- **这条帧在 `runner` 里处理，不在 `ws_api`。** 它和 `gscore_qqbot_active_test` 一样需要 `sender`，`ws_api.CONTROL_TYPES` 里没有登记它。


## 🧩 独立进程模式

默认适配器跑在核心进程里。打开独立进程模式后，核心会在启动时自动拉起一个独立的 daemon 进程来承载适配器，关闭核心时一并关掉——插件崩了不会带走核心。只有开了断连维护提示，核心退出才会故意留下它。

### 怎么开

插件配置 → **QQBotUID配置** → 〔独立进程模式〕，把 `启用独立进程` 打开，重启核心即可。核心会自动拉起 daemon，不用手动跑。

daemon 意外退出时核心会自动重启它（2 秒起指数退避，最长 30 秒；稳定跑满 60 秒后退避归零）。核心启动时如果 `daemon.lock` 里记的 pid 还活着（30 秒内有心跳），说明上一轮核心留下的 daemon 还在跑，直接接管它、不再重复拉起；锁是死的（pid 不存在或心跳过期）就正常拉一个新的。daemon 自己启动时如果发现锁是活的，会打 error 日志并以退出码 `3` 退出，避免两个进程抢同一个 bot。

同机部署不用配任何东西：核心默认把 `127.0.0.1` 列在 `TRUSTED_IPS` 里。跨机才需要填 `核心地址`，并且要在核心那边配好 `WS_TOKEN`。

### 和同进程模式的差别

| 项 | 同进程 | 独立进程 |
| :--- | :--- | :--- |
| 崩溃隔离 | 和核心共享进程 | 独立进程，崩了不影响核心 |
| 核心停止后 | 机器人一起下线 | 开了断连维护提示就继续值守、回维护提示 |
| 核心图床 | 可用 | 可用，含 `CNBUpload` 这类插件接管的上传通道 |
| 改配置 | 保存即生效 | 保存后 daemon 3 秒内自己重载；图床配置单独一路，只重建客户端不重启 Bot |
| 改代码 | 重载插件即生效 | 重载插件 / 重启核心时自动换掉跑旧代码的 daemon |
| 部署 | 只跑核心 | 核心自动托管 daemon，仍是一个启动入口 |


### 常驻

不用额外配 systemd / nssm：daemon 由核心托管，核心退出时会先 `terminate`，8 秒内没退就 `kill`。想让它脱离核心单独跑（比如核心和 daemon 分机器部署），也可以直接执行 `python daemon.py`，这时关掉核心里的 `启用独立进程` 免得重复拉起。

开了高级配置里的 `启用断连维护提示` 之后，核心退出不再结束 daemon——它留在后台继续收 QQ 消息、回维护提示，等核心回来自己重连。核心下次启动时读到 `daemon.lock` 里还活着的 pid 就直接接管，不会再拉一个。要彻底停掉这种常驻的 daemon，关掉那个开关再关核心，或者手动结束 `daemon.lock` 里记的 pid。

### 看日志

| 方式 | 用法 |
| :--- | :--- |
| 插件自带脚本 | `python logs.py`，见下方命令 |
| 直接翻文件 | `data/logs/2026-10-29.log`，每行一条 JSON（`event` / `level` / `timestamp` / `plugin`） |

`logs.py` 在插件目录下：

```bash
python logs.py                  # 最近 50 条本插件的日志
python logs.py -n 200           # 最近 200 条
python logs.py -f               # 实时跟踪，Ctrl+C 退出
python logs.py -a               # 所有插件，不只本插件
python logs.py -d 2026-10-28    # 看某一天
python logs.py -k 维护          # 只留事件里含「维护」的行
python logs.py -l warning       # 只要 warning 及以上
```

### 配置项

在 **QQBotUID配置** 页的〔独立进程模式〕分组：

| 配置项 | 说明 | 默认值 |
| :--- | :--- | :--- |
| **启用独立进程** | 打开后核心拉起 daemon 承载适配器，关掉即回到同进程 | `false` |
| **核心地址** | daemon 连核心用的地址，同机填 `127.0.0.1` | `127.0.0.1` |
| **核心端口** | 要和核心配置里的 `PORT` 一致 | `8765` |
| **核心 WS Token** | 核心配置里的 `WS_TOKEN`，同机留空即可 | `空` |


## ❓ 常见问题 (FAQ)

### Q1: 日志提示「是 Webhook 模式但未启用『启用 Webhook』，已跳过监听」？
**A**: 机器人选了 `Webhook`，但 `QQBotUID配置` 里的 `启用 Webhook` 总开关没打开。两者都要开，Webhook 才会真正监听端口。

### Q2: Markdown 里的图片显示不出来？
**A**:
1. 官方要求 MD 里的图片要么是公网可访问的 URL，要么是素材管理返回的 `qqbot-media://` 引用；本地路径和内联 base64 都不行。
2. 检查是否开了「启用将图片转链接发送」，或者核心有没有可用的图床客户端；两样都没有时会回落 QQ 素材管理。
3. 素材管理有 10MB 单张上限、每个机器人 100 张配额，超限的图会被跳过（日志里会写明原因）；插件发完 10 秒会删掉刚上传的素材释放配额，已发出的消息不受影响。

### Q3: 能接多个机器人吗？
**A**: 能，Bot 管理页里加几条就是几个。`Webhook` 模式下所有机器人共用 `Webhook 监听端口` 这一个端口，靠回调路径里的 AppID 区分，不用多开端口；同一个 AppID 不要重复添加。


### Q4: 配置文件在哪？
**A**:

| 文件 | 内容 |
| :--- | :--- |
| `data/QQBotUID/config.json` | 全局开关与 Webhook 参数 |
| `data/QQBotUID/advanced_config.json` | 接口地址、客户端名称、主动队列间隔、维护提示 |
| `data/QQBotUID/bots.json` | 机器人清单，**含 AppSecret，别外传** |
| `data/QQBotUID/commands.json` | 插件名 → 命令词快照，核心启动 / 退出时自动重写，不用手改 |


### Q5: 回复走主动消息还是被动消息？
**A**: 插件收到消息时会记下 `msg_id`，5 分钟内的回复带上它，算被动消息，不消耗主动额度。超过窗口的会去掉 `msg_id` 走主动队列——订阅推送就是典型，它带的是注册订阅时的旧 `msg_id`，必然过期，所以固定走主动，按间隔排队发出。

## 📄 License

MIT License
