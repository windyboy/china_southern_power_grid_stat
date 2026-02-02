# 南方电网Home Assistant集成代码审查报告

**审查日期**: 2026-02-02
**审查版本**: v1.2.0
**审查者视角**: 资深Home Assistant Python开发者
**审查范围**: 完整集成代码库

---

## 执行摘要

### 总体评分: 6.0/10

这是一个**功能完整、架构合理**的Home Assistant自定义集成，展现了对HA开发模式的良好理解。代码实现了复杂的多步认证流程、智能数据缓存策略和并发API调用优化。然而，存在一些**关键的安全隐患、错误处理不足和代码质量问题**需要改进。

### 关键优势 ✅
- ✅ 完整遵循Home Assistant架构模式（ConfigFlow、DataUpdateCoordinator、CoordinatorEntity）
- ✅ 智能缓存策略减少不必要的API调用
- ✅ 使用asyncio.gather实现并发数据获取，性能优化良好
- ✅ 支持多种登录方式（SMS、密码+SMS、三种二维码）
- ✅ 完善的设备和实体管理

### 关键问题 ⚠️
- 🔴 **严重**: 密码明文存储在config entry中
- 🔴 **严重**: API响应解析缺少None检查，存在崩溃风险
- 🟡 **重要**: 缺少重试机制和速率限制
- 🟡 **重要**: 硬编码中文字符串，国际化支持不足
- 🟡 **重要**: 完全缺少单元测试和集成测试

---

## 详细审查

### 1. 架构与设计 (8/10)

#### 优点
- **标准HA模式**: 正确使用ConfigFlow、OptionsFlow、DataUpdateCoordinator和CoordinatorEntity
- **关注点分离**: `csg_client/`作为独立API客户端库，可复用性强
- **设备模型**: 每个电费账户作为独立设备，16个传感器实体，符合HA设备建模最佳实践
- **异步包装**: 正确使用`async_add_executor_job`包装同步API调用

#### 问题
1. **Coordinator职责过重** (sensor.py:334-1018)
   - `CSGCoordinator`类超过600行，包含8个更新方法
   - 违反单一职责原则，建议拆分为独立的数据获取服务类

2. **unique_id设计缺陷** (sensor.py:227-228)
   ```python
   def unique_id(self) -> str | None:
       return f"{DOMAIN}.{self._account_number}.{self._entity_suffix}"
   ```
   - 缺少`config_entry_id`，可能导致跨账户冲突
   - 建议: `f"{DOMAIN}.{config_entry_id}.{self._account_number}.{self._entity_suffix}"`

3. **状态管理混乱**
   - 使用字符串常量`STATE_UPDATE_UNCHANGED`混入数据流
   - 应使用专门的数据类或TypedDict来表示更新状态

---

### 2. 安全性 (4/10) 🔴

#### 严重问题

**2.1 密码明文存储** (config_flow.py:329-330)
```python
data = {
    CONF_USERNAME: username,
    CONF_PASSWORD: password,  # ⚠️ 明文存储
    CONF_LOGIN_TYPE: login_type,
    CONF_AUTH_TOKEN: auth_token,
}
```
- **风险**: config entry数据存储在`.storage/core.config_entries`，密码可被直接读取
- **影响**: 如果系统被入侵，攻击者可获取用户南方电网账户密码
- **修复建议**:
  - 仅存储`auth_token`，删除密码存储
  - 如必须存储，使用HA的`async_get_secret()`加密存储
  - 参考官方集成如`homeassistant/components/nest/`的做法

**2.2 硬编码加密密钥** (csg_client/const.py)
- RSA公钥和AES密钥硬编码在代码中
- 虽然这是API要求，但应添加注释说明安全模型

**2.3 缺少输入验证**
- 手机号仅验证长度，未验证格式 (config_flow.py:112-114)
- API响应数据未验证类型和边界

---

### 3. 错误处理 (5/10) 🟡

#### 主要问题

**3.1 API响应解析缺少None检查**

