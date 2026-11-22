# 交通信号配时变更核台

离线采录路口检测器、车辆样本、相位审批和控制器现场读回。三个账本分别保留流量需求、方案合同和实际启用结果；上传成功不能代替控制器读回。项目只使用仓内合成 JSON，SQLite 数据库在本地生成，无网络服务或密钥。

## 安装与运行

需要 Python 3.11+。在本仓库目录执行：

```powershell
python -m pip install -e .
python -m signalphase demo --db run.sqlite
python -m signalphase audit --db run.sqlite
python -B -m pytest -q -p no:cacheprovider
```

单步导入入口：

```powershell
python -m signalphase flow-import --db run.sqlite --file fixtures/flow-import-ok.json
python -m signalphase plan-import --db run.sqlite --file fixtures/plan-import-ok.json
python -m signalphase field-import --db run.sqlite --file fixtures/field-import-ok.json
python -m signalphase drain-import --db run.sqlite --file fixtures/drain-balance.json
python -m signalphase explain --db run.sqlite --well north-curb --from-level DL1 --to-level DL2
python -m signalphase report --db run.sqlite
```

`demo` 顺序导入五份 fixture：校准与流量、两个批准方案、两次现场读回、控制器故障、迟到车次。第二方案因故障失效，已验证的旧方案重新激活；迟到车次的观测时刻落在故障影响窗口内，不能进入正常流量基线。再次导入同一个源文件按 SHA 去重，复用事件 ID 但改写内容则拒绝整个文件。

## 业务状态

| 子系统 | 输入与决定 | 持久化及可见结果 |
| --- | --- | --- |
| 路口流量采信 | 检测器校准须覆盖观测时刻，跨测点同车次去重，异常速度与现场故障窗口隔离 | `detector`、`vehicle_sample`、`demand` 保存样本结论及需求修订；撤销校准会重算未发布方案 |
| 配时方案审批 | 相位冲突、行人过街时间、审批角色和窗口共同决定方案资格 | `phase_plan`、`phase_plan_history`、`approval` 保存当前版和历史版摘要；有效迟到流量可重评尚未启用的方案 |
| 现场执行回退 | 控制器读回完整相位与时长，摘要吻合才启用；故障回到先前已验证方案及其原始版次 | `controller`、`activation`、`rollback` 保存实际生效时段、失败责任人与影响区间 |
| 排水异常归因 | 井筒标定面积把两次水位换成储量变化；出口铭牌容量、实测流量、汇水边界与雷达雨强共同约束入流/出流；泵机通电只作运行条件 | `drain_balance`、`drain_gap` 保留每条可行水量平衡及其待证缺口；`drain_dispatch` 逐版追加器材清单，`drain_work` 保留已执行工时与已排水量 |

`fixtures/*-bad.json` 各给出一条业务失败样例。它们分别演示失准和异常样本、冲突相位、错版读回。导入计划或现场失败样例前，先按上面的单步顺序建立相应的流量和方案前置状态。失败样例会留下可查询的裁定记录，不会冒充成功启用。

## 排水归因

`drain-import` 还接受 `drain_outlet`、`drain_catchment`、`drain_rainfall`、`drain_flow`、`drain_pump`、`drain_work`、`drain_dispatch`、`drain_redispatch`。同一井在整个区间的体积变化由一对时间积分入流/出流解释：

```
inflow - outflow = net = storage_area_m2 * (depth_to - depth_from) / hours
```

一个方程两个未知量，因此只凭水位只能得到净增（例：10 m²、08:00 1.2 m、08:10 1.3 m 即 +1 m³/10min = 6 m³/h），不能直接当作堵塞量。`explain` 枚举满足守恒的端点组合：

- 上游暴雨入流 18、出口畅通出流 12 → 应临时调蓄，撤回未执行的清淤车；
- 普通入流 6、出口堵塞出流 0 → 应清通出口并设便携泵。

两者都给出相同水位原件和 `DV1=inspect`。把净增 6 直接当堵塞量会在第一种世界派错器材；把上涨都归为暴雨会漏掉第二种。`drain-level` 的 1.0 m 阈值只是到场巡查门槛，不是堵塞定位或排水量估计。

中间时刻水位若要求不同的净速率，整段无法由同一对进出流解释，会写 `trajectory_not_integrated` 并且不派清淤或调蓄。流量记录必须覆盖整个水位区间，否则记为 `unusable_flow_window` 而不是与相邻分钟峰值拼接。雷达雨强先按有效汇水面积和损失系数换算为 m³/h。出水泵通电只证明运行条件，不计为出流。

现场核查有两种路线，起点相同但覆盖范围不同：出口夹装流量计可证实出流 12 并反推出入流 18（派调蓄，但无法定位 18 中哪条支沟来水）；只覆盖屋面汇水区的高分辨率雷达雨量可证实至少 6 m³/h（派出临时泵与管道检查，但不能签发已定位出口堵塞）。`drain_gap` 分别记录 `inflow_unattributed`、`outflow_unmeasured`、`blockage_unmeasured`、`catchment_uncovered` 等缺口。

迟到流量证据用 `drain_redispatch` 追加新版本，旧版派遣、到场与工时原样保留；`audit` 从源文件重放全部排水表，篡改 `drain_balance`、`drain_work` 或源文件都会使重放失败。

每个源文件和事件保留原文与 SHA。`audit` 从原始源文件在临时数据库重放全部业务表，并同时核对源文件和事件摘要；任何不一致都会在三项布尔结果中暴露。单文件导入使用 SQLite 事务，事件中途失败不会留下前半段决定。

## 数据格式

导入文件为 `{ "events": [...] }`。每条事件有全局唯一 `id` 和 `kind`。`flow-import` 接受 `detector`、`sample`、`revoke`，`plan-import` 接受 `plan`，`field-import` 接受 `upload`、`fault`，`drain-import` 接受上节列出的排水事件。具体字段见 `fixtures/demo` 与 `fixtures/drain-balance.json`。时间使用 UTC ISO 8601 字符串，窗口编号由采样方提供；控制器读回必须给出行人过街距离、两种绿灯时长和完整相位图。

本项目的安全步速使用 0.8 米/秒的合成演示值；真实道路配置应由当地审定参数替换。没有先前验证方案的现场故障会标为 `needs_manual_safe_mode`，不会伪造自动回退成功。
