# CLAUDE.md

For agents working in this repository. User install steps, feature lists, screenshots, and encryption / packet-capture history live in `README.md`. Do not repeat them here.

Home Assistant custom integration for China Southern Power Grid (Guangdong, Guangxi, Yunnan, Guizhou, Hainan) usage and billing.

Fork of CubicPill. Current maintainer is `@windyboy`. Do not rewrite README / LICENSE credit. Do not erase the original author.

## Where to edit

| File | Role |
|------|------|
| `custom_components/china_southern_power_grid_stat/__init__.py` | Entry setup/unload, session check, device removal |
| `custom_components/china_southern_power_grid_stat/config.py` | Read IP family and refresh interval; request an address-family-bound HA `ClientSession` |
| `custom_components/china_southern_power_grid_stat/config_flow.py` | Network step, five login paths, options (add account / settings) |
| `custom_components/china_southern_power_grid_stat/sensor.py` | `CSGCoordinator`, `_SENSOR_DEFINITIONS`, entities |
| `custom_components/china_southern_power_grid_stat/const.py` | Suffixes, refresh-window thresholds, IP-family options and default |
| `custom_components/china_southern_power_grid_stat/csg_client/__init__.py` | `CSGClient` (usable without Home Assistant) |
| `custom_components/china_southern_power_grid_stat/csg_client/const.py` | `LoginType`, API constants |
| `custom_components/china_southern_power_grid_stat/csg_client_demo.py` | Standalone client demo |
| `custom_components/china_southern_power_grid_stat/strings.json` | Chinese UI source |
| `custom_components/china_southern_power_grid_stat/translations/zh-Hans.json` | Same values as `strings.json` |
| `custom_components/china_southern_power_grid_stat/translations/en.json` | English |
| `tests/` | Behavior tests; do not change them to accommodate a refactor |

Data flow: pick IP family → one of the five login paths → write token into the config entry → setup only verifies the session (expired login requires manual reauth; do not auto-relogin) → coordinator fetches on the refresh windows → `_SENSOR_DEFINITIONS` creates entities.

## Do not break

Write rules. Check the source files. Do not copy lists or numbers into this file.

- Do not drop a login path. The list is `LoginType` in `csg_client/const.py`.
- Do not rename suffixes, entity IDs, or device IDs, and do not change their formulas. Suffix constants live in the top-level `const.py`. The declaration table `_SENSOR_DEFINITIONS` lives in `sensor.py`.
- Change refresh windows only by editing the thresholds in the top-level `const.py`. The default interval is in that file too.
- Keep IP family a user option (`auto` / `ipv4` / `ipv6`). Do not hard-code a single family. Options and default live in the top-level `const.py`; session binding lives in `config.py`.
- Do not silently change CSG API request shapes. Implementation is in `csg_client/`.
- Do not change `tests/` to accommodate a refactor.
- Do not delete these lookalikes:
  - `csg_client_demo.py`: standalone client demo, not dead code.
  - Translation trio: this repo intentionally uses `strings.json` as the Chinese source, `zh-Hans.json` with the same values, and `en.json` for English.
  - QR help HTML placeholders: `strings.json` conflicts with HTML tags. See `description_placeholders` in `config_flow.py`.

When you change one place, change the coupled places too:

| You change | Also touch |
|------------|------------|
| New suffix | Top-level `const.py` + `_SENSOR_DEFINITIONS` in `sensor.py` (and the translation trio if copy is needed) |
| New login path | `LoginType` in `csg_client/const.py` + `config_flow.py` + the translation trio |
| Refresh window | Thresholds in the top-level `const.py` |

## How to test

Locally:

```bash
python -m pytest -q
```

The three test files are `tests/test_sensor_behavior.py`, `tests/test_config_options.py`, and `tests/test_csg_client_http.py`.

CI jobs: Tests, hassfest, HACS. Treat `.github/workflows/` as the source of truth for matrix and versions. Minimum Home Assistant version is in `README.md`.

Do not treat docker hassfest/HACS as the local default. Do not treat `csg_client_demo.py` as the default test.

## Traps

- `create_or_update_config_entry` still accepts an unused `password`. `tests/test_config_options.py` calls the four-argument form. Do not drop the argument to "clean unused parameters".
- The options menu labels `添加已绑定的缴费号` / `参数设置` and the interval check `刷新间隔不能低于60秒` are hardcoded in `config_flow.py`. They are not in the translation JSON. Do not pretend they live in `strings.json` when you edit that copy.

When you change a module, test, or rule this file points at, update only the matching section.