sensor.py:631-654中存在多处潜在崩溃点:
```python
if success_cost:
    (
        this_month_cost,
        this_month_kwh_from_cost,
        ladder,
        this_month_by_day_from_cost,
    ) = result_cost
    # ⚠️ 未检查ladder是否为None
    ladder_stage = (
        ladder[WF_ATTR_LADDER]  # 如果ladder=None会崩溃
        if ladder[WF_ATTR_LADDER] is not None
        else STATE_UNAVAILABLE
    )
```

**修复建议**:
```python
if success_cost and result_cost:
    this_month_cost, this_month_kwh_from_cost, ladder, this_month_by_day_from_cost = result_cost
    if ladder and isinstance(ladder, dict):
        ladder_stage = ladder.get(WF_ATTR_LADDER) or STATE_UNAVAILABLE
    else:
        ladder_stage = STATE_UNAVAILABLE
```

**3.2 缺少重试机制**
- 网络请求失败直接返回`STATE_UNAVAILABLE`
- 应实现指数退避重试，特别是对于临时网络故障

**3.3 异常处理过于宽泛**
```python
except Exception as err:  # pylint: disable=broad-except
    _LOGGER.error("Unexpected exception: %s", err)
```
- 捕获所有异常会隐藏真实问题
- 应区分可恢复错误和致命错误

**3.4 JSON解析不健壮** (csg_client/__init__.py:254-256)
```python
json_str = response.content.decode("utf-8", errors="ignore")
json_str = json_str[json_str.find("{") : json_str.rfind("}") + 1]
json_data = json.loads(json_str)
```
- 使用字符串切片解析JSON非常危险
- 如果响应格式变化会导致解析失败
- 建议直接使用`response.json()`

---

### 4. 代码质量 (6/10)

#### 问题清单

**4.1 硬编码字符串** (config_flow.py:112-114, 272-273)
```python
vol.All(str, vol.Length(min=11, max=11), msg="请输入11位手机号")
# ...
"description": f"<p>使用{LOGIN_TYPE_TO_QR_APP_NAME[login_type]}扫码登录..."
```
- 中文字符串应移至`strings.json`
- HTML内容应使用模板系统

**4.2 魔法数字**
```python
if this_day <= SETTING_LAST_MONTH_UPDATE_DAY_THRESHOLD:  # 3
if this_day <= SETTING_LAST_YEAR_UPDATE_DAY_THRESHOLD:   # 7
```
- 虽然定义了常量，但缺少文档说明为何选择这些值

**4.3 复杂的条件逻辑** (sensor.py:808-844)
- `_update_latest_day`方法嵌套过深
- 建议提取为独立的辅助函数

**4.4 不一致的命名**
- `ele_account` vs `electricity_account`
- `kwh` vs `usage` vs `energy`
- 应统一术语

**4.5 缺少类型注解**
- 许多函数缺少返回类型注解
- `_async_fetch`返回类型应为`tuple[bool, Any]`

---


### 5. 性能与效率 (7/10)

#### 优点
- ✅ 使用`asyncio.gather`并发获取多个API数据
- ✅ 智能缓存策略：上月数据仅在月初3天更新，去年数据仅在1月前7天更新
- ✅ 使用`async_timeout`防止请求挂起
- ✅ `should_poll = False`配合coordinator，避免不必要的轮询

#### 问题

**5.1 缺少速率限制**
- 没有API调用频率控制
- 如果用户设置过短的更新间隔（最小60秒），可能触发API限流

**5.2 不必要的Event同步** (sensor.py:362, 706, 714)
```python
self._this_month_update_completed_flag = asyncio.Event()
# ...
await self._this_month_update_completed_flag.wait()
```
- 使用Event在协程间同步增加复杂度
- 可以通过调整`asyncio.gather`的任务顺序来避免

**5.3 重复的配置读取**
```python
config_entry = hass.config_entries.async_get_entry(self._config_entry_id)
```
- 在coordinator中多次调用，应缓存结果

---

### 6. 可维护性 (6/10)

#### 问题

**6.1 完全缺少测试**
- 无单元测试
- 无集成测试
- 无测试覆盖率报告
- **建议**: 至少添加coordinator和API客户端的单元测试

