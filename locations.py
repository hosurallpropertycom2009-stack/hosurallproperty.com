"""
Single source of truth for the places Hosur All Property serves.

The order of LOCATIONS is the order shown on every page (home map, footers,
filters, post-ad dropdowns). Older listings may have been saved with an
older spelling (e.g. "Bangalore", "Soolagiri", "Denkani Kotta"); ALIASES lets
the filters and the AI assistant still find them under the current name.
"""
import re
from typing import List

LOCATIONS: List[str] = [
    "Hosur",
    "Bagalur",
    "Shoolagiri",
    "Rayakottai",
    "Denkanikotta",
    "Thally",
    "Vepanapalli",
    "Marandahalli",
    "Krishnagiri",
    "Bengaluru",
    "Yercaud",
    "Chennai",
    "Ooty",
    "Kodaikanal",
    "Salem",
    "Coimbatore",
]

# canonical name -> other spellings seen in older data
ALIASES = {
    "Bagalur": ["Baagalur", "Bagaluru", "Baagaluru"],
    "Shoolagiri": ["Soolagiri", "Sulagiri", "Shulagiri"],
    "Rayakottai": ["Rayakotta", "Rayakottah", "Rayakkottai"],
    "Denkanikotta": ["Denkani Kotta", "Denkanikottai", "Denkani Kottai", "Denkanikottah"],
    "Bengaluru": ["Bangalore", "Bengalore", "Bangaluru"],
}


def _key(value: str) -> str:
    """Lowercase letters only, so 'Denkani Kotta' == 'denkanikotta'."""
    return re.sub(r"[^a-z]", "", (value or "").lower())


_KEY_TO_CANON = {_key(name): name for name in LOCATIONS}
for _canon, _alts in ALIASES.items():
    for _alt in _alts:
        _KEY_TO_CANON[_key(_alt)] = _canon


def canonical_location(value: str) -> str:
    """Return the current spelling for a known place; unknown places are
    returned trimmed but otherwise unchanged."""
    value = (value or "").strip()
    return _KEY_TO_CANON.get(_key(value), value)


def location_variants(value: str) -> List[str]:
    """All lowercase spellings that should match `value` in a filter."""
    canon = canonical_location(value)
    names = [canon] + ALIASES.get(canon, [])
    if value and value.strip():
        names.append(value.strip())
    return sorted({n.lower() for n in names})


def location_sort_key(value: str):
    """Sort key that follows LOCATIONS order; unknown places go last."""
    canon = canonical_location(value)
    try:
        return (0, LOCATIONS.index(canon), canon.lower())
    except ValueError:
        return (1, 0, canon.lower())
