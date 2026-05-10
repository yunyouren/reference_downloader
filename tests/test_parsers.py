"""Tests for reference parsing functions."""
from src.parsers import (
    cleanup_reference_text,
    parse_numeric_references,
    parse_non_numeric_references,
    split_references,
    DOI_RE,
    URL_RE,
)


class TestCleanupReferenceText:
    def test_smart_quotes(self):
        text = '“Hello world”'
        cleaned = cleanup_reference_text(text)
        assert cleaned == '"Hello world"'

    def test_single_smart_quotes(self):
        text = "‘word’"
        cleaned = cleanup_reference_text(text)
        assert cleaned == "'word'"

    def test_hyphenated_line_break(self):
        text = "contin-\nued"
        cleaned = cleanup_reference_text(text)
        assert cleaned == "continued"

    def test_multiple_spaces(self):
        text = "hello    world  \n  test"
        cleaned = cleanup_reference_text(text)
        assert cleaned == "hello world test"


class TestParseNumericReferences:
    def test_bracketed_numbers(self):
        section = (
            "[1] Smith et al., A Study on X. doi:10.1234/foo\n"
            "[2] Jones, Another Paper. https://example.com"
        )
        refs = parse_numeric_references(section)
        assert len(refs) == 2
        assert refs[0].number == 1
        assert "10.1234/foo" in refs[0].dois
        assert refs[1].number == 2
        assert "https://example.com" in refs[1].urls

    def test_dotted_numbers(self):
        section = "1. First paper\n2. Second paper with doi:10.5678/bar"
        refs = parse_numeric_references(section)
        assert len(refs) == 2
        assert refs[0].number == 1
        assert refs[1].number == 2
        assert "10.5678/bar" in refs[1].dois

    def test_parenthesized_numbers(self):
        section = "(1) Paper A\n(2) Paper B"
        refs = parse_numeric_references(section)
        assert len(refs) == 2
        assert refs[0].number == 1
        assert refs[1].number == 2

    def test_chinese_parentheses(self):
        section = "（1） Chinese paper A\n（2） Chinese paper B"
        refs = parse_numeric_references(section)
        assert len(refs) == 2
        assert refs[0].number == 1

    def test_doi_extraction(self):
        section = (
            "[1] Author et al., Title. Journal. "
            "doi:10.1007/s12345-021-12345-6\n"
            "[2] Author B, Another. doi:10.1109/TPEL.2023.1234567"
        )
        refs = parse_numeric_references(section)
        assert len(refs) == 2
        assert "10.1007/s12345-021-12345-6" in refs[0].dois
        assert "10.1109/TPEL.2023.1234567" in refs[1].dois


class TestSplitReferences:
    def test_dispatches_to_numeric(self):
        section = """[1] Smith et al., A Study on X. doi:10.1234/foo, pp. 1-10.
[2] Jones, Another Paper. https://example.com, pp. 11-20."""
        refs = split_references(section)
        assert len(refs) == 2
        assert refs[0].number == 1

    def test_dispatches_to_non_numeric(self):
        section = (
            "Smith, J. (2021) A Study on X\n"
            "Jones, P. (2022) Another Paper"
        )
        refs = split_references(section)
        assert len(refs) >= 1


class TestRegexConstants:
    def test_doi_regex(self):
        matches = DOI_RE.findall("doi:10.1234/foo.bar_123")
        assert len(matches) == 1
        assert matches[0] == "10.1234/foo.bar_123"

    def test_doi_regex_doi_org(self):
        match = DOI_RE.search("https://doi.org/10.1007/s12345-021-12345-6")
        assert match is not None
        assert match.group(1) == "10.1007/s12345-021-12345-6"

    def test_url_regex(self):
        matches = URL_RE.findall("See https://example.com/paper.pdf for details")
        assert len(matches) >= 1
        assert "https://example.com/paper.pdf" in matches
