"""
ZTE Router Network Scanner.

Scans an IP range or CIDR subnet (e.g. 10.200.204.0/24) to discover ZTE GPON routers
specifically filtered to models F609 and F677V2, and optionally registers them into the registry.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import random
import re
import time
from typing import AsyncGenerator, Optional

import httpx

from .router_registry import RouterRegistry, RouterRegistryError

# Models supported by this scanner
SUPPORTED_MODELS = ("F609", "F677V2", "F477V2")

# Regex to match F609, F677, and F477 in HTML/text
_RE_F609 = re.compile(r"\bF609\b|ZXHN\s*F609|F609V", re.IGNORECASE)
_RE_F677 = re.compile(r"\bF677V2\b|\bF677\b|ZXHN\s*F677", re.IGNORECASE)
_RE_F477 = re.compile(r"\bF477V2\b|\bF477\b|ZXHN\s*F477", re.IGNORECASE)

# ZTE GCH indicator regex
_RE_ZTE_GCH = re.compile(r"Frm_Logintoken|UserRandomNum|getpage\.gch|login_t\.gch|ZXHN", re.IGNORECASE)


def parse_ip_targets(target_str: str, max_hosts: int = 512) -> list[str]:
    """
    Parse an IP target string into a list of individual IPv4 address strings.

    Supports:
    - CIDR notation: "10.200.204.0/24", "192.168.1.0/28"
    - IP Range: "10.200.204.1-10.200.204.254" or "10.200.204.1-254"
    - Single IP: "10.200.204.10"
    """
    target = (target_str or "").strip()
    if not target:
        raise ValueError("Rentang IP atau CIDR tidak boleh kosong")

    # 1. CIDR notation
    if "/" in target:
        try:
            network = ipaddress.ip_network(target, strict=False)
        except ValueError as e:
            raise ValueError(f"Format CIDR tidak valid '{target}': {e}")

        # If it's a /32 or single host
        if network.num_addresses == 1:
            return [str(network.network_address)]

        hosts = [str(ip) for ip in network.hosts()]
        if not hosts:
            hosts = [str(ip) for ip in network]
        if len(hosts) > max_hosts:
            raise ValueError(
                f"Jumlah IP ({len(hosts)}) melebihi batas maksimum {max_hosts} host. "
                f"Gunakan subnet yang lebih kecil (misalnya /24 atau lebih spesifik)."
            )
        return hosts

    # 2. IP Range (e.g. 10.200.204.1-10.200.204.50 or 10.200.204.1-50)
    if "-" in target:
        parts = [p.strip() for p in target.split("-", 1)]
        start_str = parts[0]
        end_str = parts[1]
        try:
            start_addr = ipaddress.IPv4Address(start_str)
        except ValueError:
            raise ValueError(f"IP awal tidak valid: '{start_str}'")

        if "." not in end_str:
            # Short form: 10.200.204.1-50
            base = start_str.rsplit(".", 1)[0]
            end_str = f"{base}.{end_str}"

        try:
            end_addr = ipaddress.IPv4Address(end_str)
        except ValueError:
            raise ValueError(f"IP akhir tidak valid: '{end_str}'")

        start_int = int(start_addr)
        end_int = int(end_addr)
        if start_int > end_int:
            start_int, end_int = end_int, start_int

        count = end_int - start_int + 1
        if count > max_hosts:
            raise ValueError(
                f"Rentang IP ({count} host) melebihi batas maksimum {max_hosts} host."
            )

        return [str(ipaddress.IPv4Address(i)) for i in range(start_int, end_int + 1)]

    # 3. Single IP
    try:
        ipaddress.IPv4Address(target)
        return [target]
    except ValueError:
        raise ValueError(f"Format IP tidak valid: '{target}'")


def identify_model_from_text(text: str) -> Optional[str]:
    """Check text (HTML, title, script) for F609, F677, or F477 markers."""
    if not text:
        return None
    # Check F477 and F677 first
    if _RE_F477.search(text):
        return "F477V2"
    if _RE_F677.search(text):
        return "F677V2"
    if _RE_F609.search(text):
        return "F609"
    return None


async def check_tcp_port(ip: str, port: int, timeout: float = 1.5) -> bool:
    """Fast TCP port open check."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port),
            timeout=timeout,
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


