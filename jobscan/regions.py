"""World regions as job ads name them, each with the countries it takes in, so that a remote job in a region holding a
country the profile names is in the profile's places (`prefilter.in_places`): "Remote - EMEA" for a search in the UK,
and "Remote - GB" too, the country itself under another of its names. Only upward: a profile naming a region never
takes in the countries inside it.

The countries are those of the markets jobscan serves: Europe, the US and Canada, APAC and India. A country only counts
when a profile names it, so one left out costs nothing elsewhere; a profile naming it gets no regions, as before the
table. Membership is generous where usage varies (Türkiye and Cyprus are European, the Nordics and Iberia in Western
Europe): a wrong inclusion costs one tagged posting, a wrong exclusion hides a job. The EU and the EEA are exact,
without the UK."""
from collections.abc import Iterable

# Every country by its name, with the other names ads and profiles give it. A name holding another country's name
# is listed under its own country, so the longer name is read first: "New England" is never England, nor "New
# South Wales" Wales.
COUNTRIES: dict[str, tuple[str, ...]] = {
    # Europe
    "United Kingdom": ("UK", "U.K.", "Great Britain", "Britain", "GB", "GBR", "England", "Scotland", "Wales",
                       "Northern Ireland"),
    "Ireland": ("Republic of Ireland",), "Iceland": (), "Norway": (), "Sweden": (), "Finland": (), "Denmark": (),
    "Estonia": (), "Latvia": (), "Lithuania": (),
    "France": (), "Belgium": (), "Netherlands": ("The Netherlands", "Holland"), "Luxembourg": (),
    "Germany": ("Deutschland",), "Austria": (), "Switzerland": (), "Liechtenstein": (), "Monaco": (),
    "Spain": (), "Portugal": (), "Italy": (), "Greece": (), "Malta": (), "Andorra": (), "San Marino": (),
    "Vatican City": ("Holy See",), "Cyprus": (),
    "Slovenia": (), "Croatia": (), "Bosnia and Herzegovina": ("Bosnia",), "Serbia": (), "Montenegro": (),
    "North Macedonia": ("Macedonia",), "Albania": (), "Kosovo": (),
    "Poland": (), "Czechia": ("Czech Republic",), "Slovakia": (), "Hungary": (), "Romania": (), "Bulgaria": (),
    "Ukraine": (), "Moldova": (), "Belarus": (), "Russia": ("Russian Federation",), "Türkiye": ("Turkey", "Turkiye"),
    # North America
    "United States": ("US", "U.S.", "USA", "U.S.A.", "United States of America", "New England", "New Britain"),
    "Canada": (),
    # APAC
    "Australia": ("New South Wales",), "New Zealand": (), "Japan": (), "South Korea": ("Korea", "Republic of Korea"),
    "China": (), "Hong Kong": (), "Taiwan": (), "Singapore": (), "Malaysia": (), "Indonesia": (),
    "Philippines": ("The Philippines",), "Thailand": (), "Vietnam": ("Viet Nam",), "India": (),
}

_NORDICS = {"Denmark", "Norway", "Sweden", "Finland", "Iceland"}
_BALTICS = {"Estonia", "Latvia", "Lithuania"}
_BALKANS = {"Slovenia", "Croatia", "Bosnia and Herzegovina", "Serbia", "Montenegro", "North Macedonia", "Albania",
            "Kosovo", "Bulgaria", "Romania", "Greece"}
_EU = {"Austria", "Belgium", "Bulgaria", "Croatia", "Cyprus", "Czechia", "Denmark", "Estonia", "Finland", "France",
       "Germany", "Greece", "Hungary", "Ireland", "Italy", "Latvia", "Lithuania", "Luxembourg", "Malta", "Netherlands",
       "Poland", "Portugal", "Romania", "Slovakia", "Slovenia", "Spain", "Sweden"}
_WESTERN_EUROPE = ({"United Kingdom", "Ireland", "France", "Belgium", "Netherlands", "Luxembourg", "Germany",
                    "Austria", "Switzerland", "Liechtenstein", "Monaco", "Spain", "Portugal", "Italy", "Greece",
                    "Malta", "Andorra", "San Marino", "Vatican City", "Cyprus"} | _NORDICS)
_EASTERN_EUROPE = ({"Poland", "Czechia", "Slovakia", "Hungary", "Romania", "Bulgaria", "Ukraine", "Moldova",
                    "Belarus", "Russia"} | _BALTICS | _BALKANS - {"Greece"})
_EUROPE = _WESTERN_EUROPE | _EASTERN_EUROPE | {"Türkiye"}
_NORTH_AMERICA = {"United States", "Canada"}
_EAST_ASIA = {"Japan", "South Korea", "China", "Hong Kong", "Taiwan"}
_SOUTHEAST_ASIA = {"Singapore", "Malaysia", "Indonesia", "Philippines", "Thailand", "Vietnam"}
_OCEANIA = {"Australia", "New Zealand"}
_APAC = _EAST_ASIA | _SOUTHEAST_ASIA | {"India"} | _OCEANIA
_WESTERN_ASIA, _NORTH_ASIA = {"Türkiye", "Cyprus"}, {"Russia"}
_ASIA = _APAC - _OCEANIA | _WESTERN_ASIA | _NORTH_ASIA


