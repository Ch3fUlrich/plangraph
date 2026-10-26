"""W29: registered harnesses must not silently execute as opencode."""
import os
import subprocess

import pytest

from plangraph.routing_support.clients import STDIN_PROMPT_HARNESSES
from plangraph.spawner.clients import build_command


def test_agy_harness_dispatch():
    assert build_command('hello', 'gemini-3.8-flash', client='agy') == [
        'agy', '--dangerously-skip-permissions', '--model',
        'gemini-3.8-flash', '-p', 'hello']


def test_opencode_harness_dispatch():
    assert build_command('hello', 'omniroute/cx-gpt-6.1-sol', client='opencode') == [
        'opencode', 'run', '--standalone', '--model', 'omniroute/cx-gpt-6.1-sol']


def test_unknown_harness_rejected():
    with pytest.raises(ValueError, match='unknown harness'):
        build_command('hello', client='not-a-harness')


def test_qodercli_harness_dispatch():
    assert build_command('hello', 'Qwen3.8-Flash', client='qodercli') == [
        'qodercli', '-p', 'hello', '--model', 'Qwen3.8-Flash']


@pytest.mark.parametrize('harness,model', [
    ('agy', 'gemini-3.8-flash'), ('opencode', 'omniroute/cx-gpt-6.1-sol')])
def test_unmocked_harness_popen(tmp_path, harness, model):
    binary = tmp_path / harness
    binary.write_text('#!/bin/sh\nprintf "%s\\n" "$0" "$@"\ncat\n')
    binary.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ['PATH'])
    stdin_prompt = harness in STDIN_PROMPT_HARNESSES
    with subprocess.Popen(build_command('hello', model, client=harness), env=env,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as child:
        stdout, stderr = child.communicate(b'hello\n' if stdin_prompt else b'', timeout=5)
    assert child.returncode == 0, stderr
    lines = stdout.decode().splitlines()
    assert lines[0] == str(binary)
    assert model in lines
    assert 'hello' in lines
