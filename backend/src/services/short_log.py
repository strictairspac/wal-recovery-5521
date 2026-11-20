"""交班复测短日志：整段事务取舍的最小化与逐点复测。

规则（与 wal_engine 的 strict-wal-quiescent 模型对齐）：
- 每个原断电点 c 的恢复读数 = 最近检查点账本 + 已提交（commit LSN <= c）事务写入；
  未结束事务与已 abort 事务对读数无贡献。
- 短日志以整段事务为取舍单位：要么保留一个事务的 begin/write/commit 全部条目，
  要么整段丢弃；checkpoint 一律丢弃（其只决定重做起点，不影响最终读数）。
- 最小化采用需求驱动贪心：先确定每个选定断电点上各键的“必需读数”（该点最后
  一条已提交写入的值；与初始账本相同则无需求），再沿 LSN 保留最早建立该必需值
  的写入。同值覆写、未被任何选点读到的中间值、未提交/abort 事务都不影响读数，
  随事务整段删除。含必要写入的事务整段保留，因此保留条目数达到最小。
- 保留条目按原 LSN 顺序组成短日志；位置对照把原 LSN 映射到短日志 1 基序号，
  并把原断电点换算成短日志复测点。
"""
import copy
import hashlib
import json

from src.services.wal_engine import recover, validate

MODEL_VERSION = 'handover-short-log-v1'


def _recover_readings(initial, records, crash_after):
    """空短日志的复测读数即初始账本；非空调用同一 WAL 模型。"""
    if crash_after == 0 and not records:
        return copy.deepcopy(initial)
    return recover(initial, records, crash_after)['recovered']


