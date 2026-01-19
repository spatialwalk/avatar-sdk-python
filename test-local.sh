#!/bin/bash
# Local testing script for avatarkit
# Tests across multiple Python versions and dependency combinations

set -e

echo "==================================="
echo "Avatar SDK Python - Local Test Runner"
echo "==================================="
echo ""

# Ensure dependencies are installed
echo "Ensuring tox is installed..."
uv sync --group dev

# Function to show usage
show_usage() {
    echo "Usage: ./test-local.sh [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  all              - Test all Python versions with all dependency combinations"
    echo "  py39             - Test Python 3.9 only"
    echo "  py310            - Test Python 3.10 only"
    echo "  py311            - Test Python 3.11 only"
    echo "  py312            - Test Python 3.12 only"
    echo "  py313            - Test Python 3.13 only"
    echo "  min              - Test with minimum dependency versions (all Python versions)"
    echo "  latest           - Test with latest dependency versions (all Python versions)"
    echo "  quick            - Test current Python version only"
    echo ""
    echo "Examples:"
    echo "  ./test-local.sh all          # Run all tests"
    echo "  ./test-local.sh py311        # Test Python 3.11 only"
    echo "  ./test-local.sh min          # Test minimum deps on all Python versions"
    echo "  ./test-local.sh quick        # Quick test on current Python"
}

# Parse arguments
case "${1:-quick}" in
    all)
        echo "Running tests on all Python versions with all dependency combinations..."
        uv run tox
        ;;
    py39|py310|py311|py312|py313)
        echo "Running tests for $1..."
        uv run tox -e $1,$1-min,$1-latest
        ;;
    min)
        echo "Running tests with minimum dependency versions..."
        uv run tox -e py39-min,py310-min,py311-min,py312-min,py313-min
        ;;
    latest)
        echo "Running tests with latest dependency versions..."
        uv run tox -e py39-latest,py310-latest,py311-latest,py312-latest,py313-latest
        ;;
    quick)
        echo "Running quick test on current Python version..."
        uv run pytest
        ;;
    help|--help|-h)
        show_usage
        exit 0
        ;;
    *)
        echo "Unknown option: $1"
        echo ""
        show_usage
        exit 1
        ;;
esac

echo ""
echo "==================================="
echo "Tests completed successfully!"
echo "==================================="
