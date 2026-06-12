"""
scanner/chains.py — Chain and non-lead business filtering.

Used by reporter.py to exclude businesses that are not useful web design leads:
  - National/regional chains (already have corporate websites)
  - Place-of-worship, schools, government (not target clients)

Matching is case-insensitive. Name matching uses the CHAIN_NAMES set for
exact matches plus CHAIN_SUBSTRINGS for partial/prefix matches.

To include chains in a report, pass --include-chains on the CLI.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# OSM/Google place types that are never web design leads
# ---------------------------------------------------------------------------

SKIP_TYPES: frozenset[str] = frozenset({
    # Religious
    "place_of_worship", "church", "mosque", "synagogue", "temple",
    # Education
    "school", "university", "college", "library", "kindergarten",
    # Government / civic
    "government", "city_hall", "courthouse", "police", "fire_station",
    "post_office", "townhall",
    # Infrastructure
    "parking", "fuel", "charging_station", "bus_station",
    "atm",  # standalone ATMs
})

# ---------------------------------------------------------------------------
# Exact-match chain names (case-insensitive)
# ---------------------------------------------------------------------------

CHAIN_NAMES: frozenset[str] = frozenset({
    # Gas / convenience
    "chevron", "shell", "bp", "exxon", "mobil", "valero", "arco", "76",
    "maverick", "pilot", "flying j", "love's travel stop", "casey's",
    "7-eleven", "circle k", "holiday station store",
    # Banks / financial
    "wells fargo", "chase", "bank of america", "us bank", "citibank",
    "zions bank", "bank of american fork", "td bank", "pnc bank",
    "key bank", "regions bank", "truist", "ally bank", "discover bank",
    "utah community credit union", "america first credit union",
    "mountain america credit union", "goldenwest credit union",
    "deseret first credit union", "wasatch peaks credit union",
    # National fast food
    "mcdonald's", "burger king", "wendy's", "taco bell", "kfc",
    "chick-fil-a", "subway", "domino's", "pizza hut", "little caesars",
    "papa john's", "papa murphy's", "panda express", "chipotle",
    "five guys", "shake shack", "wingstop", "jersey mike's",
    "jimmy john's", "firehouse subs", "arby's", "sonic drive-in", "sonic",
    "dairy queen", "hardee's", "carl's jr.", "jack in the box",
    "popeyes", "culver's", "del taco", "in-n-out burger", "in-n-out",
    "whataburger", "checkers", "rally's", "cook out", "steak 'n shake",
    "wienerschnitzel", "hot dog on a stick",
    # Regional chains (Utah / Florida focus)
    "arctic circle", "jcw's", "crown burger", "costa vida", "cafe rio",
    "swig", "fiiz drinks", "crumbl cookies", "r&r bbq", "zao asian cafe",
    "cubby's", "kneaders bakery", "great harvest bread",
    "barbacoa", "blue lemon", "pizza pie cafe", "roxberry juice",
    "roxberry", "craigo's pizza", "noodles & company", "noodles and company",
    "the crack shack", "porcupine pub", "red robin",
    "dutch bros", "dutch bros coffee", "human bean", "the human bean",
    "black rock coffee", "grounds for coffee",
    "tropical smoothie cafe", "bahama buck's", "bahama bucks",
    "orange julius", "jamba", "jamba juice", "smoothie king",
    "golden corral", "texas roadhouse", "outback steakhouse",
    "applebee's", "chili's", "olive garden", "red lobster",
    "denny's", "ihop", "waffle house", "cracker barrel",
    "panera bread", "panera", "jason's deli", "mcalister's deli",
    "paradise bakery",
    # Coffee
    "starbucks", "dunkin", "dunkin' donuts",
    # Grocery / retail
    "walmart", "walmart supercenter", "target", "costco", "sam's club",
    "kroger", "macey's", "smith's", "harmons", "harmon's", "ridley's",
    "winco", "winco foods", "whole foods", "whole foods market",
    "trader joe's", "sprouts", "natural grocers", "albertsons",
    "safeway", "publix", "h-e-b", "meijer", "hy-vee", "fresh market",
    "aldi", "lidl", "dollar tree", "dollar general", "family dollar",
    "five below", "ross", "ross dress for less", "tj maxx", "marshalls",
    "homegoods", "home goods", "burlington", "burlington coat factory",
    "old navy", "gap", "banana republic", "h&m", "forever 21",
    "american eagle", "hollister", "abercrombie", "victoria's secret",
    "bath & body works", "the body shop", "sephora", "ulta beauty", "ulta",
    "best buy", "apple store", "at&t", "verizon", "t-mobile", "sprint",
    "boost mobile", "metro by t-mobile", "cricket wireless",
    # Home / hardware
    "ace hardware", "home depot", "the home depot", "lowe's", "menards",
    "true value", "do it best",
    # Auto
    "jiffy lube", "firestone", "goodyear", "midas", "meineke",
    "pep boys", "o'reilly auto parts", "autozone", "napa auto parts",
    "advance auto parts", "carmax", "carvana",
    "enterprise rent-a-car", "enterprise", "hertz", "avis", "budget",
    "national car rental", "alamo", "thrifty",
    "les schwab", "discount tire", "big o tires",
    # Shipping / postal
    "the ups store", "ups store", "fedex office", "fedex", "usps",
    "united states post office", "post office",
    # Health / pharmacy
    "walgreens", "cvs", "cvs pharmacy", "rite aid", "walmart pharmacy",
    "instacare", "urgent care",
    # Fitness chains
    "planet fitness", "la fitness", "anytime fitness", "24 hour fitness",
    "gold's gym", "crunch fitness", "equinox", "orangetheory",
    "orangetheory fitness", "f45", "pure barre",
    # Hotels
    "marriott", "hilton", "hyatt", "sheraton", "westin",
    "holiday inn", "best western", "comfort inn", "comfort suites",
    "hampton inn", "courtyard", "fairfield inn", "springhill suites",
    "residence inn", "homewood suites", "extended stay america",
    "days inn", "super 8", "motel 6", "la quinta",
    # Dry cleaning / services
    "red hanger", "martinizing", "tide cleaners", "pressed",
    # Other well-known national services
    "h&r block", "jackson hewitt", "liberty tax",
    "edward jones", "ameriprise", "northwestern mutual",
    "state farm", "allstate", "farmers insurance", "geico",
    "century 21", "keller williams", "re/max", "coldwell banker",
    "prudential", "berkshire hathaway home services",
    "great clips", "sport clips", "great clips", "regis salon",
    "fantastic sams", "supercuts",
    "nail salon",  # generic name
})

# ---------------------------------------------------------------------------
# Substring patterns — if the business name CONTAINS any of these it's a chain
# (applied after exact match, case-insensitive)
# ---------------------------------------------------------------------------

CHAIN_SUBSTRINGS: tuple[str, ...] = (
    "church of jesus christ",
    "lds church",
    "latter-day saints",
    "latter day saints",
    "7-eleven",
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_chain(name: str, types: list[str]) -> bool:
    """Return True if this business is likely a chain or non-lead entity.

    Checks (in order):
    1. Any type in SKIP_TYPES (churches, schools, government)
    2. Exact name match in CHAIN_NAMES (normalised — curly apostrophes → straight)
    3. Substring match in CHAIN_SUBSTRINGS
    """
    # Type-based filter
    for t in types:
        if t.lower() in SKIP_TYPES:
            return True

    # Normalise curly/unicode apostrophes to straight before matching.
    # OSM often stores names with curly quotes (e.g. "Macey’s").
    name_lower = _normalise_apostrophes(name).lower().strip()

    # Exact name match
    if name_lower in CHAIN_NAMES:
        return True

    # Substring match
    for pattern in CHAIN_SUBSTRINGS:
        if pattern in name_lower:
            return True

    return False


def _normalise_apostrophes(s: str) -> str:
    """Replace curly/unicode apostrophes with a straight apostrophe."""
    return s.replace("‘", "'").replace("’", "'").replace("ʼ", "'")
