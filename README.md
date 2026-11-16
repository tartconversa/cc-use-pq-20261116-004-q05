# 炭素材料证据与产能台账

本地 FastAPI 服务记录生产批次、实验室检验、发运、产线产能、客户订单与交付证据。所有示例记录均为合成数据，不连接真实工厂。

## 运行

在已准备好锁定依赖的 Python 环境中执行 uvicorn carbon_trace.api:app，通过 CARBON_TRACE_DB 指定 SQLite 文件。接口说明见 /docs。执行 python -m carbon_trace.cli demo --fixture fixtures/demo.json 可运行隔离的合成数据演示，输出日报、版本与对账结果。

## 数据约定

日期为 YYYY-MM-DD，事件时间必须带时区。工厂时区决定生产与发运所属日期，数量采用十进制吨。来源与外部事件编号组成不可变身份；完全相同的重试视为重复，不同内容的重试产生冲突拒绝。批次编号也具备幂等性，错误行不会回滚同批有效行。

登记工厂、产线和等级标准后提交生产批次。等级标准生效区间不得重叠；质量分析使用生产当日标准和该批次最新检验，区分未检验和检验不合格。准备发布时固定数据修订号与发布头；如其间新增事件或竞争版本，原候选失效，已发布数据保持不变。

订单预约只接受检验合格批次，且不得超过订单需求或批次余量。取消订单或释放预约后可重新使用余量。事故标记显示在日报；交付核收与物理发运量分别统计。

## 来源限制（合规冻结）

来源策略（`/sources/{source}/policy`）只能阻止后续事件摄取；来源限制则面向**已入库的存量材料**。合规团队可对某个来源开启带生效时间的限制（`POST /restrictions`），并在解除时写入解除记录（`POST /restrictions/{id}/lift`）。限制是追加式证据：解除只补记 `lifted_at` 与修订号，不删除或改写任何记录；历史发运、来源取证和已发布报表的版本快照保持不变。

限制挂在来源上，沿材料身份 `lots.source` 传播到三处：

- **库存可用性**：`GET /plants/{plant_id}/inventory` 对每个批次给出 `restricted`/`available` 标记，并把余量拆成 `available_tons` 与 `restricted_tons`（`remaining_tons` 口径不变）。按天查询以工厂当地当日结束时点评估。
- **预约决策**：限制生效期间，受影响批次的新预约被硬阻断（`business_invariant`，错误详情带限制编号）；限制生效前已存在的预约不会被自动释放或改写。
- **对外质量声明**：`GET /lots/{lot_id}/quality` 在检验结论之外新增 `compliance` 块，标注来源是否受限及限制编号；合格/不合格的检验判定本身不变。

`GET /lots/{lot_id}/availability?at=<timestamp>` 解释某批材料在特定时点**为什么**可用或不可用，分别给出库存可用性原因（如 `source_restricted`）与可承诺原因（叠加 `quality_unmeasured`/`quality_failed`）。

解除限制后，库存自动恢复为可预约、可用于新承诺；但解除前已存在且仍 active 的预约属于**既存承诺**，进入 `GET /restrictions/{id}/review-queue` 人工复核队列，经 `POST /restrictions/reviews` 处置后销项。解除之后新建的预约不入队，从而区分“恢复可用的库存”与“仍需人工处理的既有承诺”。对账（reconciliation）相应新增 `source_restricted` 与 `legacy_commitment_pending_review` 两个只增不改的异常代码。限制的开启、解除、复核都进入修订审计，可通过溯源与 `GET /revisions/{revision}` 查询。


## 核心接口

使用 POST /plants、POST /lines、POST /specifications 和 POST /batches 建立数据。日报通过 GET /plants/{plant_id}/report 查询。先从 GET /status 取得修订号与发布头，再调用 POST /candidates 和 POST /candidates/{candidate_id}/publish。历史版本通过 GET /versions/{version} 查询。其他接口提供库存、质量、溯源、对账、产能预测、订单、预约、事故和交付核收。错误响应包含稳定代码与原因。

来源限制接口：POST /restrictions 开启（含 source、reason、effective_at），POST /restrictions/{restriction_id}/lift 解除（带乐观锁 expected_revision），GET /restrictions 与 GET /restrictions/{restriction_id} 查询限制及解除记录，GET /restrictions/{restriction_id}/review-queue 查看待人工处理的既存承诺，POST /restrictions/reviews 提交复核处置，GET /lots/{lot_id}/availability 解释批次在指定时点的可用性原因。
