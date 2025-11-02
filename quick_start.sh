#!/bin/bash

# Hummingbot Quick Start Script
# This script allows you to quickly start a strategy without entering the interactive CLI

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Default values
SCRIPT=""
CONF=""
PASSWORD=""
HEADLESS=false
SHOW_HELP=false

# Function to display usage
usage() {
    echo -e "${GREEN}Hummingbot Quick Start Script${NC}"
    echo ""
    echo "Usage: ./quick_start.sh [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  -s, --script SCRIPT        Script file name (e.g., v2_with_controllers.py) [REQUIRED]"
    echo "  -c, --conf CONF           Configuration file (e.g., conf_xemm_perpetual_example.yml)"
    echo "  -p, --password PASSWORD   Password (or use HUMMINGBOT_PASSWORD env var)"
    echo "  -H, --headless           Run in headless mode (no UI)"
    echo "  -h, --help               Show this help message"
    echo ""
    echo "Examples:"
    echo "  # With UI mode (will prompt for password if not provided):"
    echo "  ./quick_start.sh -s v2_with_controllers.py -c conf_xemm_perpetual_example.yml"
    echo ""
    echo "  # With password in command:"
    echo "  ./quick_start.sh -s v2_with_controllers.py -c conf_xemm_perpetual_example.yml -p mypassword"
    echo ""
    echo "  # With password from environment variable:"
    echo "  export HUMMINGBOT_PASSWORD=mypassword"
    echo "  ./quick_start.sh -s v2_with_controllers.py -c conf_xemm_perpetual_example.yml"
    echo ""
    echo "  # Headless mode (no UI, runs in background):"
    echo "  ./quick_start.sh -s v2_with_controllers.py -c conf_xemm_perpetual_example.yml -p mypassword -H"
    echo ""
    exit 0
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -s|--script)
            SCRIPT="$2"
            shift 2
            ;;
        -c|--conf)
            CONF="$2"
            shift 2
            ;;
        -p|--password)
            PASSWORD="$2"
            shift 2
            ;;
        -H|--headless)
            HEADLESS=true
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            usage
            ;;
    esac
done

# Check if script is provided
if [ -z "$SCRIPT" ]; then
    echo -e "${RED}Error: Script file is required${NC}"
    echo ""
    usage
fi

# Check if script file exists
if [ ! -f "scripts/$SCRIPT" ]; then
    echo -e "${RED}Error: Script file 'scripts/$SCRIPT' not found${NC}"
    exit 1
fi

# Check if conf file exists (if provided)
if [ -n "$CONF" ] && [ ! -f "conf/scripts/$CONF" ]; then
    echo -e "${RED}Error: Config file 'conf/scripts/$CONF' not found${NC}"
    exit 1
fi

# Use environment variable if password not provided
if [ -z "$PASSWORD" ] && [ -n "$HUMMINGBOT_PASSWORD" ]; then
    PASSWORD="$HUMMINGBOT_PASSWORD"
fi

# Build command
CMD="./bin/hummingbot_quickstart.py -f $SCRIPT"

if [ -n "$CONF" ]; then
    CMD="$CMD -c $CONF"
fi

if [ -n "$PASSWORD" ]; then
    CMD="$CMD -p $PASSWORD"
fi

if [ "$HEADLESS" = true ]; then
    CMD="$CMD --headless"
fi

# Display info
echo -e "${GREEN}Starting Hummingbot...${NC}"
echo -e "Script: ${YELLOW}$SCRIPT${NC}"
if [ -n "$CONF" ]; then
    echo -e "Config: ${YELLOW}$CONF${NC}"
fi
if [ "$HEADLESS" = true ]; then
    echo -e "Mode: ${YELLOW}Headless${NC}"
else
    echo -e "Mode: ${YELLOW}UI${NC}"
fi
echo ""

# Run command
eval $CMD
