PYTHON ?= python3
APP_NAME ?= tokenbudget
MAIN_SCRIPT := desktop/tokenbudget_qt.py
BUILD_DIR := build/nuitka
DIST_DIR := dist
NPROC := $(shell nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)
NUITKA := $(PYTHON) -m nuitka
NUITKA_FLAGS := \
	--onefile \
	--assume-yes-for-downloads \
	--enable-plugin=pyside6 \
	--jobs=$(NPROC) \
	--lto=yes \
	--output-dir=$(BUILD_DIR) \
	--output-filename=$(APP_NAME) \
	--remove-output \
	--include-package-data=dateparser \
	--include-module=desktop.tokenbudget_snapshot \
	--include-module=desktop.tokenbudget_config \
	--include-module=claude_usage_costs \
	--include-module=cursor_agent_usage_costs \
	--include-module=gemini_usage_costs \
	--include-module=_json

.PHONY: build-deps onefile clean

build-deps:
	$(PYTHON) -m pip install --user nuitka ordered-set zstandard

onefile:
	mkdir -p "$(BUILD_DIR)" "$(DIST_DIR)"
	$(NUITKA) $(NUITKA_FLAGS) "$(MAIN_SCRIPT)"
	out=""; \
	for candidate in "$(BUILD_DIR)/$(APP_NAME)" "$(BUILD_DIR)/$(APP_NAME).bin"; do \
		if [ -f "$$candidate" ]; then \
			out="$$candidate"; \
			break; \
		fi; \
	done; \
	if [ -z "$$out" ]; then \
		echo "Unable to locate built executable in $(BUILD_DIR)" >&2; \
		exit 1; \
	fi; \
	cp "$$out" "$(DIST_DIR)/$(APP_NAME)"; \
	chmod +x "$(DIST_DIR)/$(APP_NAME)"

clean:
	rm -rf "$(BUILD_DIR)" "$(DIST_DIR)"