**6.2 文档不足**
- 函数缺少docstring说明参数和返回值
- 复杂逻辑缺少注释
- README.md缺少故障排查指南

**6.3 日志记录不一致**
- 有些地方使用`_LOGGER.debug`，有些使用`_LOGGER.info`
- 缺少结构化日志（如用户ID、账户号）
- 敏感信息可能泄露到日志中

**6.4 配置管理混乱**
```python
self._config = config_entry.data
# ...
new_config = self._config.copy()
```
- 直接修改config entry data，应使用不可变数据结构

---

### 7. Home Assistant集成规范 (7/10)

#### 符合规范
- ✅ 使用config flow而非YAML配置
- ✅ 正确实现reauth流程
- ✅ 支持options flow修改配置
- ✅ 正确的设备和实体注册
- ✅ manifest.json配置完整

#### 不符合规范

**7.1 实体命名** (sensor.py:231-232)
```python
def name(self) -> str | None:
    return f"{self._account_number}-{self._entity_suffix}"
```
- 应使用`_attr_has_entity_name = True`和`_attr_name`
- 设备名称应该是账户号，实体名称应该是传感器类型

**7.2 缺少翻译**
- `strings.json`存在但未被充分使用
- 错误消息、实体名称应支持多语言

**7.3 设备信息不完整** (sensor.py:239-246)
```python
return DeviceInfo(
    identifiers={(DOMAIN, self._account_number)},
    name=f"CSGAccount-{self._account_number}",
    manufacturer="CSG",
    model="CSG Virtual Electricity Meter",
)
```
- 缺少`sw_version`（可以使用API版本）
- 缺少`configuration_url`（可以链接到南方电网官网）

**7.4 状态类使用不当**
```python
_attr_state_class = SensorStateClass.TOTAL
```
- 余额、欠费不应使用`TOTAL`，应使用`MEASUREMENT`
- 阶梯电价传感器不应有state_class


### 8. 具体代码问题

#### 8.1 config_flow.py

**问题1: 缺少unique_id验证** (line 315-320)
```python
async def check_and_set_unique_id(self, username: str):
    # TODO: username (mobile) may not be the best unique id
    unique_id = f"CSG-{username}"
    await self.async_set_unique_id(unique_id)
    self._abort_if_unique_id_configured()
```
- TODO注释说明了问题但未解决
- 如果用户更换手机号，会创建新的config entry
- 建议使用CSG账户的custNumber作为unique_id

**问题2: QR码轮询无超时** (line 280-313)
- 用户点击"下一步"后立即检查QR码状态
- 如果未扫码，返回同一页面让用户再次点击
- 应实现自动轮询或WebSocket推送

**问题3: 错误信息处理不当** (line 183-185)
```python
description_placeholders={"error_detail": error_detail}
```
- 将异常详情直接显示给用户
- 可能暴露内部实现细节或敏感信息

#### 8.2 sensor.py

**问题1: Coordinator初始化时机** (line 337-343)
```python
def __init__(self, hass: HomeAssistant, config_entry_id: str) -> None:
    self._config_entry_id = config_entry_id
    config_entry = hass.config_entries.async_get_entry(self._config_entry_id)
    if config_entry is None:
        raise ValueError(f"Config entry {self._config_entry_id} not found")
    self._config = config_entry.data
```
- 在`__init__`中访问config entry可能导致竞态条件
- 应在`_async_update_data`中动态获取

**问题2: 数据合并逻辑复杂** (line 553-595)
```python
@staticmethod
def merge_by_day_data(
    by_day_from_cost: list | str,
    kwh_from_cost: float | str,
    by_day_from_usage: list | str,
    kwh_from_usage: float | str,
) -> (list | str, float | str):
```
- 混合使用list和str类型（STATE_UNAVAILABLE）
- 应使用Optional[list]和Optional[float]
- 返回类型注解不正确，应为`tuple[list | str, float | str]`

**问题3: 状态更新逻辑** (line 249-307)
```python
def _handle_coordinator_update(self) -> None:
    if not self._coordinator.data:
        self._attr_available = False
        self.async_write_ha_state()
        return
    # ... 多层嵌套检查
```
- 过多的None检查和嵌套
- 建议使用早期返回模式简化

