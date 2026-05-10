"""Tests for interactive terminal UI module."""
import pytest
from unittest.mock import patch, Mock, MagicMock
from io import StringIO
import sys

from src.interactive_ui import (
    should_run_interactive,
    display_domain_summary,
    prompt_cookie_configuration,
    configure_cookies_interactively,
    confirm_continue_without_cookies,
    prompt_for_additional_cookies,
    display_download_summary,
)
from site_handlers.domain_analyzer import DomainInfo
from src.models import ReferenceItem


# ---------------------------------------------------------------------------
# should_run_interactive
# ---------------------------------------------------------------------------

class TestShouldRunInteractive:
    def test_true_setting_returns_true(self):
        assert should_run_interactive("true") is True

    def test_false_setting_returns_false(self):
        assert should_run_interactive("false") is False

    def test_auto_tty_detected(self):
        with patch.object(sys.stdin, 'isatty', return_value=True), \
             patch.object(sys.stdout, 'isatty', return_value=True):
            assert should_run_interactive("auto") is True

    def test_auto_not_tty(self):
        with patch.object(sys.stdin, 'isatty', return_value=False), \
             patch.object(sys.stdout, 'isatty', return_value=True):
            assert should_run_interactive("auto") is False

    def test_auto_tty_exception(self):
        with patch.object(sys.stdin, 'isatty', side_effect=OSError):
            assert should_run_interactive("auto") is False


# ---------------------------------------------------------------------------
# display_domain_summary
# ---------------------------------------------------------------------------

class TestDisplayDomainSummary:
    def test_prints_summarized_output(self, capsys):
        # summarize_domains is imported locally inside display_domain_summary
        with patch("site_handlers.domain_analyzer.summarize_domains", return_value="SUMMARY_TEXT"):
            display_domain_summary({})
        captured = capsys.readouterr()
        assert "SUMMARY_TEXT" in captured.out

    def test_handles_empty_info(self, capsys):
        with patch("site_handlers.domain_analyzer.summarize_domains", return_value=""):
            display_domain_summary({})
        captured = capsys.readouterr()
        # Should not crash, empty output is fine
        assert captured.out == "\n"


# ---------------------------------------------------------------------------
# prompt_cookie_configuration
# ---------------------------------------------------------------------------

class TestPromptCookieConfiguration:
    def _make_info(self, domain="example.com", display_name="Example"):
        return DomainInfo(
            domain=domain,
            display_name=display_name,
            ref_numbers=[1, 2],
            count=2,
            requires_auth=True,
            has_cookies=False,
        )

    def test_skips_on_empty_input(self):
        info = self._make_info()
        with patch("builtins.input", return_value=""):
            result = prompt_cookie_configuration("example.com", info)
        assert result is None

    def test_skips_on_eof(self):
        info = self._make_info()
        with patch("builtins.input", side_effect=EOFError):
            result = prompt_cookie_configuration("example.com", info)
        assert result is None

    def test_skips_on_keyboard_interrupt(self):
        info = self._make_info()
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            result = prompt_cookie_configuration("example.com", info)
        assert result is None

    def test_returns_config_for_valid_path(self, tmp_path):
        info = self._make_info()
        cookie_file = tmp_path / "cookies.txt"
        cookie_file.write_text("# Netscape HTTP Cookie File")
        with patch("builtins.input", side_effect=[str(cookie_file), "my cookies"]):
            result = prompt_cookie_configuration("example.com", info)
        assert result == {"cookies_path": str(cookie_file), "description": "my cookies"}

    def test_warns_on_missing_path_declines(self):
        info = self._make_info()
        with patch("builtins.input", side_effect=["/nonexistent/path.txt", "n"]):
            result = prompt_cookie_configuration("example.com", info)
        assert result is None

    def test_warns_on_missing_path_accepts(self):
        info = self._make_info()
        with patch("builtins.input", side_effect=["/nonexistent/path.txt", "y", "test desc"]):
            result = prompt_cookie_configuration("example.com", info)
        assert result == {"cookies_path": "/nonexistent/path.txt", "description": "test desc"}

    def test_description_eof_gives_empty(self):
        info = self._make_info()
        cookie_file = "/tmp/cookies.txt"
        with patch("builtins.input", side_effect=[cookie_file, EOFError]):
            with patch("pathlib.Path.exists", return_value=True):
                result = prompt_cookie_configuration("example.com", info)
        assert result == {"cookies_path": cookie_file, "description": ""}

    def test_whitespace_only_input_skips(self):
        info = self._make_info()
        with patch("builtins.input", return_value="   "):
            result = prompt_cookie_configuration("example.com", info)
        assert result is None


# ---------------------------------------------------------------------------
# configure_cookies_interactively
# ---------------------------------------------------------------------------

