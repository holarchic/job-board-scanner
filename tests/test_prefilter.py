import unittest

from jobscan.config import Profile
from jobscan.prefilter import in_places, matches
from jobscan.records import Posting

PROFILE = Profile(
    name="ae", titles=("Account Executive", "Founding Sales"), exclude_titles=("Sales Development", "Junior", "intern"),
    locations=("London", "UK", "Remote (UK)"), eligible_where="the UK", tags=("founding",), custom_tags=(),
)


def posting(title, location="London"):
    return Posting(key="ashby:acme:1", ats="ashby", board="ashby:acme", company="Acme", title=title, location=location,
                   url="https://jobs.ashbyhq.com/acme/1", description="")


class PrefilterTest(unittest.TestCase):
    def test_titles_kept(self):
        for title in ["Account Executive", "Senior Account Executive, Enterprise - UK", "account executive (UK)",
                      "Founding Sales (Munich Office)", "International Account Executive"]:
            self.assertTrue(matches(posting(title), PROFILE), title)

    def test_near_misses_and_excludes_dropped(self):
        for title in ["Senior Product Marketing Manager", "Technical Project Manager - UK", "Account Manager",
                      "Founding Sales Development Representative", "Inside Sales (Junior Account Executive)",
                      "Account Executives Lead", "Account Executive Intern"]:
            self.assertFalse(matches(posting(title), PROFILE), title)

    def test_locations_are_word_adjacent(self):
        self.assertTrue(matches(posting("Account Executive", "UK & Ireland"), PROFILE))
        self.assertTrue(matches(posting("Account Executive", "remote (uk)"), PROFILE))
        self.assertTrue(matches(posting("Account Executive", "Berlin / London"), PROFILE))
        self.assertFalse(matches(posting("Account Executive", "Kyiv, Ukraine"), PROFILE))
        self.assertFalse(matches(posting("Account Executive", "Londonderry"),
                                 PROFILE))
        self.assertFalse(matches(posting("Account Executive", ""), PROFILE))

    def test_unicode_case_folding(self):
        only = Profile(name="ae", titles=("Account Executive",), exclude_titles=(), locations=("Weißenburg",),
                       eligible_where="the UK", tags=("founding",), custom_tags=())
        self.assertTrue(matches(posting("ACCOUNT EXECUTIVE", "WEISSENBURG"), only))

    def test_punctuation_ended_term_matches_itself(self):
        only = Profile(name="ae", titles=("Account Executive",), exclude_titles=(), locations=("Remote (UK)",),
                       eligible_where="the UK", tags=("founding",), custom_tags=())
        self.assertTrue(matches(posting("Account Executive", "Remote (UK)"), only))
        self.assertTrue(matches(posting("Account Executive", "London / remote (uk)"), only))
        self.assertFalse(matches(posting("Account Executive", "Remote (UKI)"), only))

    def test_in_places_is_the_location_half_of_matches(self):
        for place in ["UK & Ireland", "Berlin / London", "Kyiv, Ukraine", "Londonderry", "", "remote (uk)"]:
            self.assertEqual(in_places(PROFILE, place), matches(posting("Account Executive", place), PROFILE), place)


def places(*locations):
    return Profile(name="pm", titles=("Product Manager",), exclude_titles=(), locations=locations,
                   eligible_where="the UK", tags=(), custom_tags=())


