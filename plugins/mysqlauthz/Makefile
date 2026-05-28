# Run the test suite under stock Rocky 8 Pythons (no venv, no tox).
#
# Why no tox: tox 4 + virtualenv 21+ uses an interpreter-discovery probe
# written in Py3.8+ syntax (the `:=` walrus operator), so it can't drive
# Py3.6 (Rocky 8's stock /usr/bin/python3) or Py2.7 (the production Trac
# interpreter today).  This Makefile sidesteps the issue by invoking each
# system Python's own `-m pytest` directly.  pytest itself + the plugin's
# pyproject `[tool.pytest.ini_options] pythonpath = ["src"]` handle the
# rest (no install needed; tests find the source via that pythonpath).
#
# Setup once per host:
#   sudo dnf install python3 python2                                  # Rocky 8 base
#   python3 -m pip install --user 'pytest>=7,<7.1' 'pymysql>=1.0,<1.1'  # Py3.6 caps
#   python2 -m pip install --user 'pytest<5' 'pymysql<1.0' configparser
#
# Run:
#   make            # = make test = make test-py3 (Py3.6)
#   make test-py2   # Py2.7
#   make test-all   # both

.PHONY: test test-py3 test-py2 test-all

test: test-py3

test-py3:
	python3 -m pytest

test-py2:
	python2 -m pytest

test-all: test-py3 test-py2
