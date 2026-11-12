import subprocess
import sys
from pathlib import Path

import pytest

from ledger.__main__ import main

REPO = Path(__file__).resolve().parents[2]
FILES = ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv",
         "data_quality.json"]


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    out = tmp_path_factory.mktemp("gen") / "data"
    subprocess.run(
        [sys.executable, str(REPO / "assignment-a-data" / "tools" / "generate_data.py"),
         "--seed", "7", "--scale", "small", "--out", str(out)],
        check=True, capture_output=True)
    return out


def build(tmp_path, name, data, *ingests):
    """Run the CLI: each entry of `ingests` is one `ingest` call over the named drops."""
    db = str(tmp_path / f"{name}.sqlite")  # a fresh PII key file is created next to each db
    for drops in ingests:
        assert main(["--db", db, "ingest", *(str(data / d) for d in drops)]) == 0
    out = tmp_path / f"{name}_reports"
    assert main(["--db", db, "report", "--out", str(out)]) == 0
    return {f: (out / f).read_bytes() for f in FILES}


def test_reports_identical_across_split_single_and_repeated_ingest(tmp_path, data, monkeypatch):
    monkeypatch.delenv("AU_PII_KEY", raising=False)
    split = build(tmp_path, "split", data, ["drop_1"], ["drop_2"])
    single = build(tmp_path, "single", data, ["drop_1", "drop_2"])
    again = build(tmp_path, "again", data, ["drop_1"], ["drop_2"], ["drop_1", "drop_2"])
    assert split["lead_report.csv"].count(b"\n") > 100  # real content, not three empty files
    assert split == single
    assert split == again
