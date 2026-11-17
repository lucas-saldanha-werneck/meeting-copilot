"""A local certificate authority for the copilot's HTTPS page on the home network, made with `openssl`.

Why: a phone's camera (getUserMedia) only works on a secure page, and a page at http://copilot.local is not
one. So the copilot serves https://copilot.local with a certificate from a small CA of its own that you
install on the phone once.

Safety, because a CA trusted on a phone could sign a certificate for any site:
  1. The CA carries *critical* Name Constraints: only the one LAN name (copilot.local) and only private
     address ranges (192.168/16, 10/8, 172.16/12). A certificate it signs for any other name is refused by
     a client that enforces constraints (openssl and Apple's Security framework do; checked in the tests).
     Some old clients ignored them, so this is the second layer, not the first.
  2. The first layer: the CA's private key never outlives the signing. `issue()` makes the CA, signs the
     server certificate, overwrites and deletes the CA key. Nothing is left that could sign anything else.
     A renewal makes a new CA, and the phones install the new one.
  3. The server key is mode 0600 in the data folder (excluded from Time Machine on macOS).

Files in the folder: ca.crt (public), server.key (0600), server.crt, ca.mobileconfig (public, for iOS).
"""
import base64
import datetime as dt
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

PRIVATE_NETS = (("192.168.0.0", "255.255.0.0"), ("10.0.0.0", "255.0.0.0"), ("172.16.0.0", "255.240.0.0"))
CA_DAYS = 3650
SERVER_DAYS = 825          # iOS and macOS refuse a longer-lived server certificate (Apple, HT211025)
RENEW_WITHIN_DAYS = 30     # `status` and the start note warn this long before the server certificate ends
FILES = ("ca.crt", "server.key", "server.crt", "ca.mobileconfig")


class CertError(Exception):
    pass


