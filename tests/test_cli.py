"""atlassian-cli builds valid CQL from free text."""

from atlassian_cli import build_cql


def test_quotes_in_search_text_are_escaped():
    assert build_cql('say "hi"') == 'text ~ "say \\"hi\\""'
    assert build_cql("a\\b") == 'text ~ "a\\\\b"'


def test_space_filter_quotes_both_values():
    assert build_cql('it"s', space='DE"V') == 'space = "DE\\"V" AND text ~ "it\\"s"'


def test_cql_passes_through_untouched():
    assert build_cql('title = "Release"') == 'title = "Release"'
