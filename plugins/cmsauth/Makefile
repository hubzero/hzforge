# Run the test suite under stock Rocky 8 Pythons (no venv, no tox).
# See plugins/mysqlauthz/Makefile for the why-no-tox reasoning.
#
# Setup once per host (cmsauth has no third-party deps -- just pytest):
#   python3 -m pip install --user 'pytest>=7,<7.1'
#   python2 -m pip install --user 'pytest<5'
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
