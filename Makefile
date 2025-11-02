.ONESHELL:
.PHONY: test
.PHONY: run_coverage
.PHONY: report_coverage
.PHONY: development-diff-cover
.PHONY: docker
.PHONY: install
.PHONY: uninstall
.PHONY: clean
.PHONY: build
.PHONY: run-v2
.PHONY: run-strategy
.PHONY: run-headless

test:
	coverage run -m pytest \
 	--ignore="test/mock" \
 	--ignore="test/hummingbot/connector/exchange/ndax/" \
 	--ignore="test/hummingbot/connector/derivative/dydx_v4_perpetual/" \
 	--ignore="test/hummingbot/remote_iface/" \
 	--ignore="test/connector/utilities/oms_connector/" \
 	--ignore="test/hummingbot/strategy/amm_arb/" \
 	--ignore="test/hummingbot/strategy/cross_exchange_market_making/" \

run_coverage: test
	coverage report
	coverage html

report_coverage:
	coverage report
	coverage html

development-diff-cover:
	coverage xml
	diff-cover --compare-branch=origin/development coverage.xml

docker:
	git clean -xdf && make clean && docker build -t hummingbot/hummingbot${TAG} -f Dockerfile .

clean:
	./clean

install:
	./install

uninstall:
	./uninstall

build:
	./compile

run-v2:
	./bin/hummingbot_quickstart.py -p a -f v2_with_controllers.py -c $(filter-out $@,$(MAKECMDGOALS))

# Quick run strategy with password from env or prompt
# Usage: make run-strategy SCRIPT=v2_with_controllers.py CONF=conf_xemm_perpetual_example.yml PASSWORD=your_password
# Or with headless mode: make run-strategy SCRIPT=v2_with_controllers.py CONF=conf_xemm_perpetual_example.yml PASSWORD=your_password HEADLESS=true
run-strategy:
	@if [ -z "$(SCRIPT)" ]; then \
		echo "Error: SCRIPT parameter is required. Usage: make run-strategy SCRIPT=v2_with_controllers.py CONF=conf_xemm_perpetual_example.yml"; \
		exit 1; \
	fi; \
	if [ -n "$(PASSWORD)" ]; then \
		if [ "$(HEADLESS)" = "true" ]; then \
			./bin/hummingbot_quickstart.py -p $(PASSWORD) -f $(SCRIPT) $(if $(CONF),-c $(CONF)) --headless; \
		else \
			./bin/hummingbot_quickstart.py -p $(PASSWORD) -f $(SCRIPT) $(if $(CONF),-c $(CONF)); \
		fi \
	elif [ -n "$$HUMMINGBOT_PASSWORD" ]; then \
		if [ "$(HEADLESS)" = "true" ]; then \
			./bin/hummingbot_quickstart.py -p $$HUMMINGBOT_PASSWORD -f $(SCRIPT) $(if $(CONF),-c $(CONF)) --headless; \
		else \
			./bin/hummingbot_quickstart.py -p $$HUMMINGBOT_PASSWORD -f $(SCRIPT) $(if $(CONF),-c $(CONF)); \
		fi \
	else \
		if [ "$(HEADLESS)" = "true" ]; then \
			./bin/hummingbot_quickstart.py -f $(SCRIPT) $(if $(CONF),-c $(CONF)) --headless; \
		else \
			./bin/hummingbot_quickstart.py -f $(SCRIPT) $(if $(CONF),-c $(CONF)); \
		fi \
	fi

# Quick run in headless mode (background)
# Usage: make run-headless SCRIPT=v2_with_controllers.py CONF=conf_xemm_perpetual_example.yml PASSWORD=your_password
run-headless:
	@$(MAKE) run-strategy SCRIPT=$(SCRIPT) CONF=$(CONF) PASSWORD=$(PASSWORD) HEADLESS=true

%:
	@:
