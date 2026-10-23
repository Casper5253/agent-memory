"""Secret and sensitive-data filtering. Applied before any model call and before any vault write."""
import math
import re
from dataclasses import dataclass
from typing import Literal

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
    (?P<val>\[REDACTED\]|"[^"\n]{4,}"|'[^'\n]{4,}'|[^\s,;}\]]{6,})
    """
)
_URL_CREDS = re.compile(r"(?P<scheme>[a-z][a-z0-9+.\-]*://)[^\s:/@]+:[^\s@/]+@", re.I)
_CANDIDATE = re.compile(r"[A-Za-z0-9+/_\-]{32,}={0,2}")
_UUID = re.compile(r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$", re.I)
_HEX = re.compile(r"^[0-9a-f]+$", re.I)

SensitiveCategory = Literal["secret", "iban", "payment_card", "bsn", "phone", "email"]


@dataclass(frozen=True)
class Finding:
    category: SensitiveCategory
    start: int
    end: int


_IBAN = re.compile(r"(?<![A-Z0-9])[A-Z]{2}\d{2}(?:[ -]?[A-Z0-9]){11,30}(?![A-Z0-9])", re.I)
_CARD = re.compile(r"(?<![A-Z0-9])(?:\d[ -]?){12,18}\d(?![A-Z0-9])", re.I)
_BSN = re.compile(r"(?<![A-Z0-9.\-])[1-9]\d{8}(?![A-Z0-9.\-])", re.I)
_PHONE = re.compile(r"(?<![\w+])\+(?:\d[ -]?){7,14}\d(?!\d)")
_NO_REPLY = re.compile(r"(?:(?:no-?reply|donotreply|do-not-reply)(?:[+.][^@]*)?@|[^@]*@(?:[^@]*\.)?noreply\.)", re.I)
_EMAIL = re.compile(r"(?<![\w.+/@-])[A-Z0-9][A-Z0-9.!#$%&'*+/=?^_`{|}~-]*@[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)*\.[A-Z]{2,}(?![A-Z])", re.I)
_BSN_KEYWORD = re.compile(r"\b(?:bsn|burgerservicenummer|sofi)\b", re.I)
_IBAN_LENGTHS = {"NL": 18, "DE": 22, "GB": 22, "BE": 16, "FR": 27}
_FRONTMATTER_SESSION = re.compile(
    r"(?m)^[ \t]*(?P<key>[A-Za-z0-9_.-]*session_id)\s*:\s*(?P<value>\S+)[^\r\n]*$", re.I)
_PHONE_CODES = frozenset((
    "1", "7", "20", "27", "30", "31", "32", "33", "34", "36", "39", "40", "41", "43", "44", "45", "46", "47", "48", "49",
    "51", "52", "53", "54", "55", "56", "57", "58", "60", "61", "62", "63", "64", "65", "66", "81", "82", "84", "86", "90", "91", "92", "93", "94", "95", "98",
    "211", "212", "213", "216", "218", "220", "221", "222", "223", "224", "225", "226", "227", "228", "229", "230", "231", "232", "233", "234", "235", "236", "237", "238", "239", "240", "241", "242", "243", "244", "245", "246", "248", "249",
    "250", "251", "252", "253", "254", "255", "256", "257", "258", "260", "261", "262", "263", "264", "265", "266", "267", "268", "269", "290", "291", "297", "298", "299",
    "350", "351", "352", "353", "354", "355", "356", "357", "358", "359", "370", "371", "372", "373", "374", "375", "376", "377", "378", "380", "381", "382", "383", "385", "386", "387", "389",
    "420", "421", "423", "500", "501", "502", "503", "504", "505", "506", "507", "508", "509", "590", "591", "592", "593", "594", "595", "596", "597", "598", "599",
    "670", "672", "673", "674", "675", "676", "677", "678", "679", "680", "681", "682", "683", "685", "686", "687", "688", "689", "690", "691", "692", "850", "852", "853", "855", "856", "880", "886", "960", "961", "962", "963", "964", "965", "966", "967", "968", "970", "971", "972", "973", "974", "975", "976", "977", "992", "993", "994", "995", "996", "998",
))


def _valid_iban(value: str) -> bool:
    compact = re.sub(r"[ -]", "", value).upper()
    if len(compact) != _IBAN_LENGTHS.get(compact[:2]) or not compact[2:4].isdigit():
        return False
    remainder = 0
    for char in compact[4:] + compact[:4]:
        digits = str(ord(char) - 55) if char.isalpha() else char
        for digit in digits:
            remainder = (remainder * 10 + int(digit)) % 97
    return remainder == 1


def _valid_bsn(value: str) -> bool:
    digits = [int(char) for char in value]
    return len(digits) == 9 and (9 * digits[0] + 8 * digits[1] + 7 * digits[2] + 6 * digits[3]
            + 5 * digits[4] + 4 * digits[5] + 3 * digits[6] + 2 * digits[7] - digits[8]) % 11 == 0


def _is_frontmatter_session_id(text: str, match: re.Match) -> bool:
    start = match.start("val") if "val" in match.re.groupindex else match.start()
    end = match.end("val") if "val" in match.re.groupindex else match.end()
    return any(start < identity_end and identity_start < end
               for identity_start, identity_end in _frontmatter_session_spans(text))


def _frontmatter_session_spans(text: str) -> list[tuple[int, int]]:
    if not text.startswith("---\n"):
        return []
    header_end = text.find("\n---\n", 4)
    if header_end < 0:
        return []
    return [(match.start("value"), match.end("value")) for match in _FRONTMATTER_SESSION.finditer(text)
            if match.start() < header_end and match.group("key").casefold().endswith("session_id")
            and match.group("value").strip("\"'") != MASK]


def _is_masked_assignment(match: re.Match) -> bool:
    return match.group("val").strip("\"'") == MASK


def _has_bsn_keyword(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 30):start]
    after = text[end:min(len(text), end + 30)]
    return bool(_BSN_KEYWORD.search(before) or _BSN_KEYWORD.search(after))


def _valid_phone(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    return 8 <= len(digits) <= 15 and digits[0] != "0" and any(digits.startswith(code) for code in _PHONE_CODES)


def _email_allowlist(value):
    return {str(address).strip().casefold() for address in (value or ())}


def findings(text: str, email_allowlist=None) -> list[Finding]:
    """Return non-overlapping sensitive spans without copying their values into the result."""
    candidates = []
    identity_spans = _frontmatter_session_spans(text)

    def is_identity(start, end):
        return any(start < identity_end and identity_start < end for identity_start, identity_end in identity_spans)

    for pattern in _PATTERNS:
        candidates.extend(Finding("secret", match.start(), match.end()) for match in pattern.finditer(text)
                          if not is_identity(match.start(), match.end()))
    candidates.extend(Finding("secret", match.start("val"), match.end("val")) for match in _ASSIGN.finditer(text)
                      if not _is_masked_assignment(match) and not _is_frontmatter_session_id(text, match))
    candidates.extend(Finding("secret", match.start(), match.end()) for match in _URL_CREDS.finditer(text)
                      if not is_identity(match.start(), match.end()))
    candidates.extend(Finding("secret", match.start(), match.end()) for match in _CANDIDATE.finditer(text)
                      if _high_entropy(match) == MASK and not is_identity(match.start(), match.end()))
    for category, pattern, validator in (
        ("iban", _IBAN, _valid_iban),
        ("payment_card", _CARD, lambda value: _luhn(re.sub(r"[ -]", "", value))),
        ("bsn", _BSN, lambda value: _valid_bsn(value)),
        ("phone", _PHONE, _valid_phone),
    ):
        candidates.extend(Finding(category, match.start(), match.end()) for match in pattern.finditer(text)
                          if validator(match.group(0)) and
                          (category != "bsn" or _has_bsn_keyword(text, match.start(), match.end())))
    allow = _email_allowlist(email_allowlist)
    candidates.extend(Finding("email", match.start(), match.end()) for match in _EMAIL.finditer(text)
                      if match.group(0).casefold() not in allow and not _NO_REPLY.match(match.group(0)))
    priority = {"secret": 0, "iban": 1, "payment_card": 2, "bsn": 3, "phone": 4, "email": 5}
    selected = []
    for item in sorted(candidates, key=lambda f: (f.start, priority[f.category], -(f.end - f.start))):
        if selected and item.start < selected[-1].end:
            continue
        selected.append(item)
    return selected


def _luhn(value: str) -> bool:
    if not 13 <= len(value) <= 19 or len(set(value)) == 1:
        return False
    total = 0
    for index, char in enumerate(reversed(value)):
        digit = int(char)
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0

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
    if any(_HEX.fullmatch(segment) and 7 <= len(segment) <= 64
           for segment in re.split(r"[\\/._-]", core)):
        return tok  # hashes embedded in paths, names, and session links
    segments = re.split(r"[\\/]", core)
    if len(segments) >= 2 and max(len(s) for s in segments) < 25:
        return tok  # path-like: base64 secrets that contain "/" still have long random segments
    words = [w for w in re.split(r"[-_./\\]", core) if w]
    if len(words) >= 3 and max(len(w) for w in words) < 20:
        return tok  # slug-like (note names, kebab-case ids): secrets have long random runs
    classes = sum(bool(re.search(p, core)) for p in (r"[a-z]", r"[A-Z]", r"\d"))
    if classes >= 3 and _entropy(core) >= 4.0:
        return MASK
    return tok


def redact(text: str, email_allowlist=None) -> str:
    if not text:
        return text
    personal = [item for item in findings(text, email_allowlist) if item.category != "secret"]
    for item in reversed(personal):
        text = text[:item.start] + MASK + text[item.end:]
    for pat in _PATTERNS:
        text = pat.sub(MASK, text)
    text = _URL_CREDS.sub(lambda m: m.group("scheme") + MASK + "@", text)
    def redact_assignment(match):
        if _is_masked_assignment(match) or _is_frontmatter_session_id(text, match):
            return match.group(0)
        value = match.group("val")
        replacement = f"{value[0]}{MASK}{value[0]}" if value[:1] in ('"', "'") else MASK
        return match.group("key") + match.group("sep") + replacement
    text = _ASSIGN.sub(redact_assignment, text)
    text = _CANDIDATE.sub(_high_entropy, text)
    return text


def is_sensitive_path(path: str) -> bool:
    return bool(path) and bool(SENSITIVE_PATH.search(path.strip().strip("'\"")))


def contains_secret(text: str) -> bool:
    """True if redact() would change anything: used by tests and as a last write-time assertion."""
    return redact(text) != text


def redact_session_note(text: str, email_allowlist=None) -> str:
    """Keep only the renderer's own session identity assignment out of free-text filtering."""
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return redact(text, email_allowlist=email_allowlist)
    header, body = text[4:].split("\n---\n", 1)
    lines = header.splitlines()
    identity = [line for line in lines if line.startswith("session_id: ")]
    if len(identity) != 1:
        raise ValueError("capture_identity_conflict: malformed session identity")
    identity_lines = set(identity)
    identity_lines.update(line for line in lines if re.match(
        r"\s*[A-Za-z0-9_.-]*_session_id\s*:\s*\S+\s*$", line, re.I))
    return "---\n" + "\n".join(line if line in identity_lines else redact(line, email_allowlist=email_allowlist)
                                      for line in lines) + "\n---\n" + redact(body, email_allowlist=email_allowlist)