class TestConfigureCookiesInteractively:
    def _make_info(self, domain="springer.com", display_name="Springer", requires_auth=True, has_cookies=False, count=5):
        return DomainInfo(
            domain=domain,
            display_name=display_name,
            ref_numbers=list(range(1, count + 1)),
            count=count,
            requires_auth=requires_auth,
            has_cookies=has_cookies,
        )

    def test_all_domains_already_configured(self, capsys):
        domain_info = {"springer.com": self._make_info(has_cookies=True)}
        result = configure_cookies_interactively(domain_info)
        captured = capsys.readouterr()
        assert "都已配置cookies" in captured.out
        assert result == {}  # returns empty (existing_config passed as None)

    def test_skip_selection(self, capsys):
        domain_info = {"springer.com": self._make_info()}
        with patch("builtins.input", return_value="skip"):
            result = configure_cookies_interactively(domain_info)
        captured = capsys.readouterr()
        assert "以下域名可能需要机构登录" in captured.out
        assert result == {}

    def test_eof_skips(self):
        domain_info = {"springer.com": self._make_info()}
        with patch("builtins.input", side_effect=EOFError):
            result = configure_cookies_interactively(domain_info)
        assert result == {}

    def test_all_selection(self):
        domain_info = {
            "springer.com": self._make_info("springer.com", "Springer"),
            "ieee.org": self._make_info("ieee.org", "IEEE"),
        }
        with patch("builtins.input", side_effect=["all", "/tmp/c.txt", "desc1", "/tmp/c2.txt", "desc2"]):
            with patch("pathlib.Path.exists", return_value=True):
                result = configure_cookies_interactively(domain_info)
        assert len(result) == 2
        assert "springer.com" in result
        assert "ieee.org" in result

    def test_numeric_selection(self):
        domain_info = {
            "springer.com": self._make_info("springer.com", "Springer"),
            "ieee.org": self._make_info("ieee.org", "IEEE"),
        }
        with patch("builtins.input", side_effect=["1", "/tmp/c.txt", "desc"]):
            with patch("pathlib.Path.exists", return_value=True):
                result = configure_cookies_interactively(domain_info)
        assert len(result) == 1
        assert "springer.com" in result

    def test_comma_separated_numbers(self):
        domain_info = {
            "springer.com": self._make_info("springer.com", "Springer"),
            "ieee.org": self._make_info("ieee.org", "IEEE"),
            "wiley.com": self._make_info("wiley.com", "Wiley"),
        }
        with patch("builtins.input", side_effect=[
            "1,3",                          # select first and third
            "/tmp/c1.txt", "desc1",         # for springer
            "/tmp/c3.txt", "desc3",         # for wiley
        ]):
            with patch("pathlib.Path.exists", return_value=True):
                result = configure_cookies_interactively(domain_info)
        assert len(result) == 2
        assert "springer.com" in result
        assert "wiley.com" in result

    def test_invalid_numeric_input(self, capsys):
        domain_info = {"springer.com": self._make_info()}
        with patch("builtins.input", return_value="not-a-number"):
            result = configure_cookies_interactively(domain_info)
        captured = capsys.readouterr()
        assert "无效输入" in captured.out
        assert result == {}

    def test_out_of_range_index(self):
        domain_info = {"springer.com": self._make_info()}
        with patch("builtins.input", return_value="99"):
            result = configure_cookies_interactively(domain_info)
        assert result == {}

    def test_skips_individual_config_on_empty(self):
        domain_info = {"springer.com": self._make_info()}
        with patch("builtins.input", side_effect=["1", ""]):
            result = configure_cookies_interactively(domain_info)
        assert result == {}  # no cookie was added

    def test_preserves_existing_config(self):
        domain_info = {"springer.com": self._make_info(has_cookies=False)}
        existing = {"ieee.org": {"cookies_path": "/existing/cookies.txt"}}
        with patch("builtins.input", side_effect=["skip"]):
            result = configure_cookies_interactively(domain_info, existing_config=existing)
        assert "ieee.org" in result


# ---------------------------------------------------------------------------
# confirm_continue_without_cookies
# ---------------------------------------------------------------------------

