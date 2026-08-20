"""CRUD for the user-maintained proxy pool, persisted at state/proxies.json."""
import json
import os
import uuid

import state_paths

_VALID_SCHEMES = ("http://", "https://", "socks5://")


def list_proxies() -> list[dict]:
    path = state_paths.proxies_file()
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(proxies: list[dict]) -> None:
    path = state_paths.proxies_file()
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(proxies, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def add_proxy(label: str, url: str) -> dict:
    if not label.strip():
        raise ValueError("label must not be empty")
    if not url.startswith(_VALID_SCHEMES):
        raise ValueError("url must start with http://, https:// or socks5://")
    proxies = list_proxies()
    entry = {"id": uuid.uuid4().hex[:8], "label": label, "url": url}
    proxies.append(entry)
    _save(proxies)
    return entry


def delete_proxy(proxy_id: str) -> bool:
    proxies = list_proxies()
    remaining = [p for p in proxies if p["id"] != proxy_id]
    if len(remaining) == len(proxies):
        return False
    _save(remaining)
    return True


def get_proxy_url(proxy_id: str) -> str | None:
    for p in list_proxies():
        if p["id"] == proxy_id:
            return p["url"]
    return None
