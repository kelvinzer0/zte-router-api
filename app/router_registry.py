"""
Router registry for multi-router management.

Stores ZTE router/ONT connections (host, credentials, metadata) in a local
JSON file so routers can be added or edited from the Web UI without editing
environment variables or restarting the server.

Credentials are stored in plain text inside the registry file, so the file is
created with owner-only permissions (0600) and is git-ignored by default.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

# Registry file location (relative paths resolve against the process CWD)
DEFAULT_REGISTRY_PATH = os.environ.get(
    "ZTE_ROUTERS_FILE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "routers.json"),
)

# Fields accepted from API payloads
EDITABLE_FIELDS = ("name", "host", "port", "username", "password", "timeout", "location", "notes", "ssid")

# hostname / IPv4 with optional scheme, port or trailing path
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")


class RouterRegistryError(Exception):
    """Raised when a router entry is invalid or not found"""


def _now() -> str:
    """
    Current UTC timestamp in ISO format.

    Microsecond precision matters: this value doubles as the cache stamp that
    invalidates a router's HTTP session when its credentials change, so two
    edits within the same second must not look identical.
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _is_valid_host(host: str) -> bool:
    """Accept hostnames, IPv4 and IPv6 literals"""
    if not host:
        return False
    if _HOST_RE.match(host):
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def normalize_host(raw: Any) -> tuple[str, Optional[int]]:
    """
    Normalize user input into a bare host and optional port.

    Accepts ``192.168.1.1``, ``192.168.1.1:8080``, ``router.local`` and
    ``http://192.168.1.1/anything``.

    Returns:
        Tuple of (host, port or None when no port was given)
    """
    if raw is None:
        raise RouterRegistryError("Host wajib diisi")

    value = str(raw).strip()
    if not value:
        raise RouterRegistryError("Host wajib diisi")

    # Strip scheme and anything after the host (path, query, fragment)
    value = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", value)
    value = value.split("/")[0].split("?")[0].split("#")[0]
    value = value.strip()

    port: Optional[int] = None
    if value.startswith("["):  # bracketed IPv6, optionally with port
        closing = value.find("]")
        if closing == -1:
            raise RouterRegistryError("Format host tidak valid")
        host = value[1:closing]
        rest = value[closing + 1:]
        if rest.startswith(":"):
            port = rest[1:]
        elif rest:
            raise RouterRegistryError(f"Format host tidak valid: {raw}")
    elif value.count(":") == 1:
        host, _, port = value.partition(":")
    else:
        # Bare IPv6 address or malformed input
        host = value

    if not _is_valid_host(host):
        raise RouterRegistryError(f"Host tidak valid: {raw}")

    if port is not None and port != "":
        try:
            port_num = int(port)
        except ValueError:
            raise RouterRegistryError(f"Port tidak valid: {port}")
        if not 1 <= port_num <= 65535:
            raise RouterRegistryError("Port harus di antara 1 dan 65535")
        return host, port_num

    return host, None


def _as_int(value: Any, field_name: str, minimum: int, maximum: int, default: int) -> int:
    """Coerce a value to int within bounds"""
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise RouterRegistryError(f"{field_name} harus berupa angka")
    if not minimum <= number <= maximum:
        raise RouterRegistryError(f"{field_name} harus di antara {minimum} dan {maximum}")
    return number


def _as_text(value: Any, field_name: str, max_length: int, default: str = "") -> str:
    """Coerce a value to stripped text"""
    if value is None:
        return default
    text = str(value).strip()
    if len(text) > max_length:
        raise RouterRegistryError(f"{field_name} maksimal {max_length} karakter")
    return text


