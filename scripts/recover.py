"""Inspect or explicitly reset operational HALTED state, without network access."""

import argparse
import json

from telegram_mcp.paths import PROJECT_ROOT
from telegram_mcp.rpc import RpcState, load_limits
from telegram_mcp.session_lock import SessionLock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true", help="Explicitly reset HALTED after reviewing uncertain outcomes")
    args = parser.parse_args()
    path = PROJECT_ROOT / "data" / "operations.sqlite3"
    with SessionLock():
        if not path.exists():
            print("No operational state database exists; nothing to inspect or reset.")
            return
        state = RpcState(path, load_limits(PROJECT_ROOT / "rate_limits.yaml"))
        try:
            print(json.dumps(state.inspect(), sort_keys=True))
            if args.confirm:
                state.recover()
                print("HALTED cleared. Counters and cooldowns retained. No Telegram request made.")
        finally:
            state.close()


if __name__ == "__main__":
    main()
