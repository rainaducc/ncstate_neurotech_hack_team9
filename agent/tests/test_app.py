"""App smoke tests: each report loads and renders without errors (streamlit AppTest)."""
import os
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import ReportStore  # noqa: E402

# the app hides a provisional report once its final report is loaded (F18), so test the ids it lists
IDS = ReportStore.default().visible_ids()


@pytest.mark.parametrize("rid", IDS)
def test_app_renders_each_report(rid, tmp_path, monkeypatch):
    monkeypatch.setenv("NEUROSYM_AUDIT_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.chdir(ROOT)
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.run()
    at.sidebar.selectbox[0].set_value(rid).run()
    assert not at.exception, at.exception
    assert any(rid in m.value for m in at.markdown)
    at.button(key=f"b1-{rid}").click().run()          # ask the copilot
    assert not at.exception
    at.button(key=f"accept-{rid}").click().run()      # accept the draft
    assert not at.exception and at.success
    assert list((tmp_path / "accepted").glob(f"{rid}_*.json"))
