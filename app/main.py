"""
ZTE Router OpenAPI Server

RESTful API and Web UI for ZTE GPON routers (F609, F677V2, F8648P, etc.).
Provides programmatic access to router status, PON optical metrics, WiFi credentials,
LAN/WAN interfaces, and multi-router management.
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, File, HTTPException, Query, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import sys
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "app"

from .router_registry import RouterRegistry, RouterRegistryError
from .router_scanner import (
    AUTO_CREDENTIALS,
    check_tcp_port,
    parse_ip_targets,
    probe_zte_device,
    scan_and_add_routers,
    scan_range_generator,
)
from .zte_crypto import ZTEConfigError, decrypt_config_bin, extract_credentials_from_xml
from .zte_telnet import (
    ZTETelnetClient,
    ZTETelnetError,
    enable_telnet_factory_mode,
    test_telnet_port,
)
from .zte_client import (
    ConnectedDevice,
    DeviceInfo,
    LANPort,
    VoIPLine,
    WANConnection,
    ZTEAuthenticationError,
    ZTEClient,
    ZTEClientError,
    ZTESessionError,
)

# Configuration from environment (used as fallback when registry is empty)
ZTE_HOST = os.environ.get("ZTE_HOST", "192.168.178.1")
ZTE_USERNAME = os.environ.get("ZTE_USERNAME", "admin")
ZTE_PASSWORD = os.environ.get("ZTE_PASSWORD", "admin")
ZTE_TIMEOUT = int(os.environ.get("ZTE_TIMEOUT", "10"))

# Static directory
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Registry and Client pool
registry = RouterRegistry()
_client_pool: dict[str, tuple[str, ZTEClient]] = {}


def get_router_or_default(router_id: Optional[str] = None):
    """Retrieve router entry by ID or return first registered router"""
    if router_id:
        try:
            return registry.get(router_id)
        except RouterRegistryError:
            raise HTTPException(status_code=404, detail=f"Router '{router_id}' tidak ditemukan")
    routers = registry.list()
    if routers:
        return routers[0]
    return None


def get_client_for_router(router_id: Optional[str] = None) -> ZTEClient:
    """Get cached or new ZTEClient instance for the specified router"""
    entry = get_router_or_default(router_id)
    if entry:
        cid = entry.id
        fingerprint = f"{entry.address}:{entry.username}:{entry.password}:{entry.timeout}:{entry.updated_at}"
        if cid in _client_pool:
            old_fp, client = _client_pool[cid]
            if old_fp == fingerprint:
                return client
            try:
                if client.is_logged_in:
                    client.logout()
            except Exception:
                pass
        client = ZTEClient(
            host=entry.address,
            username=entry.username,
            password=entry.password,
            timeout=entry.timeout,
        )
        _client_pool[cid] = (fingerprint, client)
        return client

    # Fallback to env
    cid = "_env_default"
    fingerprint = f"{ZTE_HOST}:{ZTE_USERNAME}:{ZTE_PASSWORD}:{ZTE_TIMEOUT}"
    if cid in _client_pool:
        old_fp, client = _client_pool[cid]
        if old_fp == fingerprint:
            return client
    client = ZTEClient(
        host=ZTE_HOST,
        username=ZTE_USERNAME,
        password=ZTE_PASSWORD,
        timeout=ZTE_TIMEOUT,
    )
    _client_pool[cid] = (fingerprint, client)
    return client


def ensure_client_logged_in(client: ZTEClient) -> ZTEClient:
    """Ensure client is logged in and model is detected"""
    if not client.is_logged_in:
        try:
            client.login()
        except ZTEAuthenticationError as e:
            raise HTTPException(status_code=401, detail=f"Otentikasi gagal: {e}")
        except ZTEClientError as e:
            raise HTTPException(status_code=503, detail=f"Router tidak dapat dihubungi: {e}")
    if not client.model:
        try:
            client.get_device_info()
        except Exception:
            pass
    return client


def with_session_retry(func):
    """Decorator to retry on session or connection errors with fresh login"""
    def wrapper(client: ZTEClient):
        try:
            return func(client)
        except (ZTESessionError, ZTEClientError):
            client._logged_in = False
            client.login()
            return func(client)
    return wrapper


# ==============================================================================
# Pydantic Models
# ==============================================================================

class PONDataResponse(BaseModel):
    """PON optical module data"""
    onu_state: Optional[str] = Field(None, description="ONU registration state (O1-O5)")
    rx_power_dbm: Optional[float] = Field(None, description="Receive power in dBm")
    tx_power_dbm: Optional[float] = Field(None, description="Transmit power in dBm")
    voltage_mv: Optional[int] = Field(None, description="Supply voltage in mV")
    current_ma: Optional[float] = Field(None, description="Bias current in mA")
    temperature_c: Optional[float] = Field(None, description="Temperature in Celsius")
    video_rx_power_dbm: Optional[float] = Field(None, description="Video RX power in dBm")
    rf_tx_power_dbm: Optional[float] = Field(None, description="RF TX power in dBm")


class DeviceInfoResponse(BaseModel):
    """Device information"""
    model: Optional[str] = Field(None, description="Device model name")
    serial_number: Optional[str] = Field(None, description="Serial number")
    hardware_version: Optional[str] = Field(None, description="Hardware version")
    software_version: Optional[str] = Field(None, description="Firmware version")
    boot_version: Optional[str] = Field(None, description="Bootloader version")
    manufacturer: Optional[str] = Field(None, description="Manufacturer name")
    oui: Optional[str] = Field(None, description="Manufacturer OUI")


class LANPortResponse(BaseModel):
    """LAN port status"""
    name: str = Field(..., description="Port name (e.g., LAN1)")
    link: str = Field(..., description="Link status (Up/Down)")
    speed: Optional[int] = Field(None, description="Link speed in Mbps")
    duplex: Optional[str] = Field(None, description="Duplex mode (Full/Half)")
    bytes_rx: Optional[int] = Field(None, description="Bytes received")
    bytes_tx: Optional[int] = Field(None, description="Bytes transmitted")
    packets_rx: Optional[int] = Field(None, description="Packets received")
    packets_tx: Optional[int] = Field(None, description="Packets transmitted")


class VoIPLineResponse(BaseModel):
    """VoIP line status"""
    line_id: str = Field(..., description="Line identifier (e.g., Line1)")
    number: Optional[str] = Field(None, description="SIP URI or phone number")
    status: str = Field(..., description="Status (Registered/Unregistered/Inactive)")
    registered: bool = Field(..., description="Is line registered")


class WANConnectionResponse(BaseModel):
    """WAN connection status"""
    name: Optional[str] = Field(None, description="Connection name")
    connection_type: Optional[str] = Field(None, description="Connection type (PPPoE, DHCP, etc.)")
    status: Optional[str] = Field(None, description="Connection status")
    ip_address: Optional[str] = Field(None, description="IPv4 Address")
    gateway: Optional[str] = Field(None, description="Gateway Address")
    dns_servers: Optional[str] = Field(None, description="DNS Servers")
    tx_bytes: Optional[int] = Field(None, description="Bytes transmitted")
    rx_bytes: Optional[int] = Field(None, description="Bytes received")
    vlan_id: Optional[int] = Field(None, description="VLAN ID")
    mac_address: Optional[str] = Field(None, description="WAN MAC address")
    username: Optional[str] = Field(None, description="PPPoE Username")
    password: Optional[str] = Field(None, description="PPPoE Password")


class SystemStatsResponse(BaseModel):
    """System statistics"""
    cpu_usage: Optional[int] = Field(None, description="Average CPU usage percentage")
    memory_usage: Optional[int] = Field(None, description="Memory usage percentage")
    uptime_seconds: Optional[int] = Field(None, description="System uptime in seconds")
    flash_used_percent: Optional[int] = Field(None, description="Flash storage used percentage")


class ConnectedDeviceResponse(BaseModel):
    """Connected device info"""
    ip_address: str = Field(..., description="Device IP address")
    mac_address: str = Field(..., description="Device MAC address")
    interface: Optional[str] = Field(None, description="Connected interface (LAN1, etc.)")
    last_seen: Optional[str] = Field(None, description="Last connection time")


class WiFiNetworkResponse(BaseModel):
    """WiFi network SSID and passphrase configuration"""
    ssid: str = Field(..., description="WiFi SSID Name")
    password: Optional[str] = Field(None, description="WiFi Passphrase")
    enabled: bool = Field(True, description="Whether SSID is enabled")
    band: Optional[str] = Field("2.4G", description="Frequency Band (2.4G / 5G)")
    security: Optional[str] = Field("WPA/WPA2-PSK", description="Security mode")


class RouterResponse(BaseModel):
    """Router registry entry details"""
    id: str = Field(..., description="Unique router ID")
    name: str = Field(..., description="Friendly name")
    host: str = Field(..., description="Router IP or Hostname")
    port: int = Field(80, description="Web port")
    username: str = Field("admin", description="Admin username")
    timeout: int = Field(10, description="Timeout in seconds")
    location: Optional[str] = Field("", description="Router location")
    notes: Optional[str] = Field("", description="Notes")
    address: str = Field(..., description="Host:port address")
    url: str = Field(..., description="Router base URL")
    has_password: bool = Field(False, description="Whether a password is set")
    ssid: Optional[str] = Field("", description="Primary WiFi SSID label")
    created_at: Optional[str] = Field(None, description="Registration date")
    updated_at: Optional[str] = Field(None, description="Last update date")


class RouterListResponse(BaseModel):
    """List of registered routers"""
    routers: list[RouterResponse] = Field(..., description="List of registered routers")
    count: int = Field(..., description="Total routers count")
    registry_file: str = Field(..., description="Registry file path")
    persistent: bool = Field(True, description="Whether storage is persisted to disk")


class RouterCreatePayload(BaseModel):
    """Payload to add a new router"""
    name: str = Field(..., description="Friendly router name", max_length=64)
    host: str = Field(..., description="Router IP or hostname")
    port: Optional[int] = Field(80, description="Web port", ge=1, le=65535)
    credential_mode: Optional[str] = Field("manual", description="Mode kredensial: 'auto' (F609: admin/admin, F677V2: superadmin/suportadmin, F477V2: Administrator/suportadmin) atau 'manual'")
    model: Optional[str] = Field(None, description="Pilihan model router jika mode auto: 'F609', 'F677V2', 'F477V2', atau 'auto' untuk auto-detect")
    username: Optional[str] = Field("admin", description="Web admin username", max_length=64)
    password: Optional[str] = Field("", description="Web admin password", max_length=128)
    timeout: Optional[int] = Field(10, description="Request timeout seconds", ge=1, le=300)
    location: Optional[str] = Field("", description="Router location", max_length=128)
    notes: Optional[str] = Field("", description="Notes", max_length=512)
    ssid: Optional[str] = Field("", description="Primary SSID", max_length=128)


class RouterUpdatePayload(BaseModel):
    """Payload to update an existing router"""
    name: Optional[str] = Field(None, description="Friendly router name", max_length=64)
    host: Optional[str] = Field(None, description="Router IP or hostname")
    port: Optional[int] = Field(None, description="Web port", ge=1, le=65535)
    username: Optional[str] = Field(None, description="Web admin username", max_length=64)
    password: Optional[str] = Field(None, description="Web admin password (leave empty to keep current)", max_length=128)
    timeout: Optional[int] = Field(None, description="Request timeout seconds", ge=1, le=300)
    location: Optional[str] = Field(None, description="Router location", max_length=128)
    notes: Optional[str] = Field(None, description="Notes", max_length=512)
    ssid: Optional[str] = Field(None, description="Primary SSID", max_length=128)


class RouterBulkReauthPayload(BaseModel):
    """Payload to bulk re-authenticate all registered routers"""
    concurrency: Optional[int] = Field(10, ge=1, le=50, description="Maksimum router diproses bersamaan")
    timeout_sec: Optional[float] = Field(6.0, ge=1.0, le=20.0, description="Timeout per router dalam detik")
    fetch_ssid: Optional[bool] = Field(True, description="Ambil info SSID sekaligus untuk memperbarui label router")
    fetch_clients: Optional[bool] = Field(True, description="Ambil daftar DHCP client LAN/WiFi (MAC & hostname) untuk analitik")


class RouterBulkReauthItem(BaseModel):
    """Single router re-auth result"""
    router_id: str
    name: str
    ip: str
    port: int
    model: Optional[str] = None
    ssid: Optional[str] = None
    status: str = Field(..., description="'ok', 'offline', 'auth_error', 'timeout', 'error'")
    message: str
    latency_ms: int = 0
    client_count: int = 0
    clients: list[dict] = Field(default_factory=list, description="Daftar perangkat DHCP client yang terhubung")


class RouterBulkReauthResponse(BaseModel):
    """Result of bulk re-authenticating routers"""
    total: int
    success_count: int
    offline_count: int
    failed_count: int
    duration_seconds: float
    total_clients: int = 0
    client_analytics: dict = Field(default_factory=dict, description="Analitik agregat perangkat client DHCP (kategori/merek, mac prefix, hostname)")
    items: list[RouterBulkReauthItem] = Field(default_factory=list)
    message: str


class RouterScanPayload(BaseModel):
    """Payload to scan an IP range/CIDR and auto-register F609, F677V2, and F477V2 routers"""
    cidr: str = Field(..., description="IP subnet in CIDR notation (e.g. 10.200.204.0/24) or range (e.g. 10.200.204.1-254)")
    port: Optional[int] = Field(80, ge=1, le=65535, description="Port to probe")
    credential_mode: Optional[str] = Field("auto", description="Mode kredensial: 'auto' (F609: admin/admin, F677V2: superadmin/suportadmin, F477V2: Administrator/suportadmin) atau 'manual'")
    username: Optional[str] = Field("admin", max_length=64, description="Web admin username (digunakan jika credential_mode='manual')")
    password: Optional[str] = Field("", max_length=128, description="Web admin password (digunakan jika credential_mode='manual')")
    timeout: Optional[int] = Field(10, ge=1, le=120, description="Router request timeout seconds")
    connect_timeout: Optional[float] = Field(1.5, ge=0.5, le=10.0, description="Probe timeout seconds per IP")
    allowed_models: Optional[list[str]] = Field(default_factory=lambda: ["F609", "F677V2", "F477V2"], description="Filter models (default: F609, F677V2, F477V2)")
    name_template: Optional[str] = Field("ZTE {model} ({ip})", max_length=64, description="Template for friendly name")
    location: Optional[str] = Field("", max_length=128, description="Location to assign")
    notes: Optional[str] = Field("", max_length=512, description="Notes to assign")
    auto_add: Optional[bool] = Field(True, description="Whether to automatically register discovered routers")
    concurrency: Optional[int] = Field(60, ge=1, le=120, description="Max concurrent IP scans")


class RouterScanItem(BaseModel):
    """Discovered router item during scan"""
    ip: str
    port: int
    model: str
    name: str
    username: Optional[str] = Field(None, description="Username yang digunakan")
    status: str
    router_id: Optional[str] = None
    message: Optional[str] = None


class RouterScanResponse(BaseModel):
    """Result of router range scan"""
    target: str = Field(..., description="Target subnet or range scanned")
    total_scanned: int = Field(..., description="Total IP addresses probed")
    found_count: int = Field(..., description="Total matched routers found")
    added_count: int = Field(..., description="Total new routers added to registry")
    already_exists_count: int = Field(..., description="Total routers already registered")
    duration_seconds: float = Field(..., description="Scan duration in seconds")
    items: list[RouterScanItem] = Field(default_factory=list, description="List of discovered/added routers")
    message: str = Field(..., description="Scan status message")


class RouterSSIDSearchPayload(BaseModel):
    """Payload to search SSID across all registered routers with per-router reauth"""
    query: str = Field(..., description="Nama atau potongan nama SSID yang dicari (kosongkan atau '*' untuk semua)")
    exact_match: Optional[bool] = Field(False, description="Pencocokan persis (case-insensitive) jika True, atau substring jika False")
    reauth: Optional[bool] = Field(True, description="Paksa login ulang (reauth) sebelum mengambil info WiFi agar sesi tidak kedaluwarsa")
    concurrency: Optional[int] = Field(5, ge=1, le=30, description="Maksimum router diperiksa bersamaan (1 untuk urutan murni 1-per-1)")
    timeout_sec: Optional[float] = Field(6.0, ge=1.0, le=20.0, description="Batas waktu per router dalam detik")


class SSIDMatchRouter(BaseModel):
    """Result of SSID search for a single router"""
    router_id: str
    name: str
    host: str
    port: int
    model: Optional[str] = None
    location: Optional[str] = None
    notes: Optional[str] = None
    matched_ssids: list[dict] = Field(default_factory=list, description="Daftar SSID yang cocok")
    total_ssids: int = 0
    status: str = Field("ok", description="'matched', 'no_match', 'offline', 'auth_error', 'timeout', 'error'")
    error_message: Optional[str] = None
    latency_ms: Optional[int] = None


class RouterSSIDSearchResponse(BaseModel):
    """Response of SSID search across routers"""
    query: str
    total_routers: int
    total_scanned: int
    total_matches: int
    duration_seconds: float
    matches: list[SSIDMatchRouter] = Field(default_factory=list)
    message: str


class RouterTestResponse(BaseModel):
    """Result of router connectivity test"""
    reachable: bool = Field(..., description="Whether router responded and authenticated")
    latency_ms: int = Field(..., description="Round-trip latency in milliseconds")
    model: Optional[str] = Field(None, description="Detected ZTE model")
    message: str = Field(..., description="Status summary message")


class RouterReauthResponse(BaseModel):
    """Result of router re-authentication"""
    success: bool = Field(..., description="Whether re-authentication succeeded")
    latency_ms: int = Field(..., description="Round-trip latency in milliseconds")
    model: Optional[str] = Field(None, description="Detected ZTE model")
    message: str = Field(..., description="Status summary message")


class RouterOverviewResponse(BaseModel):
    """Full overview of a single router"""
    router: RouterResponse = Field(..., description="Router registry details")
    device: Optional[DeviceInfoResponse] = Field(None, description="Device info")
    pon: Optional[PONDataResponse] = Field(None, description="PON optical data")
    wifi: list[WiFiNetworkResponse] = Field(default_factory=list, description="Configured WiFi SSIDs")
    lan: list[LANPortResponse] = Field(default_factory=list, description="LAN ports status")
    wan: list[WANConnectionResponse] = Field(default_factory=list, description="WAN connections")
    voip: list[VoIPLineResponse] = Field(default_factory=list, description="VoIP lines")
    system: Optional[SystemStatsResponse] = Field(None, description="System resources")
    devices: list[ConnectedDeviceResponse] = Field(default_factory=list, description="Connected devices")


class HealthResponse(BaseModel):
    """Health check response"""
    status: str = Field(..., description="Service status")
    router_connected: bool = Field(..., description="Router connectivity status")
    router_host: str = Field(..., description="Router IP/hostname")
    routers_count: int = Field(..., description="Number of registered routers")


class LoginResponse(BaseModel):
    """Login response"""
    success: bool = Field(..., description="Login success status")
    message: str = Field(..., description="Status message")


class ErrorResponse(BaseModel):
    """Error response"""
    detail: str = Field(..., description="Error message")


class DecryptedCredentials(BaseModel):
    """Extracted sensitive router credentials"""
    pppoe: list[dict] = Field(default_factory=list, description="PPPoE username and password pairs")
    wifi: list[dict] = Field(default_factory=list, description="WiFi SSID and password pairs")
    admin: list[dict] = Field(default_factory=list, description="Web management accounts")


class ConfigDecryptResponse(BaseModel):
    """Result of router config.bin decryption"""
    success: bool = Field(..., description="Whether decryption succeeded")
    router_id: Optional[str] = Field(None, description="Router ID if from registry")
    model: Optional[str] = Field(None, description="Router model / signature")
    encryption_key: Optional[str] = Field(None, description="Encryption key used to decode")
    credentials: DecryptedCredentials = Field(default_factory=DecryptedCredentials, description="Extracted credentials")
    xml_preview: Optional[str] = Field(None, description="Decrypted XML snippet")


# ==============================================================================
# Application Lifespan
# ==============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    print(f"ZTE Router API starting...")
    routers = registry.list()
    print(f"Loaded {len(routers)} routers from registry: {[r.name for r in routers]}")
    yield
    print("Shutting down ZTE Router API...")
    for _, (_, client) in _client_pool.items():
        try:
            if client.is_logged_in:
                client.logout()
        except Exception:
            pass


# ==============================================================================
# FastAPI Application
# ==============================================================================

app = FastAPI(
    title="ZTE Router API",
    description="""
