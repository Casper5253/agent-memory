"""One support check for model statements: the statement must not add specifics its evidence lacks,
and it must share content words with the quote it cites. Paraphrase is allowed; invented specifics are not."""
import re


STOP_WORDS = frozenset("""
a an the and or but if then than so of to in on at by for from with into as via it its this that these those
is are was were be been being has have had do does did not no there their they we you he she i my our your
also just use uses used using""".split())
WORD = re.compile(r"[a-z][a-z0-9]*")
PATH = re.compile(r"(?:[A-Za-z]:)?[\w.~\-]*(?:[\\/][\w.~\-]+)+")
FILE = re.compile(r"[\w\-]+\.[A-Za-z]\w{0,7}")
IDENTIFIER = re.compile(r"\w+(?:(?:::|\.)\w+)*")
VERSION = re.compile(r"\bv?\d+(?:\.\d+)+\b|\bv\d+\b")
HEX = re.compile(r"\b[0-9a-fA-F]{7,}\b")
NUMBER = re.compile(r"\b\d{2,}\b")
SPAN = re.compile(r"`([^`]+)`")


def _normal(value):
    return re.sub(r"\s+", " ", value).strip().lower()


def _specifics(statement):
    found = {_normal(span) for span in SPAN.findall(statement)}
    found |= {_normal(path) for path in PATH.findall(statement)}
    found |= {_normal(name) for name in FILE.findall(statement)}
    found |= {_normal(token) for token in IDENTIFIER.findall(statement)
              if "_" in token or "." in token or "::" in token}
    found |= {_normal(version) for version in VERSION.findall(statement)}
    found |= {_normal(digits) for digits in HEX.findall(statement)}
    found |= {_normal(number) for number in NUMBER.findall(statement)}
    return found


def _present(specific, evidence):
    return re.search(r"(?<![a-z0-9])" + re.escape(specific) + r"(?![a-z0-9])", evidence) is not None


def _stem(word):
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[:-len(suffix)]
    return word


def _content(text):
    return {_stem(word) for word in WORD.findall(text.lower()) if word not in STOP_WORDS}


def grounded(statement, quote, evidence):
    """True when every specific in `statement` appears in `evidence`, and `statement` shares enough
    content words with `quote`: 2, or 1 when the statement has three or fewer content words."""
    evidence = _normal(evidence)
    if not all(_present(specific, evidence) for specific in _specifics(statement)):
        return False
    words = _content(statement)
    shared = words & _content(quote)
    return len(shared) >= (1 if len(words) <= 3 else 2)