# Default automatic credentials per model
AUTO_CREDENTIALS = {
    "F609": {"username": "admin", "password": "admin"},
    "F677V2": {"username": "superadmin", "password": "suportadmin"},
    "F477V2": {"username": "Administrator", "password": "suportadmin"},
}


async def probe_zte_device(
    ip: str,
    port: int = 80,
    username: str = "admin",
    password: str = "",
    credential_mode: str = "auto",
    timeout: float = 2.5,
    allowed_models: tuple[str, ...] = ("F609", "F677V2", "F477V2"),
) -> Optional[dict]:
    """
    Probe an IP to verify if it is a ZTE router and whether it matches F609 or F677V2.

    If credential_mode == "auto":
      - F609 will use user: admin / pass: admin
      - F677V2 will use user: superadmin / pass: suportadmin
    If credential_mode == "manual":
      - Uses user-supplied username and password.

    Returns dict with info if matched, or None if unreachable / not a matched model.
    """
    # 1. Fast TCP handshake
    is_open = await check_tcp_port(ip, port, timeout=min(timeout, 1.5))
    if not is_open:
        return None

    base_url = f"http://{ip}:{port}"
    detected_model: Optional[str] = None
    is_zte = False
    page_text = ""

    # 2. HTTP GET to root and/or login page
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(timeout, 1.5)),
            verify=False,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        ) as client:
            try:
                resp = await client.get(f"{base_url}/")
                try:
                    page_text = resp.content.decode("utf-8", "replace")
                except Exception:
                    page_text = resp.text
            except Exception:
                page_text = ""

            # Check if root is ZTE / has model
            detected_model = identify_model_from_text(page_text)
            if _RE_ZTE_GCH.search(page_text) or "zte" in page_text.lower():
                is_zte = True

            # If not detected, try secondary GCH login URL
            if not detected_model:
                try:
                    resp_gch = await client.get(f"{base_url}/getpage.gch?pid=1002&nextpage=login_t.gch")
                    try:
                        gch_text = resp_gch.content.decode("gb2312", "replace")
                    except Exception:
                        gch_text = resp_gch.text
                    gch_model = identify_model_from_text(gch_text)
                    if gch_model:
                        detected_model = gch_model
                        page_text += " " + gch_text
                    if _RE_ZTE_GCH.search(gch_text) or "zte" in gch_text.lower():
                        is_zte = True
                except Exception:
                    pass

            # If model still not determined but looks like ZTE GCH,
            # try login probes based on credential_mode
            if not detected_model and is_zte:
                # Extract token
                m_token = re.search(r'Frm_Logintoken"\)\.value\s*=\s*"(\d+)"', page_text)
                if not m_token:
                    m_token = re.search(r'Frm_Logintoken.*?value\s*=\s*[\'"](\d+)[\'"]', page_text, re.I | re.S)
                token = m_token.group(1) if m_token else "8"
                rnd = random.randint(10000000, 99999999)

                if credential_mode == "auto":
                    # 1. Try F609 auto-credentials (admin / admin via MD5)
                    if any("F609" in m.upper() for m in allowed_models):
                        try:
                            f609_user = AUTO_CREDENTIALS["F609"]["username"]
                            f609_pass = AUTO_CREDENTIALS["F609"]["password"]
                            pw_md5 = hashlib.md5((f609_pass + str(rnd)).encode()).hexdigest()
                            resp_md5 = await client.post(
                                f"{base_url}/",
                                data={
                                    "frashnum": "",
                                    "action": "login",
                                    "Frm_Logintoken": token,
                                    "UserRandomNum": rnd,
                                    "Username": f609_user,
                                    "Password": pw_md5,
                                },
                                headers={"Referer": f"{base_url}/"},
                            )
                            cookies = [k for k in resp_md5.cookies.keys()]
                            body_md5 = resp_md5.text
                            if "SID" in cookies and "User information is error" not in body_md5:
                                detected_model = "F609"
                        except Exception:
                            pass

                    # 2. Try F677V2 auto-credentials (superadmin / suportadmin via SHA256)
                    if not detected_model and any("F677" in m.upper() for m in allowed_models):
                        try:
                            f677_user = AUTO_CREDENTIALS["F677V2"]["username"]
                            f677_pass = AUTO_CREDENTIALS["F677V2"]["password"]
                            pw_sha = hashlib.sha256((f677_pass + str(rnd)).encode()).hexdigest()
                            resp_sha = await client.post(
                                f"{base_url}/",
                                data={
                                    "frashnum": "",
                                    "action": "login",
                                    "Frm_Logintoken": token,
                                    "UserRandomNum": rnd,
                                    "Username": f677_user,
                                    "Password": pw_sha,
                                    "Right": "0",
                                },
                                headers={"Referer": f"{base_url}/"},
                            )
                            cookies = [k for k in resp_sha.cookies.keys()]
                            body_sha = resp_sha.text
                            if "SID" in cookies and "User information is error" not in body_sha:
                                detected_model = "F677V2"
                        except Exception:
                            pass

                    # 3. Try F477V2 auto-credentials (Administrator / suportadmin via SHA256 CU format)
                    if not detected_model and any("F477" in m.upper() for m in allowed_models):
                        try:
                            f477_pass = AUTO_CREDENTIALS["F477V2"]["password"]
                            pw_sha = hashlib.sha256((f477_pass + str(rnd)).encode()).hexdigest()
                            m_chk = re.search(r'Frm_Loginchecktoken["\']?\)\.value\s*=\s*["\']?(\d+)["\']?', page_text)
                            chk_token = m_chk.group(1) if m_chk else "0"
                            resp_f477 = await client.post(
                                f"{base_url}/",
                                data={
                                    "Frm_Logintoken": token,
                                    "Frm_Loginchecktoken": chk_token,
                                    "_cu_url": "1",
                                    "Right": "1",
                                    "Username": "",
                                    "UserRandomNum": str(rnd),
                                    "Password": pw_sha,
                                    "action": "login",
                                },
                                headers={"Referer": f"{base_url}/"},
                            )
                            cookies = [k for k in resp_f477.cookies.keys()]
                            body_f477 = resp_f477.text
                            if "SID" in cookies and "User information is error" not in body_f477:
                                detected_model = "F477V2"
                        except Exception:
                            pass

                elif password:
                    # Manual credentials: test MD5 then SHA256
                    try:
                        pw_md5 = hashlib.md5((password + str(rnd)).encode()).hexdigest()
                        resp_md5 = await client.post(
                            f"{base_url}/",
                            data={
                                "frashnum": "",
                                "action": "login",
                                "Frm_Logintoken": token,
                                "UserRandomNum": rnd,
                                "Username": username,
                                "Password": pw_md5,
                            },
                            headers={"Referer": f"{base_url}/"},
                        )
                        cookies = [k for k in resp_md5.cookies.keys()]
                        body_md5 = resp_md5.text
                        if "SID" in cookies and "User information is error" not in body_md5:
                            detected_model = "F609"
                    except Exception:
                        pass

                    if not detected_model:
                        try:
                            pw_sha = hashlib.sha256((password + str(rnd)).encode()).hexdigest()
                            resp_sha = await client.post(
                                f"{base_url}/",
                                data={
                                    "frashnum": "",
                                    "action": "login",
                                    "Frm_Logintoken": token,
                                    "UserRandomNum": rnd,
                                    "Username": username,
                                    "Password": pw_sha,
                                    "Right": "0",
                                },
                                headers={"Referer": f"{base_url}/"},
                            )
                            cookies = [k for k in resp_sha.cookies.keys()]
                            body_sha = resp_sha.text
                            if "SID" in cookies and "User information is error" not in body_sha:
                                detected_model = "F677V2"
                        except Exception:
                            pass

                    if not detected_model:
                        try:
                            pw_sha = hashlib.sha256((password + str(rnd)).encode()).hexdigest()
                            m_chk = re.search(r'Frm_Loginchecktoken["\']?\)\.value\s*=\s*["\']?(\d+)["\']?', page_text)
                            chk_token = m_chk.group(1) if m_chk else "0"
                            resp_f477 = await client.post(
                                f"{base_url}/",
                                data={
                                    "Frm_Logintoken": token,
                                    "Frm_Loginchecktoken": chk_token,
                                    "_cu_url": "1",
                                    "Right": "1",
                                    "Username": "",
                                    "UserRandomNum": str(rnd),
                                    "Password": pw_sha,
                                    "action": "login",
                                },
                                headers={"Referer": f"{base_url}/"},
                            )
                            cookies = [k for k in resp_f477.cookies.keys()]
                            body_f477 = resp_f477.text
                            if "SID" in cookies and "User information is error" not in body_f477:
                                detected_model = "F477V2"
                        except Exception:
                            pass

            # Confirm exact model from status_dev_info_t.gch if we got authenticated
            if detected_model:
                try:
                    r_dev = await client.post(
                        f"{base_url}/getpage.gch?pid=1002&nextpage=status_dev_info_t.gch",
                        timeout=min(timeout, 2.0),
                    )
                    m_model = re.search(r"id=[\x22\x27]Frm_ModelName[\x22\x27][^>]*>(.*?)</td>", r_dev.text, re.S)
                    if m_model:
                        import html as _html
                        dev_model_str = _html.unescape(re.sub(r"<[^>]+>", "", m_model.group(1)).strip()).upper()
                        if "477" in dev_model_str:
                            detected_model = "F477V2"
                        elif "677" in dev_model_str:
                            detected_model = "F677V2"
                        elif "609" in dev_model_str:
                            detected_model = "F609"
                except Exception:
                    pass

    except Exception:
        return None

    # Strict model check: ONLY devices matching allowed_models (F609, F677V2, F477V2)
    if not detected_model:
        return None

    norm_detected = detected_model.upper()
    is_allowed = False
    for allowed in allowed_models:
        if allowed.upper() in norm_detected:
            is_allowed = True
            break

    if not is_allowed:
        return None

    # Determine resolved credentials
    if credential_mode == "auto":
        if "477" in norm_detected:
            resolved_user = AUTO_CREDENTIALS["F477V2"]["username"]
            resolved_pass = AUTO_CREDENTIALS["F477V2"]["password"]
        elif "677" in norm_detected:
            resolved_user = AUTO_CREDENTIALS["F677V2"]["username"]
            resolved_pass = AUTO_CREDENTIALS["F677V2"]["password"]
        else:
            resolved_user = AUTO_CREDENTIALS["F609"]["username"]
            resolved_pass = AUTO_CREDENTIALS["F609"]["password"]
    else:
        resolved_user = username or "admin"
        resolved_pass = password or ""

    return {
        "ip": ip,
        "port": port,
        "model": detected_model,
        "username": resolved_user,
        "password": resolved_pass,
        "is_zte": True,
    }


