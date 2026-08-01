from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_crawler_page_uses_category_cards_without_source_selectors() -> None:
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "frontend" / "assets" / "app.js").read_text(encoding="utf-8")

    assert 'class="panel crawler-preset-panel"' in html
    assert 'id="crawler-presets" class="crawler-preset-grid"' in html
    assert 'id="crawler-selected-preset"' in html
    assert "crawler-preset-card" in script
    assert "selectCrawlerPreset" in script
    assert "selectCustomCrawlerMode" in script
    assert 'crawlerPresetMode==="custom"' in script
    assert 'preset_ids:presetId?[presetId]:[]' in script
    assert "自定义采集至少需要一个 URL、关键词或站点入口" in script
    assert "9 + CUSTOM" in html
    assert '$("#crawler-keywords").value=' in script
    assert '$("#crawler-sites").value=' in script

    removed_ids = (
        "crawler-structured-sources",
        "crawler-legacy-category",
        "crawler-legacy-offset",
        "crawler-route-category",
    )
    for element_id in removed_ids:
        assert element_id not in html
        assert element_id not in script
