"""Feishu (Lark) backend.

Public surface (filled in across slices):
    from paige.adapters.feishu.channel import FeishuChannel   # slice 15b

Other modules here are implementation details — pure rendering
helpers and the lark-oapi client wrapper. Only
`entrypoint/main.py` may import from this subpackage; application
services see `paige.ports.channel.Channel` and don't know Feishu
exists.

Slice layout:

    post.py      — neutral text → Feishu post envelope        (15a)
    channel.py   — FeishuChannel (text outbound + inbound)    (15b)
    inbound.py   — pure event → Inbound/ActionEvent converter (15b)
    client.py    — LarkClientWrapper: retries + token refresh (15b)
    cards.py     — Card → Feishu card JSON + tap dispatch     (15c)
    (attachments + image upload + edit-window fallback come later)
"""