def build_short_log(initial, records, crash_points):
    """生成最小短日志与位置对照，并逐点复测。返回完整演算结果。"""
    validate(initial, records)
    if not crash_points:
        raise ValueError('至少选择一个原断电点')
    points = sorted({int(p) for p in crash_points})
    if any(not 0 <= p <= len(records) for p in points):
        raise ValueError('断电点超出日志范围')

    # 仅需事务的提交位置与 abort 集合即可判定可见性，状态变化由复测阶段重放。
    commit_lsn = {}
    aborted = set()
    for lsn, row in enumerate(records, 1):
        op = row['op']
        if op == 'commit':
            commit_lsn[row['tx']] = lsn
        elif op == 'abort':
            aborted.add(row['tx'])

    # 需求驱动的最小化：先确定每个选点上各键的必需读数（该点最后一条已提交
    # 写入的值；尚无已提交写入的键无需求，初始账本天然满足），再沿 LSN 贪心
    # 保留“最早建立该必需值”的写入。同值覆写不承担任何选点读数，整段删除；
    # 含必要写入的事务整段保留。
    necessary_lsns = set()
    requirements = {}          # key -> [(point, value, lsn)] 去重后的连续需求
    for point in points:
        last_write = {}        # key -> (lsn, value) 该点最后一条已提交写入
        for lsn, row in enumerate(records[:point], 1):
            if row['op'] != 'write':
                continue
            tx = row['tx']
            if commit_lsn.get(tx) is not None and commit_lsn[tx] <= point:
                last_write[row['key']] = (lsn, row['value'])
        for key, (lsn, value) in last_write.items():
            # 必需读数与初始账本相同时无需任何写入保留：删除全部写入后该键
            # 自然回到初始值。只在需求值首次出现且不同于初始值时建立需求。
            seq = requirements.setdefault(key, [])
            if value == initial[key]:
                continue
            if not seq or seq[-1][1] != value:
                seq.append((point, value, lsn))
    for key, seq in requirements.items():
        retained_value = object()      # 尚无保留写入时的哨兵，任何 int 值都与之不等
        for point, value, _last_lsn in seq:
            if value == retained_value:
                continue
            # 取 LSN 最早、取值匹配且不晚于该选点的写入：它最早建立该读数，
            # 同值的后续覆写因此不再需要。
            candidates = [
                lsn for lsn, row in enumerate(records[:point], 1)
                if row['op'] == 'write' and row['key'] == key and row['value'] == value
                and commit_lsn.get(row['tx']) is not None
                and commit_lsn[row['tx']] <= point]
            necessary_lsns.add(min(candidates))
            retained_value = value

    # 事务级取舍：拥有任一“影响读数写入”的已提交事务整段保留，其余整段丢弃。
    kept_tx = {records[lsn - 1]['tx'] for lsn in necessary_lsns}
    kept_lsns = [
        lsn for lsn, row in enumerate(records, 1)
        if row.get('op') != 'checkpoint' and row.get('tx') in kept_tx
    ]
    kept_set = set(kept_lsns)
    short_records = [copy.deepcopy(records[lsn - 1]) for lsn in kept_lsns]

    # 位置对照：原 LSN -> 短日志 1 基序号；原断电点 -> 短日志复测点。
    lsn_index = {lsn: idx for idx, lsn in enumerate(kept_lsns, 1)}
    point_mapping = []
    for point in points:
        mapped = sum(1 for lsn in kept_lsns if lsn <= point)
        point_mapping.append({'original_crash_after': point,
                              'short_crash_after': mapped})

    dropped = [
        {'lsn': lsn, 'op': row['op'],
         'tx': row.get('tx'), 'reason': _drop_reason(row, commit_lsn, aborted, kept_set)}
        for lsn, row in enumerate(records, 1) if lsn not in kept_set
    ]

    # 复测：短日志在换算后的复测点上跑同一 WAL 模型，与原账逐点比对。
    checks = []
    reject_reasons = []
    passed = True
    for item in point_mapping:
        original = item['original_crash_after']
        mapped = item['short_crash_after']
        expected = _recover_readings(initial, records, original)
        actual = _recover_readings(initial, short_records, mapped)
        same = expected == actual
        passed = passed and same
        checks.append({'original_crash_after': original,
                       'short_crash_after': mapped,
                       'expected': expected,
                       'actual': actual,
                       'readings_match': same})
        if not same:
            changed = [k for k in expected if actual.get(k) != expected[k]]
            reject_reasons.append(
                f'原断电点{original}（复测点{mapped}）读数不一致：'
                f'键 {", ".join(changed) or "未知"} 的恢复值与原账不同，'
                '说明影响读数的写入被误删，整单退回')

    if short_records:
        validate(initial, short_records)
    minimality = {
        'kept_tx_count': len(kept_tx),
        'kept_entry_count': len(kept_lsns),
        'dropped_entry_count': len(records) - len(kept_lsns),
        'rule': '仅保留在选定断电点上存在影响读数写入的已提交事务，整段取舍；同值覆写不保留',
    }
    source = json.dumps(
        {'initial': initial, 'records': records, 'crash_points': points},
        sort_keys=True, ensure_ascii=False)
    short_payload = json.dumps(short_records, sort_keys=True, ensure_ascii=False)
    return {
        'model_version': MODEL_VERSION,
        'simulation': True,
        'crash_points': points,
        'short_records': short_records,
        'position_map': {
            'lsn_index': lsn_index,
            'crash_points': point_mapping,
        },
        'dropped_entries': dropped,
        'minimality': minimality,
        'checks': checks,
        'reject_reasons': reject_reasons,
        'retest_passed': passed,
        'short_log_sha256': hashlib.sha256(short_payload.encode()).hexdigest(),
        'input_sha256': hashlib.sha256(source.encode()).hexdigest(),
        'boundary': ('Python WAL教学模型；短日志仅做事务级删减，不改写任何条目内容，'
                     '复测与原账使用同一恢复模型。未注入真实PostgreSQL故障。'),
    }


def _drop_reason(row, commit_lsn, aborted, kept_set):
    op = row['op']
    if op == 'checkpoint':
        return '检查点只决定重做起点，不影响恢复读数'
    tx = row.get('tx')
    if tx in aborted:
        return '事务已abort，写入会被撤销，对读数无贡献'
    if tx not in commit_lsn:
        return '事务未提交，恢复时按未结束事务撤销，对读数无贡献'
    return '已提交但在选定断电点上不影响读数：写入被后续提交覆盖，或取值与已可见值相同'
