"""Secret-based login. Credentials never have repository defaults."""
import hashlib
import hmac

def verify_password(entered, expected):
    return bool(expected) and hmac.compare_digest(str(entered).encode("utf-8"),str(expected).encode("utf-8"))

def credential_version(admin, member):
    # Server-side session marker only, never display or persist publicly.
    return hmac.new(str(admin).encode(),str(member).encode(),hashlib.sha256).hexdigest()
