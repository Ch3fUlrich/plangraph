from plangraph.spawner.env_redact import OutputLines, unavailable_reason
from plangraph.spawner.exit_outcome import terminal_reason


def test_overflow_marker_once_per_line():
    lines = OutputLines(lambda text: text)
    assert lines.feed('x' * 65537) == '[autoos:overlong line withheld]\n'
    assert lines.feed('more') == ''
    assert lines.feed('still more') == ''
    assert lines.feed('\rhealthy\r') == 'healthy\r'


def test_stdout_host_marker_needs_error_shape():
    text = 'not_served_on_host: provider/model'
    assert unavailable_reason(1, text, '') is None
    assert unavailable_reason(1, 'Error: ' + text, '') == 'Error: ' + text
    assert unavailable_reason(1, '', text) == text


def test_terminal_reason_ignores_skipped_final_attempt():
    record = dict(status='failed', reason='harness_limit', exit_code=1,
                  attempts=[dict(status='failed', skip_reason='transport failed'),
                            dict(status='skipped')])
    terminal_reason(record, None)
    assert record['reason'] == 'transport failed'