def _part_of(whole: str, *stems: str) -> tuple[str, ...]:
    """A part of Europe's or Asia's names, as ads write them: every stem with the noun and with its adjective, so
    neither is read as the whole ("Southeastern European countries", "South-eastern Asia")."""
    return tuple(f"{stem} {noun}" for stem in stems for noun in (whole, {"Europe": "European", "Asia": "Asian"}[whole]))


_SOUTH_EAST = ("Southeast", "South-East", "South East", "Southeastern", "South-Eastern", "South Eastern")
_SOUTH_WEST = ("Southwest", "South-West", "South West", "Southwestern", "South-Western", "South Western")
_NORTH_EAST = ("Northeast", "North-East", "North East", "Northeastern", "North-Eastern", "North Eastern")
_NORTH_WEST = ("Northwest", "North-West", "North West", "Northwestern", "North-Western", "North Western")

# Each region's names as ads write them, with its countries.
REGIONS: tuple[tuple[tuple[str, ...], frozenset[str]], ...] = tuple((names, frozenset(members)) for names, members in (
    (("Europe", "European"), _EUROPE),
    (_part_of("Europe", "Continental", "Mainland"),
     _EUROPE - {"United Kingdom", "Ireland", "Iceland", "Malta", "Cyprus"}),
    (("EU", "E.U.", "European Union"), _EU),
    (("EEA", "European Economic Area"), _EU | {"Iceland", "Liechtenstein", "Norway"}),
    (_part_of("Europe", "Western", "West", *_NORTH_WEST), _WESTERN_EUROPE),
    (_part_of("Europe", "Northern", "North"), {"United Kingdom", "Ireland"} | _NORDICS | _BALTICS),
    (_part_of("Europe", "Southern", "South"),
     {"Spain", "Portugal", "Italy", "Greece", "Malta", "Andorra", "San Marino", "Vatican City", "Cyprus", "Türkiye"}
     | _BALKANS),
    (("CEE", *_part_of("Europe", "Eastern", "East", "Central and Eastern", "Central & Eastern", "Central Eastern",
                       "Central-Eastern", *_NORTH_EAST)), _EASTERN_EUROPE),
    (_part_of("Europe", "Central"),
     {"Germany", "Austria", "Switzerland", "Liechtenstein", "Poland", "Czechia", "Slovakia", "Hungary", "Slovenia"}),
    (_part_of("Europe", *_SOUTH_EAST), _BALKANS),
    (_part_of("Europe", *_SOUTH_WEST), {"Spain", "Portugal", "Andorra"}),
    (("UK&I", "UKI", "UK & I", "UK and Ireland", "UK & Ireland", "British Isles"), {"United Kingdom", "Ireland"}),
    (("DACH",), {"Germany", "Austria", "Switzerland", "Liechtenstein"}),
    (("Benelux",), {"Belgium", "Netherlands", "Luxembourg"}),
    (("Nordics", "Nordic", "Nordic countries", "Scandinavia"), _NORDICS),
    (("Baltics", "Baltic states", "Baltic"), _BALTICS),
    (("Balkans",), _BALKANS),
    (("Iberia", "Iberian Peninsula"), {"Spain", "Portugal", "Andorra"}),
    (("EMEA",), _EUROPE),
    (("EMEIA",), _EUROPE | {"India"}),
    (("North America", "North American", "NAMER"), _NORTH_AMERICA),
    (("Americas", "AMER"), _NORTH_AMERICA),
    (("APAC", "APJ", "Asia Pacific", "Asia-Pacific", "Asia/Pacific"), _APAC),
    (("Asia", "Asian"), _ASIA),
    (_part_of("Asia", "East", "Eastern", *_NORTH_EAST), _EAST_ASIA),
    (("ASEAN", *_part_of("Asia", *_SOUTH_EAST)), _SOUTHEAST_ASIA),
    (_part_of("Asia", "South", "Southern"), {"India"}),
    (_part_of("Asia", "West", "Western", *_SOUTH_WEST), _WESTERN_ASIA),
    (_part_of("Asia", "North", "Northern"), _NORTH_ASIA),
    (_part_of("Asia", "Central"), set()),  # none of the countries above, but read whole: never Asia
    (("Oceania", "Australasia", "ANZ"), _OCEANIA),
))

_BY_NAME = {name.casefold(): country for country, others in COUNTRIES.items() for name in (country, *others)}

# Every region and country name, case-folded, longest first: read in that order, "Eastern Europe" is never read as
# "Europe", nor "Northern Ireland" as "Ireland".
NAMES: tuple[str, ...] = tuple(sorted({n.casefold() for names, _ in REGIONS for n in names} | _BY_NAME.keys(),
                                      key=lambda n: (-len(n), n)))


def containing(places: Iterable[str]) -> frozenset[str]:
    """The names, case-folded, of every country one of the places names exactly (case-folded, by any of the
    country's names), each name that country goes by, and every region holding it: "United Kingdom" gives "uk", "gb",
    "europe", "emea", "uk&i" and the rest, never "eu". A country is the smallest region holding itself."""
    countries = {_BY_NAME[p] for p in (p.strip().casefold() for p in places) if p in _BY_NAME}
    return (frozenset(name for name, country in _BY_NAME.items() if country in countries)
            | frozenset(n.casefold() for names, members in REGIONS if countries & members for n in names))
