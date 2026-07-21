#!/usr/bin/env python3
"""
Publish Windows auto-update files to the Telegram update channels.

The client resolves the feed channel over MTProto, reads its latest message,
parses it as a JSON map of platform -> channel -> type -> "<version>:<files
channel>#<message id>", and downloads the referenced document. So publishing is
two moves: upload each update file to the files channel, then post one feed
message that points at those uploads.

The feed's latest message must carry every platform at once (the client only
reads one message), so this merges the new entries onto the previous feed JSON
rather than replacing it - a single-arch run then keeps the other platform live.

Env:
  TG_API_ID, TG_API_HASH, TG_SESSION  - uploader account credentials (secrets)
  TG_FEED_CHANNEL   default frkgrmfeed2
  TG_FILES_CHANNEL  default frkgrmfiles
  ARTIFACTS_DIR     where the downloaded build artifacts live
  TG_ENTRY_KEY      "released" (default) or "testing"
  TG_DRY_RUN        "1" to resolve and compose without uploading or posting
  TG_SCHEDULE_DAYS  post as scheduled messages this many days ahead (0 = now).
                    A smoke test that exercises the real upload and post path
                    while keeping everything out of the live channel: the
                    messages sit in each channel's Scheduled queue, invisible to
                    subscribers, until the far-future date (delete them after).
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

FEED = os.environ.get("TG_FEED_CHANNEL", "frkgrmfeed2")
FILES = os.environ.get("TG_FILES_CHANNEL", "frkgrmfiles")
ARTIFACTS_DIR = os.environ.get("ARTIFACTS_DIR", "artifacts")
ENTRY_KEY = os.environ.get("TG_ENTRY_KEY", "released")
DRY_RUN = os.environ.get("TG_DRY_RUN", "") == "1"
SCHEDULE_DAYS = int(os.environ.get("TG_SCHEDULE_DAYS", "0") or "0")

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
                # Same platform twice means duplicate artifacts; keep the first
                # and refuse to guess which is authoritative.
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
        merged = load_previous_feed(previous[0].message if previous else "")

        when = None
        if SCHEDULE_DAYS > 0:
            when = datetime.now(timezone.utc) + timedelta(days=SCHEDULE_DAYS)
            print(f"Scheduling every message for {when:%Y-%m-%d} "
                  f"({SCHEDULE_DAYS} days out); nothing appears in the channels now.")

        # The fork's feed points every channel/type key of a platform at the same
        # update message. A released build therefore lands in all four (beta and
        # stable, released and testing); a testing build only touches the testing
        # keys, leaving released users on the previous version.
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
        posted = await client.send_message(feed, text, schedule=when)
        where = "scheduled" if when else "posted"
        print(f"\n{where} feed message #{posted.id} to {FEED}.")


if __name__ == "__main__":
    asyncio.run(main())
