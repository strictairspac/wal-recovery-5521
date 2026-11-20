"""短日志最小化与复测的纯逻辑测试（不依赖数据库/Redis）。

运行：cd backend && python -m tests.test_short_log
"""
import copy

from src.services.wal_engine import recover
from src.services.short_log import build_short_log

SEED_RECORDS = [
    {'op': 'begin', 'tx': 'T1'},
    {'op': 'write', 'tx': 'T1', 'key': 'sample_A', 'value': 75},
    {'op': 'commit', 'tx': 'T1'},
    {'op': 'checkpoint'},
    {'op': 'begin', 'tx': 'T2'},
    {'op': 'write', 'tx': 'T2', 'key': 'sample_B', 'value': 40},
    {'op': 'begin', 'tx': 'T3'},
    {'op': 'write', 'tx': 'T3', 'key': 'sample_A', 'value': 60},
    {'op': 'commit', 'tx': 'T3'},
    {'op': 'write', 'tx': 'T2', 'key': 'sample_B', 'value': 35},
    {'op': 'commit', 'tx': 'T2'},
]
SEED_INITIAL = {'sample_A': 100, 'sample_B': 50}


def assert_equal(actual, expected, msg):
    if actual != expected:
        raise AssertionError(f'{msg}: expected {expected!r}, got {actual!r}')


def test_seed_points_0_10_11():
    """种子日志 + 断电点 0/10/11：T1 被检查点固化、T2 在点10未提交、T3 在点10已提交。"""
    result = build_short_log(SEED_INITIAL, SEED_RECORDS, [10, 11, 0])
    assert result['retest_passed'] is True, result['checks']
    kept_tx = {r['tx'] for r in result['short_records'] if 'tx' in r}
    # T1 已在检查点固化，点10/11读数由 durable 携带，无需保留；T2 在点10未提交，点11提交后B=35需保留；T3 点10已提交需保留
    assert_equal(kept_tx, {'T2', 'T3'}, '保留事务集合')
    assert 'checkpoint' not in [r['op'] for r in result['short_records']], '检查点应丢弃'
    # 顺序保持原 LSN
    mapped = {c['original_crash_after']: c['short_crash_after']
              for c in result['position_map']['crash_points']}
    # 点10前保留条目：T3三条(LSN7-9) + T2的begin/write(LSN5,6) = 6；点11再含T2的write/commit共7
    assert_equal(mapped, {0: 0, 10: 6, 11: 7}, '断电点位置对照')
    # 逐点读数与原账一致
    for point in (0, 10, 11):
        expected = recover(SEED_INITIAL, SEED_RECORDS, point)['recovered']
        actual = next(c['actual'] for c in result['checks']
                      if c['original_crash_after'] == point)
        assert_equal(actual, expected, f'点{point}读数')
    # 点10 原账：A60（T3）、B50（T2未提交撤销）
    expected_10 = recover(SEED_INITIAL, SEED_RECORDS, 10)['recovered']
    assert_equal(expected_10, {'sample_A': 60, 'sample_B': 50}, '点10原账')


def test_only_loser_point():
    """只选点10：T2 未提交应整段丢弃，仅保留 T3，且撤销语义不依赖 T2 条目。"""
    result = build_short_log(SEED_INITIAL, SEED_RECORDS, [10])
    assert result['retest_passed'] is True
    kept_tx = {r['tx'] for r in result['short_records'] if 'tx' in r}
    assert_equal(kept_tx, {'T3'}, '点10仅T3可见')
    assert_equal(len(result['short_records']), 3, 'T3 三条')


def test_aborted_transaction_dropped():
    records = [
        {'op': 'begin', 'tx': 'X1'},
        {'op': 'write', 'tx': 'X1', 'key': 'k', 'value': 1},
        {'op': 'abort', 'tx': 'X1'},
        {'op': 'begin', 'tx': 'X2'},
        {'op': 'write', 'tx': 'X2', 'key': 'k', 'value': 2},
        {'op': 'commit', 'tx': 'X2'},
    ]
    result = build_short_log({'k': 0}, records, [6, 3])
    assert result['retest_passed'] is True
    kept_tx = {r['tx'] for r in result['short_records'] if 'tx' in r}
    assert_equal(kept_tx, {'X2'}, 'abort 事务整段丢弃')
    for check in result['checks']:
        assert check['readings_match'] is True


