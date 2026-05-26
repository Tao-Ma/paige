"""One-shot: create a Lark Topic Mode Group and print its chat_id.

Reads `PAIGE_FEISHU_APP_ID`, `PAIGE_FEISHU_APP_SECRET`, and
`PAIGE_ALLOWED_USERS` from `~/.paige/.env` (or the process env if
the file is absent). Creates a chat with `chat_mode=group` +
`group_message_type=thread` (the recommended "Topic Mode Group"
variant — supports both plain messages and per-message topics).

Adds the first user in `PAIGE_ALLOWED_USERS` as a member.

Prints `PAIGE_FEISHU_GROUP_ID=oc_…` on success — paste that into
`~/.paige/.env` and `prod.sh restart` to start logging topic-mode
intent at startup.

Run from the project root:

    ~/.paige/venv/bin/python scripts/create_topic_group.py [--name <name>]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateChatRequest, CreateChatRequestBody


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="paige", help='Chat name (default: "paige")')
    parser.add_argument(
        "--env",
        default=str(Path.home() / ".paige" / ".env"),
        help="Path to a dotenv file to source (default: ~/.paige/.env)",
    )
    args = parser.parse_args()

    _load_env_file(Path(args.env))

    app_id = os.environ.get("PAIGE_FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("PAIGE_FEISHU_APP_SECRET", "").strip()
    allowed_csv = os.environ.get("PAIGE_ALLOWED_USERS", "").strip()
    if not app_id or not app_secret:
        print("PAIGE_FEISHU_APP_ID and PAIGE_FEISHU_APP_SECRET are required", file=sys.stderr)
        return 2

    user_ids = [u.strip() for u in allowed_csv.split(",") if u.strip()]

    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

    body = (
        CreateChatRequestBody.builder()
        .name(args.name)
        .description("paige — pane-per-topic routing")
        .chat_mode("group")
        .group_message_type("thread")
        .chat_type("private")
        .user_id_list(user_ids)
        .build()
    )
    req = CreateChatRequest.builder().user_id_type("open_id").request_body(body).build()
    resp = client.im.v1.chat.create(req)
    if not resp.success():
        print(f"chat.create failed: code={resp.code} msg={resp.msg}", file=sys.stderr)
        return 1

    chat_id = resp.data.chat_id if resp.data else ""
    if not chat_id:
        print("chat.create returned no chat_id", file=sys.stderr)
        return 1

    print(f"✓ Created topic-mode group: {args.name}")
    print(f"  chat_id: {chat_id}")
    if user_ids:
        print(f"  members: {', '.join(user_ids)}")
    print()
    print("Add this to ~/.paige/.env (then `prod.sh restart`):")
    print(f"  PAIGE_FEISHU_GROUP_ID={chat_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
