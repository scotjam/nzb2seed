

def test_a_keep_time_set_in_days_reads_as_hours(tmp_path):
    from nzb2seed import config as c
    cfg = c.from_sections({"behaviour": {"download_keep_days": 3}}, tmp_path / "x.toml")
    assert cfg.download_keep_hours == 72
    cfg = c.from_sections({"behaviour": {"download_keep_days": 3, "download_keep_hours": 12}}, tmp_path / "x.toml")
    assert cfg.download_keep_hours == 12
