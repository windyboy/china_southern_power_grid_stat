# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Home Assistant custom integration for China Southern Power Grid (南方电网) electricity usage statistics. Fetches electricity consumption data, billing information, and ladder pricing from the CSG API for regions: Guangdong, Guangxi, Yunnan, Guizhou, and Hainan.

## Development Commands

**Validation:**
```bash
# Run Home Assistant hassfest validation (checks manifest.json, translations, etc.)
docker run --rm -v $(pwd):/github/workspace ghcr.io/home-assistant/hassfest

# Run HACS validation
docker run --rm -v $(pwd):/github/workspace ghcr.io/hacs/action
```

**Testing the CSG client standalone:**
```bash
cd custom_components/china_southern_power_grid_stat
python csg_client_demo.py
```

## Architecture

### Core Components

**`csg_client/`** - Standalone API client library for CSG's mobile app API
- `__init__.py`: `CSGClient` class implementing all API calls with AES/RSA encryption
- `const.py`: API constants, encryption keys, endpoint paths, response codes
- Can be used independently of Home Assistant (see `csg_client_demo.py`)

**Integration Files:**
- `__init__.py`: Entry setup/teardown, session validation, device removal
- `config_flow.py`: Multi-step login flows (SMS, SMS+password, QR code via WeChat/Alipay/CSG app)
- `sensor.py`: `CSGCoordinator` for data fetching + sensor entities (energy, cost, ladder)
- `const.py`: Integration constants, sensor suffixes, update thresholds

### Data Flow

1. **Login**: `CSGConfigFlow` handles authentication via `CSGClient` API methods
2. **Coordinator**: `CSGCoordinator` (in `sensor.py`) polls API at configurable intervals (default 4h)
3. **Sensors**: 16 sensor types per electricity account (balance, arrears, usage by day/month/year, ladder info)

### Key Design Patterns

- **Synchronous API client**: `CSGClient` uses `requests` (not async) - wrapped with `hass.async_add_executor_job()`
- **Session persistence**: Auth token stored in config entry data, validated on each setup
- **Conditional updates**: Last month/year data only updates during first few days of new periods to reduce API calls
- **Parallel fetching**: Multiple API calls run concurrently via `asyncio.gather()` in coordinator

### Login Types (LoginType enum)
- `LOGIN_TYPE_SMS`: Phone + SMS code only
- `LOGIN_TYPE_PWD_AND_SMS`: Phone + password + SMS code
- `LOGIN_TYPE_CSG_QR`, `LOGIN_TYPE_WX_QR`, `LOGIN_TYPE_ALI_QR`: QR code login

### API Encryption
- Request body: AES-CBC encrypted with hardcoded key/IV
- Password field: RSA public key encrypted
- Response: AES-CBC decrypted with same key/IV

## File Locations

- Integration code: `custom_components/china_southern_power_grid_stat/`
- Translations: `strings.json` (Chinese UI strings)
- Manifest: `manifest.json` (version, requirements: pycryptodome, brotli)
