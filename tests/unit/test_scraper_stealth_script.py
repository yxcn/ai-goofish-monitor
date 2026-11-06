from src.scraper import _default_context_options, build_stealth_init_script


def test_mobile_context_has_no_plugins_and_five_touch_points():
    script = build_stealth_init_script(_default_context_options())
    assert "'plugins', {get: () => []}" in script
    assert "'maxTouchPoints', {get: () => 5}" in script


def test_desktop_snapshot_without_touch_reports_zero_touch_points_and_plugins():
    script = build_stealth_init_script({"is_mobile": False, "has_touch": False})
    assert "'maxTouchPoints', {get: () => 0}" in script
    assert "'plugins', {get: () => [1, 2, 3, 4, 5]}" in script


def test_load_times_is_no_longer_faked_and_real_chrome_object_is_kept():
    for options in (_default_context_options(), {"is_mobile": False, "has_touch": False}, {}):
        script = build_stealth_init_script(options)
        assert "loadTimes" not in script
        assert "if (!window.chrome)" in script
        assert "'webdriver', {get: () => undefined}" in script
