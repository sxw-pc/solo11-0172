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

每个源文件和事件保留原文与 SHA。`audit` 从原始源文件在临时数据库重放全部业务表，并同时核对源文件和事件摘要；任何不一致都会在三项布尔结果中暴露。单文件导入使用 SQLite 事务，事件中途失败不会留下前半段决定。

## 分叉光纤故障路段与派车工单

`fiber-branch-import` 在主柜、Y 形分线井和南北远端之间保存带版本的拓扑（主柜至分线井、两支线长度、路线校准速度、接线版次、各支线通行封控时间）与接头反射模板。主柜单端回波只保存测量方向为 `forward` 的完整回波，距离一律按 `v·t/2` 由往返时间换算为单程距离；远端收光表试验方向为 `reverse`，逐支隔离的复测仍为 `forward`，往返时间不会被当成单程距离。

单端回波的一维距离经过 Y 井后可能落在多条物理路段上：北支线 0.4 公里处与南支线 0.4 公里处的断点产生完全相同的 14 微秒、同幅度记录，交换南北后观测不变而挖掘点相反。因此初始工单是两个可执行候选井箱（`ambiguous_candidates`），按各支线封控时间排出最短核查顺序，而不是把距离数值当唯一地理点，也不报告成“两支线都坏”。

- 收光表补测（A）：远端不断开运行支线，无光的一支把工单收窄为整条支线（`branch_scoped`），仍需带全支线巡检器材；某支线的检修断开不能冒充另一支线已验证连续。
- 逐支隔离补测（B）：按已签切换顺序隔离并复测，隔离北线回波消失、隔离南线保留时把工单定位到具体点（`point_localized`），派针对性接续班组；试验前必须先取得备用通信链 `carrying_traffic` 回执，回执与控制器 readback 分表保存，停服窗口单独记账，不回写为原始断线时刻。
- 迟到拓扑修订只重投影仍开放的工单，已派发的点定位工单保留其拓扑版次；旧接线的接头模板绑定接线版次，不能套到新接线上。对回波投影或工单行的任何篡改都会在 `audit` 重放比对中暴露。

## 数据格式

导入文件为 `{ "events": [...] }`。每条事件有全局唯一 `id` 和 `kind`。`flow-import` 接受 `detector`、`sample`、`revoke`，`plan-import` 接受 `plan`，`field-import` 接受 `upload`、`fault`，`fiber-branch-import` 接受 `branch_topology`、`branch_echo`、`remote_probe`、`backup_switchover`、`branch_isolation`。具体字段见 `fixtures/demo` 与 `fixtures/fiber-branch.json`。时间使用 UTC ISO 8601 字符串，窗口编号由采样方提供；控制器读回必须给出行人过街距离、两种绿灯时长和完整相位图。

本项目的安全步速使用 0.8 米/秒的合成演示值；真实道路配置应由当地审定参数替换。没有先前验证方案的现场故障会标为 `needs_manual_safe_mode`，不会伪造自动回退成功。
