.DEFAULT_GOAL := help

TF_DIR      := terraform
LAMBDA_DIR  := lambda
PYTHON      ?= python3

# Backend settings are supplied at init time. Export these, or pass them on the
# command line: make tf-init TF_STATE_BUCKET=... TF_STATE_KEY=...
TF_STATE_BUCKET     ?=
TF_STATE_KEY        ?= ai-incident-summarizer/dev/terraform.tfstate
TF_STATE_LOCK_TABLE ?=
AWS_REGION          ?= us-east-1

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Terraform
# ---------------------------------------------------------------------------

.PHONY: tf-init
tf-init: ## Initialize Terraform against the remote backend
	@test -n "$(TF_STATE_BUCKET)" || { echo "TF_STATE_BUCKET is required"; exit 1; }
	terraform -chdir=$(TF_DIR) init -input=false \
		-backend-config="bucket=$(TF_STATE_BUCKET)" \
		-backend-config="key=$(TF_STATE_KEY)" \
		-backend-config="region=$(AWS_REGION)" \
		-backend-config="dynamodb_table=$(TF_STATE_LOCK_TABLE)"

.PHONY: tf-init-local
tf-init-local: ## Initialize without a backend (validation only)
	terraform -chdir=$(TF_DIR) init -backend=false -input=false

.PHONY: fmt
fmt: ## Rewrite Terraform files to canonical format
	terraform -chdir=$(TF_DIR) fmt -recursive

.PHONY: validate
validate: tf-init-local ## Check formatting and validate the configuration
	terraform -chdir=$(TF_DIR) fmt -check -recursive -diff
	terraform -chdir=$(TF_DIR) validate

.PHONY: validate-modules
validate-modules: ## Validate every module in isolation
	@set -e; for m in $(TF_DIR)/modules/*/; do \
		echo "--> $$m"; \
		terraform -chdir=$$m init -backend=false -input=false >/dev/null; \
		terraform -chdir=$$m validate; \
	done

.PHONY: plan
plan: ## Show pending infrastructure changes
	terraform -chdir=$(TF_DIR) plan -input=false

.PHONY: apply
apply: ## Apply infrastructure changes
	terraform -chdir=$(TF_DIR) apply -input=false

# ---------------------------------------------------------------------------
# Lambda
# ---------------------------------------------------------------------------

.PHONY: install
install: ## Install development dependencies
	$(PYTHON) -m pip install -r $(LAMBDA_DIR)/requirements-dev.txt

.PHONY: lint
lint: ## Lint and format-check the Lambda source
	ruff check $(LAMBDA_DIR)
	ruff format --check $(LAMBDA_DIR)

.PHONY: format
format: ## Auto-format the Lambda source
	ruff format $(LAMBDA_DIR)
	ruff check --fix $(LAMBDA_DIR)

.PHONY: test
test: ## Run the test suite
	pytest $(LAMBDA_DIR)/tests -v

.PHONY: coverage
coverage: ## Run tests with a coverage report
	pytest $(LAMBDA_DIR)/tests --cov=$(LAMBDA_DIR) --cov-report=term-missing

.PHONY: package
package: ## Build the deployment package locally (arm64)
	rm -rf build deployment-package.zip
	mkdir -p build
	pip install -r $(LAMBDA_DIR)/requirements.txt --target build \
		--platform manylinux2014_aarch64 --implementation cp \
		--python-version 3.13 --only-binary=:all: --upgrade
	cp $(LAMBDA_DIR)/*.py build/
	rm -rf build/tests
	cd build && zip -qrX ../deployment-package.zip . -x '*.pyc' '*__pycache__*'
	@echo "Built deployment-package.zip ($$(du -h deployment-package.zip | cut -f1))"

.PHONY: check
check: validate lint test ## Run everything CI runs

.PHONY: clean
clean: ## Remove build artifacts and caches
	rm -rf build deployment-package.zip .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