## ZTE Router OpenAPI & Multi-Router Management API

RESTful API and Web UI for ZTE GPON routers (including F609, F677V2, F8648P).

### Features
- **Multi-Router Management**: Add, update, delete, and switch routers on the fly.
- **WiFi Extraction**: Retrieve configured SSIDs, passphrases, and security status.
- **PON Optical Metrics**: RX/TX power, temperature, voltage, and ONU state.
- **LAN & WAN Interfaces**: Port speeds, link status, IP addresses, and traffic counters.
- **Web UI**: Fast, responsive, non-modal interface at `/ui`.

### Read-Only Guarantee
Router configurations are strictly read-only. No network, wireless, or routing parameters are modified.
""",
    version="1.1.0",
    lifespan=lifespan,
    responses={
        401: {"model": ErrorResponse, "description": "Otentikasi gagal"},
        404: {"model": ErrorResponse, "description": "Router atau data tidak ditemukan"},
        503: {"model": ErrorResponse, "description": "Router tidak dapat dihubungi"},
    },
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ==============================================================================
# Router Registry Endpoints
# ==============================================================================

@app.get(
    "/routers",
    response_model=RouterListResponse,
    tags=["Routers"],
    summary="List registered routers",
    description="Get all registered ZTE routers without passwords.",
)
async def list_routers():
    """List all registered routers"""
    entries = registry.public_list()
    return RouterListResponse(
        routers=[RouterResponse(**e) for e in entries],
        count=len(entries),
        registry_file=registry.path,
        persistent=registry.persistent,
    )


@app.post(
    "/routers",
    response_model=RouterResponse,
    status_code=201,
    tags=["Routers"],
    summary="Register a router",
    description="Add a new ZTE router to the registry.",
)
async def create_router(payload: RouterCreatePayload):
    """Add a new router"""
    try:
        data = payload.model_dump(exclude_unset=True)
        cred_mode = data.pop("credential_mode", "manual")
        req_model = data.pop("model", None)

        if cred_mode == "auto":
            if req_model and "477" in req_model.upper():
                data["username"] = AUTO_CREDENTIALS["F477V2"]["username"]
                data["password"] = AUTO_CREDENTIALS["F477V2"]["password"]
            elif req_model and "677" in req_model.upper():
                data["username"] = AUTO_CREDENTIALS["F677V2"]["username"]
                data["password"] = AUTO_CREDENTIALS["F677V2"]["password"]
            elif req_model and "609" in req_model.upper():
                data["username"] = AUTO_CREDENTIALS["F609"]["username"]
                data["password"] = AUTO_CREDENTIALS["F609"]["password"]
            else:
                # Auto-detect via fast probe
                probe = await probe_zte_device(
                    ip=data.get("host", ""),
                    port=data.get("port", 80),
                    credential_mode="auto",
                    timeout=min(float(data.get("timeout", 10)), 3.0),
                )
                if probe and probe.get("username") and probe.get("password"):
                    data["username"] = probe["username"]
                    data["password"] = probe["password"]
                    if not data.get("name") or data.get("name") == "ZTE Router":
                        data["name"] = f"ZTE {probe['model']} ({probe['ip']})"
                else:
                    # Fallback to F609 default
                    data["username"] = AUTO_CREDENTIALS["F609"]["username"]
                    data["password"] = AUTO_CREDENTIALS["F609"]["password"]

        entry = registry.create(data)
        return RouterResponse(**entry.to_dict())
    except RouterRegistryError as e:
        raise HTTPException(status_code=400, detail=str(e))


async def _extract_router_wifi_record(entry, timeout_sec: float = 3.5) -> tuple[str, list[dict]]:
    """
    Extract WiFi networks from a single router entry.
    Returns (ip, list of dicts with 'ssid' and 'password').
    """
    ip = entry.host
    try:
        # Fast TCP port check to skip offline routers immediately
        if not await check_tcp_port(ip, entry.port or 80, timeout=0.8):
            return ip, []

        client = get_client_for_router(entry.id)

        def _fetch():
            if not client.is_logged_in:
                client.login()
            return client.get_wifi_networks()

        networks = await asyncio.wait_for(asyncio.to_thread(_fetch), timeout=timeout_sec)
        return ip, networks or []
    except Exception:
        return ip, []


@app.get(
    "/routers/backup-csv",
    tags=["Routers"],
    summary="Backup router WiFi credentials as CSV (IP, SSID, Password)",
    description="Ekspor data router dalam format CSV dengan kolom HANYA: ip, ssid, password.",
)
async def backup_routers_wifi_csv(
    router_id: Optional[str] = Query(None, description="Router ID tertentu (kosongkan untuk backup seluruh router)"),
    include_empty: bool = Query(False, description="Sertakan IP router meski offline atau tidak ada WiFi"),
    concurrency: int = Query(20, ge=1, le=50, description="Maksimum router diproses bersamaan"),
    timeout_sec: float = Query(4.0, ge=1.0, le=15.0, description="Timeout per router dalam detik"),
):
    """
    Backup router WiFi ke CSV:
    Hanya mengekspor kolom: ip, ssid, password
    """
    if router_id:
        target_router = get_router_or_default(router_id)
        if not target_router:
            raise HTTPException(status_code=404, detail="Router tidak ditemukan")
        target_routers = [target_router]
    else:
        target_routers = registry.list()

    if not target_routers:
        raise HTTPException(status_code=404, detail="Tidak ada router terdaftar di sistem")

    # Resolve concurrency & timeout safely if called directly
    limit_concurrency = concurrency if isinstance(concurrency, int) else 20
    limit_timeout = timeout_sec if isinstance(timeout_sec, (int, float)) else 4.0
    sem = asyncio.Semaphore(limit_concurrency)

    async def _worker(r):
        async with sem:
            return await _extract_router_wifi_record(r, timeout_sec=limit_timeout)

    results = await asyncio.gather(*[_worker(r) for r in target_routers])

    output = io.StringIO()
    writer = csv.writer(output)
    # Header: ONLY ip, ssid, password as explicitly requested
    writer.writerow(["ip", "ssid", "password"])

    for ip, networks in results:
        if networks:
            for net in networks:
                ssid = net.get("ssid") or ""
                pwd = net.get("password") or ""
                if ssid:
                    writer.writerow([ip, ssid, pwd])
        elif include_empty:
            writer.writerow([ip, "", ""])

    csv_data = output.getvalue()
    ts = int(time.time())
    if router_id and len(target_routers) == 1:
        clean_ip = target_routers[0].host.replace(".", "_")
        filename = f"router_{clean_ip}_wifi_backup_{ts}.csv"
    else:
        filename = f"routers_wifi_backup_{ts}.csv"

    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "text/csv; charset=utf-8",
        },
    )


@app.post(
    "/routers/scan",
    response_model=RouterScanResponse,
    tags=["Routers"],
    summary="Scan IP range and add F609/F677V2 routers",
    description="Scan an IP range (e.g. 10.200.204.0/24) specifically for ZTE F609 and F677V2 routers and register them.",
)
async def scan_routers(payload: RouterScanPayload):
    """Scan IP range for F609 and F677V2 routers and optionally auto-add them"""
    try:
        parse_ip_targets(payload.cidr)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        result = await scan_and_add_routers(
            registry=registry,
            target_str=payload.cidr,
            port=payload.port or 80,
            username=payload.username or "admin",
            password=payload.password or "",
            credential_mode=payload.credential_mode or "auto",
            timeout=payload.timeout or 10,
            connect_timeout=payload.connect_timeout or 1.5,
            allowed_models=tuple(payload.allowed_models or ["F609", "F677V2", "F477V2"]),
            name_template=payload.name_template or "ZTE {model} ({ip})",
            location=payload.location or "",
            notes=payload.notes or "",
            auto_add=payload.auto_add if payload.auto_add is not None else True,
            concurrency=payload.concurrency or 60,
        )
        return RouterScanResponse(
            target=result["target"],
            total_scanned=result["total_scanned"],
            found_count=result["found_count"],
            added_count=result["added_count"],
            already_exists_count=result["already_exists_count"],
            duration_seconds=result["duration_seconds"],
            items=[RouterScanItem(**item) for item in result.get("items", [])],
            message=result["message"],
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal memindai subnet: {e}")


@app.post(
    "/routers/scan-stream",
    tags=["Routers"],
    summary="Scan IP range with live SSE stream",
    description="Scan an IP range with real-time SSE event streaming for live discovery updates.",
)
async def scan_routers_stream(payload: RouterScanPayload):
    """Stream live events during IP range scan"""
    try:
        parse_ip_targets(payload.cidr)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    import json

    async def event_generator():
        try:
            async for event in scan_range_generator(
                registry=registry,
                target_str=payload.cidr,
                port=payload.port or 80,
                username=payload.username or "admin",
                password=payload.password or "",
                credential_mode=payload.credential_mode or "auto",
                timeout=payload.timeout or 10,
                connect_timeout=payload.connect_timeout or 1.5,
                allowed_models=tuple(payload.allowed_models or ["F609", "F677V2", "F477V2"]),
                name_template=payload.name_template or "ZTE {model} ({ip})",
                location=payload.location or "",
                notes=payload.notes or "",
                auto_add=payload.auto_add if payload.auto_add is not None else True,
                concurrency=payload.concurrency or 60,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            err_event = {"type": "error", "message": str(e)}
            yield f"data: {json.dumps(err_event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _search_single_router_wifi(
    entry,
    query: str,
    exact_match: bool = False,
    force_reauth: bool = True,
    timeout_sec: float = 6.0,
) -> dict:
    """
    Search WiFi networks on a single router with optional fresh reauth.
    Returns dict representing SSIDMatchRouter fields.
    """
    ip = entry.host
    port = entry.port or 80
    t0 = time.time()
    result = {
        "router_id": entry.id,
        "name": entry.name,
        "host": ip,
        "port": port,
        "model": None,
        "location": entry.location,
        "notes": entry.notes,
        "matched_ssids": [],
        "total_ssids": 0,
        "status": "ok",
        "error_message": None,
        "latency_ms": 0,
    }

    try:
        # Fast TCP port check to skip offline routers immediately
        if not await check_tcp_port(ip, port, timeout=1.0):
            result["status"] = "offline"
            result["error_message"] = f"Port {port} tidak merespons (Offline)"
            result["latency_ms"] = int((time.time() - t0) * 1000)
            return result

        client = get_client_for_router(entry.id)
        result["model"] = client.model

        def _work():
            # Force reauth per router if requested or if not logged in
            if force_reauth:
                client.reauth()
            elif not client.is_logged_in:
                client.login()
            return client.get_wifi_networks()

        networks = await asyncio.wait_for(asyncio.to_thread(_work), timeout=timeout_sec)
        result["latency_ms"] = int((time.time() - t0) * 1000)
        result["model"] = client.model

        clean_query = (query or "").strip().lower()
        matched = []
        for net in (networks or []):
            ssid_name = str(net.get("ssid") or "").strip()
            if not ssid_name:
                continue

            if not clean_query or clean_query == "*":
                is_match = True
            elif exact_match:
                is_match = (ssid_name.lower() == clean_query)
            else:
                is_match = (clean_query in ssid_name.lower())

            if is_match:
                matched.append({
                    "ssid": ssid_name,
                    "password": net.get("password") or "",
                    "enabled": bool(net.get("enabled", True)),
                    "security": net.get("security") or "WPA/WPA2-PSK",
                    "band": net.get("band") or "2.4G",
                })

        result["total_ssids"] = len(networks or [])
        result["matched_ssids"] = matched
        result["status"] = "matched" if matched else "no_match"
        return result

    except asyncio.TimeoutError:
        result["status"] = "timeout"
        result["error_message"] = f"Timeout ({timeout_sec}s) saat menghubungi router"
        result["latency_ms"] = int((time.time() - t0) * 1000)
        return result
    except ZTEAuthenticationError as e:
        result["status"] = "auth_error"
        result["error_message"] = f"Gagal otentikasi: {e}"
        result["latency_ms"] = int((time.time() - t0) * 1000)
        return result
    except Exception as e:
        result["status"] = "error"
        result["error_message"] = str(e)
        result["latency_ms"] = int((time.time() - t0) * 1000)
        return result


async def search_ssid_stream_generator(
    query: str,
    exact_match: bool = False,
    reauth: bool = True,
    concurrency: int = 5,
    timeout_sec: float = 6.0,
):
    """
    Generator streaming SSE events while checking routers for matching SSIDs.
    """
    routers = registry.list()
    total = len(routers)
    t0 = time.time()

    yield {
        "type": "start",
        "query": query,
        "total_routers": total,
        "exact_match": exact_match,
        "reauth": reauth,
        "concurrency": concurrency,
    }

    if total == 0:
        yield {
            "type": "done",
            "query": query,
            "total_scanned": 0,
            "total_matches": 0,
            "duration_seconds": 0.0,
            "matches": [],
            "message": "Tidak ada router terdaftar di sistem.",
        }
        return

    sem = asyncio.Semaphore(concurrency)
    matched_results = []
    queue: asyncio.Queue = asyncio.Queue()

    async def _worker(idx: int, entry):
        async with sem:
            await queue.put({
                "type": "router_checking",
                "index": idx + 1,
                "total": total,
                "router_id": entry.id,
                "ip": entry.host,
                "name": entry.name,
            })
            res = await _search_single_router_wifi(
                entry,
                query=query,
                exact_match=exact_match,
                force_reauth=reauth,
                timeout_sec=timeout_sec,
            )
            await queue.put({
                "type": "router_result",
                "index": idx + 1,
                "total": total,
                "result": res,
            })

    async def _producer():
        tasks = [_worker(i, r) for i, r in enumerate(routers)]
        await asyncio.gather(*tasks, return_exceptions=True)
        await queue.put(None)

    producer_task = asyncio.create_task(_producer())
    scanned_count = 0
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            if item.get("type") == "router_result":
                scanned_count += 1
                item["scanned_count"] = scanned_count
                item["percent"] = int((scanned_count / total) * 100) if total else 100
                res = item.get("result", {})
                if res.get("status") == "matched":
                    matched_results.append(res)
            yield item
    finally:
        if not producer_task.done():
            producer_task.cancel()

    duration = round(time.time() - t0, 2)
    yield {
        "type": "done",
        "query": query,
        "total_scanned": total,
        "total_matches": len(matched_results),
        "duration_seconds": duration,
        "matches": matched_results,
        "message": f"Pencarian selesai dalam {duration} detik. Ditemukan {len(matched_results)} router yang memiliki SSID '{query}'.",
    }


@app.post(
    "/routers/search-ssid-stream",
    tags=["Routers"],
    summary="Search SSID across routers with live SSE stream",
    description="Cari di IP router mana suatu SSID berada dengan re-auth per router dan live SSE streaming update.",
)
async def search_ssid_stream(payload: RouterSSIDSearchPayload):
    """Stream live SSE events while searching SSID across all registered routers"""
    import json

    async def event_generator():
        try:
            async for event in search_ssid_stream_generator(
                query=payload.query,
                exact_match=bool(payload.exact_match),
                reauth=bool(payload.reauth),
                concurrency=payload.concurrency or 5,
                timeout_sec=payload.timeout_sec or 6.0,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            err_event = {"type": "error", "message": str(e)}
            yield f"data: {json.dumps(err_event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post(
    "/routers/search-ssid",
    response_model=RouterSSIDSearchResponse,
    tags=["Routers"],
    summary="Search SSID across registered routers",
    description="Cari di IP router mana suatu SSID berada dengan opsi re-auth per router.",
)
async def search_ssid(payload: RouterSSIDSearchPayload):
    """Cari SSID di seluruh router terdaftar (melakukan re-auth per router jika diminta)"""
    routers = registry.list()
    total = len(routers)
    t0 = time.time()

    if total == 0:
        return RouterSSIDSearchResponse(
            query=payload.query,
            total_routers=0,
            total_scanned=0,
            total_matches=0,
            duration_seconds=0.0,
            matches=[],
            message="Tidak ada router terdaftar di sistem.",
        )

    concurrency = payload.concurrency or 5
    sem = asyncio.Semaphore(concurrency)

    async def _worker(r):
        async with sem:
            return await _search_single_router_wifi(
                r,
                query=payload.query,
                exact_match=bool(payload.exact_match),
                force_reauth=bool(payload.reauth),
                timeout_sec=payload.timeout_sec or 6.0,
            )

    results = await asyncio.gather(*[_worker(r) for r in routers], return_exceptions=True)
    matches = []
    for res in results:
        if isinstance(res, dict) and res.get("status") == "matched":
            matches.append(SSIDMatchRouter(**res))

    duration = round(time.time() - t0, 2)
    return RouterSSIDSearchResponse(
        query=payload.query,
        total_routers=total,
        total_scanned=total,
        total_matches=len(matches),
        duration_seconds=duration,
        matches=matches,
        message=f"Pencarian selesai dalam {duration} detik. Ditemukan {len(matches)} router yang memiliki SSID '{payload.query}'.",
    )


@app.get(
    "/routers/search-ssid",
    response_model=RouterSSIDSearchResponse,
    tags=["Routers"],
    summary="Search SSID across routers (GET query)",
    description="Cari SSID di seluruh router via GET query parameter.",
)
async def search_ssid_get(
    query: str = Query(..., description="Nama atau potongan nama SSID yang dicari"),
    exact_match: bool = Query(False, description="Pencocokan persis (case-insensitive)"),
    reauth: bool = Query(True, description="Paksa login ulang (reauth) per router"),
    concurrency: int = Query(5, ge=1, le=30, description="Maksimum router bersamaan"),
    timeout_sec: float = Query(6.0, ge=1.0, le=20.0, description="Timeout per router (detik)"),
):
    """Cari SSID via GET query parameter"""
    payload = RouterSSIDSearchPayload(
        query=query,
        exact_match=exact_match,
        reauth=reauth,
        concurrency=concurrency,
        timeout_sec=timeout_sec,
    )
    return await search_ssid(payload)


@app.get(
    "/routers/{router_id}",
    response_model=RouterResponse,
    tags=["Routers"],
    summary="Get router details",
    description="Get metadata of a registered router.",
)
async def get_router(router_id: str):
    """Get one router by ID"""
    try:
        entry = registry.get(router_id)
        return RouterResponse(**entry.to_dict())
    except RouterRegistryError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.put(
    "/routers/{router_id}",
    response_model=RouterResponse,
    tags=["Routers"],
    summary="Update router details",
    description="Update router settings. Empty password keeps current password.",
)
async def update_router(router_id: str, payload: RouterUpdatePayload):
    """Update an existing router"""
    try:
        data = payload.model_dump(exclude_unset=True)
        if "password" in data and (data["password"] is None or data["password"] == ""):
            data.pop("password", None)
        entry = registry.update(router_id, data)
        return RouterResponse(**entry.to_dict())
    except RouterRegistryError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete(
    "/routers/{router_id}",
    tags=["Routers"],
    summary="Delete router",
    description="Remove a router from the registry.",
)
async def delete_router(router_id: str):
    """Delete a router"""
    try:
        registry.delete(router_id)
        if router_id in _client_pool:
            _, client = _client_pool.pop(router_id)
            try:
                if client.is_logged_in:
                    client.logout()
            except Exception:
                pass
        return {"status": "deleted", "id": router_id}
    except RouterRegistryError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post(
    "/routers/{router_id}/test",
    response_model=RouterTestResponse,
    tags=["Routers"],
    summary="Test router connectivity",
    description="Quickly verify login and connectivity to the router.",
)
async def test_router(router_id: str):
    """Test router connection and auth"""
    entry = get_router_or_default(router_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Router '{router_id}' tidak ditemukan")

    t0 = time.time()
    try:
        client = get_client_for_router(router_id)
        if not client.is_logged_in:
            client.login()
        else:
            try:
                client.get_device_info()
            except Exception:
                client._logged_in = False
                client.login()
        latency = int((time.time() - t0) * 1000)
        return RouterTestResponse(
            reachable=True,
            latency_ms=latency,
            model=client.model,
            message="Koneksi dan otentikasi berhasil",
        )
    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        return RouterTestResponse(
            reachable=False,
            latency_ms=latency,
            model=None,
            message=str(e),
        )


def classify_dhcp_device(hostname: Optional[str], mac: Optional[str]) -> str:
    """Classify device manufacturer/type by hostname and MAC address"""
    hn = (hostname or "").lower().strip()
    if "oppo" in hn:
        return "OPPO"
    if "realme" in hn:
        return "Realme"
    if any(k in hn for k in ("redmi", "xiaomi", "poco", "mi ")):
        return "Xiaomi / Redmi / POCO"
    if any(k in hn for k in ("galaxy", "samsung")):
        return "Samsung"
    if any(k in hn for k in ("vivo", "iqoo")) or hn.startswith(("v2", "i2")):
        return "Vivo / iQOO"
    if any(k in hn for k in ("infinix", "tecno", "itel")):
        return "Infinix / Transsion"
    if any(k in hn for k in ("iphone", "ipad", "apple", "macbook")):
        return "Apple (iOS/macOS)"
    if any(k in hn for k in ("desktop", "laptop", "windows", "pc-")):
        return "PC / Laptop Windows"
    if any(k in hn for k in ("tenda", "tp-link", "totolink", "mercusys", "router", "cpe", "ap-")):
        return "Router / Extender"
    if any(k in hn for k in ("tv", "android", "stb", "box")):
        return "Smart TV / Android Box"
    if any(k in hn for k in ("aliosthings", "tuya", "esp_", "esp32", "esp8266", "iot", "cam", "ipc-")):
        return "IoT / IP Cam / Smart Home"
    if hn:
        return f"Lainnya ({hostname})"
    return "Tanpa Hostname"


async def _reauth_single_router(
    entry,
    fetch_ssid: bool = True,
    fetch_clients: bool = True,
    timeout_sec: float = 6.0,
) -> dict:
    """Re-authenticate a single router and optionally fetch & persist its SSID and DHCP clients"""
    ip = entry.host
    port = entry.port or 80
    t0 = time.time()
    res = {
        "router_id": entry.id,
        "name": entry.name,
        "ip": ip,
        "port": port,
        "model": None,
        "ssid": entry.ssid or "",
        "status": "ok",
        "message": "Otentikasi ulang berhasil",
        "latency_ms": 0,
        "client_count": 0,
        "clients": [],
    }

    try:
        if not await check_tcp_port(ip, port, timeout=1.0):
            res["status"] = "offline"
            res["message"] = f"Port {port} tidak merespons (Offline)"
            res["latency_ms"] = int((time.time() - t0) * 1000)
            return res

        client = get_client_for_router(entry.id)

        def _work():
            client.reauth()
            networks = None
            raw_clients = None
            if fetch_clients:
                try:
                    raw_clients = client.get_connected_devices()
                except Exception:
                    raw_clients = None
            if fetch_ssid:
                try:
                    networks = client.get_wifi_networks()
                except Exception:
                    networks = None
            return networks, raw_clients

        networks, raw_clients = await asyncio.wait_for(asyncio.to_thread(_work), timeout=timeout_sec)
        res["latency_ms"] = int((time.time() - t0) * 1000)
        res["model"] = client.model

        if networks:
            for net in networks:
                s = str(net.get("ssid") or "").strip()
                if s:
                    res["ssid"] = s
                    registry.update_ssid(entry.id, s)
                    break

        if raw_clients:
            cleaned_clients = []
            for d in raw_clients:
                cleaned_clients.append({
                    "ip": d.ip_address,
                    "mac": d.mac_address,
                    "hostname": d.hostname,
                    "interface": d.interface,
                    "brand": classify_dhcp_device(d.hostname, d.mac_address),
                    "last_seen": d.last_seen,
                })
            res["clients"] = cleaned_clients
            res["client_count"] = len(cleaned_clients)

        return res

    except asyncio.TimeoutError:
        res["status"] = "timeout"
        res["message"] = f"Timeout ({timeout_sec}s)"
        res["latency_ms"] = int((time.time() - t0) * 1000)
        return res
    except ZTEAuthenticationError as e:
        res["status"] = "auth_error"
        res["message"] = f"Gagal otentikasi: {e}"
        res["latency_ms"] = int((time.time() - t0) * 1000)
        return res
    except Exception as e:
        res["status"] = "error"
        res["message"] = str(e)
        res["latency_ms"] = int((time.time() - t0) * 1000)
        return res


async def reauth_all_stream_generator(
    concurrency: int = 10,
    timeout_sec: float = 6.0,
    fetch_ssid: bool = True,
    fetch_clients: bool = True,
):
    """Generator streaming SSE events during bulk router re-authentication with DHCP client counting"""
    routers = registry.list()
    total = len(routers)
    t0 = time.time()

    yield {
        "type": "start",
        "total": total,
        "concurrency": concurrency,
    }

    if total == 0:
        yield {
            "type": "done",
            "total": 0,
            "success_count": 0,
            "offline_count": 0,
            "failed_count": 0,
            "total_clients": 0,
            "client_analytics": {},
            "duration_seconds": 0.0,
            "message": "Tidak ada router terdaftar.",
        }
        return

    sem = asyncio.Semaphore(concurrency)
    queue: asyncio.Queue = asyncio.Queue()

    async def _worker(idx: int, entry):
        async with sem:
            await queue.put({
                "type": "checking",
                "index": idx + 1,
                "total": total,
                "router_id": entry.id,
                "ip": entry.host,
                "name": entry.name,
            })
            res = await _reauth_single_router(
                entry,
                fetch_ssid=fetch_ssid,
                fetch_clients=fetch_clients,
                timeout_sec=timeout_sec,
            )
            await queue.put({
                "type": "result",
                "index": idx + 1,
                "total": total,
                "item": res,
            })

    async def _producer():
        tasks = [_worker(i, r) for i, r in enumerate(routers)]
        await asyncio.gather(*tasks, return_exceptions=True)
        await queue.put(None)

    producer_task = asyncio.create_task(_producer())
    success_count = 0
    offline_count = 0
    failed_count = 0
    scanned_count = 0
    total_clients = 0
    brand_counts: dict[str, int] = {}
    top_hostnames: dict[str, int] = {}

    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            if item.get("type") == "result":
                scanned_count += 1
                r = item.get("item", {})
                st = r.get("status")
                if st == "ok":
                    success_count += 1
                    # Aggregate clients
                    c_list = r.get("clients", [])
                    total_clients += len(c_list)
                    for c in c_list:
                        b = c.get("brand") or "Tanpa Hostname"
                        brand_counts[b] = brand_counts.get(b, 0) + 1
                        hn = c.get("hostname")
                        if hn:
                            top_hostnames[hn] = top_hostnames.get(hn, 0) + 1
                elif st == "offline":
                    offline_count += 1
                else:
                    failed_count += 1
                item["scanned_count"] = scanned_count
                item["percent"] = int((scanned_count / total) * 100) if total else 100
                item["success_count"] = success_count
                item["offline_count"] = offline_count
                item["failed_count"] = failed_count
                item["total_clients"] = total_clients
                item["brand_counts"] = brand_counts
            yield item
    finally:
        if not producer_task.done():
            producer_task.cancel()

    duration = round(time.time() - t0, 2)
    # Sort analytics
    sorted_brands = dict(sorted(brand_counts.items(), key=lambda kv: kv[1], reverse=True))
    sorted_hosts = dict(sorted(top_hostnames.items(), key=lambda kv: kv[1], reverse=True)[:30])

    yield {
        "type": "done",
        "total": total,
        "success_count": success_count,
        "offline_count": offline_count,
        "failed_count": failed_count,
        "total_clients": total_clients,
        "client_analytics": {
            "total_clients": total_clients,
            "brands": sorted_brands,
            "top_hostnames": sorted_hosts,
        },
        "duration_seconds": duration,
        "message": (
            f"Re-Auth Massal selesai ({duration}s): "
            f"{success_count} berhasil, {offline_count} offline, {failed_count} gagal, "
            f"{total_clients} perangkat LAN/WiFi terhitung."
        ),
    }


@app.post(
    "/routers/reauth-all-stream",
    tags=["Routers"],
    summary="Bulk re-authenticate all routers with live SSE stream",
    description="Re-authenticate all routers concurrently with real-time SSE progress, DHCP client device analytics, and auto-update SSID labels.",
)
async def reauth_all_routers_stream(payload: Optional[RouterBulkReauthPayload] = None):
    """Stream live SSE events during bulk router re-authentication"""
    import json
    p = payload or RouterBulkReauthPayload()

    async def event_generator():
        try:
            async for event in reauth_all_stream_generator(
                concurrency=p.concurrency or 10,
                timeout_sec=p.timeout_sec or 6.0,
                fetch_ssid=True if p.fetch_ssid is None else p.fetch_ssid,
                fetch_clients=True if p.fetch_clients is None else p.fetch_clients,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as e:
            err_event = {"type": "error", "message": str(e)}
            yield f"data: {json.dumps(err_event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post(
    "/routers/reauth-all",
    response_model=RouterBulkReauthResponse,
    tags=["Routers"],
    summary="Bulk re-authenticate all routers",
    description="Force re-authenticate all registered routers, counting connected DHCP devices and updating SSID labels.",
)
async def reauth_all_routers(payload: Optional[RouterBulkReauthPayload] = None):
    """Bulk re-authenticate all registered routers"""
    p = payload or RouterBulkReauthPayload()
    routers = registry.list()
    total = len(routers)
    t0 = time.time()

    if total == 0:
        return RouterBulkReauthResponse(
            total=0,
            success_count=0,
            offline_count=0,
            failed_count=0,
            duration_seconds=0.0,
            total_clients=0,
            client_analytics={},
            items=[],
            message="Tidak ada router terdaftar.",
        )

    concurrency = p.concurrency or 10
    sem = asyncio.Semaphore(concurrency)

    async def _worker(r):
        async with sem:
            return await _reauth_single_router(
                r,
                fetch_ssid=True if p.fetch_ssid is None else p.fetch_ssid,
                fetch_clients=True if p.fetch_clients is None else p.fetch_clients,
                timeout_sec=p.timeout_sec or 6.0,
            )

    results = await asyncio.gather(*[_worker(r) for r in routers], return_exceptions=True)
    items = []
    success_count = 0
    offline_count = 0
    failed_count = 0
    total_clients = 0
    brand_counts: dict[str, int] = {}
    top_hostnames: dict[str, int] = {}

    for res in results:
        if isinstance(res, dict):
            st = res.get("status")
            if st == "ok":
                success_count += 1
                c_list = res.get("clients", [])
                total_clients += len(c_list)
                for c in c_list:
                    b = c.get("brand") or "Tanpa Hostname"
                    brand_counts[b] = brand_counts.get(b, 0) + 1
                    hn = c.get("hostname")
                    if hn:
                        top_hostnames[hn] = top_hostnames.get(hn, 0) + 1
            elif st == "offline":
                offline_count += 1
            else:
                failed_count += 1
            items.append(RouterBulkReauthItem(**res))

    duration = round(time.time() - t0, 2)
    sorted_brands = dict(sorted(brand_counts.items(), key=lambda kv: kv[1], reverse=True))
    sorted_hosts = dict(sorted(top_hostnames.items(), key=lambda kv: kv[1], reverse=True)[:30])

    return RouterBulkReauthResponse(
        total=total,
        success_count=success_count,
        offline_count=offline_count,
        failed_count=failed_count,
        duration_seconds=duration,
        total_clients=total_clients,
        client_analytics={
            "total_clients": total_clients,
            "brands": sorted_brands,
            "top_hostnames": sorted_hosts,
        },
        items=items,
        message=(
            f"Re-Auth Massal selesai ({duration}s): "
            f"{success_count} berhasil, {offline_count} offline, {failed_count} gagal, "
            f"{total_clients} perangkat LAN/WiFi terhitung."
        ),
    )


@app.get(
    "/routers/analytics/clients",
    tags=["Routers"],
    summary="Get aggregated DHCP client analytics across routers",
    description="Fetch connected client device breakdown, vendor/brand distribution, and top hostnames.",
)
async def get_routers_client_analytics(
    limit_routers: Optional[int] = Query(None, ge=1, le=500, description="Batasi jumlah router yang dicek"),
    timeout_sec: float = Query(4.0, ge=1.0, le=15.0, description="Batas timeout per router"),
):
    """Aggregate connected DHCP clients from routers"""
    routers = registry.list()
    if limit_routers:
        routers = routers[:limit_routers]

    t0 = time.time()
    sem = asyncio.Semaphore(15)

    async def _fetch(r):
        async with sem:
            try:
                if not await check_tcp_port(r.host, r.port or 80, timeout=1.0):
                    return r, []
                client = get_client_for_router(r.id)
                def _get():
                    if not client.is_logged_in:
                        client.login()
                    return client.get_connected_devices()
                devs = await asyncio.wait_for(asyncio.to_thread(_get), timeout=timeout_sec)
                return r, devs
            except Exception:
                return r, []

    results = await asyncio.gather(*[_fetch(r) for r in routers], return_exceptions=True)
    total_clients = 0
    brand_counts: dict[str, int] = {}
    top_hostnames: dict[str, int] = {}
    router_client_breakdown = []

    for item in results:
        if isinstance(item, tuple):
            r, devs = item
            if devs:
                dev_count = len(devs)
                total_clients += dev_count
                router_client_breakdown.append({
                    "router_id": r.id,
                    "name": r.name,
                    "ip": r.host,
                    "ssid": r.ssid or "",
                    "client_count": dev_count,
                })
                for d in devs:
                    b = classify_dhcp_device(d.hostname, d.mac_address)
                    brand_counts[b] = brand_counts.get(b, 0) + 1
                    if d.hostname:
                        top_hostnames[d.hostname] = top_hostnames.get(d.hostname, 0) + 1

    sorted_brands = dict(sorted(brand_counts.items(), key=lambda kv: kv[1], reverse=True))
    sorted_hosts = dict(sorted(top_hostnames.items(), key=lambda kv: kv[1], reverse=True)[:30])
    router_client_breakdown.sort(key=lambda x: x["client_count"], reverse=True)

    return {
        "status": "ok",
        "total_routers_scanned": len(routers),
        "routers_with_clients": len(router_client_breakdown),
        "total_clients": total_clients,
        "brands": sorted_brands,
        "top_hostnames": sorted_hosts,
        "top_routers": router_client_breakdown[:20],
        "duration_seconds": round(time.time() - t0, 2),
    }


@app.post(
    "/routers/{router_id}/reauth",
    response_model=RouterReauthResponse,
    tags=["Routers"],
    summary="Force re-authenticate router",
    description="Force clear session cookies, re-login, and verify router session.",
)
async def reauth_router(router_id: str):
    """Force re-authentication with router"""
    entry = get_router_or_default(router_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Router '{router_id}' tidak ditemukan")

    t0 = time.time()
    try:
        client = get_client_for_router(router_id)
        client.reauth()
        latency = int((time.time() - t0) * 1000)
        # Auto-update primary SSID in registry if available
        try:
            networks = client.get_wifi_networks()
            if networks:
                for net in networks:
                    s = str(net.get("ssid") or "").strip()
                    if s:
                        registry.update_ssid(router_id, s)
                        break
        except Exception:
            pass
        return RouterReauthResponse(
            success=True,
            latency_ms=latency,
            model=client.model,
            message="Otentikasi ulang berhasil",
        )
    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        return RouterReauthResponse(
            success=False,
            latency_ms=latency,
            model=None,
            message=f"Otentikasi ulang gagal: {e}",
        )


@app.post(
    "/reauth",
    response_model=RouterReauthResponse,
    tags=["Authentication"],
    summary="Force re-authenticate router",
    description="Force clear session cookies and re-login.",
)
async def reauth(
    router_id: Optional[str] = Query(None, description="Router ID dari /routers")
):
    """Force re-authentication"""
    client = get_client_for_router(router_id)
    t0 = time.time()
    try:
        client.reauth()
        latency = int((time.time() - t0) * 1000)
        return RouterReauthResponse(
            success=True,
            latency_ms=latency,
            model=client.model,
            message="Otentikasi ulang berhasil",
        )
    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        return RouterReauthResponse(
            success=False,
            latency_ms=latency,
            model=None,
            message=f"Otentikasi ulang gagal: {e}",
        )


@app.get(
    "/routers/{router_id}/overview",
    response_model=RouterOverviewResponse,
    tags=["Routers"],
    summary="Get router overview",
    description="Fetch comprehensive router data including WiFi, PON, and interfaces.",
)
async def get_router_overview(router_id: str):
    """Fetch complete overview for a single router"""
    entry = get_router_or_default(router_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Router '{router_id}' tidak ditemukan")

    client = get_client_for_router(router_id)
    ensure_client_logged_in(client)

    # Safely fetch all modules
    def safe_call(func, default):
        try:
            return with_session_retry(func)(client)
        except Exception as e:
            print(f"[overview] Error fetching {func}: {e}")
            return default

    dev = safe_call(lambda c: c.get_device_info(), None)
    pon = safe_call(lambda c: c.get_pon_data(), None)
    devices = safe_call(lambda c: c.get_connected_devices(), [])
    wifi = safe_call(lambda c: c.get_wifi_networks(), [])
    if wifi:
        for w in wifi:
            if isinstance(w, dict) and w.get("ssid"):
                registry.update_ssid(router_id, w["ssid"])
                entry.ssid = w["ssid"]
                break
    lan = safe_call(lambda c: c.get_lan_status(), [])
    wan = safe_call(lambda c: c.get_wan_status(), [])
    voip = safe_call(lambda c: c.get_voip_status(), [])
    system = safe_call(lambda c: c.get_system_stats(), {})

    return RouterOverviewResponse(
        router=RouterResponse(**entry.to_dict()),
        device=DeviceInfoResponse(**dev.to_dict()) if dev else None,
        pon=PONDataResponse(**pon.to_dict()) if pon else None,
        wifi=[WiFiNetworkResponse(**w) for w in wifi],
        lan=[LANPortResponse(**p.to_dict()) for p in lan],
        wan=[WANConnectionResponse(**w.to_dict()) for w in wan],
        voip=[VoIPLineResponse(**v.to_dict()) for v in voip],
        system=SystemStatsResponse(**system) if system else None,
        devices=[ConnectedDeviceResponse(**d.to_dict()) for d in devices],
    )


# ==============================================================================
# Status & Metric Endpoints
# ==============================================================================

@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["System"],
    summary="Health check",
    description="Check API server health and router connectivity.",
)
async def health_check(
    router_id: Optional[str] = Query(None, description="Router ID dari /routers")
):
    """Health check endpoint"""
    client = get_client_for_router(router_id)
    return HealthResponse(
        status="healthy",
        router_connected=client.is_logged_in,
        router_host=client.host,
        routers_count=len(registry.list()),
    )


@app.post(
    "/login",
    response_model=LoginResponse,
    tags=["Authentication"],
    summary="Login to router",
    description="Authenticate with the specified ZTE router.",
)
async def login(
    router_id: Optional[str] = Query(None, description="Router ID dari /routers")
):
    """Login to router"""
    client = get_client_for_router(router_id)
    try:
        if client.is_logged_in:
            return LoginResponse(success=True, message="Sudah login")
        client.login()
        return LoginResponse(success=True, message="Login berhasil")
    except ZTEAuthenticationError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except ZTEClientError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.post(
    "/logout",
    response_model=LoginResponse,
    tags=["Authentication"],
    summary="Logout from router",
    description="End the session with the specified ZTE router.",
)
async def logout(
    router_id: Optional[str] = Query(None, description="Router ID dari /routers")
):
    """Logout from router"""
    client = get_client_for_router(router_id)
    if client.logout():
        return LoginResponse(success=True, message="Logout berhasil")
    return LoginResponse(success=False, message="Logout gagal atau belum login")


@app.get(
    "/wifi",
    response_model=list[WiFiNetworkResponse],
    tags=["WiFi"],
    summary="Get WiFi networks and passwords",
    description="Ambil konfigurasi WiFi (SSID, password/passphrase, security, dan band) yang terpasang.",
)
async def get_wifi(
    router_id: Optional[str] = Query(None, description="Router ID dari /routers")
):
    """Get WiFi SSIDs and passwords"""
    client = get_client_for_router(router_id)
    ensure_client_logged_in(client)
    try:
        get_data = with_session_retry(lambda c: c.get_wifi_networks())
        data = get_data(client)
        return [WiFiNetworkResponse(**w) for w in data]
    except ZTESessionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except ZTEClientError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/pon",
    response_model=PONDataResponse,
    tags=["PON"],
    summary="Get PON optical data",
    description="Fetch PON optical module parameters including RX/TX power, temperature, voltage, and current.",
)
async def get_pon_data(
    router_id: Optional[str] = Query(None, description="Router ID dari /routers")
):
    """Get PON optical module data"""
    client = get_client_for_router(router_id)
    ensure_client_logged_in(client)
    try:
        get_data = with_session_retry(lambda c: c.get_pon_data())
        data = get_data(client)
        return PONDataResponse(**data.to_dict())
    except ZTESessionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except ZTEClientError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/device",
    response_model=DeviceInfoResponse,
    tags=["Device"],
    summary="Get device information",
    description="Fetch device model, firmware version, and hardware details.",
)
async def get_device_info(
    router_id: Optional[str] = Query(None, description="Router ID dari /routers")
):
    """Get device information"""
    client = get_client_for_router(router_id)
    ensure_client_logged_in(client)
    try:
        get_data = with_session_retry(lambda c: c.get_device_info())
        data = get_data(client)
        return DeviceInfoResponse(**data.to_dict())
    except ZTESessionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except ZTEClientError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/lan",
    response_model=list[LANPortResponse],
    tags=["LAN"],
    summary="Get LAN port status",
    description="Fetch status of all LAN ports including link state, speed, and traffic counters.",
)
async def get_lan_status(
    router_id: Optional[str] = Query(None, description="Router ID dari /routers")
):
    """Get LAN port status"""
    client = get_client_for_router(router_id)
    ensure_client_logged_in(client)
    try:
        get_data = with_session_retry(lambda c: c.get_lan_status())
        ports = get_data(client)
        return [LANPortResponse(**p.to_dict()) for p in ports]
    except ZTESessionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except ZTEClientError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/voip",
    response_model=list[VoIPLineResponse],
    tags=["VoIP"],
    summary="Get VoIP line status",
    description="Fetch VoIP line registration status for all configured lines.",
)
async def get_voip_status(
    router_id: Optional[str] = Query(None, description="Router ID dari /routers")
):
    """Get VoIP line status"""
    client = get_client_for_router(router_id)
    ensure_client_logged_in(client)
    try:
        get_data = with_session_retry(lambda c: c.get_voip_status())
        lines = get_data(client)
        return [VoIPLineResponse(**l.to_dict()) for l in lines]
    except ZTESessionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except ZTEClientError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/wan",
    response_model=list[WANConnectionResponse],
    tags=["WAN"],
    summary="Get WAN connection status",
    description="Fetch WAN connection information including IP addresses and connection status.",
)
async def get_wan_status(
    router_id: Optional[str] = Query(None, description="Router ID dari /routers")
):
    """Get WAN connection status"""
    client = get_client_for_router(router_id)
    ensure_client_logged_in(client)
    try:
        get_data = with_session_retry(lambda c: c.get_wan_status())
        connections = get_data(client)
        return [WANConnectionResponse(**c.to_dict()) for c in connections]
    except ZTESessionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except ZTEClientError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/system/stats",
    response_model=SystemStatsResponse,
    tags=["System"],
    summary="Get system statistics",
    description="Fetch CPU and memory usage, uptime, and storage statistics.",
)
async def get_system_stats(
    router_id: Optional[str] = Query(None, description="Router ID dari /routers")
):
    """Get system statistics"""
    client = get_client_for_router(router_id)
    ensure_client_logged_in(client)
    try:
        get_data = with_session_retry(lambda c: c.get_system_stats())
        stats = get_data(client)
        return SystemStatsResponse(**stats)
    except ZTESessionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except ZTEClientError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/devices",
    response_model=list[ConnectedDeviceResponse],
    tags=["Network"],
    summary="Get connected devices",
    description="Fetch list of devices connected to the router.",
)
async def get_connected_devices(
    router_id: Optional[str] = Query(None, description="Router ID dari /routers")
):
    """Get connected devices"""
    client = get_client_for_router(router_id)
    ensure_client_logged_in(client)
    try:
        get_data = with_session_retry(lambda c: c.get_connected_devices())
        devices = get_data(client)
        return [ConnectedDeviceResponse(**d.to_dict()) for d in devices]
    except ZTESessionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except ZTEClientError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/status",
    tags=["Status"],
    summary="Get full router status",
    description="Fetch all available status information in a single request.",
)
async def get_full_status(
    router_id: Optional[str] = Query(None, description="Router ID dari /routers")
):
    """Get full router status"""
    client = get_client_for_router(router_id)
    ensure_client_logged_in(client)
    try:
        get_device = with_session_retry(lambda c: c.get_device_info())
        get_pon = with_session_retry(lambda c: c.get_pon_data())
        get_lan = with_session_retry(lambda c: c.get_lan_status())
        get_voip = with_session_retry(lambda c: c.get_voip_status())
        get_wan = with_session_retry(lambda c: c.get_wan_status())
        get_system = with_session_retry(lambda c: c.get_system_stats())
        get_devices = with_session_retry(lambda c: c.get_connected_devices())
        get_wifi_net = with_session_retry(lambda c: c.get_wifi_networks())

        return {
            "device": get_device(client).to_dict(),
            "pon": get_pon(client).to_dict(),
            "wifi": get_wifi_net(client),
            "lan": [p.to_dict() for p in get_lan(client)],
            "voip": [l.to_dict() for l in get_voip(client)],
            "wan": [c.to_dict() for c in get_wan(client)],
            "system": get_system(client),
            "devices": [d.to_dict() for d in get_devices(client)],
        }
    except ZTESessionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except ZTEClientError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==============================================================================
# Configuration Backup & Decryption Endpoints
# ==============================================================================

@app.get(
    "/routers/{router_id}/backup-config",
    tags=["Configuration"],
    summary="Download router config.bin",
    description="Download the binary configuration backup file (config.bin) directly from the router.",
)
async def download_router_config(router_id: str):
    """Download config.bin backup from router"""
    entry = get_router_or_default(router_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Router '{router_id}' tidak ditemukan")

    client = get_client_for_router(router_id)
    ensure_client_logged_in(client)
    try:
        data = with_session_retry(lambda c: c.backup_config())(client)
        filename = f"config_{entry.name or client.model or 'router'}_{router_id}.bin"
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except ZTEClientError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal mengunduh config.bin: {e}")


@app.post(
    "/routers/{router_id}/decrypt-config",
    response_model=ConfigDecryptResponse,
    tags=["Configuration"],
    summary="Decrypt router config.bin and extract PPPoE credentials",
    description="Download and decrypt the router's config.bin file, recovering plaintext PPPoE credentials, WiFi passwords, and admin accounts.",
)
async def decrypt_router_config(
    router_id: str,
    key: Optional[str] = Query(None, description="Custom AES key (jika diketahui)"),
):
    """Decrypt router config.bin to extract plaintext credentials"""
    entry = get_router_or_default(router_id)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Router '{router_id}' tidak ditemukan")

    client = get_client_for_router(router_id)
    ensure_client_logged_in(client)

    # Gather device info for key derivation
    dev_sn, dev_mac = None, None
    try:
        dev_info = client.get_device_info()
        dev_sn = dev_info.serial_number
    except Exception:
        pass

    try:
        data = with_session_retry(lambda c: c.backup_config())(client)
    except ZTEClientError as e:
        raise HTTPException(status_code=502, detail=f"Tidak dapat mengunduh config.bin dari router: {e}")

    try:
        xml_bytes, used_key = decrypt_config_bin(data, key=key, serial=dev_sn, mac=dev_mac)
        extracted = extract_credentials_from_xml(xml_bytes)
        preview = xml_bytes.decode("latin1", "replace")[:1000]
        return ConfigDecryptResponse(
            success=True,
            router_id=router_id,
            model=client.model,
            encryption_key=used_key,
            credentials=DecryptedCredentials(**extracted),
            xml_preview=preview,
        )
    except ZTEConfigError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal mendekripsi config.bin: {e}")


@app.post(
    "/decrypt-config",
    response_model=ConfigDecryptResponse,
    tags=["Configuration"],
    summary="Decrypt uploaded config.bin",
    description="Upload a config.bin file to decrypt and extract PPPoE credentials, WiFi passwords, and admin accounts.",
)
async def decrypt_uploaded_config(
    file: UploadFile = File(..., description="ZTE config.bin file"),
    key: Optional[str] = Query(None, description="Custom AES key (opsional)"),
    serial: Optional[str] = Query(None, description="Router Serial Number (opsional)"),
    mac: Optional[str] = Query(None, description="Router MAC Address (opsional)"),
):
    """Upload and decrypt any ZTE config.bin file"""
    data = await file.read()
    if len(data) < 16:
        raise HTTPException(status_code=400, detail="Berkas terlalu kecil atau kosong")

    try:
        xml_bytes, used_key = decrypt_config_bin(data, key=key, serial=serial, mac=mac)
        extracted = extract_credentials_from_xml(xml_bytes)
        preview = xml_bytes.decode("latin1", "replace")[:1000]
        return ConfigDecryptResponse(
            success=True,
            router_id=None,
            model=file.filename,
            encryption_key=used_key,
            credentials=DecryptedCredentials(**extracted),
            xml_preview=preview,
        )
    except ZTEConfigError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gagal mendekripsi file: {e}")


# ==============================================================================
# Telnet & Factory Mode Endpoints
# ==============================================================================

class TelnetStatusResponse(BaseModel):
    open: bool
    port: int
    latency_ms: Optional[float] = None
    message: str


class TelnetEnableRequest(BaseModel):
    mode: int = Field(2, description="Factory mode (1=ops, 2=dev, 3=production, 4=user)")


class TelnetEnableResponse(BaseModel):
    success: bool
    telnet_user: Optional[str] = None
    telnet_pass: Optional[str] = None
    message: str


class TelnetExtractRequest(BaseModel):
    username: Optional[str] = Field(None, description="Telnet username (default auto-try root/admin)")
    password: Optional[str] = Field(None, description="Telnet password (default auto-try Zte521/admin)")
    port: int = Field(23, description="Telnet port")
    timeout: float = Field(8.0, description="Socket timeout in seconds")


class TelnetCommandRequest(BaseModel):
    command: str = Field(..., description="Shell or sendcmd command (e.g. 'sendcmd 1 DB p WANCP')")
    username: Optional[str] = Field(None, description="Telnet username")
    password: Optional[str] = Field(None, description="Telnet password")
    port: int = Field(23, description="Telnet port")
    timeout: float = Field(8.0, description="Socket timeout in seconds")


class TelnetCommandResponse(BaseModel):
    success: bool
    command: str
    output: str


class TelnetExtractResponse(BaseModel):
    success: bool
    router_id: Optional[str] = None
    credentials: DecryptedCredentials
    raw_outputs: dict[str, str] = Field(default_factory=dict)


@app.get(
    "/routers/{router_id}/telnet/status",
    response_model=TelnetStatusResponse,
    tags=["Telnet"],
    summary="Check Telnet port status",
    description="Check whether port 23 (Telnet) is open and responsive on the specified router.",
)
async def get_router_telnet_status(router_id: str, port: int = Query(23, description="Port to probe")):
    entry = get_router_or_default(router_id)
    return test_telnet_port(entry.host, port=port)


@app.post(
    "/routers/{router_id}/telnet/enable-factory",
    response_model=TelnetEnableResponse,
    tags=["Telnet"],
    summary="Enable Telnet via Factory Mode",
    description="Trigger ZTE webFac / webFacEntry factory mode to turn on Telnet service and generate credentials.",
)
async def enable_router_telnet(router_id: str, req: TelnetEnableRequest = TelnetEnableRequest()):
    entry = get_router_or_default(router_id)
    res = enable_telnet_factory_mode(
        host=entry.host,
        port=entry.port,
        username=entry.username,
        password=entry.password,
        mode=req.mode,
    )
    return TelnetEnableResponse(**res)


@app.post(
    "/routers/{router_id}/telnet/extract",
    response_model=TelnetExtractResponse,
    tags=["Telnet"],
    summary="Extract credentials via Telnet (sendcmd)",
    description="Connect via Telnet, authenticate, and execute sendcmd to extract PPPoE, WiFi, and Admin credentials.",
)
async def extract_router_telnet(router_id: str, req: TelnetExtractRequest = TelnetExtractRequest()):
    entry = get_router_or_default(router_id)

    # Determine login candidates
    candidates = []
    if req.username and req.password is not None:
        candidates.append((req.username, req.password))
    else:
        candidates.extend([
            ("root", "Zte521"),
            ("root", "Pon521"),
            ("admin", "admin"),
            ("root", "root"),
            (entry.username, entry.password),
        ])

    last_error = None
    for user, pwd in candidates:
        client = ZTETelnetClient(host=entry.host, port=req.port, timeout=req.timeout)
        try:
            client.connect()
            client.login(user, pwd)
            creds = client.extract_credentials()
            client.close()
            return TelnetExtractResponse(
                success=True,
                router_id=router_id,
                credentials=DecryptedCredentials(
                    pppoe=creds["pppoe"],
                    admin=creds["admin"],
                    wifi=creds["wifi"],
                    tr069=[],
                ),
                raw_outputs=creds["raw_outputs"],
            )
        except Exception as e:
            last_error = e
            client.close()

    raise HTTPException(status_code=422, detail=f"Gagal mengekstrak via Telnet: {last_error}")


@app.post(
    "/routers/{router_id}/telnet/command",
    response_model=TelnetCommandResponse,
    tags=["Telnet"],
    summary="Run command via Telnet",
    description="Execute an arbitrary shell or sendcmd command on the router via Telnet.",
)
async def run_router_telnet_command(router_id: str, req: TelnetCommandRequest):
    entry = get_router_or_default(router_id)
    user = req.username or "root"
    pwd = req.password or "Zte521"

    client = ZTETelnetClient(host=entry.host, port=req.port, timeout=req.timeout)
    try:
        client.connect()
        client.login(user, pwd)
        output = client.run_command(req.command, timeout=req.timeout)
        client.close()
        return TelnetCommandResponse(success=True, command=req.command, output=output)
    except Exception as e:
        client.close()
        raise HTTPException(status_code=422, detail=f"Gagal menjalankan perintah telnet: {e}")


# ==============================================================================
# OpenAPI Schema Sanitization
# ==============================================================================

def custom_openapi():
    """Sanitize OpenAPI schema: remove anyOf in query parameters for OpenAPI compliance"""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            if not isinstance(operation, dict):
                continue
            for param in operation.get("parameters", []):
                param_schema = param.get("schema", {})
                if "anyOf" in param_schema:
                    types = [t for t in param_schema["anyOf"] if t.get("type") != "null"]
                    if len(types) == 1:
                        cleaned = dict(types[0])
                        cleaned["nullable"] = True
                        if "default" in param_schema:
                            cleaned["default"] = param_schema["default"]
                        if "description" in param_schema:
                            cleaned["description"] = param_schema["description"]
                        param["schema"] = cleaned
    app.openapi_schema = schema
    return app.openapi_schema

app.openapi = custom_openapi


# ==============================================================================
# UI Static Mounting & Root Redirect
# ==============================================================================

@app.get("/", include_in_schema=False)
async def root_redirect():
    """Redirect root to /ui"""
    return RedirectResponse(url="/ui")

if os.path.exists(STATIC_DIR):
    app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