async def scan_range_generator(
    registry: RouterRegistry,
    target_str: str,
    port: int = 80,
    username: str = "admin",
    password: str = "",
    credential_mode: str = "auto",
    timeout: int = 10,
    connect_timeout: float = 1.5,
    allowed_models: tuple[str, ...] = ("F609", "F677V2"),
    name_template: str = "ZTE {model} ({ip})",
    location: str = "",
    notes: str = "",
    auto_add: bool = True,
    concurrency: int = 60,
) -> AsyncGenerator[dict, None]:
    """
    Async generator that scans the IP target and yields progress and discovery events.

    Yields event dicts:
    - {"type": "start", "total": N, "target": target_str}
    - {"type": "progress", "scanned": X, "total": N, "percent": P}
    - {"type": "found", "item": {...}}
    - {"type": "done", "summary": {...}}
    """
    targets = parse_ip_targets(target_str)
    total = len(targets)

    yield {
        "type": "start",
        "target": target_str,
        "total": total,
    }

    t0 = time.time()
    scanned_count = 0
    found_items: list[dict] = []
    sem = asyncio.Semaphore(concurrency)

    # Pre-fetch existing hosts to detect duplicates
    existing_hosts = {e.host: e for e in registry.list()}

    async def worker(ip: str) -> Optional[dict]:
        async with sem:
            return await probe_zte_device(
                ip=ip,
                port=port,
                username=username,
                password=password,
                credential_mode=credential_mode,
                timeout=connect_timeout,
                allowed_models=allowed_models,
            )

    # Run tasks with progress tracking
    tasks = [asyncio.create_task(worker(ip)) for ip in targets]

    for f in asyncio.as_completed(tasks):
        result = await f
        scanned_count += 1

        if result:
            ip = result["ip"]
            model = result["model"]
            router_name = name_template.replace("{model}", model).replace("{ip}", ip)
            assigned_user = result.get("username") or (
                AUTO_CREDENTIALS.get(model, {}).get("username", "admin")
                if credential_mode == "auto"
                else (username or "admin")
            )
            assigned_pass = (
                result.get("password")
                if "password" in result
                else (
                    AUTO_CREDENTIALS.get(model, {}).get("password", "")
                    if credential_mode == "auto"
                    else (password or "")
                )
            )

            item: dict = {
                "ip": ip,
                "port": port,
                "model": model,
                "name": router_name,
                "username": assigned_user,
                "status": "found",
                "router_id": None,
                "message": f"Ditemukan ZTE {model}",
            }

            if auto_add:
                # Check if router with this host is already in registry
                if ip in existing_hosts:
                    existing = existing_hosts[ip]
                    item["status"] = "already_exists"
                    item["router_id"] = existing.id
                    item["message"] = f"Sudah terdaftar sebagai '{existing.name}'"
                else:
                    try:
                        entry = registry.create(
                            {
                                "name": router_name,
                                "host": ip,
                                "port": port,
                                "username": assigned_user,
                                "password": assigned_pass,
                                "timeout": timeout,
                                "location": location or "",
                                "notes": notes or f"Ditemukan otomatis via scan subnet {target_str}",
                            }
                        )
                        existing_hosts[ip] = entry
                        item["status"] = "added"
                        item["router_id"] = entry.id
                        item["message"] = f"Berhasil didaftarkan sebagai '{router_name}'"
                    except RouterRegistryError as e:
                        item["status"] = "error"
                        item["message"] = f"Gagal menambahkan: {e}"

            found_items.append(item)
            yield {
                "type": "found",
                "item": item,
                "scanned": scanned_count,
                "total": total,
                "percent": int((scanned_count / total) * 100),
            }

        # Yield periodic progress every 15 items or on completion
        if scanned_count % 15 == 0 or scanned_count == total:
            yield {
                "type": "progress",
                "scanned": scanned_count,
                "total": total,
                "percent": int((scanned_count / total) * 100),
                "found_count": len(found_items),
            }

    duration = round(time.time() - t0, 2)
    added_count = sum(1 for x in found_items if x["status"] == "added")
    already_exists_count = sum(1 for x in found_items if x["status"] == "already_exists")

    yield {
        "type": "done",
        "target": target_str,
        "total_scanned": total,
        "found_count": len(found_items),
        "added_count": added_count,
        "already_exists_count": already_exists_count,
        "duration_seconds": duration,
        "items": found_items,
        "message": (
            f"Pemindaian {total} IP selesai dalam {duration} detik. "
            f"Ditemukan: {len(found_items)} router ({added_count} ditambahkan, {already_exists_count} sudah ada)."
        ),
    }