def test_overwrite_chain_minimality():
    """同一键连续三个提交事务，选最后一个点：仅最后事务可见，前两个整段丢弃。"""
    records = []
    for tx, value in (('C1', 1), ('C2', 2), ('C3', 3)):
        records += [{'op': 'begin', 'tx': tx},
                    {'op': 'write', 'tx': tx, 'key': 'k', 'value': value},
                    {'op': 'commit', 'tx': tx}]
    result = build_short_log({'k': 0}, records, [9])
    assert result['retest_passed'] is True
    kept_tx = {r['tx'] for r in result['short_records'] if 'tx' in r}
    assert_equal(kept_tx, {'C3'}, '仅最后可见写入的事务保留')
    assert_equal(result['minimality']['kept_entry_count'], 3, '最小条目数')


def test_earlier_point_keeps_earlier_tx():
    """选两个点时，中间事务在较早点可见，必须保留。"""
    records = []
    for tx, value in (('C1', 1), ('C2', 2), ('C3', 3)):
        records += [{'op': 'begin', 'tx': tx},
                    {'op': 'write', 'tx': tx, 'key': 'k', 'value': value},
                    {'op': 'commit', 'tx': tx}]
    result = build_short_log({'k': 0}, records, [3, 9])
    assert result['retest_passed'] is True
    kept_tx = {r['tx'] for r in result['short_records'] if 'tx' in r}
    # C1 在点3可见；C2 在任何点都不是最后写入（点9被C3覆盖）→ C2 丢弃；C3 点9可见
    assert_equal(kept_tx, {'C1', 'C3'}, '多点并集保留')


def test_all_dropped_empty_short_log():
    """所有事务都未提交/abort 时短日志为空，复测仍须与原账逐点相同。"""
    records = [
        {'op': 'begin', 'tx': 'L1'},
        {'op': 'write', 'tx': 'L1', 'key': 'k', 'value': 9},
    ]
    result = build_short_log({'k': 7}, records, [2, 0])
    assert result['retest_passed'] is True
    assert_equal(result['short_records'], [], '空短日志')
    for check in result['checks']:
        assert_equal(check['actual'], {'k': 7}, '空短日志恢复为初始账本')
        assert check['readings_match'] is True


def test_same_value_overwrite_dropped():
    """同值覆写不影响读数：后一事务写入与已提交值相同，应整段删除且读数不变。"""
    records = [
        {'op': 'begin', 'tx': 'S1'},
        {'op': 'write', 'tx': 'S1', 'key': 'k', 'value': 40},
        {'op': 'commit', 'tx': 'S1'},
        {'op': 'begin', 'tx': 'S2'},
        {'op': 'write', 'tx': 'S2', 'key': 'k', 'value': 40},
        {'op': 'commit', 'tx': 'S2'},
    ]
    result = build_short_log({'k': 16}, records, [6])
    assert result['retest_passed'] is True
    kept_tx = {r['tx'] for r in result['short_records'] if r.get('tx')}
    assert_equal(kept_tx, {'S1'}, '同值覆写事务 S2 应删除')
    assert_equal(result['checks'][0]['actual'], {'k': 40}, '读数仍为40')


def test_same_value_but_needed_via_other_key():
    """S2 对 k 是同值覆写（对 k 不影响读数），但同事务对 j 是首写；
    若选点落在 S3 覆写 k 之前，S2 因 j 而成为必要事务，整段保留。"""
    records = [
        {'op': 'begin', 'tx': 'S1'},
        {'op': 'write', 'tx': 'S1', 'key': 'k', 'value': 40},
        {'op': 'commit', 'tx': 'S1'},
        {'op': 'begin', 'tx': 'S2'},
        {'op': 'write', 'tx': 'S2', 'key': 'k', 'value': 40},
        {'op': 'write', 'tx': 'S2', 'key': 'j', 'value': 5},
        {'op': 'commit', 'tx': 'S2'},
        {'op': 'begin', 'tx': 'S3'},
        {'op': 'write', 'tx': 'S3', 'key': 'k', 'value': 7},
        {'op': 'commit', 'tx': 'S3'},
    ]
    initial = {'k': 16, 'j': 0}
    # 只选末点10：S1 的 k 被 S3 覆盖而删除；S2 凭 j 的首写（5≠初始0）保留
    result = build_short_log(initial, records, [10])
    assert result['retest_passed'] is True
    kept = {r['tx'] for r in result['short_records'] if r.get('tx')}
    assert_equal(kept, {'S2', 'S3'}, '末点 S1 被覆盖删除，S2 凭 j 保留')
    # 把 j 改成同值首写（j 初始即5）后，S2 对两个键都不影响读数，整段删除
    records2 = [dict(r) for r in records]
    result2 = build_short_log({'k': 16, 'j': 5}, records2, [10])
    kept2 = {r['tx'] for r in result2['short_records'] if r.get('tx')}
    assert_equal(kept2, {'S3'}, 'S1 被覆盖、S2 全同值，均整段删除')
    assert result2['retest_passed'] is True
    assert_equal(result2['checks'][0]['actual'], {'k': 7, 'j': 5}, '读数不变')


