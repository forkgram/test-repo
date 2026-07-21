#!/usr/bin/env python3
"""
Publish Windows auto-update files to the Telegram update channels.

The client reads the feed channel's latest message over MTProto - a JSON map of
platform -> channel -> type -> "<version>:<files channel>#<message id>" - and
downloads the referenced document. So: upload each update file to the files
channel, then post one feed message pointing at the uploads. That message must
carry every platform at once, so new entries are merged onto the previous feed
JSON rather than replacing it.

Env: TG_API_ID, TG_API_HASH, TG_SESSION (uploader account, secrets);
TG_FEED_CHANNEL, TG_FILES_CHANNEL, ARTIFACTS_DIR;
TG_ENTRY_KEY "released"/"testing"; TG_DRY_RUN "1"; TG_SCHEDULE_DAYS N (post N days
ahead into the Scheduled queue - an invisible smoke test); TG_FEED_MAX_AGE_DAYS D
(edit the latest feed message instead of posting a new one when it is <= D days
old and already carries this version, so one message covers all platforms;
default 2).
"""
import os
import re
import sys
import json
import glob
import asyncio
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient
from telethon.sessions import StringSession

FEED = os.environ["TG_FEED_CHANNEL"]
FILES = os.environ["TG_FILES_CHANNEL"]
ARTIFACTS_DIR = os.environ.get("ARTIFACTS_DIR", "artifacts")
ENTRY_KEY = os.environ.get("TG_ENTRY_KEY", "released")
DRY_RUN = os.environ.get("TG_DRY_RUN", "") == "1"
SCHEDULE_DAYS = int(os.environ.get("TG_SCHEDULE_DAYS", "0") or "0")
MAX_AGE_DAYS = int(os.environ.get("TG_FEED_MAX_AGE_DAYS", "2") or "2")

# Update file name -> platform key the client matches against Platform::AutoUpdateKey().
NAME_TO_PLATFORM = [
    (re.compile(r"^tx64upd(\d+)$"), "win64"),
    (re.compile(r"^tarm64upd(\d+)$"), "winarm64"),
    (re.compile(r"^tupdate(\d+)$"), "win"),
]


def find_update_files(root):
    """Return {platform: (version:int, path)} for every update file under root."""
    result = {}
    for path in sorted(glob.glob(os.path.join(root, "**", "*"), recursive=True)):
        if not os.path.isfile(path):
            continue
        name = os.path.basename(path)
        for rx, platform in NAME_TO_PLATFORM:
            m = rx.match(name)
            if not m:
                continue
            if platform in result:
                # Duplicate artifacts for one platform - refuse to guess.
                sys.exit(f"Two update files map to {platform}: "
                         f"{result[platform][1]} and {path}")
            result[platform] = (int(m.group(1)), path)
            break
    return result


def load_previous_feed(text):
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        print("Previous feed message is not JSON; starting fresh.")
        return {}
    return data if isinstance(data, dict) else {}


def versions_in(feed):
    """Every version integer referenced by any entry ('<ver>:chan#id') in a feed."""
    found = set()
    for platform in feed.values():
        if not isinstance(platform, dict):
            continue
        for chan in platform.values():
            if not isinstance(chan, dict):
                continue
            for entry in chan.values():
                head = entry.split(":", 1)[0] if isinstance(entry, str) else ""
                if head.isdigit():
                    found.add(int(head))
    return found


async def main():
    updates = find_update_files(ARTIFACTS_DIR)
    if not updates:
        sys.exit(f"No update files found under {ARTIFACTS_DIR!r}.")

    print("Update files to publish:")
    for platform, (version, path) in sorted(updates.items()):
        size = os.path.getsize(path) / 1048576
        print(f"  {platform}: version {version}, {size:.0f} MiB, {path}")

    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    session = os.environ["TG_SESSION"]

    async with TelegramClient(StringSession(session), api_id, api_hash) as client:
        feed = await client.get_entity(FEED)
        files = await client.get_entity(FILES)

        previous = await client.get_messages(feed, limit=1)
        prev_msg = previous[0] if previous else None
        merged = load_previous_feed(prev_msg.message if prev_msg else "")
        prev_versions = versions_in(merged)
        release_version = max(v for v, _ in updates.values())

        when = None
        if SCHEDULE_DAYS > 0:
            when = datetime.now(timezone.utc) + timedelta(days=SCHEDULE_DAYS)
            print(f"Scheduling every message for {when:%Y-%m-%d} "
                  f"({SCHEDULE_DAYS} days out); nothing appears in the channels now.")

        # The feed sets all four keys per platform (beta/stable x released/testing)
        # to one message; a released build lands in all, a testing build only in
        # the testing keys, leaving released users on the previous version.
        if ENTRY_KEY == "testing":
            targets = [("beta", "testing"), ("stable", "testing")]
        else:
            targets = [(chan, key)
                       for chan in ("beta", "stable")
                       for key in ("released", "testing")]

        for platform, (version, path) in sorted(updates.items()):
            if DRY_RUN:
                entry = f"{version}:{FILES}#<dry-run>"
                print(f"[dry-run] would upload {path} -> {platform}")
            else:
                msg = await client.send_file(
                    files, path,
                    force_document=True,
                    caption=f"{os.path.basename(path)} ({platform})",
                    schedule=when)
                entry = f"{version}:{FILES}#{msg.id}"
                print(f"uploaded {platform}: {entry}")
            entry_map = merged.setdefault(platform, {})
            for chan, key in targets:
                entry_map.setdefault(chan, {})[key] = entry

        text = json.dumps(merged, separators=(",", ":"), sort_keys=True)
        print("\nFeed JSON:")
        print(text)

        if DRY_RUN:
            print("\n[dry-run] not posting the feed message.")
            return

        # Edit the latest message in place when it belongs to this release wave -
        # recent and already carrying this version (e.g. Mac published it first) -
        # so one message covers every platform. Otherwise post a new one. A
        # scheduled test never edits the live message.
        age = (datetime.now(timezone.utc) - prev_msg.date) if prev_msg else None
        same_wave = (when is None
                     and prev_msg is not None
                     and age <= timedelta(days=MAX_AGE_DAYS)
                     and release_version in prev_versions)
        if same_wave:
            await client.edit_message(feed, prev_msg.id, text)
            print(f"\nedited feed message #{prev_msg.id} in {FEED} "
                  f"(age {age.days}d, version {release_version} already present).")
        else:
            posted = await client.send_message(feed, text, schedule=when)
            reason = ("scheduled test" if when
                      else "no recent same-version message" if prev_msg
                      else "empty feed")
            print(f"\n{'scheduled' if when else 'posted'} feed message "
                  f"#{posted.id} to {FEED} ({reason}).")


if __name__ == "__main__":
    asyncio.run(main())
