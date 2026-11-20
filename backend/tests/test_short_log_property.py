"""随机化性质测试：复测恒成立 + 最小化必要性（纯标准库）。

运行：cd backend && python -m tests.test_short_log_property
"""
import random

from src.services.wal_engine import recover, validate
from src.services.short_log import build_short_log


def gen_log(rng, initial):
    """生成满足 wal_engine.validate 约束的随机日志。"""
    keys = list(initial)
    records = []
    active = {}          # tx -> {'writes':[(key,before)], 'locks':set}
    locks = {}
    used = set()
    seq = 0
    target = rng.randint(6, 40)
    while len(records) < target:
        can_commit = bool(active)
        roll = rng.random()
        if roll < 0.28 and not active and records:
            records.append({'op': 'checkpoint'})
            continue
        if roll < 0.62 or not can_commit:
            seq += 1
            tx = f'T{seq}'
            if tx in used:
                continue
            used.add(tx)
            active[tx] = {'writes': [], 'locks': set()}
            records.append({'op': 'begin', 'tx': tx})
            continue
        tx = rng.choice(list(active))
        roll2 = rng.random()
        if roll2 < 0.65:
            free_keys = [k for k in keys if k not in locks or locks[k] == tx]
            if not free_keys:
                continue
            key = rng.choice(free_keys)
            value = rng.randint(-50, 50)
            state = _simulate_state(initial, records)
            active[tx]['writes'].append((key, state[key]))
            active[tx]['locks'].add(key)
            locks[key] = tx
            records.append({'op': 'write', 'tx': tx, 'key': key, 'value': value})
        else:
            op = 'abort' if rng.random() < 0.25 else 'commit'
            if op == 'abort':
                for key, _before in reversed(active[tx]['writes']):
                    pass
            for key in active[tx]['locks']:
                locks.pop(key, None)
            del active[tx]
            records.append({'op': op, 'tx': tx})
    for tx in list(active):
        for key in active[tx]['locks']:
            locks.pop(key, None)
        del active[tx]
        records.append({'op': 'commit', 'tx': tx})
    validate(initial, records)
    return records


def _simulate_state(initial, records):
    state = dict(initial)
    active = {}
    for r in records:
        if r['op'] == 'begin':
            active[r['tx']] = []
        elif r['op'] == 'write':
            active[r['tx']].append((r['key'], state[r['key']]))
            state[r['key']] = r['value']
        elif r['op'] == 'abort':
            for key, before in reversed(active.pop(r['tx'])):
                state[key] = before
        elif r['op'] == 'commit':
            active.pop(r['tx'])
    return state


def main():
    rng = random.Random(20261120)
    for trial in range(120):
        n_keys = rng.randint(1, 4)
        initial = {f'k{i}': rng.randint(-20, 20) for i in range(n_keys)}
        records = gen_log(rng, initial)
        n = len(records)
        points = sorted({rng.randint(0, n) for _ in range(rng.randint(1, 4))})
        result = build_short_log(initial, records, points)

        # 性质1：任何随机样本，复测都必须与原账逐点相同。
        assert result['retest_passed'] is True, f'trial {trial} 复测失败 {result["checks"]}'
        for check in result['checks']:
            expected = recover(initial, records, check['original_crash_after'])['recovered']
            assert check['actual'] == expected

        # 性质2：顺序保持——短日志是原日志的子序列（借助位置对照反查原 LSN）。
        short = result['short_records']
        idx_to_lsn = {idx: lsn for lsn, idx in
                      result['position_map']['lsn_index'].items()}
        orig_lsns = [idx_to_lsn[i] for i in range(1, len(short) + 1)]
        assert orig_lsns == sorted(orig_lsns), f'trial {trial} 短日志顺序破坏'
        for idx, lsn in idx_to_lsn.items():
            assert records[lsn - 1] == short[idx - 1]

        # 性质3：最小化必要性——删掉任一保留事务，至少一个点的读数会变。
        kept_tx = {r['tx'] for r in short if r.get('tx')}
        for victim in kept_tx:
            reduced = [r for r in short if r.get('tx') != victim]
            reduced_orig = [orig_lsns[i] for i, r in enumerate(short)
                            if r.get('tx') != victim]
            for check in result['checks']:
                point = check['original_crash_after']
                mapped = sum(1 for lsn in reduced_orig if lsn <= point)
                actual = (recover(initial, reduced, mapped)['recovered']
                          if reduced else dict(initial))
                if actual != check['expected']:
                    break
            else:
                raise AssertionError(f'trial {trial}: 事务 {victim} 可删而读数不变，非最小')

        # 性质4：幂等签名只取决于日志集输入与断电点集合。
        again = build_short_log(initial, records, list(reversed(points)))
        assert again['input_sha256'] == result['input_sha256']
    print('120 组随机日志全部满足：复测逐点一致 / 顺序保持 / 最小必要 / 签名稳定')


if __name__ == '__main__':
    main()
