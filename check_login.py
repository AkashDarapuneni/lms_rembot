"""Local check that calendar-link fetching works against your Moodle site.
Run:  python check_login.py   (asks for your username and password; nothing is saved)"""
import asyncio
import getpass
import os

import mini_login

SITE = os.getenv("DEFAULT_SITE", "lms.kluniversity.in")


async def main():
    user = input(f"Username for {SITE}: ").strip()
    pw = getpass.getpass("Password: ")
    for name, fn in (("web service API", mini_login._via_api), ("website login", mini_login._via_web)):
        try:
            url = await fn(SITE, user, pw)
            masked = url.split("authtoken=")[0] + "authtoken=" + url.split("authtoken=")[1][:4] + "..."
            print(f"[OK]   {name}: {masked}")
        except Exception as ex:
            print(f"[FAIL] {name}: {ex}")


asyncio.run(main())