**问题4: 时间计算** (line 855-871)
```python
def _update_states(self):
    current_dt = datetime.datetime.now()
    this_year, this_month, this_day = (
        current_dt.year,
        current_dt.month,
        current_dt.day,
    )
```
- 使用`datetime.datetime.now()`而非`dt_util.now()`
- 未考虑时区问题

#### 8.3 csg_client/__init__.py

**问题1: 会话管理** (line 197-210)
```python
self._session: requests.Session = requests.Session()
```
- Session对象未正确关闭
- 应实现`__enter__`和`__exit__`或提供`close()`方法

**问题2: 异常层次混乱**
```python
class CSGAPIError(Exception)
class CSGHTTPError(CSGAPIError)
class InvalidCredentials(CSGAPIError)
class NotLoggedIn(CSGAPIError)
```
- 异常类定义合理，但使用时未充分区分
- 某些地方捕获Exception而非特定异常

**问题3: 加密实现** (line 79-105)
```python
def encrypt_params(params: dict) -> str:
    json_cipher = AES.new(PARAM_KEY, AES.MODE_CBC, PARAM_IV)
    def pad(content: str) -> str:
        return content + (16 - len(content) % 16) * "\x00"
```
- 使用null字节填充而非PKCS7标准填充
- 虽然能工作，但不符合加密最佳实践


---

## 优先级改进建议

### 🔴 P0 - 立即修复（安全和稳定性）

1. **移除密码明文存储**
   - 文件: `config_flow.py:329-330`
   - 影响: 严重安全隐患
   - 工作量: 2小时
   - 修复: 删除`CONF_PASSWORD`存储，仅保留`auth_token`

2. **修复API响应None检查**
   - 文件: `sensor.py:631-654`, `csg_client/__init__.py`
   - 影响: 运行时崩溃
   - 工作量: 4小时
   - 修复: 添加完整的None和类型检查

3. **修复JSON解析**
   - 文件: `csg_client/__init__.py:254-256`
   - 影响: 解析失败导致集成不可用
   - 工作量: 1小时
   - 修复: 使用`response.json()`并添加异常处理

### 🟡 P1 - 重要改进（功能和质量）

4. **添加重试机制**
   - 文件: `csg_client/__init__.py`
   - 影响: 提高可靠性
   - 工作量: 6小时
   - 建议: 使用`tenacity`库实现指数退避

5. **修复unique_id设计**
   - 文件: `sensor.py:227-228`
   - 影响: 多账户冲突
   - 工作量: 3小时
   - 修复: 包含`config_entry_id`

6. **国际化支持**
   - 文件: `config_flow.py`, `strings.json`
   - 影响: 用户体验
   - 工作量: 8小时
   - 修复: 将所有硬编码字符串移至翻译文件

### 🟢 P2 - 优化改进（长期质量）

7. **添加单元测试**
   - 工作量: 20小时
   - 覆盖率目标: >70%
   - 重点: coordinator逻辑、API客户端

8. **重构Coordinator**
   - 文件: `sensor.py:334-1018`
   - 工作量: 12小时
   - 目标: 拆分为多个服务类

9. **改进实体命名**
   - 文件: `sensor.py:231-232`
   - 工作量: 4小时
   - 符合HA 2024+命名规范

10. **完善文档**
    - 工作量: 8小时
    - 包括: API文档、故障排查、开发指南


---

## 详细评分矩阵

| 维度 | 评分 | 权重 | 加权分 | 说明 |
|------|------|------|--------|------|
| **功能完整性** | 8/10 | 15% | 1.20 | 实现了所有核心功能，支持多种登录方式 |
| **代码质量** | 6/10 | 20% | 1.20 | 存在硬编码、命名不一致等问题 |
| **安全性** | 4/10 | 20% | 0.80 | 密码明文存储是严重问题 |
| **错误处理** | 5/10 | 15% | 0.75 | 缺少None检查和重试机制 |
| **性能** | 7/10 | 10% | 0.70 | 并发优化良好，但缺少速率限制 |
| **可维护性** | 6/10 | 10% | 0.60 | 缺少测试和文档 |
| **HA规范符合度** | 7/10 | 10% | 0.70 | 基本符合，但实体命名等需改进 |
| **总分** | - | 100% | **5.95/10** | 约等于6.0/10 |

