import pytest

@pytest.fixture
def qa_report_dir(tmp_path):
    report_dir = tmp_path / "qa_reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    return report_dir