async def scan_and_add_routers(
    registry: RouterRegistry,
    target_str: str,
    port: int = 80,
    username: str = "admin",
    password: str = "",
    credential_mode: str = "auto",
    timeout: int = 10,
    connect_timeout: float = 1.5,
    allowed_models: tuple[str, ...] = ("F609", "F677V2"),
    name_template: str = "ZTE {model} ({ip})",
    location: str = "",
    notes: str = "",
    auto_add: bool = True,
    concurrency: int = 60,
) -> dict:
    """
    Run full scan and return complete results dict.
    """
    last_event = None
    async for event in scan_range_generator(
        registry=registry,
        target_str=target_str,
        port=port,
        username=username,
        password=password,
        credential_mode=credential_mode,
        timeout=timeout,
        connect_timeout=connect_timeout,
        allowed_models=allowed_models,
        name_template=name_template,
        location=location,
        notes=notes,
        auto_add=auto_add,
        concurrency=concurrency,
    ):
        if event["type"] == "done":
            last_event = event

    if last_event:
        return last_event
    return {
        "type": "done",
        "target": target_str,
        "total_scanned": 0,
        "found_count": 0,
        "added_count": 0,
        "already_exists_count": 0,
        "duration_seconds": 0.0,
        "items": [],
        "message": "Pemindaian selesai tanpa hasil.",
    }
