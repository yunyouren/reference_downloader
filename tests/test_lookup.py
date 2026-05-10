import unittest

from src.lookup import (
    guess_title_query,
    parse_ref_year,
    parse_first_author_surname,
    secondary_title_score,
    unique_preserve_order,
    is_neurips_reference,
)


class TestGuessTitleQuery(unittest.TestCase):
    def test_uses_quoted_text(self):
        text = 'J. Smith, "A Novel Method for Power Converter Simulation", IEEE Trans.'
        result = guess_title_query(text)
        self.assertIn("Novel Method", result)

    def test_falls_back_to_longest_sentence(self):
        text = "Short intro. A comprehensive analysis of grid stability. Brief conclusion."
        result = guess_title_query(text)
        self.assertIn("comprehensive analysis", result)

    def test_strips_volume_number_suffix(self):
        text = "A study of microgrid control vol. 12 no. 3 pp. 45-60"
        result = guess_title_query(text)
        self.assertNotIn("vol", result.lower())

    def test_empty_returns_truncated(self):
        result = guess_title_query("")
        self.assertEqual(result, "")

    def test_short_text(self):
        result = guess_title_query("hello")
        self.assertEqual(result, "hello")


class TestParseRefYear(unittest.TestCase):
    def test_extracts_4digit_year(self):
        self.assertEqual(parse_ref_year("Smith et al., 2023, Nature"), 2023)

    def test_returns_none_for_no_year(self):
        self.assertIsNone(parse_ref_year("No year here"))

    def test_returns_none_for_non_year_number(self):
        self.assertIsNone(parse_ref_year("Model 3000 series"))

    def test_extracts_early_1900s(self):
        self.assertEqual(parse_ref_year("Published 1956 by ACM"), 1956)


class TestParseFirstAuthorSurname(unittest.TestCase):
    def test_extracts_first_capitalized_word(self):
        self.assertEqual(parse_first_author_surname("Smith, J. and Brown, T."), "smith")

    def test_extracts_from_comma_format(self):
        self.assertEqual(parse_first_author_surname("Zhang, Y., Li, W."), "zhang")

    def test_empty_input(self):
        self.assertEqual(parse_first_author_surname(""), "")

    def test_no_capital_word(self):
        self.assertEqual(parse_first_author_surname("123 abc"), "")


class TestSecondaryTitleScore(unittest.TestCase):
    def test_identical_titles(self):
        score = secondary_title_score(
            "A fast method for power converter simulation",
            "A fast method for power converter simulation",
        )
        self.assertAlmostEqual(score, 1.0, places=6)

    def test_similar_titles(self):
        score = secondary_title_score(
            "A fast method for power converter simulation",
            "Fast methods for simulation of power converters",
        )
        self.assertGreater(score, 0.3)
        self.assertLess(score, 1.0)

    def test_different_titles(self):
        score = secondary_title_score(
            "Grid stability analysis",
            "Completely different topic here",
        )
        self.assertLess(score, 0.5)

    def test_empty_inputs(self):
        self.assertEqual(secondary_title_score("", ""), 0.0)
        self.assertEqual(secondary_title_score("title", ""), 0.0)


class TestUniquePreserveOrder(unittest.TestCase):
    def test_removes_duplicates_preserving_order(self):
        self.assertEqual(
            unique_preserve_order(["a", "b", "a", "c", "b"]),
            ["a", "b", "c"],
        )

    def test_filters_empty_strings(self):
        self.assertEqual(
            unique_preserve_order(["", "a", "", "b", ""]),
            ["a", "b"],
        )

    def test_empty_list(self):
        self.assertEqual(unique_preserve_order([]), [])


class TestIsNeuripsReference(unittest.TestCase):
    def test_detects_neurips_abbrev(self):
        self.assertTrue(is_neurips_reference("Advances in Neural Information Processing Systems (NeurIPS)"))

    def test_detects_nips(self):
        self.assertTrue(is_neurips_reference("NIPS 2020 proceedings"))

    def test_not_neurips(self):
        self.assertFalse(is_neurips_reference("IEEE Conference on Computer Vision"))

    def test_detects_full_name(self):
        self.assertTrue(is_neurips_reference(
            "Neural Information Processing Systems 2019"
        ))

    def test_detects_abbreviated(self):
        self.assertTrue(is_neurips_reference(
            "In: Neural Inf. Process. Syst. (NeurIPS), 2021"
        ))


if __name__ == "__main__":
    unittest.main()
