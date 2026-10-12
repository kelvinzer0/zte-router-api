"""
ZTE F8648P Router API Client

Lightweight Python client for ZTE GPON routers.
Uses SHA256 token-based authentication (not RSA as the web UI suggests).
"""

import hashlib
import json
import random
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Any, Optional
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener


@dataclass
class PONData:
    """PON optical module data"""
    onu_state: Optional[str] = None
    rx_power_dbm: Optional[float] = None
    tx_power_dbm: Optional[float] = None
    voltage_mv: Optional[int] = None
    current_ma: Optional[float] = None
    temperature_c: Optional[float] = None
    video_rx_power_dbm: Optional[float] = None
    rf_tx_power_dbm: Optional[float] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class DeviceInfo:
    """Device information"""
    model: Optional[str] = None
    serial_number: Optional[str] = None
    hardware_version: Optional[str] = None
    software_version: Optional[str] = None
    boot_version: Optional[str] = None
    manufacturer: Optional[str] = None
    oui: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class LANPort:
    """LAN port status"""
    name: str
    link: str
    speed: Optional[int] = None
    duplex: Optional[str] = None
    bytes_rx: Optional[int] = None
    bytes_tx: Optional[int] = None
    packets_rx: Optional[int] = None
    packets_tx: Optional[int] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class WANConnection:
    """WAN connection info"""
    name: Optional[str] = None
    connection_type: Optional[str] = None
    ip_address: Optional[str] = None
    gateway: Optional[str] = None
    dns_servers: Optional[list] = None
    status: Optional[str] = None
    tx_bytes: Optional[int] = None
    rx_bytes: Optional[int] = None
    vlan_id: Optional[int] = None
    mac_address: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None

    def to_dict(self) -> dict:
        result = {k: v for k, v in self.__dict__.items() if v is not None}
        return result


@dataclass
class ConnectedDevice:
    """Connected device info"""
    ip_address: str
    mac_address: str
    interface: Optional[str] = None
    hostname: Optional[str] = None
    last_seen: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class VoIPLine:
    """VoIP line status"""
    line_id: str
    number: Optional[str] = None
    status: Optional[str] = None
    registered: bool = False

    def to_dict(self) -> dict:
        return self.__dict__


class ZTEClientError(Exception):
    """ZTE client error"""
    pass


class ZTEAuthenticationError(ZTEClientError):
    """Authentication failed"""
    pass


class ZTESessionError(ZTEClientError):
    """Session expired or invalid"""
    pass


