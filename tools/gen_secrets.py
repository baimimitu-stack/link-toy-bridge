"""生成这套东西要用的随机密钥（只在本机打印，不联网）。

    python tools/gen_secrets.py

会打印四串：
  BRIDGE_TOKEN      电脑 .env 里，本机桥的钥匙
  LINK_RELAY_TOKEN  电脑 .env 里；同一串也要写进服务器的 RELAY_TOKEN
  LINK_TOKEN        服务器 /etc/link-mcp.env 里；手机/Grok 的地址和钥匙都是它
  （RELAY_TOKEN 和 LINK_TOKEN 必须不一样）
"""
import secrets

bridge = secrets.token_hex(24)
relay = secrets.token_hex(24)
link = secrets.token_hex(24)

print("# ── 电脑 .env ──")
print(f"BRIDGE_TOKEN={bridge}")
print(f"LINK_RELAY_TOKEN={relay}")
print()
print("# ── 服务器 /etc/link-mcp.env ──")
print(f"LINK_TOKEN={link}")
print(f"RELAY_TOKEN={relay}   # 和电脑的 LINK_RELAY_TOKEN 是同一串")
print()
print("# 小手机「钥匙」栏、Grok 网址里的那串 = LINK_TOKEN（上面第三串）")
print("# 这些只打印在你自己的终端里，别发到聊天、别提交到 git。")
