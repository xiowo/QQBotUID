from typing import List

from gsuid_core.sv import SV
from gsuid_core.bot import Bot
from gsuid_core.models import Event, Message

sv = SV("QQBotUID", pm=6, area="ALL")

_LABELS = {
    "group": ("群", "用户"),
    "direct": ("", "用户"),
    "channel": ("子频道", "用户"),
    "sub_channel": ("子频道", "用户"),
}


@sv.on_fullmatch("id")
async def qqbotuid_id(bot: Bot, ev: Event) -> None:
    blocks = _id_blocks(ev)
    if not blocks:
        await bot.send("好像出了点小问题，没有获取到OpenID")
        return
    await bot.send(Message(type="markdown", data="## 🐱猫猫的OpenID\n\n" + "\n\n".join(blocks)))


def _id_blocks(ev: Event) -> List[str]:
    group_label, user_label = _LABELS.get(str(ev.user_type or ""), ("", "用户"))
    blocks: List[str] = []
    if ev.group_id and group_label:
        blocks.append(f"```{group_label}\n{ev.group_id}\n```")
    if ev.user_id:
        blocks.append(f"```{user_label}\n{ev.user_id}\n```")
    return blocks
