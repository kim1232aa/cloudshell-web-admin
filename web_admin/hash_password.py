#!/usr/bin/env python3
"""Print a PBKDF2 hash suitable for ADMIN_PASSWORD_HASH in .env.

Usage: python3 web_admin/hash_password.py
"""
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auth  # noqa: E402


def main() -> None:
    password = getpass.getpass("New admin password: ")
    confirm = getpass.getpass("Confirm: ")
    if password != confirm:
        print("passwords do not match", file=sys.stderr)
        sys.exit(1)
    if len(password) < 8:
        print("password must be at least 8 characters", file=sys.stderr)
        sys.exit(1)
    print(auth.hash_password(password))


if __name__ == "__main__":
    main()
