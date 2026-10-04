"""
ZTE Router OpenAPI Server

RESTful API and Web UI for ZTE GPON routers (F609, F677V2, F8648P, etc.).
Provides programmatic access to router status, PON optical metrics, WiFi credentials,
LAN/WAN interfaces, and multi-router management.
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .router_registry import RouterRegistry, RouterRegistryError
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
    username: Optional[str] = Field("admin", description="Web admin username", max_length=64)
    password: Optional[str] = Field("", description="Web admin password", max_length=128)
    timeout: Optional[int] = Field(10, description="Request timeout seconds", ge=1, le=300)
    location: Optional[str] = Field("", description="Router location", max_length=128)
    notes: Optional[str] = Field("", description="Notes", max_length=512)


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
        entry = registry.create(payload.model_dump(exclude_unset=True))
        return RouterResponse(**entry.to_dict())
    except RouterRegistryError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
    wifi = safe_call(lambda c: c.get_wifi_networks(), [])
    lan = safe_call(lambda c: c.get_lan_status(), [])
    wan = safe_call(lambda c: c.get_wan_status(), [])
    voip = safe_call(lambda c: c.get_voip_status(), [])
    system = safe_call(lambda c: c.get_system_stats(), {})
    devices = safe_call(lambda c: c.get_connected_devices(), [])

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
