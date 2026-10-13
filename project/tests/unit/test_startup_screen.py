import json
from startup_screen import read_status


def test_loading_screen_never_uses_status_from_another_launch(tmp_path):
    path = tmp_path / 'status.json'
    path.write_text(json.dumps({'run_id': 'previous', 'state': 'ready', 'url': 'http://127.0.0.1:5000'}))
    result = read_status(path, 'current')
    assert result['state'] == 'starting'
    assert 'url' not in result


def test_loading_screen_handles_missing_partial_and_powershell_json(tmp_path):
    path = tmp_path / 'status.json'
    assert read_status(path, 'current')['state'] == 'starting'
    path.write_text('{partial')
    assert read_status(path, 'current')['state'] == 'starting'
    path.write_text(json.dumps({'run_id': 'current', 'state': 'failed', 'error': 'Database unavailable'}), encoding='utf-8-sig')
    assert read_status(path, 'current')['error'] == 'Database unavailable'