def clean_payload(payload: dict, partial: bool = False) -> dict:
    """
    Validate and normalize an API payload.

    Args:
        payload: Raw payload from the API
        partial: When True, missing fields are ignored (update flow)

    Returns:
        Dict with only editable fields
    """
    if not isinstance(payload, dict):
        raise RouterRegistryError("Payload harus berupa objek JSON")

    unknown = set(payload) - set(EDITABLE_FIELDS) - {"id"}
    if unknown:
        raise RouterRegistryError(f"Field tidak dikenal: {', '.join(sorted(unknown))}")

    result: dict[str, Any] = {}

    if "host" in payload or not partial:
        host, port = normalize_host(payload.get("host"))
        result["host"] = host
        if port is not None:
            result["port"] = port
        elif not partial:
            result["port"] = 80

    if "port" in payload and str(payload["port"]).strip() != "":
        result["port"] = _as_int(payload["port"], "Port", 1, 65535, 80)

    if "timeout" in payload or not partial:
        result["timeout"] = _as_int(payload.get("timeout"), "Timeout", 1, 300, 10)

    if "name" in payload or not partial:
        name = _as_text(payload.get("name"), "Nama", 64)
        if not name:
            raise RouterRegistryError("Nama wajib diisi")
        result["name"] = name

    if "username" in payload or not partial:
        username = _as_text(payload.get("username"), "Username", 64) or "admin"
        result["username"] = username

    if "password" in payload:
        # Empty password on update means "keep current password"
        result["password"] = _as_text(payload.get("password"), "Password", 128, default="")

    if "location" in payload or not partial:
        result["location"] = _as_text(payload.get("location"), "Lokasi", 128)

    if "notes" in payload or not partial:
        result["notes"] = _as_text(payload.get("notes"), "Catatan", 512)

    if "ssid" in payload or not partial:
        result["ssid"] = _as_text(payload.get("ssid"), "SSID", 128)

    return result


@dataclass
class RouterEntry:
    """A registered ZTE router/ONT"""

    id: str
    name: str
    host: str
    username: str = "admin"
    password: str = ""
    port: int = 80
    timeout: int = 10
    location: str = ""
    notes: str = ""
    ssid: str = ""
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    @property
    def address(self) -> str:
        """``host[:port]`` string used to build the ZTEClient"""
        if self.port and self.port != 80:
            return f"{self.host}:{self.port}"
        return self.host

    @property
    def url(self) -> str:
        """Full base URL of the router web interface"""
        return f"http://{self.address}"

    def to_dict(self, include_password: bool = False) -> dict:
        """Serialise entry, omitting the password unless explicitly requested"""
        data = asdict(self)
        data["address"] = self.address
        data["url"] = self.url
        data["has_password"] = bool(self.password)
        if not include_password:
            data.pop("password", None)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "RouterEntry":
        """Build an entry from stored data, ignoring unknown keys"""
        host, port = normalize_host(data.get("host"))
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex[:12]),
            name=_as_text(data.get("name"), "Nama", 64) or host,
            host=host,
            username=_as_text(data.get("username"), "Username", 64) or "admin",
            password=_as_text(data.get("password"), "Password", 128),
            port=int(data.get("port") or port or 80),
            timeout=int(data.get("timeout") or 10),
            location=_as_text(data.get("location"), "Lokasi", 128),
            notes=_as_text(data.get("notes"), "Catatan", 512),
            ssid=_as_text(data.get("ssid"), "SSID", 128),
            created_at=str(data.get("created_at") or _now()),
            updated_at=str(data.get("updated_at") or _now()),
        )


