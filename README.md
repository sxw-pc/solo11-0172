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

## 步行线路柴油暴露

`walk-import` 在同一套事件账本上新增步行暴露数据产品。事件采录路段长度与行人通过时间、路边三通道原件、风向、站点覆盖、参考指纹版本和仪器校准批次；`walk_assessment` 对每条路径给出柴油浓度·分钟积分的联合可行上下界、尚无监测覆盖的路段，以及在现有证据下东/西路线是否可比。

三通道参考向量存在严格线性依赖：二次背景 `b=[5.5,4,3.5]` 恰好等于柴油 `d=[4,6,2]` 与扬尘 `u=[7,2,5]` 的均值，观测矩阵的零空间方向是 `(-0.5,-0.5,1)`。校门读数 `y=b` 同时零残差地对应系数 `(0,0,1)` 与 `(0.5,0.5,0)`，因此产品枚举全部基本可行解而不是把零残差族压成单一系数点；只有一条路径在所有可行分解下确实占优（一条路径的上界严格小于另一条的下界）才发布确定推荐，原始通道总量和路径长短都不能直接充当最短路权重。未覆盖路段保持显式开区间，校门滤膜化学分析只能给点位可信度，不能为另一条路线的未测路段补造浓度。

可取得的两份附加原件覆盖不同：与三通道同步的移动第四通道（`walk_channel4`，并在 `walk_source_channel4` 登记同版指纹的第四分量）若能区分柴油与背景，可在连续覆盖的路线上把柴油积分缩为确定点并发布全线路线比较；遮蔽路段仍回退到三通道可行界。风向反转、校准批次或指纹版本不一致的原件在评估时标记 `excluded` 并记录原因，不会跨风场或跨批次并入。旧 `air_advisory` 的最近邻记录作为原始采录结果原样保留，不覆写成混合份额。

## 数据格式

导入文件为 `{ "events": [...] }`。每条事件有全局唯一 `id` 和 `kind`。`flow-import` 接受 `detector`、`sample`、`revoke`，`plan-import` 接受 `plan`，`field-import` 接受 `upload`、`fault`。`walk-import` 接受 `air_signature`、`walk_calibration`、`walk_route`、`walk_segment`、`walk_reading`、`walk_channel4`、`walk_source_channel4`、`walk_filter` 和 `walk_assessment`。具体字段见 `fixtures/demo` 与 `fixtures/walk-air.json`。时间使用 UTC ISO 8601 字符串，窗口编号由采样方提供；控制器读回必须给出行人过街距离、两种绿灯时长和完整相位图。

本项目的安全步速使用 0.8 米/秒的合成演示值；真实道路配置应由当地审定参数替换。没有先前验证方案的现场故障会标为 `needs_manual_safe_mode`，不会伪造自动回退成功。
