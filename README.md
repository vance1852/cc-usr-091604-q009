# 球队出行服务

职业足球队客场出行编排系统。维护球员与工作人员名单、证件有效期、交通班次、
车辆容量、住宿房型、训练场时段和比赛日程，生成可拆分的行程段，处理跨时区到达
与最晚报到时间，并支持「领队提案 → 运营审批」的改签工作流。

## 运行

```bash
python -m unittest discover -s tests -v
```

## 核心能力

- **目录与名单**：`Person`（角色：球员/工作人员/队医/领队/运营，证件有效期、
  伤员标记、紧急联系人、医疗备注）、`TransportOption`、`Vehicle`、`Room`、
  `TrainingSlot`、`Match`（开球时间与最晚报到时间）。
- **可拆分行程序**：`ItinerarySegment` 分四类 `transport / vehicle / hotel /
  training`；`split_segment` 可把草稿段按成员划分拆成多段，拆分失败自动回滚。
- **跨时区与跨夜**：所有时间必须带时区，内部统一换算 UTC 比较；成员视图同时
  输出当地时间与 UTC。`confirm_itinerary` 校验证件未过期且每人最早抵达时间
  不晚于最晚报到时间。
- **改签审批**：领队 `propose_change` 提案（cancel / replace），俱乐部运营
  `approve_change` / `reject_change` 后才生效。审批通过时重新校验资源占用与
  最晚报到时间；已完成的行程段不可重写；取消/替换记录供应商、费用差额、
  通知对象与操作人。
- **占用防重**：同班次/训练场超容量、同一车辆派车时段重叠、同房间同夜超员、
  同一人同夜占多间房、个人行程时间重叠都会抛 `BookingConflictError`；
  伤员只能安排单人房。
- **临时退出**：`withdraw_person` 只释放本人资源，合住房/合乘车辆由其余人
  保留；最后一人离开才整体取消；已完成段保持不变。
- **供应商回调**：`vendor_callback` 以幂等键去重，重复回调不重复入账，
  支持班次取消通知与退款登记。
- **费用汇总**：`cost_summary` 保留已发生费用（含已取消待报销部分）、退款、
  替换段费用差额与按供应商分组，给出可报销变更记录 ID。
- **隐私保护**：医疗备注仅本人与队医可见；紧急联系人对本人、领队、运营、
  队医可见；普通成员无法查看他人档案。
- **恢复接口**：`save` 原子写快照，`restore` 重启后重建全部状态并执行
  `audit_consistency` 资源一致性复核；回调幂等键跨重启保留。

## 关键接口

| 接口 | 说明 |
| --- | --- |
| `add_person / add_transport / add_vehicle / add_room / add_training_slot / add_match` | 维护基础目录 |
| `create_trip / add_segment / split_segment / complete_segment` | 建行程、排段、拆分、标记完成 |
| `confirm_itinerary` | 证件 + 最晚报到校验，确认全部行程段 |
| `propose_change / approve_change / reject_change` | 改签提案与审批 |
| `withdraw_person` | 成员临时退出，定向释放资源 |
| `vendor_callback` | 供应商取消/退款回调（幂等） |
| `member_view / list_roster` | 成员视图与名单（角色化隐私） |
| `cost_summary / change_history` | 费用汇总与变更审计 |
| `save / restore / audit_consistency` | 快照、重启恢复、一致性复核 |

## 目录

- `app/travel.py`：出行领域模型与 `TravelService`（仅依赖标准库，线程安全）
- `tests/test_smoke.py`：基础健康检查
- `tests/test_travel.py`：35 个行为测试，覆盖并发改签、夜间跨日、跨时区报到、
  供应商重复回调、成员退出、隐私视图与重启后资源一致性