class TestConfirmContinueWithoutCookies:
    def test_empty_domains_returns_true(self):
        assert confirm_continue_without_cookies([]) is True

    def test_user_accepts(self):
        with patch("builtins.input", return_value="y"):
            assert confirm_continue_without_cookies(["example.com"]) is True

    def test_user_declines(self):
        with patch("builtins.input", return_value="n"):
            assert confirm_continue_without_cookies(["example.com"]) is False

    def test_eof_returns_true(self):
        with patch("builtins.input", side_effect=EOFError):
            assert confirm_continue_without_cookies(["example.com"]) is True

    def test_keyboard_interrupt_returns_true(self):
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            assert confirm_continue_without_cookies(["example.com"]) is True

    def test_truncates_long_list(self, capsys):
        domains = ["a.com", "b.com", "c.com", "d.com", "e.com", "f.com", "g.com"]
        with patch("builtins.input", return_value="y"):
            result = confirm_continue_without_cookies(domains)
        assert result is True
        captured = capsys.readouterr()
        assert "... 还有 2 个" in captured.out

    def test_shows_all_when_five_or_under(self, capsys):
        domains = ["a.com", "b.com", "c.com"]
        with patch("builtins.input", return_value="y"):
            confirm_continue_without_cookies(domains)
        captured = capsys.readouterr()
        assert "a.com" in captured.out
        assert "b.com" in captured.out
        assert "c.com" in captured.out
        assert "还有" not in captured.out


# ---------------------------------------------------------------------------
# prompt_for_additional_cookies
# ---------------------------------------------------------------------------

class TestPromptForAdditionalCookies:
    def test_empty_domains_returns_none(self):
        assert prompt_for_additional_cookies({}) is None

    def test_no_likely_paywall_returns_none(self, capsys):
        failed_domains = {
            "example.com": {
                "failed_count": 3,
                "display_name": "Example",
                "ref_numbers": [1, 2, 3],
                "likely_paywall": False,
            },
        }
        result = prompt_for_additional_cookies(failed_domains)
        assert result is None

    def test_user_declines_configuring(self):
        failed_domains = {
            "springer.com": {
                "failed_count": 5,
                "display_name": "Springer Link",
                "ref_numbers": [1, 2, 3, 4, 5],
                "likely_paywall": True,
            },
        }
        with patch("builtins.input", return_value="n"):
            result = prompt_for_additional_cookies(failed_domains)
        assert result is None

    def test_eof_returns_none(self):
        failed_domains = {
            "springer.com": {
                "failed_count": 5,
                "display_name": "Springer Link",
                "ref_numbers": [1, 2, 3, 4, 5],
                "likely_paywall": True,
            },
        }
        with patch("builtins.input", side_effect=EOFError):
            result = prompt_for_additional_cookies(failed_domains)
        assert result is None

    def test_user_accepts_and_configures(self):
        failed_domains = {
            "springer.com": {
                "failed_count": 5,
                "display_name": "Springer Link",
                "ref_numbers": [1, 2, 3, 4, 5],
                "likely_paywall": True,
            },
        }
        expected_config = {"springer.com": {"cookies_path": "/tmp/c.txt"}}
        with patch("builtins.input", return_value="y"):
            with patch("src.interactive_ui.configure_cookies_interactively",
                       return_value=expected_config) as mock_configure:
                result = prompt_for_additional_cookies(failed_domains)
        assert result == expected_config
        mock_configure.assert_called_once()


# ---------------------------------------------------------------------------
# display_download_summary
# ---------------------------------------------------------------------------

class TestDisplayDownloadSummary:
    def _make_ref(self, number, status):
        return ReferenceItem(number=number, text="test", download_status=status)

    def test_shows_counts_correctly(self, capsys):
        refs = [
            self._make_ref(1, "downloaded_pdf"),
            self._make_ref(2, "downloaded_pdf"),
            self._make_ref(3, "saved_landing_url"),
            self._make_ref(4, "failed"),
            self._make_ref(5, "not_attempted"),
        ]
        display_download_summary(refs)
        captured = capsys.readouterr()
        assert "总计: 5篇" in captured.out
        assert "PDF下载成功: 2篇" in captured.out
        assert "落地页保存: 1篇" in captured.out
        assert "下载失败: 1篇" in captured.out

    def test_div_by_zero_empty_refs(self, capsys):
        """Empty refs should not crash on division by zero."""
        display_download_summary([])
        captured = capsys.readouterr()
        assert "总计: 0篇" in captured.out
        assert "0.0%" in captured.out

    def test_shows_failure_analysis(self, capsys):
        refs = [
            self._make_ref(1, "failed"),
            self._make_ref(2, "failed"),
        ]
        domain_info = {
            "springer.com": DomainInfo(
                domain="springer.com",
                display_name="Springer Link",
                ref_numbers=[1, 2],
                count=2,
                requires_auth=True,
                has_cookies=False,
            ),
        }
        display_download_summary(refs, domain_info)
        captured = capsys.readouterr()
        assert "失败域名分析" in captured.out

    def test_no_domain_info_no_failure_section(self, capsys):
        refs = [self._make_ref(1, "downloaded_pdf")]
        display_download_summary(refs)  # domain_info=None
        captured = capsys.readouterr()
        assert "失败域名分析" not in captured.out