---

## 与同类集成对比

### 参考标准
- **官方集成**: `homeassistant/components/` (如`nest`, `xiaomi_miio`)
- **HACS热门集成**: 如`xiaomi_miot`, `hacs`

### 对比分析

| 方面 | 本集成 | 官方集成标准 | 差距 |
|------|--------|--------------|------|
| 测试覆盖率 | 0% | >80% | ❌ 严重不足 |
| 类型注解 | 部分 | 完整 | ⚠️ 需改进 |
| 文档完整性 | 基础 | 详细 | ⚠️ 需改进 |
| 错误处理 | 基础 | 完善 | ⚠️ 需改进 |
| 国际化 | 部分 | 完整 | ⚠️ 需改进 |
| 安全性 | 有隐患 | 严格 | ❌ 需立即修复 |


---

## 代码示例：推荐改进

### 示例1: 修复密码存储问题

**当前代码** (config_flow.py:327-337):
```python
data = {
    CONF_USERNAME: username,
    CONF_PASSWORD: password,  # ❌ 明文存储
    CONF_LOGIN_TYPE: login_type,
    CONF_AUTH_TOKEN: auth_token,
    CONF_ELE_ACCOUNTS: {},
    CONF_SETTINGS: {
        CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL,
    },
    CONF_UPDATED_AT: str(int(time.time() * 1000)),
}
```

**推荐改进**:
```python
data = {
    CONF_USERNAME: username,
    # 移除密码存储，仅保留token
    CONF_LOGIN_TYPE: login_type,
    CONF_AUTH_TOKEN: auth_token,
    CONF_ELE_ACCOUNTS: {},
    CONF_SETTINGS: {
        CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL,
    },
    CONF_UPDATED_AT: str(int(time.time() * 1000)),
}
```

### 示例2: 修复API响应解析

**当前代码** (sensor.py:631-639):
```python
if success_cost:
    (this_month_cost, this_month_kwh_from_cost, ladder, this_month_by_day_from_cost) = result_cost
    ladder_stage = (
        ladder[WF_ATTR_LADDER]  # ❌ ladder可能为None
        if ladder[WF_ATTR_LADDER] is not None
        else STATE_UNAVAILABLE
    )
```

**推荐改进**:
```python
if success_cost and result_cost:
    this_month_cost, this_month_kwh_from_cost, ladder, this_month_by_day_from_cost = result_cost
    
    # 安全的字典访问
    if ladder and isinstance(ladder, dict):
        ladder_stage = ladder.get(WF_ATTR_LADDER) or STATE_UNAVAILABLE
        ladder_remaining_kwh = ladder.get(WF_ATTR_LADDER_REMAINING_KWH) or STATE_UNAVAILABLE
        ladder_tariff = ladder.get(WF_ATTR_LADDER_TARIFF) or STATE_UNAVAILABLE
        ladder_start_date = ladder.get(WF_ATTR_LADDER_START_DATE) or STATE_UNAVAILABLE
    else:
        ladder_stage = STATE_UNAVAILABLE
        ladder_remaining_kwh = STATE_UNAVAILABLE
        ladder_tariff = STATE_UNAVAILABLE
        ladder_start_date = STATE_UNAVAILABLE
```


### 示例3: 改进实体命名

**当前代码** (sensor.py:226-232):
```python
@property
def unique_id(self) -> str | None:
    return f"{DOMAIN}.{self._account_number}.{self._entity_suffix}"

@property
def name(self) -> str | None:
    return f"{self._account_number}-{self._entity_suffix}"
```

**推荐改进**:
```python
# 需在实体 __init__ 中增加参数并保存: config_entry_id: str -> self._config_entry_id
# 创建实体时传入: CSGEnergySensor(coordinator, account_number, suffix, config_entry_id=config_entry.entry_id)
_attr_has_entity_name = True

@property
def unique_id(self) -> str:
    # 包含config_entry_id避免跨配置项冲突
    return f"{self._config_entry_id}_{self._account_number}_{self._entity_suffix}"

@property
def name(self) -> str:
    # 仅返回传感器类型，设备名称由device_info提供
    return SENSOR_NAMES.get(self._entity_suffix, self._entity_suffix)
```

