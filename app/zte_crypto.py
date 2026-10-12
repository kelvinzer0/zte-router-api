"""
ZTE Router Configuration File (config.bin) Decryptor & Parser

Supports ZTE config.bin payload types:
- Type 0: Unencrypted zlib compression
- Type 2: AES-128-ECB + zlib compression
- Type 3/4: AES-256-CBC + zlib compression

Extracts:
- PPPoE usernames and plaintext passwords
- Web admin logins and passwords
- WiFi SSIDs and WPA keys
"""

import io
import re
import struct
import zlib
from typing import BinaryIO, Optional
import xml.etree.ElementTree as ET

try:
    from Cryptodome.Cipher import AES
except ImportError:
    try:
        from Crypto.Cipher import AES
    except ImportError:
        AES = None

PAYLOAD_MAGIC = 0x01020304
ZTE_MAGIC = 0x04030201

# Known keys for ZTE routers
KNOWN_KEYS = [
    "MIK@0STzKpB%qJZe",
    "MIK@0STzKpB%qJZf",
    "402c38de39bed665",
    "Q#Zxn*x3kVLc",
    "Wj%2$CjM",
    "Wj",
    "m8@96&ZG3Nm7N&Iz",
    "GrWM2Hz&LTvz&f^5",
    "GrWM3Hz&LTvz&f^9",
    "Renjx%2$CjM",
    "tHG@Ti&GVh@ql3XN",
    "SDEwOE5WMi41Uk9T",
    "Zte521",
    "zte521",
    "f680",
    "f660v9.0",
    "f609",
    "F609",
    "Key02710001",
    "Key02710010",
    "Key02721401",
    "Key02660004",
    "Key02670001",
]


class ZTEConfigError(Exception):
    """Base exception for config decryption errors"""
    pass


class ZTEConfigHeader:
    """Header metadata for ZTE config.bin"""
    def __init__(self, signature: str, payload_type: int, payload_start: int, total_size: int, chunk_size: int):
        self.signature = signature
        self.payload_type = payload_type
        self.payload_start = payload_start
        self.total_size = total_size
        self.chunk_size = chunk_size

    def __repr__(self):
        return (f"ZTEConfigHeader(signature={self.signature!r}, type={self.payload_type}, "
                f"start={self.payload_start}, total_size={self.total_size})")


def parse_header(data: bytes) -> ZTEConfigHeader:
    """Parse signature and payload metadata from config.bin"""
    if len(data) < 60:
        raise ZTEConfigError("Data too short to be a valid ZTE config file")

    sig_magic = struct.unpack(">I", data[:4])[0]
    payload_start = 0
    signature = ""

    if sig_magic in (ZTE_MAGIC, 0x01020304):
        # Header present: [magic 4B][0 4B][sig_len 4B][sig ...]
        sig_len = struct.unpack(">I", data[8:12])[0]
        if sig_len > 0 and 12 + sig_len <= len(data):
            signature = data[12:12 + sig_len].decode("latin1", "replace").strip("\x00")
            payload_start = 12 + sig_len
        else:
            payload_start = 12
    else:
        payload_start = 0

    # Look for payload magic: 0x01020304
    idx = data.find(struct.pack(">I", PAYLOAD_MAGIC), payload_start)
    if idx == -1:
        # Check if direct zlib (0x78 0x01, 0x78 0x9c, 0x78 0xda)
        if data[:2] in (b"\x78\x01", b"\x78\x9c", b"\x78\xda"):
            return ZTEConfigHeader(signature="RAW_ZLIB", payload_type=0, payload_start=0, total_size=len(data), chunk_size=65536)
        raise ZTEConfigError("Payload magic (0x01020304) not found in config file")

    payload_start = idx
    if len(data) < payload_start + 24:
        raise ZTEConfigError("File truncated at payload header")

    magic, payload_type, unenc_len, total_size, chunk_size = struct.unpack(">5I", data[payload_start:payload_start + 20])
    # The payload block starts after 60-byte payload header
    block_start = payload_start + 60

    return ZTEConfigHeader(
        signature=signature,
        payload_type=payload_type,
        payload_start=block_start,
        total_size=total_size,
        chunk_size=chunk_size,
    )


def _read_chunks(infile: BinaryIO) -> bytes:
    """Read ZTE chunked blocks: [12-byte header (dec_size, enc_size, more_flag)][data]"""
    result = bytearray()
    while True:
        hdr = infile.read(12)
        if len(hdr) < 12:
            break
        chunk_size, dec_size, more_chunks = struct.unpack(">3I", hdr)
        chunk_data = infile.read(chunk_size)
        result.extend(chunk_data)
        if more_chunks == 0:
            break
    return bytes(result)


