.PHONY: refresh check-refresh test

refresh:
	python3 scripts/refresh.py

check-refresh:
	python3 scripts/refresh.py --check

test:
	python3 -m unittest discover -s tests -v
