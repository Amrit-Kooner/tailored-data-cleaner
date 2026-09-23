# detectors.py
"""
"What kind of column is this?" -- pure predicates, no side effects, no
logging. Used by steps/dtypes.py (to convert), steps/values.py (phone
formatting) and masks.py (price flagging).
"""

from config import (
    ID_KEYWORDS, PRICE_KEYWORDS, PRICE_COMPOUND_SUFFIXES, NOT_PRICE_TOKENS,
    MEASURE_KEYWORDS, PHONE_NAME_TOKENS, CATEGORY_MAPPINGS, UNIQUE_ID_KEYWORDS,
    FLAG_MIN_MATCH_RATE,
)
from .textutil import name_has_keyword, name_has_word, name_is_keyword, name_tokens


# The words that count as a flag value, lower-cased. Any column whose every
# value is one of these and which has both a true-side and a false-side value
# is a flag column (see looks_like_boolean). Keeping them in one place means
# the detector and the Y/N converter in steps/dtypes.py can never drift apart.
FLAG_TRUE_VALUES = frozenset({"true", "t", "yes", "y", "1", "active", "enabled", "on"})
FLAG_FALSE_VALUES = frozenset({"false", "f", "no", "n", "0", "inactive", "disabled", "off"})
FLAG_ALL_VALUES = FLAG_TRUE_VALUES | FLAG_FALSE_VALUES

# "Status"-style words. In a column whose name has its own category mapping in
# config.CATEGORY_MAPPINGS (e.g. "status": Active / Inactive) these words are a
# STATE, not a yes/no flag, so they are not converted to Y/N there.
FLAG_STATE_WORDS = frozenset({"active", "inactive", "enabled", "disabled", "on", "off"})

# Words that are unmistakably yes/no on their own, so a column holding only one
# side of them (all TRUE, all FALSE, all Yes ...) is still a flag column.
# (1/0 and letters like t/f/y/n need a name hint or both sides to count.)
FLAG_CLEAR_WORDS = frozenset({"true", "false", "yes", "no"})


def flag_words_for(col_name):
    """(true_words, false_words) that count as flag values for THIS column."""
    if any(t in CATEGORY_MAPPINGS for t in name_tokens(col_name)):
        return FLAG_TRUE_VALUES - FLAG_STATE_WORDS, FLAG_FALSE_VALUES - FLAG_STATE_WORDS
    return FLAG_TRUE_VALUES, FLAG_FALSE_VALUES


def flag_keys(series):
    """
    Lower-cased, stripped text of every value, so 'TRUE', ' true ' and True all
    become 'true'. A number that came from a column with blanks ('1.0' / '0.0')
    is turned back into '1' / '0'. Blanks become 'nan' -- callers drop them
    first or ignore them.
    """
    text = series.astype(str).str.strip().str.lower()
    return text.str.replace(r"^([01])\.0+$", r"\1", regex=True)


def _sample(series):
    return series.dropna().astype(str)


def name_has_phone_token(col_name):
    """True when the column NAME contains a phone token as a whole word."""
    return any(t in PHONE_NAME_TOKENS for t in name_tokens(col_name))


def looks_like_price(col_name):
    """
    True when a price word (config.PRICE_KEYWORDS) is a WHOLE WORD in the name
    and no config.NOT_PRICE_TOKENS word sits beside it. "unit_price",
    "net_amount" and "ListPrice" are prices; "total_qty", "cost_centre",
    "pricelist" and "charge_on_finance_charge_flag" are not.

    A percentage is never a price either, even when a price word is also in
    the name ("discount_percentage_charge", "rebate_percent_of_cost"): the
    value is a rate/ratio, not a currency amount.
    """
    tokens = name_tokens(col_name)
    if any(t in NOT_PRICE_TOKENS for t in tokens):
        return False
    # One-word compounds ending in "flag"/"flags" ("priceflag", "chargeflags")
    # are flag columns too, so they can never be prices. Same for a
    # one-word "...percent"/"...percentage" compound ("costpercentage").
    if any(t.endswith(("flag", "flags", "percent", "percents", "percentage", "percentages"))
           for t in tokens):
        return False
    if name_has_word(col_name, PRICE_KEYWORDS):
        return True
    # One-word compounds: "listprice", "netamount", "subtotal", "totalcost".
    return any(t != suffix and t.endswith(suffix)
               for t in tokens for suffix in PRICE_COMPOUND_SUFFIXES)


def looks_like_measure(col_name):
    """
    True for a measurement column by name (config.MEASURE_KEYWORDS: height,
    weight, quantity, ...). A price never counts as a measurement.
    """
    return not looks_like_price(col_name) and name_has_word(col_name, MEASURE_KEYWORDS)


def looks_like_id(col_name, series, sample=None):
    """
    BUG FIX (kept from the original): a column whose NAME explicitly
    identifies it as a phone field is short-circuited to False before the
    ID_KEYWORDS check. Without this, "phone_number" matched ID_KEYWORDS via
    the shared "number" token, was classified as a text ID, and the dedicated
    phone branch was unreachable.
    """
    if name_has_phone_token(col_name):
        return False

    # A flag column is checked BEFORE the ID keywords: "app_approved",
    # "bpa_active", "id_verified" or "has_code" contain an ID word but hold
    # yes/no values, and used to be treated as text IDs and never standardised.
    # (Real unique-ID columns -- app numbers -- are excluded inside
    # looks_like_boolean.)
    if looks_like_boolean(col_name, series, sample):
        return False

    if name_has_keyword(col_name, ID_KEYWORDS):
        return True
    if looks_like_price(col_name) or looks_like_measure(col_name):
        return False
    col_lower = str(col_name).lower()
    if "date" in col_lower or col_lower.endswith("_dt"):
        return False
    if looks_like_currency(series, sample) or looks_like_percentage(series, sample):
        return False
    if looks_like_email(series, sample):
        return False
    if sample is None:
        sample = _sample(series)
    if len(sample) == 0:
        return False
    has_leading_zero = sample.str.match(r"^0\d").any()
    non_null_count = len(sample)
    uniqueness = series.nunique() / non_null_count if non_null_count > 0 else 0
    return has_leading_zero or uniqueness > 0.95


