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

同相位灯头组（`head-import`）在同一供电预算与同一相机相位下联合约束全部灯头与全部帧：`head_scan` 保存灯头、采样率、曝光时长、快门窗与逐帧值；`power_group` 登记标定供电总功率上限、光强/功率换算、同步偏差与现场 activation 归属（有读回归入 `safe-base` 第1版，无读回为 `unattributed`）。联合可行集给出每盏灯的光强区间与整组“至少几盏欠亮”的可证范围：逐灯 `possible_dim` 不能拼成整组可用，预算 1.0 下两盏各需 0.6（合计 1.2）即被证伪，未定位的灯头只登记组级待查工单并关闭车道，不随机指认。补证两条路线：取回共同表时校准的完整分支电流可回溯旧窗口并补发具体灯头工单（已执行封控留痕，只重裁未执行任务）；历史电流缺页则保留封控、装独立临时红灯（其亮度不计入原灯组）和只对安装后窗口有效的电流钳，旧窗口永远停留在“至少一盏、对象未定”。`health_verdict`、`control_action` 等新表全部纳入 `audit` 重放，篡改任何裁定或删除封控记录都会使 `replay_ok` 失败。


## 业务状态

| 子系统 | 输入与决定 | 持久化及可见结果 |
| --- | --- | --- |
| 路口流量采信 | 检测器校准须覆盖观测时刻，跨测点同车次去重，异常速度与现场故障窗口隔离 | `detector`、`vehicle_sample`、`demand` 保存样本结论及需求修订；撤销校准会重算未发布方案 |
| 配时方案审批 | 相位冲突、行人过街时间、审批角色和窗口共同决定方案资格 | `phase_plan`、`phase_plan_history`、`approval` 保存当前版和历史版摘要；有效迟到流量可重评尚未启用的方案 |
| 现场执行回退 | 控制器读回完整相位与时长，摘要吻合才启用；故障回到先前已验证方案及其原始版次 | `controller`、`activation`、`rollback` 保存实际生效时段、失败责任人与影响区间 |
| 同相位灯头可见性 | 单一供电预算与一个相机相位在全部灯头、全部帧上联合约束；逐灯光强区间不能合并成整组结论，未定位灯头只登记组级待查 | `head_scan`、`power_group`、`health_verdict`、`current_reading`、`clamp_reading`、`control_action` 保存联合可行集裁定、封控与补证边界 |

`fixtures/*-bad.json` 各给出一条业务失败样例。它们分别演示失准和异常样本、冲突相位、错版读回。导入计划或现场失败样例前，先按上面的单步顺序建立相应的流量和方案前置状态。失败样例会留下可查询的裁定记录，不会冒充成功启用。

每个源文件和事件保留原文与 SHA。`audit` 从原始源文件在临时数据库重放全部业务表，并同时核对源文件和事件摘要；任何不一致都会在三项布尔结果中暴露。单文件导入使用 SQLite 事务，事件中途失败不会留下前半段决定。

## 数据格式

导入文件为 `{ "events": [...] }`。每条事件有全局唯一 `id` 和 `kind`。`flow-import` 接受 `detector`、`sample`、`revoke`，`plan-import` 接受 `plan`，`field-import` 接受 `upload`、`fault`。具体字段见 `fixtures/demo`。时间使用 UTC ISO 8601 字符串，窗口编号由采样方提供；控制器读回必须给出行人过街距离、两种绿灯时长和完整相位图。

本项目的安全步速使用 0.8 米/秒的合成演示值；真实道路配置应由当地审定参数替换。没有先前验证方案的现场故障会标为 `needs_manual_safe_mode`，不会伪造自动回退成功。
