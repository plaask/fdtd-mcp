# -*- coding: utf-8 -*-
"""Run-log diagnostics (no engine needed)."""
from fdtd_mcp.handlers.session import (
    _autoshutoff_from_line,
    _parse_engine_log,
    _redact_log_line,
)


DIVERGING_LOG = """\
2026-01-01 00:00:00 Info: initializing
2026-01-01 00:00:01 Progress: 4677/68573 time steps
2026-01-01 00:00:02 ERROR: Early termination of simulation, the electromagnetic fields are diverging.
2026-01-01 00:00:02 Auto Shutoff from 0.02 to 5108
2026-01-01 00:00:02 WARNING: wrote C:\\Users\\alice\\proj\\out.fsp
"""


def test_parse_diverging_log(tmp_path):
    p = tmp_path / 'sim_p0.log'
    p.write_text(DIVERGING_LOG, encoding='utf-8')
    d = _parse_engine_log(str(p))
    assert d['log_found'] is True
    assert d['diverged'] is True
    assert d['early_termination'] is True
    assert d['autoshutoff'] == 5108.0
    assert d['step'] == 4677 and d['total_steps'] == 68573
    assert any('diverging' in line for line in d['errors'])
    assert any('<user>' in line for line in d['warnings'])
    assert d['log_tail']


def test_parse_normal_autoshutoff(tmp_path):
    p = tmp_path / 'sim_p0.log'
    p.write_text('Auto Shutoff level 8.2e-06\n', encoding='utf-8')
    d = _parse_engine_log(str(p))
    assert d['diverged'] is False
    assert d['autoshutoff'] == 8.2e-06


def test_parse_iteration_wording(tmp_path):
    p = tmp_path / 'sim_p0.log'
    p.write_text('Starting 3147 total iterations\n'
                 'Completed 101 iterations, or 6.4e-15s of Simulation Time\n',
                 encoding='utf-8')
    d = _parse_engine_log(str(p))
    assert d['step'] == 101
    assert d['total_steps'] == 3147


def test_autoshutoff_prefers_from_to_target():
    assert _autoshutoff_from_line('Auto Shutoff from 0.02 to 5108') == 5108.0
    assert _autoshutoff_from_line('autoshutoff level: 1e-05') == 1e-05
    assert _autoshutoff_from_line('nothing here') is None


def test_redact_user_name():
    assert (_redact_log_line(r'open C:\Users\alice\proj\a.fsp failed')
            == r'open C:\Users\<user>\proj\a.fsp failed')
    assert _redact_log_line('no path') == 'no path'
