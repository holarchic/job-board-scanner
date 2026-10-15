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


if __name__ == "__main__":
    unittest.main()
