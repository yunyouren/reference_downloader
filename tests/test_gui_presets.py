from reference_tool_gui import (
    build_domain_cookies_config_from_folder,
    build_parameter_help_text,
    recommended_download_preset,
    rename_mode_label_to_value,
    rename_mode_value_to_label,
)


def test_recommended_download_preset_values() -> None:
    preset = recommended_download_preset()
    assert preset["pdf_parser"] == "pdfplumber"
    assert preset["secondary_lookup"] is True
    assert preset["secondary_max"] == 60
    assert preset["secondary_top_k"] == 3
    assert preset["max_candidates_per_item"] == 5
    assert preset["retries"] == 2
    assert preset["timeout"] == 25


def test_parameter_help_text_mentions_verify_and_secondary() -> None:
    text_en = build_parameter_help_text("en")
    text_zh = build_parameter_help_text("zh")
    assert "verify_rename" in text_en
    assert "secondary_lookup" in text_en
    assert "generic_download_sites" in text_en
    assert "参数说明" in text_zh
    assert "secondary_lookup" in text_zh
    assert "generic_download_sites" in text_zh


def test_rename_mode_label_value_mapping() -> None:
    assert rename_mode_value_to_label("number_only", "zh") == "仅编号"
    assert rename_mode_label_to_value("编号+原名", "zh") == "number_and_original"
    assert rename_mode_value_to_label("original", "en") == "Original Name"
    assert rename_mode_label_to_value("Number Only", "en") == "number_only"


def test_build_domain_cookies_config_from_folder(tmp_path) -> None:
    (tmp_path / "aps.json").write_text("[]", encoding="utf-8")
    (tmp_path / "ieee.txt").write_text("", encoding="utf-8")
    (tmp_path / "unknown.json").write_text("[]", encoding="utf-8")
    cfg = build_domain_cookies_config_from_folder(tmp_path)
    domains = cfg.get("domains", {})
    assert "link.aps.org" in domains
    assert "ieeexplore.ieee.org" in domains
    assert all("cookies_path" in v for v in domains.values())
