# 交通信号配时变更核台

离线采录路口检测器、车辆样本、相位审批和控制器现场读回。三个账本分别保留流量需求、方案合同和实际启用结果；上传成功不能代替控制器读回。项目只使用仓内合成 JSON，SQLite 数据库在本地生成，无网络服务或密钥。

## 安装与运行

需要 Python 3.11+。在本仓库目录执行：

```powershell
python -m pip install -e .
python -m signalphase demo --db run.sqlite
python -m signalphase audit --db run.sqlite
python -m unittest discover -s tests -v
```

单步导入入口：

```powershell
python -m signalphase flow-import --db run.sqlite --file fixtures/flow-import-ok.json
python -m signalphase plan-import --db run.sqlite --file fixtures/plan-import-ok.json
python -m signalphase field-import --db run.sqlite --file fixtures/field-import-ok.json
python -m signalphase report --db run.sqlite
```

`demo` 顺序导入五份 fixture：校准与流量、两个批准方案、两次现场读回、控制器故障、迟到车次。第二方案因故障失效，已验证的旧方案重新激活；迟到车次的观测时刻落在故障影响窗口内，不能进入正常流量基线。再次导入同一个源文件按 SHA 去重，复用事件 ID 但改写内容则拒绝整个文件。

## 业务状态

| 子系统 | 输入与决定 | 持久化及可见结果 |
| --- | --- | --- |
| 路口流量采信 | 检测器校准须覆盖观测时刻，跨测点同车次去重，异常速度与现场故障窗口隔离 | `detector`、`vehicle_sample`、`demand` 保存样本结论及需求修订；撤销校准会重算未发布方案 |
| 配时方案审批 | 相位冲突、行人过街时间、审批角色和窗口共同决定方案资格 | `phase_plan`、`phase_plan_history`、`approval` 保存当前版和历史版摘要；有效迟到流量可重评尚未启用的方案 |
| 现场执行回退 | 控制器读回完整相位与时长，摘要吻合才启用；故障回到先前已验证方案及其原始版次 | `controller`、`activation`、`rollback` 保存实际生效时段、失败责任人与影响区间 |

`fixtures/*-bad.json` 各给出一条业务失败样例。它们分别演示失准和异常样本、冲突相位、错版读回。导入计划或现场失败样例前，先按上面的单步顺序建立相应的流量和方案前置状态。失败样例会留下可查询的裁定记录，不会冒充成功启用。

## 排水异常归因与器材派遣

北进口雨水井水位越过一米只构成到场巡查门槛（`inspect`）：它既不定位出口堵塞，也不估计排水量。`drain-balance-import` 在同一账本上继续采录多时刻水位、井筒面积校准、汇水边界、夹装流量计读数、泵机通电与计量回执，并按体积守恒做归因：

- 储量变化只用已标定井筒面积与有效期内的水位原件计算（10 m² × 0.1 m = 1 m³，十分钟即净增 6 m³/h）。同一井整段窗口的体积变化必须由同一组进/出流时间积分解释；分段速率不一致时所有候选平衡都被拒绝，不允许逐分钟换一个无来源出流。
- 候选世界（如甲：入流 18/出流 12，乙：入流 6/出流 0）只要同时满足守恒和证据边界就全部保留，并各自登记待证缺口。净增 6 不能直接当成堵塞量，也不能把全部上涨归为暴雨。
- 泵机通电只证明运行条件，不产生出流；只有夹装流量计或泵计量回执才算出流证据。雷达雨强必须按有效汇水面积与损失系数换算（mm/h × 面积 × 系数），未覆盖暗渠的雷达读数被排除，不能直接写成井内 m³/h。
- 派遣随证据收敛：出口实测有流则只派上游短时调蓄并撤回未执行的清淤车；实测零流才派清通与临时泵；出口未证且至少有入流证据时只派便携泵与管道检查，不签发“已定位堵塞”。迟到证据生成新修订版，已执行的到场、清淤、泵工和排出水量原件永不改写。

`fixtures/drain-balance.json` 给出共用同一对水位原件的甲乙两个守恒世界。`tests/test_drain_balance.py` 重放有效期从 `inspect` 到 `held` 的变化、同水位异处置反例、时序积分一致性、两条核查路线的派遣范围、泵回执边界与审计篡改。

每个源文件和事件保留原文与 SHA。`audit` 从原始源文件在临时数据库重放全部业务表，并同时核对源文件和事件摘要；任何不一致都会在三项布尔结果中暴露。单文件导入使用 SQLite 事务，事件中途失败不会留下前半段决定。

## 数据格式

导入文件为 `{ "events": [...] }`。每条事件有全局唯一 `id` 和 `kind`。`flow-import` 接受 `detector`、`sample`、`revoke`，`plan-import` 接受 `plan`，`field-import` 接受 `upload`、`fault`，`drain-balance-import` 接受 `drain_well`、`drain_level`、`drain_visit`、`drain_catchment`、`drain_rainfall`、`drain_flow`、`drain_pump_power`、`drain_pump_receipt`、`drain_scenario`、`drain_balance`、`drain_work_receipt`。具体字段见 `fixtures/demo` 与 `fixtures/drain-balance.json`。时间使用 UTC ISO 8601 字符串，窗口编号由采样方提供；控制器读回必须给出行人过街距离、两种绿灯时长和完整相位图。

本仓库的离线环境没有预装 pytest，仓内 `pytest.py` 是仅用标准库 `unittest` 的最小命令垫片，使 `python -B -m pytest -q -p no:cacheprovider` 可直接运行；环境中若存在真实 pytest 会优先生效。

本项目的安全步速使用 0.8 米/秒的合成演示值；真实道路配置应由当地审定参数替换。没有先前验证方案的现场故障会标为 `needs_manual_safe_mode`，不会伪造自动回退成功。