### 示例4: 添加重试机制

**推荐新增** (csg_client/__init__.py):
```python
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((requests.RequestException, CSGHTTPError)),
    reraise=True
)
def _make_request_with_retry(self, path: str, payload: dict | None, **kwargs):
    """带重试的请求方法"""
    return self._make_request(path, payload, **kwargs)
```


---

## 测试建议

### 推荐的测试结构

```
tests/
├── __init__.py
├── conftest.py                    # pytest fixtures
├── test_config_flow.py            # 配置流程测试
├── test_sensor.py                 # 传感器测试
├── test_coordinator.py            # 协调器测试
└── test_csg_client.py             # API客户端测试
```

### 关键测试用例

**1. Config Flow测试**
- SMS登录流程
- 密码+SMS登录流程
- 二维码登录流程
- 重新认证流程
- 错误处理（网络错误、认证失败）

**2. Coordinator测试**
- 数据更新成功场景
- API失败处理
- 缓存策略验证
- 并发请求处理

**3. API客户端测试**
- 加密/解密功能
- 请求构造
- 响应解析
- 异常处理


---

## 参考资源

### Home Assistant开发文档
- [Integration Quality Scale](https://developers.home-assistant.io/docs/integration_quality_scale_index/)
- [Config Flow](https://developers.home-assistant.io/docs/config_entries_config_flow_handler/)
- [Data Update Coordinator](https://developers.home-assistant.io/docs/integration_fetching_data/)
- [Entity Naming](https://developers.home-assistant.io/docs/core/entity/#entity-naming)

### 最佳实践参考
- [官方集成示例](https://github.com/home-assistant/core/tree/dev/homeassistant/components)
- [集成质量检查清单](https://developers.home-assistant.io/docs/creating_integration_manifest/)


---

## 总结

### 整体评价

这是一个**功能完整、架构合理**的Home Assistant集成，展现了开发者对HA开发模式的扎实理解。代码实现了复杂的多步认证流程、智能的数据缓存策略和高效的并发API调用。

### 主要亮点

1. **架构设计**: 正确使用了ConfigFlow、DataUpdateCoordinator等HA核心模式
2. **性能优化**: 智能缓存和并发请求显著提升了效率
3. **用户体验**: 支持多种登录方式，配置流程友好
4. **代码组织**: API客户端独立封装，可复用性强

### 关键改进方向

1. **安全性**: 立即移除密码明文存储
2. **稳定性**: 添加完整的None检查和异常处理
3. **质量**: 补充单元测试，提高代码覆盖率
4. **规范性**: 改进实体命名，完善国际化支持

### 建议行动计划

**第一阶段（1-2周）**: 修复P0安全和稳定性问题
- 移除密码存储
- 修复API响应解析
- 添加重试机制

**第二阶段（2-4周）**: 改进代码质量
- 添加单元测试
- 修复unique_id设计
- 完善国际化

**第三阶段（长期）**: 持续优化
- 重构Coordinator
- 改进文档
- 性能监控

---

## 审查结论

**推荐状态**: ✅ **可用于生产环境，但需尽快修复P0问题**

该集成已经可以正常工作并为用户提供价值，但存在的安全隐患和稳定性问题需要在短期内解决。建议开发者按照优先级逐步改进，最终目标是达到Home Assistant官方集成的质量标准。

---

**审查完成日期**: 2026-02-02  
**下次审查建议**: 完成P0问题修复后

---

## 补充问题（审查时未覆盖或可进一步纳入）

以下问题在逐文件核对代码库时发现，**未**在报告正文中单独列出，建议一并纳入改进范围。

### 9.1 __init__.py

**设备移除与卸载逻辑**

- **identifiers 未做空检查** (line 67)  
  `account_num = list(device_entry.identifiers)[0][1]`：若 `device_entry.identifiers` 为空会触发 `IndexError`。建议先判断 `if not device_entry.identifiers` 并提前返回或记录日志。

- **pop 可能 KeyError** (line 83)  
  `new_data[CONF_ELE_ACCOUNTS].pop(account_num)`：若该设备对应的 `account_num` 已不在当前 config 的 `CONF_ELE_ACCOUNTS` 中（例如配置已被其他流程修改），会抛出 `KeyError`。建议使用 `pop(account_num, None)` 或先做 `if account_num in new_data[CONF_ELE_ACCOUNTS]`。

- **卸载时未覆盖**：报告未单独对 `__init__.py` 的 `async_unload_entry`、`async_remove_config_entry_device`、`async_remove_entry` 做审查，上述为补充点。

### 9.2 const.py

**DEFAULT_UPDATE_INTERVAL 写法存在隐患** (line 94)

```python
DEFAULT_UPDATE_INTERVAL = timedelta(hours=4).seconds
```

- `timedelta.seconds` 仅表示「不足 1 天的秒数」；若将来改为 `timedelta(days=1)` 或更长，`.seconds` 会变为 0，导致更新间隔错误。
- 建议改为 `int(timedelta(hours=4).total_seconds())` 或直接使用字面量 `4 * 3600`，避免歧义。

### 9.3 utils.py

- 文件仅定义 `_LOGGER`，无其他辅助函数。若项目内无引用，可考虑删除或注明预留用途，避免“空模块”混淆。

### 9.4 代码内 TODO 汇总（便于跟踪）

报告已提及 config_flow 中 unique_id 的 TODO，其余散布在代码中的 TODO 如下，便于统一排期或标注：

| 文件 | 位置/内容 |
|------|-----------|
| config_flow.py | 111: 硬编码字符串应引用 strings.json |
| config_flow.py | 317: username (mobile) 可能不是最佳 unique_id |
| csg_client/__init__.py | 166: 将来将 ATTR_METERING_POINT_NUMBER 加入检查 |
| csg_client/__init__.py | 480: funid "100t002" 作用未明（区域？） |
| sensor.py | 766: 最后一档阶梯时 current_ladder_remaining_kwh 的处理 |
| sensor.py | 917: Python 3.11+ 可考虑使用 asyncio.TaskGroup() |

### 9.5 小结

- **已反映在报告中的**：密码存储、API/None 检查、JSON 解析、重试与速率限制、国际化、测试与文档、unique_id、实体命名、设备信息、状态类、ConfigFlow/Coordinator/csg_client 等主要问题均在正文中有对应描述。
- **本补充节**：主要增加 __init__.py 设备移除与卸载的健壮性、const 中更新间隔的写法、utils.py 的用途说明，以及代码内 TODO 的集中列表，便于确认「项目的所有问题是否已经反映在文档里」时有一份完整清单。

---

## 文档审查修订记录（2026-02-02）

对本代码审查报告进行严格核对后的修正：

1. **评分计算错误**：详细评分矩阵中加权和为 1.20+1.20+0.80+0.75+0.70+0.60+0.70 = **5.95**，原文档误写为 6.95。已改为总分 **5.95/10**（约 6.0/10），并同步修正执行摘要中的总体评分为 **6.0/10**。
2. **代码示例笔误**：示例1「当前代码」中 `CONF_ELE_ACCOUNTS: ,` 为无效语法（缺少右值），与源码不符。已改为 `CONF_ELE_ACCOUNTS: {}`，并补全 `CONF_UPDATED_AT` 以与 config_flow.py 实际代码一致。
3. **示例3实施缺口**：unique_id 改进建议使用 `self._config_entry_id`，但当前实体类未接收该参数。已补充说明：需在实体 `__init__` 中增加 `config_entry_id` 参数并在创建实体时传入 `config_entry.entry_id`。

**与源码核对结论**：报告中对 config_flow.py 密码存储、sensor.py 的 ladder 解析 None 风险、csg_client JSON 切片解析、merge_by_day_data 返回类型、Coordinator 初始化、设备信息等问题的描述与引用行号与当前代码一致，结论成立。

