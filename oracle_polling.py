import json
import time

from bot import TOKEN, handle_update, tg


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN kiritilmagan")

    tg("deleteWebhook", {"drop_pending_updates": "false"})
    offset = 0
    print("Oracle polling bot ishga tushdi", flush=True)

    while True:
        try:
            updates = tg(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 50,
                    "allowed_updates": json.dumps(["message", "callback_query"]),
                },
                timeout=65,
            ) or []
            for update in updates:
                offset = max(offset, update["update_id"] + 1)
                handle_update(update)
        except Exception as exc:
            print(f"Polling xatosi: {exc}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