def _decompress_zlib(data: bytes) -> bytes:
    """Decompress raw or prefixed zlib stream"""
    try:
        return zlib.decompress(data)
    except zlib.error:
        # Try raw deflate without header
        try:
            return zlib.decompress(data, -zlib.MAX_WBITS)
        except zlib.error:
            # Try skipping prepended junk bytes
            for offset in (2, 4, 8, 12, 16):
                if offset < len(data):
                    try:
                        return zlib.decompress(data[offset:])
                    except zlib.error:
                        pass
            raise


def decrypt_type_2(encrypted_chunks: bytes, key: bytes) -> bytes:
    """Decrypt AES-128-ECB and decompress zlib"""
    if AES is None:
        raise ZTEConfigError("pycryptodomex library is required for decryption")
    key = key.ljust(16, b"\x00")[:16]
    cipher = AES.new(key, AES.MODE_ECB)
    decrypted = cipher.decrypt(encrypted_chunks)
    return _decompress_zlib(decrypted)


def decrypt_type_3_4(encrypted_chunks: bytes, key: bytes, iv: bytes) -> bytes:
    """Decrypt AES-256-CBC and decompress zlib"""
    if AES is None:
        raise ZTEConfigError("pycryptodomex library is required for decryption")
    key = key.ljust(32, b"\x00")[:32]
    iv = iv.ljust(16, b"\x00")[:16]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = cipher.decrypt(encrypted_chunks)
    return _decompress_zlib(decrypted)


def decrypt_config_bin(
    data: bytes,
    key: Optional[str] = None,
    serial: Optional[str] = None,
    mac: Optional[str] = None,
) -> tuple[bytes, str]:
    """
    Decrypt config.bin bytes and return (decompressed_xml_bytes, used_key).
    Tries specified key, then candidate keys, hardware serial/mac derivations.
    """
    # 1. Check if raw XML
    if data.strip().startswith(b"<?xml") or data.strip().startswith(b"<DB") or data.strip().startswith(b"<ZTE"):
        return data, "PLAINTEXT"

    header = parse_header(data)
    infile = io.BytesIO(data[header.payload_start:])
    encrypted_payload = _read_chunks(infile)
    if not encrypted_payload:
        encrypted_payload = data[header.payload_start:]

    # Type 0: No encryption, just zlib
    if header.payload_type == 0:
        decomp = _decompress_zlib(encrypted_payload)
        return decomp, "NONE"

    # Assemble candidate keys
    candidates = []
    if key:
        candidates.append(key)

    # Hardware-derived keys
    if serial:
        clean_sn = serial.strip()
        candidates.extend([
            clean_sn,
            f"8cc72b05705d5c46{clean_sn}",
            clean_sn.replace("ZTE", ""),
        ])
    if mac:
        clean_mac = mac.replace(":", "").replace("-", "").strip()
        candidates.extend([clean_mac, clean_mac.lower(), clean_mac.upper()])

    # Signature-based keys
    if header.signature:
        candidates.append(header.signature)
        candidates.append(header.signature.lower())
        candidates.append(f"{header.signature}Key02710001")
        candidates.append(f"{header.signature}Key02721401")

    # Add known dictionary keys
    for k in KNOWN_KEYS:
        if k not in candidates:
            candidates.append(k)

    last_error = None
    for cand in candidates:
        cand_bytes = cand.encode("latin1", "ignore")
        try:
            if header.payload_type == 2:
                decomp = decrypt_type_2(encrypted_payload, cand_bytes)
                return decomp, cand
            elif header.payload_type in (3, 4):
                # Try IV == key or zero IV
                for iv_cand in [cand_bytes[:16], b"\x00" * 16]:
                    try:
                        decomp = decrypt_type_3_4(encrypted_payload, cand_bytes, iv_cand)
                        return decomp, cand
                    except Exception:
                        continue
        except Exception as e:
            last_error = e
            continue

    raise ZTEConfigError(f"Gagal mendekripsi config.bin: tidak ditemukan kunci yang cocok (dicoba {len(candidates)} kunci). Error terakhir: {last_error}")


