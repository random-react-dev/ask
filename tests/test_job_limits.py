"""Local deadline and retention checks; no service or installed runtime."""
import importlib.util
import json
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import ask


def receipt(root, number, status='preparing'):
    identifier = 'ask_' + format(number, '032x')
    path = root / 'jobs' / identifier
    ask.private_dir(path)
    value = dict(job_id=identifier, created_at=number, status=status,
                 question='test question', workspace=str(root), deadline_at=time.time()+3600)
    ask.write_json(path / 'receipt.json', value)
    return value


def test_deadline_is_terminal_and_retry_is_explicit(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ask, 'STATE_DIR', tmp_path)
    value = receipt(tmp_path, 1)
    value['deadline_at'] = 0
    failed = ask.terminal_state(value)
    assert failed['status'] == 'failed'
    assert 'not cancelled' in failed['message']
    assert ask.terminal_state({**value, 'status': 'ready'})['status'] == 'failed'
    ask.show(value)
    output = json.loads(capsys.readouterr().out)
    assert '--result' in output['next_command']
    assert '--new' in output['new_request_fallback']
    assert (tmp_path/'jobs'/value['job_id']/'outcome.json').stat().st_mode & 0o777 == 0o600


def test_retention_protects_unfinished(tmp_path, monkeypatch):
    monkeypatch.setattr(ask, 'STATE_DIR', tmp_path)
    active = receipt(tmp_path, 1)
    for number in range(2, 201):
        receipt(tmp_path, number, 'ready')
    ask.make_room(tmp_path/'jobs')
    assert (tmp_path/'jobs'/active['job_id']).exists()
    assert len(list((tmp_path/'jobs').iterdir())) == 199
    assert not (tmp_path/'jobs'/('ask_'+format(2,'032x'))).exists()


def test_full_unfinished_store_refuses_new_job(tmp_path, monkeypatch):
    monkeypatch.setattr(ask, 'STATE_DIR', tmp_path)
    for number in range(220):
        receipt(tmp_path, number)
    with pytest.raises(ask.ClientError, match='220-job limit'):
        ask.make_room(tmp_path/'jobs')
    assert len(list((tmp_path/'jobs').iterdir())) == 220


def test_real_failure_survives_later_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(ask, 'STATE_DIR', tmp_path)
    value = receipt(tmp_path, 1, 'failed')
    value.update(deadline_at=60, error='Provider rejected request at T+5', message='Provider rejected request at T+5')
    monkeypatch.setattr(ask.time, 'time', lambda: 61)
    actual = ask.terminal_state(value)
    assert actual['message'] == value['message']
    assert not (tmp_path/'jobs'/value['job_id']/'outcome.json').exists()


def test_recent_terminal_jobs_use_soft_headroom(tmp_path, monkeypatch):
    monkeypatch.setattr(ask, 'STATE_DIR', tmp_path)
    for number in range(200):
        value = receipt(tmp_path, number, 'ready')
        value['finished_at'] = time.time()
        ask.write_json(ask.job_path(value['job_id'])/'receipt.json', value)
    ask.make_room(tmp_path/'jobs')
    assert len(list((tmp_path/'jobs').iterdir())) == 200


def test_returned_result_has_retention_priority(tmp_path, monkeypatch):
    monkeypatch.setattr(ask, 'STATE_DIR', tmp_path)
    for number in range(200): receipt(tmp_path, number, 'ready')
    value = ask.read_job('ask_'+format(199,'032x'))
    ask.write_json(ask.job_path(value['job_id'])/'returned.json', {'result_returned_at':time.time()})
    ask.make_room(tmp_path/'jobs')
    assert not ask.job_path(value['job_id']).exists()
    assert ask.job_path('ask_'+format(0,'032x')).exists()


def test_stale_status_offers_same_job_resume(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ask, 'STATE_DIR', tmp_path)
    value=receipt(tmp_path, 1)
    ask.write_json(ask.job_path(value['job_id'])/'heartbeat.json', {'updated_at':time.time()-20})
    ask.show(value)
    data=json.loads(capsys.readouterr().out)
    assert '--resume '+value['job_id'] in data['next_command']
    assert 'stale' in data['message']


def test_retention_preserves_locked_terminal_job(tmp_path, monkeypatch):
    import fcntl, os
    monkeypatch.setattr(ask, 'STATE_DIR', tmp_path)
    for number in range(220):
        value=receipt(tmp_path, number, 'ready')
        value['finished_at']=time.time()
        ask.write_json(ask.job_path(value['job_id'])/'receipt.json', value)
    oldest=ask.job_path('ask_'+format(0,'032x'))
    ask.write_json(oldest/'returned.json', {'result_returned_at':time.time()})
    fd=os.open(oldest/'operation.lock',os.O_RDWR|os.O_CREAT,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(ask.ClientError, match='220-job limit'):ask.make_room(tmp_path/'jobs')
        assert oldest.exists()
    finally:os.close(fd)


@pytest.mark.parametrize('content,truncated', [(b'normal report\n', False), (b'a' * (200*1024), False), (b'a' * (200*1024+1), True), (b'a' * (200*1024-1) + '😀'.encode(), True)])
def test_result_stdout_is_bounded_utf8(tmp_path, monkeypatch, capsys, content, truncated):
    monkeypatch.setattr(ask, 'STATE_DIR', tmp_path)
    value=receipt(tmp_path, 1, 'ready')
    report=ask.job_path(value['job_id'])/'report.md';report.write_bytes(content)
    value.update(report_path=str(report), deliverables=[str(report)])
    assert ask.show(value, result=True)==0
    stdout=capsys.readouterr().out
    result=json.loads(stdout)
    assert len(result['report'].encode('utf-8')) <= 200*1024
    assert result['report_truncated'] is truncated
    assert '\ufffd' not in result['report']
    assert list(result).count('report')==1
    if not truncated:assert result['report']==content.decode()
