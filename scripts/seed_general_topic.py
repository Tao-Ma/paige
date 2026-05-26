"""One-shot: post a seed message in the paige topic-mode group and
create a `general` topic rooted at it.

Reads `PAIGE_FEISHU_APP_ID`, `PAIGE_FEISHU_APP_SECRET`, and
`PAIGE_FEISHU_GROUP_ID` from `~/.paige/.env` (or the process env if
absent). Persists `{seed_msg_id, root_msg_id, thread_id}` to
`~/.paige/topic_seed.json` so future runs can recover the seed
without re-posting.

Idempotent: if `~/.paige/topic_seed.json` already exists, this
script exits without sending anything.

Run from the project root:

    ~/.paige/venv/bin/python scripts/seed_general_topic.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)


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
    parser.add_argument(
        "--env",
        default=str(Path.home() / ".paige" / ".env"),
        help="Dotenv path to source (default: ~/.paige/.env)",
    )
    parser.add_argument(
        "--seed-file",
        default=str(Path.home() / ".paige" / "topic_seed.json"),
        help="Where to persist the seed marker (default: ~/.paige/topic_seed.json)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-seed even if the marker file already exists.",
    )
    args = parser.parse_args()

    seed_file = Path(args.seed_file)
    if seed_file.exists() and not args.force:
        existing = json.loads(seed_file.read_text())
        print(f"✓ Seed already exists at {seed_file}:")
        print(f"  thread_id:    {existing.get('thread_id')}")
        print(f"  root_msg_id:  {existing.get('root_msg_id')}")
        print(f"  seed_msg_id:  {existing.get('seed_msg_id')}")
        print("Use --force to re-seed.")
        return 0

    _load_env_file(Path(args.env))
    app_id = os.environ.get("PAIGE_FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("PAIGE_FEISHU_APP_SECRET", "").strip()
    chat_id = os.environ.get("PAIGE_FEISHU_GROUP_ID", "").strip()
    if not app_id or not app_secret or not chat_id:
        print(
            "PAIGE_FEISHU_APP_ID, PAIGE_FEISHU_APP_SECRET, and PAIGE_FEISHU_GROUP_ID required",
            file=sys.stderr,
        )
        return 2

    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

    # Step 1: post the seed message in the main chat. Plain text — its
    # only job is to anchor the general topic.
    seed_body = (
        CreateMessageRequestBody.builder()
        .receive_id(chat_id)
        .msg_type("text")
        .content(
            json.dumps(
                {"text": "🧭 paige · general — chat with the bot here, spawn pane topics by /start."}
            )
        )
        .build()
    )
    seed_req = (
        CreateMessageRequest.builder().receive_id_type("chat_id").request_body(seed_body).build()
    )
    seed_resp = client.im.v1.message.create(seed_req)
    if not seed_resp.success() or not seed_resp.data:
        print(f"seed post failed: code={seed_resp.code} msg={seed_resp.msg}", file=sys.stderr)
        return 1
    seed_msg_id = seed_resp.data.message_id or ""
    if not seed_msg_id:
        print("seed post returned no message_id", file=sys.stderr)
        return 1

    # Step 2: reply with reply_in_thread=true to spawn the topic.
    reply_body = (
        ReplyMessageRequestBody.builder()
        .msg_type("text")
        .content(json.dumps({"text": "general"}))
        .reply_in_thread(True)
        .build()
    )
    reply_req = ReplyMessageRequest.builder().message_id(seed_msg_id).request_body(reply_body).build()
    reply_resp = client.im.v1.message.reply(reply_req)
    if not reply_resp.success() or not reply_resp.data:
        print(f"thread spawn failed: code={reply_resp.code} msg={reply_resp.msg}", file=sys.stderr)
        return 1
    root_msg_id = reply_resp.data.message_id or ""
    thread_id = reply_resp.data.thread_id or ""
    if not root_msg_id or not thread_id:
        print(
            f"thread spawn returned partial payload: msg_id={root_msg_id!r} thread_id={thread_id!r}",
            file=sys.stderr,
        )
        return 1

    payload = {
        "chat_id": chat_id,
        "seed_msg_id": seed_msg_id,
        "root_msg_id": root_msg_id,
        "thread_id": thread_id,
    }
    seed_file.parent.mkdir(parents=True, exist_ok=True)
    seed_file.write_text(json.dumps(payload, indent=2) + "\n")
    print("✓ Seeded `general` topic")
    print(f"  thread_id:    {thread_id}")
    print(f"  root_msg_id:  {root_msg_id}")
    print(f"  seed_msg_id:  {seed_msg_id}")
    print(f"  persisted to: {seed_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
