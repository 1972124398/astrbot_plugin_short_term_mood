"""Exercise AstrBot's real loader in a fresh process and runtime directory."""

import asyncio
import hashlib
import inspect
import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

root = Path(sys.argv[1]).resolve()
source = Path(sys.argv[2]).resolve()
destination = root / "data/plugins/astrbot_plugin_short_term_mood"
destination.parent.mkdir(parents=True)
shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "data"))
os.environ["ASTRBOT_ROOT"] = str(root)
sys.path.insert(0, str(root))

from astrbot.core import LogBroker, db_helper
from astrbot.core.core_lifecycle import AstrBotCoreLifecycle

other = root / "data/plugin_data/another_plugin"
other.mkdir(parents=True)


def fingerprint(path):
    stat = path.stat()
    return (hashlib.sha256(path.read_bytes()).hexdigest(), stat.st_mtime_ns, stat.st_mode, stat.st_uid, stat.st_gid)


sentinels = {}
for name in ("control.sqlite", "control.sqlite-shm", "control.sqlite-wal"):
    target = other / name
    target.write_bytes(b"Existing other-plugin data: " + name.encode())
    sentinels[target] = fingerprint(target)

database_opens = []
own_data = (root / "data/plugin_data/astrbot_plugin_short_term_mood").resolve()


def audit(event, args):
    if event not in {"sqlite3.connect", "os.chmod", "os.chown", "subprocess.Popen"}:
        return
    frame = inspect.currentframe().f_back
    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        if module.startswith("data.plugins.astrbot_plugin_short_term_mood."):
            if event == "subprocess.Popen":
                raise RuntimeError("Plugin installation attempted to launch an external service")
            target = Path(args[0]).resolve()
            if event == "sqlite3.connect":
                if target != own_data / "mood.sqlite3":
                    raise RuntimeError("Plugin opened a database outside its own data directory")
                database_opens.append(target.name)
            elif not target.is_relative_to(own_data):
                raise RuntimeError("Plugin changed permissions outside its own data directory")
            break
        frame = frame.f_back


sys.addaudithook(audit)


async def main():
    core = AstrBotCoreLifecycle(LogBroker(), db_helper)
    # Provider metadata refresh is unrelated to plugin installation and requires network access.
    with patch("astrbot.core.core_lifecycle.update_llm_metadata", AsyncMock()):
        await core.initialize()
    plugin = next(star for star in core.star_context.get_all_stars() if star.name == "astrbot_plugin_short_term_mood")
    assert plugin.star_cls is not None and plugin.activated
    assert plugin.config is not None and plugin.config["enabled"] is True
    assert plugin.config.schema["analysis_provider_id"]["_special"] == "select_provider"
    success, message = await core.plugin_manager.reload(plugin.name)
    assert success, message
    await core.stop()
    assert sentinels == {path: fingerprint(path) for path in sentinels}
    assert database_opens and set(database_opens) == {"mood.sqlite3"}
    print(json.dumps({"installed": True, "configured": True, "hot_reloaded": success,
                      "other_plugin_data_unchanged": True, "databases": database_opens}))


asyncio.run(main())
