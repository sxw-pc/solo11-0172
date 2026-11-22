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
python -m signalphase surface-import --db run.sqlite --file fixtures/surface-ok.json
python -m signalphase ice-import --db run.sqlite --file fixtures/ice-time.json
python -m signalphase report --db run.sqlite
```

`demo` 顺序导入六份 fixture：校准与流量、两个批准方案、两次现场读回、控制器故障、迟到车次，以及北进口车道的结冰核查历史包。第二方案因故障失效，已验证的旧方案重新激活；迟到车次的观测时刻落在故障影响窗口内，不能进入正常流量基线。再次导入同一个源文件按 SHA 去重，复用事件 ID 但改写内容则拒绝整个文件。

## 业务状态

| 子系统 | 输入与决定 | 持久化及可见结果 |
| --- | --- | --- |
| 路口流量采信 | 检测器校准须覆盖观测时刻，跨测点同车次去重，异常速度与现场故障窗口隔离 | `detector`、`vehicle_sample`、`demand` 保存样本结论及需求修订；撤销校准会重算未发布方案 |
| 配时方案审批 | 相位冲突、行人过街时间、审批角色和窗口共同决定方案资格 | `phase_plan`、`phase_plan_history`、`approval` 保存当前版和历史版摘要；有效迟到流量可重评尚未启用的方案 |
| 现场执行回退 | 控制器读回完整相位与时长，摘要吻合才启用；故障回到先前已验证方案及其原始版次 | `controller`、`activation`、`rollback` 保存实际生效时段、失败责任人与影响区间 |
| 路面结冰核查与复开 | 涂层留样发射率范围贯穿整个维护周期，按 B=εT+(1-ε)S 反推各时刻实温区间；负亮温只触发保守 watch，不作冰的实测证明 | `coating_sample`、`water_film`、`ice_frame`、`ice_window`、`lane_closure`、`lane_reopen` 等保存核查、封控责任与复开；证书可回溯修订历史，接触测温只支撑此后的复开 |

`fixtures/*-bad.json` 各给出一条业务失败样例。它们分别演示失准和异常样本、冲突相位、错版读回。导入计划或现场失败样例前，先按上面的单步顺序建立相应的流量和方案前置状态。失败样例会留下可查询的裁定记录，不会冒充成功启用。

结冰核查在 `surface-import` 的临时巡查之上：`ice-import` 采录涂层留样、水膜、实际封控与绕行、防滑处置、接触测温和复开发布。同一维护周期只用一个 ε：评估给区间，实验室证书把整周期钉到单一值，绝不逐帧挑选；冻结/融化界一律记为两次观测之间的区间，不写伪精确时刻。水膜消失必须有独立证据。迟到证书原件可重新界定历史危险时段与维护责任，但已执行的封控、复开与绕行记录永不删除；未来接触测温不能倒推历史。雨后新水膜到来时既有复开自动失效，车道重新进入待核。`fixtures/ice-time.json` 给出 06:00 与 06:10 两帧（B=-2/-1°C、S=-15°C、ε∈[0.85,0.90]）的完整历史包，值班室可由此走证书回溯或接触复测两条补证路线。

每个源文件和事件保留原文与 SHA。`audit` 从原始源文件在临时数据库重放全部业务表，并同时核对源文件和事件摘要；任何不一致都会在三项布尔结果中暴露。单文件导入使用 SQLite 事务，事件中途失败不会留下前半段决定。

## 数据格式

导入文件为 `{ "events": [...] }`。每条事件有全局唯一 `id` 和 `kind`。`flow-import` 接受 `detector`、`sample`、`revoke`，`plan-import` 接受 `plan`，`field-import` 接受 `upload`、`fault`，`surface-import` 接受 `station`、`frame`、`lane_check`，`ice-import` 在此基础上另接受 `coating`、`water`、`closure`、`detour`、`treatment`、`contact`、`assess`、`certificate`、`reopen`。具体字段见 `fixtures/demo`。时间使用 UTC ISO 8601 字符串，窗口编号由采样方提供；控制器读回必须给出行人过街距离、两种绿灯时长和完整相位图。

本项目的安全步速使用 0.8 米/秒的合成演示值；真实道路配置应由当地审定参数替换。没有先前验证方案的现场故障会标为 `needs_manual_safe_mode`，不会伪造自动回退成功。