def test_idempotent_input_hash():
    r1 = build_short_log(SEED_INITIAL, SEED_RECORDS, [10, 0])
    r2 = build_short_log(SEED_INITIAL, SEED_RECORDS, [0, 10])
    assert_equal(r1['input_sha256'], r2['input_sha256'], '断电点顺序不影响签名')
    r3 = build_short_log(SEED_INITIAL, SEED_RECORDS, [11])
    assert r1['input_sha256'] != r3['input_sha256'], '换断电点签名不同'


def test_position_map_consistency():
    result = build_short_log(SEED_INITIAL, SEED_RECORDS, [10, 11])
    short = result['short_records']
    lsn_index = result['position_map']['lsn_index']
    # 每个保留原 LSN 的序号与短日志条目对应
    for lsn, idx in lsn_index.items():
        assert_equal(short[idx - 1], SEED_RECORDS[lsn - 1], f'LSN {lsn} 对照一致')
    # 复测点不超过短日志长度
    for item in result['position_map']['crash_points']:
        assert 0 <= item['short_crash_after'] <= len(short)


def test_invalid_point_rejected():
    for bad in ([12], [-1]):
        try:
            build_short_log(SEED_INITIAL, SEED_RECORDS, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f'非法断电点 {bad} 应拒绝')
    try:
        build_short_log(SEED_INITIAL, SEED_RECORDS, [])
    except ValueError:
        pass
    else:
        raise AssertionError('空断电点应拒绝')


def test_multi_key_interleaving():
    """两键交错事务，验证整段保留与逐点读数。"""
    records = [
        {'op': 'begin', 'tx': 'M1'},
        {'op': 'write', 'tx': 'M1', 'key': 'a', 'value': 1},
        {'op': 'begin', 'tx': 'M2'},
        {'op': 'write', 'tx': 'M2', 'key': 'b', 'value': 2},
        {'op': 'commit', 'tx': 'M1'},
        {'op': 'commit', 'tx': 'M2'},
    ]
    result = build_short_log({'a': 0, 'b': 0}, records, [5, 6])
    assert result['retest_passed'] is True
    # 点5：M1已提交可见，M2未提交；点6：两者均提交 → 并集都保留
    kept_tx = {r['tx'] for r in result['short_records'] if 'tx' in r}
    assert_equal(kept_tx, {'M1', 'M2'}, '交错事务并集')
    # 原顺序保持
    assert [r['tx'] for r in result['short_records']] == \
           [r['tx'] for r in records if r.get('tx') in kept_tx]


def test_reject_reasons_collected_on_mismatch(monkeypatch=None):
    """模拟复测不一致：retest_passed 为 False 且给出逐点退回原因、不产生短日志发放语义。"""
    from src.services import short_log as mod
    original = mod.recover

    def fake_recover(initial, records, crash_after):
        out = original(initial, records, crash_after)
        # 短日志重放时篡改恢复值，制造与原账的读数差异
        if records is not SEED_RECORDS and records:
            out = dict(out)
            out['recovered'] = dict(out['recovered'])
            first_key = next(iter(out['recovered']))
            out['recovered'][first_key] += 1
        return out

    mod.recover = fake_recover
    try:
        result = mod.build_short_log(SEED_INITIAL, SEED_RECORDS, [10])
    finally:
        mod.recover = original
    assert result['retest_passed'] is False
    assert result['reject_reasons'], '必须给出退回原因'
    assert any('原断电点10' in reason for reason in result['reject_reasons'])
    assert any(not c['readings_match'] for c in result['checks'])


if __name__ == '__main__':
    tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in tests:
        fn()
        print(f'PASS {fn.__name__}')
    print(f'\n全部 {len(tests)} 项测试通过')
