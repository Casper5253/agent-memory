"""Secret and sensitive-data filtering. Applied before any model call and before any vault write."""
import math
import re

MASK = "[REDACTED]"

_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bsk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\bya29\.[0-9A-Za-z_\-]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{16,}=*"),
    re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/]{16,}=*"),
]
# key = value / key: value / "key": "value" for credential-looking keys: keep the key, mask the value.
_ASSIGN = re.compile(
    r"""(?ix)
    (?P<key>["']?[A-Za-z0-9_.\-]*(?:password|passwd|pwd|secret|token|api[_\-]?key|access[_\-]?key|
        private[_\-]?key|client[_\-]?secret|auth(?:orization)?|credential|cookie|session[_\-]?id)["']?)
    (?P<sep>\s*[:=]\s*)
    (?P<val>"[^"\n]{4,}"|'[^'\n]{4,}'|[^\s,;}\]]{6,})
    """
)
_URL_CREDS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.\-]*://)[^\s:/@]+:[^\s@/]+@", re.I)
_CANDIDATE = re.compile(r"[A-Za-z0-9+/_\-]{32,}={0,2}")
_UUID = re.compile(r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$", re.I)
_HEX = re.compile(r"^[0-9a-f]+$", re.I)

# Files whose contents must never be captured, even partially.
SENSITIVE_PATH = re.compile(
    r"(?i)(?:^|[\\/])(?:\.env(?:\.[^\\/]*)?|\.netrc|\.npmrc|\.pypirc|auth\.json|\.credentials\.json|"
    r"credentials(?:\.json)?|id_rsa|id_ed25519|[^\\/]*\.pem|[^\\/]*\.key|[^\\/]*\.dpapi|secrets?\.[a-z]+|"
    r"accounts\.local\.json)$"
)


def _entropy(s: str) -> float:
    counts = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _high_entropy(match: re.Match) -> str:
    tok = match.group(0)
    core = tok.rstrip("=")
    if _UUID.match(core) or (_HEX.match(core) and len(core) <= 64):  # commit hashes, sha256, uuids
        return tok
    segments = re.split(r"[\\/]", core)
    if len(segments) >= 2 and max(len(s) for s in segments) < 25:
        return tok  # path-like: base64 secrets that contain "/" still have long random segments
    classes = sum(bool(re.search(p, core)) for p in (r"[a-z]", r"[A-Z]", r"\d"))
    if classes >= 3 and _entropy(core) >= 4.0:
        return MASK
    return tok


def redact(text: str) -> str:
    if not text:
        return text
    for pat in _PATTERNS:
        text = pat.sub(MASK, text)
    text = _URL_CREDS.sub(lambda m: m.group("scheme") + MASK + "@", text)
    text = _ASSIGN.sub(lambda m: m.group("key") + m.group("sep") + MASK, text)
    text = _CANDIDATE.sub(_high_entropy, text)
    return text


def is_sensitive_path(path: str) -> bool:
    return bool(path) and bool(SENSITIVE_PATH.search(path.strip().strip("'\"")))


def contains_secret(text: str) -> bool:
    """True if redact() would change anything: used by tests and as a last write-time assertion."""
    return redact(text) != text
