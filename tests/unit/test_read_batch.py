import json
import threading
from pathlib import Path
import pytest
from app.config.loader import use_workspace_root
from app.tools.builtin.files import _read_file
from app.tools.builtin.read_batch import read_files, READ_FILES
from app.tools.registry import Tool, ToolRegistry, ToolExecutionError
from app.agent.cancel import Cancelled
from app.agent.subagent_runtime import RestrictedToolRegistry
from app.agent.tool_batch import ToolCallBatch
from app.models.protocol import ToolCall


def test_nested_child_read_requests_overlap_and_reserve_budgets(tmp_path):
    barrier = threading.Barrier(3)
    def read(args):
        barrier.wait(timeout=2)
        return args['path']
    source = ToolRegistry()
    source.register(Tool('read_file', 'read', {'type': 'object'}, read, risk='read', parallel_safe=True))
    inner = RestrictedToolRegistry(source, {'read_file'}, workspace=tmp_path, max_calls=3)
    child = RestrictedToolRegistry(inner, {'read_file'}, workspace=tmp_path, max_calls=3)
    calls = [ToolCall(str(i), 'read_file', {'path': str(i)}) for i in range(3)]
    batch = ToolCallBatch(child, calls)
    for call in calls:
        assert batch.execute(call) == (call.arguments['path'], False)
    assert child.calls == inner.calls == 3
    assert len(child.read_signatures) == len(inner.read_signatures) == 3
    with pytest.raises(Cancelled):
        child.execute('read_file', {'path': 'extra'})


def test_parallel_does_not_reserve_a_partial_batch_when_path_is_denied(tmp_path):
    source = ToolRegistry()
    source.register(Tool('read_file', 'read', {'type': 'object'}, lambda args: 'ok', risk='read', parallel_safe=True))
    child = RestrictedToolRegistry(source, {'read_file'}, workspace=tmp_path)
    calls = [ToolCall('a', 'read_file', {'path': 'inside'}),
             ToolCall('b', 'read_file', {'path': '../outside'})]
    batch = ToolCallBatch(child, calls)
    assert batch.execute(calls[0]) == ('ok', False)
    assert batch.execute(calls[1])[1]
    assert child.calls == 1


def test_batch_reads_multiple_ranges_labels_and_errors(tmp_path):
    (tmp_path / 'a').write_text('one\ntwo\nthree\nfour\n')
    with use_workspace_root(tmp_path):
        result = json.loads(read_files({'requests': [
            {'path': 'a', 'offset': 1, 'limit': 1}, {'path': 'a', 'offset': 3, 'limit': 2}, {'path': 'missing'}]}))
    a, b, missing = result['files']
    assert a['content'] == 'one\n' and a['next_offset'] == 2
    assert b['content'] == 'three\nfour\n' and b['next_offset'] is None
    assert 'error' in missing and result['pending_indices'] == []


def test_batch_total_budget_and_explicit_continuations(tmp_path):
    for i in range(12):
        (tmp_path / str(i)).write_text(('abcd\t' * 20 + '\n') * 200)
    with use_workspace_root(tmp_path):
        output = read_files({'requests': [{'path': str(i)} for i in range(12)]})
    assert len(output) <= 9000
    result = json.loads(output)
    assert len(result['files']) + len(result['pending_indices']) == 12
    assert all(item['truncated'] and item['next_offset'] >= 1 for item in result['files'])


def test_batch_refuses_external_path_before_reading(tmp_path):
    with use_workspace_root(tmp_path), pytest.raises(ToolExecutionError, match='outside'):
        read_files({'requests': [{'path': '../outside'}]})


def test_child_batch_cannot_bypass_path_restrictions(tmp_path):
    source = ToolRegistry()
    source.register(READ_FILES)
    child = RestrictedToolRegistry(source, {'read_files'}, workspace=tmp_path)
    with use_workspace_root(tmp_path):
        result, error = child.execute('read_files', {'requests': [{'path': '../outside'}]})
    assert error and 'outside' in result and child.calls == 0


def test_page_seek_cache_avoids_rescanning_and_invalidates_after_replace(tmp_path, monkeypatch):
    path = tmp_path / 'a'
    path.write_bytes(''.join(f'{i}中文\r\n' for i in range(1000)).encode())
    original = Path.open
    reads = []
    class Reader:
        def __init__(self, fh): self.fh = fh
        def __enter__(self): self.fh.__enter__(); return self
        def __exit__(self, *args): return self.fh.__exit__(*args)
        def __getattr__(self, name): return getattr(self.fh, name)
        def readline(self): reads.append(1); return self.fh.readline()
    def opened(self, *args, **kwargs):
        fh = original(self, *args, **kwargs)
        return Reader(fh) if args and args[0] == 'r' else fh
    monkeypatch.setattr(Path, 'open', opened)
    with use_workspace_root(tmp_path):
        _read_file({'path': 'a', 'limit': 200})
        result = _read_file({'path': 'a', 'offset': 201, 'limit': 200})
        assert '200中文\r\n' in result and len(reads) < 450
        replacement = tmp_path / 'replacement'
        replacement.write_text('new\n' * 500)
        replacement.replace(path)
        assert 'new\n' in _read_file({'path': 'a', 'offset': 201, 'limit': 2})
