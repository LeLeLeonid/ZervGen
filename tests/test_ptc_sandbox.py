import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.base_agent import StemAgent


def _agent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    agent = StemAgent(name="system", provider=None, skill_name="system", mode="BUILD")
    agent._silent = True
    agent._set_status = lambda *args, **kwargs: None
    agent._log = lambda *args, **kwargs: None
    return agent


def _blocked(agent, code):
    return agent._validate_ptc(code) is not None


def test_import_is_blocked(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)
    assert _blocked(agent, "import os\nos.system('id')")


def test_import_after_first_node_is_blocked(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)
    assert _blocked(agent, "x = 1\ny = 2\nimport os")


def test_private_attribute_is_blocked(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)
    assert _blocked(agent, "json.__builtins__['__import__']('os').system('id')")
    assert _blocked(agent, "print.__self__")


def test_dunder_class_chain_is_blocked(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)
    assert _blocked(agent, "().__class__.__base__.__subclasses__()")
    assert _blocked(agent, "type(1).__mro__")


def test_escape_helpers_are_blocked(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)
    assert _blocked(agent, "getattr(object, 'x')")
    assert _blocked(agent, "open('/etc/passwd').read()")
    assert _blocked(agent, "__import__('os')")


def test_format_field_traversal_is_blocked(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)
    assert _blocked(agent, "'{0.__globals__[__builtins__]}'.format(json.dumps)")
    assert _blocked(agent, "'{0.__class__.__mro__}'.format(1)")
    # splitting the field so the literal check cannot see it must still fail
    assert _blocked(agent, "('{' + '0.__class__' + '}').format(1)")
    assert _blocked(agent, "'%s %s'.format_map({'a': 1})")


def test_normal_script_passes(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)
    assert agent._validate_ptc("data = json.dumps({'a': 1})\nprint(len(data))") is None


def test_syntax_error_is_reported(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)
    error = agent._validate_ptc("def broken(:\n    pass")
    assert error and error.startswith("Syntax Error")


def test_execution_rejects_escape(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)
    output = asyncio.run(agent._run_code("import os", ""))
    assert output.startswith("PTC blocked")


def test_execution_prints_and_returns(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)
    output = asyncio.run(agent._run_code("print('hi')\nreturn 2 + 2", ""))
    assert output == "hi\n4"


def test_json_namespace_exposes_only_data_helpers(monkeypatch, tmp_path):
    agent = _agent(monkeypatch, tmp_path)
    output = asyncio.run(agent._run_code("return json.dumps({'k': 1})", ""))
    assert output == '{"k": 1}'
    rejected = asyncio.run(agent._run_code("return json.__builtins__", ""))
    assert rejected.startswith("PTC blocked")