class ZTEClient:
    """
    ZTE F8648P Router API Client

    Provides access to router data via the internal web API.
    Handles SHA256 token-based authentication automatically.
    """

    # ONU state mapping
    ONU_STATES = {
        1: "Initial State (O1)",
        2: "Standby State (O2)",
        3: "Serial Number State (O3)",
        4: "Ranging State (O4)",
        5: "Operation State (O5)",
    }

    def __init__(self, host: str, username: str = "admin", password: str = "admin", timeout: int = 10):
        """
        Initialize ZTE client.

        Args:
            host: Router IP address or hostname
            username: Login username (default: admin)
            password: Login password
            timeout: Request timeout in seconds
        """
        clean_host = host.replace("http://", "").replace("https://", "").strip("/")
        self.host = clean_host
        self.username = username
        self.password = password
        self.timeout = timeout
        self.base_url = f"http://{clean_host}"
        self.session_token: Optional[str] = None
        self._logged_in = False
        self.is_gch_device = False
        self.model: Optional[str] = None

        # Setup cookie-enabled opener
        self.cookie_jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookie_jar))

    @property
    def is_logged_in(self) -> bool:
        """Check if currently logged in"""
        return self._logged_in

    def _request(self, path: str, method: str = "GET", data: dict = None, encoding: str = 'utf-8') -> Optional[str]:
        """Make HTTP request with cookies"""
        url = f"{self.base_url}{path}"

        if data and method == "POST":
            encoded_data = urlencode(data).encode('utf-8')
            req = Request(url, data=encoded_data, method=method)
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        else:
            req = Request(url, method=method)

        req.add_header("User-Agent", "ZTE-API-Client/1.0")
        req.add_header("Referer", self.base_url)
        req.add_header("Accept", "application/xml, text/xml, */*")

        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                body = response.read()
                try:
                    return body.decode(encoding, 'replace')
                except Exception:
                    return body.decode('utf-8', 'replace')
        except Exception as e:
            raise ZTEClientError(f"Request failed: {e}")

    def _get_timestamp(self) -> int:
        """Get cache-busting timestamp"""
        return int(time.time() * 1000)

    def _load_view(self, view_tag: str, retry: bool = True) -> bool:
        """
        Load a menuView (required before fetching menuData).

        The ZTE router requires loading the view template before
        data can be fetched for that section.
        """
        ts = self._get_timestamp()
        try:
            response = self._request(f"/?_type=menuView&_tag={view_tag}&Menu3Location=0&_={ts}")
            if response is None or "SessionTimeout" in response:
                # Session expired, try to re-login and retry once
                if retry:
                    self._logged_in = False
                    self.login()
                    return self._load_view(view_tag, retry=False)
                return False
            return True
        except ZTEClientError:
            return False

    def _parse_xml_params(self, xml_data: str, patterns: dict) -> dict:
        """Parse XML data using ParaName/ParaValue pattern"""
        result = {}
        for key, pattern in patterns.items():
            match = re.search(pattern, xml_data)
            if match:
                value = match.group(1).strip()
                # Try to convert to number
                try:
                    if '.' in value:
                        result[key] = float(value)
                    else:
                        result[key] = int(value)
                except ValueError:
                    result[key] = value
        return result

    def login(self) -> bool:
        """
        Login to ZTE router.

        Supports multiple authentication methods found on different ZTE firmware:
        - Legacy: JSON-based SHA256 (F8648P-style)
        - Form POST: MD5 (F609) or SHA256 (F677V2) with dynamic Frm_Logintoken
        """
        # Try form-based login first for F609/F677V2, fallback to legacy JSON
        success = False
        try:
            success = self._login_form()
        except Exception as form_err:
            try:
                success = self._login_legacy()
            except Exception:
                # Re-raise the form auth error if it's an auth error
                if isinstance(form_err, ZTEAuthenticationError):
                    raise form_err
                raise
        if success:
            try:
                self.get_device_info()
            except Exception:
                pass
        return success

    def _login_form(self) -> bool:
        """Form-based authentication (F609 MD5, F677V2 SHA256)."""
        # GET login page
        page_html = self._request_raw("/")
        if page_html is None:
            raise ZTEAuthenticationError("Failed to fetch login page")

        # Detect encoding
        enc = "utf-8"
        try:
            page_html_lower = page_html.lower() if isinstance(page_html, bytes) else page_html.lower().encode('latin-1', 'replace')
            if b"charset=gb2312" in page_html_lower or b"charset=gbk" in page_html_lower:
                enc = "gb2312"
        except Exception:
            pass
        try:
            page = page_html.decode(enc, "replace") if isinstance(page_html, bytes) else str(page_html)
        except Exception:
            page = str(page_html) if not isinstance(page_html, bytes) else page_html.decode("utf-8", "replace")

        # Detect model from page HTML if present
        if "F477" in page:
            self.model = "F477V2"
        elif "F609" in page:
            self.model = "F609"
        elif "F677" in page:
            self.model = "F677V2"

        # Extract token and checktoken
        token_match = re.search(r'Frm_Logintoken["\']?\)\.value\s*=\s*["\']?(\d+)["\']?', page)
        token = token_match.group(1) if token_match else None
        if not token:
            # Try other patterns
            token_match = re.search(r'Frm_Logintoken.*?value\s*=\s*[\'"](\d+)[\'"]', page, re.I | re.S)
            token = token_match.group(1) if token_match else ("8" if "md5" in page.lower() or True else "4")

        check_match = re.search(r'Frm_Loginchecktoken["\']?\)\.value\s*=\s*["\']?(\d+)["\']?', page)
        check_token = check_match.group(1) if check_match else None

        rnd = random.randint(10000000, 99999999)

        # 1. Try F477V2 / China Unicom style auth if detected or checktoken present
        if check_token or "_cu_url" in page or self.model == "F477V2":
            pw_sha = hashlib.sha256((self.password + str(rnd)).encode()).hexdigest()
            is_admin = self.username.lower() in ("administrator", "admin", "superadmin", "")
            data_cu = {
                "Frm_Logintoken": token,
                "Frm_Loginchecktoken": check_token or "0",
                "_cu_url": "1" if is_admin else "0",
                "Right": "1" if is_admin else "2",
                "Username": "" if is_admin else self.username,
                "UserRandomNum": str(rnd),
                "Password": pw_sha,
                "action": "login",
            }
            try:
                resp = self._post_raw("/", data_cu, referer=f"{self.base_url}/")
                if resp is not None:
                    sid = None
                    for c in self.cookie_jar:
                        if c.name == "SID":
                            sid = c.value
                            break
                    body_text = resp.decode(enc, "replace")
                    has_error = "User information is error" in body_text or "username or password" in body_text.lower()
                    if sid and not has_error:
                        self._logged_in = True
                        self.is_gch_device = True
                        self.session_token = sid
                        self.model = self.model or "F477V2"
                        return True
            except ZTEClientError:
                pass

        # Try MD5 first (common for older models like F609)
        pw_md5 = hashlib.md5((self.password + str(rnd)).encode()).hexdigest()
        data_md5 = {
            "frashnum": "",
            "action": "login",
            "Frm_Logintoken": token,
            "UserRandomNum": rnd,
            "Username": self.username,
            "Password": pw_md5,
        }

        try:
            resp = self._post_raw("/", data_md5, referer=f"{self.base_url}/")
            if resp is not None:
                body = resp
                try:
                    body_text = body.decode(enc, "replace")
                except Exception:
                    body_text = body.decode("utf-8", "replace")
                sid = None
                for c in self.cookie_jar:
                    if c.name == "SID":
                        sid = c.value
                        break
                has_error = "User information is error" in body_text or "username or password" in body_text.lower()
                if sid and not has_error:
                    self._logged_in = True
                    self.is_gch_device = True
                    self.session_token = sid
                    if not self.model:
                        self.model = "F609"
                    return True
        except ZTEClientError:
            pass

        # Try SHA256 (F677V2)
        pw_sha = hashlib.sha256((self.password + str(rnd)).encode()).hexdigest()
        data_sha = {
            "frashnum": "",
            "action": "login",
            "Frm_Logintoken": token,
            "UserRandomNum": rnd,
            "Username": self.username,
            "Password": pw_sha,
            "Right": "0",
        }
        resp = self._post_raw("/", data_sha, referer=f"{self.base_url}/")
        if resp is not None:
            body = resp
            try:
                body_text = body.decode(enc, "replace")
            except Exception:
                body_text = body.decode("utf-8", "replace")
            sid = None
            for c in self.cookie_jar:
                if c.name == "SID":
                    sid = c.value
                    break
            has_error = "User information is error" in body_text or "username or password" in body_text.lower()
            if sid and not has_error:
                self._logged_in = True
                self.is_gch_device = True
                self.session_token = sid
                if not self.model:
                    self.model = "F677V2"
                return True

        # Fallback: Try F477V2 format if not yet tried
        if not (check_token or "_cu_url" in page or self.model == "F477V2"):
            data_cu_fallback = {
                "Frm_Logintoken": token,
                "Frm_Loginchecktoken": "0",
                "_cu_url": "1",
                "Right": "1",
                "Username": "",
                "UserRandomNum": str(rnd),
                "Password": pw_sha,
                "action": "login",
            }
            try:
                resp = self._post_raw("/", data_cu_fallback, referer=f"{self.base_url}/")
                if resp is not None:
                    sid = None
                    for c in self.cookie_jar:
                        if c.name == "SID":
                            sid = c.value
                            break
                    body_text = resp.decode(enc, "replace")
                    has_error = "User information is error" in body_text or "username or password" in body_text.lower()
                    if sid and not has_error:
                        self._logged_in = True
                        self.is_gch_device = True
                        self.session_token = sid
                        self.model = "F477V2"
                        return True
            except ZTEClientError:
                pass

        raise ZTEAuthenticationError("Login failed: invalid credentials")

    def _login_legacy(self) -> bool:
        """Legacy JSON-based SHA256 auth (F8648P)."""
        try:
            response = self._request("/?_type=loginData&_tag=login_entry")
        except ZTEClientError as e:
            raise ZTEAuthenticationError(f"Failed to connect: {e}")

        if not response:
            raise ZTEAuthenticationError("Failed to get initial session")

        try:
            data = json.loads(response)
            self.session_token = data.get("sess_token", "")

            if data.get("lockingTime", 0) > 0:
                raise ZTEAuthenticationError(
                    f"Login locked. Wait {data['lockingTime']} seconds."
                )

        except json.JSONDecodeError:
            raise ZTEAuthenticationError("Failed to parse session response")

        try:
            token_response = self._request("/?_type=loginData&_tag=login_token")
        except ZTEClientError as e:
            raise ZTEAuthenticationError(f"Failed to get login token: {e}")

        if not token_response:
            raise ZTEAuthenticationError("Failed to get login token")

        try:
            root = ET.fromstring(token_response)
            login_token = root.text.strip() if root.text else ""
        except ET.ParseError:
            match = re.search(r'>([^<]+)<', token_response)
            login_token = match.group(1) if match else ""

        if not login_token:
            raise ZTEAuthenticationError("Failed to extract login token")

        password_hash = hashlib.sha256(
            (self.password + login_token).encode()
        ).hexdigest()

        login_data = {
            "action": "login",
            "Username": self.username,
            "Password": password_hash,
            "_sessionTOKEN": self.session_token,
        }

        try:
            login_response = self._request(
                "/?_type=loginData&_tag=login_entry", "POST", login_data
            )
        except ZTEClientError as e:
            raise ZTEAuthenticationError(f"Login request failed: {e}")

        if not login_response:
            raise ZTEAuthenticationError("Login request failed")

        try:
            result = json.loads(login_response)
            self.session_token = result.get("sess_token", self.session_token)

            if result.get("login_need_refresh"):
                self._logged_in = True
                return True
            else:
                error_msg = result.get("loginErrMsg", "Unknown error")
                raise ZTEAuthenticationError(f"Login failed: {error_msg}")

        except json.JSONDecodeError:
            raise ZTEAuthenticationError("Failed to parse login response")

    def reauth(self) -> bool:
        """Force re-authentication by clearing cookies and logging in afresh"""
        self.cookie_jar.clear()
        self._logged_in = False
        self.session_token = None
        return self.login()

    def _request_raw(self, path: str, retries: int = 1) -> Optional[bytes]:
        url = f"{self.base_url}{path}"
        req = Request(url, method="GET")
        req.add_header("User-Agent", "Mozilla/5.0")
        req.add_header("Referer", self.base_url)
        req.add_header("Connection", "close")
        for attempt in range(retries + 1):
            try:
                with self.opener.open(req, timeout=self.timeout) as response:
                    data = response.read()
                    # If redirected to login page while expecting data, session expired
                    if path != "/" and self._logged_in and data and (
                        b"Frm_Logintoken" in data
                        or b"SessionTimeout" in data
                        or b"login_entry" in data
                    ):
                        self._logged_in = False
                        raise ZTESessionError("Session expired, router redirected to login page")
                    return data
            except ZTESessionError:
                raise
            except Exception as e:
                err_str = str(e).lower()
                if attempt < retries and ("remote end closed" in err_str or "connection reset" in err_str or "broken pipe" in err_str):
                    time.sleep(0.3)
                    continue
                raise ZTEClientError(f"Request failed: {e}")

    def _post_raw(self, path: str, data: dict, referer: Optional[str] = None, retries: int = 1) -> Optional[bytes]:
        url = f"{self.base_url}{path}"
        encoded_data = urlencode(data).encode('utf-8')
        req = Request(url, data=encoded_data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("User-Agent", "Mozilla/5.0")
        req.add_header("Connection", "close")
        if referer:
            req.add_header("Referer", referer)
        req.add_header("Origin", self.base_url)
        for attempt in range(retries + 1):
            try:
                with self.opener.open(req, timeout=self.timeout) as response:
                    return response.read()
            except Exception as e:
                err_str = str(e).lower()
                if attempt < retries and ("remote end closed" in err_str or "connection reset" in err_str or "broken pipe" in err_str):
                    time.sleep(0.3)
                    continue
                raise ZTEClientError(f"Request failed: {e}")

    def _get_pppoe_creds_f677v2(self, index: int, name: str) -> tuple[Optional[str], Optional[str]]:
        """
        Fetch PPPoE username and password for a WAN connection on F677V2 / GCH routers.
        """
        try:
            post_data = {
                "IF_ACTION": "wanctype",
                "IF_INDEX": str(index),
                "IF_NAME": name,
                "IF_MULTIDISPLAY": "0",
                "_sessionTOKEN": self.session_token or "",
            }
            resp = self._post_raw("/getpage.gch?pid=1002&nextpage=net_ethwan_conf_t.gch", post_data)
            if not resp:
                return None, None
            txt = resp.decode("latin1", "replace")
            import html as _html

            def unesc(s: str) -> str:
                s = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)
                return _html.unescape(s).strip()

            u_match = re.search(
                rf"""Transfer_meaning\s*\(\s*['"]UserName{index}['"]\s*,\s*['"]([^'"]*)['"]""",
                txt,
            )
            p_match = re.search(
                rf"""Transfer_meaning\s*\(\s*['"]Password{index}['"]\s*,\s*['"]([^'"]*)['"]""",
                txt,
            )
            user = unesc(u_match.group(1)) if u_match else None
            pwd = unesc(p_match.group(1)) if p_match else None
            return user, pwd
        except Exception:
            return None, None

    def logout(self) -> bool:
        """Logout from the router"""
        if not self._logged_in:
            return True

        try:
            self._request(
                "/?_type=loginData&_tag=logout_entry",
                "POST",
                {"IF_LogOff": 1, "_sessionTOKEN": self.session_token}
            )
            self._logged_in = False
            return True
        except ZTEClientError:
            return False

    def get_pon_data(self) -> PONData:
        """
        Fetch PON optical module data.
        """
        try:
            if self.is_gch_device:
                raise ZTEClientError("GCH device")
            if not self._load_view("ponopticalinfo"):
                raise ZTESessionError("Failed to load PON view")
            ts = self._get_timestamp()
            response = self._request(f"/?_type=menuData&_tag=optical_info_lua.lua&_={ts}")
            if not response:
                raise ZTEClientError("Failed to fetch PON data")
            if "SessionTimeout" in response:
                self._logged_in = False
                raise ZTESessionError("Session expired")
            patterns = {
                "rx_power": r"<ParaName>RxPower</ParaName><ParaValue>([^<]+)</ParaValue>",
                "tx_power": r"<ParaName>TxPower</ParaName><ParaValue>([^<]+)</ParaValue>",
                "voltage": r"<ParaName>Volt</ParaName><ParaValue>([^<]+)</ParaValue>",
                "current": r"<ParaName>Current</ParaName><ParaValue>([^<]+)</ParaValue>",
                "temperature": r"<ParaName>Temp</ParaName><ParaValue>([^<]+)</ParaValue>",
                "reg_status": r"<ParaName>RegStatus</ParaName><ParaValue>([^<]+)</ParaValue>",
                "video_rx": r"<ParaName>VideoRxPower</ParaName><ParaValue>([^<]+)</ParaValue>",
                "rf_tx": r"<ParaName>RFTxPower</ParaName><ParaValue>([^<]+)</ParaValue>",
            }
            data = self._parse_xml_params(response, patterns)
            reg_status = data.get("reg_status")
            onu_state = None
            if isinstance(reg_status, int):
                onu_state = self.ONU_STATES.get(reg_status, f"Unknown ({reg_status})")
            return PONData(
                onu_state=onu_state,
                rx_power_dbm=data.get("rx_power"),
                tx_power_dbm=data.get("tx_power"),
                voltage_mv=data.get("voltage"),
                current_ma=data.get("current"),
                temperature_c=data.get("temperature"),
                video_rx_power_dbm=data.get("video_rx"),
                rf_tx_power_dbm=data.get("rf_tx"),
            )
        except Exception:
            try:
                body = self._request_raw('/getpage.gch?pid=1002&nextpage=pon_status_link_info_t.gch')
                if not body:
                    raise ZTEClientError("Failed to fetch PON data")
                try:
                    text = body.decode('gb2312', 'replace')
                except Exception:
                    text = body.decode('utf-8', 'replace')
                out = {}
                for k, pat in [
                    ('rx_power', r'RxPower\s*=\s*"([-\d.]+)"'),
                    ('tx_power', r'TxPower\s*=\s*"([-\d.]+)"'),
                    ('reg_status', r'RegStatus\s*=\s*"(\d+)"'),
                ]:
                    m = re.search(pat, text)
                    if m:
                        out[k] = float(m.group(1)) if '.' in m.group(1) else int(m.group(1))
                if 'reg_status' not in out:
                    m_reg = re.search(r"Transfer_meaning\s*\(\s*['\"]RegStatus['\"]\s*,\s*['\"](\d+)['\"]", text)
                    if m_reg:
                        out['reg_status'] = int(m_reg.group(1))

                for k, fid in [
                    ('voltage_mv', 'Frm_Volt'),
                    ('current_ma', 'Frm_Current'),
                    ('temperature_c', 'Frm_Temp'),
                ]:
                    m = re.search(rf'id="{fid}"[^>]*>([^<]+)', text)
                    if m:
                        v = m.group(1).strip()
                        try:
                            out[k] = int(float(v)) if k == 'voltage_mv' or k == 'temperature_c' else float(v)
                        except Exception:
                            pass

                rx_val = out.get('rx_power')
                tx_val = out.get('tx_power')
                volt_val = out.get('voltage_mv')
                curr_val = out.get('current_ma')
                temp_val = out.get('temperature_c')

                if rx_val is not None and abs(rx_val) > 1000:
                    rx_val = round(rx_val / 10000.0, 2)
                if tx_val is not None and abs(tx_val) > 1000:
                    tx_val = round(tx_val / 10000.0, 2)
                if volt_val is not None and volt_val > 10000:
                    volt_val = round(volt_val / 1000.0)
                if curr_val is not None and curr_val > 1000:
                    curr_val = round(curr_val / 1000.0, 2)

                reg_status = out.get('reg_status')
                onu_state = None
                if isinstance(reg_status, int):
                    onu_state = self.ONU_STATES.get(reg_status, f"Unknown ({reg_status})")
                return PONData(
                    onu_state=onu_state,
                    rx_power_dbm=rx_val,
                    tx_power_dbm=tx_val,
                    voltage_mv=volt_val,
                    current_ma=curr_val,
                    temperature_c=temp_val,
                )
            except Exception as e:
                raise ZTEClientError(f"Failed to fetch PON data: {e}")

    def get_device_info(self) -> DeviceInfo:
        """
        Fetch device information.

        Returns:
            DeviceInfo object with device details
        """
        try:
            if self.is_gch_device:
                raise ZTEClientError("GCH device")
            if not self._load_view("statusMgr"):
                raise ZTESessionError("Failed to load status view")
            ts = self._get_timestamp()
            response = self._request(f"/?_type=menuData&_tag=devmgr_statusmgr_lua.lua&_={ts}", encoding='utf-8')
            if not response:
                raise ZTEClientError("Failed to fetch device info")
            if "SessionTimeout" in response:
                self._logged_in = False
                raise ZTESessionError("Session expired")
            patterns = {
                "model": r"<ParaName>ModelName</ParaName><ParaValue>([^<]+)</ParaValue>",
                "serial": r"<ParaName>SerialNumber</ParaName><ParaValue>([^<]+)</ParaValue>",
                "hw_ver": r"<ParaName>HardwareVer</ParaName><ParaValue>([^<]+)</ParaValue>",
                "sw_ver": r"<ParaName>SoftwareVer</ParaName><ParaValue>([^<]+)</ParaValue>",
                "boot_ver": r"<ParaName>BootVer</ParaName><ParaValue>([^<]+)</ParaValue>",
                "manufacturer": r"<ParaName>ManuFacturer</ParaName><ParaValue>([^<]+)</ParaValue>",
                "oui": r"<ParaName>ManuFacturerOui</ParaName><ParaValue>([^<]+)</ParaValue>",
            }
            data = self._parse_xml_params(response, patterns)
            dev_info = DeviceInfo(
                model=data.get("model"),
                serial_number=data.get("serial"),
                hardware_version=data.get("hw_ver"),
                software_version=data.get("sw_ver"),
                boot_version=data.get("boot_ver"),
                manufacturer=data.get("manufacturer"),
                oui=data.get("oui"),
            )
            if dev_info.model:
                self.model = dev_info.model
            return dev_info
        except Exception:
            # Fallback: gch page
            try:
                body = self._request_raw('/getpage.gch?pid=1002&nextpage=status_dev_info_t.gch')
                if not body:
                    raise ZTEClientError("Failed to fetch device info")
                try:
                    text = body.decode('gb2312', 'replace')
                except Exception:
                    text = body.decode('utf-8', 'replace')
                import html as _html
                out = {}
                for k, fid in [
                    ('model', 'Frm_ModelName'),
                    ('serial_number', 'Frm_SerialNumber'),
                    ('hardware_version', 'Frm_HardwareVer'),
                    ('software_version', 'Frm_SoftwareVer'),
                    ('boot_version', 'Frm_BootVer'),
                ]:
                    m = re.search(rf'id="{fid}"[^>]*>(.*?)</td>', text, re.S)
                    if not m:
                        m = re.search(rf'id="{fid}"[^>]*>(.*?)<', text, re.S)
                    if m:
                        v = re.sub(r'<[^>]+>', '', m.group(1)).strip()
                        out[k] = _html.unescape(v)
                dev_info = DeviceInfo(**out)
                if dev_info.model:
                    if "F477" in dev_info.model.upper():
                        self.model = "F477V2"
                    else:
                        self.model = dev_info.model
                return dev_info
            except Exception as e:
                raise ZTEClientError(f"Failed to fetch device info: {e}")

    def get_lan_status(self) -> list[LANPort]:
        """
        Fetch LAN port status.
        """
        try:
            if self.is_gch_device:
                raise ZTEClientError("GCH device")
            if not self._load_view("localNetStatus"):
                raise ZTESessionError("Failed to load LAN status view")
            ts = self._get_timestamp()
            response = self._request(f"/?_type=menuData&_tag=status_lan_info_lua.lua&_={ts}")
            if not response:
                raise ZTEClientError("Failed to fetch LAN status")
            if "SessionTimeout" in response:
                self._logged_in = False
                raise ZTESessionError("Session expired")
            ports = []
            instances = re.findall(r'<Instance>(.*?)</Instance>', response, re.DOTALL)
            for instance in instances:
                patterns = {
                    "id": r"<ParaName>_InstID</ParaName><ParaValue>([^<]+)</ParaValue>",
                    "status": r"<ParaName>Status</ParaName><ParaValue>([^<]+)</ParaValue>",
                    "speed": r"<ParaName>Speed</ParaName><ParaValue>([^<]+)</ParaValue>",
                    "duplex": r"<ParaName>Duplex</ParaName><ParaValue>([^<]+)</ParaValue>",
                    "bytes_rx": r"<ParaName>InBytes</ParaName><ParaValue>([^<]+)</ParaValue>",
                    "bytes_tx": r"<ParaName>OutBytes</ParaName><ParaValue>([^<]+)</ParaValue>",
                    "packets_rx": r"<ParaName>InPkts</ParaName><ParaValue>([^<]+)</ParaValue>",
                    "packets_tx": r"<ParaName>OutPkts</ParaName><ParaValue>([^<]+)</ParaValue>",
                }
                data = self._parse_xml_params(instance, patterns)
                if data.get("id"):
                    port_id = data["id"]
                    match = re.search(r'IF(\d+)', port_id)
                    name = f"LAN{match.group(1)}" if match else port_id
                    link = "Up" if data.get("status") == 0 else "Down"
                    ports.append(LANPort(
                        name=name,
                        link=link,
                        speed=data.get("speed"),
                        duplex=data.get("duplex"),
                        bytes_rx=data.get("bytes_rx"),
                        bytes_tx=data.get("bytes_tx"),
                        packets_rx=data.get("packets_rx"),
                        packets_tx=data.get("packets_tx"),
                    ))
            return ports
        except Exception:
            # gch fallback
            try:
                ports = []
                if self.model == "F609":
                    # F609 style
                    try:
                        body = self._request_raw('/getpage.gch?pid=1002&nextpage=pon_status_lan_info_t.gch')
                        if body:
                            try:
                                text = body.decode('gb2312', 'replace')
                            except Exception:
                                text = body.decode('utf-8', 'replace')
                            for i in range(1, 5):
                                m = re.search(rf'Ethernet Port\s*</td>\s*<td[^>]*>\s*LAN{i}\b', text)
                                if not m:
                                    continue
                                seg = text[m.start():m.start()+800]
                                link = 'Down'
                                if re.search(r'Status[^<]*Linkup', seg, re.I) or ('Status' in seg and 'Linkup' in seg):
                                    link = 'Up'
                                elif re.search(r'Status[^<]*Linkdown', seg, re.I) or re.search(r'Status[^<]*Down', seg, re.I):
                                    link = 'Down'
                                speed = None
                                sm = re.search(r'Speed[^<]*>(\d+)\s*M', seg, re.I)
                                if sm:
                                    speed = int(sm.group(1))
                                dm = re.search(r'Mode[^<]*>(Full|Half)', seg, re.I)
                                duplex = dm.group(1) if dm else None
                                ports.append(LANPort(name=f'LAN{i}', link=link, speed=speed, duplex=duplex))
                    except Exception:
                        pass
                else:
                    # F677V2 / F477V2 / generic table style
                    try:
                        body = self._request_raw('/getpage.gch?pid=1002&nextpage=status_lan_info_t.gch')
                        if body:
                            try:
                                text = body.decode('gb2312', 'replace')
                            except Exception:
                                text = body.decode('utf-8', 'replace')
                            tables = re.findall(r"<table[^>]*>.*?</table>", text, re.DOTALL | re.IGNORECASE)
                            for tbl in tables:
                                m_name = re.search(r"(?:Ethernet Port|Port name)\s*</td>\s*<td[^>]*>(LAN\d+)</td>", tbl, re.IGNORECASE)
                                if not m_name:
                                    continue
                                name = m_name.group(1).upper()
                                m_stat = re.search(r"[Ss]tatus\s*</td>\s*<td[^>]*>([^<]+)</td>", tbl, re.IGNORECASE)
                                stat_raw = m_stat.group(1).strip() if m_stat else ""
                                link = "Up" if ("up" in stat_raw.lower() or ("connect" in stat_raw.lower() and "not" not in stat_raw.lower())) else "Down"
                                m_mode = re.search(r"Mode\s*</td>\s*<td[^>]*>([^<]+)</td>", tbl, re.IGNORECASE)
                                duplex = None
                                if m_mode:
                                    duplex = "Full" if "full" in m_mode.group(1).lower() else ("Half" if "half" in m_mode.group(1).lower() else None)
                                m_rate = re.search(r"Rate\s*</td>\s*<td[^>]*>(\d+)", tbl, re.IGNORECASE)
                                speed = int(m_rate.group(1)) if m_rate else None
                                m_rx = re.search(r"(?:Packets Received/Bytes Received|Received packets/bytes)\s*</td>\s*<td[^>]*>(\d+)/(\d+)</td>", tbl, re.IGNORECASE)
                                pkts_rx = int(m_rx.group(1)) if m_rx else None
                                bytes_rx = int(m_rx.group(2)) if m_rx else None
                                m_tx = re.search(r"(?:Packets Sent/Bytes Sent|Transmit packets/bytes)\s*</td>\s*<td[^>]*>(\d+)/(\d+)</td>", tbl, re.IGNORECASE)
                                pkts_tx = int(m_tx.group(1)) if m_tx else None
                                bytes_tx = int(m_tx.group(2)) if m_tx else None
                                ports.append(LANPort(
                                    name=name, link=link, speed=speed, duplex=duplex,
                                    bytes_rx=bytes_rx, bytes_tx=bytes_tx,
                                    packets_rx=pkts_rx, packets_tx=pkts_tx
                                ))
                    except Exception:
                        pass

                if not ports:
                    raise ZTEClientError("Failed to parse LAN status")
                return ports
            except Exception as e:
                raise ZTEClientError(f"Failed to fetch LAN status: {e}")

    def get_voip_status(self) -> list[VoIPLine]:
        try:
            if not self._load_view("voipStatus"):
                raise ZTESessionError("Failed to load VoIP status view")
            ts = self._get_timestamp()
            response = self._request(f"/?_type=menuData&_tag=voipRegStatus_lua.lua&_={ts}")
            if not response:
                raise ZTEClientError("Failed to fetch VoIP status")
            if "SessionTimeout" in response:
                self._logged_in = False
                raise ZTESessionError("Session expired")
            lines = []
            instances = re.findall(r'<Instance>(.*?)</Instance>', response, re.DOTALL)
            for i, instance in enumerate(instances, 1):
                patterns = {
                    "number": r"<ParaName>DirectoryNumber</ParaName><ParaValue>([^<]+)</ParaValue>",
                    "status": r"<ParaName>IsOnline</ParaName><ParaValue>([^<]+)</ParaValue>",
                }
                data = self._parse_xml_params(instance, patterns)
                number = data.get("number", "")
                if number:
                    number = str(number)
                is_online = data.get("status") == 1
                if not number or number == "":
                    status = "Inactive"
                elif is_online:
                    status = "Registered"
                else:
                    status = "Unregistered"
                lines.append(VoIPLine(line_id=f"Line{i}", number=number if number else None, status=status, registered=is_online))
            return lines
        except Exception:
            try:
                import html as _html
                body = self._request_raw('/getpage.gch?pid=1002&nextpage=status_voip_4less_t.gch')
                if not body:
                    body = self._request_raw('/getpage.gch?pid=1002&nextpage=status_voip_4less1_t.gch')
                if not body:
                    return []
                try:
                    text = body.decode('gb2312', 'replace')
                except Exception:
                    text = body.decode('utf-8', 'replace')
                lines = []
                pairs = re.findall(r'<td[^>]*>([^<]+)</td>\s*<td[^>]*>([^<]+)</td>', text, re.I | re.S)
                for k, v in pairs:
                    k2 = k.strip()
                    v2 = _html.unescape(v.strip())
                    if 'Register Status' in k2:
                        vlow = v2.lower()
                        registered = 'register' in vlow and 'unreg' not in vlow and 'inactive' not in vlow
                        status = 'Registered' if registered else (v2 if v2 else 'Inactive')
                        lines.append(VoIPLine(line_id=f'Line{len(lines)+1}', status=status, registered=registered))
                return lines
            except Exception:
                return []
    def get_wan_status(self) -> list[WANConnection]:
        """
        Fetch WAN connection status.
        """
        try:
            if self.is_gch_device:
                raise ZTEClientError("GCH device")
            if not self._load_view("ethWanConfig"):
                raise ZTESessionError("Failed to load WAN status view")
            ts = self._get_timestamp()
            response = self._request(
                f"/?_type=menuData&_tag=wan_internet_lua.lua&TypeUplink=2&pageType=0&_={ts}"
            )
            if not response:
                raise ZTEClientError("Failed to fetch WAN status")
            if "SessionTimeout" in response:
                self._logged_in = False
                raise ZTESessionError("Session expired")
            connections = []
            instances = re.findall(r'<Instance>(.*?)</Instance>', response, re.DOTALL)
            for instance in instances:
                patterns = {
                    "name": r"<ParaName>WANCName</ParaName><ParaValue>([^<]+)</ParaValue>",
                    "type": r"<ParaName>TransType</ParaName><ParaValue>([^<]+)</ParaValue>",
                    "mode": r"<ParaName>mode</ParaName><ParaValue>([^<]+)</ParaValue>",
                    "vlan_id": r"<ParaName>VLANID</ParaName><ParaValue>([^<]+)</ParaValue>",
                    "mac": r"<ParaName>WorkIFMac</ParaName><ParaValue>([^<]+)</ParaValue>",
                    "mtu": r"<ParaName>MTU</ParaName><ParaValue>([^<]+)</ParaValue>",
                    "tx_bytes": r"<ParaName>TxBytes</ParaName><ParaValue>([^<]+)</ParaValue>",
                    "rx_bytes": r"<ParaName>RxBytes</ParaName><ParaValue>([^<]+)</ParaValue>",
                    "conn_error": r"<ParaName>ConnError</ParaName><ParaValue>([^<]+)</ParaValue>",
                    "username": r"<ParaName>UserName</ParaName><ParaValue>([^<]+)</ParaValue>",
                }
                data = self._parse_xml_params(instance, patterns)
                if data.get("name"):
                    conn_error = data.get("conn_error", "")
                    status = "Connected" if conn_error == "ERROR_NONE" else conn_error
                    connections.append(WANConnection(
                        name=data.get("name"),
                        connection_type=data.get("type"),
                        status=status,
                        tx_bytes=data.get("tx_bytes"),
                        rx_bytes=data.get("rx_bytes"),
                        vlan_id=data.get("vlan_id"),
                        mac_address=data.get("mac"),
                        username=data.get("username"),
                    ))
            return connections
        except Exception:
            try:
                import html as _html
                conns = []
                # Check status_ethwan_if_t.gch (F677V2, F477V2, and modern GCH models)
                try:
                    wbody = self._request_raw('/getpage.gch?pid=1002&nextpage=status_ethwan_if_t.gch')
                    if wbody:
                        try:
                            wtext = wbody.decode('gb2312', 'replace')
                        except Exception:
                            wtext = wbody.decode('utf-8', 'replace')
                        tables = re.findall(r"<table[^>]*>.*?</table>", wtext, re.DOTALL | re.IGNORECASE)
                        for tbl in tables:
                            if "Connection" not in tbl and "WAN MAC" not in tbl:
                                continue
                            pairs = {}
                            for m in re.finditer(r"<td[^>]*class=[\x27\x22]tdleft(?:_\d+)?[\x27\x22][^>]*>(.*?)</td>\s*<td[^>]*class=[\x27\x22]tdright(?:_\d+)?[\x27\x22][^>]*>(.*?)</td>", tbl, re.DOTALL | re.IGNORECASE):
                                k = re.sub(r"<[^>]+>", "", m.group(1)).strip()
                                v = _html.unescape(re.sub(r"<[^>]+>", "", m.group(2)).strip())
                                pairs[k] = v
                            if pairs.get("Connection Name") or pairs.get("Connection name") or pairs.get("WAN MAC"):
                                c_name = pairs.get("Connection Name") or pairs.get("Connection name") or "WAN"
                                u_pppoe, p_pppoe = None, None
                                try:
                                    u_pppoe, p_pppoe = self._get_pppoe_creds_f677v2(len(conns), c_name)
                                except Exception:
                                    pass
                                conns.append({
                                    'name': c_name,
                                    'connection_type': pairs.get("Type") or pairs.get("mode"),
                                    'ip_address': pairs.get("IP"),
                                    'gateway': pairs.get("Gateway"),
                                    'dns_servers': ", ".join(filter(None, [pairs.get("DNS1"), pairs.get("DNS2")])) or pairs.get("DNS"),
                                    'status': pairs.get("Connection Status") or "Connected",
                                    'mac_address': pairs.get("WAN MAC"),
                                    'username': u_pppoe,
                                    'password': p_pppoe,
                                })
                except Exception:
                    pass
                else:
                    try:
                        body = self._request_raw('/getpage.gch?pid=1002&nextpage=IPv46_status_wan2_if_t.gch')
                        if body:
                            try:
                                text = body.decode('gb2312', 'replace')
                            except Exception:
                                text = body.decode('utf-8', 'replace')
                            # look for td pairs
                            pairs = re.findall(r'<td[^>]*>([^<]+)</td>\s*<td[^>]*>([^<]+)</td>', text, re.I|re.S)
                            # collect sequentially
                            for k,v in pairs:
                                k2 = k.strip(); v2 = _html.unescape(v.strip())
                                if 'Connection Status' in k2:
                                    conns.append({'status': v2})
                                elif 'IP' in k2 and conns:
                                    if 'ip_address' not in conns[-1] or conns[-1]['ip_address'] in ('0.0.0.0', '&nbsp;'):
                                        conns[-1]['ip_address'] = v2
                                elif 'Connection Name' in k2 and conns:
                                    if 'name' not in conns[-1] or len(conns[-1]['name']) < 2:
                                        conns[-1]['name'] = v2
                            if not conns:
                                # inputs fallback
                                for m in re.finditer(r'<Input[^>]*id="([^"]+)"[^>]*value=["\']([^"\']*)["\']', text, re.I):
                                    fid, val = m.group(1), _html.unescape(m.group(2))
                                    if 'WANCName' in fid:
                                        conns.append({'name': val, 'status': 'Connected'})
                                    if 'IP' in fid and conns and not conns[-1].get('ip_address'):
                                        conns[-1]['ip_address'] = val
                    except Exception:
                        pass

                if not conns:
                    raise ZTEClientError("No WAN data")
                return [WANConnection(**c) for c in conns]
            except Exception as e:
                raise ZTEClientError(f"Failed to fetch WAN status: {e}")

    def get_system_stats(self) -> dict:
        """
        Fetch system statistics (CPU, memory usage, uptime).

        Returns:
            Dictionary with system stats
        """
        if self.is_gch_device:
            return {}

        if not self._load_view("statusMgr"):
            raise ZTESessionError("Failed to load status view")

        ts = self._get_timestamp()
        response = self._request(f"/?_type=menuData&_tag=devmgr_statusmgr_lua.lua&_={ts}")

        if not response:
            raise ZTEClientError("Failed to fetch system stats")

        if "SessionTimeout" in response:
            self._logged_in = False
            raise ZTESessionError("Session expired")

        # Parse CPU usage (4 cores) and memory
        patterns = {
            "cpu1": r"<ParaName>CpuUsage1</ParaName><ParaValue>([^<]+)</ParaValue>",
            "cpu2": r"<ParaName>CpuUsage2</ParaName><ParaValue>([^<]+)</ParaValue>",
            "cpu3": r"<ParaName>CpuUsage3</ParaName><ParaValue>([^<]+)</ParaValue>",
            "cpu4": r"<ParaName>CpuUsage4</ParaName><ParaValue>([^<]+)</ParaValue>",
            "memory_usage": r"<ParaName>MemUsage</ParaName><ParaValue>([^<]+)</ParaValue>",
            "uptime_seconds": r"<ParaName>PowerOnTime</ParaName><ParaValue>([^<]+)</ParaValue>",
            "flash_used_percent": r"<ParaName>Flash_Percent_Used</ParaName><ParaValue>([^<]+)</ParaValue>",
        }

        data = self._parse_xml_params(response, patterns)

        # Calculate average CPU usage
        cpu_values = [data.get(f"cpu{i}") for i in range(1, 5) if data.get(f"cpu{i}") is not None]
        if cpu_values:
            data["cpu_usage"] = int(sum(cpu_values) / len(cpu_values))

        return data

    def get_connected_devices(self) -> list[ConnectedDevice]:
        """
        Fetch list of connected devices.

        Returns:
            List of ConnectedDevice objects
        """
        if self.is_gch_device:
            return []

        # Load homepage view first
        if not self._load_view("homePage"):
            raise ZTESessionError("Failed to load home page view")

        ts = self._get_timestamp()
        response = self._request(
            f"/?_type=menuData&_tag=accessdev_homepage_lua.lua&_={ts}"
        )

        if not response:
            raise ZTEClientError("Failed to fetch connected devices")

        if "SessionTimeout" in response:
            self._logged_in = False
            raise ZTESessionError("Session expired")

        devices = []
        instances = re.findall(r'<Instance>(.*?)</Instance>', response, re.DOTALL)

        for instance in instances:
            patterns = {
                "ip": r"<ParaName>IPAddress</ParaName><ParaValue>([^<]+)</ParaValue>",
                "mac": r"<ParaName>MACAddress</ParaName><ParaValue>([^<]+)</ParaValue>",
                "interface": r"<ParaName>AliasName</ParaName><ParaValue>([^<]+)</ParaValue>",
                "last_seen": r"<ParaName>LastConnection</ParaName><ParaValue>([^<]+)</ParaValue>",
            }

            data = self._parse_xml_params(instance, patterns)

            if data.get("ip") and data.get("mac"):
                # Clean up HTML entities in last_seen
                last_seen = data.get("last_seen", "")
                if last_seen:
                    last_seen = last_seen.replace("&#32;", " ")

                devices.append(ConnectedDevice(
                    ip_address=str(data.get("ip")),
                    mac_address=str(data.get("mac")),
                    interface=data.get("interface"),
                    last_seen=last_seen if last_seen else None,
                ))

        return devices

    def get_wifi_networks(self) -> list[dict]:
        """Get WiFi SSIDs and passwords for all configured networks."""
        import html as _html

        def _unescape(val: str) -> str:
            if not val:
                return ""
            val = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), val)
            return _html.unescape(val).strip()

        networks = []

        # 0. Try F477V2 style (e8_status_wlan_info_t.gch + net_wlan_secrity_t.gch)
        if self.model in ("F477V2", "ZXHN F477V2") or self.model != "F609":
            try:
                raw_status = self._request_raw("/getpage.gch?pid=1002&nextpage=e8_status_wlan_info_t.gch")
                if raw_status:
                    text_status = raw_status.decode("latin1", "replace")
                    f477_ssids = []
                    for m in re.finditer(r"Transfer_meaning\s*\(\s*['\"]ESSID(\d+)['\"]\s*,\s*['\"]([^'\"]+)['\"]", text_status):
                        idx = m.group(1)
                        name = _unescape(m.group(2))
                        if name:
                            f477_ssids.append((idx, name))

                    if f477_ssids:
                        raw_sec = self._request_raw("/getpage.gch?pid=1002&nextpage=net_wlan_secrity_t.gch")
                        text_sec = raw_sec.decode("latin1", "replace") if raw_sec else ""
                        m_pwds = re.findall(r"Transfer_meaning\s*\(\s*['\"]KeyPassphrase['\"]\s*,\s*['\"]([^'\"]+)['\"]", text_sec)
                        pwd = None
                        for p in reversed(m_pwds):
                            up = _unescape(p)
                            if up:
                                pwd = up
                                break

                        for idx, name in f477_ssids:
                            networks.append({
                                "ssid": name,
                                "password": pwd if idx == "0" else None,
                                "enabled": True,
                                "band": "2.4G",
                                "security": "WPA/WPA2-PSK" if (pwd and idx == "0") else "Open",
                            })
                        if networks:
                            if not self.model:
                                self.model = "F477V2"
                            return networks
            except Exception:
                pass

        # 1. Try F677V2 style if model is F677V2 or not F609
        if self.model != "F609":
            try:
                raw_conf = self._request_raw("/getpage.gch?pid=1002&nextpage=pon_net_wlan_conf1_t.gch")
                if raw_conf:
                    text_conf = raw_conf.decode("latin1", "replace")
                    m_ssid = re.search(r"Transfer_meaning\s*\(\s*['\"]ESSID['\"]\s*,\s*['\"]([^'\"]+)['\"]", text_conf)
                    if m_ssid:
                        ssid_name = _unescape(m_ssid.group(1))
                        m_pwds = re.findall(r"Transfer_meaning\s*\(\s*['\"]KeyPassphrase['\"]\s*,\s*['\"]([^'\"]+)['\"]", text_conf)
                        pwd = None
                        for p in reversed(m_pwds):
                            up = _unescape(p)
                            if up:
                                pwd = up
                                break
                        m_en = re.search(r"Transfer_meaning\s*\(\s*['\"]Enable['\"]\s*,\s*['\"]([^'\"]+)['\"]", text_conf)
                        is_en = (m_en.group(1).strip() == "1") if m_en else True
                        if ssid_name:
                            networks.append({
                                "ssid": ssid_name,
                                "password": pwd,
                                "enabled": is_en,
                                "band": "2.4G",
                                "security": "WPA/WPA2-PSK" if pwd else "Open",
                            })
                            if not self.model:
                                self.model = "F677V2"
            except Exception:
                pass
            if networks:
                return networks

        # 2. Try F609 style (status_wlanm_info1_t.gch + net_wlanm_secrity1_t.gch)
        try:
            raw_status = self._request_raw("/getpage.gch?pid=1002&nextpage=status_wlanm_info1_t.gch")
            if raw_status:
                text_status = raw_status.decode("latin1", "replace")
                enables = {}
                for m in re.finditer(r"Transfer_meaning\s*\(\s*['\"]Enable(\d+)['\"]\s*,\s*['\"]([^'\"]+)['\"]", text_status):
                    enables[m.group(1)] = (m.group(2).strip() == "1")

                f609_ssids = []
                for m in re.finditer(r"Transfer_meaning\s*\(\s*['\"]ESSID(\d+)['\"]\s*,\s*['\"]([^'\"]+)['\"]", text_status):
                    idx = m.group(1)
                    name = _unescape(m.group(2))
                    if name:
                        f609_ssids.append((idx, name, enables.get(idx, True)))

                if f609_ssids:
                    raw_sec = self._request_raw("/getpage.gch?pid=1002&nextpage=net_wlanm_secrity1_t.gch")
                    text_sec = raw_sec.decode("latin1", "replace") if raw_sec else ""
                    pwd = None
                    for m in re.finditer(r"Transfer_meaning\s*\(\s*['\"]KeyPassphrase['\"]\s*,\s*['\"]([^'\"]+)['\"]", text_sec):
                        val = _unescape(m.group(1))
                        if val:
                            pwd = val

                    for idx, name, is_enabled in f609_ssids:
                        networks.append({
                            "ssid": name,
                            "password": pwd if idx == "0" else None,
                            "enabled": is_enabled,
                            "band": "2.4G",
                            "security": "WPA/WPA2-PSK" if pwd else "Open",
                        })
        except Exception:
            pass
        if networks:
            return networks

        # 3. Try F677V2 style if not yet tried
        if not networks:
            try:
                raw_conf = self._request_raw("/getpage.gch?pid=1002&nextpage=pon_net_wlan_conf1_t.gch")
                if raw_conf:
                    text_conf = raw_conf.decode("latin1", "replace")
                    m_ssid = re.search(r"Transfer_meaning\s*\(\s*['\"]ESSID['\"]\s*,\s*['\"]([^'\"]+)['\"]", text_conf)
                    if m_ssid:
                        ssid_name = _unescape(m_ssid.group(1))
                        m_pwds = re.findall(r"Transfer_meaning\s*\(\s*['\"]KeyPassphrase['\"]\s*,\s*['\"]([^'\"]+)['\"]", text_conf)
                        pwd = None
                        for p in reversed(m_pwds):
                            up = _unescape(p)
                            if up:
                                pwd = up
                                break
                        m_en = re.search(r"Transfer_meaning\s*\(\s*['\"]Enable['\"]\s*,\s*['\"]([^'\"]+)['\"]", text_conf)
                        is_en = (m_en.group(1).strip() == "1") if m_en else True
                        if ssid_name:
                            networks.append({
                                "ssid": ssid_name,
                                "password": pwd,
                                "enabled": is_en,
                                "band": "2.4G",
                                "security": "WPA/WPA2-PSK" if pwd else "Open",
                            })
            except Exception:
                pass

        # 3. Fallback to standard ZTE pages (wlan_basic_t.gch / wlan_security_t.gch)
        if not networks:
            try:
                for basic, sec in [('wlan_basic_t.gch','wlan_security_t.gch'), ('wlan1_basic_t.gch','wlan1_security_t.gch')]:
                    try:
                        b = self._request_raw(f'/getpage.gch?pid=1002&nextpage={basic}')
                        s = self._request_raw(f'/getpage.gch?pid=1002&nextpage={sec}')
                        if not b: continue
                        tb = b.decode('gb2312','replace')
                        ts = s.decode('gb2312','replace') if s else ''
                        ssids = []
                        for m in re.finditer(r'name="([^"]+)"[^>]*value="([^"]*)"', tb, re.I):
                            if 'SSID' in m.group(1) and m.group(2).strip():
                                ssids.append(_unescape(m.group(2).strip()))
                        pwds = []
                        for m in re.finditer(r'name="([^"]+(?:PSK|PreSharedKey|Key|Passphrase|WPAPSK)[^"]*)"[^>]*value="([^"]*)"', ts, re.I):
                            pwds.append(_unescape(m.group(2)))
                        if not pwds:
                            for m in re.finditer(r'name="([^"]+)"[^>]*value="([^"]*)"', ts, re.I):
                                if ('Key' in m.group(1) or 'PSK' in m.group(1)) and m.group(2):
                                    pwds.append(_unescape(m.group(2)))
                        for i, ssid in enumerate(ssids):
                            pwd = pwds[i] if i < len(pwds) else (pwds[-1] if pwds else None)
                            networks.append({
                                'ssid': ssid,
                                'password': pwd,
                                'enabled': True,
                                'band': '2.4G',
                                'security': 'WPA/WPA2-PSK' if pwd else 'Open'
                            })
                    except Exception:
                        continue
            except Exception:
                pass

        valid = [n for n in networks if n.get('ssid')]
        if not valid:
            raise ZTEClientError("Tidak ada jaringan WiFi terdeteksi atau sesi kadaluarsa")
        return valid

    def backup_config(self) -> bytes:
        """
        Download router configuration binary (config.bin).
        """
        if not self._logged_in:
            self.login()

        # 1. Try standard GCH config page (F609, F660, F670, etc.)
        for attempt in range(2):
            try:
                time.sleep(0.2)
                resp = self._request_raw("/getpage.gch?pid=1002&nextpage=manager_dev_config_t.gch")
                txt = resp.decode("latin1", "replace") if resp else ""
                m_token = re.search(r'UPLOAD_SESSION_TOKEN.*?value=[\'"]([^\'"]+)[\'"]', txt, re.I)
                token = m_token.group(1) if m_token else None
                if token:
                    time.sleep(0.2)
                    try:
                        self._post_raw("/checkSessionToken.gch", {"UPLOAD_ENV": token})
                    except Exception:
                        pass

                    m_act = re.search(r'action=[\'"]([^\'"]*(?:pid=101)[^\'"]*)[\'"]', txt)
                    if m_act:
                        import html as _html
                        action = _html.unescape(m_act.group(1))
                        url = f"{self.base_url}/{action.lstrip('/')}"
                        boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
                        body = (
                            f"--{boundary}\r\n"
                            'Content-Disposition: form-data; name="config"\r\n\r\n'
                            "\r\n"
                            f"--{boundary}--\r\n"
                        ).encode("latin1")
                        req = Request(
                            url,
                            data=body,
                            headers={
                                "Content-Type": f"multipart/form-data; boundary={boundary}",
                                "Referer": f"{self.base_url}/getpage.gch?pid=1002&nextpage=manager_dev_config_t.gch",
                                "Origin": self.base_url,
                                "Connection": "close",
                            },
                        )
                        time.sleep(0.2)
                        with self.opener.open(req, timeout=self.timeout) as r:
                            data = r.read()
                            if len(data) > 64:
                                return data
            except Exception as e:
                if attempt == 0:
                    time.sleep(0.5)
                    continue

        # 2. Try direct download endpoints
        for endpoint in [
            "/getpage.gch?pid=101&nextpage=manager_dev_config_t.gch",
            "/config.bin",
            "/backup.bin",
        ]:
            try:
                data = self._request_raw(endpoint)
                if data and len(data) > 64 and (data[:4] in (b"\x04\x03\x02\x01", b"\x01\x02\x03\x04") or data[:2] == b"\x1f\x8b"):
                    return data
            except Exception:
                pass

        raise ZTEClientError(f"Fitur download konfigurasi (config.bin) tidak didukung atau diblokir pada firmware {self.model or 'router ini'}")

    def __enter__(self):
        """Context manager entry"""
        self.login()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        self.logout()
        return False
