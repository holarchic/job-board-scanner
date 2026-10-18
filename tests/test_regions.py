import unittest

from jobscan import regions

EU = {"Austria", "Belgium", "Bulgaria", "Croatia", "Cyprus", "Czechia", "Denmark", "Estonia", "Finland", "France",
      "Germany", "Greece", "Hungary", "Ireland", "Italy", "Latvia", "Lithuania", "Luxembourg", "Malta", "Netherlands",
      "Poland", "Portugal", "Romania", "Slovakia", "Slovenia", "Spain", "Sweden"}


def members(name):
    return next(m for names, m in regions.REGIONS if name in names)


class RegionTableTest(unittest.TestCase):
    def test_every_member_is_a_listed_country_and_every_country_is_in_a_region(self):
        listed = set().union(*(m for _, m in regions.REGIONS))
        self.assertEqual(listed - regions.COUNTRIES.keys(), set())
        self.assertEqual(regions.COUNTRIES.keys() - listed, set())

    def test_no_name_is_two_countries_or_two_regions(self):
        names = [n.casefold() for country, others in regions.COUNTRIES.items() for n in (country, *others)]
        self.assertEqual(len(names), len(set(names)))
        region_names = [n.casefold() for names, _ in regions.REGIONS for n in names]
        self.assertEqual(len(region_names), len(set(region_names)))
        self.assertEqual(set(names) & set(region_names), set())

    def test_the_eu_and_eea_are_exact(self):
        self.assertEqual(members("EU"), EU)
        self.assertEqual(members("EEA"), EU | {"Iceland", "Liechtenstein", "Norway"})

    def test_the_uk_is_in_europe_and_emea_but_not_the_eu(self):
        uk = regions.containing(["UK"])
        self.assertLessEqual({"europe", "emea", "uk&i", "uki", "uk and ireland", "western europe", "northern europe"},
                             uk)
        self.assertEqual(uk & {"eu", "european union", "eea", "eastern europe", "dach", "apac", "north america"}, set())

    def test_broad_regions_hold_the_narrow_ones(self):
        self.assertLessEqual(members("North America"), members("Americas"))
        self.assertLessEqual(members("Nordics"), members("Europe"))
        self.assertLessEqual(members("Europe"), members("EMEA"))
        self.assertLessEqual(members("Western Asia") | members("North Asia") | members("East Asia"), members("Asia"))
        self.assertEqual(members("APAC") & {"Türkiye", "Cyprus", "Russia"}, set())
        self.assertLessEqual(members("ANZ") | members("Southeast Asia"), members("APAC"))

    def test_a_country_counts_by_any_of_its_names_exactly(self):
        self.assertEqual(regions.containing([" uk "]), regions.containing(["United Kingdom"]))
        self.assertEqual(regions.containing(["England"]), regions.containing(["United Kingdom"]))
        self.assertLessEqual({"uk", "united kingdom", "gb", "great britain", "scotland"},
                             regions.containing(["United Kingdom"]))
        self.assertEqual(regions.containing(["Ireland"]) & {"northern ireland", "uk"}, set())
        for place in ["London", "Europe", "EMEA", "Remote", "UK & Ireland", ""]:
            self.assertEqual(regions.containing([place]), frozenset(), place)

    def test_names_are_case_folded_longest_first(self):
        self.assertEqual(list(regions.NAMES), sorted(regions.NAMES, key=lambda n: (-len(n), n)))
        self.assertLess(regions.NAMES.index("eastern europe"), regions.NAMES.index("europe"))
        self.assertLess(regions.NAMES.index("northern ireland"), regions.NAMES.index("ireland"))
        self.assertLess(regions.NAMES.index("new england"), regions.NAMES.index("england"))
        self.assertTrue(all(n == n.casefold() for n in regions.NAMES))

    def test_every_part_of_europe_and_asia_has_both_forms(self):
        for names, _ in regions.REGIONS:
            for name in names:
                for noun, adjective in [(" Europe", " European"), (" Asia", " Asian")]:
                    if name.endswith(noun):
                        self.assertIn(name.removesuffix(noun) + adjective, names)


if __name__ == "__main__":
    unittest.main()
