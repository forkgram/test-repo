#!/usr/bin/env python3
"""
One-time, LOCAL session minter for the release uploader account.

Run this on your own machine (never in CI). It logs the uploader account in
interactively (phone number + the login code Telegram sends, plus the 2FA
password if the account has one) and prints a Telethon StringSession.

    pip install telethon
    python mint_session.py

Put the three values into GitHub Actions secrets:
    TG_API_ID        - from https://my.telegram.org  (the UPLOADER account's own)
    TG_API_HASH      - from https://my.telegram.org
    TG_SESSION       - the string this script prints

The StringSession grants FULL control of whatever account you log in with, so
log in with a DEDICATED account that is an admin of only the two update
channels (TG_FEED_CHANNEL and TG_FILES_CHANNEL) - not your personal account.
"""
import os
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(os.environ.get("TG_API_ID") or input("api_id: ").strip())
api_hash = os.environ.get("TG_API_HASH") or input("api_hash: ").strip()

with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("\n--- copy the line below into the TG_SESSION secret ---\n")
    print(client.session.save())
    print("\n--- keep it secret; anyone with it controls this account ---")
    me = client.get_me()
    print(f"\nlogged in as: {me.first_name} (@{me.username or 'no-username'}, id={me.id})")
