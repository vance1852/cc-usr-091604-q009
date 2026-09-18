# 球队出行服务

职业足球队客场出行编排系统。覆盖名单与证件、交通班次、车辆容量、住宿房型、
训练场时段与比赛日程的维护，支持可拆分行程段、跨时区到达与最晚报到时间校验、
领队改签 + 运营审核的变更链路、车辆/房间防重复占用、按角色保护的隐私信息、
费用台账与快照恢复。

## 运行

```bash
python -m unittest discover -s tests -v
```

## 目录

- `app/models.py`：领域模型（成员、证件、车辆、房间、班次、训练场、比赛、行程段、方案、台账）
- `app/roster.py`：名单与证件有效期；紧急联系人/医疗备注按角色保护
- `app/inventory.py`：交通班次、车辆、房间、训练场时段、比赛日程
- `app/itinerary.py`：行程段生成/拆分/确认，跨时区与报到时限校验，行程与成员视图
- `app/assignments.py`：车辆/房间分配（防重复占用、伤员单间）、成员临时退出只释放本人资源
- `app/changes.py`：改签方案（领队提出、运营审核、原子生效），取消/替换留痕供应商、费用差额与通知对象
- `app/costs.py`：费用台账与汇总，已发生费用保留以便报销
- `app/callbacks.py`：供应商回调，按 callback_id 幂等
- `app/recovery.py` + `app/store.py`：原子快照与重启后的资源一致性校验
- `app/service.py`：`TravelService` 门面，内部可重入锁保证并发安全
- `app/travel.py`：对外入口（保留 `TripSegment` 与 `TravelService` 兼容导出）
- `tests/`：行为测试（含并发改签、夜间跨日、供应商重复回调、重启一致性）

## 核心流程

```python
from app.service import TravelService

svc = TravelService(storage_path="state.json")   # 每次成功变更自动落快照

# 1. 名单与库存
svc.add_member("李领队", "leader", member_id="L1")
svc.add_document("L1", "passport", "E1234567", expires_on=date(2028, 1, 1))
svc.add_vehicle("大巴", 45, "车队A", vehicle_id="V1")
svc.add_transport_option("flight", "航司A", "CA851", "上海", "伦敦",
                         departs_at, arrives_at, capacity=30, cost=28000, option_id="F1")

# 2. 行程编排与确认（校验证件、报到时限、资源分配）
svc.create_itinerary("客场征程", match_id="M1", itinerary_id="IT1")
svc.add_segment_from_option("IT1", "F1", members, segment_id="S-FLY")
svc.auto_assign_vehicles("S-BUS")
svc.auto_assign_rooms("S-HOTEL")                  # 自动满足伤员单间要求
svc.confirm_itinerary("IT1", by_member="L1")

# 3. 航班取消 → 领队改签 → 运营审核 → 生效留痕
svc.supplier_callback("cb-1", "航司A", "S-FLY", "cancelled")
pid = svc.propose_change("IT1", proposer_id="L1", reason="航班取消",
                         actions=[{"type": "add", "segment": {...}, "notify": ["L1", "O1"]}])
svc.approve_change(pid, by_member="O1")

# 4. 视图、费用与恢复
svc.member_schedule("IT1", "P1")                  # 成员视图（隐私按角色过滤）
svc.cost_summary("IT1")                           # 费用汇总（含已发生待报销）
TravelService.recover("state.json")               # 重启恢复并校验一致性
```

## 关键规则

- 行程确认后不可直接改，必须走「领队提案 → 运营审核」；方案携带版本号，
  并发审核只有一个生效，其余报 `ConflictError`。
- 已完成的行程段不可重写；取消/替换记录供应商、费用差额、通知对象，
  已发生费用保留在台账中标记为可报销。
- 车辆座位、房间床位、班次容量、训练场时段均防重复占用；成员临时退出
  只释放其本人占用的资源。
- 医疗备注仅队医与本人可见；紧急联系人仅队医/领队/运营与本人可见。
- 供应商回调按 `callback_id` 幂等，重复推送不改变状态。
