"""The watchlist's matcher: whole words of a company's name, or a board's slug."""
import unittest

from jobscan.watch import watched


class WatchedTest(unittest.TestCase):
    def test_a_name_is_whole_words_of_the_company_case_folded(self):
        for terms, company, expected in [(("Acme",), "Acme AI", True), (("acme ai",), "ACME AI Ltd", True),
                                         (("Acme",), "Acmeco", False), (("Acme",), "Big Acme", True),
                                         (("Straße",), "STRASSE GmbH", True), (("AI",), "Acme AI", True),
                                         (("Globex", "Acme"), "Acme", True), ((), "Acme", False),
                                         (("Initech",), "Initrode", False)]:
            self.assertEqual(watched(terms, company, "ashby:x"), expected, (terms, company))

    def test_a_slug_matches_the_boards_own_slug_exactly(self):
        for term, board, expected in [("hooli-eu", "ashby:hooli-eu", True), ("Hooli-EU", "lever:hooli-eu", True),
                                      ("hooli-eu", "ashby:hooli-eu-2", False), ("hooli", "ashby:hooli-eu", False),
                                      ("acme.wd3.careers", "workday:acme.wd3.careers", True)]:
            self.assertEqual(watched((term,), "Someone Else", board), expected, (term, board))


if __name__ == "__main__":
    unittest.main()