def looks_like_currency(series, sample=None):
    if sample is None:
        sample = _sample(series)
    if len(sample) == 0:
        return False
    return sample.str.contains(r"[£$€]", regex=True).mean() >= 0.3


def looks_like_percentage(series, sample=None):
    if sample is None:
        sample = _sample(series)
    if len(sample) == 0:
        return False
    return sample.str.contains(r"%", regex=False).mean() >= 0.3


def looks_like_boolean(col_name, series, sample=None):
    """
    True for a column whose values are flag-like. The output of these columns
    is standardised to Y/N by steps/dtypes.py.

    Detection is broad, but never guesses. A column is a flag column when
    (values are compared lower-cased and stripped; blanks are ignored):

      1. ALL its values are known flag words (true/false, t/f, yes/no, y/n,
         on/off, active/inactive, enabled/disabled, 1/0) and BOTH a yes-side
         and a no-side word are present -- any mix of them in one column;
      2. ALL its values are unmistakable yes/no words but only ONE side
         appears (a column that is entirely TRUE, entirely FALSE, entirely
         "Yes" ...), or it is one-sided and the name hints it is a flag;
      3. the name hints it is a flag (is_ / has_ prefix, or a whole token
         flag / enabled / active / boolean / bool / yn) and every value is 1
         or 0 (the bare {1, 0} pair is otherwise just numbers);
      4. at least config.FLAG_MIN_MATCH_RATE of the values are flag words,
         both sides are present, and the rest are stray values ("maybe"):
         the flag words are converted and each stray value is kept and
         flagged RED (see steps/dtypes.py).

    Never a flag column: an exact unique-ID column (app numbers), and --
    for the state words active / inactive / on / off / enabled / disabled -- a
    column with its own mapping in config.CATEGORY_MAPPINGS such as "status".

    A number that came through a column with blanks ("1.0" / "0.0") counts as
    1 / 0. The name hint uses WHOLE-TOKEN matching, not substring, so a column
    called "issue" or "deactivated" holding {yes, no} / {1, 0} is not silently
    converted.
    """
    if name_is_keyword(col_name, UNIQUE_ID_KEYWORDS):
        return False

    if sample is None:
        sample = series.dropna()
    sample = flag_keys(sample)
    if len(sample) == 0:
        return False

    unique_vals = set(sample.unique())
    true_words, false_words = flag_words_for(col_name)
    all_words = true_words | false_words
    has_true = bool(unique_vals & true_words)
    has_false = bool(unique_vals & false_words)

    tokens = name_tokens(str(col_name).lower())
    name_lower = str(col_name).lower()
    flag_tokens = {"flag", "enabled", "active", "boolean", "bool", "yn"}
    name_hint = (
        name_lower.startswith("is_")
        or name_lower.startswith("has_")
        or any(t in flag_tokens for t in tokens)
    )

    # Rule 1: only flag words, both sides present. {1, 0} on its own is left to Rule 3.
    if unique_vals <= all_words and unique_vals != {"1", "0"} and has_true and has_false:
        return True

    # Rule 2: only one side, but unmistakable words (or a flag-like name).
    if unique_vals <= all_words and unique_vals != {"1", "0"} and (has_true or has_false):
        if unique_vals <= FLAG_CLEAR_WORDS or (name_hint and not unique_vals <= {"1", "0"}):
            return True

    # Rule 3: name hint required for numbers -- covers {1, 0}, only 1s, only 0s.
    if name_hint and unique_vals <= {"1", "0"}:
        return True

    # Rule 4: mostly flag words plus a few strays (the strays stay and go RED).
    # 1 / 0 are numbers, not evidence of a flag, so they don't count here.
    words_only = all_words - {"1", "0"}
    known = sample.isin(words_only)
    if known.mean() >= FLAG_MIN_MATCH_RATE and not known.all():
        present = set(sample[known])
        if (present & true_words) and (present & false_words):
            return True

    return False


def looks_like_email(series, sample=None):
    if sample is None:
        sample = _sample(series)
    if len(sample) == 0:
        return False
    return sample.str.contains(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", regex=True).mean() >= 0.5


def looks_like_phone(col_name, series, sample=None):
    """
    BUG FIX (kept from the original): the name check is whole-token, so
    "hotel_name" is no longer classified as a phone column because "tel" sits
    inside "hotel". Uses the same PHONE_NAME_TOKENS list as
    steps/values.format_phone_numbers, so the two stay consistent.
    """
    if name_has_phone_token(col_name):
        return True
    if looks_like_price(col_name) or looks_like_measure(col_name):
        return False
    if sample is None:
        sample = _sample(series)
    if len(sample) == 0:
        return False
    pattern = r"^[\d\s\-\(\)\.\+]{7,}$"
    return sample.str.match(pattern).mean() >= 0.5


def looks_like_category(series, max_unique_ratio=0.2):
    non_null = series.dropna()
    if len(non_null) == 0:
        return False
    unique_ratio = non_null.nunique() / len(non_null)
    return unique_ratio <= max_unique_ratio and non_null.nunique() > 1