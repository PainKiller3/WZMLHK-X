#!/usr/bin/env python3
"""
gen_scripts/gen_webhook_config.py
Generates secure webhook secret keys and output configurations for Mirror Bot and Sub Bot.
"""

from secrets import token_hex
from sys import argv


def main():
    server_host = argv[1] if len(argv) > 1 else "YOUR_MIRROR_SERVER_IP_OR_DOMAIN"
    port = argv[2] if len(argv) > 2 else "8080"

    webhook_url = f"http://{server_host}:{port}/webhook/subscription-bot"
    webhook_secret = token_hex(16)
    webhook_api_key = token_hex(16)

    print("============================================================")
    print("MIRROR BOT & SUB BOT WEBHOOK CONFIG GENERATOR")
    print("============================================================")
    print("\n1. PASTE THIS INTO MIRROR BOT (config.env):\n")
    print(f'SERVICE_BOT_WEBHOOK_URL = "{webhook_url}"')
    print(f'SERVICE_BOT_WEBHOOK_SECRET = "{webhook_secret}"')
    print(f'SERVICE_BOT_WEBHOOK_API_KEY = "{webhook_api_key}"')

    print("\n" + "-" * 60)
    print("\n2. SEND THIS TO SUB BOT DEVELOPER (for Sub Bot .env):\n")
    print(f"SERVICE_BOT_WEBHOOK_URL={webhook_url}")
    print(f"SERVICE_BOT_WEBHOOK_SECRET={webhook_secret}")
    print(f"SERVICE_BOT_WEBHOOK_API_KEY={webhook_api_key}")
    print("============================================================")


if __name__ == "__main__":
    main()