class RouterRegistry:
    """
    Thread-safe JSON-backed registry of routers.

    The file is written atomically (temp file + replace) so a crash mid-write
    cannot corrupt it. If the file cannot be read or written the registry keeps
    working in memory and logs a warning, so the API stays available.
    """

    def __init__(self, path: str = DEFAULT_REGISTRY_PATH, seed: Optional[dict] = None):
        self.path = path
        self._lock = threading.RLock()
        self._entries: dict[str, RouterEntry] = {}
        self._persistent = True
        self.load()
        if seed and not self._entries:
            self._create_seed(seed)

    # ---------------------------------------------------------------- storage

    def load(self) -> None:
        """Load entries from disk"""
        with self._lock:
            self._entries = {}
            if not os.path.exists(self.path):
                return
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    raw = json.load(handle)
            except (json.JSONDecodeError, OSError) as e:
                print(f"[registry] Gagal membaca {self.path}: {e} (mulai dengan daftar kosong)")
                self._persistent = False
                return

            entries = raw.get("routers", []) if isinstance(raw, dict) else raw
            for item in entries or []:
                try:
                    entry = RouterEntry.from_dict(item)
                except RouterRegistryError as e:
                    print(f"[registry] Melewati entri tidak valid: {e}")
                    continue
                self._entries[entry.id] = entry

    def _save(self) -> bool:
        """Atomically write the registry to disk"""
        payload = {
            "version": 1,
            "updated_at": _now(),
            "routers": [entry.to_dict(include_password=True) for entry in self._entries.values()],
        }
        tmp_path = f"{self.path}.tmp"
        try:
            directory = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(directory, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.path)
            return True
        except OSError as e:
            print(f"[registry] Gagal menyimpan {self.path}: {e} (perubahan hanya di memori)")
            self._persistent = False
            return False

    def _create_seed(self, seed: dict) -> None:
        """Create the initial entry from environment variables"""
        try:
            data = clean_payload(seed)
        except RouterRegistryError as e:
            print(f"[registry] Seed dari environment dilewati: {e}")
            return

        with self._lock:
            entry = RouterEntry(
                id="default",
                name=data.get("name") or "Router Default",
                host=data["host"],
                username=data.get("username", "admin"),
                password=data.get("password", ""),
                port=data.get("port", 80),
                timeout=data.get("timeout", 10),
                location=data.get("location", ""),
                notes="Dibuat otomatis dari environment variable (ZTE_HOST/ZTE_USERNAME/ZTE_PASSWORD).",
            )
            self._entries[entry.id] = entry
            self._save()

    # ------------------------------------------------------------------- CRUD

    def list(self) -> list[RouterEntry]:
        """All registered entries"""
        with self._lock:
            return list(self._entries.values())

    def get(self, router_id: str) -> RouterEntry:
        """Get one entry by id"""
        with self._lock:
            entry = self._entries.get(router_id)
        if entry is None:
            raise RouterRegistryError(f"Router '{router_id}' tidak ditemukan")
        return entry

    def find_by_host(self, host: str, port: int = 80) -> Optional[RouterEntry]:
        """Find an entry by host and port"""
        with self._lock:
            for entry in self._entries.values():
                if entry.host == host and entry.port == port:
                    return entry
        return None

    def create(self, payload: dict, router_id: Optional[str] = None) -> RouterEntry:
        """Create a new entry"""
        data = clean_payload(payload, partial=False)
        with self._lock:
            new_id = router_id or uuid.uuid4().hex[:12]
            if new_id in self._entries:
                raise RouterRegistryError(f"Router '{new_id}' sudah ada")
            entry = RouterEntry(id=new_id, **data)
            self._entries[new_id] = entry
            self._save()
            return entry

    def update(self, router_id: str, payload: dict) -> RouterEntry:
        """Update an existing entry (empty password keeps the current one)"""
        data = clean_payload(payload, partial=True)
        with self._lock:
            entry = self.get(router_id)
            if not data.get("password") and entry.password:
                data.pop("password", None)
            for key, value in data.items():
                setattr(entry, key, value)
            entry.updated_at = _now()
            self._save()
            return entry

    def delete(self, router_id: str) -> None:
        """Delete an entry"""
        with self._lock:
            self.get(router_id)
            del self._entries[router_id]
            self._save()

    def update_ssid(self, router_id: str, ssid: str) -> None:
        """Update SSID label for a router and persist to disk"""
        with self._lock:
            entry = self._entries.get(router_id)
            if entry and entry.ssid != str(ssid or "").strip():
                entry.ssid = str(ssid or "").strip()
                self._save()

    # ---------------------------------------------------------------- helpers

    @property
    def persistent(self) -> bool:
        """True while the registry can be written to disk"""
        return self._persistent

    def public_list(self) -> list[dict]:
        """All entries as dicts without passwords"""
        return [entry.to_dict() for entry in self.list()]