class RemoteTest(unittest.TestCase):
    def test_a_remote_place_keeps_remote_jobs_naming_no_place_or_a_region_holding_the_country(self):
        uk_or_remote, uk = places("London", "United Kingdom", "Remote"), places("London", "United Kingdom")
        for text, with_remote, without in [
            ("Remote", True, False),
            ("100% Remote / Remote", True, False),
            ("Anywhere", True, False),
            ("Remote - Worldwide", True, False),
            ("Hybrid / Remote", True, False),
            ("Remote | UTC-6 to UTC+2", True, False),
            ("Remote working", True, False),
            ("Work remotely", True, False),
            ("Remote (EST)", False, False),  # a named zone is read as a place
            ("Remote - Eastern European countries", False, False),
            ("Remote - Southeastern Europe", False, False),
            ("Remote - Continental Europe", False, False),
            ("Remote - Southeastern European countries", False, False),
            ("Remote - Continental European countries", False, False),
            ("Remote - Southwestern European countries", False, False),
            ("Remote - South-Western European countries", False, False),
            ("Remote - Mainland European", False, False),
            ("Remote - Northeastern European", False, False),
            ("Remote - North West Europe", True, True),
            ("New England | Remote / USA - Remote", False, False),  # a UK name inside a US one
            ("New Britain, CT / Remote", False, False),
            ("Remote - New South Wales", False, False),
            ("Remote - UK", True, True),  # the country the profile names, by another of its names
            ("Remote, GB", True, True),
            ("Remote - Ireland", False, False),
            ("Remote - Northern Ireland", True, True),
            ("London or Remote", True, True),
            ("Remote - EMEA", True, True),
            ("Remote (Europe)", True, True),
            ("Home based - EMEA", True, True),
            ("Remote - European timezone", True, True),
            ("Remote - Global Anywhere / Remote - EMEA", True, True),
            ("Remote - United States", False, False),
            ("Canada / Remote", False, False),
            ("Berlin, Germany / Remote", False, False),
            ("Anywhere in France", False, False),
            ("Remote - EU", False, False),
            ("Remote - Eastern Europe", False, False),
            ("Remote - DACH", False, False),
            ("EMEA", False, False),  # an office somewhere in EMEA, not anywhere in it
            ("Europe / Paris", False, False),
            ("New York", False, False),
        ]:
            with self.subTest(text):
                self.assertEqual(in_places(uk_or_remote, text), with_remote)
                self.assertEqual(in_places(uk, text), without)

    def test_a_remote_place_is_any_place_holding_only_remote_words(self):
        for term in ["Remote", "fully remote", "Work from home", "Anywhere", "Remote-first"]:
            with self.subTest(term):
                self.assertTrue(in_places(places("London", term), "Remote"))
                self.assertFalse(in_places(places("London", term), "Remote - US"))
        self.assertFalse(in_places(places("London", "Remote UK"), "Remote"))  # names a place: matched as written
        self.assertTrue(in_places(places("London", "Remote UK"), "Remote UK / Remote US"))

    def test_regions_count_only_through_a_country_the_profile_names(self):
        self.assertFalse(in_places(places("London", "Remote"), "Remote - EMEA"))
        self.assertTrue(in_places(places("England"), "Remote - EMEA"))
        self.assertTrue(in_places(places("Germany"), "Remote - DACH"))
        self.assertFalse(in_places(places("Germany"), "Remote - UK&I"))
        self.assertFalse(in_places(places("EMEA"), "Remote - Germany"))  # never downward from a region
        self.assertTrue(in_places(places("USA"), "Remote - North American time zones"))
        for country, valid in [("India", "Remote - South Asia"), ("Singapore", "Remote - Southeast Asia")]:
            for profile in (places(country), places(country, "Remote")):
                with self.subTest(profile.locations):
                    self.assertTrue(in_places(profile, valid))
                    self.assertTrue(in_places(profile, "Remote - APAC"))
                    for elsewhere in ["Western Asia", "Central Asia", "Southwestern Asia", "North Asia"]:
                        self.assertFalse(in_places(profile, f"Remote - {elsewhere}"), elsewhere)
        for country, inside, outside in [("Singapore", "South-eastern Asia", "Southern Asia"),
                                         ("India", "Southern Asian", "Southeastern Asia"),
                                         ("Japan", "Northeast Asia", "South Asia"),
                                         ("Türkiye", "Western Asia", "Central Asia"),
                                         ("Russia", "North Asia", "Southwestern Asia")]:
            for profile in (places(country), places(country, "Remote")):
                with self.subTest(country, profile=profile.locations):
                    self.assertTrue(in_places(profile, f"Remote - {inside}"))
                    self.assertFalse(in_places(profile, f"Remote - {outside}"))
                    self.assertTrue(in_places(profile, "Remote - Asian countries"))
                    self.assertEqual(in_places(profile, "Remote - APAC"), country not in {"Türkiye", "Russia"})
        self.assertTrue(in_places(places("Germany"), "Remote - Continental Europe"))


if __name__ == "__main__":
    unittest.main()