def extract_credentials_from_xml(xml_bytes: bytes) -> dict:
    """
    Extract WAN PPPoE credentials, WiFi passwords, and admin accounts from decrypted XML.
    """
    text = xml_bytes.decode("latin1", "replace")
    results = {
        "pppoe": [],
        "wifi": [],
        "admin": [],
        "tr069": [],
    }

    # 1. Search PPPoE Accounts
    # Patterns for ZTE XML (e.g. <DM name="UserName" value="..."/> or <ParaName>UserName</ParaName>)
    # Look for instances or DM tags with UserName and Password
    wancp_matches = re.finditer(r'<DM\s+name=[\'"]UserName[\'"]\s+value=[\'"]([^\'"]+)[\'"]\s*/>.*?<DM\s+name=[\'"]Password[\'"]\s+value=[\'"]([^\'"]*)[\'"]\s*/>', text, re.DOTALL | re.I)
    for m in wancp_matches:
        u, p = m.group(1).strip(), m.group(2).strip()
        if u and "@" in u or "telkom" in u or "net" in u or "user" in u.lower():
            results["pppoe"].append({"username": u, "password": p})

    # Also search reverse order (Password before UserName)
    wancp_rev = re.finditer(r'<DM\s+name=[\'"]Password[\'"]\s+value=[\'"]([^\'"]*)[\'"]\s*/>.*?<DM\s+name=[\'"]UserName[\'"]\s+value=[\'"]([^\'"]+)[\'"]\s*/>', text, re.DOTALL | re.I)
    for m in wancp_rev:
        p, u = m.group(1).strip(), m.group(2).strip()
        if u and not any(x["username"] == u for x in results["pppoe"]):
            results["pppoe"].append({"username": u, "password": p})

    # Search <ParaName>UserName</ParaName><ParaValue>...</ParaValue>
    pv_pairs = re.findall(r'<ParaName>UserName</ParaName>\s*<ParaValue>([^<]+)</ParaValue>.*?<ParaName>Password</ParaName>\s*<ParaValue>([^<]*)</ParaValue>', text, re.DOTALL)
    for u, p in pv_pairs:
        u, p = u.strip(), p.strip()
        if u and not any(x["username"] == u for x in results["pppoe"]):
            results["pppoe"].append({"username": u, "password": p})

    # 2. Search WiFi SSIDs & Passwords
    wifi_matches = re.finditer(r'<DM\s+name=[\'"]ESSID[\'"]\s+value=[\'"]([^\'"]+)[\'"]\s*/>.*?<DM\s+name=[\'"]KeyPassphrase[\'"]\s+value=[\'"]([^\'"]*)[\'"]\s*/>', text, re.DOTALL | re.I)
    for m in wifi_matches:
        ssid, pwd = m.group(1).strip(), m.group(2).strip()
        if ssid:
            results["wifi"].append({"ssid": ssid, "password": pwd})

    wifi_rev = re.finditer(r'<DM\s+name=[\'"]KeyPassphrase[\'"]\s+value=[\'"]([^\'"]*)[\'"]\s*/>.*?<DM\s+name=[\'"]ESSID[\'"]\s+value=[\'"]([^\'"]+)[\'"]\s*/>', text, re.DOTALL | re.I)
    for m in wifi_rev:
        pwd, ssid = m.group(1).strip(), m.group(2).strip()
        if ssid and not any(x["ssid"] == ssid for x in results["wifi"]):
            results["wifi"].append({"ssid": ssid, "password": pwd})

    # Also search PreSharedKey
    psk_matches = re.finditer(r'<DM\s+name=[\'"]ESSID[\'"]\s+value=[\'"]([^\'"]+)[\'"]\s*/>.*?<DM\s+name=[\'"]PreSharedKey[\'"]\s+value=[\'"]([^\'"]*)[\'"]\s*/>', text, re.DOTALL | re.I)
    for m in psk_matches:
        ssid, pwd = m.group(1).strip(), m.group(2).strip()
        if ssid and not any(x["ssid"] == ssid for x in results["wifi"]):
            results["wifi"].append({"ssid": ssid, "password": pwd})

    # 3. Search Admin / Management Logins (DevAuthInfo / UserAccount)
    auth_matches = re.finditer(r'<DM\s+name=[\'"]User[\'"]\s+value=[\'"]([^\'"]+)[\'"]\s*/>.*?<DM\s+name=[\'"]Pass[\'"]\s+value=[\'"]([^\'"]*)[\'"]\s*/>', text, re.DOTALL | re.I)
    for m in auth_matches:
        u, p = m.group(1).strip(), m.group(2).strip()
        if u and not any(x["username"] == u for x in results["admin"]):
            results["admin"].append({"username": u, "password": p})

    return results
