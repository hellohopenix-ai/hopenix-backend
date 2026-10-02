"""Real two-factor authentication (TOTP — Google Authenticator, Microsoft
Authenticator, Authy, 1Password...) for the Settings -> Security tab.

Flow
  setup   -> a fresh secret is created and kept as "pending"; the QR code and
             key are shown once.
  enable  -> the user types the 6-digit code their app shows. Only then does the
             secret become active, two_factor_enabled flips on, and 8 one-time
             backup codes are issued (shown once, stored only as hashes).
  login   -> users/views.py asks for the code after the password (and after
             Google sign-in) whenever requires_otp(user) is true.
  disable -> needs the account password AND a valid code (or backup code).

The secret is encrypted at rest with a key derived from SECRET_KEY, so a
database dump alone can't be used to generate codes.
"""
import base64
import hashlib
import hmac
import re
import secrets
import time

import pyotp
import segno
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.utils import timezone

ISSUER = "Hopenix"
STEP = 30
BACKUP_CODE_COUNT = 8


def _fernet():
    key = base64.urlsafe_b64encode(hashlib.sha256(f"hopenix-totp|{settings.SECRET_KEY}".encode()).digest())
    return Fernet(key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        return ""


def new_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


def qr_data_uri(uri: str) -> str:
    """SVG data-URI of the QR code, generated here so the browser needs no QR library."""
    return segno.make(uri, error="m").svg_data_uri(scale=6, border=2, dark="#111111", light="#ffffff")


def _clean(code) -> str:
    return re.sub(r"[\s-]", "", str(code or ""))


def verify_totp(secret: str, code, last_step: int = 0):
    """Return the accepted time step (int) or None. Allows one step of clock
    drift either side and refuses a step that was already used."""
    code = _clean(code)
    if not secret or not re.fullmatch(r"\d{6}", code):
        return None
    totp = pyotp.TOTP(secret, interval=STEP)
    now_step = int(time.time() // STEP)
    for step in (now_step, now_step - 1, now_step + 1):
        if step <= last_step:
            continue
        if hmac.compare_digest(totp.at(step * STEP), code):
            return step
    return None


def _hash_backup(code: str) -> str:
    return hashlib.sha256(f"hopenix-backup|{_clean(code).lower()}".encode()).hexdigest()


def make_backup_codes():
    """-> (plain codes to show once, hashes to store)"""
    plain = []
    for _ in range(BACKUP_CODE_COUNT):
        raw = secrets.token_hex(5)  # 10 hex chars
        plain.append(f"{raw[:5]}-{raw[5:]}")
    return plain, [_hash_backup(c) for c in plain]


def get_setting(user):
    from .models import SecuritySetting

    obj, _ = SecuritySetting.objects.get_or_create(user=user)
    return obj


def requires_otp(user) -> bool:
    try:
        from .models import SecuritySetting

        sec = SecuritySetting.objects.filter(user=user).first()
        return bool(sec and sec.two_factor_enabled and sec.totp_secret)
    except Exception:  # noqa: BLE001 — never block a login because of a lookup problem
        return False


def check_login_code(user, code) -> bool:
    """True if `code` is a valid authenticator code or an unused backup code
    for this user. Consumes the step / backup code so it can't be reused."""
    sec = get_setting(user)
    secret = decrypt(sec.totp_secret)
    step = verify_totp(secret, code, sec.totp_last_step)
    if step is not None:
        sec.totp_last_step = step
        sec.save(update_fields=["totp_last_step", "updated_at"])
        return True
    h = _hash_backup(code)
    if h in (sec.backup_codes or []):
        sec.backup_codes = [c for c in sec.backup_codes if c != h]
        sec.save(update_fields=["backup_codes", "updated_at"])
        return True
    return False


def begin_setup(user):
    sec = get_setting(user)
    secret = new_secret()
    sec.totp_pending_secret = encrypt(secret)
    sec.save(update_fields=["totp_pending_secret", "updated_at"])
    uri = provisioning_uri(secret, user.email)
    return {"secret": secret, "uri": uri, "qr": qr_data_uri(uri)}


def confirm_setup(user, code):
    """-> backup codes (list) on success, None if the code was wrong."""
    sec = get_setting(user)
    secret = decrypt(sec.totp_pending_secret)
    step = verify_totp(secret, code, 0)
    if step is None:
        return None
    plain, hashes = make_backup_codes()
    sec.totp_secret = encrypt(secret)
    sec.totp_pending_secret = ""
    sec.two_factor_enabled = True
    sec.two_factor_enabled_at = timezone.now()
    sec.totp_last_step = step
    sec.backup_codes = hashes
    sec.save()
    return plain


def disable(user):
    sec = get_setting(user)
    sec.totp_secret = ""
    sec.totp_pending_secret = ""
    sec.two_factor_enabled = False
    sec.two_factor_enabled_at = None
    sec.backup_codes = []
    sec.totp_last_step = 0
    sec.save()