def _run(openssl: str, *args: str, cwd: Path) -> str:
    try:
        p = subprocess.run([openssl, *args], cwd=str(cwd), capture_output=True, text=True, timeout=60,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as ex:
        raise CertError(f"openssl could not run: {ex}") from None
    if p.returncode != 0:
        raise CertError(f"openssl {args[0]} failed: {(p.stderr or p.stdout).strip()[-300:]}")
    return p.stdout


def _check_name(name: str) -> None:
    if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.local", name):
        raise CertError(f"not a .local name: {name!r}")


def ca_config(name: str) -> str:
    # nameConstraints = critical, one permitted DNS name and the private address ranges
    nc = ",".join([f"permitted;DNS:{name}", *(f"permitted;IP:{a}/{m}" for a, m in PRIVATE_NETS)])
    return f"""[req]
distinguished_name = dn
x509_extensions = v3_ca
prompt = no
[dn]
CN = Meeting Copilot local CA ({name})
[v3_ca]
basicConstraints = critical,CA:TRUE,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash
nameConstraints = critical,{nc}
"""


def server_config(names: list[str], ips: list[str]) -> str:
    alt = [f"DNS:{n}" for n in names] + [f"IP:{i}" for i in ips]
    return f"""[req]
distinguished_name = dn
req_extensions = v3_req
prompt = no
[dn]
CN = {names[0]}
[v3_req]
subjectAltName = {",".join(alt)}
[v3_srv]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = {",".join(alt)}
authorityKeyIdentifier = keyid
"""


def _write(path: Path, text: str, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def make_ca(d: Path, name: str, openssl: str) -> None:
    """ca.key (0600) and ca.crt, valid CA_DAYS, constrained to `name` and the private ranges."""
    _check_name(name)
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    cfg = d / "ca.cnf"
    _write(cfg, ca_config(name))
    old = os.umask(0o077)
    try:
        _run(openssl, "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", "ca.key", cwd=d)
        _run(openssl, "req", "-x509", "-new", "-key", "ca.key", "-sha256", "-days", str(CA_DAYS), "-config", "ca.cnf",
             "-out", "ca.crt", cwd=d)
    finally:
        os.umask(old)
        cfg.unlink(missing_ok=True)
    os.chmod(d / "ca.key", 0o600)


def sign_server(d: Path, names: list[str], ips: list[str], openssl: str, days: int = SERVER_DAYS) -> None:
    """server.key (0600) and server.crt signed by ca.key, for `names` (+ `ips` as extra names)."""
    cfg = d / "server.cnf"
    _write(cfg, server_config(names, ips))
    old = os.umask(0o077)
    try:
        _run(openssl, "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", "server.key", cwd=d)
        _run(openssl, "req", "-new", "-key", "server.key", "-config", "server.cnf", "-out", "server.csr", cwd=d)
        _run(openssl, "x509", "-req", "-in", "server.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial",
             "-days", str(days), "-sha256", "-extfile", "server.cnf", "-extensions", "v3_srv", "-out", "server.crt", cwd=d)
    finally:
        os.umask(old)
        for n in ("server.cnf", "server.csr", "ca.srl"):
            (d / n).unlink(missing_ok=True)
    os.chmod(d / "server.key", 0o600)


def wipe(path: Path) -> None:
    """Overwrites a file with zeros, then deletes it. (On an SSD the overwrite is not a guarantee, which is
    why the key is also never written anywhere but this folder.)"""
    try:
        n = path.stat().st_size
        with open(path, "r+b") as f:
            f.write(b"\0" * n)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
    path.unlink(missing_ok=True)


def mobileconfig(ca_pem: str, name: str) -> bytes:
    """An iOS profile that installs the CA certificate (unsigned: iOS says "Not Signed", which is expected)."""
    der = base64.b64decode("".join(l for l in ca_pem.splitlines() if "-----" not in l))
    ident = f"local.meeting-copilot.{name}"
    payload = {
        "PayloadContent": [{
            "PayloadType": "com.apple.security.root", "PayloadVersion": 1,
            "PayloadIdentifier": ident + ".ca", "PayloadUUID": str(uuid.uuid4()).upper(),
            "PayloadDisplayName": f"Meeting Copilot CA ({name})", "PayloadContent": der,
        }],
        "PayloadType": "Configuration", "PayloadVersion": 1, "PayloadIdentifier": ident,
        "PayloadUUID": str(uuid.uuid4()).upper(), "PayloadDisplayName": f"Meeting Copilot ({name})",
        "PayloadDescription": f"Trusts a certificate authority that can only vouch for {name} and home "
                              f"network addresses (192.168.x.x, 10.x.x.x, 172.16-31.x.x).",
        "PayloadOrganization": "meeting-copilot",
    }
    return plistlib.dumps(payload)


def verify(d: Path, openssl: str, cert: str = "server.crt", name: str | None = None) -> bool:
    """Whether `cert` verifies against ca.crt (for `name` when given)."""
    args = ["verify", "-CAfile", "ca.crt"]
    try:
        _run(openssl, *args, *(["-verify_hostname", name] if name else []), cert, cwd=d)
        return True
    except CertError as ex:
        if name and "usage" in str(ex).lower():     # LibreSSL (macOS /usr/bin/openssl) has no -verify_hostname
            return verify(d, openssl, cert)
        return False


def issue(d: Path, name: str, ips: list[str], openssl: str, exclude=None) -> None:
    """A new CA and a server certificate for `name` (+ `ips`), then the CA key is gone. Replaces what is in
    `d`. Phones that trusted the old CA must install the new one."""
    _check_name(name)
    tmp = Path(tempfile.mkdtemp(prefix=".issue-", dir=str(d.parent)))
    os.chmod(tmp, 0o700)
    try:
        make_ca(tmp, name, openssl)
        try:
            sign_server(tmp, [name], ips, openssl)
            if not verify(tmp, openssl):
                raise CertError("the new server certificate does not verify against the new CA")
        finally:
            wipe(tmp / "ca.key")                     # whatever happened above: no CA key is left
        pem = (tmp / "ca.crt").read_text()
        (tmp / "ca.mobileconfig").write_bytes(mobileconfig(pem, name))
        os.chmod(tmp / "ca.mobileconfig", 0o644)
        os.chmod(tmp / "ca.crt", 0o644)
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(d, 0o700)
        for n in FILES:
            os.replace(tmp / n, d / n)
        if exclude:
            exclude(d)                               # Time Machine: the server key stays off the backups
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for n in ("ca.key", "ca.srl"):
        assert not (d / n).exists()


def cert_info(d: Path, openssl: str) -> dict | None:
    """{"expires": datetime, "names": [...]} of the server certificate, or None when there is none."""
    crt = d / "server.crt"
    if not crt.is_file() or not (d / "server.key").is_file() or not (d / "ca.crt").is_file():
        return None
    try:
        out = _run(openssl, "x509", "-in", "server.crt", "-noout", "-enddate", "-text", cwd=d)
    except CertError:
        return None
    m = re.search(r"notAfter=(.+)", out)
    if not m:
        return None
    try:
        exp = dt.datetime.strptime(m.group(1).strip().replace("  ", " "), "%b %d %H:%M:%S %Y %Z")
    except ValueError:
        return None
    return {"expires": exp, "names": re.findall(r"DNS:([^,\s]+)", out)}


def days_left(info: dict) -> int:
    return (info["expires"] - dt.datetime.utcnow()).days
