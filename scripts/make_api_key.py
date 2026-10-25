"""Create an API key for a client. The key is shown once; the server stores only its hash."""

from __future__ import annotations

import argparse
import secrets

from guardsql.api.security import hash_key, parse_api_keys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="client name, e.g. alice (letters, digits, _ . -)")
    args = parser.parse_args()

    key = "gsql_" + secrets.token_urlsafe(32)
    entry = f"{args.name}:{hash_key(key)}"
    parse_api_keys(entry)  # rejects a bad client name before printing anything
    print(f"API key for {args.name} (give it to the client; it cannot be recovered):")
    print(f"  {key}\n")
    print("Add this entry to API_KEYS on the server (separate several with commas):")
    print(f"  {entry}")


if __name__ == "__main__":
    main()
