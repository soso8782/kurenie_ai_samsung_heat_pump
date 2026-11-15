"""Web Push (RFC 8030 / 8291 aes128gcm / 8292 VAPID) bez externych sluzieb.

Zavislost: python3-cryptography. Ziadne tajomstva sa nelogujú ani nevracajú
v texte chyby (endpoint, kluce).
"""
import base64
import json
import os
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

RS = 4096
MAX_PAYLOAD = RS - 86 - 16 - 1  # hlavicka 86 B, GCM tag 16 B, delimiter 1 B = 3993


def b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(s) -> bytes:
    if isinstance(s, bytes):
        s = s.decode("ascii")
    s = s.strip()
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _pub_raw(pub: ec.EllipticCurvePublicKey) -> bytes:
    return pub.public_bytes(serialization.Encoding.X962,
                            serialization.PublicFormat.UncompressedPoint)


def _load_private(raw32: bytes) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(raw32, "big"), ec.SECP256R1())


def _load_public(raw65: bytes) -> ec.EllipticCurvePublicKey:
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw65)


def generate_vapid():
    """-> (private_b64url [32 B raw], public_b64url [65 B uncompressed])."""
    key = ec.generate_private_key(ec.SECP256R1())
    priv = key.private_numbers().private_value.to_bytes(32, "big")
    return b64u_encode(priv), b64u_encode(_pub_raw(key.public_key()))


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt_aes128gcm(plaintext: bytes, ua_public: bytes, auth_secret: bytes,
                      as_private=None, salt=None) -> bytes:
    """RFC 8291 aes128gcm telo spravy (hlavicka + jediny zaznam).

    ua_public: 65 B uncompressed p256dh; auth_secret: 16 B; as_private: 32 B raw
    (None = nahodny efemerny kluc); salt: 16 B (None = nahodny).
    """
    if len(plaintext) > MAX_PAYLOAD:
        raise ValueError("payload je dlhsi ako %d bajtov" % MAX_PAYLOAD)
    if salt is None:
        salt = os.urandom(16)
    if len(salt) != 16:
        raise ValueError("salt musi mat 16 bajtov")
    if as_private is None:
        as_key = ec.generate_private_key(ec.SECP256R1())
    else:
        as_key = _load_private(as_private)
    as_public = _pub_raw(as_key.public_key())
    ecdh = as_key.exchange(ec.ECDH(), _load_public(ua_public))

    key_info = b"WebPush: info\x00" + ua_public + as_public
    ikm = _hkdf(auth_secret, ecdh, key_info, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)

    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    header = salt + struct.pack(">IB", RS, len(as_public)) + as_public
    return header + ciphertext


def _vapid_jwt(aud: str, subject: str, private_b64url: str, ttl_s: int = 12 * 3600 - 60) -> str:
    header = b64u_encode(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    claims = b64u_encode(json.dumps(
        {"aud": aud, "exp": int(time.time()) + ttl_s, "sub": subject},
        separators=(",", ":")).encode())
    signing_input = (header + "." + claims).encode("ascii")
    key = _load_private(b64u_decode(private_b64url))
    der = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")  # raw r||s, nie DER
    return header + "." + claims + "." + b64u_encode(sig)


def send(subscription, payload: bytes, vapid_private_b64url, vapid_public_b64url,
         subject, ttl=3600, urgency="normal", topic=None, timeout=10):
    """Posle push. -> (status, None|text_chyby). 201 = ok, 404/410 = predplatne zaniklo,
    0 = siet/vstup. Nikdy nevyhadzuje vynimku."""
    try:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if len(payload) > MAX_PAYLOAD:
            return 0, "payload je prilis dlhy (max %d bajtov)" % MAX_PAYLOAD
        endpoint = subscription["endpoint"]
        keys = subscription["keys"]
        ua_public = b64u_decode(keys["p256dh"])
        auth_secret = b64u_decode(keys["auth"])
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            return 0, "neplatny endpoint"
        aud = "%s://%s" % (parsed.scheme, parsed.netloc)
        body = encrypt_aes128gcm(payload, ua_public, auth_secret)
        jwt = _vapid_jwt(aud, subject, vapid_private_b64url)
        headers = {
            "Authorization": "vapid t=%s, k=%s" % (jwt, vapid_public_b64url),
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
            "TTL": str(int(ttl)),
            "Urgency": urgency,
        }
        if topic:
            headers["Topic"] = str(topic)
        req = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = resp.getcode()
        except urllib.error.HTTPError as e:
            code = e.code
        if code == 201:
            return 201, None
        if code in (404, 410):
            return code, "predplatne zaniklo"
        if 200 <= code < 300:
            return code, None
        return code, "chyba push sluzby (HTTP %d)" % code
    except Exception as e:  # noqa: BLE001 - nikdy nevyhadzovat
        # len typ vynimky, aby sa neunikol endpoint/kluce
        return 0, "chyba: %s" % type(e).__name__
