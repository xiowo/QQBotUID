from gsuid_core.sv import Plugins

Plugins(
    name="QQBotUID",
    force_prefix=[],
    allow_empty_prefix=True,
)

from .webui import *  # noqa: E402,F403
from .startup import *  # noqa: E402,F403
