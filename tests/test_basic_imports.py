import importlib


def test_import_agentseal():
    mod = importlib.import_module("agentseal")
    assert hasattr(mod, "__version__")


def test_cli_imports():
    importlib.import_module("agentseal.cli")


def test_report_imports():
    importlib.import_module("agentseal.report")
