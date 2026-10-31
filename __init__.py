from gsuid_core.sv import Plugins

Plugins(
    name="QQBotUID",
    force_prefix=["qqbotuid"],
    allow_empty_prefix=False,
)

from . import id_command  # noqa: E402,F401
from .webui import *  # noqa: E402,F403
from .qq_api import patch_image_fallback  # noqa: E402
from .startup import *  # noqa: E402,F403

patch_image_fallback()
